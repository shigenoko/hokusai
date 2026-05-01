"""通知系インテグレーション

Slack 等の外部通知サービスへ送信するモジュール群。
"""

from .slack import notify_slack

__all__ = ["notify_slack"]
