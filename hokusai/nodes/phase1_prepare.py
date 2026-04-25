"""
Phase 1: タスク受領と準備

- タスク情報の確認
- ステータスを「進行中」に変更
- featureブランチを作成
- ワークフローIDをタスクに記録

Note:
    --from-phase オプションで途中から開始する場合、
    このフェーズはタスク情報取得と既存ブランチのチェックアウトのみ実行します。
    ワークフローIDの記録は初回実行時のみ行い、リスタート時は行いません。
"""

import subprocess
from datetime import datetime

from ..config import get_config
from ..constants import CALLOUT_WORKFLOW_START
from ..integrations.factory import get_task_client
from ..integrations.git import BranchReuseDenied, GitClient
from ..logging_config import get_logger
from ..state import (
    PhaseStatus,
    WorkflowState,
    add_audit_log,
    init_repository_state,
    should_skip_phase,
    update_phase_status,
)
from ..utils.notion_helpers import build_callout

logger = get_logger("phase1")


def phase1_prepare_node(state: WorkflowState) -> WorkflowState:
    """Phase 1: タスク受領と準備"""

    # Phase 1がスキップ対象かチェック
    is_skipped = should_skip_phase(state, 1)

    if not is_skipped:
        state = update_phase_status(state, 1, PhaseStatus.IN_PROGRESS)

    try:
        config = get_config()
        task_client = get_task_client()

        # ターゲットリポジトリを取得（default_target=Trueのもののみ）
        target_repositories = config.get_target_repositories()
        if not target_repositories:
            # 設定自体がないのか、default_targetが全てFalseなのかを判定
            all_repos = config.repositories
            if not all_repos:
                raise ValueError("設定ファイルにリポジトリが1件も定義されていません")
            else:
                raise ValueError(
                    f"有効な対象リポジトリ（default_target=true）がありません。"
                    f"定義されているリポジトリ数: {len(all_repos)}件"
                )

        # メインリポジトリ（最初のターゲットリポジトリ）でブランチ名を生成
        main_repo = target_repositories[0]
        git = GitClient(main_repo.path)

        # 1. タスク情報の取得（常に実行）
        task_info = task_client.fetch_task(state["task_url"])
        state["task_title"] = task_client.get_task_title(task_info)

        if is_skipped:
            # スキップモード: 最小限の処理のみ
            if state["branch_name"]:
                existing_repos = state.get("repositories", [])

                if existing_repos:
                    # state に repositories がある場合: worktree の存在確認
                    for repo_state in existing_repos:
                        from pathlib import Path as _Path
                        wt_path = _Path(repo_state.get("path", ""))
                        if repo_state.get("worktree_created", False):
                            if wt_path.exists():
                                logger.info(f"既存 worktree を再利用: {wt_path}")
                            else:
                                raise RuntimeError(
                                    f"Worktree が存在しません: {wt_path}\n"
                                    f"手動で削除された可能性があります。\n"
                                    f"ワークフローを新規作成してください。"
                                )
                    print("⏭️  Phase 1 スキップ: 既存 worktree を再利用")
                else:
                    # state に repositories がない場合: worktree を新規作成
                    import re as _re
                    workflow_id = state["workflow_id"]
                    worktree_root = config.worktree_root

                    for repo in target_repositories:
                        base_branch = repo.base_branch or state["base_branch"]
                        repo_git = GitClient(repo.path)

                        safe_name = _re.sub(r"[^a-zA-Z0-9_-]", "_", repo.name)
                        worktree_dir = worktree_root / f"{safe_name}_{workflow_id}"

                        # 既存ブランチ用の worktree を作成
                        try:
                            repo_git.create_worktree(
                                worktree_dir, state["branch_name"], f"origin/{base_branch}",
                            )
                        except BranchReuseDenied as e:
                            raise RuntimeError(
                                _format_branch_reuse_error(repo.name, e)
                            ) from e

                        if config.submodule_enabled:
                            repo_git.init_submodules(worktree_dir)

                        repo_state_entry = init_repository_state(
                            name=repo.name,
                            path=str(worktree_dir),
                            branch=state["branch_name"],
                            base_branch=base_branch,
                            source_path=str(repo.path),
                            worktree_created=True,
                        )
                        repositories = list(state.get("repositories", []))
                        repositories.append(repo_state_entry)
                        state["repositories"] = repositories

                    print(f"⏭️  Phase 1 スキップ: 既存ブランチ {state['branch_name']} で worktree を作成")
            else:
                print("⏭️  Phase 1 スキップ")

            # リスタート時はワークフローIDをNotionに書き込まない
            # （初回実行時に既に記録済みのため）

            state = add_audit_log(state, 1, "phase_skipped", "success")
            return state

        # 通常モード: フル処理

        # 2. ステータスを「進行中」に変更
        status = config.get_status("in_progress")
        result = task_client.update_status(state["task_url"], status)
        if hasattr(result, 'result'):
            state = add_audit_log(
                state, 1, "notion_update_status", result.result.value,
                details={"status": status},
                error=result.reason,
            )

        # 3. ブランチ名の生成（未設定の場合、メインリポジトリで1回だけ）
        if not state["branch_name"]:
            branch_name = git.generate_branch_name(state["task_title"])
            state["branch_name"] = branch_name

        # 4. 各ターゲットリポジトリで worktree + ブランチを作成
        created_repos = []
        created_worktrees = []  # ロールバック用
        workflow_id = state["workflow_id"]
        worktree_root = config.worktree_root

        try:
            for repo in target_repositories:
                base_branch = repo.base_branch or state["base_branch"]
                repo_git = GitClient(repo.path)

                # worktree パスを生成（filesystem-safe な名前）
                import re as _re
                safe_name = _re.sub(r"[^a-zA-Z0-9_-]", "_", repo.name)
                worktree_dir = worktree_root / f"{safe_name}_{workflow_id}"

                # worktree + ブランチを一発で作成
                try:
                    repo_git.create_worktree(
                        worktree_dir, state["branch_name"], f"origin/{base_branch}",
                    )
                except BranchReuseDenied as e:
                    raise RuntimeError(
                        _format_branch_reuse_error(repo.name, e)
                    ) from e
                created_worktrees.append((repo_git, worktree_dir))

                # submodule が有効な場合は初期化
                if config.submodule_enabled:
                    repo_git.init_submodules(worktree_dir)

                # setup_command が設定されている場合は実行
                if repo.setup_command:
                    print(f"⚙️  {repo.name}: setup_command を実行中...")
                    setup_result = subprocess.run(
                        repo.setup_command,
                        shell=True,
                        cwd=str(worktree_dir),
                        capture_output=True,
                        text=True,
                        timeout=config.command_timeout,
                    )
                    if setup_result.returncode != 0:
                        logger.error(
                            f"setup_command 失敗 ({repo.name}): {setup_result.stderr[:500]}"
                        )
                        raise RuntimeError(
                            f"{repo.name} の setup_command が失敗しました: "
                            f"{setup_result.stderr[:200]}"
                        )
                    print(f"✅ {repo.name}: setup_command 完了")

                # state にリポジトリ情報を保存（worktree path を正とする）
                repo_state = init_repository_state(
                    name=repo.name,
                    path=str(worktree_dir),
                    branch=state["branch_name"],
                    base_branch=base_branch,
                    source_path=str(repo.path),
                    worktree_created=True,
                )
                repositories = list(state.get("repositories", []))
                repositories.append(repo_state)
                state["repositories"] = repositories

                created_repos.append(repo.name)
                print(f"🌿 {repo.name}: worktree + ブランチ {state['branch_name']} を作成 → {worktree_dir}")
        except Exception:
            # ロールバック: 作成済み worktree を削除
            for rollback_git, rollback_path in created_worktrees:
                try:
                    rollback_git.remove_worktree(rollback_path, force=True)
                    logger.info(f"ロールバック: worktree 削除 {rollback_path}")
                except Exception as rb_err:
                    logger.warning(f"ロールバック失敗: {rb_err}")
            raise

        # 5. ワークフローIDをタスクの先頭に追記
        workflow_info = _generate_workflow_start_info(state, target_repos=created_repos)
        result = task_client.prepend_content(state["task_url"], workflow_info)
        if hasattr(result, 'result'):
            state = add_audit_log(
                state, 1, "notion_prepend_content", result.result.value,
                error=result.reason,
            )

        state = update_phase_status(state, 1, PhaseStatus.COMPLETED)
        state = add_audit_log(
            state,
            1,
            "phase_completed",
            "success",
            {
                "branch_name": state["branch_name"],
                "task_title": state["task_title"],
                "target_repositories": created_repos,
            },
        )

        print(f"✅ Phase 1 完了: ブランチ {state['branch_name']} を作成しました（{len(created_repos)}リポジトリ）")

    except Exception as e:
        state = update_phase_status(state, 1, PhaseStatus.FAILED, str(e))
        state = add_audit_log(state, 1, "phase_failed", "error", error=str(e))
        print(f"❌ Phase 1 失敗: {e}")
        raise

    return state


def _generate_workflow_start_info(
    state: WorkflowState,
    skipped_phases: bool = False,
    target_repos: list[str] | None = None,
) -> str:
    """ワークフロー開始情報を生成（Notion calloutフォーマット）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    body_lines = [
        f"- **ID:** `{state['workflow_id']}`",
        f"- **Branch:** `{state['branch_name']}`",
        f"- **Started:** {now}",
    ]

    if target_repos:
        body_lines.append(f"- **Target:** {', '.join(target_repos)}")

    if skipped_phases:
        current_phase = state.get("current_phase", 1)
        body_lines.append(f"- **From:** Phase {current_phase}")

    return build_callout(**CALLOUT_WORKFLOW_START, body_lines=body_lines)


def _format_branch_reuse_error(repo_name: str, error: BranchReuseDenied) -> str:
    """BranchReuseDenied をユーザー向けの整形メッセージに変換する"""
    issues_text = "\n".join(f"  - {issue}" for issue in error.issues)
    return (
        f"🚫 {repo_name}: 既存ブランチ {error.branch_name} は "
        f"origin/{error.base_branch} と構造差分があります。\n"
        f"{issues_text}\n\n"
        f"まず base を取り込んでください:\n"
        f"  cd <repo_path>\n"
        f"  git fetch origin {error.base_branch}\n"
        f"  git rebase origin/{error.base_branch}   または   "
        f"git merge origin/{error.base_branch}\n\n"
        f"解決後に hokusai continue <workflow_id> で再開できます。"
    )
