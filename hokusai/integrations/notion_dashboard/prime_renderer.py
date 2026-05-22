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
    work_items: list[dict] | None = None,
    review_issues: list[dict] | None = None,
    gates: list[dict] | None = None,
) -> str:
    """active Memory + workgraph context のリストを Agent prompt 向け
    Markdown へ整形する（Workgraph 完成 / Issue #54）。

    出力順序（要件 §8.4 で「先に必要な情報」を冒頭に並べる方針）:
    1. Handover Notes（前任引き継ぎ）
    2. 残り Memory Type（project_rule / architecture_decision など）
    3. Work Items（ready / in_progress）
    4. Review Issues（open）
    5. Workflow Gates（pending / blocked / open）

    空のセクションは省略する。

    Returns:
        UTF-8 Markdown 文字列。
    """
    work_items = work_items or []
    review_issues = review_issues or []
    gates = gates or []

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

    has_any = bool(memories) or bool(work_items) or bool(review_issues) or bool(gates)
    if not has_any:
        lines.append("_active な workgraph context はありません_")
        lines.append("")
        return "\n".join(lines)

    # Memory セクション
    if memories:
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

        # 知らない type（DB schema drift 想定外）は Memory 末尾に出す
        for mtype, entries in grouped.items():
            if mtype in MEMORY_TYPE_DISPLAY_ORDER:
                continue
            lines.append(f"## {mtype}")
            lines.append("")
            for entry in entries:
                lines.extend(_render_memory_entry(entry, mtype))
                lines.append("")

    # Work Items
    if work_items:
        lines.append("## Ready Work Items（Phase 5 で Agent に渡せる候補）")
        lines.append("")
        for wi in work_items:
            lines.extend(_render_work_item_entry(wi))
            lines.append("")

    # Review Issues
    if review_issues:
        lines.append("## Open Review Issues（未解消の指摘）")
        lines.append("")
        for issue in review_issues:
            lines.extend(_render_review_issue_entry(issue))
            lines.append("")

    # Gates
    if gates:
        lines.append("## Pending Gates（次に必要な人間判断 / ブロッキング）")
        lines.append("")
        for gate in gates:
            lines.extend(_render_gate_entry(gate))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_prime_json(
    *,
    workflow_id: str,
    profile: str | None,
    current_phase: str | None,
    memories: list[dict],
    work_items: list[dict] | None = None,
    review_issues: list[dict] | None = None,
    gates: list[dict] | None = None,
) -> str:
    """active Memory + workgraph context を Agent / 自動処理向け JSON へ整形
    する（Workgraph 完成 / Issue #54）。

    Markdown 版との差分: 表示順 / 整形を行わず、各カテゴリ配列に raw 抜き出し
    dict を入れる。空入力カテゴリは空配列で出力（呼び出し側で「未取得」と
    「空」を区別できるよう常に key を含める）。
    """
    work_items = work_items or []
    review_issues = review_issues or []
    gates = gates or []
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "profile": profile,
        "current_phase": current_phase,
        "memories": [_extract_memory_dict(page) for page in memories],
        "work_items": [_extract_work_item_dict(p) for p in work_items],
        "review_issues": [_extract_review_issue_dict(p) for p in review_issues],
        "gates": [_extract_gate_dict(p) for p in gates],
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
    # Summary が空白のみだと truthy 判定で content が無視されるため、
    # 個別に strip して非空の方を採用する（Copilot 指摘）。Summary 優先で
    # 内容が無ければ Content にフォールバック。
    summary_stripped = summary.strip()
    content_stripped = content.strip()
    body = summary_stripped or content_stripped
    if body:
        # 引用 block で囲み、複数行は各行に `> ` を付与
        for ln in body.splitlines() or [""]:
            out.append(f"> {ln}" if ln else ">")
    return out


def _render_work_item_entry(page: dict) -> list[str]:
    title = _extract_title(page, "Title") or "(untitled)"
    status = _extract_select_name(page, "Status") or "(unknown)"
    phase = _extract_select_name(page, "Phase")
    out: list[str] = [f"### {title}"]
    meta = [f"**Status:** `{status}`"]
    if phase:
        meta.append(f"**Phase:** `{phase}`")
    out.append(" / ".join(meta))
    description = _extract_rich_text(page, "Description").strip()
    if description:
        for ln in description.splitlines() or [""]:
            out.append(f"> {ln}" if ln else ">")
    return out


def _render_review_issue_entry(page: dict) -> list[str]:
    title = _extract_title(page, "Title") or "(untitled)"
    severity = _extract_select_name(page, "Severity") or "(unknown)"
    source = _extract_select_name(page, "Source")
    rule_id = _extract_rich_text(page, "Rule ID")
    file_path = _extract_rich_text(page, "File Path")
    out: list[str] = [f"### {title}"]
    meta = [f"**Severity:** `{severity}`"]
    if source:
        meta.append(f"**Source:** `{source}`")
    if rule_id:
        meta.append(f"**Rule:** `{rule_id}`")
    if file_path:
        meta.append(f"**File:** `{file_path}`")
    out.append(" / ".join(meta))
    message = _extract_rich_text(page, "Message").strip()
    if message:
        for ln in message.splitlines() or [""]:
            out.append(f"> {ln}" if ln else ">")
    return out


def _render_gate_entry(page: dict) -> list[str]:
    name = _extract_title(page, "Name") or "(untitled)"
    status = _extract_select_name(page, "Status") or "(unknown)"
    gate_type = _extract_select_name(page, "Gate Type")
    required_phase = _extract_rich_text(page, "Required By Phase")
    out: list[str] = [f"### {name}"]
    meta = [f"**Status:** `{status}`"]
    if gate_type:
        meta.append(f"**Type:** `{gate_type}`")
    if required_phase:
        meta.append(f"**Required by:** `{required_phase}`")
    out.append(" / ".join(meta))
    description = _extract_rich_text(page, "Description").strip()
    if description:
        for ln in description.splitlines() or [""]:
            out.append(f"> {ln}" if ln else ">")
    return out


def _extract_work_item_dict(page: dict) -> dict[str, Any]:
    return {
        "id": page.get("id"),
        "title": _extract_title(page, "Title"),
        "status": _extract_select_name(page, "Status"),
        "phase": _extract_select_name(page, "Phase"),
        "description": _extract_rich_text(page, "Description"),
    }


def _extract_review_issue_dict(page: dict) -> dict[str, Any]:
    return {
        "id": page.get("id"),
        "title": _extract_title(page, "Title"),
        "status": _extract_select_name(page, "Status"),
        "severity": _extract_select_name(page, "Severity"),
        "source": _extract_select_name(page, "Source"),
        "rule_id": _extract_rich_text(page, "Rule ID"),
        "file_path": _extract_rich_text(page, "File Path"),
        "message": _extract_rich_text(page, "Message"),
    }


def _extract_gate_dict(page: dict) -> dict[str, Any]:
    return {
        "id": page.get("id"),
        "name": _extract_title(page, "Name"),
        "status": _extract_select_name(page, "Status"),
        "gate_type": _extract_select_name(page, "Gate Type"),
        "required_by_phase": _extract_rich_text(page, "Required By Phase"),
        "description": _extract_rich_text(page, "Description"),
    }


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
    return _join_rich_text_items(title.get("title") or [])


def _extract_rich_text(page: dict, prop_name: str) -> str:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    return _join_rich_text_items(prop.get("rich_text") or [])


def _join_rich_text_items(items: list[dict]) -> str:
    """Notion の rich_text / title array の全要素を連結して 1 つの文字列にする。

    Notion は装飾 / メンション / リンクで rich_text を複数 element に分割する
    ため、先頭要素だけ読むと後続のテキストが欠落する。各 element は
    `plain_text` をまず採用し、無ければ `text.content` でフォールバック
    （mention / equation 等で `text` キーが無いケースに耐性を持たせる）。
    Copilot 指摘。
    """
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        plain = item.get("plain_text")
        if isinstance(plain, str) and plain:
            parts.append(plain)
            continue
        text = item.get("text")
        if isinstance(text, dict):
            content = text.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    return "".join(parts)


def _extract_select_name(page: dict, prop_name: str) -> str | None:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    sel = prop.get("select") or {}
    return sel.get("name")


def _extract_multi_select(page: dict, prop_name: str) -> list[str]:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    items = prop.get("multi_select") or []
    return [opt.get("name") for opt in items if opt.get("name")]
