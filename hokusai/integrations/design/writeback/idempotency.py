"""冪等キー生成。

計画書 §9.1 に従う形式:

    {workflow_id}:{event_type}:{resource}:{revision}

例:

    wf_20260513_001:phase8a_completed:figma_frame_abc123:a1b2c3d4

注意: 各構成要素は **URL エンコード（percent-encoded）してから** ":" で結合する。
これにより Figma node_id（"0:1"）のようにコロンを含む値も曖昧化なく扱える:

    resource="figma_0:1" → encoded "figma_0%3A1" → key の対応部分は "figma_0%3A1"

旧実装は ":" を含む構成要素を ValueError で拒否していたが、Figma の node_id は
通常コロンを含むため writeback が必ず失敗する致命的バグだった。
"""

from __future__ import annotations

from urllib.parse import quote


# 構成要素のエンコード時に安全文字として残すもの（英数字に加えて）
# `:` は区切り文字として使うため必ずエンコードする
_SAFE_CHARS = "-_.~"


def build_idempotency_key(
    workflow_id: str,
    event_type: str,
    resource: str,
    revision: str,
) -> str:
    """冪等キーを構築する。

    各構成要素を URL エンコードしてから ":" で結合する。

    Args:
        workflow_id: ワークフロー ID
        event_type: イベント種別（例: "phase8a_completed"）
        resource: 投稿先リソース ID（frame_id / board_id 等、":" を含んでも OK）
        revision: 投稿対象の revision（commit sha / MR iid 等）

    Returns:
        コロン区切り（各 part は percent-encoded）の冪等キー文字列。

    Raises:
        ValueError: 引数が空文字の場合
    """
    parts = (workflow_id, event_type, resource, revision)
    for part in parts:
        if not part:
            raise ValueError(f"冪等キー構成要素が空: {parts}")
    encoded = [quote(str(p), safe=_SAFE_CHARS) for p in parts]
    return ":".join(encoded)
