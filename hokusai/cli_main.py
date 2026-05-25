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


def _positive_retention_days(value: str) -> int:
    """argparse type=... 用の retention_days バリデータ（M2.5 / #100）。

    1 以上の整数のみ許容し、それ以外は argparse.ArgumentTypeError を投げて
    usage と共に非 0 終了させる（PR #101 Copilot Round 1 #2 指摘: user error
    を CLI 成功終了で隠さない）。
    """
    import argparse

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--retention-days には整数を指定してください: got {value!r}"
        ) from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            f"--retention-days は 1 以上の整数を指定してください: got {parsed}"
        )
    return parsed


def _build_parser():
    """CLI 用 argparse パーサを構築する。

    main() から分離している主な理由はテスタビリティ。例えば
    `hokusai --dry-run notion-migrate-schema` と
    `hokusai notion-migrate-schema --dry-run` で args.dry_run が
    意図通り True になるか（サブパーサが SUPPRESS で上書きしないか）を
    parser-level test で検証する。

    Returns:
        (parser, profile_parser, connect_parser): main() でハンドラ
        ディスパッチに使う 3 つの参照。
    """
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
    start_parser.add_argument(
        "--supersedes",
        default=None,
        metavar="WORKFLOW_ID",
        help=(
            "引き継ぎ元 workflow ID（要件 §9.3.2）。指定すると新 workflow の "
            "Workflows DB レコードの Supersedes リレーションに旧 workflow を "
            "紐付け、`hokusai prime` の handover_note 世代遡及で参照できる "
            "ようになる。"
        ),
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
    cleanup_parser.add_argument(
        "--cancel-reason",
        default=None,
        metavar="TEXT",
        help=(
            "cleanup で workflow を Canceled 化する際の理由を Workflows DB の "
            "Cancel Reason プロパティに記入する（要件 §9.3.2、引き継ぎ運用時に "
            "推奨）。Workflows DB ID が未設定の環境では no-op。"
        ),
    )
    cleanup_parser.add_argument(
        "--gc-workflows",
        action="store_true",
        help=(
            "完了済み workflow (current_phase >= 10) のうち --retention-days "
            "より古いものを workflow.db から cascade 削除する（findings §4.1 / "
            "Issue #100 / M2.5、opt-in なため明示指定が必要）"
        ),
    )
    cleanup_parser.add_argument(
        "--retention-days",
        type=_positive_retention_days,
        default=90,
        metavar="N",
        help=(
            "--gc-workflows と組み合わせて使う保持期間（日数、1 以上の整数）。"
            "既定 90 日。進行中 workflow (current_phase < 10) は absolutely "
            "削除されない。"
        ),
    )
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        # dest="cleanup_dry_run": トップレベル --dry-run (`print_dry_run_mode`
        # 用) と cleanup サブパーサの --dry-run (--stale worktree 削除を空振り
        # させる) は意味が異なるため、namespace 属性を分離する（Copilot Round 5
        # 指摘）。
        # 旧実装は dest 衝突 + SUPPRESS 併用で `hokusai --dry-run cleanup wf-x`
        # も M2.6 validation で reject されていたが、トップレベル --dry-run は
        # 元々 cleanup では no-op なので、後方互換のため別 dest に逃がす。
        # 参照は getattr(args, "cleanup_dry_run", False) で取る。
        dest="cleanup_dry_run",
        default=argparse.SUPPRESS,
        help=(
            "--stale と組み合わせて、実際の worktree 削除を行わず削除予定を "
            "列挙のみ（誤操作防止、findings §4.3 / Issue #107 / M2.6）。"
        ),
    )
    cleanup_parser.add_argument(
        "--sync-notion",
        action="store_true",
        help=(
            "--stale で削除した workflow について Notion Workflows DB の "
            "Status を Canceled 化し Cancel Reason='stale cleanup' を記入する"
            "（ゴースト残留防止、findings §4.3 / Issue #107 / M2.6）。"
        ),
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
            "notion_dashboard.workflows_db_id_env が指す env を参照し、"
            "それも無ければ既定 env HOKUSAI_NOTION_WORKFLOWS_DB_ID にフォールバックする。"
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
        # default=argparse.SUPPRESS:
        # トップレベルで先に解析された --dry-run（store_true なので未指定時 False）が
        # サブパーサの暗黙 default=False で上書きされ、
        # `hokusai --dry-run notion-migrate-schema` の意図に反して False になる問題を回避。
        # 未指定時は属性自体を追加しないことで、トップレベルの値を保持する。
        # 参照側は getattr(args, "dry_run", False) で取得する。
        default=argparse.SUPPRESS,
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

    # prime コマンド: active Project Memory を Agent prompt へ要約注入する
    # ためのテキストを出力する（Workgraph Phase 6 / Issue #48）
    prime_parser = subparsers.add_parser(
        "prime",
        help="active Project Memory を Agent prompt 用に出力（Workgraph Phase 6）",
        parents=[shared_options],
    )
    prime_parser.add_argument(
        "workflow_id",
        help="ワークフローID（state から current phase / profile を解決）",
    )
    prime_parser.add_argument(
        "--phase",
        default=None,
        help=(
            "対象 phase を上書き指定（例 phase5）。未指定なら workflow state の "
            "current_phase を採用、それも無ければ phase フィルタ無し。"
        ),
    )
    prime_parser.add_argument(
        "--type",
        action="append",
        dest="memory_types",
        default=None,
        help=(
            "対象 Memory Type を絞り込む（複数指定可: --type project_rule "
            "--type avoidance）。未指定なら全 Type。"
        ),
    )
    prime_parser.add_argument(
        "--output",
        choices=("markdown", "json"),
        default="markdown",
        help="出力形式（既定 markdown）",
    )

    return parser, profile_parser, connect_parser


def main():
    """メインエントリーポイント"""
    parser, profile_parser, connect_parser = _build_parser()

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
        # profile 解決失敗（ProfileError 系）は --dry-run でも常に exit 1 で中断する。
        # 例外として、汎用の config 読み込み失敗（例: ファイル parse error）のみ
        # --dry-run 時に警告で続行を許可する（API を叩かないため）。
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
    profile_arg_explicit = getattr(args, "profile", None)
    # M2.3 (#94): explicit と implicit (default_profile) を CLI 表示で区別する
    # ため、create_config_from_env_and_file を呼ぶ前に implicit 解決を試みて
    # 結果を控える。PR #95 Copilot Round 1 #3 指摘で、implicit 解決した値を
    # WorkflowRunner / dashboard 等の subcommand handler にも一貫して渡す
    # よう、effective_profile を作って args.profile に反映する形に変更
    # （state["profile_name"] や Notion 同期での profile_name 伝搬を担保）。
    implicit_default_profile: str | None = None
    if profile_arg_explicit is None and config_arg is None:
        from .config.profiles import try_resolve_default_profile_name
        implicit_default_profile = try_resolve_default_profile_name()
    # explicit が None のときだけ implicit に流れる。truthy 評価で or を使うと
    # explicit "--profile ''" が silent に default_profile へ置き換わって挙動が
    # 変わるため、None かどうかで明示的に分岐する（PR #95 Copilot Round 2 指摘）。
    # 空文字 / whitespace-only な値は manager 側の `if profile_name is not None:`
    # 分岐 (Round 3 指摘で追加) を経て resolve_profile_to_config_path に渡り、
    # validate_profile_name によって InvalidProfileNameError で明示的に reject
    # される（silent な claude-workflow.yaml 探索フォールバックには流れない）。
    profile_arg = (
        profile_arg_explicit
        if profile_arg_explicit is not None
        else implicit_default_profile
    )
    if implicit_default_profile and profile_arg_explicit is None:
        # subcommand handler が args.profile を直接読む経路 (dashboard /
        # notion-setup 等) でも implicit 解決後の値が見えるよう namespace
        # に反映する。
        args.profile = profile_arg
    try:
        config = create_config_from_env_and_file(
            config_arg, profile_name=profile_arg
        )
        set_config(config)
        if profile_arg_explicit:
            print(f"Profile: {profile_arg_explicit}")
        elif implicit_default_profile:
            # M2.3 (#94): --profile 未指定で default_profile が auto-resolve
            # された場合、明示指定と区別できる文言で user に通知。
            print(f"Profile: {implicit_default_profile} (default_profile)")
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

    # M2.1 (#96): HOKUSAI_SKIP_NOTION=1 が起動時 env で pre-set されている
    # ケースの profile 整合性 warning。check_notion_connection が後段で
    # set する経路と区別するため、subcommand dispatch より前 (config 解決
    # 直後) で判定する。
    _warn_if_skip_notion_pre_set(config, profile_arg)

    # Issue #111 / C. SKIP_NOTION profile 化:
    # 解決済み profile を HOKUSAI_ACTIVE_PROFILE env に export し、後段の
    # core パス（state / workflow / dispatcher / connection_status）で
    # is_skip_notion() が profile-aware lookup できるようにする。
    # legacy 経路 (notion_helpers / 各 _safe_notion_dispatch warning) は
    # 後続 PR で置換する（本 PR スコープ外）。
    from .utils.skip_notion import set_active_profile
    set_active_profile(profile_arg or implicit_default_profile)

    # dashboard コマンド: config 解決後に起動（WorkflowRunner は不要）
    if args.command == "dashboard":
        sys.exit(_handle_dashboard(args, config))

    # prime コマンド: active Project Memory を Agent prompt 用に出力
    if args.command == "prime":
        sys.exit(_handle_prime(args, config))

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

            # M0.2: Notion DB share 健全性チェック（Issue #82）。enabled かつ
            # 必要 env が揃っているケースで、各 DB に integration が share
            # されているかを retrieve_database で事前確認する。失敗があれば
            # warning を表示するが workflow start は継続（fail-open）。
            # `--dry-run` のときは API call を抑止する（check_notion_connection
            # と同じ規約、Issue #82 Copilot Round 2 指摘）。
            if not getattr(args, "dry_run", False):
                _print_notion_db_share_warnings(config)

            workflow_id = runner.start(
                args.task_url,
                from_phase=from_phase,
                branch_name=branch,
                supersedes_workflow_id=getattr(args, "supersedes", None),
            )
            print_workflow_id_result(workflow_id)

        elif args.command == "continue":
            runner.continue_workflow(args.workflow_id, action=args.action)

        elif args.command == "status":
            runner.status(args.workflow_id, verbose=args.verbose)

        elif args.command == "list":
            runner.status(None, verbose=args.verbose)

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
         workflows_db_id_env, pull_requests_db_id_env, review_issues_db_id_env}
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
    review_issues_env = "HOKUSAI_NOTION_REVIEW_ISSUES_DB_ID"

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
            review_issues_env = _pick_env_name(
                getattr(nd_cfg, "review_issues_db_id_env", None),
                review_issues_env,
                "notion_dashboard.review_issues_db_id_env",
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
    if result.get("review_issues_db_id"):
        print(f"  Review Issues DB:      {result['review_issues_db_id']}")

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
    if result.get("review_issues_db_id"):
        print(f'  export {review_issues_env}="{result["review_issues_db_id"]}"')

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
                review_issues_env_name=review_issues_env,
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


def _handle_prime(args, config) -> int:
    """`hokusai prime <workflow-id>` のハンドラ（Workgraph Phase 6 / Issue #48）。

    active Project Memory を要約整形して stdout に出力する。

    解決順序:
    - profile: CLI 明示 (--profile) > workflow state["profile_name"]（後発で
      上書きされた値）
    - current_phase: CLI 明示 (--phase) > workflow state["current_phase"]
    - Notion API token / Project Memory DB ID: profile config（既存
      notion_dashboard config の解決則と同じ）

    Project Memory DB が未設定 / Notion 未接続なら出力 0 件で gracefully
    skip（exit 0、Agent prompt として「memory なし」状態を許容）。

    Returns:
        0: 正常出力 / 1: workflow_id が SQLite に無い等の致命エラー
    """
    import os

    from .integrations.notion_dashboard.client import NotionAPIClient
    from .integrations.notion_dashboard.prime_renderer import (
        render_prime_json,
        render_prime_markdown,
    )
    from .integrations.notion_dashboard.project_memory_db import (
        ProjectMemoryDBClient,
    )
    from .persistence import SQLiteStore

    workflow_id: str = args.workflow_id
    cli_phase: str | None = getattr(args, "phase", None)
    memory_types = getattr(args, "memory_types", None)
    output_format: str = getattr(args, "output", "markdown")
    profile_arg = getattr(args, "profile", None)

    # workflow state を SQLite から取得（profile / current_phase の解決源）
    store = SQLiteStore(config.database_path)
    state = store.load_workflow(workflow_id)
    if state is None:
        print(
            f"✗ workflow '{workflow_id}' が見つかりません",
            file=sys.stderr,
        )
        return 1

    resolved_profile = profile_arg or state.get("profile_name")
    # workflow state の current_phase は int（1..10）で保存される一方、
    # Project Memory DB の Applies To は `phase1`..`phase10` 文字列なので、
    # state 由来の int は `phase{n}` に正規化する。CLI `--phase` の文字列指定
    # はそのまま優先採用（呼び出し側責任で `phase5` 等を渡す）。Copilot 指摘。
    resolved_phase: str | None = cli_phase
    if resolved_phase is None:
        raw_phase = state.get("current_phase")
        if isinstance(raw_phase, int) and 1 <= raw_phase <= 10:
            resolved_phase = f"phase{raw_phase}"
        elif isinstance(raw_phase, str) and raw_phase:
            # 既に文字列で渡ってきた場合はそのまま採用
            resolved_phase = raw_phase

    # `--profile` も `--config` も指定されていない場合に限り、workflow
    # state の profile_name に基づいて config を再ロードする。
    # - `--config` 明示時はそちらを尊重して再ロードしない（明示 > state）
    # - `--profile` 明示時は既に正しい profile が解決済みなので再ロード不要
    # - 上記いずれも未指定で state.profile_name があるときのみ、別 profile
    #   の env を引かないよう state 側 profile の config に切り替える
    # （WorkflowConfig 自体は profile_name 属性を持たないため、CLI 引数の
    # 有無で分岐する：Copilot 指摘）。再ロード失敗時は warning + 既存 config 続行。
    config_arg = getattr(args, "config", None)
    state_profile = state.get("profile_name")
    if (
        profile_arg is None
        and not config_arg
        and state_profile
    ):
        try:
            from .config import create_config_from_env_and_file
            config = create_config_from_env_and_file(
                None, profile_name=state_profile
            )
        except Exception as e:
            print(
                f"⚠ state の profile '{state_profile}' に基づく config "
                f"再ロード失敗（既存 config で続行）: {e}",
                file=sys.stderr,
            )

    # Project Memory DB が未設定なら memory 0 件の prime を返す（後方互換）
    notion_cfg = getattr(config, "notion_dashboard", None)
    api_token = ""
    db_id = ""
    if notion_cfg is not None and getattr(notion_cfg, "enabled", False):
        api_token = os.environ.get(notion_cfg.api_token_env, "").strip()
        db_id = os.environ.get(
            notion_cfg.project_memory_db_id_env, ""
        ).strip()

    # Workflows DB ID は handover_note 世代遡及 / workgraph context 統合で使う
    # （Issue #52 / #54 / 要件 §8.4）
    workflows_db_id = ""
    work_items_db_id = ""
    review_issues_db_id = ""
    workflow_gates_db_id = ""
    if notion_cfg is not None and getattr(notion_cfg, "enabled", False):
        workflows_db_id = os.environ.get(
            getattr(notion_cfg, "workflows_db_id_env", ""), ""
        ).strip()
        work_items_db_id = os.environ.get(
            getattr(notion_cfg, "work_items_db_id_env", ""), ""
        ).strip()
        review_issues_db_id = os.environ.get(
            getattr(notion_cfg, "review_issues_db_id_env", ""), ""
        ).strip()
        workflow_gates_db_id = os.environ.get(
            getattr(notion_cfg, "workflow_gates_db_id_env", ""), ""
        ).strip()

    memories: list[dict] = []
    # workgraph context 3 カテゴリは「未取得」と「取得済み 0 件」を JSON 出力
    # で区別するため None で初期化（Copilot 指摘）。`None` のままになる条件:
    #   1. 該当 DB ID が未設定（環境変数未設定 / profile config 未対応）
    #   2. workflows_db_id が未設定で workflow_page_id を解決できない
    #      （3 カテゴリは全て Workflow relation で絞るため必須）
    #   3. workflows_db_id があっても `find_workflow_page_id` が None を返した
    #      （workflow が Notion 側に未同期 / Workflows DB に存在しない）
    # 各 DB ID + workflow_page_id が揃って fetch を実際に試みた場合のみ list
    # に切り替え、API 障害時も list（部分結果保持）として返る。
    work_items: list[dict] | None = None
    review_issues: list[dict] | None = None
    gates: list[dict] | None = None
    # M2.4 (#92) Copilot 1 回目指摘: Notion fetch 例外時に memories=[] のまま
    # 落ちると diagnostics で「取得済 0 件」と誤認するため、except 句でフラグ
    # を立てて _build_prime_diagnostics に渡し、明示的な「取得失敗」行を出す。
    notion_fetch_error: str | None = None
    if api_token and db_id:
        try:
            api = NotionAPIClient(
                api_token=api_token,
                requests_per_second=getattr(
                    notion_cfg.rate_limit, "requests_per_second", 3.0
                ),
            )
            client = ProjectMemoryDBClient(api=api, database_id=db_id)
            memories = client.list_active_memories(
                profile=resolved_profile,
                phase=resolved_phase,
                types=memory_types,
            )
            # handover_note 世代遡及 + workgraph context 統合（要件 §8.4）。
            # Workflows DB ID 未設定 / API 障害なら handover skip（graceful
            # degrade）。--type で handover_note を除外している場合も skip。
            inject_handover = bool(workflows_db_id) and (
                memory_types is None
                or "handover_note" in (memory_types or [])
            )
            # workflow_page_id 解決は handover 注入 OR workgraph context fetch
            # の少なくとも 1 つが必要な場合に限る（Copilot 指摘: 不要な
            # find_workflow_page_id 呼び出しを避けて API 呼び出しを最小化）。
            need_page_id = inject_handover or bool(
                work_items_db_id or review_issues_db_id or workflow_gates_db_id
            )
            current_page_id = None
            wf_client = None
            if workflows_db_id and need_page_id:
                from .integrations.notion_dashboard.workflows_db import (
                    WorkflowsDBClient,
                )
                wf_client = WorkflowsDBClient(
                    api=api, database_id=workflows_db_id
                )
                current_page_id = wf_client.find_workflow_page_id(workflow_id)

            if inject_handover and wf_client is not None and current_page_id:
                # 解決済の current_page_id を渡して重複検索を避ける（Copilot 指摘）
                handover_memories = _collect_handover_notes(
                    wf_client=wf_client,
                    pm_client=client,
                    workflow_id=workflow_id,
                    profile=resolved_profile,
                    start_page_id=current_page_id,
                )
                memories = _merge_memories_dedup(memories, handover_memories)

            # workgraph context fetch（current workflow に紐づく ready Work Item /
            # open Review Issue / pending Gate）。各 DB ID 未設定なら該当 section
            # を skip（既存 graceful degrade と整合、Issue #54）。
            if current_page_id and work_items_db_id:
                from .integrations.notion_dashboard.work_items_db import (
                    WorkItemsDBClient,
                )
                wi_client = WorkItemsDBClient(
                    api=api, database_id=work_items_db_id
                )
                work_items = wi_client.list_ready_work_items_for_workflow(
                    current_page_id
                )
            if current_page_id and review_issues_db_id:
                from .integrations.notion_dashboard.review_issues_db import (
                    ReviewIssuesDBClient,
                )
                ri_client = ReviewIssuesDBClient(
                    api=api, database_id=review_issues_db_id
                )
                review_issues = ri_client.list_open_review_issues_for_workflow(
                    current_page_id
                )
            if current_page_id and workflow_gates_db_id:
                from .integrations.notion_dashboard.workflow_gates_db import (
                    WorkflowGatesDBClient,
                )
                wg_client = WorkflowGatesDBClient(
                    api=api, database_id=workflow_gates_db_id
                )
                gates = wg_client.list_pending_gates_for_workflow(
                    current_page_id
                )
        except Exception as e:
            # Notion fetch（Memory / Workflows / Work Items / Review Issues /
            # Gates いずれか）で例外が出た場合、取得済みの部分結果で続行
            # （Copilot 指摘で「Project Memory」固有メッセージ → generic 化）。
            # M2.4 Copilot 1 回目指摘: stderr の warning は stdout-only 運用
            # (例: hokusai prime ... > prime.md) では見えないため、フラグ化
            # して diagnostics 経路で stdout 側にも「取得失敗」を必ず残す。
            notion_fetch_error = f"{type(e).__name__}: {e}"
            print(
                f"⚠ prime context（Notion）取得で失敗（部分結果で続行）: {e}",
                file=sys.stderr,
            )

    # M2.4 (#92): 空状態の prime 出力で「なぜ空か」（DB share 未完了 / env
    # 未設定 / 取得済 0 件 / Notion 障害）を原因切り分けできるよう、構成
    # 要素ごとの状態を診断行リストに組み立てる（findings §2.1）。Markdown
    # 側では has_any=False のときだけ表示、JSON 側は常に key を残す。
    diagnostics = _build_prime_diagnostics(
        notion_cfg=notion_cfg,
        api_token=api_token,
        memories_db_id=db_id,
        memories=memories,
        workflows_db_id=workflows_db_id,
        work_items_db_id=work_items_db_id,
        work_items=work_items,
        review_issues_db_id=review_issues_db_id,
        review_issues=review_issues,
        workflow_gates_db_id=workflow_gates_db_id,
        gates=gates,
        notion_fetch_error=notion_fetch_error,
    )

    if output_format == "json":
        sys.stdout.write(
            render_prime_json(
                workflow_id=workflow_id,
                profile=resolved_profile,
                current_phase=resolved_phase,
                memories=memories,
                work_items=work_items,
                review_issues=review_issues,
                gates=gates,
                diagnostics=diagnostics,
            )
        )
    else:
        sys.stdout.write(
            render_prime_markdown(
                workflow_id=workflow_id,
                profile=resolved_profile,
                current_phase=resolved_phase,
                memories=memories,
                work_items=work_items,
                review_issues=review_issues,
                gates=gates,
                diagnostics=diagnostics,
            )
        )
    return 0


def _build_prime_diagnostics(
    *,
    notion_cfg,
    api_token: str,
    memories_db_id: str,
    memories: list[dict],
    workflows_db_id: str,
    work_items_db_id: str,
    work_items: list[dict] | None,
    review_issues_db_id: str,
    review_issues: list[dict] | None,
    workflow_gates_db_id: str,
    gates: list[dict] | None,
    notion_fetch_error: str | None = None,
) -> list[str]:
    """`hokusai prime` 出力用の診断行リストを組み立てる（M2.4 / #92）。

    `_handle_prime` の fetch ロジックと整合する観点で、各構成要素の状態を
    以下のいずれかの 1 行文字列で表現する:

    - Notion 連携自体が無効: `Notion 連携: 無効 (notion_dashboard.enabled=false)`
    - Notion fetch 例外発生: `Notion: 取得失敗 (<exception type>: ...)`
      → 0 件と区別するため、token / DB ID の有無より先に出す（Copilot 1 回目
      指摘: stderr warning は stdout-only 運用で見えないので diagnostics 経路
      でも明示）
    - API token 未設定: `Notion API Token: 未設定 (env XXX)`
    - 各 DB ID 未設定: `<DB 名>: 未設定 (env XXX)`
    - 取得未試行（None になる理由を api_token / workflows_db_id の状態で分岐:
      Copilot 1 回目指摘で「Workflows DB ID 未設定」一律表示は誤誘導と判明）:
      `<DB 名>: 未取得 (<具体的な理由>)`
    - 取得済 0 件: `<DB 名>: 取得済 0 件`

    出力は呼び出し側（renderer）が italic bullet として整形する想定。"""
    diagnostics: list[str] = []

    if notion_cfg is None or not getattr(notion_cfg, "enabled", False):
        diagnostics.append(
            "Notion 連携: 無効 (notion_dashboard.enabled=false)"
        )
        return diagnostics

    # Notion fetch 例外があった場合は最初に明示（0 件と区別、stdout 経路で見える）
    if notion_fetch_error:
        diagnostics.append(f"Notion: 取得失敗 ({notion_fetch_error})")

    # API token 単独で判定（token が無いと _handle_prime はそもそも Notion
    # 呼び出しを走らせず memories=[] / 他=None になるので、その前に出す）
    if not api_token:
        diagnostics.append(
            f"Notion API Token: 未設定 (env {notion_cfg.api_token_env})"
        )

    # Project Memory DB
    if not memories_db_id:
        diagnostics.append(
            "Project Memory DB: 未設定 "
            f"(env {notion_cfg.project_memory_db_id_env})"
        )
    elif notion_fetch_error:
        # fetch 例外時は 0 件と区別するため Project Memory DB レベルでは
        # 何も追加しない（先頭の「Notion: 取得失敗」で全体状態を表現済み）
        pass
    elif api_token and not memories:
        # token + db_id が揃って fetch を試みたが 0 件
        diagnostics.append("Project Memory DB: 取得済 0 件")
    elif not api_token:
        # token 無しなら memories は [] のまま fetch されない
        diagnostics.append("Project Memory DB: 未取得 (API Token 未設定)")

    # Workflows DB（handover_note / workgraph context 3 カテゴリの workflow
    # relation 絞りに必須）
    if not workflows_db_id:
        diagnostics.append(
            "Workflows DB: 未設定 "
            f"(env {getattr(notion_cfg, 'workflows_db_id_env', '')}) "
            "→ Work Items / Review Issues / Gates / handover_note 遡及が skip"
        )

    # workgraph context 3 カテゴリ: list=取得済、None=未取得（DB ID 未設定 or
    # workflow_page_id 解決失敗 or API token 未設定 or fetch 例外）
    def _section_status(label: str, db_id: str, env_key: str, data) -> str | None:
        if not db_id:
            return f"{label}: 未設定 (env {getattr(notion_cfg, env_key, '')})"
        if data is None:
            # DB ID あるが取得が走らなかった理由を分岐:
            # 優先順位 1: fetch 例外発生時は「取得失敗」（先頭で全体出力済）
            # 優先順位 2: API token 未設定 → そもそも fetch ループに入らない
            # 優先順位 3: workflows_db_id 未設定 → workflow_page_id 解決不能
            # 優先順位 4: 上記以外 → workflow が Notion 側に未同期
            # （Copilot 1 回目指摘: 一律「Workflows DB ID 未設定」は誤誘導）
            if notion_fetch_error:
                return f"{label}: 未取得 (Notion 取得失敗のため skip)"
            if not api_token:
                return f"{label}: 未取得 (API Token 未設定)"
            if not workflows_db_id:
                return f"{label}: 未取得 (Workflows DB ID 未設定)"
            return f"{label}: 未取得 (workflow が Notion 側に未同期)"
        if not data:
            return f"{label}: 取得済 0 件"
        return None  # 取得済かつ非空 → diagnostics に出さない

    for label, db_id, env_key, data in (
        ("Work Items DB", work_items_db_id, "work_items_db_id_env", work_items),
        ("Review Issues DB", review_issues_db_id, "review_issues_db_id_env", review_issues),
        ("Workflow Gates DB", workflow_gates_db_id, "workflow_gates_db_id_env", gates),
    ):
        msg = _section_status(label, db_id, env_key, data)
        if msg:
            diagnostics.append(msg)

    return diagnostics


def _collect_handover_notes(
    *,
    wf_client,
    pm_client,
    workflow_id: str,
    profile: str | None,
    max_depth: int = 3,
    start_page_id: str | None = None,
) -> list[dict]:
    """Supersedes リレーションを辿って active handover_note を集める
    （Workgraph Phase 7 / Issue #52 / 要件 §8.4 lookup rule）。

    - 起点 workflow_id → page_id を解決（`start_page_id` で事前解決済みのものを
      渡せば再検索を skip、Issue #54 Copilot 指摘）
    - `get_supersedes` で旧 workflow page_id を取得（最大 `max_depth` 世代まで遡る）
    - 各旧 workflow について `find_handover_notes_for_workflow` を呼び active を集める
    - 環状回避: 訪問済 page_id は再訪しない
    - すべての段階で失敗時は部分結果を返す（prime 全消失より部分提供を優先）

    起点 workflow 自身は対象外（current workflow に handover_note を残すケース
    は無く、handover_note は「前任から渡される」もの: 要件 §8.6）。
    """
    from .integrations.notion_dashboard.client import (
        NotionAPIError,
        NotionRateLimitError,
    )

    if start_page_id is None:
        start_page_id = wf_client.find_workflow_page_id(workflow_id)
    if not start_page_id:
        return []

    visited: set[str] = {start_page_id}
    chain: list[str] = []
    current_page_id = start_page_id
    for _ in range(max_depth):
        try:
            priors = wf_client.get_supersedes(current_page_id)
        except (NotionAPIError, NotionRateLimitError) as e:
            # API 系例外のみ握り潰して graceful degrade。stderr に warning を
            # 出して原因調査を可能にする（Copilot 指摘: 「Supersedes 未設定 =
            # 空リスト」と「Notion 障害」を区別するため、`get_supersedes` 側で
            # API 系例外を伝播させる設計に変更）。それ以外の例外は呼び出し元の
            # 大きな try-except に任せる（バグ早期発見）。
            print(
                f"⚠ handover_note 世代遡及で get_supersedes が失敗（"
                f"page_id={current_page_id[:8]}...）。chain 打ち切り: {e}",
                file=sys.stderr,
            )
            break
        if not priors:
            break
        # single_property relation だが念のため最初の要素のみ採用（深さ優先で
        # 1 本のチェーンを辿る、要件 §8.4: A → A' → B で A' を優先）
        next_page_id = priors[0]
        if next_page_id in visited:
            break
        visited.add(next_page_id)
        chain.append(next_page_id)
        current_page_id = next_page_id

    if not chain:
        return []

    collected: list[dict] = []
    for prior_page_id in chain:
        notes = pm_client.find_handover_notes_for_workflow(
            prior_page_id, profile=profile
        )
        collected.extend(notes)
    return collected


def _merge_memories_dedup(
    base: list[dict], extra: list[dict]
) -> list[dict]:
    """memory リストを id ベースで重複排除しながら結合する（順序は base 先）。

    handover_note 注入時に、`list_active_memories` 結果と `find_handover_notes_for_workflow`
    結果に同じ memory が含まれることがある（旧 workflow の active が現 workflow から
    手動でも参照されている場合等）。Notion page id で dedup する。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for src in (base, extra):
        for m in src:
            mid = m.get("id")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            out.append(m)
    return out


def _handle_notion_migrate_schema(args, config=None) -> int:
    """既存 Workflows DB に v0.4.8+ で追加されたプロパティを追加する。

    Issue #21 / v0.4.8: 既存環境の Workflows DB に Operator (rich_text) を
    追加する。Notion API は同名プロパティが存在する場合は no-op になるため
    idempotent。

    解決順序:
    - api token env 名: CLI 明示 > profile config > "HOKUSAI_NOTION_API_TOKEN"
    - workflows_db_id: CLI 明示 > profile config の env 変数 >
      既定 env 変数 "HOKUSAI_NOTION_WORKFLOWS_DB_ID"

    Returns:
        0=成功 / 1=失敗
    """
    from .integrations.notion_dashboard import is_valid_env_var_name
    from .integrations.notion_dashboard.client import NotionAPIClient

    # 追加対象のプロパティ。将来 v0.4.x で追加されるプロパティもここに足せる。
    # Supersedes（self-link relation）は対象 DB id が必要なので migrate 実行時
    # に決定する（Issue #50 / Workgraph Phase 7、要件 §9.3.3）。
    PROPERTIES_TO_ADD: dict = {
        "Operator": {"rich_text": {}},
        "Cancel Reason": {"rich_text": {}},
    }

    dry_run = getattr(args, "dry_run", False)

    # api token env 名 / DB ID の解決順序
    # - api_token_env: CLI 明示 > profile config > "HOKUSAI_NOTION_API_TOKEN"
    # - workflows_db_id: CLI 明示 > profile config の env 変数 > 既定 HOKUSAI_NOTION_WORKFLOWS_DB_ID
    #
    # config 由来の env 名は採用前にシェル変数名として妥当か検証する
    # （notion-setup と同じ方針）。不正値（空白 / 改行 / `;` 等）が混入すると
    # rc 破損 / コマンド注入のリスクがあるため、無効なら警告して既定にフォールバック。
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

    api_token_env = getattr(args, "api_token_env", None)
    if api_token_env is not None and not is_valid_env_var_name(api_token_env):
        # CLI 明示で不正値の場合は中断する（誤って source した時に致命的なため）
        print(
            f"✗ --api-token-env={api_token_env!r} は不正な env 変数名です "
            f"（[A-Za-z_][A-Za-z0-9_]* に合致する必要があります）"
        )
        return 1
    workflows_db_id_env = None
    workflows_db_id = getattr(args, "workflows_db_id", None)

    if config is not None:
        nd_cfg = getattr(config, "notion_dashboard", None)
        if nd_cfg is not None:
            if api_token_env is None:
                api_token_env = _pick_env_name(
                    getattr(nd_cfg, "api_token_env", None),
                    "HOKUSAI_NOTION_API_TOKEN",
                    "notion_dashboard.api_token_env",
                )
            workflows_db_id_env = _pick_env_name(
                getattr(nd_cfg, "workflows_db_id_env", None),
                "HOKUSAI_NOTION_WORKFLOWS_DB_ID",
                "notion_dashboard.workflows_db_id_env",
            )

    if api_token_env is None:
        api_token_env = "HOKUSAI_NOTION_API_TOKEN"
    if not workflows_db_id_env:
        # profile config 未指定または fields 未設定の場合、既定の env 名にフォールバック
        workflows_db_id_env = "HOKUSAI_NOTION_WORKFLOWS_DB_ID"

    if not workflows_db_id:
        workflows_db_id = os.environ.get(workflows_db_id_env, "")
    # 空白のみの値は未設定として扱う（API に `/databases/   ` を投げないため）
    workflows_db_id = (workflows_db_id or "").strip()

    if not workflows_db_id:
        print(
            f"✗ Workflows DB ID が解決できません。--workflows-db-id <id> で明示するか、"
            f"環境変数 {workflows_db_id_env} を設定してください。"
        )
        return 1

    # Supersedes は self-link relation のため対象 DB id を含めて payload を組む
    # （Issue #50 / Workgraph Phase 7）。Notion API は同名プロパティ既存時に
    # no-op になるため idempotent。dry-run 出力と実際の payload が一致する
    # よう、表示前に payload を確定させる（Copilot 指摘）。
    properties_payload: dict = dict(PROPERTIES_TO_ADD)
    properties_payload["Supersedes"] = {
        "relation": {
            "database_id": workflows_db_id,
            "single_property": {},
        }
    }

    print(f"対象 Workflows DB: {workflows_db_id}")
    print("追加予定プロパティ:")
    for name, schema in properties_payload.items():
        print(f"  - {name}: {schema}")

    # --dry-run は API 呼び出しを行わないため、token 未設定でも実行可能にする。
    if dry_run:
        print("--dry-run 指定のため API 呼び出しはスキップしました。")
        return 0

    # token も whitespace-only を未設定扱いにする（notion-setup と同じ方針）
    api_token = os.environ.get(api_token_env, "").strip()
    if not api_token:
        print(f"✗ API token 環境変数 {api_token_env} が設定されていません")
        return 1

    try:
        api = NotionAPIClient(api_token=api_token)
        result = api.update_database(
            workflows_db_id,
            {"properties": properties_payload},
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


def _warn_if_skip_notion_pre_set(config, profile_label: str | None) -> None:
    """`HOKUSAI_SKIP_NOTION=1` が起動時 env で既にセットされているケースに
    対して profile 整合性 warning を出す（Issue #96 / M2.1）。

    findings §1.3: 「HOKUSAI_SKIP_NOTION=1 が profile を跨いで global に
    残っていると、profile 単位の Notion 設定と矛盾して片方の経路だけ動く
    状態になる」問題への対応。新しい env naming convention を導入する
    のは callsite が多いため、最小スコープで「起動時 pre-set 状態で
    Notion 設定済み profile を実行している」mismatch を警告する。

    判定ルール:
    - `HOKUSAI_SKIP_NOTION` が "1" でない → 何もしない（既存挙動）
    - "1" かつ profile の `notion_dashboard.enabled=True` → 強い warning
      （別 profile 用に設定された SKIP フラグが残っている可能性）
    - "1" かつ `notion_dashboard.enabled=False` / 不在 → info notice
      （skip は意図通り、profile が Notion 連携 off）

    `check_notion_connection` が後段で SKIP_NOTION を set する経路は対象外
    （`main()` 内で本 helper を呼ぶ位置がそれより前なので、起動時 env と
    runtime 設定を自然に区別できる）。
    """
    import os
    import sys

    if os.environ.get("HOKUSAI_SKIP_NOTION") != "1":
        return

    notion_cfg = getattr(config, "notion_dashboard", None)
    enabled = notion_cfg is not None and getattr(notion_cfg, "enabled", False)
    profile_text = (
        f"profile '{profile_label}'" if profile_label else "current profile"
    )

    if enabled:
        # mismatch: Notion 設定済み profile に対して global な SKIP が残存。
        # PR #97 Copilot Round 1 指摘: dispatcher (_safe_notion_dispatch /
        # NotionSyncDispatcher.dispatch) は HOKUSAI_SKIP_NOTION を見ておら
        # ず、env が揃っていれば継続する。一方で Phase 2/3 ノードの Notion
        # 書き込み (notion_helpers.py) / CLI 系操作 (task_backend, cancel
        # reason, connection_status) / workflow.py の page_id 解決系は
        # SKIP_NOTION を見て早期 return する。この食い違いが findings §1.3
        # で言う「片方だけ動く」状態。
        message = (
            f"⚠ HOKUSAI_SKIP_NOTION=1 が設定されていますが {profile_text} は "
            "notion_dashboard.enabled=true です。Phase 2/3 ノードの Notion "
            "書き込みや CLI 系の Notion 操作は skip される一方で、dispatcher "
            "経路 (workflow_started / pr_created 等) は notion_dashboard "
            "設定が揃っていれば継続するため、片方だけ動く整合性に注意して "
            "ください。別 profile からの持ち越しの場合は `unset "
            "HOKUSAI_SKIP_NOTION` を推奨。"
        )
        print(message, file=sys.stderr)
    else:
        # 想定どおりの skip（Notion 連携 off な profile）
        print(
            f"ℹ HOKUSAI_SKIP_NOTION=1: {profile_text} の Notion 連携を "
            "skip して実行します。",
            file=sys.stderr,
        )


def _print_notion_db_share_warnings(config) -> None:
    """Notion DB share の健全性を事前チェックして warning を表示する（Issue #82 / M0.2）。

    `hokusai start` 冒頭で呼び、Notion 側で integration "HOKUSAI" に
    Workflows / PR / Memory 等の各 DB が share されているかを retrieve_database
    で確認する。share されていない DB があれば warning として一覧表示し、
    workflow start 自体は継続する（fail-open）。

    以下の場合は何もしない（早期 return）:
    - Notion 機能が無効化されている (`notion_dashboard.enabled=False`)
    - `HOKUSAI_SKIP_NOTION=1` が設定されている（他 Notion ヘルパーと同じく
      opt-out signal として尊重する。Issue #82 Copilot Round 1 指摘）
    """
    import os

    try:
        if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
            return
        notion_cfg = getattr(config, "notion_dashboard", None)
        if notion_cfg is None or not notion_cfg.enabled:
            return

        from .integrations.notion_dashboard.dispatcher import NotionSyncDispatcher
        from .persistence.sqlite_store import SQLiteStore

        store = SQLiteStore(config.database_path)
        dispatcher = NotionSyncDispatcher(store, notion_cfg)

        results = dispatcher.check_db_share_health()
        failed = [(env, msg) for env, (ok, msg) in results.items() if not ok]
        if not failed:
            return

        print(
            f"⚠️  Notion DB share check で {len(failed)} 件の問題が見つかりました:"
        )
        for env, msg in failed:
            print(f"   - {env}: {msg}")
        print(
            "   integration \"HOKUSAI\" を該当 DB に share してください。"
            "workflow は継続しますが、Notion 同期は outbox 経由でリトライされ続けます。"
        )
    except Exception as exc:
        # 健全性チェック自体が失敗しても workflow start を止めない。
        # logger は main() 内 local なので module level get_logger を使う
        from .logging_config import get_logger

        get_logger("cli_main").debug(
            "Notion DB share check 自体が失敗 (type=%s)", type(exc).__name__
        )


def _warn_cleanup_without_cancel_reason(config, workflow_id: str) -> None:
    """`hokusai cleanup` で `--cancel-reason` が未指定のときに、Notion ゴースト
    レコードが残る可能性を stderr で警告する（Issue #98 / M2.2）。

    findings §4.2: reason 未指定だと worktree は削除されるが Notion Workflows
    DB Status は更新されないため、ダッシュボード上「アクティブだが worktree 無い」
    レコードが残る運用穴。

    Notion を意図的に触らないケースでは warning しない（ノイズ防止）:
    - `HOKUSAI_SKIP_NOTION=1`（ユーザの明示 opt-out）
    - `notion_dashboard.enabled=False` / `notion_dashboard` 不在（連携 off）

    本 helper は警告のみ、cleanup の中断はしない（既存挙動と完全後方互換）。
    実 sync を未指定時にも走らせる挙動変更（reason 必須化 or デフォルト値注入）は
    user impact が大きいため future iteration（別 PR）に切り出す。
    """
    import os
    import sys

    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        return
    notion_cfg = getattr(config, "notion_dashboard", None)
    if notion_cfg is None or not getattr(notion_cfg, "enabled", False):
        return

    message = (
        f"⚠ --cancel-reason 未指定のため workflow '{workflow_id}' の Notion "
        "Workflows DB Status は更新されません。Notion ダッシュボード上に "
        "「worktree 削除済みなのに Status は phaseN のまま」のゴーストレコード "
        "が残る可能性があります。ゴースト発生を避けるには次のように reason を "
        f"つけて再実行することを推奨: `hokusai cleanup {workflow_id} "
        "--cancel-reason '<理由>'`"
    )
    print(message, file=sys.stderr)


def _sync_workflow_cancel_reason(
    *, config, workflow_id: str, state: dict, cancel_reason: str
) -> None:
    """`cleanup --cancel-reason` の Notion 同期パス（Issue #56 / 要件 §9.3.2）。

    対象 workflow を Workflows DB 上で Status=Canceled に遷移させ、Cancel Reason
    プロパティに `cancel_reason` テキストを記入する。Notion 接続が無い環境
    （HOKUSAI_SKIP_NOTION=1 / 各種 env 未設定）では warning を出して skip
    （worktree 削除は既に完了済みなので CLI 全体は止めない）。

    既存の `WorkflowsDBClient.apply_event` を `phase_changed` event で呼び出し、
    payload に `status=canceled` + `cancel_reason=<text>` を含める。
    `_build_properties` 側で Cancel Reason rich_text + Status=Canceled select
    が書かれる。
    """
    import os

    from .integrations.notion_dashboard.client import NotionAPIClient
    from .integrations.notion_dashboard.workflows_db import WorkflowsDBClient

    # HOKUSAI_SKIP_NOTION=1 はユーザの「Notion なしで続行」選択。docstring と
    # 実装を整合させるため明示的に skip する（Copilot 指摘）。
    if os.environ.get("HOKUSAI_SKIP_NOTION") == "1":
        print(
            "⚠ HOKUSAI_SKIP_NOTION=1 のため Cancel Reason は記録しません "
            "（worktree 削除は完了）",
            file=sys.stderr,
        )
        return

    notion_cfg = getattr(config, "notion_dashboard", None)
    if notion_cfg is None or not getattr(notion_cfg, "enabled", False):
        print(
            "⚠ Notion 同期が無効のため Cancel Reason は記録しません "
            "（worktree 削除は完了）",
            file=sys.stderr,
        )
        return
    api_token = os.environ.get(notion_cfg.api_token_env, "").strip()
    db_id = os.environ.get(notion_cfg.workflows_db_id_env, "").strip()
    if not api_token or not db_id:
        print(
            "⚠ Workflows DB ID / API token 未設定のため Cancel Reason は "
            "記録しません（worktree 削除は完了）",
            file=sys.stderr,
        )
        return

    try:
        # dispatcher 側と同じ retry / rate_limit 設定でクライアントを初期化
        # （Copilot 指摘: 既定値のままだとリトライ挙動が他経路と不整合）
        api = NotionAPIClient(
            api_token=api_token,
            max_attempts=notion_cfg.retry.max_attempts,
            backoff_seconds=notion_cfg.retry.backoff_seconds,
            requests_per_second=notion_cfg.rate_limit.requests_per_second,
        )
        client = WorkflowsDBClient(api=api, database_id=db_id)
        client.apply_event(
            "phase_changed",
            {
                "workflow_id": workflow_id,
                "task_title": state.get("task_title"),
                "status": "canceled",
                "cancel_reason": cancel_reason,
            },
        )
        print(
            f"✓ Workflows DB を Canceled 化しました（理由: {cancel_reason}）"
        )
    except Exception as e:
        print(
            f"⚠ Cancel Reason 記録失敗: {type(e).__name__}: {e}",
            file=sys.stderr,
        )


def _sync_stale_workflows_notion(
    *, config, store, workflow_ids: list[str], dry_run: bool
) -> None:
    """`cleanup --stale --sync-notion` の Notion 同期パス（Issue #107 / M2.6）。

    stale 削除した worktree に対応する workflow を Workflows DB 上で Canceled 化する。
    `_sync_workflow_cancel_reason` を `cancel_reason="stale cleanup"` で呼ぶ薄いラッパ。

    `store.load_workflow(wf_id) is None`（DB から既に消えている orphan）は state を
    組めないため warning + skip。Notion 接続無し環境は `_sync_workflow_cancel_reason`
    側の既存ロジックで skip される。

    `dry_run=True` 時は load のみ実施して「同期予定」のみ表示し、実 API 呼ばない。
    """
    if dry_run:
        for wf_id in workflow_ids:
            state = store.load_workflow(wf_id)
            if state is None:
                # 警告は通常経路と同じく stderr に統一（Copilot Round 2 指摘）。
                # stdout を一覧取得用にパイプする運用を阻害しないため。
                print(
                    f"(dry-run) ⚠ {wf_id}: workflow.db に state 無し、Notion 同期スキップ",
                    file=sys.stderr,
                )
                continue
            print(f"(dry-run) Notion 同期予定: {wf_id} → Status=Canceled, cancel_reason='stale cleanup'")
        return

    for wf_id in workflow_ids:
        state = store.load_workflow(wf_id)
        if state is None:
            print(
                f"⚠ {wf_id}: workflow.db に state が無いため Notion 同期 skip"
                "（orphan worktree、Notion 側は手動更新が必要な可能性）",
                file=sys.stderr,
            )
            continue
        _sync_workflow_cancel_reason(
            config=config,
            workflow_id=wf_id,
            state=state,
            cancel_reason="stale cleanup",
        )


def _handle_cleanup(args, config):
    """cleanup コマンドのハンドラ"""
    from .integrations.git import GitClient
    from .persistence import SQLiteStore

    store = SQLiteStore(config.database_path)

    # --cancel-reason を strip して空白のみは None 扱い（Copilot 指摘:
    # 空白だけの値で Notion 側 Cancel Reason を見た目空で上書きしないため）
    raw_cancel_reason = getattr(args, "cancel_reason", None)
    cancel_reason = (
        raw_cancel_reason.strip()
        if isinstance(raw_cancel_reason, str)
        else None
    ) or None

    # M2.6 (#107) Copilot Round 2 指摘: --dry-run / --sync-notion は --stale 専用。
    # `hokusai cleanup wf-xxx --dry-run` のように workflow_id 指定モードで一緒に
    # 渡されたとき、現状の workflow_id 経路は両フラグを参照しないため「dry-run
    # なので安全」とユーザが誤解して実削除される事故になり得る。明示的に reject する。
    #
    # Copilot Round 5 指摘: トップレベル --dry-run は元々 cleanup では no-op
    # なので reject 対象外。cleanup サブパーサの --dry-run のみ別 dest
    # ("cleanup_dry_run") で受けて、ここでは cleanup 側のみを判定する。
    dry_run_flag = bool(getattr(args, "cleanup_dry_run", False))
    sync_notion_flag = bool(getattr(args, "sync_notion", False))
    if (dry_run_flag or sync_notion_flag) and not args.stale:
        bad_flags = []
        if dry_run_flag:
            bad_flags.append("--dry-run")
        if sync_notion_flag:
            bad_flags.append("--sync-notion")
        base_msg = (
            f"✗ {' / '.join(bad_flags)} は --stale 専用のフラグです "
            "（workflow_id 指定 / --gc-workflows 単独 / 引数なしと併用不可）。"
        )
        # workflow_id 指定モードのときだけ --cancel-reason の案内を追加。
        # `cleanup --gc-workflows --dry-run` や引数なしのケースでは
        # --cancel-reason が無関係なため案内に含めない（Copilot Round 4 指摘）。
        # 連結時の文の区切りを明示するため改行を挟む（Copilot Round 5 指摘）。
        if args.workflow_id:
            base_msg += (
                "\n  workflow_id 指定モードで Notion 同期したい場合は "
                "--cancel-reason を使ってください。"
            )
        print(base_msg, file=sys.stderr)
        sys.exit(1)

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

        # --cancel-reason 指定時は Workflows DB の Status=Canceled +
        # Cancel Reason を更新（Issue #56 / 要件 §9.3.2 引き継ぎ運用）。
        # Notion 同期未設定 / API 失敗時は warning + skip（worktree 削除は完了済）。
        if cancel_reason:
            _sync_workflow_cancel_reason(
                config=config,
                workflow_id=args.workflow_id,
                state=state,
                cancel_reason=cancel_reason,
            )
        else:
            # M2.2 (#98) findings §4.2: --cancel-reason 未指定の cleanup は
            # Notion Workflows DB Status を更新せず、ダッシュボード上に
            # 「worktree 削除済みなのに Status は phaseN のまま」のゴースト
            # レコードが残る。ユーザが意図的に Notion を触らないケース
            # (SKIP_NOTION=1 / Notion 無効化 profile) を除いて警告を出す。
            _warn_cleanup_without_cancel_reason(config, args.workflow_id)

    elif args.stale:
        # 完了済み workflow の worktree を一括削除
        # PR #101 Copilot Round 2 #6 指摘: 旧実装は worktree_root 不在時に
        # `return` で関数を抜けていたため、`--stale --gc-workflows` 併用時に
        # 後段の GC post-action に到達できないバグがあった。worktree 走査が
        # no-op になるだけにして、writeback cleanup と GC は必ず実行する。
        #
        # M2.6 (Issue #107) findings §4.3: --dry-run で誤操作防止、
        # --sync-notion でゴースト残留防止。両フラグ default off で完全後方互換。
        dry_run = dry_run_flag
        sync_notion = sync_notion_flag
        workflows = store.list_active_workflows()
        worktree_root = config.worktree_root
        cleaned = 0
        # 同一 workflow が複数 repo/worktree を持つケースで wf_id が重複し、
        # --sync-notion 時に Notion 更新が二重発火する（Copilot Round 2 指摘）。
        # set で一意化しつつ、最初に出現した順序を保つため挿入順 dict を使う。
        deleted_workflow_ids: dict[str, None] = {}

        if dry_run:
            print("⚠ --dry-run: 実際の削除は行いません（候補のみ列挙）")

        if not worktree_root.exists():
            print("✓ worktree ディレクトリが存在しません。worktree 削除はスキップ。")
        else:
            active_ids = {w["workflow_id"] for w in workflows}

            for wt_dir in worktree_root.iterdir():
                if not wt_dir.is_dir():
                    continue
                # ディレクトリ名から workflow_id を抽出（{repo_name}_{wf-xxxx}）
                parts = wt_dir.name.rsplit("_wf-", 1)
                if len(parts) == 2:
                    wf_id = f"wf-{parts[1]}"
                    if wf_id not in active_ids:
                        if dry_run:
                            print(f"(dry-run) 削除予定: {wt_dir}")
                            cleaned += 1
                            deleted_workflow_ids[wf_id] = None
                            continue
                        try:
                            import shutil
                            shutil.rmtree(wt_dir)
                            print(f"🧹 stale 削除: {wt_dir}")
                            cleaned += 1
                            deleted_workflow_ids[wf_id] = None
                        except Exception as e:
                            print(f"⚠️ 削除失敗: {wt_dir}: {e}")

            # git worktree prune で削除済みディレクトリの登録を解除
            # （dry-run 時は実削除していないので prune も skip）
            if cleaned > 0 and not dry_run:
                for repo in config.get_all_repositories():
                    try:
                        git = GitClient(str(repo.path))
                        git._run_git("worktree", "prune")
                    except Exception:
                        pass

            if dry_run:
                print(f"(dry-run) {cleaned} 件の stale worktree が削除候補")
            else:
                print(f"✓ {cleaned} 件の stale worktree を削除しました")

        # M2.6: --sync-notion 時、stale 削除した workflow について
        # Notion Workflows DB の Status を Canceled 化する（cancel_reason="stale cleanup"）。
        # store.load_workflow が None を返す orphan（DB から既に消えている）は
        # state を組めないため warning のみ。dry-run 時は実 API 呼ばずプレビュー出力。
        # dict の keys() は挿入順を保つため、観測順で同期される。
        if sync_notion and deleted_workflow_ids:
            _sync_stale_workflows_notion(
                config=config,
                store=store,
                workflow_ids=list(deleted_workflow_ids),
                dry_run=dry_run,
            )

        # Phase E (v0.4.0): writeback errors / idempotency を 30 日経過で削除
        # worktree_root 不在でも実行する（DB 側のクリーンアップは worktree
        # の有無と独立）。dry-run 時は writeback cleanup も skip（実 DB 書き換え
        # を伴うため副作用なしの原則を守る）。
        if not dry_run:
            try:
                _cleanup_writeback_old_errors(config)
            except Exception as e:
                print(f"⚠️ writeback cleanup でエラー: {type(e).__name__}: {e}")

    elif args.gc_workflows:
        # gc-workflows のみの実行（workflow_id / --stale なし）。下の
        # post-action ブロックで処理されるため、ここでは何もしない。
        pass
    else:
        print(
            "✗ workflow_id, --stale, または --gc-workflows を指定してください"
        )
        sys.exit(1)

    # M2.5 (#100): opt-in な workflow 行 GC。workflow_id / --stale / 単独
    # いずれのモードでも `--gc-workflows` が立っていれば実行する。
    # retention_days の不正値は argparse 側 (_positive_retention_days) で
    # 事前 reject されているため、ここでは予期せぬ実行時例外のみ握り潰す
    # （PR #101 Copilot Round 1 #2 指摘で ValueError catch を削除）。
    if args.gc_workflows:
        try:
            _gc_old_workflows(store, args.retention_days)
        except Exception as e:
            print(
                f"⚠️ workflow GC でエラー: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            sys.exit(1)


def _gc_old_workflows(store, retention_days: int) -> None:
    """`hokusai cleanup --gc-workflows` の本体ハンドラ（Issue #100 / M2.5）。

    SQLiteStore.delete_old_completed_workflows を呼び、count summary を
    stdout に出力する。`retention_days < 1` の入力値検証は argparse 側
    (`_positive_retention_days`) で事前に reject される設計なので、ここでは
    例外変換は行わない（PR #101 Copilot Round 2 #3 指摘で docstring 整合）。
    予期せぬ実行時例外（DB lock 等）は呼び出し側 `_handle_cleanup` の
    try/except で stderr 出力 + sys.exit(1) に変換される。
    """
    counts = store.delete_old_completed_workflows(
        retention_days=retention_days
    )
    workflows_deleted = counts.get("workflows", 0)
    if workflows_deleted == 0:
        print(
            f"✓ workflow GC: {retention_days} 日以上前の完了 workflow なし "
            "（削除対象 0 件）"
        )
        return

    print(
        f"🧹 workflow GC: {workflows_deleted} 件の完了 workflow を削除 "
        f"(retention: {retention_days} 日)"
    )
    detail_parts = [
        f"{name}={n}"
        for name, n in counts.items()
        if n > 0 and name != "workflows"
    ]
    if detail_parts:
        print("    cascade: " + ", ".join(detail_parts))


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
