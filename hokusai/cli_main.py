#!/usr/bin/env python3
"""
CLI Entry Point

ワークフロー管理のコマンドラインインターフェース。

Usage:
    workflow start <task_url>    新しいワークフローを開始
    workflow continue <id>       ワークフローを再開
    workflow status [id]         状態を表示
    workflow list                アクティブなワークフロー一覧

Options:
    -c, --config FILE    設定ファイルのパス
    -v, --verbose        詳細ログを出力
    --log-file FILE      ログファイルのパス
    --dry-run            実際には実行せず、何が起こるかを表示
"""

import argparse
import os
import sys
from pathlib import Path

from .cli import (
    check_environment,
    check_notion_connection,
)
from .config import create_config_from_env_and_file, set_config
from .logging_config import get_default_log_path, setup_logging
from .ui.console import (
    print_config_error,
    print_config_file,
    print_dry_run_mode,
    print_environment_warnings,
    print_error,
    print_from_phase_start,
    print_interrupted,
    print_step_mode,
    print_verbose_mode,
    print_workflow_id_result,
)
from .workflow import WorkflowRunner


def main():
    """メインエントリーポイント"""
    parser = argparse.ArgumentParser(
        description="LangGraph開発ワークフローCLI",
        prog="workflow",
    )

    # グローバルオプション
    parser.add_argument(
        "-c", "--config",
        help="設定ファイルのパス（例: configs/example-github-issue.yaml）",
        metavar="FILE",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="詳細ログを出力（デバッグ用）",
    )
    parser.add_argument(
        "--log-file",
        help="ログファイルのパス（省略時: --verboseの場合は自動生成）",
        metavar="FILE",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際には実行せず、何が起こるかを表示",
    )
    parser.add_argument(
        "--step",
        action="store_true",
        help="ステップ実行モード: 各フェーズ完了後に一時停止して確認",
    )

    subparsers = parser.add_subparsers(dest="command", help="コマンド")

    # start コマンド
    start_parser = subparsers.add_parser(
        "start",
        help="新しいワークフローを開始",
    )
    start_parser.add_argument(
        "task_url",
        help="NotionタスクページのURL",
    )
    start_parser.add_argument(
        "--from-phase",
        type=int,
        choices=range(1, 11),
        metavar="N",
        help="指定したフェーズから開始（1-10）。以前のフェーズはスキップ扱い",
    )
    start_parser.add_argument(
        "--branch",
        help="使用するブランチ名（--from-phase使用時に既存ブランチを指定）",
    )

    # continue コマンド
    continue_parser = subparsers.add_parser(
        "continue",
        help="中断したワークフローを再開",
    )
    continue_parser.add_argument(
        "workflow_id",
        help="ワークフローID",
    )
    continue_parser.add_argument(
        "--action",
        help="衛生チェック対応アクション（rebase, cherry-pick, merge-{base}, ignore）",
        default=None,
    )

    # status コマンド
    status_parser = subparsers.add_parser(
        "status",
        help="ワークフローの状態を表示",
    )
    status_parser.add_argument(
        "workflow_id",
        nargs="?",
        help="ワークフローID（省略時は全て表示）",
    )

    # list コマンド
    subparsers.add_parser(
        "list",
        help="アクティブなワークフロー一覧を表示",
    )

    # cleanup コマンド
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="ワークフローの worktree を削除",
    )
    cleanup_parser.add_argument(
        "workflow_id",
        nargs="?",
        help="ワークフローID（省略時は --stale が必要）",
    )
    cleanup_parser.add_argument(
        "--stale",
        action="store_true",
        help="完了済みまたは古い worktree を一括削除",
    )

    # pr-status コマンド
    pr_status_parser = subparsers.add_parser(
        "pr-status",
        help="PRのステータスを更新（オプションなしでGitHubから同期）",
    )
    pr_status_parser.add_argument(
        "workflow_id",
        help="ワークフローID",
    )
    pr_status_parser.add_argument(
        "pr_number",
        type=int,
        help="PR番号",
    )
    pr_status_parser.add_argument(
        "--status",
        choices=["pending", "approved", "changes_requested", "draft"],
        help="ワークフロー内ステータス（手動指定）",
    )
    pr_status_parser.add_argument(
        "--github-status",
        choices=["draft", "open", "merged", "closed"],
        help="GitHubステータス（手動指定）",
    )
    pr_status_parser.add_argument(
        "--no-sync",
        action="store_true",
        help="GitHubからの同期をスキップ（手動指定のみ）",
    )

    # connect コマンド（Phase C: gh / glab CLI 認証導線）
    connect_parser = subparsers.add_parser(
        "connect",
        help="外部サービスへの認証導線（gh / glab CLI を経由）",
    )
    connect_parser.add_argument(
        "service",
        nargs="?",
        choices=["github", "gitlab"],
        help="接続するサービス",
    )
    connect_parser.add_argument(
        "--status",
        action="store_true",
        help="全サービスの接続状態を表示",
    )
    connect_parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="認証コマンドを自動実行せず、表示のみにする",
    )
    connect_parser.add_argument(
        "--force",
        action="store_true",
        help="既に認証済みでも再認証を実行する",
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # ログ設定
    log_file = None
    if args.log_file:
        log_file = Path(args.log_file)
    elif args.verbose:
        # verboseモードの場合、デフォルトのログファイルに出力
        log_file = get_default_log_path()

    logger = setup_logging(verbose=args.verbose, log_file=log_file)

    if args.verbose:
        print_verbose_mode(log_file)

    if args.dry_run:
        print_dry_run_mode()

    if args.step:
        print_step_mode()

    # connect コマンドは config / Notion を必要としないため、早期に処理して終了する
    if args.command == "connect":
        from .cli.commands.connect import connect_service, show_status

        if args.status:
            sys.exit(show_status())
        if args.service:
            sys.exit(
                connect_service(
                    args.service,
                    no_interactive=args.no_interactive,
                    force=args.force,
                )
            )
        connect_parser.print_help()
        sys.exit(1)

    # 設定ファイルを読み込み
    try:
        config = create_config_from_env_and_file(args.config)
        set_config(config)
        if args.config:
            print_config_file(args.config)
        if args.verbose:
            logger.debug(f"プロジェクトルート: {config.project_root}")
            logger.debug(f"ベースブランチ: {config.base_branch}")
            logger.debug(f"ビルドコマンド: {config.build_command}")
            logger.debug(f"テストコマンド: {config.test_command}")
    except FileNotFoundError as e:
        print_config_error(str(e))
        sys.exit(1)

    # 環境設定チェック（start/continueコマンドの場合）
    if args.command in ("start", "continue"):
        env_warnings = check_environment()
        print_environment_warnings(env_warnings)

    # Notion接続確認（start/continueコマンドの場合）
    if args.command in ("start", "continue"):
        notion_ok, should_continue = check_notion_connection(dry_run=args.dry_run)
        if not should_continue:
            sys.exit(1)
        if not notion_ok:
            # Notion接続なしで続行する場合、環境変数でフラグを設定
            os.environ["HOKUSAI_SKIP_NOTION"] = "1"

    runner = WorkflowRunner(
        verbose=args.verbose,
        dry_run=args.dry_run,
        step_mode=args.step,
    )

    try:
        if args.command == "start":
            from_phase = getattr(args, "from_phase", None)
            branch = getattr(args, "branch", None)

            if from_phase and from_phase > 1:
                print_from_phase_start(from_phase, branch)

            workflow_id = runner.start(
                args.task_url,
                from_phase=from_phase,
                branch_name=branch,
            )
            print_workflow_id_result(workflow_id)

        elif args.command == "continue":
            runner.continue_workflow(args.workflow_id, action=args.action)

        elif args.command == "status":
            runner.status(args.workflow_id)

        elif args.command == "list":
            runner.status(None)

        elif args.command == "cleanup":
            _handle_cleanup(args, config)

        elif args.command == "pr-status":
            status_opt = args.status
            github_status_opt = getattr(args, "github_status", None)
            no_sync = getattr(args, "no_sync", False)

            # オプションが指定されていない場合はGitHubから同期
            if not status_opt and not github_status_opt and not no_sync:
                success, message = runner.sync_pr_status(
                    args.workflow_id,
                    args.pr_number,
                )
            else:
                success, message = runner.update_pr_status(
                    args.workflow_id,
                    args.pr_number,
                    status=status_opt,
                    github_status=github_status_opt,
                )

            if success:
                print(f"✓ {message}")
            else:
                print(f"✗ {message}")
                sys.exit(1)

    except KeyboardInterrupt:
        print_interrupted()
        sys.exit(130)

    except Exception as e:
        print_error(str(e))
        sys.exit(1)


def _handle_cleanup(args, config):
    """cleanup コマンドのハンドラ"""
    from .integrations.git import GitClient
    from .persistence import SQLiteStore

    store = SQLiteStore(config.database_path)

    if args.workflow_id:
        # 指定 workflow の worktree を削除
        state = store.load_workflow(args.workflow_id)
        if state is None:
            print(f"✗ ワークフロー '{args.workflow_id}' が見つかりません")
            sys.exit(1)

        cleaned = 0
        for repo in state.get("repositories", []):
            if not repo.get("worktree_created", False):
                continue
            source_path = repo.get("source_path", "")
            wt_path = repo.get("path", "")
            if not source_path or not wt_path:
                continue
            try:
                git = GitClient(source_path)
                git.remove_worktree(wt_path, force=True)
                print(f"🧹 削除: {wt_path}")
                cleaned += 1
            except Exception as e:
                print(f"⚠️ 削除失敗: {wt_path}: {e}")

        print(f"✓ {cleaned} 件の worktree を削除しました")

    elif args.stale:
        # 完了済み workflow の worktree を一括削除
        workflows = store.list_active_workflows()

        # アクティブでない workflow（Phase 10 完了済み）の state を取得
        # list_active_workflows は current_phase < 10 のみなので、
        # worktree_root を直接スキャンする
        worktree_root = config.worktree_root
        if not worktree_root.exists():
            print("✓ worktree ディレクトリが存在しません。削除対象なし。")
            return

        active_ids = {w["workflow_id"] for w in workflows}
        cleaned = 0

        for wt_dir in worktree_root.iterdir():
            if not wt_dir.is_dir():
                continue
            # ディレクトリ名から workflow_id を抽出（{repo_name}_{wf-xxxx}）
            parts = wt_dir.name.rsplit("_wf-", 1)
            if len(parts) == 2:
                wf_id = f"wf-{parts[1]}"
                if wf_id not in active_ids:
                    try:
                        import shutil
                        shutil.rmtree(wt_dir)
                        print(f"🧹 stale 削除: {wt_dir}")
                        cleaned += 1
                    except Exception as e:
                        print(f"⚠️ 削除失敗: {wt_dir}: {e}")

        # git worktree prune で削除済みディレクトリの登録を解除
        if cleaned > 0:
            for repo in config.get_all_repositories():
                try:
                    git = GitClient(str(repo.path))
                    git._run_git("worktree", "prune")
                except Exception:
                    pass

        print(f"✓ {cleaned} 件の stale worktree を削除しました")

    else:
        print("✗ workflow_id または --stale を指定してください")
        sys.exit(1)


if __name__ == "__main__":
    main()
