"""
共通テストフィクスチャ
"""

import pytest
from datetime import datetime

from hokusai.state import (
    WorkflowState,
    PhaseStatus,
    VerificationResult,
    VerificationErrorEntry,
    PhaseState,
)


@pytest.fixture
def minimal_state() -> WorkflowState:
    """最小限のワークフロー状態"""
    now = datetime.now().isoformat()
    phases = {}
    for i in range(1, 11):
        phases[i] = PhaseState(
            status=PhaseStatus.PENDING.value,
            started_at=None,
            completed_at=None,
            error_message=None,
            retry_count=0,
        )

    return WorkflowState(
        workflow_id="wf-test0001",
        task_url="https://notion.so/test-task",
        task_title="Test Task",
        branch_name="feature/test",
        base_branch="main",
        current_phase=1,
        run_mode="auto",
        phases=phases,
        schema_change_required=False,
        schema_pr_url=None,
        schema_pr_merged=False,
        pull_requests=[],
        current_pr_index=0,
        verification={
            "build": VerificationResult.NOT_RUN.value,
            "test": VerificationResult.NOT_RUN.value,
            "lint": VerificationResult.NOT_RUN.value,
        },
        verification_errors=[],
        repository_status={},
        repositories=[],
        final_review_passed=False,
        final_review_issues=[],
        final_review_rules={},
        final_review_by_repo={},
        research_result=None,
        work_plan=None,
        implementation_result=None,
        expected_changed_files=[],
        branch_hygiene_issues=[],
        cherry_picked_from=None,
        cherry_picked_commits=[],
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
        issue_comments=[],
        review_fix_requested=False,
        cross_review_results={},
    )


@pytest.fixture
def state_with_verification_errors(minimal_state: WorkflowState) -> WorkflowState:
    """検証エラーを含むワークフロー状態"""
    minimal_state["verification_errors"] = [
        VerificationErrorEntry(
            repository="Backend",
            command="build",
            success=True,
            error_output=None,
        ),
        VerificationErrorEntry(
            repository="Backend",
            command="test",
            success=False,
            error_output="Error: Test failed\nExpected 1 but got 2",
        ),
        VerificationErrorEntry(
            repository="API",
            command="build",
            success=False,
            error_output="Error: TypeScript compilation failed\nTS2345: Argument of type...",
        ),
    ]
    minimal_state["phases"][6]["retry_count"] = 1
    return minimal_state


@pytest.fixture
def state_with_repository_status(minimal_state: WorkflowState) -> WorkflowState:
    """リポジトリステータスを含むワークフロー状態"""
    minimal_state["repository_status"] = {
        "Backend": "completed",
        "API": "failed",
    }
    return minimal_state


@pytest.fixture
def state_with_review_issues(minimal_state: WorkflowState) -> WorkflowState:
    """レビュー問題を含むワークフロー状態"""
    minimal_state["final_review_issues"] = [
        "console.log should be removed",
        "Unused import: lodash",
    ]
    minimal_state["phases"][7]["retry_count"] = 1
    return minimal_state
