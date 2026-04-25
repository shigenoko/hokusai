"""
Phase page template helpers.

Phase 2-4 の Notion 子ページを、人間が判断しやすい統一テンプレートで生成する。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..constants import PHASE_NAMES

if TYPE_CHECKING:
    from ..state import WorkflowState


PHASE_PAGE_STATUS_DEFAULT = "not_started"
PHASE_PAGE_DECISION_DEFAULT = "none"
PHASE_PAGE_RECOMMENDED_ACTION_DEFAULT = "none"
PHASE_PAGE_DOCUMENT_STATE_KEYS = {
    2: "research_result",
    3: "design_result",
    4: "work_plan",
}
PHASE_PAGE_SOURCE_PHASES = {
    2: "phase2_research",
    3: "phase3_design",
    4: "phase4_plan",
}


_PHASE_HUMAN_CHECK_ITEMS = {
    2: [
        "調査漏れがないか",
        "後続設計に必要な前提が揃っているか",
    ],
    3: [
        "設計方針が妥当か",
        "リスクとトレードオフに見落としがないか",
    ],
    4: [
        "実装順序が妥当か",
        "変更予定ファイルとテスト方針に漏れがないか",
    ],
}


_PHASE_SUMMARY_LINES = {
    2: [
        "調査レポートの最新版を更新済み",
        "cross-review の確認結果を反映待ち",
    ],
    3: [
        "設計チェック結果を作成済み",
        "レビュー指摘の採用判断が必要",
    ],
    4: [
        "開発計画の最新版を生成済み",
        "Phase 5 に渡してよいかの確認が必要",
    ],
}


def _phase_page_decision(state: "WorkflowState", phase: int) -> str:
    return state.get("phase_page_decision", {}).get(phase, PHASE_PAGE_DECISION_DEFAULT)


def _phase_page_last_updated(state: "WorkflowState", phase: int) -> str:
    value = state.get("phase_page_last_human_note_at", {}).get(phase) or state.get("updated_at")
    if not value:
        return datetime.now().isoformat()
    return value


def _phase_review(state: "WorkflowState", phase: int) -> dict:
    return state.get("cross_review_results", {}).get(phase, {})


def _derive_recommended_action(
    state: "WorkflowState",
    phase: int,
    override: str | None = None,
) -> str:
    if override:
        return override

    assessment = _phase_review(state, phase).get("overall_assessment")
    if assessment == "approve":
        return "approve_and_move_next"
    if assessment == "request_changes":
        return "request_changes"
    if assessment == "needs_discussion":
        return "human_review_required"

    return state.get("phase_page_recommended_action", {}).get(
        phase, PHASE_PAGE_RECOMMENDED_ACTION_DEFAULT
    )


def _derive_display_status(state: "WorkflowState", phase: int) -> str:
    phase_status = state.get("phases", {}).get(phase, {}).get("status", "pending")
    review = _phase_review(state, phase)

    if phase_status == "pending":
        return "not_started"
    if phase_status == "skipped":
        return "skipped"
    if phase_status == "completed":
        return "approved"
    if phase_status == "failed":
        phase_data = state.get("phases", {}).get(phase, {})
        error_msg = phase_data.get("error_message", "")
        # cross_review_blocked は明示的に needs_human_check
        if error_msg == "cross_review_blocked":
            return "needs_human_check"
        # グローバル waiting_for_human は current_phase のみに適用
        if state.get("waiting_for_human") and state.get("current_phase") == phase:
            return "needs_human_check"
        return "failed"
    if phase_status == "in_progress":
        if review:
            return "in_review"
        return "drafting"
    return PHASE_PAGE_STATUS_DEFAULT


def _derive_current_summary_lines(state: "WorkflowState", phase: int, display_status: str) -> list[str]:
    review = _phase_review(state, phase)
    assessment = review.get("overall_assessment")

    if display_status == "not_started":
        return ["フェーズはまだ開始されていません", "開始後に最新版ドキュメントがここへ反映されます"]
    if display_status == "drafting":
        return ["最新版ドキュメントを生成中です", "レビュー実行前のドラフト段階です"]
    if display_status == "in_review":
        line2 = "レビュー結果を確認し、次アクションを判断してください"
        if assessment == "approve":
            line2 = "レビューは承認相当です。次フェーズへ進めるか確認してください"
        elif assessment == "request_changes":
            line2 = "レビューで修正要求があります。同一フェーズでの修正要否を判断してください"
        elif assessment == "needs_discussion":
            line2 = "レビューで論点が残っています。人間確認が必要です"
        return ["クロスレビューの最新結果を反映済みです", line2]
    if display_status == "needs_human_check":
        return ["自動進行を停止しています", "人間の確認と意思決定が必要です"]
    if display_status == "approved":
        return ["このフェーズの成果物とレビュー反映は完了しています", "次フェーズへ進める状態です"]
    if display_status == "skipped":
        return ["このフェーズはスキップされました", "必要なら再実行条件を確認してください"]
    return _PHASE_SUMMARY_LINES.get(phase, ["フェーズの最新版を更新済み", "次アクションの判断が必要"])


def _format_latest_review_results(state: "WorkflowState", phase: int) -> str:
    review = _phase_review(state, phase)
    if not review:
        return "_No reviews yet._"

    findings = review.get("findings") or []
    summary_lines: list[str] = []
    if review.get("summary"):
        summary_lines.append(review["summary"])
    for finding in findings[:3]:
        title = finding.get("title") or finding.get("description") or "finding"
        severity = finding.get("severity")
        summary_lines.append(f"[{severity}] {title}" if severity else title)

    lines = [
        "- Reviewer: `codex`",
        f"- Overall Assessment: `{review.get('overall_assessment', 'unknown')}`",
    ]
    if summary_lines:
        lines.append("- Summary:")
        lines.extend([f"  - {line}" for line in summary_lines])
    return "\n".join(lines)


def _stringify_detail_items(details: object) -> list[str]:
    if not isinstance(details, dict):
        return []

    items: list[str] = []
    for key, value in details.items():
        if key == "reason":
            continue
        if isinstance(value, list):
            items.append(f"{key}={', '.join(str(v) for v in value[:3])}")
        else:
            items.append(f"{key}={value}")
    return items[:3]


def _format_revision_history(state: "WorkflowState", phase: int) -> str:
    entries = [entry for entry in state.get("audit_log", []) if entry.get("phase") == phase]
    if not entries:
        return "_No revisions yet._"

    lines: list[str] = []
    for entry in entries[-3:]:
        lines.append(f"- {entry.get('timestamp', '')}")
        lines.append(
            f"  - 対応内容: `{entry.get('action', 'unknown')}` ({entry.get('result', 'unknown')})"
        )
        for item in _stringify_detail_items(entry.get("details")):
            lines.append(f"    - {item}")
    return "\n".join(lines)


def _build_progress_checklist(state: "WorkflowState", phase: int, latest_document: str) -> str:
    phase_status = state.get("phases", {}).get(phase, {}).get("status", "pending")
    review = _phase_review(state, phase)
    review_status = state.get("phases", {}).get(phase, {}).get("review_status")
    decision = _phase_page_decision(state, phase)
    has_document = bool(latest_document and latest_document.strip())
    has_subpage = bool(state.get("phase_subpages", {}).get(phase))
    has_review = bool(review)
    review_executed = has_review or review_status in {"completed", "failed", "skipped"}
    human_decided = decision != PHASE_PAGE_DECISION_DEFAULT
    phase_done = phase_status == "completed" or decision == "approve_and_move_next"
    needs_fix = review.get("overall_assessment") == "request_changes"
    items = [
        (has_document, "1. 初回ドキュメントを生成"),
        (has_subpage, "2. フェーズページに保存"),
        (review_executed, "3. マルチLLMレビューを実行"),
        (has_review, "4. 最新レビュー結果をページに反映"),
        (needs_fix, "5. 必要なら同一フェーズ内で修正"),
        (
            review_executed or state.get("waiting_for_human") or phase_status in {"completed", "failed"},
            "6. 人間確認が必要か判定",
        ),
        (human_decided, "7. 人間レビュー結果を反映"),
        (phase_done, "8. 次フェーズへ進行可能か確定"),
    ]
    return "\n".join(f"- [{'x' if checked else ' '}] {label}" for checked, label in items)


def initialize_phase_page_state(state: "WorkflowState", phase: int) -> None:
    """フェーズページ表示に必要な state キーを初期化する。

    表示状態はテンプレート側で state から導出するため、独立した状態値は持たない。
    新規 state では legacy フィールドを生成しない。
    """
    state.setdefault("phase_page_decision", {})
    state.setdefault("phase_page_last_human_note_at", {})
    state.setdefault("phase_page_recommended_action", {})

    state["phase_page_decision"].setdefault(phase, PHASE_PAGE_DECISION_DEFAULT)
    state["phase_page_recommended_action"].setdefault(
        phase, PHASE_PAGE_RECOMMENDED_ACTION_DEFAULT
    )


def get_phase_page_context(state: "WorkflowState", phase: int) -> dict[str, str]:
    """フェーズページ表示用の導出済みコンテキストを返す。"""
    return {
        "phase_name": PHASE_NAMES.get(phase, f"Phase {phase}"),
        "phase_status": state.get("phases", {}).get(phase, {}).get("status", "pending"),
        "display_status": _derive_display_status(state, phase),
        "current_decision": _phase_page_decision(state, phase),
        "recommended_action": _derive_recommended_action(state, phase),
        "phase_subpage_url": state.get("phase_subpages", {}).get(phase, ""),
        "last_updated_at": _phase_page_last_updated(state, phase),
    }


def build_phase_page_content(
    *,
    state: "WorkflowState",
    phase: int,
    latest_document: str,
    current_summary_lines: list[str] | None = None,
    human_check_items: list[str] | None = None,
    recommended_action: str | None = None,
    review_results: str = "_No reviews yet._",
    revision_history: str = "_No revisions yet._",
    human_note_placeholder: str = "人間による判断メモをここに記載",
    source_phase: str = "phase_node",
) -> str:
    """Phase 2-4 共通のレビュー可能ページ本文を生成する。"""
    del human_note_placeholder  # 初期実装では Notion を操作面にしない。

    context = get_phase_page_context(state, phase)
    phase_name = context["phase_name"]
    phase_status = context["phase_status"]
    display_status = context["display_status"]
    decision = context["current_decision"]
    last_updated_at = context["last_updated_at"]
    phase_subpage_url = context["phase_subpage_url"]
    recommended_action = recommended_action or context["recommended_action"]
    current_summary_lines = current_summary_lines or _derive_current_summary_lines(
        state, phase, display_status
    )
    human_check_items = human_check_items or _PHASE_HUMAN_CHECK_ITEMS.get(
        phase, ["内容を確認してください", "次アクションを判断してください"]
    )
    if state.get("waiting_for_human"):
        human_check_items = [
            "最新版ドキュメントと最新レビュー結果を確認してください",
            "ダッシュボードから次アクションを選択してください",
        ]
    review_results = (
        review_results
        if review_results != "_No reviews yet._"
        else _format_latest_review_results(state, phase)
    )
    revision_history = (
        revision_history
        if revision_history != "_No revisions yet._"
        else _format_revision_history(state, phase)
    )
    checklist = _build_progress_checklist(state, phase, latest_document)
    audit_reference = f"phase={phase}, workflow_id={state.get('workflow_id', '')}"

    return f"""# Phase {phase}: {phase_name}

## フェーズ概要

- Workflow ID: `{state.get("workflow_id", "")}`
- Task URL: {state.get("task_url", "")}
- Phase Status: `{phase_status}`
- Display Status: `{display_status}`
- Current Decision: `{decision}`
- Recommended Action: `{recommended_action}`
- Last Updated: `{last_updated_at}`

## 現在の判断

- 現在状態:
  - `{display_status}`
- 停止理由 / 状況:
  - {current_summary_lines[0]}
  - {current_summary_lines[1]}
- 人間に確認してほしいこと:
  - {human_check_items[0]}
  - {human_check_items[1]}
- 操作方法:
  - ダッシュボードから `request_changes` または `approve_and_move_next` を選択する

## 進捗チェックリスト

{checklist}

## 最新版ドキュメント

{latest_document}

## 最新レビュー結果

{review_results}

## 修正履歴

{revision_history}

## 次アクション

- Recommended: `{recommended_action}`
- Dashboard Actions:
  - `request_changes`
  - `approve_and_move_next`

## システムメモ

- phase_subpage_url: `{phase_subpage_url}`
- source_phase: `{source_phase}`
- audit_ref: `{audit_reference}`
- note:
  - 実行状態の正本は state
  - このページは閲覧用に再生成される
"""
