"""
Phase 5: 実装 (自動)

Claude Codeが作業計画に従って自動で実装を行うフェーズ。
"""

from pathlib import Path

from ..config import get_config
from ..integrations.claude_code import ClaudeCodeClient
from ..integrations.factory import get_task_client
from ..integrations.git import GitClient
from ..integrations.git_hosting.github import GitHubHostingClient
from ..logging_config import get_logger
from ..state import (
    PhaseStatus,
    RepositoryPhaseStatus,
    VerificationErrorEntry,
    WorkflowState,
    add_audit_log,
    get_repository_state,
    should_skip_phase,
    update_phase_status,
)
from ..utils.notion_helpers import update_notion_checkboxes
from ..utils.repo_resolver import resolve_runtime_repositories
from ..utils.shell import ShellRunner

logger = get_logger("phase5")


def _commit_and_push(
    repo_path: Path,
    branch_name: str,
    commit_message: str,
) -> bool:
    """Claude Code 実装後の変更をコミット＆プッシュする。

    Claude Code が自発的にコミットしていない場合のみ実行する。

    Returns:
        コミット＆プッシュが成功した場合 True
    """
    git = GitClient(str(repo_path))

    if not git.has_uncommitted_changes():
        logger.info("未コミット変更なし（Claude Code が既にコミット済み）")
        return True

    repo_name = repo_path.name
    try:
        shell = ShellRunner(cwd=repo_path)
        shell.run_git("add", "-A", check=True)
        shell.run_git("commit", "-m", commit_message, check=True)
        logger.info(f"{repo_name}: コミット完了")
        print(f"   📦 {repo_name}: 変更をコミットしました")
    except Exception as e:
        logger.error(f"{repo_name}: コミット失敗: {e}")
        print(f"   ⚠️ {repo_name}: コミット失敗 - {e}")
        return False

    try:
        git_hosting = GitHubHostingClient(working_dir=repo_path)
        if git_hosting.push_branch(branch_name):
            logger.info(f"{repo_name}: プッシュ完了")
            print(f"   📤 {repo_name}: リモートにプッシュしました")
            return True
        else:
            logger.warning(f"{repo_name}: プッシュ失敗")
            print(f"   ⚠️ {repo_name}: プッシュ失敗")
            return False
    except Exception as e:
        logger.error(f"{repo_name}: プッシュ失敗: {e}")
        print(f"   ⚠️ {repo_name}: プッシュ失敗 - {e}")
        return False


def phase5_implement_node(state: WorkflowState) -> WorkflowState:
    """Phase 5: 実装 (自動)

    _prepare_implementation, _execute_implementation, _finalize_implementation
    の3つのサブ関数をオーケストレーションする。
    """

    # 1. 準備: スキップ判定、リトライ検出、作業計画取得
    state, prep_result = _prepare_implementation(state)
    if prep_result["early_return"]:
        return state

    is_retry = prep_result["is_retry"]
    phase7_retry_count = prep_result["phase7_retry_count"]
    phase6_retry_count = prep_result.get("phase6_retry_count", 0)

    # 2. 実行: プロンプト構築、Claude Code実行、結果記録
    state, result = _execute_implementation(state, is_retry, phase7_retry_count, phase6_retry_count)

    # 3. 後処理: 完了ステップ抽出、Notionチェックボックス更新
    if result is not None:
        _finalize_implementation(state, result)

    return state


def _prepare_implementation(state: WorkflowState) -> tuple[WorkflowState, dict]:
    """Phase 5の準備処理: スキップ判定、リトライモード検出、作業計画の取得。

    Returns:
        (state, prep_result): prep_resultはearly_return, is_retry, phase7_retry_countを含む
    """
    prep_result = {"early_return": False, "is_retry": False, "phase7_retry_count": 0}

    # スキップチェック
    if should_skip_phase(state, 5):
        logger.info("Phase 5 スキップ: 実装済み")
        print("⏭️  Phase 5 スキップ: 実装済み")
        prep_result["early_return"] = True
        return state, prep_result

    # リトライシナリオのチェック
    # - Phase 7で問題が検出された場合（retry_count > 0 かつ final_review_issues がある）
    # - Phase 6で検証が失敗した場合（verification_errors に失敗エントリがある）
    phases = state.get("phases", {})
    phase7_retry_count = phases.get(7, {}).get("retry_count", 0)
    phase6_retry_count = phases.get(6, {}).get("retry_count", 0)

    has_review_issues = phase7_retry_count > 0 and bool(state.get("final_review_issues"))
    has_verification_errors = phase6_retry_count > 0 and any(
        not err.get("success") for err in state.get("verification_errors", [])
    )

    is_retry = has_review_issues or has_verification_errors
    prep_result["is_retry"] = is_retry
    prep_result["phase7_retry_count"] = phase7_retry_count
    prep_result["phase6_retry_count"] = phase6_retry_count

    # 後続フェーズ完了済みのスキップ判定（リトライ時は実行する）
    phase6_status = phases.get(6, {}).get("status", "")
    phase7_status = phases.get(7, {}).get("status", "")
    if (phase6_status == PhaseStatus.COMPLETED.value or phase7_status == PhaseStatus.COMPLETED.value) and not is_retry:
        logger.info("Phase 5 スキップ: 後続フェーズが完了済み")
        print("⏭️  Phase 5 スキップ: 後続フェーズ（検証・レビュー）が完了済み")
        state = update_phase_status(state, 5, PhaseStatus.COMPLETED)
        state["current_phase"] = 6
        if state.get("work_plan"):
            work_plan_steps = _extract_steps_from_work_plan(state["work_plan"])
            if work_plan_steps:
                logger.info(f"work_planからステップを抽出: {len(work_plan_steps)}件")
                update_notion_checkboxes(state, work_plan_steps)
        prep_result["early_return"] = True
        return state, prep_result

    # リトライシナリオの場合、Phase 6/7のステータスをリセット
    if is_retry:
        review_issues_count = len(state.get("final_review_issues", []))
        verification_errors_count = len([
            err for err in state.get("verification_errors", [])
            if not err.get("success")
        ])

        if has_verification_errors and has_review_issues:
            logger.info(f"Phase 5 リトライモード: 検証エラー{verification_errors_count}件 + レビュー問題{review_issues_count}件を修正 (P6 retry={phase6_retry_count}, P7 retry={phase7_retry_count})")
            print("🔄 Phase 5 リトライモード: 検証エラーとレビュー問題を修正します")
        elif has_verification_errors:
            logger.info(f"Phase 5 リトライモード: 検証エラー{verification_errors_count}件を修正 (P6 retry={phase6_retry_count})")
            print("🔄 Phase 5 リトライモード: 検証エラーを修正します")
        else:
            logger.info(f"Phase 5 リトライモード: レビュー問題{review_issues_count}件を修正 (P7 retry={phase7_retry_count})")
            print("🔄 Phase 5 リトライモード: レビューで検出された問題を修正します")

        state = update_phase_status(state, 6, PhaseStatus.PENDING)
        state = update_phase_status(state, 7, PhaseStatus.PENDING)

    logger.info("Phase 5 開始: 自動実装")
    state = update_phase_status(state, 5, PhaseStatus.IN_PROGRESS)

    # 作業計画の解決（リトライモードの場合はスキップ可能）
    # state に既存の work_plan がある場合も検証し、不正なら他ソースにフォールバック
    if not is_retry:
        content, source = _resolve_work_plan(state)
        if content:
            state["work_plan"] = content
            state = add_audit_log(state, 5, "work_plan_resolved", "info",
                details={"source": source, "content_length": len(content)})
        logger.info(f"work_plan 取得元: {source}")

        # 全ソースの検証が失敗した場合は waiting_for_human
        if source.endswith("(invalid)"):
            logger.warning("全ソースの work_plan が検証に失敗しました")
            state["waiting_for_human"] = True
            state["human_input_request"] = (
                "作業計画（work_plan）の妥当性検証に全ソースで失敗しました。\n\n"
                "原因:\n"
                "- チェックリスト形式のステップが不足している\n"
                "- LLM プリアンブルが混入している可能性\n\n"
                "対処方法:\n"
                "1. Notion の開発計画を手動で確認・修正してください\n"
                "2. 修正後: workflow continue <workflow_id>\n"
            )
            state = add_audit_log(state, 5, "work_plan_all_invalid", "warning",
                details={"source": source})
            prep_result["early_return"] = True
            return state, prep_result

    # 取得できなかった場合（リトライ時はfinal_review_issuesで続行可能）
    if not state.get("work_plan") and not is_retry:
        state = _handle_missing_work_plan(state)
        prep_result["early_return"] = True
        return state, prep_result

    return state, prep_result


def _resolve_work_plan(state: WorkflowState) -> tuple[str | None, str]:
    """work_plan を優先順に解決する。検証失敗時は次のソースにフォールバック。

    (content, source) を返す。
    source が "(invalid)" で終わる場合、全ソースの検証に失敗したことを示す。

    優先順:
    1. state["work_plan"] (Phase 4 から直接引き継ぎ)
    2. phase_subpages[4] (Phase 4 の Notion 子ページ)
    3. 親タスクページの「開発計画」セクション
    """
    from ..nodes.phase4_plan import _validate_work_plan

    first_found: tuple[str, str] | None = None  # 検証失敗でも最初に見つかったコンテンツ

    # 1. state から取得
    if state.get("work_plan"):
        is_valid, warnings = _validate_work_plan(state["work_plan"])
        if is_valid:
            return state["work_plan"], "state"
        first_found = (state["work_plan"], "state")
        logger.warning(f"state の work_plan が検証に失敗: {warnings}、他のソースを試行")
    else:
        logger.warning("作業計画がありません - Notionから取得を試みます")
        print("⚠️  作業計画がstateにありません - Notionから取得を試みます...")

    # 2. phase_subpages[4] から取得
    subpage_url = state.get("phase_subpages", {}).get(4)
    if subpage_url:
        try:
            from ..integrations.notion_mcp import NotionMCPClient
            notion = NotionMCPClient()
            content = notion.get_page_content(subpage_url)
            if content and content.strip():
                is_valid, warnings = _validate_work_plan(content)
                if is_valid:
                    logger.info(f"Phase 4 子ページから有効な work_plan を取得: {len(content)}文字")
                    print("✅ Phase 4 子ページから「開発計画」を取得しました")
                    return content, "phase4_subpage"
                if not first_found:
                    first_found = (content, "phase4_subpage")
                logger.warning(f"Phase 4 子ページの work_plan が検証に失敗: {warnings}")
        except Exception as e:
            logger.warning(f"Phase 4 子ページ取得失敗: {e}")

    # 3. 親タスクページの「開発計画」セクション
    try:
        task_client = get_task_client()
        content = task_client.get_section_content(state["task_url"], "開発計画")
        if content:
            is_valid, warnings = _validate_work_plan(content)
            if is_valid:
                logger.info(f"親タスクページから有効な work_plan を取得: {len(content)}文字")
                print("✅ Notionタスクから「開発計画」を取得しました")
                return content, "task_page_section"
            if not first_found:
                first_found = (content, "task_page_section")
            logger.warning(f"親タスクページの work_plan が検証に失敗: {warnings}")
    except Exception as e:
        logger.warning(f"親タスクページ取得失敗: {e}")
        print(f"⚠️  Notionから取得エラー: {e}")

    # 全ソース検証失敗: 最初に見つかったものを invalid フラグ付きで返す
    if first_found:
        return first_found[0], f"{first_found[1]}(invalid)"
    return None, "not_found"


def _handle_missing_work_plan(state: WorkflowState) -> WorkflowState:
    """作業計画が取得できなかった場合のフォールバック処理。

    Human-in-the-loopモードに切り替える。
    """
    logger.warning("作業計画が取得できませんでした")
    print("⚠️  作業計画が取得できませんでした")
    print("   手動で実装を進め、完了後: workflow continue")

    state["waiting_for_human"] = True
    state["human_input_request"] = """
作業計画（work_plan）が取得できませんでした。

原因:
- --from-phase オプションでPhase 4以前をスキップした
- Notionタスクに「開発計画」セクションがない

対処方法:
1. 手動でタスクの実装を進めてください
2. 実装が完了したら: workflow continue <workflow_id>

または、Phase 1から再実行してください:
  workflow start <task_url>
"""
    state = add_audit_log(
        state, 5, "missing_work_plan", "warning",
        {"reason": "work_plan not found in state or notion"}
    )
    return state


def _execute_implementation(
    state: WorkflowState, is_retry: bool, phase7_retry_count: int, phase6_retry_count: int = 0
) -> tuple[WorkflowState, str | None]:
    """プロンプトを構築しClaude Codeで実装を実行する。タイムアウト時はNoneを返す。

    マルチリポジトリ対応: 対象リポジトリごとに順次実装を実行し、結果を結合して保存。
    リポジトリ途中失敗時は即時中断してPhase 5を失敗扱いにする。
    """
    print("""
╔══════════════════════════════════════════════════════════════════╗
║  🤖 Phase 5: 実装 - Claude Codeによる自動実装                     ║
╚══════════════════════════════════════════════════════════════════╝
""")
    try:
        config = get_config()

        # 対象リポジトリを取得（worktree path を含むランタイム情報）
        target_repositories = resolve_runtime_repositories(state, config)
        if not target_repositories:
            raise ValueError("有効な対象リポジトリがありません")

        all_results = []
        total_repos = len(target_repositories)

        print(f"📝 Claude Codeに実装を指示しています... ({total_repos}リポジトリ)")
        logger.info(f"Claude Code実行開始 (タイムアウト: {config.skill_timeout}秒, リポジトリ数: {total_repos})")

        # リポジトリごとにループ処理
        skipped_repos = 0
        for idx, repo in enumerate(target_repositories, 1):
            # リトライ時：修正対象がないリポジトリはスキップ
            should_skip = False
            skip_reason = ""
            if is_retry:
                # 1. Phase 6, 7 両方完了 → スキップ
                repo_state = get_repository_state(state, repo.name)
                if repo_state:
                    phase6_status = repo_state.get("phase_status", {}).get(6)
                    phase7_status = repo_state.get("phase_status", {}).get(7)
                    if (phase6_status == RepositoryPhaseStatus.COMPLETED.value and
                        phase7_status == RepositoryPhaseStatus.COMPLETED.value):
                        should_skip = True
                        skip_reason = "検証済み"

                # 2. このリポジトリに関連する修正対象がない → スキップ
                if not should_skip and not _has_issues_for_repo(state, repo):
                    should_skip = True
                    skip_reason = "修正対象なし"

            if should_skip:
                print(f"⏭️  [{idx}/{total_repos}] {repo.name} はスキップ（{skip_reason}）")
                logger.info(f"リポジトリ {repo.name} は{skip_reason}のためスキップ")
                all_results.append(f"# Repository: {repo.name}\n(スキップ: {skip_reason})")
                skipped_repos += 1
                continue

            print(f"🚀 [{idx}/{total_repos}] {repo.name} の実装を開始します...")
            logger.debug(f"リポジトリ {repo.name} ({repo.path}) の実装開始")

            # working_dir を各リポジトリに設定
            claude = ClaudeCodeClient(working_dir=repo.path)
            logger.debug(f"Claude Code クライアント初期化完了: {repo.path}")

            # プロンプトにリポジトリ情報を注入
            implementation_prompt = _build_implementation_prompt(
                state, repo=repo, is_retry=is_retry
            )
            logger.debug(f"実装プロンプト生成: {len(implementation_prompt)}文字 (is_retry={is_retry})")
            logger.debug(f"プロンプト内容:\n{implementation_prompt[:500]}...")

            result = claude.execute_prompt(
                prompt=implementation_prompt,
                timeout=config.skill_timeout,
                allow_file_operations=True,
            )

            all_results.append(f"# Repository: {repo.name}\n{result}")
            print(f"   ✅ {repo.name}: 実装完了")
            logger.info(f"{repo.name} 実装完了: {len(result)}文字")

            # Claude Code が未コミットの場合、明示的にコミット＆プッシュ
            branch_name = state.get("branch_name", "")
            if branch_name:
                task_title = state.get("task_title", "実装")
                if is_retry:
                    msg = f"fix: address review/verification issues ({repo.name})"
                else:
                    msg = f"feat: {task_title}"
                _commit_and_push(Path(repo.path), branch_name, msg)

        # 結果を結合
        final_result = "\n\n".join(all_results)
        state["implementation_result"] = final_result
        state = update_phase_status(state, 5, PhaseStatus.COMPLETED)
        executed_repos = total_repos - skipped_repos
        state = add_audit_log(state, 5, "implementation_completed", "success", {
            "auto_implemented": True, "output_length": len(final_result),
            "is_retry": is_retry,
            "phase6_retry_count": phase6_retry_count,
            "phase7_retry_count": phase7_retry_count,
            "repository_count": total_repos,
            "executed_repos": executed_repos,
            "skipped_repos": skipped_repos,
        })
        if skipped_repos > 0:
            print(f"✅ Phase 5 完了: 実装が完了しました ({executed_repos}リポジトリ実行, {skipped_repos}リポジトリスキップ)")
        else:
            print(f"✅ Phase 5 完了: 実装が完了しました ({total_repos}リポジトリ)")
        logger.info(f"Phase 5 完了: 実装結果 {len(final_result)}文字 (実行: {executed_repos}, スキップ: {skipped_repos})")
        logger.debug(f"実装結果:\n{final_result[:1000]}...")
        return state, final_result

    except TimeoutError as e:
        logger.warning(f"Phase 5 タイムアウト: {e}")
        state = update_phase_status(state, 5, PhaseStatus.IN_PROGRESS)
        state["waiting_for_human"] = True
        state["human_input_request"] = (
            f"実装がタイムアウトしました。手動で続行してください。\n\n"
            f"Claude Codeで作業計画に従って実装を進め、完了したら以下のコマンドを実行してください:\n"
            f"  workflow continue\n\nエラー: {e}"
        )
        state = add_audit_log(state, 5, "implementation_timeout", "warning", error=str(e))
        print("⚠️ Phase 5 タイムアウト: 手動で続行してください")
        print("   完了後: workflow continue")
        return state, None

    except Exception as e:
        state = update_phase_status(state, 5, PhaseStatus.FAILED, str(e))
        state = add_audit_log(state, 5, "phase_failed", "error", error=str(e))
        print(f"❌ Phase 5 失敗: {e}")
        logger.error(f"Phase 5 失敗: {e}")
        raise


def _finalize_implementation(state: WorkflowState, result: str) -> None:
    """Phase 5の後処理

    実装結果から完了ステップを抽出し、Notionのチェックボックスを更新する。
    """
    # 完了したステップを抽出してNotionのチェックボックスを更新
    completed_steps = _extract_completed_steps(result)

    # Claude Code応答からステップが抽出できない場合、work_planから全ステップを取得
    if not completed_steps and state.get("work_plan"):
        logger.info("応答からステップ抽出できず、work_planからフォールバック")
        completed_steps = _extract_steps_from_work_plan(state["work_plan"])

    if completed_steps:
        logger.info(f"チェックボックス更新対象: {completed_steps}")
        update_notion_checkboxes(state, completed_steps)
    else:
        logger.warning("更新対象のステップが見つかりませんでした")


def _build_implementation_prompt(
    state: WorkflowState,
    repo=None,
    is_retry: bool = False,
) -> str:
    """実装用のプロンプトを構築

    Args:
        state: ワークフロー状態
        repo: 対象リポジトリ（RepositoryConfig）。Noneの場合はプロジェクトルートを使用
        is_retry: リトライモード（Phase 7で検出された問題の修正）
    """
    from ..prompts import get_prompt

    # リトライモードの場合、問題修正に特化したプロンプトを生成
    # Phase 7の問題（final_review_issues）またはPhase 6の問題（verification_errors）がある場合
    has_verification_failures = any(
        not err.get("success") for err in state.get("verification_errors", [])
    )
    if is_retry and (state.get("final_review_issues") or has_verification_failures):
        return _build_retry_prompt(state, repo=repo)

    # リポジトリ情報を組み立て
    repo_section = ""
    if repo:
        parts = [
            "## 対象リポジトリ",
            f"あなたは **{repo.name}** ({repo.path}) を操作しています。",
        ]
        if repo.description:
            parts.append(f"このリポジトリの構成: {repo.description}")
        parts.append("このリポジトリに関連する変更のみを実装してください。")
        repo_section = "\n".join(parts) + "\n"

    # コーディングルール情報を組み立て
    coding_rules_section = ""
    if repo and repo.coding_rules:
        coding_rules_section = f"## コーディングルール\n{repo.coding_rules}\n"

    # 作業計画を組み立て
    work_plan_section = ""
    if state.get("work_plan"):
        work_plan_section = f"## 作業計画\n{state['work_plan']}\n"

    # 変更予定ファイルを組み立て
    expected_files_section = ""
    if state.get("expected_changed_files"):
        lines = ["## 変更予定ファイル"]
        lines.extend(f"- {f}" for f in state["expected_changed_files"])
        expected_files_section = "\n".join(lines) + "\n"

    return get_prompt(
        "phase5.implementation",
        repo_section=repo_section,
        coding_rules_section=coding_rules_section,
        task_url=state["task_url"],
        work_plan_section=work_plan_section,
        expected_files_section=expected_files_section,
    )


def _has_issues_for_repo(state: WorkflowState, repo) -> bool:
    """リトライ時にこのリポジトリに修正対象があるか判定する。

    _build_retry_prompt のフィルタリングロジックと同じ基準で判定し、
    修正対象がないリポジトリでは Claude Code を実行しない。
    """
    repo_path = str(repo.path)
    repo_name = repo.name

    # レビュー issue をフィルタ
    for issue in state.get("final_review_issues", []):
        issue_str = str(issue)
        if repo_path in issue_str or repo_name in issue_str:
            return True
        # パスを含まない一般的な指摘は全リポジトリに適用
        if not any(c in issue_str for c in ['/', '\\']):
            return True

    # 検証エラーをフィルタ
    for err in state.get("verification_errors", []):
        if err.get("repository") == repo_name and not err.get("success"):
            return True

    return False


def _build_retry_prompt(state: WorkflowState, repo=None) -> str:
    """リトライ用のプロンプトを構築（Phase 6/7で検出された問題を修正）

    Args:
        state: ワークフロー状態（final_review_issues, verification_errorsを含む）
        repo: 対象リポジトリ（RepositoryConfig）。Noneの場合はプロジェクトルートを使用

    改善点（2026-02-23）:
    - 再発エラーを検出し明示する
    - 直近N件の失敗のみを提示
    - 同一リポジトリの失敗コマンドを優先表示
    """
    from ..prompts import get_prompt

    MAX_RECENT_ERRORS = 5  # 直近のエラー件数制限

    all_issues = state.get("final_review_issues", [])
    all_verification_errors: list[VerificationErrorEntry] = state.get("verification_errors", [])
    retry_count = state.get("phases", {}).get(7, {}).get("retry_count", 0)
    phase6_retry_count = state.get("phases", {}).get(6, {}).get("retry_count", 0)

    # リポジトリが指定されている場合、そのリポジトリに関連する情報をフィルタリング
    if repo:
        repo_path = str(repo.path)
        repo_name = repo.name

        # レビューissueをフィルタ
        filtered_issues = []
        for issue in all_issues:
            issue_str = str(issue)
            if repo_path in issue_str or repo_name in issue_str:
                filtered_issues.append(issue)
            elif not any(c in issue_str for c in ['/', '\\']):
                filtered_issues.append(issue)

        issues = filtered_issues
        logger.debug(f"リポジトリ {repo_name} 向けにissueをフィルタ: {len(all_issues)} -> {len(issues)}件")

        filtered_verification_errors = [
            err for err in all_verification_errors
            if err.get("repository") == repo_name and not err.get("success")
        ]
        verification_errors = filtered_verification_errors
        logger.debug(f"リポジトリ {repo_name} 向けに検証エラーをフィルタ: {len(all_verification_errors)} -> {len(verification_errors)}件")
    else:
        issues = all_issues
        verification_errors = [err for err in all_verification_errors if not err.get("success")]

    # 直近N件のエラーのみを取得（最新のものを優先）
    verification_errors = verification_errors[-MAX_RECENT_ERRORS:]

    # 再発エラーを検出（同じcommandが複数回失敗している）
    recurring_commands = _detect_recurring_errors(all_verification_errors, repo.name if repo else None)

    # --- 動的セクションを組み立て ---

    # リポジトリ情報セクション
    repo_section = ""
    if repo:
        parts = [
            "## 対象リポジトリ",
            f"あなたは **{repo.name}** ({repo.path}) を操作しています。",
        ]
        if repo.description:
            parts.append(f"このリポジトリの構成: {repo.description}")
        parts.extend([
            "",
            "**重要**: このリポジトリ内のファイルのみを修正してください。",
            "他のリポジトリに関する指摘は無視してください。",
        ])
        repo_section = "\n".join(parts) + "\n"

    # コーディングルール情報を組み立て
    coding_rules_section = ""
    if repo and repo.coding_rules:
        coding_rules_section = f"## コーディングルール\n{repo.coding_rules}\n"

    # 修正すべき問題がない場合は早期リターン
    if not issues and not verification_errors:
        early_parts = [
            "# 検出された問題の修正",
            "",
        ]
        if repo_section:
            early_parts.append(repo_section)
        early_parts.extend([
            "前回の検証/レビューで問題が検出されましたが、",
            f"このリポジトリ ({repo.name if repo else 'unknown'}) に関連する問題は見つかりませんでした。",
            "",
            "何も修正する必要はありません。",
        ])
        return "\n".join(early_parts)

    # 再発エラー警告セクション
    recurring_warning_section = ""
    if recurring_commands:
        parts = [
            "## ⚠️ 再発エラー警告",
            "",
            "以下のコマンドは**複数回連続で失敗**しています。",
            "前回の修正が効いていない可能性があります。",
            "**根本原因を特定し、異なるアプローチで修正してください。**",
            "",
        ]
        for cmd, count in recurring_commands.items():
            parts.append(f"- `{cmd}`: {count}回連続失敗")
        recurring_warning_section = "\n".join(parts) + "\n"

    # 検証エラーセクション
    verification_errors_section = ""
    if verification_errors:
        parts = [
            f"## 前回の検証で失敗したコマンド（Phase 6、リトライ {phase6_retry_count}回目）",
            "",
        ]
        for err in verification_errors:
            cmd = err.get("command", "unknown")
            is_recurring = cmd in recurring_commands
            recurring_marker = " 🔄 **再発**" if is_recurring else ""
            parts.append(f"### `{cmd}` コマンドが失敗{recurring_marker}")
            if is_recurring:
                parts.append("")
                parts.append(f"> ⚠️ このエラーは{recurring_commands[cmd]}回連続で発生しています。前回と異なる修正方法を検討してください。")
            if err.get("error_output"):
                parts.append("")
                parts.append("```")
                parts.append(err["error_output"])
                parts.append("```")
            parts.append("")
        verification_errors_section = "\n".join(parts)

    # レビューissueセクション
    review_issues_section = ""
    if issues:
        parts = [
            f"## コードレビューで検出された問題（Phase 7、リトライ {retry_count}回目）",
            "",
        ]
        for issue in issues:
            parts.append(f"- {issue}")
        review_issues_section = "\n".join(parts) + "\n"

    # 再発エラーガイダンスセクション
    recurring_guidance_section = ""
    if recurring_commands:
        recurring_guidance_section = "\n".join([
            "### ⚠️ 再発エラーへの対処",
            "",
            "再発エラーが検出されました。以下を確認してください:",
            "1. **エラーメッセージを注意深く読む**: 前回と同じ箇所か、異なる箇所か",
            "2. **修正ファイルの保存漏れ**: 変更が正しく保存されているか",
            "3. **修正方法の見直し**: 前回の修正が根本解決になっていない可能性",
            "4. **依存関係の問題**: 他のファイルの変更が必要な可能性",
        ]) + "\n"

    return get_prompt(
        "phase5.retry_fix",
        repo_section=repo_section,
        coding_rules_section=coding_rules_section,
        recurring_warning_section=recurring_warning_section,
        verification_errors_section=verification_errors_section,
        review_issues_section=review_issues_section,
        recurring_guidance_section=recurring_guidance_section,
    )


def _detect_recurring_errors(
    verification_errors: list[VerificationErrorEntry],
    repo_name: str | None = None
) -> dict[str, int]:
    """再発エラー（同じコマンドが複数回失敗）を検出

    Args:
        verification_errors: 全検証エラーリスト
        repo_name: リポジトリ名でフィルタする場合に指定

    Returns:
        {コマンド名: 失敗回数} の辞書（2回以上失敗したもののみ）
    """
    from collections import Counter

    # 失敗したエラーのみを対象
    failed_errors = [
        err for err in verification_errors
        if not err.get("success")
    ]

    # リポジトリでフィルタ
    if repo_name:
        failed_errors = [
            err for err in failed_errors
            if err.get("repository") == repo_name
        ]

    # コマンド別にカウント
    command_counts = Counter(err.get("command", "unknown") for err in failed_errors)

    # 2回以上失敗したコマンドのみを返す
    return {cmd: count for cmd, count in command_counts.items() if count >= 2}


def _extract_steps_from_work_plan(work_plan: str) -> list[str]:
    """
    作業計画（work_plan）からステップ番号を抽出

    work_planの形式例:
    - [ ] **1.1** API リポジトリにスキーマ変更のPR作成
    - [x] **2.0** 共通処理の抽出（リファクタリング）
    """
    import re

    steps = []

    for line in work_plan.split("\n"):
        line_stripped = line.strip()

        # チェックボックス形式を検出: "- [ ] **1.1**" or "- [x] **2.0**"
        # 未チェック項目のみを対象とする（完了として更新するため）
        checkbox_match = re.search(
            r"-\s*\[\s*\]\s*\*{0,2}(\d+\.\d+)\*{0,2}",
            line_stripped
        )
        if checkbox_match:
            steps.append(checkbox_match.group(1))
            continue

        # 番号付きリスト形式: "1.1 説明" or "**1.1** 説明"
        numbered_match = re.search(
            r"^\*{0,2}(\d+\.\d+)\*{0,2}\s+",
            line_stripped
        )
        if numbered_match:
            steps.append(numbered_match.group(1))

    logger.debug(f"work_planから抽出したステップ: {steps}")
    return list(set(steps))  # 重複を除去


def _extract_completed_steps(implementation_result: str) -> list[str]:
    """
    実装結果から完了したステップを抽出

    より柔軟なパターンマッチングで以下の形式に対応:
    - "### 完了したステップ" セクション内のステップ
    - "✅ 1.1 完了" のような完了マーカー付きの行
    - "Step 1.1: done" のような英語形式
    """
    import re

    completed_steps = []

    # 「完了したステップ」セクションを探す
    in_completed_section = False
    for line in implementation_result.split("\n"):
        line_stripped = line.strip()

        # セクション開始を検出（より柔軟に）
        if any(keyword in line.lower() for keyword in [
            "完了したステップ", "completed steps", "実装完了",
            "finished", "done steps", "完了項目"
        ]):
            in_completed_section = True
            continue

        # 別のセクション開始で終了
        if line_stripped.startswith("###") or line_stripped.startswith("## "):
            if in_completed_section:
                in_completed_section = False
            continue

        # ステップ番号を抽出（例: "- 1.1:", "- **2.0**", "1.1 説明"）
        if in_completed_section and line_stripped:
            # パターン: "- 1.1:" or "- **1.1**" or "1.1 説明"
            match = re.search(r"[\-\*]?\s*\*{0,2}(\d+\.\d+)\*{0,2}[:\s]", line_stripped)
            if match:
                step_id = match.group(1)
                completed_steps.append(step_id)

    # セクションが見つからない場合、全体から推測
    if not completed_steps:
        for line in implementation_result.split("\n"):
            # 完了を示すキーワードと一緒にあるステップ番号を探す
            if any(keyword in line for keyword in [
                "✅", "完了", "done", "implemented", "finished",
                "✓", "完", "済", "[x]", "[X]"
            ]):
                match = re.search(r"(\d+\.\d+)", line)
                if match:
                    completed_steps.append(match.group(1))

    result = list(set(completed_steps))  # 重複を除去
    logger.debug(f"実装結果から抽出したステップ: {result}")
    return result
