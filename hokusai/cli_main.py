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
        prog="hokusai",
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

    # sync-service-status コマンド: connection_status の結果を Notion へ反映
    # 主に cron / launchd から定期実行される想定
    subparsers.add_parser(
        "sync-service-status",
        help="connection_status の結果を Notion Service Status ページに反映",
    )

    # notion-setup コマンド: Notion 上に HOKUSAI 用 DB / ページを一括作成
    notion_setup_parser = subparsers.add_parser(
        "notion-setup",
        help="Notion 上に HOKUSAI 用 DB / ページを自動作成（初期セットアップ）",
    )
    notion_setup_parser.add_argument(
        "--parent-page-id",
        required=True,
        help="親ページの Notion page ID（HOKUSAI integration が接続済みであること）",
    )
    notion_setup_parser.add_argument(
        "--api-token-env",
        default="HOKUSAI_NOTION_API_TOKEN",
        help="API token を保持する環境変数名（デフォルト: HOKUSAI_NOTION_API_TOKEN）",
    )
    notion_setup_parser.add_argument(
        "--persist",
        action="store_true",
        help="作成された DB / ページ ID をシェル rc ファイル（~/.zshrc 等）に自動追記する",
    )
    notion_setup_parser.add_argument(
        "--shell-rc",
        default=None,
        help="--persist で書き込む rc ファイルのパス（省略時は SHELL から自動検出）",
    )
    notion_setup_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="--persist 時に rc ファイルのバックアップを作成しない（非推奨）",
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

    # sync-service-status コマンド: 設定読み込み後に Notion 反映を実行
    # config 読み込みは下で行うため、ここでは早期処理せず通常フローへ流す

    # notion-setup コマンドは config を必要とせず、Notion API token のみで動く
    # （セットアップ時点では config の DB ID 環境変数はまだ存在しない想定）
    if args.command == "notion-setup":
        sys.exit(_handle_notion_setup(args))

    # connect コマンドは config / Notion を必要としないため、早期に処理して終了する
    if args.command == "connect":
        from .cli.commands.connect import connect_service, show_status

        # service と --status は曖昧なので併用不可（argparse の mutually-exclusive
        # group は positional + flag の組み合わせを safely 扱えないため、明示的に
        # error にして usage を表示する）
        if args.service and args.status:
            connect_parser.error(
                "--status は service と同時に指定できません"
            )

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

        elif args.command == "sync-service-status":
            sys.exit(_handle_sync_service_status())

    except KeyboardInterrupt:
        print_interrupted()
        sys.exit(130)

    except Exception as e:
        print_error(str(e))
        sys.exit(1)


def _handle_notion_setup(args) -> int:
    """Notion 上に HOKUSAI 用 DB / ページを一括作成する初期セットアップ。

    親ページに HOKUSAI integration が接続済みであることが前提。
    成功時は環境変数の export コマンド例を出力する。
    --persist 指定時はシェル rc ファイルへ自動追記する。

    Returns:
        終了コード（0=成功、1=失敗）
    """
    from pathlib import Path

    from .integrations.notion_dashboard import (
        NotionSetupError,
        detect_shell_rc,
        persist_env_vars,
        setup_notion_workspace,
    )

    api_token = os.environ.get(args.api_token_env, "").strip()
    if not api_token:
        print(f"環境変数 {args.api_token_env} が設定されていません")
        print(f'  例: export {args.api_token_env}="secret_xxxxxxxxxx"')
        print(
            "  Internal Integration Token は https://www.notion.so/my-integrations から発行できます"
        )
        return 1

    print(
        f"親ページ {args.parent_page_id} の配下に HOKUSAI 用リソースを作成します..."
    )
    print()

    try:
        result = setup_notion_workspace(api_token, args.parent_page_id)
    except NotionSetupError as e:
        print(f"✗ セットアップ失敗: {e}")
        print()
        print("確認事項:")
        print("  1. 親ページが存在し、HOKUSAI integration が接続されているか")
        print("  2. API token が有効か（再発行が必要かもしれません）")
        print("  3. parent_page_id が正しいか（URL 末尾の 32 桁）")
        return 1
    except Exception as e:
        print(f"✗ 予期しないエラー: {type(e).__name__}: {e}")
        return 1

    print("✓ Notion ワークスペースのセットアップが完了しました\n")
    print("=" * 70)
    print("作成されたリソース:")
    print(f"  Workflows DB:          {result['workflows_db_id']}")
    print(f"  Pull Requests DB:      {result['pull_requests_db_id']}")
    print(f"  Service Status ページ: {result['service_status_page_id']}")
    print()
    print("以下を環境変数に設定してください（~/.zshrc などに追記推奨）:")
    print()
    print(f'  export HOKUSAI_NOTION_WORKFLOWS_DB_ID="{result["workflows_db_id"]}"')
    print(f'  export HOKUSAI_NOTION_PR_DB_ID="{result["pull_requests_db_id"]}"')
    print(
        f'  export HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID="{result["service_status_page_id"]}"'
    )

    # --persist 指定時は rc ファイルへ書き込む
    if getattr(args, "persist", False):
        rc_path = (
            Path(args.shell_rc).expanduser()
            if args.shell_rc
            else detect_shell_rc()
        )
        try:
            persist_result = persist_env_vars(
                rc_path,
                result,
                backup=not getattr(args, "no_backup", False),
            )
            print()
            print(
                f"✓ 環境変数を {persist_result['rc_path']} に "
                f"{'追記' if persist_result['action'] == 'appended' else '更新'}しました"
            )
            if persist_result.get("backup_path"):
                print(f"  バックアップ: {persist_result['backup_path']}")
            print()
            print("反映するには新しいターミナルを開くか、以下を実行:")
            print(f"  source {persist_result['rc_path']}")
        except Exception as e:
            print()
            print(f"⚠️ rc ファイルへの書き込みに失敗: {type(e).__name__}: {e}")
            print("  手動で上記の export コマンドを ~/.zshrc 等に追記してください")
    else:
        print()
        print("ヒント: --persist を付けると ~/.zshrc 等に自動追記できます")
        print("  hokusai notion-setup --parent-page-id <ID> --persist")

    print()
    print("次のステップ:")
    print("  1. YAML 設定で notion_dashboard.enabled: true を有効化")
    print("  2. hokusai sync-service-status で動作確認")
    print("  3. docs/notion-dashboard-operation-guide.md を参照")
    print("=" * 70)
    return 0


def _handle_sync_service_status() -> int:
    """connection_status の結果を Notion Service Status ページに反映する。

    cron / launchd から定期実行される想定。Notion 同期が無効、または環境変数
    未設定なら skipped を表示して 0 を返す（cron で error にしない）。

    Returns:
        終了コード（0=成功 / skip、1=失敗）
    """
    from .config import get_config
    from .integrations.notion_dashboard import (
        NotionSyncDispatcher,
        sync_service_status_to_notion,
    )
    from .persistence import SQLiteStore

    config = get_config()
    if not config.notion_dashboard.enabled:
        print("notion_dashboard.enabled=false のためスキップしました")
        return 0

    store = SQLiteStore(config.database_path)
    dispatcher = NotionSyncDispatcher(store=store, config=config.notion_dashboard)

    if not dispatcher.is_configured():
        print(
            "Notion 同期の環境変数が未設定のためスキップしました "
            f"(api_token={config.notion_dashboard.api_token_env}, "
            f"workflows_db={config.notion_dashboard.workflows_db_id_env})"
        )
        return 0

    ok = sync_service_status_to_notion(dispatcher)
    if ok:
        print("✓ Service Status を Notion に反映しました")
        return 0
    print("✗ Service Status の Notion 反映に失敗しました")
    return 1


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
