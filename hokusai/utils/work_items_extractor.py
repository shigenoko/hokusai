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
# **ReDoS 対策**: leading whitespace は `\s{0,16}` で上限を設け、checkbox 内は
# 単一文字クラス `[xX ]` にしてネスト量指定子（`\s*[xX ]?\s*`）の組合せ爆発を
# 排除（SonarCloud python:S5852 対策。markdown の checkbox 表記は実用上
# 1 文字で十分で、`[x ]` / `[X]` / `[ ]` のいずれかに限定する）。
_CHECKBOX_PATTERN = re.compile(r"^\s{0,16}[-*]\s+\[[xX ]\]\s+(.+?)\s*$")

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
        # checkbox を先に判定。`_NUMBERED_PATTERN` は行頭が数字（または heading
        # prefix + 数字）でないとマッチしないため `- [ ]` 自体は番号 pattern
        # にはマッチしないが、両形式が同一 markdown 内に混在する場合は
        # checkbox を優先採用する（より明示的に task と分かるため）という
        # 全体ロジック上、行単位でも checkbox 判定を先に行う。
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
    """markdown の inline emphasis（wrapping のみ）を剥がし、前後空白を削る。

    例:
        `**login form**` → `login form`
        `` `auth.py` を修正 `` → `auth.py を修正`
        `*重要*` → `重要`
        `fix_auth_flow を修正` → `fix_auth_flow を修正`（snake_case 識別子を温存）

    実装方針:
    - emphasis を **囲んでいる場合のみ剥がす**（前回の単純な `re.sub(r"[*_`]+", "")`
      は単語内の `_` まで除去して `fix_auth_flow` → `fixauthflow` のように
      snake_case 識別子を壊す問題があった。PR #41 Copilot 3 回目指摘で修正）
    - `` ` `` / `*` は wrapping 検出にだけ正規表現を当て、内側のテキストを保持
    - `_` は word boundary（前後が英数字でない）と組み合わせて、`snake_case_id`
      のような識別子内の `_` を残しつつ `_italic_` のような wrapping だけ剥がす
    """
    # `.strip(_INLINE_EMPHASIS_CHARS)` は使わない: 片側の wrapping だけ消えて
    # regex の `\*+...\*+` などのペア検出が成立しなくなるため
    # （PR #41 Copilot 3 回目指摘の修正で気付いた）。代わりに、regex で
    # wrapping を剥がした後に通常の `.strip()` で前後空白を整える。
    stripped = (text or "").strip()
    # ReDoS 対策（SonarCloud python:S5852）: ネスト量指定子を避け、
    # `\*+` / `_+` の代わりに `\*{1,3}` / `_{1,3}` で上限を明示し、
    # かつ内側 capture も `+?` (lazy) ではなく `+` (greedy) と否定文字
    # クラス `[^*\n]+` / `[^_\n]+` の組合せにする。否定文字クラスは
    # 終端文字を最初から含まないので backtracking で爆発しない。
    # inline code: `text`
    stripped = re.sub(r"`([^`\n]+)`", r"\1", stripped)
    # bold / italic: *text* / **text** / ***text***
    stripped = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", stripped)
    # italic with underscores: word boundary が必要（snake_case 識別子を保護）。
    # 前後が英数字でない位置でだけ `_..._` を剥がす。Python re モジュールには
    # Unicode 単語境界がある（\b）が、`_` も word character なので `\b` は
    # 期待通り動かない。前後の文字を look-around で明示的に検査する。
    # `_{1,3}` で量指定子に上限を設けるのも ReDoS 対策。
    stripped = re.sub(
        r"(?<![A-Za-z0-9])_{1,3}([^_\n]+)_{1,3}(?![A-Za-z0-9])", r"\1", stripped
    )
    return stripped.strip()


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
