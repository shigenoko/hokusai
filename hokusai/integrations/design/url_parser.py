"""
Figma / Miro URL パーサ。

Notion ダッシュボードや CLI から渡される URL を解析して、API 呼び出しに
必要なリソース ID（Figma file key / node-id、Miro board id）を取り出す。

設計方針:
- 厳密な URL バリデーションは行わず、ホスト名と path 構造から「らしい」
  パターンを検出する。`/file/`, `/design/`, `/proto/` などのバリエーション
  を許容する。
- 失敗時は ValueError を投げる。呼び出し側で警告ログ + on_failure 戦略
  に従って処理させる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_FIGMA_HOSTS = {"www.figma.com", "figma.com"}
_MIRO_HOSTS = {"miro.com", "www.miro.com"}

_FIGMA_PATH_PREFIXES = ("file", "design", "proto", "board")
_MIRO_PATH_PREFIXES = ("app", "board")

_FIGMA_FILE_KEY_RE = re.compile(r"^[A-Za-z0-9]{6,}$")
_MIRO_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_=\-]{6,}$")


@dataclass(frozen=True)
class ParsedFigmaUrl:
    """Figma URL から抽出したリソース情報。

    file_key: 必須。`/file/<key>/...` などから抽出。
    node_id:  任意。クエリパラメータ `node-id` から抽出（`:` を含む形）。
              Figma の API は `0-1` のようなハイフン区切りを返すこと
              もあるため、抽出時は元の表現を保ったまま渡す。
    title:    任意。URL から抽出されるスラッグ部分（説明用）。
    """

    file_key: str
    node_id: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class ParsedMiroUrl:
    """Miro URL から抽出したリソース情報。

    board_id: 必須。`/app/board/<id>/` または `/board/<id>/` から抽出。
    title:    任意。URL から抽出されるスラッグ部分（説明用）。
    """

    board_id: str
    title: str | None = None


def parse_figma_url(url: str) -> ParsedFigmaUrl:
    """Figma URL から file_key / node_id / title を取り出す。

    対応する形式:
    - https://www.figma.com/file/<KEY>/<TITLE>?node-id=...
    - https://www.figma.com/design/<KEY>/<TITLE>?node-id=...
    - https://www.figma.com/proto/<KEY>/<TITLE>?node-id=...
    - https://www.figma.com/board/<KEY>/<TITLE> (FigJam)

    Raises:
        ValueError: 上記以外の URL や file_key が抽出できない場合。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Figma URL is empty")

    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host not in _FIGMA_HOSTS:
        raise ValueError(f"Not a Figma URL: host={host!r}")

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in _FIGMA_PATH_PREFIXES:
        raise ValueError(f"Unexpected Figma path: {parsed.path!r}")

    file_key = parts[1]
    if not _FIGMA_FILE_KEY_RE.match(file_key):
        raise ValueError(f"Invalid Figma file key: {file_key!r}")

    title = parts[2] if len(parts) >= 3 else None

    node_id = None
    if parsed.query:
        qs = parse_qs(parsed.query)
        raw = qs.get("node-id", [None])[0]
        if raw:
            node_id = raw

    return ParsedFigmaUrl(file_key=file_key, node_id=node_id, title=title)


def parse_miro_url(url: str) -> ParsedMiroUrl:
    """Miro URL から board_id / title を取り出す。

    対応する形式:
    - https://miro.com/app/board/<BOARD_ID>/
    - https://miro.com/app/board/<BOARD_ID>/?...
    - https://miro.com/board/<BOARD_ID>/<TITLE>

    Raises:
        ValueError: ホスト不一致や board_id 抽出失敗時。
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Miro URL is empty")

    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if host not in _MIRO_HOSTS:
        raise ValueError(f"Not a Miro URL: host={host!r}")

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError(f"Unexpected Miro path: {parsed.path!r}")

    if parts[0] == "app" and len(parts) >= 3 and parts[1] == "board":
        board_id = parts[2]
        title = parts[3] if len(parts) >= 4 else None
    elif parts[0] == "board" and len(parts) >= 2:
        board_id = parts[1]
        title = parts[2] if len(parts) >= 3 else None
    else:
        raise ValueError(f"Unexpected Miro path: {parsed.path!r}")

    if not _MIRO_BOARD_ID_RE.match(board_id):
        raise ValueError(f"Invalid Miro board id: {board_id!r}")

    return ParsedMiroUrl(board_id=board_id, title=title)
