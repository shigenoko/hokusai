"""`hokusai prime` の出力レンダラ（Workgraph Phase 6 / Issue #48）

Project Memory DB から取得した active Memory を Agent prompt として注入
しやすい Markdown / JSON テキストへ整形する。

設計方針:
- 純粋関数として実装し、I/O や API 呼び出しを持たない（テスト容易性）
- Memory Type ごとに section を分けて並べる（Agent が type を見て扱いを変えやすい）
- Notion page property 形式（rich_text / select / multi_select）を内部で
  解凍する `_extract_*` helper を集約し、prime_renderer 内で完結させる
"""

from __future__ import annotations

import json
from typing import Any

from .project_memory_db import (
    MEMORY_TYPE_ARCHITECTURE_DECISION,
    MEMORY_TYPE_AVOIDANCE,
    MEMORY_TYPE_DOMAIN_KNOWLEDGE,
    MEMORY_TYPE_HANDOVER_NOTE,
    MEMORY_TYPE_OPERATIONS_NOTE,
    MEMORY_TYPE_POLICY_NOTE,
    MEMORY_TYPE_PROJECT_RULE,
)

# Markdown section の表示順（要件 §8.2 の重要度 + handover_note を冒頭優先）
MEMORY_TYPE_DISPLAY_ORDER: tuple[str, ...] = (
    MEMORY_TYPE_HANDOVER_NOTE,
    MEMORY_TYPE_PROJECT_RULE,
    MEMORY_TYPE_ARCHITECTURE_DECISION,
    MEMORY_TYPE_AVOIDANCE,
    MEMORY_TYPE_DOMAIN_KNOWLEDGE,
    MEMORY_TYPE_OPERATIONS_NOTE,
    MEMORY_TYPE_POLICY_NOTE,
)

MEMORY_TYPE_HEADINGS: dict[str, str] = {
    MEMORY_TYPE_HANDOVER_NOTE: "Handover Notes（前オペレータからの引き継ぎ）",
    MEMORY_TYPE_PROJECT_RULE: "Project Rules（案件固有ルール）",
    MEMORY_TYPE_ARCHITECTURE_DECISION: "Architecture Decisions（設計判断）",
    MEMORY_TYPE_AVOIDANCE: "Avoidance（避けるべき実装）",
    MEMORY_TYPE_DOMAIN_KNOWLEDGE: "Domain Knowledge（ドメイン知識）",
    MEMORY_TYPE_OPERATIONS_NOTE: "Operations Notes（運用注意点）",
    MEMORY_TYPE_POLICY_NOTE: "Policy Notes（コンプライアンス）",
}


def render_prime_markdown(
    *,
    workflow_id: str,
    profile: str | None,
    current_phase: str | None,
    memories: list[dict],
) -> str:
    """active Memory のリストを Agent prompt 向け Markdown へ整形する。

    Memory Type ごとに `## Heading` で section 化し、各 entry を以下の形式で
    出力する:

        ### {Name}
        **Type:** {memory_type} / **Applies To:** phase1, phase2
        > {Summary or Content}

    Returns:
        UTF-8 Markdown 文字列。空入力時はヘッダのみの最小出力を返す。
    """
    lines: list[str] = []
    lines.append(f"# HOKUSAI Prime Context — workflow `{workflow_id}`")
    meta_bits = []
    if profile:
        meta_bits.append(f"profile: `{profile}`")
    if current_phase:
        meta_bits.append(f"current_phase: `{current_phase}`")
    if meta_bits:
        lines.append("")
        lines.append(" / ".join(meta_bits))
    lines.append("")

    if not memories:
        lines.append("_active Project Memory はありません_")
        lines.append("")
        return "\n".join(lines)

    grouped: dict[str, list[dict]] = {}
    for page in memories:
        mtype = _extract_select_name(page, "Type") or "unknown"
        grouped.setdefault(mtype, []).append(page)

    for mtype in MEMORY_TYPE_DISPLAY_ORDER:
        entries = grouped.get(mtype)
        if not entries:
            continue
        heading = MEMORY_TYPE_HEADINGS.get(mtype, mtype)
        lines.append(f"## {heading}")
        lines.append("")
        for entry in entries:
            lines.extend(_render_memory_entry(entry, mtype))
            lines.append("")

    # 知らない type（DB schema drift 想定外）は末尾に出す
    for mtype, entries in grouped.items():
        if mtype in MEMORY_TYPE_DISPLAY_ORDER:
            continue
        lines.append(f"## {mtype}")
        lines.append("")
        for entry in entries:
            lines.extend(_render_memory_entry(entry, mtype))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_prime_json(
    *,
    workflow_id: str,
    profile: str | None,
    current_phase: str | None,
    memories: list[dict],
) -> str:
    """active Memory を Agent / 自動処理向け JSON へ整形する。

    Markdown 版との差分: 表示順 / 整形を行わず、`memories` 配列に Memory の
    生 property を抜き出した dict を入れる。後段で別形式に再変換しやすい。
    """
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "profile": profile,
        "current_phase": current_phase,
        "memories": [_extract_memory_dict(page) for page in memories],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def _render_memory_entry(page: dict, memory_type: str) -> list[str]:
    name = _extract_title(page, "Name") or "(untitled)"
    applies = _extract_multi_select(page, "Applies To")
    summary = _extract_rich_text(page, "Summary")
    content = _extract_rich_text(page, "Content")
    profile = _extract_rich_text(page, "Profile")

    out: list[str] = [f"### {name}"]
    meta = [f"**Type:** `{memory_type}`"]
    if profile:
        meta.append(f"**Profile:** `{profile}`")
    if applies:
        meta.append("**Applies To:** " + ", ".join(f"`{p}`" for p in applies))
    out.append(" / ".join(meta))
    body = (summary or content or "").strip()
    if body:
        # 引用 block で囲み、複数行は各行に `> ` を付与
        for ln in body.splitlines() or [""]:
            out.append(f"> {ln}" if ln else ">")
    return out


def _extract_memory_dict(page: dict) -> dict[str, Any]:
    return {
        "id": page.get("id"),
        "name": _extract_title(page, "Name"),
        "memory_type": _extract_select_name(page, "Type"),
        "status": _extract_select_name(page, "Status"),
        "profile": _extract_rich_text(page, "Profile"),
        "applies_to": _extract_multi_select(page, "Applies To"),
        "summary": _extract_rich_text(page, "Summary"),
        "content": _extract_rich_text(page, "Content"),
    }


def _extract_title(page: dict, prop_name: str) -> str:
    title = (page.get("properties") or {}).get(prop_name) or {}
    items = title.get("title") or []
    if not items:
        return ""
    return (items[0].get("text") or {}).get("content") or ""


def _extract_rich_text(page: dict, prop_name: str) -> str:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    items = prop.get("rich_text") or []
    if not items:
        return ""
    return (items[0].get("text") or {}).get("content") or ""


def _extract_select_name(page: dict, prop_name: str) -> str | None:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    sel = prop.get("select") or {}
    return sel.get("name")


def _extract_multi_select(page: dict, prop_name: str) -> list[str]:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    items = prop.get("multi_select") or []
    return [opt.get("name") for opt in items if opt.get("name")]
