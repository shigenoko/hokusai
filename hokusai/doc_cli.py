"""doc-mode CLI ハンドラ（M5）

`hokusai doc start --type <requirements|design> --topic <text>` を処理する。
doc-mode グラフ（ideation→draft→crosscheck→finalize）を1周実行し、確定稿を
出力する。出力は既定で stdout、``set_output_sink`` で Notion 出力等へ差し替え可能。
確定稿は次の Issue 化（運用フロー）のインプットになる。

HITL 承認の本格的な interrupt/continue は後続課題。M5 では確定稿を「承認待ち」
として提示し、人間の承認を経て Issue 化する運用を前提とする（auto でも自動承認
しない＝沈黙の確定を避ける）。
"""

from __future__ import annotations

import uuid
from typing import Callable, Optional

from .doc_graph import create_compiled_doc_workflow
from .logging_config import get_logger
from .state import DocWorkflowState, create_doc_workflow_state

logger = get_logger("doc_cli")

# 出力シンク: (state) -> None。既定 None（stdout）。Notion 出力等を束ねるフック。
OutputSink = Callable[[DocWorkflowState], None]
_output_sink: Optional[OutputSink] = None


def set_output_sink(fn: Optional[OutputSink]) -> None:
    """確定稿の出力先を差し替える（None で stdout に戻す）。"""
    global _output_sink
    _output_sink = fn


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


def handle_doc(args) -> int:
    """`hokusai doc ...` のエントリ。終了コードを返す。"""
    if getattr(args, "doc_subcommand", None) != "start":
        print(
            "usage: hokusai doc start --type <requirements|design> "
            "--topic <text> [--feature-page <id>] [--max-rounds N] [--mode step|auto]"
        )
        return 1

    try:
        state = run_doc_workflow(
            doc_type=args.type,
            topic=args.topic,
            feature_page_id=getattr(args, "feature_page", "") or "",
            run_mode=getattr(args, "mode", "auto") or "auto",
            max_rounds=getattr(args, "max_rounds", 1) or 1,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 境界でユーザに要約表示する
        logger.warning("doc-mode 実行に失敗: %s", exc)
        print(f"doc-mode 実行に失敗しました: {exc}")
        return 1

    if _output_sink is not None:
        _output_sink(state)
    else:
        print(render_doc_output(state))

    print()
    print("→ この確定稿は Issue 化のインプット（運用フロー）として利用できます。")

    return 0 if (state.get("template_check") or {}).get("ok") else 2
