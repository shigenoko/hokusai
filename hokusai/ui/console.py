"""
Console UI Functions

ワークフローの状態表示やユーザーインタラクション用のスタンドアロン関数。
"""

from ..constants import PHASE_NAMES, PHASE_STATUS_ICONS
from ..logging_config import get_logger

logger = get_logger("ui.console")


# =============================================================================
# ワークフロー開始・再開メッセージ
# =============================================================================


def print_from_phase_warning(from_phase: int) -> None:
    """--from-phase 使用時の警告を表示"""
    print()
    print("⚠️  注意: --from-phase 5 以降で開始する場合、以下の情報が欠落します:")
    print("   - 作業計画 (work_plan) - Phase 4 で生成")
    print("   - 設計書 (design_document) - Phase 3 で生成")
    print("   - 変更予定ファイル (expected_changed_files) - Phase 4 で生成")
    print()
    print("   Phase 5 (実装) は手動実装モードにフォールバックします。")
    print("   Phase 8a (PR作成) でブランチが自動プッシュされます。")
    print()


def print_existing_workflow_found(workflow_id: str, current_phase: int) -> None:
    """既存のワークフローが見つかった場合のメッセージ"""
    print(f"⚠️ 既存のワークフローが見つかりました: {workflow_id}")
    print(f"   現在のフェーズ: Phase {current_phase}")
    print(f"   再開するには: workflow continue {workflow_id}")


def print_dry_run_start(
    workflow_id: str,
    task_url: str,
    branch_name: str | None,
    start_phase: int,
) -> None:
    """ドライランモードの開始メッセージ"""
    print(f"🧪 [ドライラン] ワークフロー {workflow_id} を開始します")
    print(f"   タスクURL: {task_url}")
    if branch_name:
        print(f"   ブランチ: {branch_name}")
    print(f"   実行されるフェーズ: Phase {start_phase} → Phase 10")
    if start_phase > 1:
        print(f"   スキップ: Phase 1-{start_phase - 1}")


def print_workflow_start(workflow_id: str) -> None:
    """ワークフロー開始メッセージ"""
    print(f"🚀 ワークフローを開始します: {workflow_id}")


def print_workflow_not_found(workflow_id: str) -> None:
    """ワークフローが見つからない場合のエラーメッセージ"""
    print(f"❌ ワークフローが見つかりません: {workflow_id}")


def print_workflow_resume(workflow_id: str, current_phase: int) -> None:
    """ワークフロー再開メッセージ"""
    print(f"▶️ ワークフローを再開します: {workflow_id}")
    print(f"   現在のフェーズ: Phase {current_phase}")


def print_dry_run_resume(workflow_id: str, current_phase: int) -> None:
    """ドライランモードの再開メッセージ"""
    print(f"🧪 [ドライラン] ワークフロー {workflow_id} を再開します")
    print(f"   現在のフェーズ: Phase {current_phase}")


# =============================================================================
# ワークフローリスト・ステータス
# =============================================================================


def print_no_active_workflows() -> None:
    """アクティブなワークフローがない場合のメッセージ"""
    print("📭 アクティブなワークフローはありません")


def print_active_workflows(workflows: list[dict]) -> None:
    """アクティブなワークフロー一覧を表示"""
    print("📋 アクティブなワークフロー:")
    for wf in workflows:
        print(f"  - {wf['workflow_id']}")
        print(f"    タスク: {wf['task_title'] or wf['task_url']}")
        print(f"    フェーズ: Phase {wf['current_phase']}")
        print(f"    更新: {wf['updated_at']}")
        print()


# =============================================================================
# フェーズ実行メッセージ
# =============================================================================


def print_workflow_completed() -> None:
    """ワークフロー完了メッセージ"""
    print("✅ ワークフローが完了しました")


def print_phase_executing(phase: int | str, phase_name: str, node_name: str) -> None:
    """フェーズ実行中メッセージ"""
    if phase_name:
        print(f"📍 Phase {phase} ({phase_name}): {node_name} を実行中...")
    else:
        print(f"📍 Phase {phase}: {node_name} を実行中...")


# =============================================================================
# エラー・警告メッセージ
# =============================================================================


def print_loop_detected(recent: list) -> None:
    """ループ検出メッセージ"""
    print(f"リトライループを検出: {recent}")


def print_max_events_reached(max_events: int) -> None:
    """最大イベント数到達メッセージ"""
    print(f"⚠️ 最大イベント数 ({max_events}) に達しました。ワークフローを一時停止します。")


def print_workflow_status(state: dict, phase_names: dict[int, str] | None = None) -> None:
    """
    ワークフロー状態を表示

    Args:
        state: ワークフロー状態辞書
        phase_names: フェーズ名マッピング（省略時はデフォルトを使用）
    """
    if phase_names is None:
        phase_names = PHASE_NAMES

    print(f"📊 ワークフロー状態: {state['workflow_id']}")
    print(f"   タスク: {state.get('task_title', state.get('task_url', '不明'))}")
    print(f"   ブランチ: {state.get('branch_name', '未作成')}")
    print(f"   現在のフェーズ: Phase {state.get('current_phase', 1)}")
    print()

    phases = state.get("phases", {})
    for i in range(1, 11):
        phase_state = phases.get(i, {})
        status = phase_state.get("status", "pending")
        icon = PHASE_STATUS_ICONS.get(status, PHASE_STATUS_ICONS["pending"])

        print(f"   {icon} Phase {i}: {PHASE_NAMES.get(i, '不明')}")

    if state.get("waiting_for_human"):
        print()
        print("⏳ Human-in-the-loop: 実装完了を待っています")
        print(f"   再開するには: workflow continue {state['workflow_id']}")


def print_step_summary(phase: int, state: dict, phase_names: dict[int, str] | None = None) -> None:
    """
    ステップ完了時のサマリーを表示

    Args:
        phase: 完了したフェーズ番号
        state: 現在の状態
        phase_names: フェーズ名マッピング（省略時はデフォルトを使用）
    """
    if phase_names is None:
        phase_names = PHASE_NAMES

    print()

    # フェーズ固有の情報を表示
    if phase == 1:
        print(f"  ブランチ: {state.get('branch_name', '未作成')}")
        print(f"  タスクURL: {state.get('task_url', '不明')}")

    elif phase == 4:
        work_plan = state.get("work_plan", "")
        if work_plan:
            # 作業計画の最初の部分を表示
            lines = work_plan.split("\n")[:10]
            print("  作業計画（抜粋）:")
            for line in lines:
                print(f"    {line}")
            if len(work_plan.split("\n")) > 10:
                print("    ...")

        expected_files = state.get("expected_changed_files", [])
        if expected_files:
            print(f"  変更予定ファイル: {len(expected_files)}件")
            for f in expected_files[:5]:
                print(f"    - {f}")
            if len(expected_files) > 5:
                print(f"    ... 他 {len(expected_files) - 5}件")

    elif phase == 5:
        result = state.get("implementation_result", "")
        if result:
            print(f"  実装結果: {len(result)}文字")

    elif phase == 6:
        verification = state.get("verification", {})
        print("  検証結果:")
        for key, value in verification.items():
            icon = "✅" if value == "pass" else "❌" if value == "fail" else "⏳"
            print(f"    {icon} {key}: {value}")

    elif phase == 8:
        pull_requests = state.get("pull_requests", [])
        if pull_requests:
            for pr in pull_requests:
                print(f"  PR: {pr.get('repo_name', 'Backend')} #{pr.get('number')} - {pr.get('url', '未作成')}")
        else:
            print("  PR: 未作成")

    # 監査ログの最新エントリ
    audit_log = state.get("audit_log", [])
    if audit_log:
        latest = audit_log[-1]
        print(f"  最新ログ: {latest.get('action')} - {latest.get('result')}")


def prompt_step_confirmation(
    phase: int,
    state: dict,
    phase_names: dict[int, str] | None = None,
    on_show_status=None,
) -> bool:
    """
    ステップ実行モードでユーザーに確認を求める

    Args:
        phase: 完了したフェーズ番号
        state: 現在の状態
        phase_names: フェーズ名マッピング（省略時はデフォルトを使用）
        on_show_status: 's' 選択時に状態詳細を表示するコールバック。
                        Noneの場合、print_workflow_status を使用。

    Returns:
        True: 続行, False: 中止
    """
    if phase_names is None:
        phase_names = PHASE_NAMES

    phase_name = phase_names.get(phase, f"Phase {phase}")

    print()
    print("=" * 60)
    print(f"⏸️  Phase {phase} ({phase_name}) が完了しました")
    print("=" * 60)

    # 状態のサマリーを表示
    print_step_summary(phase, state, phase_names)

    print()
    print("次のアクション:")
    print("  [Enter] 次のフェーズに進む")
    print("  [s]     状態の詳細を表示")
    print("  [q]     ワークフローを中断して終了")
    print()

    show_status = on_show_status if on_show_status else lambda s: print_workflow_status(s, phase_names)

    while True:
        try:
            choice = input("選択 > ").strip().lower()

            if choice == "" or choice == "c":
                logger.info(f"ユーザー確認: Phase {phase} 完了 → 続行")
                return True
            elif choice == "s":
                show_status(state)
                print()
                print("  [Enter] 次のフェーズに進む")
                print("  [q]     ワークフローを中断して終了")
            elif choice == "q":
                logger.info(f"ユーザー確認: Phase {phase} 完了 → 中断")
                print("⏹️  ワークフローを中断します")
                print(f"   再開するには: workflow continue {state['workflow_id']}")
                return False
            else:
                print("無効な選択です。Enter, s, または q を入力してください。")

        except EOFError:
            # 非インタラクティブ環境では続行
            logger.warning("非インタラクティブ環境を検出 → 自動続行")
            return True


def print_loop_detection_details(state: dict, workflow_id: str) -> None:
    """ループ検出時に詳細情報を表示

    Args:
        state: 現在のワークフロー状態
        workflow_id: ワークフローID
    """
    print()
    print("=" * 60)
    print("⚠️  リトライループを検出しました")
    print("=" * 60)
    print()

    # 検証エラー（Phase 6）の詳細を表示
    verification_errors = state.get("verification_errors", [])
    failed_verifications = [err for err in verification_errors if not err.get("success")]

    if failed_verifications:
        print("📋 検証失敗の詳細 (Phase 6):")
        for err in failed_verifications[-5:]:  # 直近5件のみ表示
            repo = err.get("repository", "unknown")
            cmd = err.get("command", "unknown")
            print(f"   ❌ {repo}: {cmd}")
            if err.get("error_output"):
                # エラー出力の先頭3行のみ表示
                error_lines = err["error_output"].strip().split("\n")[:3]
                for line in error_lines:
                    print(f"      {line[:80]}")
                if len(err["error_output"].strip().split("\n")) > 3:
                    print("      ...")
        print()

    # レビュー問題（Phase 7）の詳細を表示
    review_issues = state.get("final_review_issues", [])
    if review_issues:
        print("📋 レビュー問題の詳細 (Phase 7):")
        for issue in review_issues[-5:]:  # 直近5件のみ表示
            issue_str = str(issue)[:100]  # 100文字まで
            print(f"   ⚠️  {issue_str}")
        if len(review_issues) > 5:
            print(f"   ... 他 {len(review_issues) - 5} 件")
        print()

    # リトライ回数を表示
    phases = state.get("phases", {})
    phase6_retry = phases.get(6, {}).get("retry_count", 0)
    phase7_retry = phases.get(7, {}).get("retry_count", 0)
    print("🔄 リトライ回数:")
    print(f"   Phase 6 (検証): {phase6_retry}回")
    print(f"   Phase 7 (レビュー): {phase7_retry}回")
    print()

    # 推奨アクションを表示
    print("💡 推奨アクション:")
    print("   1. 上記のエラーを手動で確認・修正してください")
    if failed_verifications:
        print("   2. lint/build/test を手動で実行して問題を解消してください")
    if review_issues:
        print("   3. レビュー指摘に従ってコードを修正してください")
    print("   4. 修正後、以下のコマンドで再開:")
    print(f"      workflow continue {workflow_id}")
    print()
    print("=" * 60)
    print()

    logger.info(f"ループ検出詳細を表示: verif_errors={len(failed_verifications)}, review_issues={len(review_issues)}")


# =============================================================================
# CLI モード表示
# =============================================================================


def print_verbose_mode(log_file=None) -> None:
    """詳細ログモード有効メッセージ"""
    print("🔍 詳細ログモードが有効です")
    if log_file:
        print(f"📝 ログファイル: {log_file}")


def print_dry_run_mode() -> None:
    """ドライランモードメッセージ"""
    print("🧪 ドライランモード: 実際の処理は行いません")


def print_step_mode() -> None:
    """ステップ実行モードメッセージ"""
    print("⏸️  ステップ実行モード: 各フェーズ完了後に確認します")


def print_config_file(config_path: str) -> None:
    """設定ファイル読み込みメッセージ"""
    print(f"📁 設定ファイル: {config_path}")


def print_config_error(message: str) -> None:
    """設定ファイルエラーメッセージ"""
    print(f"❌ {message}")


def print_from_phase_start(from_phase: int, branch: str | None = None) -> None:
    """--from-phase 開始メッセージ"""
    print(f"⏭️  Phase {from_phase} から開始します（Phase 1-{from_phase - 1} はスキップ）")
    if branch:
        print(f"🌿 既存ブランチを使用: {branch}")


def print_workflow_id_result(workflow_id: str) -> None:
    """ワークフローID結果表示"""
    print(f"\n📝 ワークフローID: {workflow_id}")


def print_interrupted() -> None:
    """中断メッセージ"""
    print("\n⚠️ 中断されました")


def print_error(message: str) -> None:
    """汎用エラーメッセージ"""
    print(f"❌ エラー: {message}")


# =============================================================================
# 環境チェック
# =============================================================================


def print_environment_warnings(warnings: list[str]) -> None:
    """環境設定の警告を表示

    Args:
        warnings: 警告メッセージのリスト
    """
    if not warnings:
        return

    print()
    print("⚠️  環境設定の警告:")
    for w in warnings:
        print(f"   - {w}")
    print()


# =============================================================================
# Notion 接続チェック
# =============================================================================


def print_notion_checking() -> None:
    """Notion接続確認中メッセージ"""
    print("🔗 Notion接続を確認中...")


def print_notion_dry_run() -> None:
    """Notionドライランスキップメッセージ"""
    print("🔗 [ドライラン] Notion接続確認をスキップ")


def print_notion_ok() -> None:
    """Notion接続OKメッセージ"""
    print("✅ Notion接続: OK")


def print_notion_connection_error(error_message: str) -> None:
    """Notion接続エラーメッセージ"""
    print("\n⚠️  Notion接続: 失敗")
    print(f"   {error_message}")
    print("\n💡 解決方法:")
    print("   1. Claude CodeでNotionツールを使用して認証:")
    print("      例: 「Notionのページを検索して」と入力")
    print("   2. ブラウザでNotion認証を完了")
    print("   3. 認証後、再度hokusaiを実行")


def print_notion_environment_error(error_message: str) -> None:
    """Notion環境エラーメッセージ"""
    print("\n⚠️  Notion接続: 環境エラー")
    print(f"   {error_message}")
    print("\n💡 Claude Codeがインストールされていることを確認してください。")
    print("   npm install -g @anthropic-ai/claude-code")


def print_notion_unexpected_error(error_type: str, error_message: str) -> None:
    """Notion予期しないエラーメッセージ"""
    print("\n⚠️  Notion接続: 予期しないエラー")
    print(f"   {error_type}: {error_message}")
    print("\n💡 詳細ログを確認するには -v オプションを使用してください。")


def print_notion_continue_prompt() -> None:
    """Notion接続なし続行確認プロンプト"""
    print("\n❓ Notion接続なしでワークフローを続行しますか？")
    print("   （調査結果・開発計画のNotion自動保存はスキップされます）")


def print_notion_continue_yes() -> None:
    """Notion接続なし続行メッセージ"""
    print("   ⏩ Notion接続なしで続行します")


def print_notion_continue_no() -> None:
    """Notion接続なし中止メッセージ"""
    print("   ⛔ 中止します")
