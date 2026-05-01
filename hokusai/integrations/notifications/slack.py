"""Slack 通知クライアント

Slack Incoming Webhook 経由でワークフロー進行イベントを通知する。

設計方針:
- 依存追加なし（標準ライブラリ urllib のみ）
- best effort 送信（失敗してもワークフロー本体を止めない）
- Webhook URL はログ／例外メッセージ／Slack 本文に出さない
- enabled=False または event が events に含まれなければ no-op
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ...config import get_config
from ...config.models import SlackNotificationConfig
from ...logging_config import get_logger

logger = get_logger("integrations.slack")


# イベント別のヘッダ文言（テキスト送信時の 1 行目）
_EVENT_HEADERS: dict[str, str] = {
    "workflow_started": "HOKUSAI: ワークフロー開始",
    "waiting_for_human": "HOKUSAI: 人間の判断待ち",
    "workflow_failed": "HOKUSAI: ワークフローが停止しました",
    "pr_created": "HOKUSAI: PR を作成しました",
    "workflow_completed": "HOKUSAI: ワークフロー完了",
}


def notify_slack(
    event: str,
    state: dict | None,
    *,
    reason: str | None = None,
    error: str | None = None,
    config: SlackNotificationConfig | None = None,
) -> bool:
    """Slack に通知を送る。送信スキップ・失敗いずれもワークフローを止めない。

    Args:
        event: 通知イベント名（SLACK_NOTIFICATION_EVENTS のいずれか）
        state: ワークフロー state（payload 構築に利用）
        reason: 停止理由（waiting_for_human / workflow_failed で使う）
        error: 例外メッセージ等（workflow_failed で使う）
        config: 通知設定。省略時は get_config() から取得

    Returns:
        送信を試みた場合 True（成功・失敗を問わず）。送信スキップなら False。
    """
    try:
        if config is None:
            config = get_config().notifications.slack
    except Exception as e:
        logger.debug(f"Slack 通知設定の取得に失敗（スキップ）: {e}")
        return False

    if not config.enabled:
        return False
    if event not in config.events:
        return False

    webhook_url = os.environ.get(config.webhook_url_env, "").strip()
    if not webhook_url:
        logger.warning(
            "Slack 通知は有効ですが webhook URL の環境変数 "
            f"{config.webhook_url_env} が未設定のため送信をスキップします"
        )
        return False

    payload = build_text_payload(event, state or {}, reason=reason, error=error)
    return _post_webhook(webhook_url, payload, timeout=config.timeout)


def build_text_payload(
    event: str,
    state: dict,
    *,
    reason: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Slack 送信用のテキスト payload を構築する。"""
    header = _EVENT_HEADERS.get(event, f"HOKUSAI: {event}")

    workflow_id = str(state.get("workflow_id", "")) or "unknown"
    task_url = str(state.get("task_url", "")) or ""
    current_phase = state.get("current_phase")

    lines: list[str] = [header, ""]
    lines.append(f"Workflow: {workflow_id}")
    if task_url:
        lines.append(f"Task: {task_url}")
    if current_phase is not None:
        lines.append(f"Phase: {current_phase}")

    if event == "waiting_for_human":
        if reason:
            lines.append(f"Reason: {reason}")
        lines.append("")
        lines.append(f"Next: hokusai continue {workflow_id}")
    elif event == "workflow_failed":
        if reason:
            lines.append(f"Reason: {reason}")
        if error:
            # 長い例外メッセージは 500 文字に丸める（Webhook の本文上限を意識）
            trimmed = error if len(error) <= 500 else error[:497] + "..."
            lines.append(f"Error: {trimmed}")
    elif event == "pr_created":
        prs = state.get("pull_requests") or []
        if prs:
            lines.append("PRs:")
            for pr in prs:
                if not isinstance(pr, dict):
                    continue
                label_parts = []
                repo_name = pr.get("repository") or pr.get("repo")
                if repo_name:
                    label_parts.append(str(repo_name))
                pr_number = pr.get("number")
                if pr_number is not None:
                    label_parts.append(f"PR #{pr_number}")
                label = " ".join(label_parts) if label_parts else "PR"
                pr_url = pr.get("url")
                if pr_url:
                    lines.append(f"- {label}: <{pr_url}|{label}>")
                else:
                    lines.append(f"- {label}")

    text = "\n".join(line for line in lines if line is not None)
    return {"text": text}


def _post_webhook(webhook_url: str, payload: dict, *, timeout: float) -> bool:
    """Webhook URL に payload を POST する。失敗は warn ログのみ。

    Webhook URL を例外メッセージに含めないよう、すべて握り潰してログ化する。
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            if 200 <= status < 300:
                logger.debug(f"Slack 通知送信成功: status={status}")
                return True
            logger.warning(f"Slack 通知が想定外のステータスで返ってきました: status={status}")
            return True
    except urllib.error.HTTPError as e:
        logger.warning(f"Slack 通知送信に失敗（HTTPError）: status={e.code}")
        return True
    except urllib.error.URLError as e:
        # reason に URL が混入する可能性は低いが念のため文字列化のみ
        logger.warning(f"Slack 通知送信に失敗（URLError）: reason={e.reason!s}")
        return True
    except Exception as e:
        logger.warning(f"Slack 通知送信中に予期しない例外: {type(e).__name__}")
        return True
