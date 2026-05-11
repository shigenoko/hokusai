"""
Phase 10: セッション終了時の記録

- タスクページに進捗を記録
"""

from ..constants import PHASE_NAMES, PHASE_STATUS_ICONS
from ..integrations.factory import get_task_client
from ..integrations.git import GitClient
from ..logging_config import get_logger
from ..state import WorkflowState, add_audit_log
from ..utils.change_summary import build_combined_change_summary
from ..utils.phase_decorator import phase_node

logger = get_logger("phase10")


@phase_node(phase=10, action="progress_recorded", skip_check=False)
def phase10_record_node(state: WorkflowState) -> WorkflowState:
    """Phase 10: セッション終了時の記録"""
    task_client = get_task_client()

    # 進捗記録を生成
    progress_record = _generate_progress_record(state)

    # タスクに追記
    result = task_client.append_progress(state["task_url"], progress_record)
    if hasattr(result, 'result'):
        state = add_audit_log(
            state, 10, "notion_append_progress", result.result.value,
            error=result.reason,
        )

    # 確定版変更サマリーを生成してタスクページに追記（worktree cleanup 前）
    state = _append_final_change_summary(state, task_client)

    # 外部デザイン情報をタスクページに追記
    state = _append_design_summary(state, task_client)

    # worktree の自動 cleanup（HOKUSAI が作成したもののみ）
    _cleanup_worktrees(state)

    return state


def _append_final_change_summary(
    state: WorkflowState, task_client
) -> WorkflowState:
    """確定版変更サマリーを生成してタスクページに追記する。

    worktree cleanup 前に実行すること。失敗してもワークフローは継続する。
    """
    try:
        summary_md = build_combined_change_summary(state)
        if not summary_md:
            logger.info("確定版変更サマリー: 差分なし、スキップ")
            return state

        # PR 情報を付与
        pr_links = []
        for pr in state.get("pull_requests", []):
            url = pr.get("url", "")
            repo_name = pr.get("repo_name", "")
            number = pr.get("number", "")
            if url:
                pr_links.append(f"- {repo_name}: [PR #{number}]({url})")

        content_parts = ["### 変更サマリー", ""]
        if pr_links:
            content_parts.extend(pr_links)
            content_parts.append("")
        content_parts.append(summary_md)
        content = "\n".join(content_parts)

        result = task_client.append_progress(state["task_url"], content)
        if hasattr(result, 'result'):
            state = add_audit_log(
                state, 10, "final_change_summary_appended", result.result.value,
                error=result.reason,
            )
        else:
            state = add_audit_log(
                state, 10, "final_change_summary_appended", "success",
            )
        logger.info("確定版変更サマリーをタスクページに追記しました")
        print("📝 確定版変更サマリーをタスクページに追記しました")
    except Exception as e:
        logger.warning(f"確定版変更サマリーの追記に失敗: {e}")
        state = add_audit_log(
            state, 10, "final_change_summary_failed", "warning", error=str(e),
        )

    return state


def _append_design_summary(state: WorkflowState, task_client) -> WorkflowState:
    """Figma / Miro 連携結果をタスクページに追記する。

    挙動:
    - state に figma_url / miro_url のどちらも無いタスクでは何もしない
    - URL が 1 つでもあれば、連携の成否に関わらずサマリを追記する
      - 成功時: タイトル / summary / URL / 連携状態を記載
      - 取得失敗（summary が無い）時: "(取得失敗)" 表示で URL のみ残す
      - design_sync_errors がある場合: 「エラー」セクションに source 別の
        詳細を最大 5 件まで記載
    これにより運用者は Notion のタスクページだけで連携状況を把握できる。
    """
    figma_url = state.get("figma_url")
    miro_url = state.get("miro_url")
    if not (figma_url or miro_url):
        return state

    try:
        from ..utils.design_helpers import design_links_for_record

        info = design_links_for_record(state)
        lines = ["### デザイン / 業務フロー連携", ""]
        if miro_url:
            ms = info.get("miro_summary") or "(取得失敗)"
            lines.append(f"- Miro: [{ms}]({miro_url})")
        if figma_url:
            fs = info.get("figma_summary") or "(取得失敗)"
            lines.append(f"- Figma: [{fs}]({figma_url})")
        status = info.get("design_integration_status") or "unknown"
        lines.append(f"- 連携状態: {status}")
        warnings = info.get("design_warnings") or []
        if warnings:
            lines.append("- 警告:")
            for w in warnings[:5]:
                lines.append(f"  - {w}")
        errors = info.get("design_sync_errors") or []
        if errors:
            lines.append("- エラー:")
            for e in errors[:5]:
                src = e.get("source", "?")
                msg = e.get("error", "")
                lines.append(f"  - {src}: {msg}")

        content = "\n".join(lines)
        result = task_client.append_progress(state["task_url"], content)
        if hasattr(result, "result"):
            state = add_audit_log(
                state, 10, "design_summary_appended", result.result.value,
                error=result.reason,
            )
        else:
            state = add_audit_log(state, 10, "design_summary_appended", "success")
    except Exception as exc:
        logger.warning(f"デザイン情報の記録に失敗: {exc}")
        state = add_audit_log(
            state, 10, "design_summary_failed", "warning", error=str(exc),
        )
    return state


def _cleanup_worktrees(state: WorkflowState) -> None:
    """HOKUSAI が作成した worktree を削除する"""
    for repo in state.get("repositories", []):
        if not repo.get("worktree_created", False):
            continue
        source_path = repo.get("source_path", "")
        wt_path = repo.get("path", "")
        if not source_path or not wt_path:
            continue
        try:
            git = GitClient(source_path)
            git.remove_worktree(wt_path, force=False)
            print(f"🧹 worktree 削除: {wt_path}")
        except Exception as e:
            logger.warning(f"worktree 削除に失敗（手動削除してください）: {wt_path}: {e}")


def _generate_progress_record(state: WorkflowState) -> str:
    """進捗記録を生成"""
    lines = ["### 進捗状況", ""]

    for i in range(1, 11):
        phase_state = state["phases"][i]
        status = phase_state["status"]
        icon = PHASE_STATUS_ICONS.get(status, PHASE_STATUS_ICONS["pending"])
        lines.append(f"- {icon} Phase {i}: {PHASE_NAMES[i]}")

    # 次のステップ
    current = state["current_phase"]
    if current <= 10:
        lines.append("")
        lines.append(
            f"**次のステップ**: Phase {current} - {PHASE_NAMES.get(current, '完了')}"
        )

    return "\n".join(lines)
