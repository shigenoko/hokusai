"""
Workflow Definition

LangGraphベースの開発ワークフロー定義。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from .config import get_config
from .constants import MAX_WORKFLOW_EVENTS, PHASE_NAMES
from .integrations.notifications import notify_slack
from .logging_config import get_logger
from .state import PhaseStatus, add_audit_log, create_initial_state, update_phase_status

logger = get_logger("workflow")

from .graph import create_compiled_workflow  # noqa: E402
from .persistence import SQLiteStore  # noqa: E402
from .ui.console import (  # noqa: E402
    print_active_workflows,
    print_dry_run_resume,
    print_dry_run_start,
    print_existing_workflow_found,
    print_from_phase_warning,
    print_loop_detected,
    print_loop_detection_details,
    print_max_events_reached,
    print_no_active_workflows,
    print_phase_executing,
    print_workflow_completed,
    print_workflow_not_found,
    print_workflow_resume,
    print_workflow_start,
    print_workflow_status,
    prompt_step_confirmation,
)


@dataclass
class StreamResult:
    """ストリーム実行結果"""
    final_state: dict
    events_processed: int
    interrupted: bool
    interrupt_reason: str | None
    loop_detected: bool


class WorkflowRunner:
    """ワークフロー実行クラス"""

    # フェーズ名のマッピング
    PHASE_NAMES = PHASE_NAMES

    def __init__(self, verbose: bool = False, dry_run: bool = False, step_mode: bool = False):
        """
        初期化

        Args:
            verbose: 詳細ログモード
            dry_run: ドライランモード（実際の処理は行わない）
            step_mode: ステップ実行モード（各フェーズ完了後に一時停止）
        """
        self.config = get_config()
        self.store = SQLiteStore(self.config.database_path)
        self.compiled_workflow = None
        self.verbose = verbose
        self.dry_run = dry_run
        self.step_mode = step_mode

        if verbose:
            logger.debug("WorkflowRunner初期化")
            logger.debug(f"  データベース: {self.config.database_path}")
            logger.debug(f"  チェックポイントDB: {self.config.checkpoint_db_path}")
            logger.debug(f"  ステップモード: {step_mode}")

    def _ensure_compiled(self):
        """ワークフローがコンパイルされていることを確認"""
        if self.compiled_workflow is None:
            self.compiled_workflow = create_compiled_workflow()

    def _validate_worktrees(self, state: dict) -> None:
        """state 内の worktree パスが実在するか検証する

        worktree_created=True のリポジトリについて、path が存在しない場合は
        RuntimeError を発生させる。

        Args:
            state: ワークフロー状態

        Raises:
            RuntimeError: worktree が存在しない場合
        """
        for repo in state.get("repositories", []):
            if not repo.get("worktree_created", False):
                continue
            wt_path_str = repo.get("path", "")
            if not wt_path_str:
                continue
            wt_path = Path(wt_path_str)
            if not wt_path.exists():
                workflow_id = state.get("workflow_id", "unknown")
                repo_name = repo.get("name", "unknown")
                raise RuntimeError(
                    f"Worktree が存在しません: {wt_path}\n"
                    f"  リポジトリ: {repo_name}\n"
                    f"  ワークフロー: {workflow_id}\n"
                    f"推奨アクション:\n"
                    f"  - hokusai cleanup {workflow_id} で古い状態を削除\n"
                    f"  - 新しいワークフローを作成\n"
                )

    def _prompt_step_confirmation(self, phase: int, state: dict) -> bool:
        """
        ステップ実行モードでユーザーに確認を求める

        Args:
            phase: 完了したフェーズ番号
            state: 現在の状態

        Returns:
            True: 続行, False: 中止
        """
        # 非対話実行（ダッシュボード経由）は入力待ちせず1フェーズで停止
        if os.environ.get("HOKUSAI_NONINTERACTIVE_CONTINUE", "0") == "1":
            return False

        return prompt_step_confirmation(
            phase,
            state,
            phase_names=self.PHASE_NAMES,
            on_show_status=lambda s: print_workflow_status(s, self.PHASE_NAMES),
        )

    def start(
        self,
        task_url: str,
        from_phase: int | None = None,
        branch_name: str | None = None,
    ) -> str:
        """
        新しいワークフローを開始

        Args:
            task_url: NotionタスクのURL
            from_phase: 開始フェーズ（省略時はPhase 1から）
            branch_name: 使用するブランチ名（from_phase使用時に既存ブランチを指定）

        Returns:
            作成されたワークフローID
        """
        logger.info(f"ワークフロー開始: task_url={task_url}, from_phase={from_phase}")
        self._ensure_compiled()

        # --from-phase 使用時の警告
        if from_phase and from_phase >= 5:
            print_from_phase_warning(from_phase)
            logger.warning(f"--from-phase {from_phase} で開始: 前段階のデータなし")

        # 既存のワークフローを確認
        existing = self.store.find_workflow_by_task_url(task_url)
        if existing:
            # worktree の存在確認（手動削除されている場合はエラー）
            self._validate_worktrees(existing)

            workflow_id = existing["workflow_id"]
            current_phase = existing["current_phase"]
            notion_connected_env = os.environ.get("HOKUSAI_SKIP_NOTION") != "1"
            # Notion リトライで True に復元済みなら上書きしない
            if notion_connected_env or existing.get("notion_connected") is not True:
                if existing.get("notion_connected") != notion_connected_env:
                    existing["notion_connected"] = notion_connected_env
                    self.store.save_workflow(workflow_id, existing)
            print_existing_workflow_found(workflow_id, current_phase)
            logger.info(f"既存のワークフローを発見: {workflow_id}, phase={current_phase}")
            return workflow_id

        # 初期状態を作成
        state = create_initial_state(
            task_url,
            branch_name=branch_name or "",
            from_phase=from_phase,
            run_mode=("step" if self.step_mode else "auto"),
        )
        # Notion接続状態を実行時点の環境に合わせて明示的に反映
        state["notion_connected"] = os.environ.get("HOKUSAI_SKIP_NOTION") != "1"
        workflow_id = state["workflow_id"]

        if self.verbose:
            logger.debug("初期状態を作成:")
            logger.debug(f"  workflow_id: {workflow_id}")
            logger.debug(f"  base_branch: {state['base_branch']}")

        # ドライランモードの場合
        if self.dry_run:
            start_phase = from_phase or 1
            print_dry_run_start(workflow_id, task_url, branch_name, start_phase)
            logger.info("[ドライラン] 実際の処理はスキップ")
            return workflow_id

        # cross_review 実効設定をログ出力
        _log_cross_review_config(self.config)

        # ワークフローを開始（Phase 1から）
        print_workflow_start(workflow_id)

        # 開始時点で即座に永続化（ダッシュボードに表示されるように）
        self.store.save_workflow(workflow_id, state)
        logger.debug(f"初期状態を永続化: workflow_id={workflow_id}")

        # ワークフロー開始の通知（best effort: 失敗しても本体は止めない）
        _safe_notify("workflow_started", state)

        config = {"configurable": {"thread_id": workflow_id}}

        # Phase 1から自動実行（共通ループを使用）
        self._run_stream_loop(state, config, workflow_id)

        return workflow_id

    def continue_workflow(self, workflow_id: str, action: str | None = None) -> None:
        """
        中断したワークフローを再開

        Args:
            workflow_id: ワークフローID
            action: 衛生チェック対応アクション（rebase, cherry-pick, merge-{base}, ignore）
        """
        logger.info(f"ワークフロー再開: {workflow_id}")
        self._ensure_compiled()

        # 状態を読み込み
        state = self.store.load_workflow(workflow_id)
        if state is None:
            print_workflow_not_found(workflow_id)
            logger.error(f"ワークフローが見つかりません: {workflow_id}")
            return

        # worktree の存在確認（手動削除されている場合はエラー）
        self._validate_worktrees(state)

        # cross_review 実効設定をログ出力
        _log_cross_review_config(self.config)

        current_phase = state.get("current_phase", 1)
        print_workflow_resume(workflow_id, current_phase)
        logger.info(f"現在のフェーズ: Phase {current_phase}")

        if self.verbose:
            logger.debug("状態を読み込み:")
            logger.debug(f"  task_url: {state.get('task_url')}")
            logger.debug(f"  branch_name: {state.get('branch_name')}")
            logger.debug(f"  waiting_for_human: {state.get('waiting_for_human')}")

        # 実行時点の Notion 接続状態を state に反映し、ダッシュボードで即時可視化する
        # ただし、ダッシュボードからの Notion リトライで True に復元済みの場合は
        # 上書きしない（非対話モードの接続チェック失敗で False に戻るのを防止）
        notion_connected_env = os.environ.get("HOKUSAI_SKIP_NOTION") != "1"
        if notion_connected_env or state.get("notion_connected") is not True:
            state["notion_connected"] = notion_connected_env
        self.store.save_workflow(workflow_id, state)

        # ドライランモードの場合
        if self.dry_run:
            print_dry_run_resume(workflow_id, current_phase)
            logger.info("[ドライラン] 実際の処理はスキップ")
            return

        config = {"configurable": {"thread_id": workflow_id}}

        # ブランチ衛生チェック対応アクションの処理
        if action and state.get("human_input_request") == "branch_hygiene":
            from .nodes.phase7_5_hygiene import handle_hygiene_action
            logger.info(f"衛生チェックアクション実行: {action}")
            state = handle_hygiene_action(state, action)
            self.store.save_workflow(workflow_id, state)
            # コンフリクト等で再度待機状態になった場合は中断
            if state.get("waiting_for_human"):
                logger.info("衛生チェックアクション後も待機状態のため中断")
                return

        # ワークフローを再開
        # Human-in-the-loopの場合、waiting_for_humanをFalseに設定
        if state.get("waiting_for_human"):
            state["waiting_for_human"] = False
            logger.debug("waiting_for_human フラグをクリア")

        # チェックポイントが存在するか確認
        checkpoint_state = self.compiled_workflow.get_state(config)
        has_checkpoint = checkpoint_state is not None and checkpoint_state.values

        # チェックポイントが存在しても state と矛盾していれば state ベースで再開
        if has_checkpoint and not self._checkpoint_consistent_with_state(checkpoint_state, state):
            logger.warning("チェックポイントが state と矛盾 → state ベースで再開します")
            has_checkpoint = False

        if has_checkpoint:
            # チェックポイントが存在する場合は状態を更新してから再開
            logger.debug("チェックポイントから再開します")
            # SQLite の state 全体をチェックポイントに反映
            # ダッシュボードの自動修復（failed→completed 等）を含めて同期する
            self.compiled_workflow.update_state(config, state)
            self._run_stream_loop(None, config, workflow_id, resume_from_checkpoint=True)
        else:
            # チェックポイントがない or 矛盾している場合、状態から開始ノードを決定
            current_phase = state.get("current_phase", 1)

            if current_phase == 1:
                # Phase 1からは通常開始（エントリーポイントから）
                logger.debug("Phase 1から通常開始します")
                self._run_stream_loop(state, config, workflow_id)
            else:
                # Phase 2以降の場合、チェックポイントを作成して再開
                logger.debug("チェックポイントなし/不整合、状態からチェックポイントを作成します")
                resume_node = self._determine_resume_node(state)
                logger.debug(f"再開ノード: {resume_node}")

                # update_stateで状態を設定（as_nodeで開始ノードを指定）
                self.compiled_workflow.update_state(config, state, as_node=resume_node)
                self._run_stream_loop(None, config, workflow_id, resume_from_checkpoint=True)

    def status(self, workflow_id: str | None = None) -> None:
        """
        ワークフローの状態を表示

        Args:
            workflow_id: ワークフローID（省略時はアクティブな全ワークフロー）
        """
        if workflow_id:
            state = self.store.load_workflow(workflow_id)
            if state is None:
                print_workflow_not_found(workflow_id)
                return
            self._print_workflow_status(state)
        else:
            workflows = self.store.list_active_workflows()
            if not workflows:
                print_no_active_workflows()
                return

            print_active_workflows(workflows)

    def update_pr_status(
        self,
        workflow_id: str,
        pr_number: int,
        status: str | None = None,
        github_status: str | None = None,
    ) -> tuple[bool, str]:
        """
        PRのステータスを更新

        Args:
            workflow_id: ワークフローID
            pr_number: PR番号
            status: ワークフロー内ステータス
            github_status: GitHubステータス

        Returns:
            (成功フラグ, メッセージ)
        """
        return self.store.update_pr_status(
            workflow_id, pr_number, status, github_status
        )

    def sync_pr_status(
        self,
        workflow_id: str,
        pr_number: int,
    ) -> tuple[bool, str]:
        """
        GitHubからPRの状態を取得してDBを同期

        Args:
            workflow_id: ワークフローID
            pr_number: PR番号

        Returns:
            (成功フラグ, メッセージ)
        """

        from .integrations.git_hosting.github import GitHubHostingClient

        # ワークフローを読み込んでPR情報を取得
        state = self.store.load_workflow(workflow_id)
        if state is None:
            return False, f"ワークフロー '{workflow_id}' が見つかりません"

        pull_requests = state.get("pull_requests", [])
        target_pr = None
        for pr in pull_requests:
            if pr.get("number") == pr_number:
                target_pr = pr
                break

        if target_pr is None:
            pr_numbers = [pr.get("number") for pr in pull_requests]
            return False, f"PR #{pr_number} が見つかりません（登録済み: {pr_numbers}）"

        # リポジトリ情報を取得
        owner = target_pr.get("owner")
        repo = target_pr.get("repo")

        if not owner or not repo:
            # URLからowner/repoを抽出
            pr_url = target_pr.get("url", "")
            if "github.com" in pr_url:
                parts = pr_url.split("/")
                try:
                    idx = parts.index("github.com")
                    owner = parts[idx + 1]
                    repo = parts[idx + 2]
                except (ValueError, IndexError):
                    pass

        if not owner or not repo:
            return False, f"PR #{pr_number} のリポジトリ情報が不明です"

        # GitHubから状態を取得
        github_client = GitHubHostingClient(owner=owner, repo=repo)
        pr_status = github_client.get_pr_status_from_github(pr_number)

        if pr_status is None:
            return False, f"PR #{pr_number} の状態をGitHubから取得できませんでした"

        # DBを更新
        return self.store.update_pr_status(
            workflow_id,
            pr_number,
            github_status=pr_status.get("github_status"),
            copilot_review_passed=pr_status.get("copilot_review_passed"),
            copilot_comments=pr_status.get("copilot_comments"),
        )

    # ノード名 → フェーズ番号のマッピング
    _NODE_TO_PHASE: dict[str, int] = {
        "phase1_prepare": 1,
        "phase2_research": 2,
        "phase3_design": 3,
        "phase4_plan": 4,
        "phase5_implement": 5,
        "phase6_verify": 6,
        "phase7_review": 7,
        "phase7_5_hygiene": 7,
        "phase8a_pr_draft": 8,
        "phase8b_unified_wait": 9,
        "phase8c_unified_check": 9,
        "phase8d_unified_fix": 9,
        "phase8e_ready_for_review": 9,
        "phase8_complete": 9,
        "phase10_record": 10,
    }

    def _checkpoint_consistent_with_state(self, checkpoint_state, state: dict) -> bool:
        """チェックポイントの next が state と矛盾しないか検証する。

        next が完了済みフェーズを指している場合は矛盾と判定する。
        """
        checkpoint_next = getattr(checkpoint_state, "next", None) or ()
        phases = state.get("phases", {})

        for next_node in checkpoint_next:
            phase_num = self._NODE_TO_PHASE.get(next_node)
            if phase_num is None:
                continue
            phase_status = phases.get(phase_num, {}).get("status", "")
            if phase_status == PhaseStatus.COMPLETED.value:
                logger.warning(
                    f"チェックポイント不整合: next={next_node} は完了済み "
                    f"Phase {phase_num} を指しています"
                )
                return False
        return True

    def _determine_resume_node(self, state: dict) -> str:
        """
        状態から再開すべきノードを決定

        Args:
            state: ワークフロー状態

        Returns:
            再開ノード名
        """
        current_phase = state.get("current_phase", 1)
        human_input_request = state.get("human_input_request", "")

        # Phase 8: PR作成のみ
        if current_phase == 8:
            # as_node は「実行済みノード」として扱われるため、
            # phase8a_pr_draft を実行するにはその直前ノードを指定する
            return "phase7_5_hygiene"

        # Phase 9: レビュー対応
        if current_phase == 9:
            if human_input_request == "complete_review":
                # 全PR完了 → review loop をスキップして直接完了処理へ
                return "phase8_complete"
            elif human_input_request in ("review_wait", "copilot_review_wait", "human_review_wait", "review_status"):
                # レビュー再確認 → phase8b → phase8c
                return "phase8a_pr_draft"
            elif human_input_request == "review_fix":
                # レビュー修正後の再開
                return "phase8d_unified_fix"
            else:
                # Phase 9 初回: review loopへ
                return "phase8a_pr_draft"

        # Phase 10: 進捗記録
        if current_phase == 10:
            # next = phase10_record
            return "phase8_complete"

        # その他のPhaseはフェーズ番号に基づいて決定
        phase_to_node = {
            1: "phase1_prepare",
            2: "phase2_research",
            3: "phase3_design",
            4: "phase4_plan",
            5: "phase5_implement",
            6: "phase6_verify",
            7: "phase7_review",
        }

        if current_phase in phase_to_node:
            # 前のフェーズのノードを返す（そのフェーズが完了済みとして次から再開）
            prev_phase = current_phase - 1
            if prev_phase in phase_to_node:
                return phase_to_node[prev_phase]

        # デフォルトはPhase 1から
        return "phase1_prepare"

    def _handle_event(self, event: dict) -> int | None:
        """
        イベントを処理してログ出力

        Returns:
            現在のフェーズ番号（終了時はNone）
        """
        current_phase = None

        for node_name, node_output in event.items():
            if node_name == "__end__":
                print_workflow_completed()
                logger.info("ワークフロー完了")
            else:
                phase = node_output.get("current_phase", "?")
                current_phase = phase if isinstance(phase, int) else None

                phase_name = self.PHASE_NAMES.get(phase, "")
                print_phase_executing(phase, phase_name, node_name)
                logger.info(f"ノード実行: {node_name} (Phase {phase})")

                if self.verbose:
                    # フェーズの状態を詳細に記録
                    phases = node_output.get("phases", {})
                    current_phase_state = phases.get(phase, {})
                    logger.debug(f"  状態: {current_phase_state.get('status', 'unknown')}")

                    # 検証結果
                    verification = node_output.get("verification", {})
                    if any(v != "not_run" for v in verification.values()):
                        logger.debug(f"  検証: {verification}")

                    # Human-in-the-loop
                    if node_output.get("waiting_for_human"):
                        logger.debug(f"  待機理由: {node_output.get('human_input_request', '不明')}")

                    # 監査ログの最新エントリ
                    audit_log = node_output.get("audit_log", [])
                    if audit_log:
                        latest = audit_log[-1]
                        logger.debug(f"  監査ログ: {latest.get('action')} - {latest.get('result')}")

        return current_phase

    def _is_waiting_for_human(self, event: dict) -> bool:
        """Human-in-the-loop待機状態かチェック"""
        for _, node_output in event.items():
            if node_output.get("waiting_for_human"):
                return True
        return False

    def _print_workflow_status(self, state: dict) -> None:
        """ワークフロー状態を表示"""
        print_workflow_status(state, self.PHASE_NAMES)

    def _run_stream_loop(
        self, state: dict | None, config: dict, workflow_id: str,
        resume_from_checkpoint: bool = False,
    ) -> StreamResult:
        """
        イベントストリーム処理ループ（共通処理）

        start()とcontinue_workflow()で共有されるイベントループロジック。

        Args:
            state: 初期状態（resume_from_checkpoint=Trueの場合はNone可）
            config: LangGraph設定（thread_idを含む）
            workflow_id: ワークフローID
            resume_from_checkpoint: Trueの場合、チェックポイントから再開（stateは無視）

        Returns:
            StreamResult: ストリーム実行結果
        """
        event_count = 0
        user_aborted = False
        loop_detected = False
        max_events = MAX_WORKFLOW_EVENTS
        phase_history: list[int] = []
        _abort_state: dict | None = None  # リトライ中断時の補正済み状態
        interrupt_reason: str | None = None

        # PR 作成検出のため、開始時点の PR 数を記録（再開時は既存件数を起点とする）
        try:
            initial_state_for_pr = state if state is not None else (
                self.store.load_workflow(workflow_id) or {}
            )
            previous_pr_count = len(initial_state_for_pr.get("pull_requests") or [])
        except Exception:
            previous_pr_count = 0

        # チェックポイントから再開する場合はNoneを渡す
        stream_input = None if resume_from_checkpoint else state

        try:
            for event in self.compiled_workflow.stream(stream_input, config):
                event_count += 1
                current_phase = self._handle_event(event)

                # 毎イベント後に状態を永続化（retry_countなどの変更を確実に保存）
                current_state = self.compiled_workflow.get_state(config)
                self.store.save_workflow(workflow_id, current_state.values)
                logger.debug(f"イベント後に永続化: phase={current_phase}, event_count={event_count}")

                # PR 作成検出: pull_requests が増えていれば通知
                try:
                    current_prs = current_state.values.get("pull_requests") or []
                    if len(current_prs) > previous_pr_count:
                        _safe_notify("pr_created", current_state.values)
                        previous_pr_count = len(current_prs)
                except Exception as notify_err:
                    logger.debug(f"PR 作成通知中のエラーを抑制: {notify_err}")

                # ループ検出: 同じフェーズが繰り返されているか
                if current_phase:
                    phase_history.append(current_phase)
                    # 直近10イベントをチェック
                    if len(phase_history) >= 10:
                        recent = phase_history[-10:]
                        # Phase 5, 6, 7 のループパターンを検出
                        if recent.count(5) >= 3 and recent.count(6) >= 3 and recent.count(7) >= 3:
                            logger.warning(f"リトライループを検出: {recent}")
                            print_loop_detected(recent)
                            # 詳細情報を表示
                            current_state = self.compiled_workflow.get_state(config)
                            print_loop_detection_details(current_state.values, workflow_id)
                            loop_detected = True
                            interrupt_reason = "loop_detected"
                            break

                # 最大イベント数チェック
                if event_count >= max_events:
                    logger.warning(f"最大イベント数 ({max_events}) に達しました")
                    print_max_events_reached(max_events)
                    interrupt_reason = "max_events"
                    break

                # Human-in-the-loop待機状態になったら停止
                if self._is_waiting_for_human(event):
                    logger.info("Human-in-the-loop待機状態を検出 → ワークフロー一時停止")
                    interrupt_reason = "waiting_for_human"
                    break

                # ステップモードの場合、フェーズ完了を検知して停止
                # ノード名から実行フェーズを特定し、そのフェーズが COMPLETED なら停止する
                # NOTE: current_phase はノード内で次フェーズに更新済みのため比較に使わない
                if self.step_mode:
                    current_state = self.compiled_workflow.get_state(config)
                    sv = current_state.values if current_state else {}
                    noninteractive = os.environ.get("HOKUSAI_NONINTERACTIVE_CONTINUE", "0") == "1"
                    for node_name in event:
                        executed_phase = self._NODE_TO_PHASE.get(node_name)
                        if executed_phase is None:
                            continue
                        phase_info = sv.get("phases", {}).get(executed_phase, {})
                        phase_status = phase_info.get("status")

                        # フェーズ完了時は停止判定
                        if phase_status == PhaseStatus.COMPLETED.value:
                            if not self._prompt_step_confirmation(executed_phase, sv):
                                user_aborted = True
                                interrupt_reason = "user_aborted"
                            break  # 1イベントで1回だけ判定

                        # フェーズ失敗時: リトライ可能なら router に任せて
                        # Phase 5 への自動リトライを発動させる。
                        # リトライ上限到達（waiting_for_human）の場合のみ停止。
                        if phase_status == PhaseStatus.FAILED.value:
                            if sv.get("waiting_for_human", False):
                                logger.info(
                                    f"ステップモード: Phase {executed_phase} "
                                    f"リトライ上限到達 → 停止"
                                )
                                user_aborted = True
                                interrupt_reason = "user_aborted"
                                break
                            # リトライ可能: router 経由で Phase 5 に戻る
                            logger.info(
                                f"ステップモード: Phase {executed_phase} 失敗 "
                                f"→ router 経由でリトライ"
                            )
                            break

                        # 非対話モード: フェーズがリトライ失敗（IN_PROGRESS + retry_count > 0）
                        # の場合も停止する。Phase 5 への自動リトライを発動させず、
                        # ダッシュボードにフェーズの結果を返す。
                        # ステータスを failed に変更して、再実行時に stuck しないようにする。
                        if noninteractive and phase_status == PhaseStatus.IN_PROGRESS.value:
                            retry_count = phase_info.get("retry_count", 0)
                            if retry_count >= self.config.max_retry_count:
                                logger.info(
                                    f"非対話モード: Phase {executed_phase} リトライ上限到達 "
                                    f"(retry_count={retry_count}) → failed に変更して停止"
                                )
                                current_st = self.compiled_workflow.get_state(config)
                                if current_st and current_st.values:
                                    _abort_state = dict(current_st.values)
                                    _abort_state = update_phase_status(
                                        _abort_state, executed_phase,
                                        PhaseStatus.FAILED,
                                        f"リトライ上限（retry_count={retry_count}）",
                                    )
                                user_aborted = True
                                interrupt_reason = "user_aborted"
                                break

                    if user_aborted:
                        break
        except Exception as e:
            logger.error(f"ストリーム実行中に例外発生: {e}", exc_info=True)
            failed_state_for_notify: dict | None = None
            try:
                current_state = self.compiled_workflow.get_state(config)
                if current_state and current_state.values:
                    failed_state = dict(current_state.values)
                    # 実行中だったノードからフェーズを特定
                    # チェックポイントの next には失敗したノードが含まれる
                    cp = failed_state.get("current_phase", 1)
                    if current_state.next:
                        for next_node in current_state.next:
                            phase_from_node = self._NODE_TO_PHASE.get(next_node)
                            if phase_from_node is not None:
                                cp = phase_from_node
                                break
                    failed_state = update_phase_status(
                        failed_state, cp, PhaseStatus.FAILED, str(e),
                    )
                    failed_state = add_audit_log(
                        failed_state, cp,
                        "stream_execution_failed", "error",
                        error=str(e),
                    )
                    self.store.save_workflow(workflow_id, failed_state)
                    failed_state_for_notify = failed_state
                    logger.info(f"例外時の状態を保存: phase={cp}, error={e}")
            except Exception as save_err:
                logger.error(f"例外時の状態保存に失敗: {save_err}", exc_info=True)
            # 例外時の通知（best effort: 通知失敗で本来の例外を握り潰さない）
            _safe_notify(
                "workflow_failed",
                failed_state_for_notify,
                reason="exception",
                error=str(e),
            )
            raise

        if user_aborted:
            logger.info("ユーザーによる中断")
        else:
            logger.info(f"ワークフロー実行完了: {event_count}イベント処理")

        # 最終状態を取得・保存
        # リトライ中断時は補正済み状態を使う（LangGraph 側は IN_PROGRESS のまま）
        if _abort_state is not None:
            self.store.save_workflow(workflow_id, _abort_state)
            final_values = _abort_state
        else:
            final_state = self.compiled_workflow.get_state(config)
            self.store.save_workflow(workflow_id, final_state.values)
            final_values = final_state.values

        if self.verbose:
            logger.debug(f"最終状態を保存: phase={final_values.get('current_phase')}")

        interrupted = interrupt_reason is not None

        # 終了時の通知（best effort）
        _emit_terminal_notification(
            interrupt_reason=interrupt_reason,
            final_values=final_values,
        )

        return StreamResult(
            final_state=final_values,
            events_processed=event_count,
            interrupted=interrupted,
            interrupt_reason=interrupt_reason,
            loop_detected=loop_detected,
        )


def _log_cross_review_config(config) -> None:
    """cross_review 実効設定をログ出力する"""
    cr = config.cross_review
    logger.info(
        f"cross_review設定: enabled={cr.enabled}, model={cr.model}, "
        f"on_failure={cr.on_failure}, phases={cr.phases}, "
        f"timeout={cr.timeout}, max_correction_rounds={cr.max_correction_rounds}"
    )


def _safe_notify(
    event: str,
    state: dict | None,
    *,
    reason: str | None = None,
    error: str | None = None,
) -> None:
    """通知送信を best effort で実行する。例外はワークフローに伝播させない。"""
    try:
        notify_slack(event, state, reason=reason, error=error)
    except Exception as e:
        logger.debug(f"通知送信中に例外を抑制: event={event}, error={e}")


def _emit_terminal_notification(
    *,
    interrupt_reason: str | None,
    final_values: dict | None,
) -> None:
    """ストリーム終了時の通知を発火する。

    interrupt_reason に応じて以下のいずれかを送る:
    - "waiting_for_human" → waiting_for_human
    - "loop_detected" / "max_events" → workflow_failed
    - "user_aborted" → 何も送らない（ユーザ操作のため）
    - None（自然終了）→ Phase 10 完了なら workflow_completed
    """
    state = final_values or {}

    if interrupt_reason == "waiting_for_human":
        reason = state.get("human_input_request") or "waiting_for_human"
        _safe_notify("waiting_for_human", state, reason=str(reason))
        return

    if interrupt_reason in ("loop_detected", "max_events"):
        _safe_notify("workflow_failed", state, reason=interrupt_reason)
        return

    if interrupt_reason is None:
        # 自然終了: Phase 10 完了相当か確認
        phases = state.get("phases") or {}
        phase10 = phases.get(10) if isinstance(phases, dict) else None
        phase10_status = (
            (phase10 or {}).get("status") if isinstance(phase10, dict) else None
        )
        if phase10_status == PhaseStatus.COMPLETED.value:
            _safe_notify("workflow_completed", state)
