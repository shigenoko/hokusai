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
        choices=["github", "gitlab", "gemini"],
        help="接続するサービス（gemini は v0.4.6〜、cross-review 用途）",
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
        default=None,
        help=(
            "API token を保持する環境変数名。"
            "省略時は --profile 指定があれば profile config の "
            "notion_dashboard.api_token_env を採用、"
            "それも無ければ既定の HOKUSAI_NOTION_API_TOKEN を使う。"
        ),
    )
    notion_setup_parser.add_argument(
        "--persist",
        action="store_true",
        help="作成された DB / ページ ID をシェル rc ファイル（~/.zshrc 等）に自動追記する",
    )
    notion_setup_parser.add_argument(
        "--scaffold",
        action="store_true",
        help=(
            "ドキュメントツリーを自動作成する（v0.4.3〜、v0.4.5 でタイトル更新）。"
            "親ページ配下に Documentation（icon 📚）配下に 議論（💬）/ "
            "運用ガイド（📖）/ 要件定義（📋）の計 4 ページを作成。配置先パスごとに"
            "既存検出（idempotent）、v0.4.3（絵文字 prefix 付き）と v0.4.4（HOKUSAI"
            " prefix + 英語名）の旧タイトルも 2 世代分 legacy alias として検出して"
            "重複作成を回避する。"
        ),
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

    # notion-migrate-schema コマンド: 既存 DB に v0.4.8+ で追加された
    # Operator プロパティ等を後から追加する（Issue #21）
    notion_migrate_parser = subparsers.add_parser(
        "notion-migrate-schema",
        help="既存 HOKUSAI Workflows DB に v0.4.8+ の新プロパティを追加",
        parents=[shared_options],
    )
    notion_migrate_parser.add_argument(
        "--workflows-db-id",
        help=(
            "対象 Workflows DB の ID。省略時は profile config の "
            "notion_dashboard.workflows_db_id_env 経由で解決される。"
        ),
    )
    notion_migrate_parser.add_argument(
        "--api-token-env",
        default=None,
        help=(
            "Notion API token を保持する環境変数名。省略時は profile config の "
            "notion_dashboard.api_token_env、それも無ければ HOKUSAI_NOTION_API_TOKEN。"
        ),
    )
    notion_migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際の API 呼び出しを行わず、追加予定のプロパティのみ表示する。",
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

    # notion-setup コマンドは config を必須としないが、--profile 指定時は
    # profile config の env 名（notion_dashboard.api_token_env 等）を採用するため、
    # best-effort で config を読む。
    #
    # エラー方針（v0.4.1〜）:
    #   - profile 解決自体の失敗（ProfileError 系: 指定 profile が見つからない、
    #     registry がない、引数併用エラー等）→ 既定 env 名で続行すると意図しない
    #     Notion ワークスペースに対してセットアップを走らせるリスクがあるため、
    #     原因別のメッセージを出して明示エラーで終了する。
    #   - YAML 解析失敗・I/O エラーなど「profile は解決できたが config が壊れて
    #     いる」系 → 原則中断する（同様の誤注入リスクのため）。例外として
    #     `--api-token-env` が明示指定されている場合は、ユーザーが token env を
    #     明示選択しているため警告のみで続行する。
    if args.command == "notion-setup":
        from .config.profiles import (
            ConflictingProfileAndConfigError,
            InvalidProfileNameError,
            ProfileError,
            ProfileNotFoundError,
            ProfileRegistryNotFoundError,
        )

        notion_setup_profile = getattr(args, "profile", None)
        # 空文字や空白のみの profile 名は明示エラー（truthy 判定でスルーすると
        # 後段で profile 指定なし扱いとなり、--persist 時に rc 書き込みが失敗し
        # 得るため早期に弾く）。
        if notion_setup_profile is not None and not str(notion_setup_profile).strip():
            print(
                f"✗ --profile に空文字（または空白のみ）が指定されました: "
                f"{notion_setup_profile!r}"
            )
            print("  --profile を省略するか、有効な profile 名を指定してください")
            sys.exit(1)
        notion_setup_config = None
        if notion_setup_profile is not None:
            try:
                notion_setup_config_arg = getattr(args, "config", None)
                notion_setup_config = create_config_from_env_and_file(
                    notion_setup_config_arg, profile_name=notion_setup_profile
                )
            except ConflictingProfileAndConfigError as e:
                # --profile と --config の同時指定（引数の併用不可）
                print(f"✗ 引数の併用エラー: {e}")
                print("  --profile と --config はどちらか一方のみ指定してください")
                sys.exit(1)
            except ProfileNotFoundError as e:
                print(f"✗ profile '{notion_setup_profile}' が見つかりません: {e}")
                print(
                    "  確認: ~/.hokusai/profiles.yaml に "
                    f"'{notion_setup_profile}' が登録されているか"
                )
                sys.exit(1)
            except ProfileRegistryNotFoundError as e:
                print(f"✗ profile registry が見つかりません: {e}")
                print(
                    "  確認: ~/.hokusai/profiles.yaml を作成するか、"
                    "HOKUSAI_PROFILES_FILE 環境変数で path を指定してください"
                )
                sys.exit(1)
            except InvalidProfileNameError as e:
                print(f"✗ profile 名の形式が不正: {e}")
                sys.exit(1)
            except ProfileError as e:
                # 上記でカバーされない ProfileError 派生（YAML 構造エラー等）
                print(
                    f"✗ profile '{notion_setup_profile}' の registry 解析に失敗: "
                    f"{type(e).__name__}: {e}"
                )
                sys.exit(1)
            except Exception as e:
                # profile 解決自体は成功したが config 読み込みで失敗（YAML 解析
                # 失敗・I/O エラー等）。
                #
                # 既定の HOKUSAI_NOTION_API_TOKEN が別案件用に設定されている場合、
                # それを使って意図しない Notion ワークスペースにセットアップを
                #走らせてしまうリスクがあるため、安全側に倒して中断する。
                # ただし `--api-token-env` が明示指定されている場合は、ユーザー
                # が token env を明示的に選択している（誤注入リスクは限定的）ため
                # 警告のみで続行する。
                explicit_api_token_env = getattr(args, "api_token_env", None)
                if not explicit_api_token_env:
                    print(
                        f"✗ profile '{notion_setup_profile}' の config 読み込みに失敗: "
                        f"{type(e).__name__}: {e}"
                    )
                    print(
                        "  既定の env 変数名で続行すると、別案件用の "
                        "HOKUSAI_NOTION_API_TOKEN を誤って使うリスクがあるため中断します"
                    )
                    print(
                        "  対処: config の YAML を修正するか、"
                        "--api-token-env で env 名を明示指定してください"
                    )
                    sys.exit(1)
                print(
                    f"⚠️ profile '{notion_setup_profile}' の config 読み込みに失敗: "
                    f"{type(e).__name__}: {e}"
                )
                print(
                    f"  --api-token-env={explicit_api_token_env!r} が明示指定されているため "
                    f"既定 env 名フォールバックで続行します"
                )
        sys.exit(_handle_notion_setup(args, notion_setup_config))

    # notion-migrate-schema コマンド: 既存 Workflows DB に v0.4.8+ の新プロパティを追加
    if args.command == "notion-migrate-schema":
        # notion-setup と同等の厳密な profile / --config 解決を行う。
        # 別案件用の token / DB ID を誤って使うリスクを避けるため、
        # profile 解決失敗時の silent fallback は行わない（--dry-run は除く）。
        from .config.profiles import (
            ConflictingProfileAndConfigError,
            InvalidProfileNameError,
            ProfileError,
            ProfileNotFoundError,
            ProfileRegistryNotFoundError,
        )

        migrate_profile = getattr(args, "profile", None)
        if migrate_profile is not None and not str(migrate_profile).strip():
            print(
                f"✗ --profile に空文字（または空白のみ）が指定されました: "
                f"{migrate_profile!r}"
            )
            sys.exit(1)

        migrate_config = None
        if migrate_profile is not None or getattr(args, "config", None):
            try:
                migrate_config = create_config_from_env_and_file(
                    getattr(args, "config", None),
                    profile_name=migrate_profile,
                )
            except ConflictingProfileAndConfigError as e:
                print(f"✗ 引数の併用エラー: {e}")
                print("  --profile と --config はどちらか一方のみ指定してください")
                sys.exit(1)
            except (
                ProfileNotFoundError, ProfileRegistryNotFoundError,
                InvalidProfileNameError, ProfileError,
            ) as e:
                print(f"✗ profile '{migrate_profile}' の解決に失敗: {e}")
                sys.exit(1)
            except Exception as e:
                # config 読み込み失敗。--dry-run なら警告のみで続行（実 API は呼ばない）。
                if getattr(args, "dry_run", False):
                    print(
                        f"⚠️ config 読み込みに失敗: {type(e).__name__}: {e}"
                        "（--dry-run のため既定 env 名で続行）"
                    )
                else:
                    print(
                        f"✗ config 読み込みに失敗: {type(e).__name__}: {e}"
                    )
                    print(
                        "  対処: --dry-run で計画のみ確認するか、"
                        "config を修正してください"
                    )
                    sys.exit(1)
        sys.exit(_handle_notion_migrate_schema(args, migrate_config))

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
        profile_name=profile_arg,
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


def _handle_notion_setup(args, config=None) -> int:
    """Notion 上に HOKUSAI 用 DB / ページを一括作成する初期セットアップ。

    親ページに HOKUSAI integration が接続済みであることが前提。
    成功時は環境変数の export コマンド例を出力する。
    --persist 指定時はシェル rc ファイルへ自動追記する。

    env 名解決の優先順位（v0.4.1〜）:
      1. `--api-token-env` 等で CLI 明示指定された値
      2. config（profile 解決済み）の notion_dashboard.{api_token_env,
         workflows_db_id_env, pull_requests_db_id_env}
      3. 既定値（HOKUSAI_NOTION_API_TOKEN 等）

    Args:
        args: argparse の Namespace（api_token_env / parent_page_id 等）
        config: 任意。--profile 指定時のみ呼び出し側で best-effort に
            create_config_from_env_and_file() の結果を渡す。None の場合は
            既定 env 名（HOKUSAI_NOTION_API_TOKEN 等）にフォールバックする。

    Returns:
        終了コード（0=成功、1=失敗）
    """
    from pathlib import Path

    from .integrations.notion_dashboard import (
        NotionSetupError,
        detect_shell_rc,
        is_valid_env_var_name,
        persist_env_vars,
        setup_notion_workspace,
    )

    # config 由来の env 名は採用前にシェル変数名として妥当か検証する。
    # 不正値（空白 / 改行 / `;` 等）が混入すると rc 破損 / コマンド注入のリスクが
    # あるため、無効なら警告して既定値にフォールバックする。
    def _pick_env_name(
        cfg_value: object, default: str, role: str
    ) -> str:
        if cfg_value is None:
            return default
        if not is_valid_env_var_name(cfg_value):
            print(
                f"⚠️ profile config の {role}={cfg_value!r} は不正な env 変数名です。"
                f"既定値 {default!r} を使用します（[A-Za-z_][A-Za-z0-9_]* に合致する必要）"
            )
            return default
        return cfg_value

    # env 名解決: CLI 明示 > profile config > 既定値
    api_token_env = args.api_token_env  # None の可能性あり（v0.4.1〜 default が None）
    if api_token_env is not None and not is_valid_env_var_name(api_token_env):
        # CLI 明示でも不正値は中断する（誤って source した時に致命的なため）
        print(
            f"✗ --api-token-env={api_token_env!r} は不正な env 変数名です "
            f"（[A-Za-z_][A-Za-z0-9_]* に合致する必要があります）"
        )
        return 1
    workflows_env = "HOKUSAI_NOTION_WORKFLOWS_DB_ID"
    pull_requests_env = "HOKUSAI_NOTION_PR_DB_ID"

    profile_name = getattr(args, "profile", None)
    if config is not None:
        nd_cfg = getattr(config, "notion_dashboard", None)
        if nd_cfg is not None:
            if api_token_env is None:
                api_token_env = _pick_env_name(
                    getattr(nd_cfg, "api_token_env", None),
                    "HOKUSAI_NOTION_API_TOKEN",
                    "notion_dashboard.api_token_env",
                )
            workflows_env = _pick_env_name(
                getattr(nd_cfg, "workflows_db_id_env", None),
                workflows_env,
                "notion_dashboard.workflows_db_id_env",
            )
            pull_requests_env = _pick_env_name(
                getattr(nd_cfg, "pull_requests_db_id_env", None),
                pull_requests_env,
                "notion_dashboard.pull_requests_db_id_env",
            )

    if api_token_env is None:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"

    api_token = os.environ.get(api_token_env, "").strip()
    if not api_token:
        print(f"環境変数 {api_token_env} が設定されていません")
        print(f'  例: export {api_token_env}="secret_xxxxxxxxxx"')
        print(
            "  Internal Integration Token は https://www.notion.so/my-integrations から発行できます"
        )
        return 1

    print(
        f"親ページ {args.parent_page_id} の配下に HOKUSAI 用リソースを作成します..."
    )
    if profile_name:
        print(f"  Profile: {profile_name}")
    print(f"  API token env: {api_token_env}")
    print()

    scaffold_flag = getattr(args, "scaffold", False)
    try:
        result = setup_notion_workspace(
            api_token, args.parent_page_id, scaffold=scaffold_flag
        )
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

    # scaffold 結果（--scaffold 指定時のみ含まれる）
    scaffold_result = result.get("scaffold")
    if scaffold_result is not None:
        created = scaffold_result.get("created", [])
        skipped = scaffold_result.get("skipped", [])
        failed = scaffold_result.get("failed", [])
        error = scaffold_result.get("error")
        print()
        print("📚 ドキュメントツリー:")
        # 致命エラー（ハブ作成失敗等）は最初に出して成功と誤読されないようにする。
        if error:
            print(f"  ⚠️ scaffold 中にエラー: {error}")
        if created:
            for item in created:
                print(f"  ✓ 作成: {item['title']}")
        if skipped:
            for item in skipped:
                print(f"  - skip（既存）: {item['title']}")
        if failed:
            for item in failed:
                print(f"  ✗ 失敗: {item['title']}: {item.get('error', '')}")
        # 「変更なし」は error / failed が無い場合のみ表示する
        if not created and not skipped and not failed and not error:
            print("  （変更なし）")

    print()
    print("以下を環境変数に設定してください（~/.zshrc などに追記推奨）:")
    print()
    print(f'  export {workflows_env}="{result["workflows_db_id"]}"')
    print(f'  export {pull_requests_env}="{result["pull_requests_db_id"]}"')

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
                workflows_env_name=workflows_env,
                pull_requests_env_name=pull_requests_env,
                profile_name=profile_name,
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
    from .dashboard import DEFAULT_DASHBOARD_PORT, DashboardPortInUseError, start_dashboard

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

    # それでも未確定なら HOKUSAI の実効デフォルト port に解決
    # （None のまま start_dashboard に渡しても同様に解決されるが、CLI 側でも
    # 明示的に解決することでエラーメッセージに正しい port 番号が出る）
    if port is None:
        port = DEFAULT_DASHBOARD_PORT

    try:
        return start_dashboard(
            config,
            profile_name=profile_arg,
            port=port,
        )
    except DashboardPortInUseError as e:
        print(f"エラー: {e}")
        return 1
    except ValueError as e:
        # _port_in_use の range バリデーションエラー（port が 1..65535 範囲外）
        print(f"エラー: {e}")
        return 1
    except OSError as e:
        # _port_in_use が EADDRINUSE 以外（例: 特権ポート bind 時の EACCES）を
        # 再 raise するケース。スタックトレースで終了せず、ユーザに状況を説明する。
        import errno as _errno
        if e.errno == _errno.EACCES:
            print(
                f"エラー: port {port} への bind 権限がありません。"
                "特権ポート（<=1024）を指定していないか、別ユーザーが占有していないか確認してください。"
            )
        elif e.errno == _errno.EADDRNOTAVAIL:
            print(f"エラー: port {port} は利用不可な状態です: {e}")
        else:
            print(f"エラー: port {port} の確認中に予期しない OS エラー: {e}")
        return 1


def _handle_notion_migrate_schema(args, config=None) -> int:
    """既存 Workflows DB に v0.4.8+ で追加されたプロパティを追加する。

    Issue #21 / v0.4.8: 既存環境の Workflows DB に Operator (rich_text) を
    追加する。Notion API は同名プロパティが存在する場合は no-op になるため
    idempotent。

    解決順序:
    - api token env 名: CLI 明示 > profile config > "HOKUSAI_NOTION_API_TOKEN"
    - workflows_db_id: CLI 明示 > profile config の env 変数値

    Returns:
        0=成功 / 1=失敗
    """
    from .integrations.notion_dashboard.client import NotionAPIClient

    # 追加対象のプロパティ。将来 v0.4.x で追加されるプロパティもここに足せる。
    PROPERTIES_TO_ADD: dict = {
        "Operator": {"rich_text": {}},
    }

    dry_run = getattr(args, "dry_run", False)

    # api token env 名 / DB ID の解決順序
    # - api_token_env: CLI 明示 > profile config > "HOKUSAI_NOTION_API_TOKEN"
    # - workflows_db_id: CLI 明示 > profile config の env 変数 > 既定 HOKUSAI_NOTION_WORKFLOWS_DB_ID
    api_token_env = getattr(args, "api_token_env", None)
    workflows_db_id_env = None
    workflows_db_id = getattr(args, "workflows_db_id", None)

    if config is not None:
        nd_cfg = getattr(config, "notion_dashboard", None)
        if nd_cfg is not None:
            if api_token_env is None:
                api_token_env = getattr(nd_cfg, "api_token_env", None)
            workflows_db_id_env = getattr(nd_cfg, "workflows_db_id_env", None)

    if api_token_env is None:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
    if not workflows_db_id_env:
        # profile config 未指定または fields 未設定の場合、既定の env 名にフォールバック
        workflows_db_id_env = "HOKUSAI_NOTION_WORKFLOWS_DB_ID"

    if not workflows_db_id:
        workflows_db_id = os.environ.get(workflows_db_id_env)

    if not workflows_db_id:
        print(
            f"✗ Workflows DB ID が解決できません。--workflows-db-id <id> で明示するか、"
            f"環境変数 {workflows_db_id_env} を設定してください。"
        )
        return 1

    print(f"対象 Workflows DB: {workflows_db_id}")
    print("追加予定プロパティ:")
    for name, schema in PROPERTIES_TO_ADD.items():
        print(f"  - {name}: {schema}")

    # --dry-run は API 呼び出しを行わないため、token 未設定でも実行可能にする。
    if dry_run:
        print("--dry-run 指定のため API 呼び出しはスキップしました。")
        return 0

    api_token = os.environ.get(api_token_env)
    if not api_token:
        print(f"✗ API token 環境変数 {api_token_env} が設定されていません")
        return 1

    try:
        api = NotionAPIClient(api_token=api_token)
        result = api.update_database(
            workflows_db_id,
            {"properties": PROPERTIES_TO_ADD},
        )
    except Exception as e:
        print(f"✗ Workflows DB の schema 更新に失敗: {type(e).__name__}: {e}")
        return 1

    print("✓ Workflows DB schema を更新しました")
    print(f"  database id: {result.get('id', workflows_db_id)}")
    return 0


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

    v0.3.0 の検査範囲:
      1. config file が存在するか
      2. data_dir が存在するか（無ければ作成を試みる）
      3. dashboard port が他 profile と衝突していないか
      4. data_dir が他 profile と衝突していないか

    `--deep` フラグ: 受け付けるが実 API 接続確認は v0.4 以降で実装予定で、
                   現状は注意書きを表示するだけ。

    v0.3.0 では未実装（フォローアップで追加予定）:
      - env var 名（`api_token_env` 等）の存在確認
      - database_path / checkpoint_db_path / worktree_root 個別の衝突検出
      - Notion / Figma / Miro / Slack への実 API 接続確認（`--deep`）
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

    # 4. data_dir の他 profile との衝突
    # v0.3.0 では ProfileConfig.data_dir の一致のみ確認する。
    # database_path / checkpoint_db_path / worktree_root の個別衝突検出は
    # 各 profile config を読み込んで解決値で比較する必要があり、v0.4 以降。
    # data_dir 統一運用が主で個別 path override はレアケースのため、
    # data_dir 重複検出で実用上のカバレッジは確保される。
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

        # Phase E (v0.4.0): writeback errors / idempotency を 30 日経過で削除
        try:
            _cleanup_writeback_old_errors(config)
        except Exception as e:
            print(f"⚠️ writeback cleanup でエラー: {type(e).__name__}: {e}")

    else:
        print("✗ workflow_id または --stale を指定してください")
        sys.exit(1)


def _cleanup_writeback_old_errors(config) -> None:
    """Phase E (v0.4.0): figma/miro_sync_errors と design_writeback_idempotency の
    30 日経過行を削除する。

    `hokusai cleanup --stale` 実行時に同時に呼ばれる。Notion outbox cleanup と同様に
    backward-compatible（テーブル無くてもエラーにしない）。

    参考: docs/hokusai-figma-miro-writeback-implementation-plan.md §5.3, §11 (Step 7)
    """
    import sqlite3

    try:
        from .integrations.design.writeback import OutboxStore, WritebackTarget
    except ImportError:
        return  # writeback モジュール未配置（古い環境）

    db_path = config.database_path
    total = 0
    for target in (WritebackTarget.FIGMA, WritebackTarget.MIRO):
        try:
            store = OutboxStore(db_path, target=target)
            total += store.cleanup_old_errors(retention_days=30)
        except sqlite3.Error:
            # テーブル不在 / スキーマ古い等（v0.3.x DB）は無視
            # OS / I/O エラーやその他の異常は上位に伝播させ、運用者が
            # `hokusai cleanup --stale` の出力で気付けるようにする。
            continue
    if total > 0:
        print(f"🧹 writeback cleanup: {total} 件の 30 日経過 errors / idempotency を削除")


if __name__ == "__main__":
    main()
