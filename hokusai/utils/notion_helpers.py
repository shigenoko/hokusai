"""
Notion保存ヘルパーユーティリティ

NotionタスクページへのコンテンツMCP経由保存を共通化する。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING

from ..constants import CALLOUT_CROSS_REVIEW, CALLOUT_PULL_REQUESTS, PHASE_NAMES
from ..logging_config import get_logger
from .phase_page_templates import (
    PHASE_PAGE_DOCUMENT_STATE_KEYS,
    PHASE_PAGE_SOURCE_PHASES,
    build_phase_page_content,
)

if TYPE_CHECKING:
    from ..state import WorkflowState

logger = get_logger("notion_helpers")




def build_callout(icon: str, color: str, title: str, body_lines: list[str]) -> str:
    """Notion calloutブロックを統一フォーマットで組み立てる。

    Args:
        icon: Calloutアイコン（絵文字）
        color: Callout背景色（例: "blue_bg", "green_bg"）
        title: Calloutタイトル（太字で表示）
        body_lines: 本文行のリスト（箇条書き形式推奨: ``- **Key:** value``）
    """
    lines = [
        f'::: callout {{icon="{icon}" color="{color}"}}',
        f"**{title}**",
        "",
        *body_lines,
        ":::",
    ]
    return "\n".join(lines)


def save_content_to_notion(
    task_url: str,
    content: str,
    after_marker: str | None = None,
) -> None:
    """
    コンテンツをNotionタスクページに保存（MCP経由）

    環境変数 HOKUSAI_SKIP_NOTION が設定されている場合は保存をスキップする。
    コンテンツが空の場合も保存をスキップする。
    保存失敗は致命的ではないため、警告のみを出力する。

    Args:
        task_url: NotionタスクURL
        content: 保存するコンテンツ（Markdown形式）
        after_marker: 既存コンテンツ内でこのマーカーの後に挿入する。
            Noneの場合は既存コンテンツの末尾に追記。
    """
    # Notionスキップフラグをチェック
    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        logger.info("Notion接続スキップモード: コンテンツの保存をスキップ")
        print("⏭️  Notion接続なし: コンテンツの自動保存をスキップ")
        return

    if not content or not content.strip():
        logger.warning("コンテンツが空のためNotionへの保存をスキップ")
        return

    try:
        from ..integrations.notion_mcp import NotionMCPClient

        notion = NotionMCPClient()
        success = notion.insert_after_existing(
            task_url, content, after_marker=after_marker,
        )

        if success:
            logger.info("コンテンツをNotionに保存しました（MCP経由）")
            print("📝 コンテンツをNotionに保存しました")
        else:
            logger.warning("Notionへの保存に失敗しました（insert_after_existing が False を返却）")
            print("⚠️  Notionへの保存に失敗しました")

    except ImportError as e:
        logger.warning(f"Notion MCPクライアントのインポートに失敗: {e}")
        print("⚠️  Notionへの保存をスキップ（インポートエラー）")
    except Exception as e:
        # Notion保存失敗は致命的ではないので警告のみ
        logger.warning(f"Notionへの保存に失敗: {e}")
        print(f"⚠️  Notionへの保存に失敗: {e}")


def create_phase_subpage(
    task_url: str,
    phase: int,
    title: str,
    content: str,
) -> str | None:
    """タスクページの子ページとしてフェーズ出力を作成

    Args:
        task_url: NotionタスクページURL
        phase: Phase番号
        title: 子ページタイトル
        content: 子ページの本文（Markdown）

    Returns:
        作成された子ページの URL。失敗時は None。
    """
    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        logger.info("Notion接続スキップモード: 子ページ作成をスキップ")
        return None

    if not content or not content.strip():
        logger.warning("コンテンツが空のため子ページ作成をスキップ")
        return None

    try:
        import re

        from ..integrations.notion_mcp import NotionMCPClient

        notion = NotionMCPClient()
        page_id = notion._extract_page_id(task_url)

        # Step 1: タイトルのみで子ページを作成（本文は空）
        create_prompt = f"""以下のNotionページに子ページを作成してください。

親ページID: {page_id}

子ページのタイトル: {title}

手順:
1. mcp__notion__notion-create-pages ツールを使用して子ページを作成
   - parent: {{ "page_id": "{page_id}" }}
   - pages: [{{ "properties": {{ "title": "{title}" }} }}]
2. 作成されたページのURLを出力

重要: 本文は空のままにしてください。タイトルのみ設定すること。

成功したら「作成完了: <URL>」と出力してください（URLは必ず含めること）。
失敗したら「作成失敗: <理由>」と出力してください。
"""

        result = notion.claude.execute_prompt(create_prompt, timeout=120, allow_mcp_tools=True)

        # URLを抽出（第一候補: 完全なNotion URL）
        url = None
        url_match = re.search(r"https://(?:www\.)?notion\.so/[^\s)\"]+", result)
        if url_match:
            url = url_match.group(0)
        elif "作成完了" in result or "成功" in result or "created" in result.lower():
            # URL抽出失敗だが作成自体は成功した場合: ページIDからURL構築を試行
            parent_hex = page_id.replace("-", "")
            hex_matches = re.findall(r"[a-f0-9]{32}", result, re.IGNORECASE)
            child_ids = [h for h in hex_matches if h.lower() != parent_hex.lower()]
            if child_ids:
                url = f"https://www.notion.so/{child_ids[0]}"
                logger.info(f"Phase {phase} 子ページ作成成功（IDからURL構築）: {url}")

        if not url:
            logger.warning(f"子ページ作成失敗: {result[:500]}")
            print(f"⚠️  Phase {phase} 子ページ作成に失敗しました")
            return None

        # Step 2: 本文を書き込み（作成と分離して忠実度を向上）
        if not update_subpage_content(url, content):
            logger.warning(f"Phase {phase} 子ページ本文の書き込みに失敗: {url}")
            print(f"⚠️  Phase {phase} 子ページ本文の書き込みに失敗しました")
            return None

        logger.info(f"Phase {phase} 子ページ作成成功: {url}")
        print(f"📄 Phase {phase} 子ページを作成しました")
        return url

    except ImportError as e:
        logger.warning(f"Notion MCPクライアントのインポートに失敗: {e}")
        return None
    except Exception as e:
        logger.warning(f"子ページ作成に失敗: {e}")
        print(f"⚠️  Phase {phase} 子ページ作成に失敗: {e}")
        return None


def update_subpage_content(page_url: str, content: str) -> bool:
    """既存子ページの内容を上書き更新（冪等性確保）

    Args:
        page_url: 子ページURL
        content: 上書きするコンテンツ（Markdown）

    Returns:
        成功した場合 True
    """
    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        logger.info("Notion接続スキップモード: 子ページ更新をスキップ")
        return False

    if not content or not content.strip():
        logger.warning("コンテンツが空のため子ページ更新をスキップ")
        return False

    try:
        from ..integrations.notion_mcp import NotionMCPClient

        notion = NotionMCPClient()
        page_id = notion._extract_page_id(page_url)
        escaped_content = content.replace('"""', '\\"\\"\\"')

        prompt = f"""以下のNotionページの本文を上書き更新してください。

ページID: {page_id}

新しい本文:
\"\"\"
{escaped_content}
\"\"\"

手順:
1. mcp__notion__notion-update-page ツールを使用してページの本文を置き換え
   - page_id: "{page_id}"
   - command: "replace_content"
   - new_str: 上記の本文

重要:
- ページのタイトルは変更しないこと
- 本文全体を新しい内容で置き換えること

成功したら「更新完了」、失敗したら「更新失敗: <理由>」と出力してください。
"""

        result = notion.claude.execute_prompt(prompt, timeout=300, allow_mcp_tools=True)

        if "更新完了" in result or "成功" in result or "updated" in result.lower():
            logger.info(f"子ページ更新成功: {page_id}")
            return True

        logger.warning(f"子ページ更新失敗: {result[:200]}")
        return False

    except Exception as e:
        logger.debug(f"子ページ更新に失敗: {e}")
        return False


def append_to_subpage(page_url: str, content: str) -> bool:
    """子ページの末尾にコンテンツを追記（cross-review callout 用）

    Args:
        page_url: 子ページURL
        content: 追記するコンテンツ（Markdown）

    Returns:
        成功した場合 True
    """
    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        logger.info("Notion接続スキップモード: 子ページ追記をスキップ")
        return False

    if not content or not content.strip():
        logger.warning("コンテンツが空のため子ページ追記をスキップ")
        return False

    try:
        from ..integrations.notion_mcp import NotionMCPClient

        notion = NotionMCPClient()
        success = notion.append_content(page_url, content)
        if success:
            logger.info("子ページへの追記成功")
        else:
            logger.warning("子ページへの追記失敗")
        return success

    except Exception as e:
        logger.warning(f"子ページ追記に失敗: {e}")
        return False


def sync_phase_page_from_state(state: WorkflowState, phase: int) -> bool:
    """state からフェーズページ本文を再生成して子ページへ同期する。"""
    subpage_url = state.get("phase_subpages", {}).get(phase)
    if not subpage_url:
        return False

    document_key = PHASE_PAGE_DOCUMENT_STATE_KEYS.get(phase)
    if not document_key:
        return False

    latest_document = state.get(document_key)
    if not latest_document:
        return False

    content = build_phase_page_content(
        state=state,
        phase=phase,
        latest_document=latest_document,
        source_phase=PHASE_PAGE_SOURCE_PHASES.get(phase, "phase_node"),
    )
    return update_subpage_content(subpage_url, content)


def save_to_subpage_or_create(
    state: WorkflowState,
    task_url: str,
    phase: int,
    content: str,
    workflow_id: str = "",
) -> WorkflowState:
    """子ページに保存（冪等性チェック付き）

    state["phase_subpages"][phase] が存在すれば上書き更新、
    なければ新規作成してリンクを追記する。

    Args:
        state: ワークフロー状態
        task_url: タスクページURL
        phase: Phase番号
        content: 保存するコンテンツ（Markdown）
        workflow_id: ワークフローID（子ページタイトルに含める）

    Returns:
        更新されたワークフロー状態
    """
    phase_name = PHASE_NAMES.get(phase, f"Phase {phase}")
    title = f"Phase {phase}: {phase_name}"

    existing_url = state.get("phase_subpages", {}).get(phase)
    if existing_url:
        # 冪等性: 既存子ページを上書き更新
        success = update_subpage_content(existing_url, content)
        if success:
            logger.info(f"Phase {phase} 子ページを上書き更新: {existing_url}")
            return state
        # 更新失敗（削除済みページ等）→ 既存URLを無効化して新規作成へ
        logger.warning(
            f"Phase {phase} 子ページの上書き更新に失敗（削除済みの可能性）: {existing_url}"
            " → 既存URLを無効化して新規作成に切替"
        )
        if "phase_subpages" in state:
            del state["phase_subpages"][phase]

    # 新規作成
    url = create_phase_subpage(task_url, phase, title, content)
    if url:
        if "phase_subpages" not in state:
            state["phase_subpages"] = {}
        state["phase_subpages"][phase] = url
        logger.info(f"Phase {phase} 子ページを新規作成: {url}")
    else:
        raise RuntimeError(
            f"Phase {phase} 子ページの作成に失敗しました。"
            "Notion接続を確認してください。"
        )

    return state


def update_notion_checkboxes(state: WorkflowState, completed_steps: list[str]) -> None:
    """Notionのチェックボックスを更新"""
    try:
        from ..integrations.factory import get_task_client

        task_client = get_task_client()

        # チェックボックス更新メソッドがあるか確認
        if hasattr(task_client, "update_checkboxes"):
            logger.info(f"Notionチェックボックス更新: {completed_steps}")
            task_client.update_checkboxes(
                task_url=state["task_url"],
                completed_items=completed_steps,
                section_hint="開発計画",
            )
        else:
            logger.debug("タスククライアントにupdate_checkboxesメソッドがありません")

    except Exception as e:
        # チェックボックス更新の失敗は致命的ではないのでログのみ
        logger.warning(f"Notionチェックボックス更新エラー: {e}")
        print(f"⚠️ チェックボックス更新をスキップ: {e}")


def generate_cross_review_callout(review_result: dict, phase: int) -> str:
    """クロスLLMレビュー結果の Notion Callout を生成

    Args:
        review_result: レビュー結果辞書（findings, overall_assessment, summary等）
        phase: Phase番号（2 or 4）
    """
    from ..constants import PHASE_NAMES

    phase_name = PHASE_NAMES.get(phase, f"Phase {phase}")
    assessment = review_result.get("overall_assessment", "unknown")
    summary = review_result.get("summary", "")
    confidence = review_result.get("confidence_score")
    findings = review_result.get("findings", [])

    body_lines: list[str] = []
    body_lines.append(f"- **Phase:** {phase} ({phase_name})")
    body_lines.append(f"- **Assessment:** {assessment}")
    if confidence is not None:
        body_lines.append(f"- **Confidence:** {confidence:.0%}")
    body_lines.append(f"- **Summary:** {summary}")

    if findings:
        body_lines.append("")
        body_lines.append("**Findings:**")
        for f in findings:
            severity = f.get("severity", "info")
            title = f.get("title", "")
            body_lines.append(f"- [{severity}] {title}")

    return build_callout(**CALLOUT_CROSS_REVIEW, body_lines=body_lines)


def generate_pr_callout(pull_requests: list) -> str:
    """PR情報のcalloutを生成（Notion先頭追記用）"""
    if not pull_requests:
        return ""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    body_lines: list[str] = []

    for pr in pull_requests:
        repo_name = pr.get("repo_name", "")
        number = pr.get("number", 0)
        url = pr.get("url", "")

        if repo_name:
            body_lines.append(f"- **{repo_name}:** [PR #{number}]({url})")
        else:
            body_lines.append(f"- [PR #{number}]({url})")

    body_lines.append("")
    body_lines.append(f"- **Created:** {now}")

    return build_callout(**CALLOUT_PULL_REQUESTS, body_lines=body_lines)


def record_pr_callout_to_notion(state: "WorkflowState", phase: int) -> "WorkflowState":
    """PR情報をNotionタスクページに記録する（差分がある場合のみ）。

    state["notion_recorded_pr_count"] と現在の pull_requests 数を比較し、
    増加している場合のみ Notion に callout を prepend する。

    Args:
        state: ワークフロー状態
        phase: 呼び出し元のフェーズ番号（監査ログ用）

    Returns:
        更新された state
    """
    from ..integrations.factory import get_task_client
    from ..state import add_audit_log

    pull_requests = state.get("pull_requests", [])
    recorded_count = state.get("notion_recorded_pr_count", 0)

    if len(pull_requests) <= recorded_count or not pull_requests:
        return state

    try:
        task_client = get_task_client()
        pr_callout = generate_pr_callout(pull_requests)
        if pr_callout:
            result = task_client.prepend_content(state["task_url"], pr_callout)
            if hasattr(result, "result"):
                state = add_audit_log(
                    state, phase, "notion_prepend_pr_callout", result.result.value,
                    error=result.reason,
                )
            state["notion_recorded_pr_count"] = len(pull_requests)
    except Exception as e:
        print(f"   ⚠️ NotionへのPR情報追記失敗: {e}")

    return state
