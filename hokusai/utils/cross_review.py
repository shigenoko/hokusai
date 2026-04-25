"""
クロスLLMレビュー実行ユーティリティ

Phase 2/3/4 で共通利用する Codex クロスレビュー実行ロジック。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..config import get_config
from ..constants import CROSS_REVIEW_PROMPTS, PHASE_NAMES
from ..integrations.codex import CodexClient
from ..logging_config import get_logger
from ..state import add_audit_log

if TYPE_CHECKING:
    from ..state import WorkflowState

logger = get_logger("cross_review")

# レビュースキーマファイルのパス
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "review_schema.json"


def execute_cross_review(
    state: WorkflowState,
    document: str,
    phase: int,
) -> WorkflowState:
    """Codex クロスレビューを実行し、結果を state に反映

    Args:
        state: ワークフロー状態
        document: レビュー対象のドキュメント
        phase: Phase番号（2 or 3 or 4）

    Returns:
        更新されたワークフロー状態
    """
    config = get_config()

    # 設定チェック
    if not config.cross_review.enabled:
        logger.debug("クロスレビューは無効です")
        _set_review_status(state, phase, "not_run")
        return state

    if phase not in config.cross_review.phases:
        logger.debug(f"Phase {phase} はクロスレビュー対象外です")
        _set_review_status(state, phase, "not_run")
        return state

    if not document or not document.strip():
        logger.warning(f"Phase {phase}: レビュー対象ドキュメントが空のためスキップ")
        _set_review_status(state, phase, "skipped")
        return state

    # レビュープロンプトを取得
    review_prompt = CROSS_REVIEW_PROMPTS.get(phase, "")
    if not review_prompt:
        logger.warning(f"Phase {phase} 用のレビュープロンプトが未定義です")
        return state

    # CodexClient を初期化
    try:
        client = CodexClient(
            model=config.cross_review.model,
            timeout=config.cross_review.timeout,
        )
    except FileNotFoundError:
        msg = "Codex CLIが見つかりません"
        _set_review_status(state, phase, "failed")
        if config.cross_review.on_failure == "block":
            logger.error(f"Phase {phase}: {msg}（blockモード: ワークフロー停止）")
            state = add_audit_log(
                state, phase, "cross_review_failed", "error", error=msg,
            )
            state["waiting_for_human"] = True
            state["human_input_request"] = "Codex CLIが見つかりません。インストールするか、cross_review設定を無効化してください。"
            return state
        if config.cross_review.on_failure == "skip":
            logger.warning(f"Phase {phase}: {msg}（skipモード: スキップ）")
            state = add_audit_log(
                state, phase, "cross_review_skipped", "warning",
                details={"reason": "codex_not_found"},
            )
            return state
        else:
            logger.warning(f"Phase {phase}: {msg}（続行します）")
            state = add_audit_log(
                state, phase, "cross_review_skipped", "warning",
                details={"reason": "codex_not_found"},
            )
            return state

    # レビュー実行
    schema_path = str(_SCHEMA_PATH) if _SCHEMA_PATH.exists() else None
    max_attempts = max(1, int(config.cross_review.max_correction_rounds))
    result: dict | None = None
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = client.review_document(
                document=document,
                review_prompt=review_prompt,
                schema_path=schema_path,
            )
            break
        except (TimeoutError, RuntimeError) as e:
            last_error = e
            if attempt < max_attempts:
                logger.warning(
                    f"Phase {phase}: Codexレビュー失敗（{attempt}/{max_attempts}）"
                    f" - 再試行します: {e}"
                )
            else:
                logger.warning(
                    f"Phase {phase}: Codexレビュー失敗（{attempt}/{max_attempts}）"
                    f" - 再試行上限に到達: {e}"
                )

    if result is None:
        _set_review_status(state, phase, "failed")
        error_detail = (
            f"Phase {phase} cross-review failed: "
            f"model={config.cross_review.model}, "
            f"on_failure={config.cross_review.on_failure}, "
            f"error={last_error}"
        )
        if config.cross_review.on_failure == "block":
            logger.error(f"{error_detail} → ワークフロー停止")
            state = add_audit_log(
                state, phase, "cross_review_failed", "error",
                error=error_detail,
            )
            state["waiting_for_human"] = True
            state["human_input_request"] = (
                f"Codexレビューに失敗しました (model={config.cross_review.model}): {last_error}"
            )
            return state
        if config.cross_review.on_failure == "skip":
            logger.warning(f"{error_detail} → スキップ")
            state = add_audit_log(
                state, phase, "cross_review_skipped", "warning",
                details={
                    "reason": "execution_error",
                    "model": config.cross_review.model,
                    "error": str(last_error),
                },
            )
            return state
        logger.warning(f"{error_detail} → 続行")
        state = add_audit_log(
            state, phase, "cross_review_failed", "warning",
            error=error_detail,
        )
        return state

    # 結果を state に格納
    _set_review_status(state, phase, "completed")
    state["cross_review_results"][phase] = result
    assessment = result.get("overall_assessment", "unknown")
    findings = result.get("findings", [])
    critical_findings = [f for f in findings if f.get("severity") == "critical"]

    logger.info(
        f"Phase {phase} クロスレビュー完了: assessment={assessment}, "
        f"findings={len(findings)}件（critical={len(critical_findings)}件）"
    )

    # 監査ログに記録
    state = add_audit_log(
        state, phase, "cross_review_completed", "success",
        details={
            "assessment": assessment,
            "findings_count": len(findings),
            "critical_count": len(critical_findings),
            "confidence_score": result.get("confidence_score"),
        },
    )

    # critical findings + block モード → human-in-the-loop
    if critical_findings and config.cross_review.on_failure == "block":
        logger.warning(
            f"Phase {phase}: critical findings検出（{len(critical_findings)}件）"
            "→ ワークフロー停止"
        )
        state["waiting_for_human"] = True
        state["human_input_request"] = (
            f"Codexクロスレビューでcritical指摘が{len(critical_findings)}件あります。"
            "確認してください。"
        )

    # Notion に callout 保存（子ページがあれば子ページに、なければタスクページに）
    _save_review_to_notion(state, result, phase)

    logger.info(f"Phase {phase} クロスLLMレビュー完了: {assessment}")
    return state


def _set_review_status(state: WorkflowState, phase: int, status: str) -> None:
    """phases[phase] に review_status を設定する

    Args:
        state: ワークフロー状態
        phase: フェーズ番号
        status: "completed", "failed", "skipped", "not_run"
    """
    phases = state.get("phases", {})
    if phase in phases:
        phases[phase]["review_status"] = status


_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def format_cross_review_for_prompt(
    state: WorkflowState,
    source_phase: int,
) -> str:
    """前フェーズのcross-review findingsをスキルargsに追記するMarkdown文字列を生成

    Returns:
        整形されたMarkdown文字列。結果がない/findingsが空の場合は空文字列。
    """
    review = state.get("cross_review_results", {}).get(source_phase)
    if not review:
        return ""

    findings = review.get("findings", [])
    if not findings:
        return ""

    phase_name = PHASE_NAMES.get(source_phase, f"Phase {source_phase}")
    assessment = review.get("overall_assessment", "unknown")
    confidence = review.get("confidence_score")
    summary = review.get("summary", "")

    # ヘッダー
    lines = [
        "---",
        f"## 前フェーズのクロスLLMレビュー指摘事項 (Phase {source_phase}: {phase_name})",
        "",
    ]

    # Overall 行
    confidence_part = f" (confidence: {confidence}%)" if confidence is not None else ""
    lines.append(f"**Overall:** {assessment}{confidence_part}")

    if summary:
        lines.append(f"**Summary:** {summary}")

    lines.append("")
    lines.append("### Findings")

    # severity 順にソート
    sorted_findings = sorted(
        findings,
        key=lambda f: _SEVERITY_ORDER.get(f.get("severity", "info"), 99),
    )

    for f in sorted_findings:
        severity = f.get("severity", "info")
        title = f.get("title", "")
        lines.append(f"- **[{severity}] {title}**")
        description = f.get("description", "")
        if description:
            lines.append(f"  {description}")
        suggestion = f.get("suggestion", "")
        if suggestion:
            lines.append(f"  提案: {suggestion}")

    lines.append("")
    lines.append("上記のクロスLLMレビュー指摘事項を考慮して開発計画を作成してください。")

    return "\n".join(lines)


def _save_review_to_notion(state: WorkflowState, result: dict, phase: int) -> None:
    """レビュー結果をNotionに保存

    state["phase_subpages"][phase] が存在すれば子ページ末尾に追記、
    なければタスクページ末尾に追記（フォールバック）。
    """
    try:
        from .notion_helpers import (
            append_to_subpage,
            generate_cross_review_callout,
            save_content_to_notion,
        )
        callout = generate_cross_review_callout(result, phase)

        subpage_url = state.get("phase_subpages", {}).get(phase)
        if subpage_url:
            success = append_to_subpage(subpage_url, callout)
            if success:
                logger.info(f"Phase {phase} クロスレビュー callout を子ページに保存")
            else:
                logger.warning(f"Phase {phase} クロスレビュー callout の子ページ保存に失敗、タスクページにフォールバック")
                save_content_to_notion(state["task_url"], callout)
        else:
            save_content_to_notion(state["task_url"], callout)
            logger.info(f"Phase {phase} クロスレビュー callout をタスクページに保存（フォールバック）")
    except Exception as e:
        logger.warning(f"クロスレビュー結果のNotion保存に失敗: {e}")
