"""Slack 通知機能のテスト

対象:
- 設定パース（_parse_notifications_config）
- payload builder（build_text_payload）
- notify_slack の送信スキップ条件
- 送信失敗が外部に漏れないこと
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from hokusai.config.loaders import _parse_notifications_config
from hokusai.config.models import (
    NotificationConfig,
    SlackNotificationConfig,
)
from hokusai.integrations.notifications import slack as slack_module
from hokusai.integrations.notifications.slack import (
    build_text_payload,
    notify_slack,
)


# ---------------------------------------------------------------------------
# _parse_notifications_config
# ---------------------------------------------------------------------------


def test_parse_notifications_returns_default_when_missing():
    cfg = _parse_notifications_config({})
    assert isinstance(cfg, NotificationConfig)
    assert cfg.slack.enabled is False
    assert cfg.slack.webhook_url_env == "HOKUSAI_SLACK_WEBHOOK_URL"
    assert "waiting_for_human" in cfg.slack.events


def test_parse_notifications_returns_default_when_not_dict():
    cfg = _parse_notifications_config({"notifications": "yes"})
    assert cfg.slack.enabled is False


def test_parse_notifications_returns_default_when_slack_not_dict():
    cfg = _parse_notifications_config({"notifications": {"slack": "on"}})
    assert cfg.slack.enabled is False


def test_parse_notifications_accepts_full_config():
    cfg = _parse_notifications_config({
        "notifications": {
            "slack": {
                "enabled": True,
                "webhook_url_env": "MY_SLACK_HOOK",
                "events": ["pr_created", "workflow_failed"],
                "timeout": 12.5,
            }
        }
    })
    assert cfg.slack.enabled is True
    assert cfg.slack.webhook_url_env == "MY_SLACK_HOOK"
    assert cfg.slack.events == ["pr_created", "workflow_failed"]
    assert cfg.slack.timeout == 12.5


def test_parse_notifications_drops_unknown_events():
    cfg = _parse_notifications_config({
        "notifications": {
            "slack": {
                "enabled": True,
                "events": ["pr_created", "bogus_event", 123, "workflow_failed"],
            }
        }
    })
    assert cfg.slack.events == ["pr_created", "workflow_failed"]


def test_parse_notifications_falls_back_to_default_when_all_events_invalid():
    cfg = _parse_notifications_config({
        "notifications": {
            "slack": {"enabled": True, "events": ["bogus", 1, None]}
        }
    })
    # 既知イベントが残らなければデフォルトに戻す
    assert cfg.slack.events == SlackNotificationConfig().events


def test_parse_notifications_clamps_timeout():
    cfg_low = _parse_notifications_config({
        "notifications": {"slack": {"timeout": 0.1}}
    })
    cfg_high = _parse_notifications_config({
        "notifications": {"slack": {"timeout": 999}}
    })
    cfg_str = _parse_notifications_config({
        "notifications": {"slack": {"timeout": "abc"}}
    })
    assert cfg_low.slack.timeout == 1.0
    assert cfg_high.slack.timeout == 30.0
    # 不正値はデフォルトへ
    assert cfg_str.slack.timeout == SlackNotificationConfig().timeout


def test_parse_notifications_rejects_non_string_webhook_env():
    cfg = _parse_notifications_config({
        "notifications": {"slack": {"webhook_url_env": ""}}
    })
    assert cfg.slack.webhook_url_env == "HOKUSAI_SLACK_WEBHOOK_URL"


def test_parse_notifications_ignores_non_bool_enabled():
    cfg = _parse_notifications_config({
        "notifications": {"slack": {"enabled": "yes"}}
    })
    assert cfg.slack.enabled is False


# ---------------------------------------------------------------------------
# build_text_payload
# ---------------------------------------------------------------------------


def _state() -> dict[str, Any]:
    return {
        "workflow_id": "wf-1234abcd",
        "task_url": "https://example.com/task/1",
        "current_phase": 7,
        "pull_requests": [
            {"repository": "Backend", "number": 123, "url": "https://github.com/x/backend/pull/123"},
            {"repository": "Frontend", "number": 45, "url": "https://github.com/x/frontend/pull/45"},
        ],
    }


def test_payload_workflow_started_includes_workflow_and_task():
    payload = build_text_payload("workflow_started", _state())
    assert "ワークフロー開始" in payload["text"]
    assert "wf-1234abcd" in payload["text"]
    assert "https://example.com/task/1" in payload["text"]


def test_payload_waiting_for_human_includes_reason_and_next_action():
    payload = build_text_payload(
        "waiting_for_human", _state(), reason="branch_hygiene"
    )
    assert "人間の判断待ち" in payload["text"]
    assert "Reason: branch_hygiene" in payload["text"]
    assert "hokusai continue wf-1234abcd" in payload["text"]


def test_payload_pr_created_lists_all_prs():
    payload = build_text_payload("pr_created", _state())
    text = payload["text"]
    assert "PR を作成しました" in text
    assert "Backend PR #123" in text
    assert "Frontend PR #45" in text
    assert "https://github.com/x/backend/pull/123" in text


def test_payload_workflow_failed_includes_error_and_reason():
    payload = build_text_payload(
        "workflow_failed", _state(), reason="loop_detected", error="boom"
    )
    text = payload["text"]
    assert "停止しました" in text
    assert "Reason: loop_detected" in text
    assert "Error: boom" in text


def test_payload_workflow_failed_truncates_long_error():
    long_err = "x" * 1000
    payload = build_text_payload(
        "workflow_failed", _state(), reason="exception", error=long_err
    )
    # 500 文字（+省略記号）に丸められる
    assert "..." in payload["text"]
    # 元の 1000 文字がそのまま入っていない
    assert "x" * 1000 not in payload["text"]


def test_payload_workflow_completed_minimal_fields():
    payload = build_text_payload("workflow_completed", _state())
    assert "ワークフロー完了" in payload["text"]
    assert "wf-1234abcd" in payload["text"]


def test_payload_handles_empty_state():
    payload = build_text_payload("workflow_started", {})
    assert "Workflow: unknown" in payload["text"]


# ---------------------------------------------------------------------------
# notify_slack: send-skip conditions
# ---------------------------------------------------------------------------


def _disabled_config() -> SlackNotificationConfig:
    return SlackNotificationConfig(enabled=False)


def _enabled_config(events: list[str] | None = None) -> SlackNotificationConfig:
    return SlackNotificationConfig(
        enabled=True,
        webhook_url_env="TEST_SLACK_HOOK",
        events=events or list(SlackNotificationConfig().events),
        timeout=2.0,
    )


def test_notify_slack_skipped_when_disabled(monkeypatch):
    called: list[Any] = []
    monkeypatch.setattr(slack_module, "_post_webhook", lambda *a, **k: called.append(a) or True)
    result = notify_slack("pr_created", _state(), config=_disabled_config())
    assert result is False
    assert called == []


def test_notify_slack_skipped_when_event_not_in_list(monkeypatch):
    called: list[Any] = []
    monkeypatch.setattr(slack_module, "_post_webhook", lambda *a, **k: called.append(a) or True)
    cfg = _enabled_config(events=["workflow_failed"])
    result = notify_slack("pr_created", _state(), config=cfg)
    assert result is False
    assert called == []


def test_notify_slack_skipped_when_env_var_unset(monkeypatch):
    called: list[Any] = []
    monkeypatch.delenv("TEST_SLACK_HOOK", raising=False)
    monkeypatch.setattr(slack_module, "_post_webhook", lambda *a, **k: called.append(a) or True)
    result = notify_slack("pr_created", _state(), config=_enabled_config())
    assert result is False
    assert called == []


def test_notify_slack_sends_when_all_conditions_met(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_post(url: str, payload: dict, *, timeout: float) -> bool:
        captured["url"] = url
        captured["payload"] = payload
        captured["timeout"] = timeout
        return True

    monkeypatch.setenv("TEST_SLACK_HOOK", "https://hooks.slack.com/services/AAA/BBB/cccDDDeee")
    monkeypatch.setattr(slack_module, "_post_webhook", fake_post)
    result = notify_slack("pr_created", _state(), config=_enabled_config())
    assert result is True
    assert captured["url"].startswith("https://hooks.slack.com/")
    assert "wf-1234abcd" in captured["payload"]["text"]
    assert captured["timeout"] == 2.0


def test_notify_slack_payload_does_not_leak_webhook_url(monkeypatch):
    """Webhook URL が payload 本文に混入していないこと"""
    captured: dict[str, Any] = {}

    def fake_post(url: str, payload: dict, *, timeout: float) -> bool:
        captured["url"] = url
        captured["payload"] = payload
        return True

    monkeypatch.setenv("TEST_SLACK_HOOK", "https://hooks.slack.com/services/T0/B0/secret123")
    monkeypatch.setattr(slack_module, "_post_webhook", fake_post)
    notify_slack("workflow_failed", _state(), reason="boom", config=_enabled_config())
    assert "secret123" not in captured["payload"]["text"]


def test_notify_slack_skips_when_env_value_is_whitespace(monkeypatch):
    called: list[Any] = []
    monkeypatch.setenv("TEST_SLACK_HOOK", "   ")
    monkeypatch.setattr(slack_module, "_post_webhook", lambda *a, **k: called.append(a) or True)
    result = notify_slack("pr_created", _state(), config=_enabled_config())
    assert result is False
    assert called == []


# ---------------------------------------------------------------------------
# notify_slack: failure handling
# ---------------------------------------------------------------------------


def test_notify_slack_swallows_http_error(monkeypatch):
    def raise_http(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url, code=500, msg="boom",
            hdrs=None, fp=None,
        )

    monkeypatch.setenv("TEST_SLACK_HOOK", "https://hooks.slack.com/services/T0/B0/abcdef")
    monkeypatch.setattr(slack_module.urllib.request, "urlopen", raise_http)
    # 例外が漏れずに True が返ること（送信を試みた）
    assert notify_slack("workflow_failed", _state(), reason="x", config=_enabled_config()) is True


def test_notify_slack_swallows_url_error(monkeypatch):
    def raise_url(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setenv("TEST_SLACK_HOOK", "https://hooks.slack.com/services/T0/B0/abcdef")
    monkeypatch.setattr(slack_module.urllib.request, "urlopen", raise_url)
    assert notify_slack("workflow_failed", _state(), config=_enabled_config()) is True


def test_notify_slack_swallows_unexpected_exception(monkeypatch):
    def raise_unexpected(req, timeout=None):
        raise RuntimeError("unexpected")

    monkeypatch.setenv("TEST_SLACK_HOOK", "https://hooks.slack.com/services/T0/B0/abcdef")
    monkeypatch.setattr(slack_module.urllib.request, "urlopen", raise_unexpected)
    assert notify_slack("pr_created", _state(), config=_enabled_config()) is True


def test_notify_slack_returns_true_on_2xx(monkeypatch):
    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def getcode(self):
            return 200

    monkeypatch.setenv("TEST_SLACK_HOOK", "https://hooks.slack.com/services/T0/B0/abcdef")
    monkeypatch.setattr(
        slack_module.urllib.request, "urlopen", lambda req, timeout=None: FakeResponse()
    )
    assert notify_slack("workflow_completed", _state(), config=_enabled_config()) is True


# ---------------------------------------------------------------------------
# WorkflowRunner の終了通知ヘルパ（_emit_terminal_notification / _safe_notify）
# ---------------------------------------------------------------------------


from hokusai import workflow as workflow_module  # noqa: E402
from hokusai.state import PhaseStatus  # noqa: E402


def _phases_with(phase_num: int, status: str) -> dict:
    phases = {i: {"status": PhaseStatus.PENDING.value} for i in range(1, 11)}
    phases[phase_num]["status"] = status
    return phases


@pytest.fixture
def captured_notifications(monkeypatch):
    """`notify_slack` を差し替えて呼び出し履歴を記録する"""
    calls: list[dict[str, Any]] = []

    def fake_notify(event, state, *, reason=None, error=None, config=None):
        calls.append(
            {"event": event, "state": state, "reason": reason, "error": error}
        )
        return True

    monkeypatch.setattr(workflow_module, "notify_slack", fake_notify)
    return calls


def test_emit_terminal_notification_waiting_for_human(captured_notifications):
    workflow_module._emit_terminal_notification(
        interrupt_reason="waiting_for_human",
        final_values={"workflow_id": "wf-1", "human_input_request": "branch_hygiene"},
    )
    assert len(captured_notifications) == 1
    assert captured_notifications[0]["event"] == "waiting_for_human"
    assert captured_notifications[0]["reason"] == "branch_hygiene"


def test_emit_terminal_notification_loop_detected(captured_notifications):
    workflow_module._emit_terminal_notification(
        interrupt_reason="loop_detected",
        final_values={"workflow_id": "wf-2"},
    )
    assert len(captured_notifications) == 1
    assert captured_notifications[0]["event"] == "workflow_failed"
    assert captured_notifications[0]["reason"] == "loop_detected"


def test_emit_terminal_notification_max_events(captured_notifications):
    workflow_module._emit_terminal_notification(
        interrupt_reason="max_events",
        final_values={"workflow_id": "wf-3"},
    )
    assert captured_notifications[-1]["event"] == "workflow_failed"
    assert captured_notifications[-1]["reason"] == "max_events"


def test_emit_terminal_notification_user_aborted_does_not_notify(captured_notifications):
    workflow_module._emit_terminal_notification(
        interrupt_reason="user_aborted",
        final_values={"workflow_id": "wf-4"},
    )
    assert captured_notifications == []


def test_emit_terminal_notification_completed_when_phase10_done(captured_notifications):
    workflow_module._emit_terminal_notification(
        interrupt_reason=None,
        final_values={
            "workflow_id": "wf-5",
            "phases": _phases_with(10, PhaseStatus.COMPLETED.value),
        },
    )
    assert len(captured_notifications) == 1
    assert captured_notifications[0]["event"] == "workflow_completed"


def test_emit_terminal_notification_no_send_when_phase10_not_done(captured_notifications):
    workflow_module._emit_terminal_notification(
        interrupt_reason=None,
        final_values={
            "workflow_id": "wf-6",
            "phases": _phases_with(10, PhaseStatus.PENDING.value),
        },
    )
    assert captured_notifications == []


def test_safe_notify_swallows_exceptions(monkeypatch):
    """notify_slack が例外を投げてもワークフローには伝播しない"""
    def raising(event, state, *, reason=None, error=None, config=None):
        raise RuntimeError("explode")

    monkeypatch.setattr(workflow_module, "notify_slack", raising)
    # 例外が外に出なければOK
    workflow_module._safe_notify("workflow_started", {"workflow_id": "x"})
