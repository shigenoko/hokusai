"""
Tests for hokusai/workflow.py - error handling and resume logic
"""

from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from hokusai.state import PhaseStatus


@dataclass
class _FakeCheckpointState:
    """compiled_workflow.get_state() の戻り値を模倣する最小データクラス"""
    values: dict
    next: tuple = ()
    tasks: list = field(default_factory=list)


def _make_runner(*, store=None):
    """WorkflowRunner を最小構成で生成する（グラフコンパイルは不要）。"""
    with patch("hokusai.workflow.get_config") as mock_cfg, \
         patch("hokusai.workflow.SQLiteStore"):
        mock_cfg.return_value = MagicMock(
            database_path=":memory:",
            checkpoint_db_path=":memory:",
        )
        from hokusai.workflow import WorkflowRunner
        runner = WorkflowRunner()

    if store is not None:
        runner.store = store
    else:
        runner.store = MagicMock()

    # compiled_workflow を差し替え
    runner.compiled_workflow = MagicMock()
    return runner


def _make_state(phase=2, phase_status="in_progress"):
    """テスト用のワークフロー状態辞書を返す。"""
    return {
        "current_phase": phase,
        "phases": {
            phase: {
                "status": phase_status,
                "started_at": "2026-01-01T00:00:00",
                "completed_at": None,
                "error_message": None,
                "retry_count": 0,
            },
        },
        "audit_log": [],
        "updated_at": "2026-01-01T00:00:00",
    }


class TestRunStreamLoopErrorHandling:
    """_run_stream_loop のエラーハンドリングテスト"""

    def test_stream_exception_saves_failed_state(self):
        """stream() 例外時に FAILED 状態が DB に保存される"""
        store = MagicMock()
        runner = _make_runner(store=store)

        state = _make_state(phase=2)
        runner.compiled_workflow.stream.side_effect = RuntimeError("skill timeout")
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(values=dict(state))

        config = {"configurable": {"thread_id": "wf-test"}}
        with pytest.raises(RuntimeError, match="skill timeout"):
            runner._run_stream_loop(state, config, "wf-test")

        # save_workflow が例外時に呼ばれたことを検証
        store.save_workflow.assert_called_once()
        saved_state = store.save_workflow.call_args[0][1]
        assert saved_state["phases"][2]["status"] == PhaseStatus.FAILED.value
        assert saved_state["phases"][2]["error_message"] == "skill timeout"
        # 監査ログが追加されていること
        assert len(saved_state["audit_log"]) == 1
        assert saved_state["audit_log"][0]["action"] == "stream_execution_failed"

    def test_stream_exception_reraises(self):
        """元の例外が再送出される"""
        runner = _make_runner()

        state = _make_state(phase=2)
        runner.compiled_workflow.stream.side_effect = RuntimeError("original error")
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(values=dict(state))

        config = {"configurable": {"thread_id": "wf-test"}}
        with pytest.raises(RuntimeError, match="original error"):
            runner._run_stream_loop(state, config, "wf-test")

    def test_save_workflow_failure_does_not_mask_original_error(self):
        """DB保存失敗時も元の例外が再送出される"""
        store = MagicMock()
        store.save_workflow.side_effect = OSError("disk full")
        runner = _make_runner(store=store)

        state = _make_state(phase=3)
        runner.compiled_workflow.stream.side_effect = RuntimeError("original error")
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(values=dict(state))

        config = {"configurable": {"thread_id": "wf-test"}}
        # 元の RuntimeError が発生すること（OSError ではない）
        with pytest.raises(RuntimeError, match="original error"):
            runner._run_stream_loop(state, config, "wf-test")

    def test_stream_exception_with_no_state_skips_save(self):
        """get_state が None を返す場合、保存をスキップして例外を再送出する"""
        store = MagicMock()
        runner = _make_runner(store=store)

        state = _make_state(phase=2)
        runner.compiled_workflow.stream.side_effect = RuntimeError("error")
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(values=None)

        config = {"configurable": {"thread_id": "wf-test"}}
        with pytest.raises(RuntimeError, match="error"):
            runner._run_stream_loop(state, config, "wf-test")

        # state が None なので save_workflow は呼ばれない
        store.save_workflow.assert_not_called()


class TestCheckpointConsistency:
    """チェックポイント整合チェックのテスト"""

    def test_consistent_checkpoint_returns_true(self):
        """next が pending フェーズを指す場合は整合"""
        runner = _make_runner()
        state = {
            "phases": {
                1: {"status": "completed"},
                2: {"status": "pending"},
            }
        }
        cp = _FakeCheckpointState(values={}, next=("phase2_research",))
        assert runner._checkpoint_consistent_with_state(cp, state) is True

    def test_inconsistent_checkpoint_returns_false(self):
        """next が completed 済みフェーズを指す場合は不整合"""
        runner = _make_runner()
        state = {
            "phases": {
                1: {"status": "completed"},
                2: {"status": "pending"},
            }
        }
        cp = _FakeCheckpointState(values={}, next=("phase1_prepare",))
        assert runner._checkpoint_consistent_with_state(cp, state) is False

    def test_empty_next_is_consistent(self):
        """next が空の場合は整合とみなす"""
        runner = _make_runner()
        state = {"phases": {1: {"status": "completed"}}}
        cp = _FakeCheckpointState(values={}, next=())
        assert runner._checkpoint_consistent_with_state(cp, state) is True

    def test_unknown_node_is_consistent(self):
        """未知のノード名は整合とみなす"""
        runner = _make_runner()
        state = {"phases": {1: {"status": "completed"}}}
        cp = _FakeCheckpointState(values={}, next=("unknown_node",))
        assert runner._checkpoint_consistent_with_state(cp, state) is True


class TestContinueWorkflowResume:
    """continue_workflow のチェックポイント/state 再開判定テスト"""

    def test_continue_preserves_notion_connected_true(self, monkeypatch):
        """Notion リトライで True に復元済みなら SKIP_NOTION=1 でも上書きしない"""
        runner = _make_runner()
        runner.store.load_workflow.return_value = {
            "current_phase": 1,
            "waiting_for_human": False,
            "notion_connected": True,
            "phases": {
                1: {"status": "pending"},
            },
        }
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(values={})
        runner._run_stream_loop = MagicMock()
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        runner.continue_workflow("wf-test")

        runner.store.save_workflow.assert_called()
        saved_state = runner.store.save_workflow.call_args[0][1]
        assert saved_state["notion_connected"] is True

    def test_continue_updates_notion_connected_when_not_true(self, monkeypatch):
        """notion_connected が None/False の場合は環境値で更新する"""
        runner = _make_runner()
        runner.store.load_workflow.return_value = {
            "current_phase": 1,
            "waiting_for_human": False,
            "notion_connected": None,
            "phases": {
                1: {"status": "pending"},
            },
        }
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(values={})
        runner._run_stream_loop = MagicMock()
        monkeypatch.setenv("HOKUSAI_SKIP_NOTION", "1")

        runner.continue_workflow("wf-test")

        runner.store.save_workflow.assert_called()
        saved_state = runner.store.save_workflow.call_args[0][1]
        assert saved_state["notion_connected"] is False

    def test_inconsistent_checkpoint_falls_back_to_state_resume(self):
        """チェックポイント不整合時は state ベースで再開する"""
        runner = _make_runner()
        runner.store.load_workflow.return_value = {
            "current_phase": 2,
            "waiting_for_human": False,
            "phases": {
                1: {"status": "completed"},
                2: {"status": "pending"},
            },
        }
        # チェックポイントは存在するが next が completed 済みの Phase 1 を指す
        cp = _FakeCheckpointState(
            values={"current_phase": 2},
            next=("phase1_prepare",),
        )
        runner.compiled_workflow.get_state.return_value = cp

        # _run_stream_loop をモック化して呼び出し引数を検証
        runner._run_stream_loop = MagicMock()

        runner.continue_workflow("wf-test")

        # state ベース再開: update_state が as_node 付きで呼ばれる
        runner.compiled_workflow.update_state.assert_called()
        call_kwargs = runner.compiled_workflow.update_state.call_args
        assert call_kwargs[1].get("as_node"), "as_node keyword arg が指定されていません"

    def test_consistent_checkpoint_resumes_from_checkpoint(self):
        """チェックポイント整合時はチェックポイントから再開する"""
        runner = _make_runner()
        runner.store.load_workflow.return_value = {
            "current_phase": 2,
            "waiting_for_human": False,
            "phases": {
                1: {"status": "completed"},
                2: {"status": "in_progress"},
            },
        }
        # next が in_progress の Phase 2 を指す（整合）
        cp = _FakeCheckpointState(
            values={"current_phase": 2},
            next=("phase2_research",),
        )
        runner.compiled_workflow.get_state.return_value = cp
        runner._run_stream_loop = MagicMock()

        runner.continue_workflow("wf-test")

        # チェックポイント再開: state=None, resume_from_checkpoint=True で呼ばれる
        call_args = runner._run_stream_loop.call_args
        assert call_args[0][0] is None  # state=None
        assert call_args[1].get("resume_from_checkpoint") is True

    def test_checkpoint_resume_syncs_full_state(self):
        """チェックポイント再開時に SQLite の state 全体がチェックポイントに反映される。

        ダッシュボードが cross_review_blocked を解消して Phase 2 を completed に
        修復した場合、チェックポイント再開でもその修復が反映されなければならない。
        """
        runner = _make_runner()
        # SQLite: ダッシュボード自動修復済み（Phase 2 = completed, current_phase = 3）
        sqlite_state = {
            "current_phase": 3,
            "waiting_for_human": False,
            "phases": {
                1: {"status": "completed"},
                2: {"status": "completed", "error_message": None},
                3: {"status": "pending"},
            },
            "audit_log": [],
        }
        runner.store.load_workflow.return_value = sqlite_state
        # チェックポイント: Phase 2 = failed のまま残っている
        cp = _FakeCheckpointState(
            values={
                "current_phase": 2,
                "waiting_for_human": True,
                "phases": {
                    1: {"status": "completed"},
                    2: {"status": "failed", "error_message": "cross_review_blocked"},
                    3: {"status": "pending"},
                },
            },
            next=("phase3_design",),
        )
        runner.compiled_workflow.get_state.return_value = cp
        runner._run_stream_loop = MagicMock()

        runner.continue_workflow("wf-test")

        # update_state に SQLite の state 全体が渡されること
        runner.compiled_workflow.update_state.assert_called_once()
        update_call = runner.compiled_workflow.update_state.call_args
        updated_state = update_call[0][1]
        # Phase 2 が completed に修復されていること
        assert updated_state["phases"][2]["status"] == "completed"
        assert updated_state["phases"][2]["error_message"] is None
        # current_phase が 3 に進んでいること
        assert updated_state["current_phase"] == 3
        # waiting_for_human がクリアされていること
        assert updated_state["waiting_for_human"] is False


class TestUpdatePhaseStatusCurrentPhase:
    """update_phase_status の current_phase 更新テスト"""

    def test_completed_advances_current_phase(self):
        """COMPLETED 時に current_phase が次フェーズへ進む"""
        from hokusai.state import update_phase_status

        state = {
            "current_phase": 1,
            "phases": {
                1: {
                    "status": "in_progress",
                    "started_at": "2026-01-01T00:00:00",
                    "completed_at": None,
                    "error_message": None,
                    "retry_count": 0,
                },
            },
            "updated_at": "2026-01-01T00:00:00",
        }
        result = update_phase_status(state, 1, PhaseStatus.COMPLETED)
        assert result["current_phase"] == 2

    def test_in_progress_sets_current_phase(self):
        """IN_PROGRESS 時に current_phase が当該フェーズに設定される"""
        from hokusai.state import update_phase_status

        state = {
            "current_phase": 1,
            "phases": {
                2: {
                    "status": "pending",
                    "started_at": None,
                    "completed_at": None,
                    "error_message": None,
                    "retry_count": 0,
                },
            },
            "updated_at": "2026-01-01T00:00:00",
        }
        result = update_phase_status(state, 2, PhaseStatus.IN_PROGRESS)
        assert result["current_phase"] == 2

    def test_failed_does_not_advance(self):
        """FAILED 時は current_phase が進まない"""
        from hokusai.state import update_phase_status

        state = {
            "current_phase": 2,
            "phases": {
                2: {
                    "status": "in_progress",
                    "started_at": "2026-01-01T00:00:00",
                    "completed_at": None,
                    "error_message": None,
                    "retry_count": 0,
                },
            },
            "updated_at": "2026-01-01T00:00:00",
        }
        result = update_phase_status(state, 2, PhaseStatus.FAILED, "error")
        assert result["current_phase"] == 2


class TestStepModeStopCondition:
    """ステップモード停止条件のテスト（ノード名ベース）"""

    def test_stops_after_phase_completed(self):
        """フェーズ完了時にステップ停止が発火する"""
        runner = _make_runner()
        runner.step_mode = True

        # Phase 1 完了後の state（current_phase=2 に進んでいる）
        post_phase1_state = {
            "current_phase": 2,
            "phases": {
                1: {"status": "completed", "started_at": "t", "completed_at": "t",
                    "error_message": None, "retry_count": 0},
                2: {"status": "pending", "started_at": None, "completed_at": None,
                    "error_message": None, "retry_count": 0},
            },
            "audit_log": [],
            "updated_at": "t",
        }

        # stream が phase1_prepare イベントを1つ返す
        runner.compiled_workflow.stream.return_value = iter([
            {"phase1_prepare": {"current_phase": 2}},
        ])
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(
            values=post_phase1_state,
        )

        # _prompt_step_confirmation → False（非対話 = 1フェーズで停止）
        runner._prompt_step_confirmation = MagicMock(return_value=False)

        result = runner._run_stream_loop(
            post_phase1_state, {"configurable": {"thread_id": "wf-step"}}, "wf-step",
        )

        # 停止が発火していること
        assert result.interrupted is True
        assert result.interrupt_reason == "user_aborted"
        # _prompt_step_confirmation に completed した Phase 1 が渡される
        runner._prompt_step_confirmation.assert_called_once()
        assert runner._prompt_step_confirmation.call_args[0][0] == 1

    def test_does_not_stop_on_pending_phase(self):
        """pending フェーズでは停止しない"""
        runner = _make_runner()
        runner.step_mode = True

        # Phase 2 が in_progress の state
        in_progress_state = {
            "current_phase": 2,
            "phases": {
                2: {"status": "in_progress", "started_at": "t", "completed_at": None,
                    "error_message": None, "retry_count": 0},
            },
            "audit_log": [],
            "updated_at": "t",
        }

        runner.compiled_workflow.stream.return_value = iter([
            {"phase2_research": {"current_phase": 2}},
        ])
        runner.compiled_workflow.get_state.return_value = _FakeCheckpointState(
            values=in_progress_state,
        )

        runner._prompt_step_confirmation = MagicMock(return_value=False)

        result = runner._run_stream_loop(
            in_progress_state, {"configurable": {"thread_id": "wf-step2"}}, "wf-step2",
        )

        # in_progress なので停止判定は発火しない
        runner._prompt_step_confirmation.assert_not_called()
        assert result.interrupted is False
