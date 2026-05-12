"""冪等キー生成。

計画書 §9.1 に従う形式:

    {workflow_id}:{event_type}:{resource}:{revision}

例:

    wf_20260513_001:phase8a_completed:figma_frame_abc123:a1b2c3d4
"""

from __future__ import annotations


def build_idempotency_key(
    workflow_id: str,
    event_type: str,
    resource: str,
    revision: str,
) -> str:
    """冪等キーを構築する。

    Args:
        workflow_id: ワークフロー ID
        event_type: イベント種別（例: "phase8a_completed"）
        resource: 投稿先リソース ID（frame_id / board_id 等）
        revision: 投稿対象の revision（commit sha / MR iid 等）

    Returns:
        コロン区切りの冪等キー文字列。

    Raises:
        ValueError: 引数が空 / コロンを含む場合
    """
    parts = (workflow_id, event_type, resource, revision)
    for part in parts:
        if not part:
            raise ValueError(f"冪等キー構成要素が空: {parts}")
        if ":" in part:
            raise ValueError(
                f"冪等キー構成要素に ':' が含まれる（曖昧化を避けるため拒否）: {part!r}"
            )
    return ":".join(parts)
