"""work_plan markdown から Work Item 候補を抽出するヘルパー

Phase 4 plan ノードが /dev-plan の出力（markdown）から Work Items DB に
同期する Work Item の集合を抽出する際に使う。

抽出方針（MVP）:
- markdown 内の以下のパターンを Work Item 候補として拾う:
  - Checkbox 形式: `- [ ] タイトル` / `- [x] タイトル`
  - 番号付きステップ: `1.1 タイトル` / `1.1. タイトル` / `### 1. タイトル`
- 同じ markdown 内に両形式が混在する場合は **checkbox を優先**（より明示的に
  task と分かる）。checkbox が一つも無ければ番号付きステップを採用する。
- タイトルは markdown の inline emphasis（`*`, `_`, `` ` ``）を剥がし、前後
  空白を削る。空タイトルや本文 1 文字以下のものはノイズとして除外する。
- 重複は順序を保ったまま 1 件目だけ残す（dedupe_key は (workflow_id, phase,
  title) なので、同じ title が複数回出ると Notion 側で重複作成される）。
- MVP では依存関係（Dependencies）の自動推定は行わない。Phase 5+ で番号
  ツリーから先行ステップを依存にする実装を追加する想定。
"""

from __future__ import annotations

import re

# Checkbox 形式の Work Item 行: `- [ ] タイトル` / `- [x] タイトル` / `* [ ]` 等
# - 行頭の whitespace は許容（インデントされた sub-item にも反応）
# - checkbox の中身は ` ` / `x` / `X` のいずれか
# - title 部分は行末まで（trailing whitespace は後段で strip）
_CHECKBOX_PATTERN = re.compile(r"^\s*[-*]\s*\[\s*[xX ]?\s*\]\s+(.+?)\s*$")

# 番号付きステップ: `1.1 タイトル` / `1.1. タイトル` / `### 1. タイトル` / `## 1.1 タイトル`
# - 行頭の `#` (markdown heading) と whitespace は許容
# - 番号は `N` または `N.N` 形式（深さ 2 まで）
# - 番号末尾の `.` / `:` / `)` / 全角コロンは title 区切りとして許容
_NUMBERED_PATTERN = re.compile(
    r"^\s*#{0,6}\s*"
    r"(\d+(?:\.\d+)?)"  # `1` または `1.1`
    r"[\.\:\)\．\)\：]?"  # 番号末尾の区切り文字（任意、全角含む）
    r"\s+(.+?)\s*$"
)

# inline emphasis を剥がすための文字
_INLINE_EMPHASIS_CHARS = "*_`"

# 抽出対象から除外する短すぎる / 意味の無いタイトル
_MIN_TITLE_LENGTH = 2


def extract_work_items(work_plan: str) -> list[dict]:
    """work_plan markdown から Work Item 候補を抽出する。

    Args:
        work_plan: Phase 4 dev-plan の markdown 出力

    Returns:
        Work Item の dict のリスト。各要素は:
            {
                "title": str,         # Work Item タイトル（emphasis 剥がし済み）
                "source_line": str,   # 元の markdown 行（debugging 用）
            }
        順序は markdown 内の出現順。重複タイトルは 1 件目だけ残す。

    抽出ロジック:
    - checkbox が 1 件以上見つかれば checkbox のみを採用
    - そうでなければ番号付きステップを採用
    - 両方とも無ければ空リスト
    """
    if not work_plan:
        return []

    checkbox_items: list[dict] = []
    numbered_items: list[dict] = []

    for raw_line in work_plan.splitlines():
        # checkbox を優先判定（- [ ] の行も _NUMBERED_PATTERN にマッチする
        # ケースがあるため）
        cb_match = _CHECKBOX_PATTERN.match(raw_line)
        if cb_match:
            title = _normalize_title(cb_match.group(1))
            if _is_valid_title(title):
                checkbox_items.append({"title": title, "source_line": raw_line.rstrip()})
            continue

        num_match = _NUMBERED_PATTERN.match(raw_line)
        if num_match:
            title = _normalize_title(num_match.group(2))
            if _is_valid_title(title):
                numbered_items.append({"title": title, "source_line": raw_line.rstrip()})

    chosen = checkbox_items if checkbox_items else numbered_items
    return _dedupe_preserving_order(chosen)


def _normalize_title(text: str) -> str:
    """markdown の inline emphasis を剥がし、前後空白を削る。

    例:
        `**login form**` → `login form`
        `` `auth.py` を修正 `` → `auth.py を修正`
        `*重要*` → `重要`
    """
    stripped = (text or "").strip().strip(_INLINE_EMPHASIS_CHARS).strip()
    # 内部の連続 emphasis（**word**）も剥がす。re.sub で `*+`/`_+`/`` `+ ``
    # を空文字に置き換える。これは body 中の italic / bold / code を全て
    # plain text 化する単純化（Notion 側の rich_text に意味のあるマークアップ
    # を載せたい場合は別途設計）。
    return re.sub(r"[*_`]+", "", stripped).strip()


def _is_valid_title(title: str) -> bool:
    """空タイトル / 1 文字以下の意味のないタイトルを排除する。"""
    return len(title) >= _MIN_TITLE_LENGTH


def _dedupe_preserving_order(items: list[dict]) -> list[dict]:
    """同じ title の重複を 1 件目だけ残す（順序保持）。

    dedupe_key は (workflow_id, phase, title) の hash なので、同一 title を
    複数 Work Item として登録すると Notion 側で重複 page を作る要因になる。
    """
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        title = item.get("title", "")
        if title in seen:
            continue
        seen.add(title)
        result.append(item)
    return result
