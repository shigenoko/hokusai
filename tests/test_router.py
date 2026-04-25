"""
ルーター: fail-close テスト

C1: リトライ上限到達時にワークフローを停止（ENDへルーティング）
"""

import pytest
from unittest.mock import patch

from hokusai.nodes.router import (
    should_retry_implementation,
    should_retry_review,
)
from hokusai.state import VerificationResult


@pytest.fixture
def mock_config():
    """max_retry_count=3 のモック設定"""
    with patch("hokusai.nodes.router.get_config") as mock:
        mock.return_value.max_retry_count = 3
        yield mock.return_value


class TestShouldRetryImplementation:
    """Phase 6 ルーター: fail-close テスト"""

    def test_success_goes_to_review(self, minimal_state, mock_config):
        """検証成功 → phase7_review"""
        minimal_state["verification"] = {
            "build": VerificationResult.PASS.value,
            "test": VerificationResult.PASS.value,
            "lint": VerificationResult.PASS.value,
        }
        assert should_retry_implementation(minimal_state) == "phase7_review"

    def test_failure_retries(self, minimal_state, mock_config):
        """検証失敗 + リトライ余裕あり → phase5_implement"""
        minimal_state["verification"]["build"] = VerificationResult.FAIL.value
        minimal_state["phases"][6]["retry_count"] = 1
        assert should_retry_implementation(minimal_state) == "phase5_implement"

    def test_max_retry_ends_workflow(self, minimal_state, mock_config):
        """C1: リトライ上限到達 → end（fail-close）"""
        minimal_state["verification"]["build"] = VerificationResult.FAIL.value
        minimal_state["phases"][6]["retry_count"] = 3
        assert should_retry_implementation(minimal_state) == "end"

    def test_waiting_for_human_ends_workflow(self, minimal_state, mock_config):
        """C1: waiting_for_human が True → end"""
        minimal_state["waiting_for_human"] = True
        minimal_state["verification"]["build"] = VerificationResult.FAIL.value
        assert should_retry_implementation(minimal_state) == "end"


class TestShouldRetryReview:
    """Phase 7 ルーター: fail-close テスト"""

    def test_pass_goes_to_hygiene(self, minimal_state, mock_config):
        """レビュー合格 → phase7_5_hygiene"""
        minimal_state["final_review_passed"] = True
        assert should_retry_review(minimal_state) == "phase7_5_hygiene"

    def test_failure_retries(self, minimal_state, mock_config):
        """レビュー不合格 + リトライ余裕あり → phase5_implement"""
        minimal_state["final_review_passed"] = False
        minimal_state["phases"][7]["retry_count"] = 1
        assert should_retry_review(minimal_state) == "phase5_implement"

    def test_max_retry_ends_workflow(self, minimal_state, mock_config):
        """C1: リトライ上限到達 → end（fail-close）"""
        minimal_state["final_review_passed"] = False
        minimal_state["phases"][7]["retry_count"] = 3
        assert should_retry_review(minimal_state) == "end"

    def test_waiting_for_human_ends_workflow(self, minimal_state, mock_config):
        """C1: waiting_for_human が True → end"""
        minimal_state["waiting_for_human"] = True
        minimal_state["final_review_passed"] = False
        assert should_retry_review(minimal_state) == "end"
