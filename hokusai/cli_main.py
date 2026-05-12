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
    # 共有オプション parent: トップレベル / 各サブコマンドの両方で受け付けるため、
    # `hokusai --profile a start ...` と `hokusai start --profile a ...` の
    # どちらの順序でも動くようにする。
    #
    # default=argparse.SUPPRESS が必須:
    # parents=[shared_options] でサブパーサにも --profile を継承させると、
    # サブパーサ側のデフォルト値（None）がトップレベルで既に解析した値を
    # 上書きしてしまう問題がある。SUPPRESS にすると未指定時に namespace に
    # 属性そのものを追加しないため、トップレベルで設定された値が保持される。
    # アクセスは args.config / args.profile ではなく getattr(args, "config", None)
    # で行う。
    shared_options = argparse.ArgumentParser(add_help=False)
    shared_options.add_argument(
        "-c", "--config",
        help="設定ファイルのパス（例: configs/example-github-issue.yaml）",
        metavar="FILE",
        default=argparse.SUPPRESS,
    )
    shared_options.add_argument(
        "--profile",
        help="profile 名（~/.hokusai/profiles.yaml から解決）。-c/--config と同時指定不可",
        metavar="NAME",
        default=argparse.SUPPRESS,
    )

    parser = argparse.ArgumentParser(
        description="LangGraph開発ワークフローCLI",
        prog="hokusai",
        parents=[shared_options],
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
        parents=[shared_options],
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
        parents=[shared_options],
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
        parents=[shared_options],
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
        parents=[shared_options],
    )

    # cleanup コマンド
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="ワークフローの worktree を削除",
        parents=[shared_options],
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
        parents=[shared_options],
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
        parents=[shared_options],
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

    # notion-setup コマンド: Notion 上に HOKUSAI 用 DB / ページを一括作成
    notion_setup_parser = subparsers.add_parser(
        "notion-setup",
        help="Notion 上に HOKUSAI 用 DB / ページを自動作成（初期セットアップ）",
        parents=[shared_options],
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

    # profile コマンド: profile registry の管理
    profile_parser = subparsers.add_parser(
        "profile",
        help="profile（複数案件の実行スコープ）を管理",
        parents=[shared_options],
    )
    profile_subparsers = profile_parser.add_subparsers(
        dest="profile_subcommand",
        help="サブコマンド",
    )

    profile_subparsers.add_parser(
        "list",
        help="profile 一覧を表示",
    )

    profile_show_parser = profile_subparsers.add_parser(
        "show",
        help="単一 profile の解決結果を表示（シークレット値は含まない）",
    )
    profile_show_parser.add_argument("name", help="profile 名")

    profile_doctor_parser = profile_subparsers.add_parser(
        "doctor",
        help="profile 設定の整合性を診断",
    )
    profile_doctor_parser.add_argument("name", help="profile 名")
    profile_doctor_parser.add_argument(
        "--deep",
        action="store_true",
        help="実 API 接続まで踏み込んだ詳細診断（rate limit を消費するため明示指定）",
    )

    # dashboard コマンド: Operations Console を profile 指定で起動
    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Operations Console（Web Dashboard）を起動",
        parents=[shared_options],
    )
    dashboard_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="listen port（省略時は profile registry の dashboard.port → 8765）",
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

    # notion-setup コマンドは config を必要とせず、Notion API token のみで動く
    # （セットアップ時点では config の DB ID 環境変数はまだ存在しない想定）
    if args.command == "notion-setup":
        sys.exit(_handle_notion_setup(args))

    # profile コマンドは registry のみ参照し、WorkflowConfig は不要
    if args.command == "profile":
        sys.exit(_handle_profile_command(args, profile_parser))

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

    # 設定ファイルを読み込み（--profile が指定されれば registry から解決）
    # default=argparse.SUPPRESS の関係で args に属性が無い場合があるため getattr で取得
    config_arg = getattr(args, "config", None)
    profile_arg = getattr(args, "profile", None)
    try:
        config = create_config_from_env_and_file(
            config_arg, profile_name=profile_arg
        )
        set_config(config)
        if profile_arg:
            print(f"Profile: {profile_arg}")
        if config_arg:
            print_config_file(config_arg)
        if args.verbose:
            logger.debug(f"プロジェクトルート: {config.project_root}")
            logger.debug(f"ベースブランチ: {config.base_branch}")
            logger.debug(f"ビルドコマンド: {config.build_command}")
            logger.debug(f"テストコマンド: {config.test_command}")
    except FileNotFoundError as e:
        print_config_error(str(e))
        sys.exit(1)
    except Exception as e:
        # profile 系のエラー（ConflictingProfileAndConfigError /
        # ProfileNotFoundError / ProfileRegistryNotFoundError / ...）を含む
        from .config.profiles import ProfileError
        if isinstance(e, ProfileError):
            print_config_error(str(e))
            sys.exit(1)
        raise

    # dashboard コマンド: config 解決後に起動（WorkflowRunner は不要）
    if args.command == "dashboard":
        sys.exit(_handle_dashboard(args, config))

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
    print()
    print("以下を環境変数に設定してください（~/.zshrc などに追記推奨）:")
    print()
    print(f'  export HOKUSAI_NOTION_WORKFLOWS_DB_ID="{result["workflows_db_id"]}"')
    print(f'  export HOKUSAI_NOTION_PR_DB_ID="{result["pull_requests_db_id"]}"')

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
    print("  2. docs/notion-dashboard-operation-guide.md を参照")
    print("=" * 70)
    return 0


def _handle_dashboard(args, config) -> int:
    """`hokusai dashboard [--profile <name>] [--port <port>]` のハンドラ

    profile が指定されていれば registry から dashboard.port をフォールバック先に使う。
    config はすでに main() で profile 解決済み。
    """
    from .dashboard import DashboardPortInUseError, start_dashboard

    port = args.port
    profile_arg = getattr(args, "profile", None)

    # --port 未指定なら registry の dashboard.port を探す
    if port is None and profile_arg:
        try:
            from .config.profiles import load_profile_registry
            registry = load_profile_registry()
            p = registry.profiles.get(profile_arg)
            if p and p.dashboard_port:
                port = p.dashboard_port
        except Exception:
            # registry エラーはここでは無視（fallback でデフォルト port を使う）
            pass

    try:
        return start_dashboard(
            config,
            profile_name=profile_arg,
            port=port,
        )
    except DashboardPortInUseError as e:
        print(f"エラー: {e}")
        return 1


def _handle_profile_command(args, profile_parser) -> int:
    """profile サブコマンドのハンドラ

    profile list / show / doctor をルーティングする。registry のみを参照し、
    WorkflowConfig 生成は行わない（実装計画書 §6.2）。
    """
    from .config.profiles import (
        ProfileError,
        load_profile_registry,
        resolve_registry_path,
    )

    subcommand = getattr(args, "profile_subcommand", None)
    if subcommand is None:
        profile_parser.print_help()
        return 1

    try:
        registry = load_profile_registry()
    except ProfileError as e:
        registry_path = resolve_registry_path()
        print(f"エラー: {e}")
        print(f"  registry: {registry_path}")
        return 1

    if subcommand == "list":
        return _handle_profile_list(registry)
    if subcommand == "show":
        return _handle_profile_show(args.name, registry)
    if subcommand == "doctor":
        return _handle_profile_doctor(
            args.name, registry, deep=getattr(args, "deep", False)
        )

    profile_parser.print_help()
    return 1


def _handle_profile_list(registry) -> int:
    """`hokusai profile list` の実装"""
    if not registry.profiles:
        print("登録されている profile はありません。")
        print(f"  registry: {registry.source_path}")
        return 0

    print(f"{'PROFILE':<20} {'CONFIG':<50} {'DATA DIR'}")
    print("-" * 100)
    for name in registry.names():
        p = registry.profiles[name]
        data_dir = str(p.data_dir) if p.data_dir else "(default)"
        print(f"{name:<20} {str(p.config_path):<50} {data_dir}")

    if registry.default_profile:
        print()
        print(f"default_profile: {registry.default_profile}")
    return 0


def _handle_profile_show(name: str, registry) -> int:
    """`hokusai profile show <name>` の実装"""
    from .config.profiles import ProfileNotFoundError

    try:
        p = registry.get(name)
    except ProfileNotFoundError as e:
        print(f"エラー: {e}")
        return 1

    print(f"Profile: {p.name}")
    if p.label:
        print(f"  label:         {p.label}")
    if p.description:
        print(f"  description:   {p.description}")
    print(f"  config:        {p.config_path}")
    if p.data_dir:
        print(f"  data_dir:      {p.data_dir}")
    if p.dashboard_port:
        print(f"  dashboard:     port {p.dashboard_port}")
    print(f"  registry:      {registry.source_path}")
    print()
    print("  ※ シークレット値（API token 等）は表示されません。env var 名は")
    print("    profile config（YAML）内の `*_env` フィールドで確認してください。")
    return 0


def _handle_profile_doctor(name: str, registry, *, deep: bool = False) -> int:
    """`hokusai profile doctor <name>` の実装

    通常モード: 静的検査と env var 名の存在確認のみ。
    --deep: 実 API 接続まで踏み込む（Phase E で実装、現状は warning 表示）。
    """
    from .config.profiles import ProfileNotFoundError

    try:
        p = registry.get(name)
    except ProfileNotFoundError as e:
        print(f"エラー: {e}")
        return 1

    print(f"Diagnosing profile: {p.name}")
    print("-" * 60)

    issues: list[str] = []

    # 1. config file の存在
    if p.config_path.exists():
        print(f"  ✓ config file exists: {p.config_path}")
    else:
        msg = f"config file が見つかりません: {p.config_path}"
        print(f"  ✗ {msg}")
        issues.append(msg)

    # 2. data_dir の存在 / 作成可能性
    if p.data_dir:
        if p.data_dir.exists():
            print(f"  ✓ data_dir exists: {p.data_dir}")
        else:
            try:
                p.data_dir.mkdir(parents=True, exist_ok=True)
                print(f"  ✓ data_dir created: {p.data_dir}")
            except OSError as e:
                msg = f"data_dir が作成できません: {p.data_dir}: {e}"
                print(f"  ✗ {msg}")
                issues.append(msg)

    # 3. dashboard port の重複チェック（registry 内）
    if p.dashboard_port:
        conflicts = [
            other
            for other in registry.profiles.values()
            if other.name != p.name and other.dashboard_port == p.dashboard_port
        ]
        if conflicts:
            other_names = ", ".join(c.name for c in conflicts)
            msg = (
                f"dashboard port {p.dashboard_port} が他 profile と衝突: "
                f"{other_names}"
            )
            print(f"  ✗ {msg}")
            issues.append(msg)
        else:
            print(f"  ✓ dashboard port unique: {p.dashboard_port}")

    # 4. data_dir / database_path の他 profile との衝突
    if p.data_dir:
        path_conflicts = [
            other
            for other in registry.profiles.values()
            if other.name != p.name and other.data_dir == p.data_dir
        ]
        if path_conflicts:
            other_names = ", ".join(c.name for c in path_conflicts)
            msg = f"data_dir が他 profile と衝突: {other_names}"
            print(f"  ✗ {msg}")
            issues.append(msg)

    # 5. --deep モード: 実 API 接続確認（Phase E で実装予定）
    if deep:
        print()
        print("  [--deep] 実 API 接続確認は Phase E で実装予定")

    print("-" * 60)
    if issues:
        print(f"発見された問題: {len(issues)} 件")
        return 1

    print("OK: 問題ありません")
    return 0


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
