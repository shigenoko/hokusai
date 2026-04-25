"""
Phase 7.5: ブランチ衛生チェックのテスト

テスト対象:
- handle_hygiene_action: rebase アクションの動作
- _rebase_onto_base: 成功 / コンフリクト / フェッチ失敗
"""

from unittest.mock import patch, MagicMock

from hokusai.nodes.phase7_5_hygiene import handle_hygiene_action, _rebase_onto_base
from hokusai.state import add_audit_log


class TestRebaseAction:
    """rebase アクションのテスト"""

    @patch("hokusai.nodes.phase7_5_hygiene.get_config")
    @patch("hokusai.nodes.phase7_5_hygiene.GitClient")
    def test_rebase_success(self, MockGitClient, mock_config, minimal_state):
        """rebase 成功時に waiting_for_human が False になる"""
        mock_config.return_value.base_branch = "beta"
        mock_git = MockGitClient.return_value
        mock_git.run_git_command.return_value = (True, "")

        minimal_state["waiting_for_human"] = True
        minimal_state["human_input_request"] = "branch_hygiene"

        result = handle_hygiene_action(minimal_state, "rebase")

        assert result["waiting_for_human"] is False
        assert result["human_input_request"] is None
        # fetch + rebase の 2 回呼ばれる
        calls = mock_git.run_git_command.call_args_list
        assert ["fetch", "origin", "beta"] in [c[0][0] for c in calls]
        assert ["rebase", "origin/beta"] in [c[0][0] for c in calls]

    @patch("hokusai.nodes.phase7_5_hygiene.get_config")
    @patch("hokusai.nodes.phase7_5_hygiene.GitClient")
    def test_rebase_conflict_aborts_and_waits(self, MockGitClient, mock_config, minimal_state):
        """rebase コンフリクト時に abort して Human-in-the-loop に戻す"""
        mock_config.return_value.base_branch = "beta"
        mock_git = MockGitClient.return_value

        def side_effect(cmd):
            if cmd == ["fetch", "origin", "beta"]:
                return (True, "")
            if cmd == ["rebase", "origin/beta"]:
                return (False, "CONFLICT (content): Merge conflict in file.ts")
            if cmd == ["rebase", "--abort"]:
                return (True, "")
            return (True, "")

        mock_git.run_git_command.side_effect = side_effect
        minimal_state["waiting_for_human"] = True

        result = handle_hygiene_action(minimal_state, "rebase")

        # コンフリクト時は waiting_for_human のまま
        assert result["waiting_for_human"] is True
        assert result["human_input_request"] == "branch_hygiene"
        # rebase --abort が呼ばれたことを確認
        abort_calls = [
            c for c in mock_git.run_git_command.call_args_list
            if c[0][0] == ["rebase", "--abort"]
        ]
        assert len(abort_calls) == 1

    @patch("hokusai.nodes.phase7_5_hygiene.get_config")
    @patch("hokusai.nodes.phase7_5_hygiene.GitClient")
    def test_rebase_fetch_failure(self, MockGitClient, mock_config, minimal_state):
        """fetch 失敗時は rebase を実行しない"""
        mock_config.return_value.base_branch = "beta"
        mock_git = MockGitClient.return_value
        mock_git.run_git_command.return_value = (False, "fatal: unable to access")

        result = handle_hygiene_action(minimal_state, "rebase")

        # rebase は呼ばれない（fetch のみ）
        calls = [c[0][0] for c in mock_git.run_git_command.call_args_list]
        assert ["fetch", "origin", "beta"] in calls
        assert ["rebase", "origin/beta"] not in calls

    @patch("hokusai.nodes.phase7_5_hygiene.get_config")
    @patch("hokusai.nodes.phase7_5_hygiene.GitClient")
    def test_rebase_success_audit_log(self, MockGitClient, mock_config, minimal_state):
        """rebase 成功時に監査ログが記録される"""
        mock_config.return_value.base_branch = "beta"
        mock_git = MockGitClient.return_value
        mock_git.run_git_command.return_value = (True, "")

        result = handle_hygiene_action(minimal_state, "rebase")

        rebase_logs = [
            log for log in result.get("audit_log", [])
            if log.get("action") == "rebase_completed"
        ]
        assert len(rebase_logs) == 1
        assert rebase_logs[0]["result"] == "success"

    @patch("hokusai.nodes.phase7_5_hygiene.get_config")
    @patch("hokusai.nodes.phase7_5_hygiene.GitClient")
    def test_rebase_conflict_audit_log(self, MockGitClient, mock_config, minimal_state):
        """rebase コンフリクト時に警告監査ログが記録される"""
        mock_config.return_value.base_branch = "beta"
        mock_git = MockGitClient.return_value

        def side_effect(cmd):
            if cmd == ["rebase", "origin/beta"]:
                return (False, "CONFLICT")
            return (True, "")

        mock_git.run_git_command.side_effect = side_effect

        result = handle_hygiene_action(minimal_state, "rebase")

        conflict_logs = [
            log for log in result.get("audit_log", [])
            if log.get("action") == "rebase_conflict"
        ]
        assert len(conflict_logs) == 1
        assert conflict_logs[0]["result"] == "warning"
