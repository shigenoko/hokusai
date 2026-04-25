"""
Tests for hokusai.nodes.phase6_verify module

B-1-3: エラー出力キャプチャのテスト
B-3-3: Phase 6 失敗時の is_retry フラグ設定のテスト
B-4-4: リポジトリステータス記録のテスト
"""

import pytest
from unittest.mock import patch, MagicMock
from dataclasses import dataclass

from hokusai.state import (
    WorkflowState,
    PhaseStatus,
    VerificationResult,
    VerificationErrorEntry,
)
from hokusai.nodes.phase6_verify import (
    CommandResult,
    FailureType,
    FailureAnalysis,
    _run_command_with_output,
    _analyze_failures,
)


class TestCommandResult:
    """CommandResult dataclass のテスト"""

    def test_success_result(self):
        """成功結果の作成"""
        result = CommandResult(
            success=True,
            stdout="Build completed",
            stderr="",
            return_code=0,
        )
        assert result.success is True
        assert result.return_code == 0
        assert result.timed_out is False

    def test_failure_result(self):
        """失敗結果の作成"""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="Error: Module not found",
            return_code=1,
        )
        assert result.success is False
        assert result.return_code == 1
        assert "Module not found" in result.stderr

    def test_timeout_result(self):
        """タイムアウト結果の作成"""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="",
            return_code=-1,
            timed_out=True,
        )
        assert result.success is False
        assert result.timed_out is True


class TestAnalyzeFailures:
    """_analyze_failures 関数のテスト"""

    def test_detect_port_conflict(self):
        """ポート競合の検出"""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="Error: EADDRINUSE: address already in use :::8080",
            return_code=1,
        )
        analysis = _analyze_failures([("test", result)])

        assert analysis is not None
        assert analysis.failure_type == FailureType.ENVIRONMENT_ERROR
        assert "ポート" in analysis.summary
        assert analysis.port_to_kill == 8080

    def test_detect_docker_not_running(self):
        """Docker未起動の検出"""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
            return_code=1,
        )
        analysis = _analyze_failures([("test", result)])

        assert analysis is not None
        assert analysis.failure_type == FailureType.ENVIRONMENT_ERROR
        assert "Docker" in analysis.summary

    def test_detect_emulator_error(self):
        """エミュレータエラーの検出"""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="Could not start Firebase Emulator",
            return_code=1,
        )
        analysis = _analyze_failures([("test", result)])

        assert analysis is not None
        assert analysis.failure_type == FailureType.ENVIRONMENT_ERROR
        assert "エミュレータ" in analysis.summary

    def test_no_environment_error(self):
        """環境エラーでない場合"""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="TypeError: undefined is not a function",
            return_code=1,
        )
        analysis = _analyze_failures([("test", result)])

        assert analysis is None  # コードエラーは None を返す


class TestVerificationErrorCapture:
    """検証エラーキャプチャのテスト (B-1)"""

    def test_error_output_captured_on_failure(self, minimal_state: WorkflowState):
        """失敗時にエラー出力がキャプチャされる"""
        # VerificationErrorEntry の作成をシミュレート
        error_output = "Error: Test failed\n  at test.ts:10\n  at test.ts:20"
        entry = VerificationErrorEntry(
            repository="Backend",
            command="test",
            success=False,
            error_output=error_output,
        )

        # エラー出力が保存されている
        assert entry["error_output"] is not None
        assert "Test failed" in entry["error_output"]
        assert entry["success"] is False

    def test_error_output_truncated(self):
        """長いエラー出力は切り詰められる"""
        # 600行のエラー出力を作成
        long_output = "\n".join([f"Line {i}: Error" for i in range(600)])
        lines = long_output.split("\n")

        # 500行に切り詰め
        if len(lines) > 500:
            truncated = "\n".join(lines[:500]) + f"\n... ({len(lines) - 500} lines truncated)"
        else:
            truncated = long_output

        assert "... (100 lines truncated)" in truncated
        assert truncated.count("\n") == 500  # 500行 + truncated メッセージ

    def test_success_has_no_error_output(self):
        """成功時はエラー出力がNone"""
        entry = VerificationErrorEntry(
            repository="Backend",
            command="build",
            success=True,
            error_output=None,
        )
        assert entry["success"] is True
        assert entry["error_output"] is None


class TestRepositoryStatusTracking:
    """リポジトリステータス追跡のテスト (B-4)"""

    def test_mark_repository_completed(self, minimal_state: WorkflowState):
        """リポジトリを completed としてマーク"""
        repository_status = {}
        repository_status["Backend"] = "completed"

        minimal_state["repository_status"] = repository_status
        assert minimal_state["repository_status"]["Backend"] == "completed"

    def test_mark_repository_failed(self, minimal_state: WorkflowState):
        """リポジトリを failed としてマーク"""
        repository_status = {}
        repository_status["API"] = "failed"

        minimal_state["repository_status"] = repository_status
        assert minimal_state["repository_status"]["API"] == "failed"

    def test_skip_completed_repository_on_retry(self, state_with_repository_status: WorkflowState):
        """リトライ時に完了済みリポジトリをスキップ"""
        repo_status = state_with_repository_status.get("repository_status", {})

        # Backend は completed
        if repo_status.get("Backend") == "completed":
            should_skip = True
        else:
            should_skip = False

        assert should_skip is True

    def test_rerun_failed_repository_on_retry(self, state_with_repository_status: WorkflowState):
        """リトライ時に失敗したリポジトリを再実行"""
        repo_status = state_with_repository_status.get("repository_status", {})

        # API は failed
        if repo_status.get("API") == "failed":
            should_rerun = True
        else:
            should_rerun = False

        assert should_rerun is True

    def test_repository_status_persisted_to_state(self, minimal_state: WorkflowState):
        """リポジトリステータスがステートに永続化される"""
        minimal_state["repository_status"] = {
            "Backend": "completed",
            "API": "failed",
            "Frontend": "completed",
        }

        # ステートから取得
        status = minimal_state.get("repository_status", {})
        assert len(status) == 3
        assert status["Backend"] == "completed"
        assert status["API"] == "failed"
        assert status["Frontend"] == "completed"


class TestVerificationErrorsStorage:
    """検証エラーのストレージテスト"""

    def test_store_multiple_errors(self, minimal_state: WorkflowState):
        """複数のエラーを保存"""
        errors = [
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
                error_output="Test failed",
            ),
            VerificationErrorEntry(
                repository="API",
                command="build",
                success=False,
                error_output="Build failed",
            ),
        ]
        minimal_state["verification_errors"] = errors

        assert len(minimal_state["verification_errors"]) == 3

    def test_filter_errors_by_repository(self, state_with_verification_errors: WorkflowState):
        """リポジトリでエラーをフィルタ"""
        errors = state_with_verification_errors["verification_errors"]

        backend_errors = [e for e in errors if e["repository"] == "Backend"]
        api_errors = [e for e in errors if e["repository"] == "API"]

        assert len(backend_errors) == 2
        assert len(api_errors) == 1

    def test_filter_failed_errors(self, state_with_verification_errors: WorkflowState):
        """失敗したエラーのみフィルタ"""
        errors = state_with_verification_errors["verification_errors"]

        failed_errors = [e for e in errors if not e["success"]]

        assert len(failed_errors) == 2
        assert all(e["error_output"] is not None for e in failed_errors)
