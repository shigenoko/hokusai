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
    assert "_active Project Memory はありません_" in out


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
