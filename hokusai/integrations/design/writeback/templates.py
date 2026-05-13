"""Figma / Miro 投稿本文のテンプレート。

計画書 §6 に従い、Phase 8a 完了時のコメント / カード payload を構築する。

v0.4.0 スコープ:
- 単一行・日本語のみ・ハードコード
- i18n / 設定化は v0.4.1 以降
"""

from __future__ import annotations

from typing import Any


def _shorten_commit(commit_sha: str | None) -> str:
    if not commit_sha:
        return "(commit 不明)"
    return commit_sha[:7]


def render_figma_message(
    *,
    mr_url: str | None,
    commit_sha: str | None,
) -> str:
    """Figma frame コメントの本文を組み立てる（計画書 §6.1.1）"""
    return (
        f"✅ Phase 8a 完了 / MR: {mr_url or '(MR URL 不明)'}"
        f" / commit: {_shorten_commit(commit_sha)}"
    )


def build_figma_payload(
    *,
    node_id: str,
    node_offset: dict[str, float] | None,
    mr_url: str | None,
    commit_sha: str | None,
) -> dict[str, Any]:
    """Figma REST API `POST /v1/files/{file_key}/comments` の body を構築（§6.1.2）"""
    return {
        "message": render_figma_message(mr_url=mr_url, commit_sha=commit_sha),
        "client_meta": {
            "node_id": node_id,
            "node_offset": node_offset or {"x": 0, "y": 0},
        },
    }


def build_miro_card_payload(
    *,
    frame_meta: dict[str, Any],
    mr_url: str | None,
    commit_sha: str | None,
) -> dict[str, Any]:
    """Miro REST API `POST /v2/boards/{board_id}/cards` の body を構築（§6.2）。

    配置位置: 主 frame の右側 50px。

    Args:
        frame_meta: design_context から取得した frame の位置情報
                   （"x", "y", "width" を含む）
        mr_url: MR URL
        commit_sha: commit SHA

    Returns:
        Miro API に送る body 辞書
    """
    return {
        "data": {
            "title": "✅ Phase 8a 完了",
            "description": (
                f"MR: {mr_url or '(MR URL 不明)'}\n"
                f"commit: {_shorten_commit(commit_sha)}"
            ),
        },
        "position": {
            "x": float(frame_meta.get("x", 0)) + float(frame_meta.get("width", 0)) + 50.0,
            "y": float(frame_meta.get("y", 0)),
        },
        "style": {"fillColor": "#4FCC8B"},
    }
