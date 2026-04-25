"""
Workflow State Definitions

ワークフローの状態を定義するモジュール。
"""

import os
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, TypedDict

from .config import get_config


class PhaseStatus(str, Enum):
    """Phaseの状態"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class VerificationResult(str, Enum):
    """検証結果"""
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


class AuditLogEntry(TypedDict):
    """監査ログエントリ"""
    timestamp: str
    phase: int
    action: str
    result: str
    details: Optional[dict]
    error: Optional[str]


class ReviewComment(TypedDict):
    """レビューコメント情報"""
    id: int  # コメントID（返信用）
    thread_id: Optional[str]  # スレッドID（Resolve用、GraphQL node ID）
    body: str  # コメント本文
    path: Optional[str]  # ファイルパス
    line: Optional[int]  # 行番号
    replied: bool  # 返信済みフラグ
    resolved: bool  # スレッド解決済みフラグ
    fix_summary: Optional[str]  # 修正概要（返信時に使用）
    comment_type: Optional[str]  # "review" | "issue"（デフォルト: "review"）


class ReviewRuleResult(TypedDict):
    """レビュールール結果"""
    name: str  # ルール名（例: "Dead Code"）
    result: str  # "OK" | "NG" | "SKIP"
    note: str  # 備考（例: "SpaceBackgroundIdが未使用"）


class VerificationErrorEntry(TypedDict):
    """検証エラー詳細（Phase 6 → Phase 5 リトライ時の情報伝達用）"""
    repository: str  # リポジトリ名（例: "Backend", "API"）
    command: str  # コマンド種別（"build", "test", "lint"）
    success: bool  # 成功/失敗
    error_output: Optional[str]  # 失敗時のエラー出力（最大500行）


class RepositoryPhaseStatus(str, Enum):
    """リポジトリ単位のフェーズ状態"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RepositoryState(TypedDict):
    """リポジトリごとの状態管理

    各リポジトリの進行状況を独立して管理し、部分的な完了を可能にする。

    Attributes:
        name: リポジトリ名（例: "Backend", "API"）
        path: ローカルパス（worktree 使用時は worktree path）
        source_path: 元リポジトリの実パス（config の repo.path に相当）
        worktree_created: HOKUSAI が作成した worktree かどうか（cleanup 判定用）
        branch: 作業ブランチ名
        base_branch: マージ先ブランチ
        phase_status: フェーズ別ステータス（{5: "completed", 6: "failed"}）
        pr_url: PRのURL（作成済みの場合）
        pr_number: PR番号（作成済みの場合）
        verification_results: 検証結果（build, test, lint）
        review_passed: レビュー通過フラグ
        review_issues: レビューで検出された問題
    """
    name: str
    path: str
    source_path: str
    worktree_created: bool
    branch: str
    base_branch: str
    phase_status: dict  # Dict[int, str] - フェーズ番号 -> RepositoryPhaseStatus value
    pr_url: Optional[str]
    pr_number: Optional[int]
    verification_results: list  # List[VerificationErrorEntry]
    review_passed: bool
    review_issues: list  # List[str]


class PRStatus(str, Enum):
    """PRの状態"""
    PENDING = "pending"  # 未処理
    DRAFT = "draft"  # Draft PR
    REVIEWING = "reviewing"  # レビュー中
    CHANGES_REQUESTED = "changes_requested"  # 修正要求あり
    APPROVED = "approved"  # 承認済み
    MERGED = "merged"  # マージ済み


class PullRequestInfo(TypedDict):
    """PR情報"""
    repo_name: str  # リポジトリ名（例: "Schema", "Backend", "Frontend", "API"）
    title: str  # PRタイトル
    url: str  # PR URL
    number: int  # PR番号
    # 拡張フィールド（複数PR対応）
    status: Optional[str]  # PRStatus value（HOKUSAI内部ステータス）
    github_status: Optional[str]  # GitHubステータス（open/closed/merged/draft）
    owner: Optional[str]  # リポジトリオーナー（例: "my-org"）
    repo: Optional[str]  # リポジトリ名（例: "my-backend"）
    copilot_comments: Optional[list]  # Copilotレビューコメント
    human_comments: Optional[list]  # 人間レビューコメント
    issue_comments: Optional[list]  # Issue comment（PR全体へのコメント）
    copilot_review_passed: Optional[bool]
    human_review_passed: Optional[bool]


class PhaseState(TypedDict):
    """Phase状態"""
    status: str  # PhaseStatus value
    started_at: Optional[str]
    completed_at: Optional[str]
    error_message: Optional[str]
    retry_count: int


class WorkflowState(TypedDict):
    """ワークフロー状態"""
    # === 識別情報 ===
    workflow_id: str
    task_url: str
    task_title: str

    # === Git情報 ===
    branch_name: str
    base_branch: str

    # === 現在の状態 ===
    current_phase: int
    run_mode: str  # "step" | "auto"
    phases: dict  # Dict[int, PhaseState]

    # === スキーマ変更関連 ===
    schema_change_required: bool
    schema_pr_url: Optional[str]
    schema_pr_merged: bool

    # === PR関連 ===
    pull_requests: list  # List[PullRequestInfo] - 複数PR対応
    current_pr_index: int  # 現在処理中のPRインデックス
    notion_recorded_pr_count: int  # Notionに記録済みのPR数（差分検出用）

    # === 検証結果 ===
    verification: dict  # Dict[str, VerificationResult]
    verification_errors: list  # List[VerificationErrorEntry] - 検証失敗の詳細
    # @deprecated: repositories.phase_status を使用してください
    repository_status: dict  # Dict[str, str] - 後方互換性のため維持（読み取り専用）

    # === リポジトリ別状態管理 (C-1) ===
    repositories: list  # List[RepositoryState] - 各リポジトリの独立した状態（単一情報源）

    # === レビュー結果 ===
    final_review_passed: bool
    final_review_issues: list
    final_review_rules: dict  # Dict[str, ReviewRuleResult] - ルール別結果
    final_review_by_repo: dict  # リポジトリ別レビュー結果 {"Backend": {"passed": True, "rules": {...}}, ...}

    # === Phase 4: 作業計画 ===
    research_result: Optional[str]  # task-researchの出力（Phase 2）
    design_result: Optional[str]  # Phase 3 設計チェック結果
    work_plan: Optional[str]  # dev-planの出力

    # === Phase 5: 実装 ===
    implementation_result: Optional[str]  # 実装結果

    # === ブランチ衛生チェック (Phase 7.5) ===
    expected_changed_files: list  # dev-planで設定される期待変更ファイル
    branch_hygiene_issues: list  # 検出されたブランチ衛生問題
    cherry_picked_from: Optional[str]  # チェリーピック元のブランチ名
    cherry_picked_commits: list  # チェリーピックしたコミットハッシュ

    # === 設定 ===
    config_name: Optional[str]  # 使用した設定ファイル名（例: "my-project"）

    # === Notion接続状態 ===
    notion_connected: Optional[bool]  # True=接続成功, False=未接続(スキップ), None=未確認

    # === メタデータ ===
    created_at: str
    updated_at: str
    total_retry_count: int

    # === 監査ログ ===
    audit_log: list  # List[AuditLogEntry]

    # === Human-in-the-loop ===
    waiting_for_human: bool
    human_input_request: Optional[str]
    last_environment_error: Optional[dict]  # Phase 6で検出された環境問題の情報

    # === PR承認待ち ===
    waiting_for_pr_approval: bool
    pr_ready_for_review: bool

    # === Copilotレビュー ===
    waiting_for_copilot_review: bool
    copilot_review_passed: bool
    copilot_review_comments: list  # List[ReviewComment] - コメント詳細（ID含む）
    copilot_fix_requested: bool  # 修正作業が必要

    # === 人間レビュー ===
    waiting_for_human_review: bool
    human_review_passed: bool
    human_review_comments: list  # List[ReviewComment] - コメント詳細（ID含む）
    human_fix_requested: bool  # 修正作業が必要

    # === Issue Comment（PR全体へのコメント） ===
    issue_comments: list  # List[ReviewComment] with comment_type="issue"

    # === 統合レビューループ ===
    review_fix_requested: bool  # 統合レビュー: 修正作業が必要（Copilot/人間共通）
    auto_fix_attempts: int  # 自動修正の試行回数（連続リトライ制限に使用）
    push_verification_failed: bool  # プッシュ検証失敗フラグ

    # === クロスLLMレビュー結果 ===
    cross_review_results: dict  # {2: {...}, 4: {...}} — Phase番号をキーにレビュー結果を格納

    # === Notion子ページ管理 ===
    phase_subpages: dict  # {2: "https://notion.so/...", 3: "...", 4: "..."} — Phase番号をキーに子ページURLを格納

    # === フェーズページ補助情報 ===
    phase_page_decision: dict  # {2: "none", 3: "approve_and_move_next"} — 人間判断の補助情報
    phase_page_last_human_note_at: dict  # {2: "2026-03-08T10:00:00+09:00"}
    phase_page_recommended_action: dict  # {2: "none", 3: "approve_and_move_next"} — 推奨表示の補助情報


def create_initial_state(
    task_url: str,
    task_title: str = "",
    branch_name: str = "",
    from_phase: int | None = None,
    run_mode: str = "auto",
    config_name: str | None = None,
) -> WorkflowState:
    """
    ワークフローの初期状態を生成

    Args:
        task_url: タスクのURL
        task_title: タスクタイトル
        branch_name: ブランチ名
        from_phase: 開始フェーズ（指定した場合、以前のフェーズはスキップ扱い）
    """
    now = datetime.now().isoformat()
    config = get_config()

    # フェーズの初期状態を作成
    phases = {}
    for i in range(1, 11):
        if from_phase and i < from_phase:
            # from_phase以前のフェーズはスキップ扱い
            phases[i] = PhaseState(
                status=PhaseStatus.SKIPPED.value,
                started_at=None,
                completed_at=now,
                error_message=None,
                retry_count=0,
            )
        else:
            phases[i] = PhaseState(
                status=PhaseStatus.PENDING.value,
                started_at=None,
                completed_at=None,
                error_message=None,
                retry_count=0,
            )

    return WorkflowState(
        workflow_id=os.environ.pop("HOKUSAI_WORKFLOW_ID", None) or f"wf-{uuid.uuid4().hex[:8]}",
        config_name=os.environ.pop("HOKUSAI_CONFIG_NAME", None) or config_name,
        task_url=task_url,
        task_title=task_title,
        branch_name=branch_name,
        base_branch=config.base_branch,
        current_phase=from_phase or 1,
        run_mode=run_mode,
        phases=phases,
        schema_change_required=False,
        schema_pr_url=None,
        schema_pr_merged=False,
        pull_requests=[],
        current_pr_index=0,
        notion_recorded_pr_count=0,
        verification={
            "build": VerificationResult.NOT_RUN.value,
            "test": VerificationResult.NOT_RUN.value,
            "lint": VerificationResult.NOT_RUN.value,
        },
        verification_errors=[],
        repository_status={},  # @deprecated: repositories を使用
        repositories=[],
        final_review_passed=False,
        final_review_issues=[],
        final_review_rules={},
        final_review_by_repo={},
        research_result=None,
        design_result=None,
        work_plan=None,
        implementation_result=None,
        expected_changed_files=[],
        branch_hygiene_issues=[],
        cherry_picked_from=None,
        cherry_picked_commits=[],
        notion_connected=os.environ.get("HOKUSAI_SKIP_NOTION") != "1",
        created_at=now,
        updated_at=now,
        total_retry_count=0,
        audit_log=[],
        waiting_for_human=False,
        human_input_request=None,
        last_environment_error=None,
        waiting_for_pr_approval=False,
        pr_ready_for_review=False,
        waiting_for_copilot_review=False,
        copilot_review_passed=False,
        copilot_review_comments=[],
        copilot_fix_requested=False,
        waiting_for_human_review=False,
        human_review_passed=False,
        human_review_comments=[],
        human_fix_requested=False,
        # Issue comment
        issue_comments=[],
        # 統合レビューループ
        review_fix_requested=False,
        auto_fix_attempts=0,
        push_verification_failed=False,
        # クロスLLMレビュー
        cross_review_results={},
        # Notion子ページ
        phase_subpages={},
        phase_page_decision={},
        phase_page_last_human_note_at={},
        phase_page_recommended_action={},
    )


def update_phase_status(
    state: WorkflowState,
    phase: int,
    status: PhaseStatus,
    error_message: Optional[str] = None,
) -> WorkflowState:
    """Phase状態を更新"""
    now = datetime.now().isoformat()

    phase_state = state["phases"][phase].copy()
    phase_state["status"] = status.value

    if status == PhaseStatus.IN_PROGRESS:
        phase_state["started_at"] = now
        state["current_phase"] = phase
    elif status == PhaseStatus.COMPLETED:
        phase_state["completed_at"] = now
        # 完了時に current_phase を次の実行対象へ進める
        state["current_phase"] = phase + 1
    elif status in (PhaseStatus.FAILED, PhaseStatus.SKIPPED):
        phase_state["completed_at"] = now

    if error_message:
        phase_state["error_message"] = error_message

    state["phases"][phase] = phase_state
    state["updated_at"] = now

    return state


def add_audit_log(
    state: WorkflowState,
    phase: int,
    action: str,
    result: str,
    details: Optional[dict] = None,
    error: Optional[str] = None,
) -> WorkflowState:
    """監査ログにエントリを追加"""
    entry = AuditLogEntry(
        timestamp=datetime.now().isoformat(),
        phase=phase,
        action=action,
        result=result,
        details=details,
        error=error,
    )
    state["audit_log"].append(entry)
    state["updated_at"] = datetime.now().isoformat()
    return state


def should_skip_phase(state: WorkflowState, phase: int) -> bool:
    """
    指定されたフェーズをスキップすべきかチェック

    --from-phase オプションで明示的にスキップされたフェーズのみスキップする。
    COMPLETED 状態のフェーズはリトライ時に再実行されるべきなのでスキップしない。

    Args:
        state: ワークフロー状態
        phase: フェーズ番号

    Returns:
        True: スキップすべき、False: 実行すべき
    """
    phase_state = state["phases"].get(phase, {})
    status = phase_state.get("status", PhaseStatus.PENDING.value)

    # 明示的にSKIPPED状態の場合のみスキップ（--from-phase使用時）
    # COMPLETED状態はリトライ時に再実行するためスキップしない
    return status == PhaseStatus.SKIPPED.value


# === 複数PR対応ヘルパー関数 ===


def get_current_pr(state: WorkflowState) -> Optional[PullRequestInfo]:
    """
    現在処理中のPRを取得

    Args:
        state: ワークフロー状態

    Returns:
        現在のPR情報、存在しない場合はNone
    """
    pull_requests = state.get("pull_requests", [])
    index = state.get("current_pr_index", 0)
    if 0 <= index < len(pull_requests):
        return pull_requests[index]
    return None


def get_pr_by_url(state: WorkflowState, url: str) -> Optional[PullRequestInfo]:
    """
    URLでPRを検索

    Args:
        state: ワークフロー状態
        url: PR URL

    Returns:
        PR情報、見つからない場合はNone
    """
    for pr in state.get("pull_requests", []):
        if pr.get("url") == url:
            return pr
    return None


def update_pr_in_list(state: WorkflowState, url: str, updates: dict) -> WorkflowState:
    """
    pull_requestsリスト内の特定PRを更新

    Args:
        state: ワークフロー状態
        url: PR URL
        updates: 更新するフィールド

    Returns:
        更新されたワークフロー状態
    """
    pull_requests = state.get("pull_requests", [])
    for i, pr in enumerate(pull_requests):
        if pr.get("url") == url:
            pull_requests[i] = {**pr, **updates}
            break
    state["pull_requests"] = pull_requests
    return state


def move_to_next_pr(state: WorkflowState) -> WorkflowState:
    """
    次のPRに移動

    Args:
        state: ワークフロー状態

    Returns:
        更新されたワークフロー状態
    """
    pull_requests = state.get("pull_requests", [])
    current_index = state.get("current_pr_index", 0)
    if current_index < len(pull_requests) - 1:
        state["current_pr_index"] = current_index + 1
    return state


def all_prs_completed(state: WorkflowState) -> bool:
    """
    全PRの処理が完了したかチェック

    Args:
        state: ワークフロー状態

    Returns:
        True: 全PR完了、False: 未完了のPRあり
    """
    pull_requests = state.get("pull_requests", [])
    if not pull_requests:
        return True

    for pr in pull_requests:
        status = pr.get("status", PRStatus.PENDING.value)
        # APPROVED または MERGED 以外は未完了
        if status not in (PRStatus.APPROVED.value, PRStatus.MERGED.value):
            return False
    return True


def get_pending_prs(state: WorkflowState) -> list[PullRequestInfo]:
    """
    未処理またはレビュー中のPRを取得

    Args:
        state: ワークフロー状態

    Returns:
        未処理のPRリスト
    """
    pending_statuses = (
        PRStatus.PENDING.value,
        PRStatus.DRAFT.value,
        PRStatus.REVIEWING.value,
        PRStatus.CHANGES_REQUESTED.value,
    )
    return [
        pr for pr in state.get("pull_requests", [])
        if pr.get("status", PRStatus.PENDING.value) in pending_statuses
    ]


# === リポジトリ別状態管理ヘルパー関数 (C-1) ===


def init_repository_state(
    name: str,
    path: str,
    branch: str,
    base_branch: str,
    source_path: str = "",
    worktree_created: bool = False,
) -> RepositoryState:
    """
    リポジトリ状態を初期化

    Args:
        name: リポジトリ名（例: "Backend", "API"）
        path: ローカルパス（worktree 使用時は worktree path）
        branch: 作業ブランチ名
        base_branch: マージ先ブランチ
        source_path: 元リポジトリの実パス（未指定時は path と同値）
        worktree_created: HOKUSAI が作成した worktree かどうか

    Returns:
        初期化されたリポジトリ状態
    """
    return RepositoryState(
        name=name,
        path=path,
        source_path=source_path or path,
        worktree_created=worktree_created,
        branch=branch,
        base_branch=base_branch,
        phase_status={},
        pr_url=None,
        pr_number=None,
        verification_results=[],
        review_passed=False,
        review_issues=[],
    )


def get_repository_state(
    state: WorkflowState,
    repo_name: str,
) -> Optional[RepositoryState]:
    """
    リポジトリ名から状態を取得

    Args:
        state: ワークフロー状態
        repo_name: リポジトリ名

    Returns:
        リポジトリ状態、見つからない場合はNone
    """
    for repo in state.get("repositories", []):
        if repo.get("name") == repo_name:
            return repo
    return None


def update_repository_state(
    state: WorkflowState,
    repo_name: str,
    updates: dict,
) -> WorkflowState:
    """
    リポジトリ状態を更新

    Args:
        state: ワークフロー状態
        repo_name: リポジトリ名
        updates: 更新するフィールド

    Returns:
        更新されたワークフロー状態
    """
    repositories = state.get("repositories", [])
    for i, repo in enumerate(repositories):
        if repo.get("name") == repo_name:
            repositories[i] = {**repo, **updates}
            break
    state["repositories"] = repositories
    state["updated_at"] = datetime.now().isoformat()
    return state


def update_repository_phase_status(
    state: WorkflowState,
    repo_name: str,
    phase: int,
    status: RepositoryPhaseStatus,
) -> WorkflowState:
    """
    リポジトリのフェーズ状態を更新

    Args:
        state: ワークフロー状態
        repo_name: リポジトリ名
        phase: フェーズ番号
        status: 新しいステータス

    Returns:
        更新されたワークフロー状態
    """
    repo = get_repository_state(state, repo_name)
    if repo:
        phase_status = repo.get("phase_status", {})
        phase_status[phase] = status.value
        state = update_repository_state(state, repo_name, {"phase_status": phase_status})

    return state


def get_pending_repositories(
    state: WorkflowState,
    phase: int,
) -> list[RepositoryState]:
    """
    指定フェーズが未完了のリポジトリを取得

    Args:
        state: ワークフロー状態
        phase: フェーズ番号

    Returns:
        未完了のリポジトリリスト
    """
    pending = []
    for repo in state.get("repositories", []):
        phase_status = repo.get("phase_status", {})
        status = phase_status.get(phase, RepositoryPhaseStatus.PENDING.value)
        if status not in (
            RepositoryPhaseStatus.COMPLETED.value,
            RepositoryPhaseStatus.SKIPPED.value,
        ):
            pending.append(repo)
    return pending


def get_completed_repositories(
    state: WorkflowState,
    phase: int,
) -> list[RepositoryState]:
    """
    指定フェーズが完了したリポジトリを取得

    Args:
        state: ワークフロー状態
        phase: フェーズ番号

    Returns:
        完了したリポジトリリスト
    """
    completed = []
    for repo in state.get("repositories", []):
        phase_status = repo.get("phase_status", {})
        status = phase_status.get(phase, RepositoryPhaseStatus.PENDING.value)
        if status == RepositoryPhaseStatus.COMPLETED.value:
            completed.append(repo)
    return completed


def all_repositories_completed(
    state: WorkflowState,
    phase: int,
) -> bool:
    """
    全リポジトリで指定フェーズが完了したかチェック

    Args:
        state: ワークフロー状態
        phase: フェーズ番号

    Returns:
        True: 全リポジトリ完了、False: 未完了あり
    """
    repositories = state.get("repositories", [])
    if not repositories:
        return True

    for repo in repositories:
        phase_status = repo.get("phase_status", {})
        status = phase_status.get(phase, RepositoryPhaseStatus.PENDING.value)
        if status not in (
            RepositoryPhaseStatus.COMPLETED.value,
            RepositoryPhaseStatus.SKIPPED.value,
        ):
            return False
    return True
