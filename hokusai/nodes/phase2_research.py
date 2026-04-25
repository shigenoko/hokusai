"""
Phase 2: 事前調査

- /task-research スキルを実行（Notion書き込みツール遮断）
- 既存実装の調査
- 影響範囲の特定
- 詳細設計書の作成
- 調査レポートをNotionに子ページとして保存（hokusaiが唯一の書き込み経路）

Note:
    スキル側は Notion 読み取りのみ許可（書き込みは --disallowed-tools で遮断）。
    スキル出力（raw_output）を hokusai が子ページとして保存し、
    state["research_result"] には抽出後のレポートを格納する。
"""

from ..config import get_config
from ..integrations.claude_code import ClaudeCodeClient
from ..logging_config import get_logger
from ..state import (
    PhaseStatus,
    WorkflowState,
    add_audit_log,
    should_skip_phase,
    update_phase_status,
)
from ..utils.cross_review import execute_cross_review
from ..utils.notion_helpers import save_to_subpage_or_create
from ..utils.output_parser import _find_prefix_heading, extract_markdown_section
from ..utils.phase_page_templates import (
    build_phase_page_content,
    initialize_phase_page_state,
)

logger = get_logger("phase2")

# スキル実行時に遮断する Notion 書き込み系ツール
NOTION_WRITE_TOOLS = [
    "mcp__notion__notion-update-page",
    "mcp__notion__notion-create-pages",
    "mcp__notion__notion-create-comment",
]

def phase2_research_node(state: WorkflowState) -> WorkflowState:
    """Phase 2: 事前調査"""

    # スキップチェック
    if should_skip_phase(state, 2):
        print("⏭️  Phase 2 スキップ: 事前調査済み")
        return state

    state = update_phase_status(state, 2, PhaseStatus.IN_PROGRESS)

    try:
        config = get_config()
        claude = ClaudeCodeClient()

        # [1/3] 直接プロンプトで調査を実行（Notion書き込み遮断）
        print("📋 Phase 2 [1/3] 調査実行中...")
        research_prompt = _build_task_research_prompt(state["task_url"])
        logger.info(f"直接プロンプトで調査を実行: {state['task_url']}")
        raw_output = claude.execute_prompt(
            prompt=research_prompt,
            timeout=config.skill_timeout,
            allow_mcp_tools=True,
            allow_file_operations=True,
            disallowed_tools=NOTION_WRITE_TOOLS,
        )

        # raw_output がフルレポートであることを検証（サマリー出力を正本として保存しない）
        # 検証失敗時は 1 回だけ再生成を試みる
        try:
            _validate_research_output(raw_output)
        except RuntimeError as e:
            logger.warning(f"Phase 2 初回出力が検証失敗、再生成を試みます: {e}")
            state = add_audit_log(
                state, 2, "research_output_retry", "warning",
                details={
                    "reason": str(e),
                    "attempt": 2,
                    "first_output_length": len(raw_output) if raw_output else 0,
                },
            )
            print("📋 Phase 2 [1/3] 出力検証失敗、再生成中...")
            retry_prompt = _build_research_retry_prompt(
                task_url=state["task_url"],
                previous_output=raw_output,
                validation_error=str(e),
            )
            raw_output = claude.execute_prompt(
                prompt=retry_prompt,
                timeout=config.skill_timeout,
                allow_mcp_tools=True,
                allow_file_operations=True,
                disallowed_tools=NOTION_WRITE_TOOLS,
            )
            # 2回目も失敗した場合は fail-fast
            _validate_research_output(raw_output)
            logger.info("Phase 2 再生成で出力検証を通過しました")

        # 調査レポート部分のみを抽出
        research_output = _extract_research_report(raw_output)
        logger.debug(f"調査出力: {len(raw_output)}文字 → 抽出後: {len(research_output)}文字")
        state["research_result"] = research_output

        # 結果からスキーマ変更の有無を判定
        state["schema_change_required"] = _detect_schema_change(raw_output)

        # Phase page metadata を初期化（表示状態はテンプレート側で導出）
        initialize_phase_page_state(state, 2)

        phase_page_content = build_phase_page_content(
            state=state,
            phase=2,
            latest_document=raw_output,
            source_phase="phase2_research",
        )

        # [2/3] Notionに調査レポートを子ページとして保存
        print("📋 Phase 2 [2/3] 子ページ保存中...")
        state = save_to_subpage_or_create(
            state, state["task_url"], phase=2, content=phase_page_content,
            workflow_id=state.get("workflow_id", ""),
        )

        # 保存後検証: 子ページ本文が空でなく、アンカーを含むことを確認
        _verify_subpage_content(state, raw_output)

        # [3/3] Codex クロスレビュー（設定で有効時のみ）
        # 子ページ作成後に実行することで、callout が子ページに保存される
        print("📋 Phase 2 [3/3] クロスレビュー...")
        state = execute_cross_review(state, research_output, phase=2)

        # blockモードで停止要求がある場合は、このフェーズを完了扱いにしない
        if state.get("waiting_for_human"):
            state = update_phase_status(state, 2, PhaseStatus.FAILED, "cross_review_blocked")
            state = add_audit_log(
                state, 2, "cross_review_blocked", "failed",
                details={"reason": state.get("human_input_request")},
            )
            print("🛑 Phase 2 停止: クロスLLMレビューで確認が必要です")
            return state

        # 事後検証: 子ページが確実に作成されていることを確認
        _verify_notion_state(state)

        state = update_phase_status(state, 2, PhaseStatus.COMPLETED)
        state = add_audit_log(state, 2, "task_research_completed", "success", {
            "schema_change_required": state["schema_change_required"],
            "research_output_length": len(research_output),
        })

        schema_msg = "スキーマ変更が必要です" if state["schema_change_required"] else "スキーマ変更は不要です"
        print(f"✅ Phase 2 完了: 事前調査が完了しました。{schema_msg}")

    except Exception as e:
        state = update_phase_status(state, 2, PhaseStatus.FAILED, str(e))
        state = add_audit_log(state, 2, "phase_failed", "error", error=str(e))
        print(f"❌ Phase 2 失敗: {e}")
        raise

    return state


def _verify_notion_state(state: WorkflowState) -> None:
    """Phase 2 完了前に Notion 状態を検証

    検証項目:
    1. 子ページ URL が state["phase_subpages"][2] に存在すること

    失敗時は RuntimeError を送出。

    Note:
        現在は state ベースの検証のみ。実ページの存在確認や
        親タスクページへの本文混入チェック（notion-fetch による再取得）は
        追加 LLM 呼び出しコストが高いため後続対応とする。
    """
    subpage_url = state.get("phase_subpages", {}).get(2)
    if not subpage_url:
        raise RuntimeError(
            "Phase 2 検証失敗: 子ページが作成されていません。"
            "state['phase_subpages'][2] が未設定です。"
        )
    logger.info(f"Phase 2 検証OK: 子ページ={subpage_url}")


def _verify_subpage_content(state: WorkflowState, raw_output: str) -> None:
    """子ページ本文が確実に保存されていることを検証

    検証項目:
    1. state["phase_subpages"][2] が存在する
    2. 子ページ本文が空でない
    3. 子ページ本文の長さが raw_output の 30% 以上（要約検知）
    4. raw_output 中盤から抽出したアンカーが子ページに含まれる（見出しだけの一致を除外）

    失敗時は RuntimeError を送出し、Phase 2 を失敗扱いにする。
    """
    import os

    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        return

    subpage_url = state.get("phase_subpages", {}).get(2)
    if not subpage_url:
        raise RuntimeError(
            "Phase 2 本文検証失敗: 子ページURLが未登録です。"
        )

    # 子ページ本文を取得（リトライあり: Notion MCP タイムアウト対策）
    import time

    from ..integrations.notion_mcp import NotionMCPClient

    max_attempts = 3
    retry_delay = 5  # 秒
    page_content = None
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            notion = NotionMCPClient()
            page_content = notion.get_page_content(subpage_url)
            break
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                logger.warning(
                    f"Phase 2 本文取得リトライ {attempt}/{max_attempts}: {e}"
                )
                time.sleep(retry_delay)
            else:
                raise RuntimeError(
                    f"Phase 2 本文検証失敗: 子ページ本文の取得に失敗しました"
                    f" ({max_attempts}回試行): {last_error}"
                )

    if not page_content or not page_content.strip():
        raise RuntimeError(
            "Phase 2 本文検証失敗: 子ページ本文が空です。"
        )

    # 長さ比率チェック: 子ページ本文が raw_output の 30% 未満なら要約と判断
    raw_len = len(raw_output.strip())
    page_len = len(page_content.strip())
    if raw_len > 0 and page_len < raw_len * 0.3:
        raise RuntimeError(
            f"Phase 2 本文検証失敗: 子ページ本文が短すぎます。"
            f" raw={raw_len}文字, page={page_len}文字"
            f" (比率={page_len / raw_len:.0%}, 閾値=30%)"
        )

    # 本文中盤アンカーチェック: 見出しだけでなく実コンテンツが存在するか検証
    anchor = _pick_body_anchor(raw_output)
    if anchor and anchor not in page_content:
        raise RuntimeError(
            f"Phase 2 本文検証失敗: 子ページに本文アンカーが見つかりません。"
            f" アンカー={anchor[:60]!r}"
        )

    logger.info(
        f"Phase 2 本文検証OK: 子ページに本文が保存されています"
        f" (raw={raw_len}, page={page_len})"
    )


def _pick_body_anchor(raw_output: str) -> str | None:
    """raw_output の中盤から非見出し行を抽出してアンカーにする。

    見出し行だけだとサマリーでも一致してしまうため、
    本文の実コンテンツ行（中盤付近）を選ぶ。
    """
    lines = raw_output.strip().splitlines()
    # 空行・見出し行・短すぎる行を除外した実コンテンツ行を収集
    content_lines = [
        line.strip()
        for line in lines
        if line.strip()
        and not line.strip().startswith("#")
        and len(line.strip()) >= 10
    ]
    if not content_lines:
        return None
    # 中盤（50%地点）の行を選択
    mid_idx = len(content_lines) // 2
    return content_lines[mid_idx][:80]


_RESEARCH_START_MARKERS = [
    "## 事前調査結果",
    "## 事前調査レポート",
    "### 事前調査結果",
    "### 事前調査レポート",
]


# フルレポートに必須のセクション見出し（4つ中3つ以上必要）
_REQUIRED_SECTIONS = [
    "### タスク概要",
    "### 1. 現状",
    "### 2. 関連エンティティ仕様",
    "### 3. 必要な変更",
]

# サマリー専用見出し（これらのみで構成されている場合は拒否）
_SUMMARY_ONLY_HEADINGS = [
    "調査結果のサマリー",
    "調査結果サマリー",
    "要点まとめ",
]

# 禁止メタ説明（含まれていたら拒否）
_FORBIDDEN_META_PHRASES = [
    "手動でNotionに貼り付け",
    "Notion MCPにはページコンテンツ更新ツールがない",
    "手動で貼り付けてください",
    "Notion追記について",
]


def _validate_research_output(raw_output: str) -> None:
    """raw_output がフルレポートであることを検証（サマリー出力を正本として保存しない）

    検証条件:
    1. 許可開始見出しで始まること（前置き文の混入を防止）
    2. 必須セクション見出しを 4 つ中 3 つ以上含むこと
    3. サマリー専用見出しのみで構成されていないこと
    4. 禁止メタ説明を含まないこと

    失敗時は RuntimeError を送出し、Phase 2 を fail-fast にする。
    """
    if not raw_output or not raw_output.strip():
        raise RuntimeError(
            "Phase 2 出力検証失敗: raw_output が空です。"
        )

    # 前置き文チェック: 許可開始見出しで始まること
    stripped = raw_output.strip()
    starts_with_marker = any(stripped.startswith(m) for m in _RESEARCH_START_MARKERS)
    if not starts_with_marker:
        first_line = stripped.split("\n", 1)[0][:80]
        raise RuntimeError(
            f"Phase 2 出力検証失敗: 許可開始見出しで始まっていません。"
            f" 先頭行={first_line!r}"
        )

    # 禁止メタ説明チェック
    for phrase in _FORBIDDEN_META_PHRASES:
        if phrase in raw_output:
            raise RuntimeError(
                f"Phase 2 出力検証失敗: 禁止メタ説明を含んでいます。"
                f" phrase={phrase!r}"
            )

    # 必須セクション数チェック
    section_count = sum(1 for s in _REQUIRED_SECTIONS if s in raw_output)
    has_summary_heading = any(h in raw_output for h in _SUMMARY_ONLY_HEADINGS)

    if section_count < 3:
        detail = f"必須セクション={section_count}/4 (閾値=3)"
        if has_summary_heading:
            detail += ", サマリー見出しを検出"
        raise RuntimeError(
            f"Phase 2 出力検証失敗: フルレポートの必須セクションが不足しています。"
            f" {detail}"
        )

    logger.info(f"Phase 2 出力検証OK: 必須セクション={section_count}/4")


def _extract_research_report(output: str) -> str:
    """スキル出力から調査レポート部分のみを抽出

    /task-research の出力フォーマットは揺れがあるため、
    複数の開始マーカーを試行する。抽出に失敗した場合は
    会話文の丸ごと保存を防ぐため空文字列を返す。

    Note:
        出力全体が正しいレポート（前置きなしで見出し開始）の場合、
        extract_markdown_section は result == output を返す。
        この場合はマーカー存在チェックで有効なレポートと判定する。
    """
    result = extract_markdown_section(
        output,
        start_markers=_RESEARCH_START_MARKERS,
        end_markers=["Generated by"],
    )
    if result == output:
        # 出力全体が返された場合:
        # 出力内に許可見出しが存在すれば、前置きなしの正当なレポート
        if _find_prefix_heading(output, _RESEARCH_START_MARKERS):
            return result
        logger.warning(
            "調査レポートの抽出マーカーが見つかりません（保存スキップ）: "
            f"出力先頭={output[:80]!r}"
        )
        return ""
    return result


def _detect_schema_change(raw_output: str) -> bool:
    """調査出力からスキーマ変更の有無を検出"""
    config = get_config()
    output_lower = raw_output.lower()
    keywords = config.schema_change_keywords
    return any(keyword.lower() in output_lower for keyword in keywords)


def _build_research_retry_prompt(
    task_url: str,
    previous_output: str,
    validation_error: str,
) -> str:
    """検証失敗時の再生成プロンプトを構築する

    前回出力が仕様違反だったことを明示し、元のタスク情報と調査手順を
    再注入した上で全文を規定フォーマットで再出力させる。
    """
    from ..prompts import get_prompt

    return get_prompt(
        "phase2.task_research_retry",
        task_url=task_url,
        previous_output=previous_output,
        validation_error=validation_error,
    )


def _build_task_research_prompt(task_url: str) -> str:
    """Phase 2 調査用の直接プロンプトを構築する

    slash skill ではなく execute_prompt() で直接実行するためのプロンプト。
    出力フォーマットをプロンプト本文に埋め込むことで、
    前置き文や要約の混入を防ぐ。
    """
    from ..prompts import get_prompt

    return get_prompt("phase2.task_research", task_url=task_url)
