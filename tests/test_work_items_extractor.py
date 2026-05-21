"""work_items_extractor の単体テスト（Issue #38 / Workgraph Phase 2）"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.utils.work_items_extractor import extract_work_items

# ---------------------------------------------------------------------------
# 空入力 / 何も抽出できない場合
# ---------------------------------------------------------------------------


def test_extract_returns_empty_for_empty_input():
    assert extract_work_items("") == []


def test_extract_returns_empty_when_no_patterns_match():
    """checkbox も番号付きステップも無い markdown は空 list"""
    md = """\
## 開発計画

ここには本文だけがあり、checkbox も番号付きステップもありません。
普通の段落です。
"""
    assert extract_work_items(md) == []


# ---------------------------------------------------------------------------
# Checkbox 形式
# ---------------------------------------------------------------------------


def test_extract_checkbox_items():
    md = """\
## 開発計画

- [ ] implement login form
- [ ] add unit tests
- [x] design review
"""
    items = extract_work_items(md)
    titles = [it["title"] for it in items]
    assert titles == ["implement login form", "add unit tests", "design review"]


def test_extract_checkbox_with_inline_emphasis():
    """** や ` の emphasis は剥がされる"""
    md = """\
- [ ] **implement** `auth.py` を修正
- [ ] *important* refactor
"""
    items = extract_work_items(md)
    assert items[0]["title"] == "implement auth.py を修正"
    assert items[1]["title"] == "important refactor"


def test_extract_preserves_snake_case_identifiers():
    """snake_case の識別子は title 内の `_` を温存する
    （PR #41 Copilot 3 回目指摘: 旧版は `_normalize_title` が全 `_` を除去し
    `fix_auth_flow` → `fixauthflow` のように壊していた）"""
    md = """\
- [ ] fix_auth_flow を修正
- [ ] add `oauth_callback_handler` to `src/api.py`
- [ ] _italic wrap_ but keep snake_case_id intact
"""
    items = extract_work_items(md)
    titles = [it["title"] for it in items]
    assert titles[0] == "fix_auth_flow を修正"
    assert titles[1] == "add oauth_callback_handler to src/api.py"
    # `_italic wrap_` は wrapping として剥がされるが、`snake_case_id` の `_` は残る
    assert titles[2] == "italic wrap but keep snake_case_id intact"


def test_extract_checkbox_dedupe_preserves_first():
    """同じ title は 1 件目だけ残す"""
    md = """\
- [ ] implement X
- [ ] implement X
- [ ] implement Y
"""
    items = extract_work_items(md)
    assert [it["title"] for it in items] == ["implement X", "implement Y"]


def test_extract_checkbox_skips_too_short_titles():
    """1 文字以下のタイトルは除外（ノイズ抑制）"""
    md = """\
- [ ] A
- [ ] AB
- [ ] ABC
"""
    items = extract_work_items(md)
    assert [it["title"] for it in items] == ["AB", "ABC"]


def test_extract_checkbox_with_asterisk_bullet():
    """`*` バレットでも recognize される（一部 LLM 出力で混在する）"""
    md = """\
* [ ] step alpha
* [ ] step beta
"""
    items = extract_work_items(md)
    assert [it["title"] for it in items] == ["step alpha", "step beta"]


# ---------------------------------------------------------------------------
# 番号付きステップ
# ---------------------------------------------------------------------------


def test_extract_numbered_steps():
    md = """\
## 開発計画

1.1 setup database
1.2 implement model
2.1 add API endpoint
"""
    items = extract_work_items(md)
    assert [it["title"] for it in items] == [
        "setup database",
        "implement model",
        "add API endpoint",
    ]


def test_extract_numbered_steps_with_dot_separator():
    """`1.1.` のドット区切りも認識"""
    md = """\
1.1. step alpha
1.2. step beta
"""
    items = extract_work_items(md)
    assert [it["title"] for it in items] == ["step alpha", "step beta"]


def test_extract_numbered_steps_with_heading_prefix():
    """`### 1. タイトル` のような heading prefix も認識"""
    md = """\
### 1. implement feature
### 2. write tests
"""
    items = extract_work_items(md)
    assert [it["title"] for it in items] == ["implement feature", "write tests"]


# ---------------------------------------------------------------------------
# checkbox 優先（両形式混在時）
# ---------------------------------------------------------------------------


def test_checkbox_takes_precedence_over_numbered_when_both_present():
    """両方ある場合は checkbox を採用（より明示的に task と分かるため）"""
    md = """\
## 開発計画

### 1. setup
1.1 prepare environment

- [ ] implement login
- [ ] write tests
"""
    items = extract_work_items(md)
    titles = [it["title"] for it in items]
    # checkbox のみ採用
    assert titles == ["implement login", "write tests"]


def test_numbered_used_when_no_checkboxes():
    md = """\
### 1. setup environment
### 2. implement core
"""
    items = extract_work_items(md)
    assert [it["title"] for it in items] == ["setup environment", "implement core"]


# ---------------------------------------------------------------------------
# source_line の保持
# ---------------------------------------------------------------------------


def test_source_line_is_preserved():
    md = "- [ ] implement X\n"
    items = extract_work_items(md)
    assert items[0]["source_line"] == "- [ ] implement X"
