"""prime_renderer の単体テスト（Workgraph Phase 6 / Issue #48）"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.integrations.notion_dashboard.prime_renderer import (
    MEMORY_TYPE_DISPLAY_ORDER,
    render_prime_json,
    render_prime_markdown,
)
from hokusai.integrations.notion_dashboard.project_memory_db import (
    MEMORY_TYPE_AVOIDANCE,
    MEMORY_TYPE_HANDOVER_NOTE,
    MEMORY_TYPE_PROJECT_RULE,
)


def _page(
    *,
    page_id: str,
    name: str,
    memory_type: str,
    summary: str | None = None,
    content: str = "",
    applies_to: list[str] | None = None,
    profile: str | None = None,
) -> dict:
    props: dict = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Type": {"select": {"name": memory_type}},
        "Status": {"select": {"name": "active"}},
        "Content": {"rich_text": [{"text": {"content": content}}]},
    }
    if summary is not None:
        props["Summary"] = {"rich_text": [{"text": {"content": summary}}]}
    if applies_to:
        props["Applies To"] = {"multi_select": [{"name": p} for p in applies_to]}
    if profile:
        props["Profile"] = {"rich_text": [{"text": {"content": profile}}]}
    return {"id": page_id, "properties": props}


# ---------------------------------------------------------------------------
# render_prime_markdown
# ---------------------------------------------------------------------------


def test_markdown_emits_header_meta_and_workflow_id():
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile="acme",
        current_phase="phase5",
        memories=[],
    )
    assert "# HOKUSAI Prime Context — workflow `wf-1`" in out
    assert "profile: `acme`" in out
    assert "current_phase: `phase5`" in out
    assert "_active な workgraph context はありません_" in out


def test_markdown_omits_meta_line_when_profile_and_phase_missing():
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
    )
    assert "profile:" not in out
    assert "current_phase:" not in out


def test_markdown_groups_by_type_with_handover_first():
    memories = [
        _page(
            page_id="p1",
            name="Avoid raw SQL",
            memory_type=MEMORY_TYPE_AVOIDANCE,
            content="use ORM",
        ),
        _page(
            page_id="p2",
            name="Handover from Alice",
            memory_type=MEMORY_TYPE_HANDOVER_NOTE,
            summary="See prior PR #100",
        ),
        _page(
            page_id="p3",
            name="No PII in logs",
            memory_type=MEMORY_TYPE_PROJECT_RULE,
            content="redact emails",
        ),
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile="acme",
        current_phase="phase5",
        memories=memories,
    )
    # handover_note section が project_rule / avoidance より先に登場
    pos_handover = out.find("Handover Notes")
    pos_rule = out.find("Project Rules")
    pos_avoid = out.find("Avoidance")
    assert -1 < pos_handover < pos_rule < pos_avoid
    # Summary 優先で body を出力
    assert "> See prior PR #100" in out
    # Summary 無ければ Content 採用
    assert "> redact emails" in out


def test_markdown_includes_applies_to_when_present():
    memories = [
        _page(
            page_id="p1",
            name="X",
            memory_type=MEMORY_TYPE_PROJECT_RULE,
            content="body",
            applies_to=["phase4", "phase5"],
        ),
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=memories,
    )
    assert "**Applies To:** `phase4`, `phase5`" in out


def test_markdown_display_order_matches_constant():
    """MEMORY_TYPE_DISPLAY_ORDER の先頭は handover_note（要件 §8.4）"""
    assert MEMORY_TYPE_DISPLAY_ORDER[0] == MEMORY_TYPE_HANDOVER_NOTE


def test_markdown_falls_back_to_content_when_summary_is_whitespace_only():
    """Summary が空白のみなら Content にフォールバックする（Copilot 指摘:
    `or` 演算子で空白文字列が truthy になり Content が無視される問題を防止）"""
    memories = [
        _page(
            page_id="p1",
            name="X",
            memory_type=MEMORY_TYPE_PROJECT_RULE,
            summary="   \n  ",  # 空白だけ
            content="real content body",
        ),
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=memories,
    )
    assert "> real content body" in out


def test_markdown_includes_work_items_section():
    """Work Items 渡されたら専用 section に出力（Issue #54 / Workgraph 完成）"""
    work_items = [
        {
            "id": "wi-1",
            "properties": {
                "Title": {"title": [{"text": {"content": "Implement login"}}]},
                "Status": {"select": {"name": "ready"}},
                "Phase": {"select": {"name": "phase5"}},
                "Description": {"rich_text": [{"text": {"content": "Use OAuth2"}}]},
            },
        }
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        work_items=work_items,
    )
    assert "## Work Items" in out
    assert "### Implement login" in out
    assert "**Status:** `ready`" in out
    assert "**Phase:** `phase5`" in out
    assert "> Use OAuth2" in out


def test_markdown_includes_review_issues_section():
    review_issues = [
        {
            "id": "ri-1",
            "properties": {
                "Title": {"title": [{"text": {"content": "Missing null check"}}]},
                "Severity": {"select": {"name": "high"}},
                "Source": {"select": {"name": "copilot_review"}},
                "Rule ID": {"rich_text": [{"text": {"content": "PYL-W0612"}}]},
                "File Path": {"rich_text": [{"text": {"content": "src/auth.py"}}]},
                "Message": {"rich_text": [{"text": {"content": "Add null guard"}}]},
            },
        }
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        review_issues=review_issues,
    )
    assert "## Open Review Issues" in out
    assert "### Missing null check" in out
    assert "**Severity:** `high`" in out
    assert "**Rule:** `PYL-W0612`" in out
    assert "**File:** `src/auth.py`" in out
    assert "> Add null guard" in out


def test_markdown_includes_gates_section():
    gates = [
        {
            "id": "g-1",
            "properties": {
                "Name": {"title": [{"text": {"content": "Security review"}}]},
                "Status": {"select": {"name": "pending"}},
                "Gate Type": {"select": {"name": "human_approval"}},
                # Workflow Gates DB schema は number プロパティ（Copilot 指摘）
                "Required By Phase": {"number": 8},
                "Description": {"rich_text": [{"text": {"content": "Sign off needed"}}]},
            },
        }
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        gates=gates,
    )
    assert "## Workflow Gates" in out
    assert "### Security review" in out
    assert "**Status:** `pending`" in out
    assert "**Type:** `human_approval`" in out
    assert "**Required by:** `phase8`" in out


def test_markdown_section_order_handover_memory_workitems_issues_gates():
    """セクション順序: Handover → Memory → Work Items → Issues → Gates"""
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[
            _page(page_id="m1", name="A", memory_type=MEMORY_TYPE_HANDOVER_NOTE),
            _page(page_id="m2", name="B", memory_type=MEMORY_TYPE_PROJECT_RULE),
        ],
        work_items=[{"id": "wi1", "properties": {"Title": {"title": [{"text": {"content": "T"}}]}, "Status": {"select": {"name": "ready"}}}}],
        review_issues=[{"id": "ri1", "properties": {"Title": {"title": [{"text": {"content": "I"}}]}, "Severity": {"select": {"name": "low"}}}}],
        gates=[{"id": "g1", "properties": {"Name": {"title": [{"text": {"content": "G"}}]}, "Status": {"select": {"name": "pending"}}}}],
    )
    pos_h = out.find("Handover Notes")
    pos_rule = out.find("Project Rules")
    pos_wi = out.find("Work Items")
    pos_ri = out.find("Open Review Issues")
    pos_g = out.find("Workflow Gates")
    assert -1 < pos_h < pos_rule < pos_wi < pos_ri < pos_g


def test_json_includes_workgraph_context_arrays():
    """JSON 出力に work_items / review_issues / gates キーが必ず含まれる"""
    out = render_prime_json(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        work_items=[{"id": "wi1", "properties": {"Title": {"title": [{"text": {"content": "T"}}]}, "Status": {"select": {"name": "ready"}}, "Phase": {"select": {"name": "phase5"}}}}],
        review_issues=[],
        gates=[],
    )
    payload = json.loads(out)
    assert payload["work_items"] == [{
        "id": "wi1",
        "title": "T",
        "status": "ready",
        "phase": "phase5",
        "description": "",
    }]
    assert payload["review_issues"] == []
    assert payload["gates"] == []


def test_markdown_concatenates_multi_element_rich_text():
    """Notion が rich_text を装飾 / mention 等で複数要素に分割しても
    全要素が連結されて出力される（Copilot 指摘）"""
    memories = [
        {
            "id": "p1",
            "properties": {
                "Name": {
                    "title": [
                        {"plain_text": "Part1 "},
                        {"plain_text": "Part2"},
                    ]
                },
                "Type": {"select": {"name": MEMORY_TYPE_PROJECT_RULE}},
                "Content": {
                    "rich_text": [
                        # plain_text 経路
                        {"plain_text": "Line1 "},
                        # text.content フォールバック経路
                        {"text": {"content": "Line2 "}},
                        # mention（text key 無し）→ skip
                        {"type": "mention", "mention": {"type": "user"}},
                        # 末尾の通常要素
                        {"plain_text": "Tail"},
                    ]
                },
            },
        }
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=memories,
    )
    assert "### Part1 Part2" in out
    assert "> Line1 Line2 Tail" in out


def test_markdown_handles_unknown_memory_type_gracefully():
    """schema drift で想定外 type が来ても落ちず、末尾 section に出す"""
    memories = [
        {
            "id": "p1",
            "properties": {
                "Name": {"title": [{"text": {"content": "X"}}]},
                "Type": {"select": {"name": "wild_card_type"}},
                "Content": {"rich_text": [{"text": {"content": "body"}}]},
            },
        },
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=memories,
    )
    assert "## wild_card_type" in out


# ---------------------------------------------------------------------------
# render_prime_json
# ---------------------------------------------------------------------------


def test_json_emits_structured_payload():
    memories = [
        _page(
            page_id="p1",
            name="X",
            memory_type=MEMORY_TYPE_PROJECT_RULE,
            content="body",
            applies_to=["phase5"],
            profile="acme",
            summary="s",
        ),
    ]
    out = render_prime_json(
        workflow_id="wf-1",
        profile="acme",
        current_phase="phase5",
        memories=memories,
    )
    payload = json.loads(out)
    assert payload["workflow_id"] == "wf-1"
    assert payload["profile"] == "acme"
    assert payload["current_phase"] == "phase5"
    assert len(payload["memories"]) == 1
    m = payload["memories"][0]
    assert m["id"] == "p1"
    assert m["name"] == "X"
    assert m["memory_type"] == MEMORY_TYPE_PROJECT_RULE
    assert m["status"] == "active"
    assert m["applies_to"] == ["phase5"]
    assert m["profile"] == "acme"
    assert m["summary"] == "s"
    assert m["content"] == "body"


def test_json_returns_empty_memories_array_when_none():
    out = render_prime_json(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
    )
    payload = json.loads(out)
    assert payload["memories"] == []


# ---------------------------------------------------------------------------
# M2.4 (#92): 空状態の prime 出力に構成要素別 diagnostics を表示
# ---------------------------------------------------------------------------


def test_markdown_renders_diagnostics_as_italic_bullets_when_empty():
    """has_any=False かつ diagnostics 指定時、existing 空メッセージの後に
    italic bullet として診断行が出る。"""
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile="hokusai",
        current_phase="phase4",
        memories=[],
        diagnostics=[
            "Project Memory DB: 未設定 (env HOKUSAI_NOTION_PROJECT_MEMORY_DB_ID)",
            "Work Items DB: 取得済 0 件",
        ],
    )
    assert "_active な workgraph context はありません_" in out
    assert "- _Project Memory DB: 未設定 (env HOKUSAI_NOTION_PROJECT_MEMORY_DB_ID)_" in out
    assert "- _Work Items DB: 取得済 0 件_" in out


def test_markdown_omits_diagnostics_when_any_section_present():
    """has_any=True のときは diagnostics を無視（output ノイズ防止）."""
    memories = [
        _page(
            page_id="p1",
            name="Rule",
            memory_type=MEMORY_TYPE_PROJECT_RULE,
            content="x",
        ),
    ]
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=memories,
        diagnostics=["Project Memory DB: 取得済 0 件"],
    )
    # 空状態ではないので diagnostic は出さない
    assert "Project Memory DB: 取得済 0 件" not in out
    # 通常 section は通常通り出る
    assert "Project Rules" in out


def test_markdown_empty_without_diagnostics_keeps_existing_output():
    """diagnostics=None のときは従来通り「空メッセージ」のみ（後方互換）."""
    out = render_prime_markdown(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
    )
    assert "_active な workgraph context はありません_" in out
    # bullet 行は無い
    assert "- _" not in out


def test_json_includes_diagnostics_key_always():
    """JSON 側は has_any に関わらず diagnostics key を保持。
    自動処理側が必要に応じて参照できるよう、None or list が必ず入る。"""
    out_none = render_prime_json(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
    )
    payload_none = json.loads(out_none)
    assert "diagnostics" in payload_none
    assert payload_none["diagnostics"] is None

    out_with = render_prime_json(
        workflow_id="wf-1",
        profile=None,
        current_phase=None,
        memories=[],
        diagnostics=["Project Memory DB: 未設定 (env X)"],
    )
    payload_with = json.loads(out_with)
    assert payload_with["diagnostics"] == [
        "Project Memory DB: 未設定 (env X)"
    ]
