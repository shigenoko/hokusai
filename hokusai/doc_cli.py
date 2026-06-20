"""doc-mode CLI ハンドラ（M5）

`hokusai doc start --type <requirements|design> --topic <text>` を処理する。
doc-mode グラフ（ideation→draft→crosscheck→finalize）を1周実行し、確定稿を
出力する。出力は既定で stdout、``set_output_sink`` で Notion 出力等へ差し替え可能。
確定稿は次の Issue 化（運用フロー）のインプットになる。

HITL 承認は本格対応済み: ``--mode step`` で確定稿の前に承認ゲートへ interrupt し、
``hokusai doc continue <wid> --approve|--reject`` で再開する（LangGraph interrupt +
checkpointer）。auto モードは自動承認しない（承認待ちのまま＝沈黙の確定を避ける）。
"""

from __future__ import annotations

import uuid
from typing import Callable, Optional

from .config import get_config
from .doc_graph import create_compiled_doc_workflow
from .logging_config import get_logger
from .state import DocWorkflowState, create_doc_workflow_state
from .utils.skip_notion import is_skip_notion

logger = get_logger("doc_cli")

# 出力シンク: (state) -> None。既定 None（stdout）。Notion 出力等を束ねるフック。
OutputSink = Callable[[DocWorkflowState], None]
_output_sink: Optional[OutputSink] = None


class DocOutputError(RuntimeError):
    """doc-mode の出力（Notion 書き込み等）に失敗したことを表す。"""


# Notion クライアント factory（テストで差し替え可能）
_notion_client_factory: Optional[Callable[[], object]] = None


def _default_notion_client():
    from .integrations.notion_mcp import NotionMCPClient

    return NotionMCPClient()


def set_notion_client_factory(fn: Optional[Callable[[], object]]) -> None:
    """Notion クライアント factory を差し替える（None で既定に戻す）。"""
    global _notion_client_factory
    _notion_client_factory = fn


def set_output_sink(fn: Optional[OutputSink]) -> None:
    """確定稿の出力先を差し替える（None で既定動作に戻す）。"""
    global _output_sink
    _output_sink = fn


def _doc_title(state: DocWorkflowState) -> str:
    """IA の命名規約に沿った子ページタイトルを返す。"""
    if state.get("doc_type") == "design":
        return f"【設計書】{state.get('topic')}"
    return f"要件整理：{state.get('topic')}"


def _notion_body(state: DocWorkflowState) -> str:
    """Notion 子ページ本文（メタ callout + final_doc）を組み立てる。"""
    tc = state.get("template_check") or {}
    status = "OK" if tc.get("ok") else "NG"
    return "\n".join(
        [
            f"> 📐 doc-mode 生成（workflow: {state.get('workflow_id')} / 型準拠: {status}）",
            f"> 承認: {'済' if state.get('approved') else '未（HITL 承認待ち）'}",
            "",
            state.get("final_doc", ""),
        ]
    )


def notion_output_sink(state: DocWorkflowState) -> None:
    """確定稿を IA に従って機能ページ配下の子ページとして保存する。

    ``feature_page_id`` 未指定なら stdout にフォールバックする（graceful）。
    保存失敗時は ``DocOutputError`` を送出する。
    """
    feature = state.get("feature_page_id")
    if not feature:
        print(render_doc_output(state))
        print("\n（feature-page 未指定のため Notion 出力をスキップしました）")
        return

    # HOKUSAI_SKIP_NOTION 等が有効なら既存ヘルパーと同様に skip して stdout へ
    if is_skip_notion():
        print(render_doc_output(state))
        print("\n（Notion skip 設定が有効のため Notion 出力をスキップしました）")
        return

    factory = _notion_client_factory or _default_notion_client
    try:
        client = factory()
        url = client.create_subpage(feature, _doc_title(state), _notion_body(state))
    except DocOutputError:
        raise
    except Exception as exc:  # noqa: BLE001 - CLI 境界で Notion/Claude 例外を要約
        raise DocOutputError(
            f"Notion への出力に失敗しました（feature_page={feature}）: {exc}"
        ) from exc

    if url is None:
        raise DocOutputError(
            f"Notion への出力に失敗しました（feature_page={feature}）"
        )
    print(f"→ Notion に確定稿を保存しました: {url or '(URL 不明)'}")


def render_doc_output(state: DocWorkflowState) -> str:
    """確定稿を人間がレビュー/Issue 化しやすい形に整形する。"""
    tc = state.get("template_check") or {}
    if tc.get("ok"):
        template_line = "型準拠: OK"
    else:
        missing = ", ".join(tc.get("missing", []))
        template_line = f"型準拠: NG（欠落: {missing}）"
    approval_line = "承認: 済" if state.get("approved") else "承認: 未（HITL 承認待ち）"

    return "\n".join(
        [
            f"# doc-mode 出力（{state.get('doc_type')}）",
            f"workflow_id: {state.get('workflow_id')}",
            f"topic: {state.get('topic')}",
            template_line,
            approval_line,
            "",
            "## final_doc",
            state.get("final_doc", ""),
        ]
    )


def run_doc_workflow(
    doc_type: str,
    topic: str,
    feature_page_id: str = "",
    run_mode: str = "auto",
    max_rounds: int = 1,
    workflow_id: Optional[str] = None,
) -> DocWorkflowState:
    """doc-mode グラフを1周実行し、確定後の state を返す。"""
    wid = workflow_id or f"doc-{uuid.uuid4().hex[:8]}"
    app = create_compiled_doc_workflow()
    state = create_doc_workflow_state(
        workflow_id=wid,
        doc_type=doc_type,
        topic=topic,
        feature_page_id=feature_page_id,
        run_mode=run_mode,
        max_rounds=max_rounds,
    )
    return app.invoke(state)


# === HITL（interrupt / continue）===========================================
# step モードでは HITL 承認ゲートで interrupt するため、checkpointer 付きの
# コンパイル済みアプリを使い、thread_id=workflow_id で start/continue を跨ぐ。

_doc_checkpointer = None
_doc_app = None


def set_doc_checkpointer(checkpointer) -> None:
    """doc-mode（HITL）の checkpointer を差し替える（テストで MemorySaver 注入）。

    None を渡すと既定（SqliteSaver）に戻す。差し替え時はアプリを作り直す。
    """
    global _doc_checkpointer, _doc_app
    _doc_checkpointer = checkpointer
    _doc_app = None


def _get_doc_app():
    """checkpointer 付きのコンパイル済み doc-mode アプリ（HITL 用、再利用）。"""
    global _doc_checkpointer, _doc_app
    if _doc_app is not None:
        return _doc_app
    if _doc_checkpointer is None:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        data_dir = get_config().data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(data_dir / "doc_checkpoint.db"), check_same_thread=False
        )
        checkpointer = SqliteSaver(conn)
        checkpointer.setup()
        _doc_checkpointer = checkpointer
    _doc_app = create_compiled_doc_workflow(_doc_checkpointer)
    return _doc_app


def start_doc_step(
    doc_type: str,
    topic: str,
    feature_page_id: str = "",
    max_rounds: int = 1,
    workflow_id: Optional[str] = None,
):
    """step モードで開始。HITL ゲートで interrupt し (wid, state, interrupted) を返す。"""
    wid = workflow_id or f"doc-{uuid.uuid4().hex[:8]}"
    app = _get_doc_app()
    state = create_doc_workflow_state(
        workflow_id=wid,
        doc_type=doc_type,
        topic=topic,
        feature_page_id=feature_page_id,
        run_mode="step",
        max_rounds=max_rounds,
    )
    config = {"configurable": {"thread_id": wid}}
    result = app.invoke(state, config=config)
    interrupted = "__interrupt__" in result
    return wid, result, interrupted


def continue_doc(workflow_id: str, approve: bool) -> DocWorkflowState:
    """interrupt 中の doc-mode を承認/却下で再開し、確定 state を返す。"""
    from langgraph.types import Command

    app = _get_doc_app()
    config = {"configurable": {"thread_id": workflow_id}}
    return app.invoke(Command(resume=bool(approve)), config=config)


def _resolve_max_rounds(args, doc_cfg) -> int:
    """--max-rounds 未指定時は doc_orchestration.rounds を既定反映する。"""
    max_rounds = getattr(args, "max_rounds", None)
    if max_rounds is not None:
        return max_rounds
    return getattr(doc_cfg, "rounds", 1) if doc_cfg is not None else 1


def _emit_doc_output(state: DocWorkflowState, template_ok: bool) -> bool:
    """確定稿を出力する。成功なら True、DocOutputError なら False。"""
    try:
        if _output_sink is not None:
            # 明示的に差し替えられたシンクを最優先（NG 判定は呼び出し側責務）
            _output_sink(state)
        elif state.get("feature_page_id") and template_ok:
            # --feature-page 指定 かつ 型OK のときのみ実 Notion 出力。
            # 型NG の不完全な成果物を Notion に残さない安全弁（HITL/型準拠）。
            notion_output_sink(state)
        else:
            print(render_doc_output(state))
            if state.get("feature_page_id") and not template_ok:
                print("\n（型NG のため Notion 出力をスキップしました）")
    except DocOutputError as exc:
        print(f"出力に失敗しました: {exc}")
        return False
    return True


def _issue_handoff_note() -> None:
    print()
    print("→ この確定稿は Issue 化のインプット（運用フロー）として利用できます。")


def handle_doc(args) -> int:
    """`hokusai doc ...` のエントリ。終了コードを返す。"""
    sub = getattr(args, "doc_subcommand", None)
    if sub == "continue":
        return handle_doc_continue(args)
    if sub != "start":
        print(
            "usage:\n"
            "  hokusai doc start --type <requirements|design> --topic <text> "
            "[--feature-page <id>] [--max-rounds N] [--mode step|auto]\n"
            "  hokusai doc continue <workflow-id> [--approve|--reject]"
        )
        return 1

    # doc_orchestration の enabled ガードと rounds→max_rounds 既定反映
    doc_cfg = getattr(get_config(), "doc_orchestration", None)
    if doc_cfg is not None and not getattr(doc_cfg, "enabled", False):
        print(
            "doc-mode は無効です。config の doc_orchestration.enabled: true "
            "で有効化してください。"
        )
        return 1

    max_rounds = _resolve_max_rounds(args, doc_cfg)
    mode = getattr(args, "mode", "auto") or "auto"

    if mode == "step":
        return _handle_doc_start_step(args, max_rounds)

    try:
        state = run_doc_workflow(
            doc_type=args.type,
            topic=args.topic,
            feature_page_id=getattr(args, "feature_page", "") or "",
            run_mode="auto",
            max_rounds=max_rounds,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 境界でユーザに要約表示する
        logger.warning("doc-mode 実行に失敗: %s", exc)
        print(f"doc-mode 実行に失敗しました: {exc}")
        return 1

    template_ok = bool((state.get("template_check") or {}).get("ok"))
    if not _emit_doc_output(state, template_ok):
        return 1
    _issue_handoff_note()
    return 0 if template_ok else 2


def _handle_doc_start_step(args, max_rounds: int) -> int:
    """step モード: HITL ゲートで停止し、continue を促す。"""
    try:
        wid, result, interrupted = start_doc_step(
            doc_type=args.type,
            topic=args.topic,
            feature_page_id=getattr(args, "feature_page", "") or "",
            max_rounds=max_rounds,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 境界でユーザに要約表示する
        logger.warning("doc-mode（step）実行に失敗: %s", exc)
        print(f"doc-mode 実行に失敗しました: {exc}")
        return 1

    if interrupted:
        print("【HITL】確定稿のレビューをお願いします（承認待ち）。\n")
        print(render_doc_output(result))
        print(f"\n承認: hokusai doc continue {wid} --approve")
        print(f"却下: hokusai doc continue {wid} --reject")
        return 0

    # interrupt されなかった（型NG が上限到達で終了した等）→ そのまま出力
    template_ok = bool((result.get("template_check") or {}).get("ok"))
    if not _emit_doc_output(result, template_ok):
        return 1
    return 0 if template_ok else 2


def handle_doc_continue(args) -> int:
    """`hokusai doc continue <wid> [--approve|--reject]` のハンドラ。"""
    wid = getattr(args, "workflow_id", None)
    if not wid:
        print("usage: hokusai doc continue <workflow-id> [--approve|--reject]")
        return 1

    approve = not getattr(args, "reject", False)  # 既定は承認（--reject で却下）
    try:
        final = continue_doc(wid, approve)
    except Exception as exc:  # noqa: BLE001 - CLI 境界でユーザに要約表示する
        logger.warning("doc-mode 再開に失敗: %s", exc)
        print(f"doc-mode の再開に失敗しました: {exc}")
        return 1

    if not final.get("approved", False):
        print("却下されました。Notion 出力はスキップします。")
        return 1

    template_ok = bool((final.get("template_check") or {}).get("ok"))
    if not _emit_doc_output(final, template_ok):
        return 1
    _issue_handoff_note()
    return 0 if template_ok else 2
