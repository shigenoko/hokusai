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

# title / status の欠落表示（SonarCloud 文字列重複対策: 定数化）
_UNTITLED = "(untitled)"
_UNKNOWN_STATUS = "(unknown)"


def extract_prime_index_entries(
    *,
    memories: list[dict],
    work_items: list[dict] | None = None,
    review_issues: list[dict] | None = None,
    gates: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Prime v2 MVP-2 (docs/design-prime-v2.md §8.1): Notion から取得した
    active context (memories / work_items / review_issues / gates) を
    `SQLiteStore.upsert_prime_index` に渡せる形に変換する純関数。

    各 entry は以下の dict を返す:
      - source_type: 'memory' / 'work_item' / 'review_issue' / 'gate'
      - source_id: Notion page ID
      - title: 検索対象タイトル
      - body: 検索対象本文
      - phase: 関連 phase（あれば、整数）
      - notion_page_id: 引用元アドレス
      - file_path: review_issue で File Path があれば
    title / body が両方空の entry は skip する（FTS5 index に空 token を
    入れない）。Notion page ID が無い entry も skip（citation 元として
    使えないため）。
    """
    entries: list[dict[str, Any]] = []
    work_items = work_items or []
    review_issues = review_issues or []
    gates = gates or []

    for page in memories:
        page_id = page.get("id") or ""
        if not page_id:
            continue
        title = _extract_title(page, "Name") or ""
        # Summary 優先、空なら Content にフォールバック（renderer と同じロジック）
        body = (
            _extract_rich_text(page, "Summary").strip()
            or _extract_rich_text(page, "Content").strip()
        )
        if not title and not body:
            continue
        # Applies To の最初の phase から phase 番号を抜く（複数なら最初を採用）
        phase_num = _parse_first_phase_int(_extract_multi_select(page, "Applies To"))
        entries.append({
            "source_type": "memory",
            "source_id": page_id,
            "title": title,
            "body": body,
            "phase": phase_num,
            "notion_page_id": page_id,
        })

    for page in work_items:
        page_id = page.get("id") or ""
        if not page_id:
            continue
        title = _extract_title(page, "Title") or ""
        body = _extract_rich_text(page, "Description").strip()
        if not title and not body:
            continue
        phase_num = _parse_first_phase_int(
            [_extract_select_name(page, "Phase")] if _extract_select_name(page, "Phase") else []
        )
        entries.append({
            "source_type": "work_item",
            "source_id": page_id,
            "title": title,
            "body": body,
            "phase": phase_num,
            "notion_page_id": page_id,
        })

    for page in review_issues:
        page_id = page.get("id") or ""
        if not page_id:
            continue
        title = _extract_title(page, "Title") or ""
        body = _extract_rich_text(page, "Message").strip()
        if not title and not body:
            continue
        file_path = _extract_rich_text(page, "File Path").strip() or None
        entries.append({
            "source_type": "review_issue",
            "source_id": page_id,
            "title": title,
            "body": body,
            "phase": None,
            "notion_page_id": page_id,
            "file_path": file_path,
        })

    for page in gates:
        page_id = page.get("id") or ""
        if not page_id:
            continue
        title = _extract_title(page, "Name") or ""
        body = _extract_rich_text(page, "Description").strip()
        if not title and not body:
            continue
        required_phase = _extract_number(page, "Required By Phase")
        phase_num = (
            required_phase
            if isinstance(required_phase, int) and 1 <= required_phase <= 10
            else None
        )
        entries.append({
            "source_type": "gate",
            "source_id": page_id,
            "title": title,
            "body": body,
            "phase": phase_num,
            "notion_page_id": page_id,
        })

    return entries


def _parse_first_phase_int(values: list[str] | None) -> int | None:
    """`["phase4", "phase7"]` のような list から最初の `phase{N}` を整数 N に
    変換する。値が無いか変換できなければ None。
    """
    if not values:
        return None
    for v in values:
        if not isinstance(v, str):
            continue
        s = v.strip().lower()
        if s.startswith("phase") and s[5:].isdigit():
            n = int(s[5:])
            if 1 <= n <= 10:
                return n
    return None


def render_prime_markdown(
    *,
    workflow_id: str,
    profile: str | None,
    current_phase: str | None,
    memories: list[dict],
    work_items: list[dict] | None = None,
    review_issues: list[dict] | None = None,
    gates: list[dict] | None = None,
    diagnostics: list[str] | None = None,
    query: str | None = None,
    query_results: list[dict[str, Any]] | None = None,
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

    `diagnostics` (M2.4 / #92): 出力する section が 1 つも無いとき、構成要素
    ごとの「設定有無 / 取得結果」を 1 行ずつ italic bullet で表示する用の
    事前整形済み文字列リスト。「DB share 未完了 / env 未設定 / 取得済 0 件 /
    Notion 障害」のどれかを呼び出し側 (cli_main) で判定して渡す想定。
    section が 1 つでもあれば diagnostics は無視（出力ノイズを増やさない）。

    Returns:
        UTF-8 Markdown 文字列。
    """
    # Markdown 側は「未取得」と「空」を区別する必要がないため None を [] に
    # 正規化（JSON renderer は None / [] を区別保持、Copilot 指摘）。
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
        # M2.4 (#92): findings §2.1 の dogfooding 観察に基づく診断行。
        # 「空状態の prime はそのまま LLM に渡しても情報量ゼロで、なぜ memory
        # が空か (DB share 未完了 / DB ID env 未設定 / 本当に空 / Notion 障害)
        # が分からない」問題への対応。各構成要素の状態を italic bullet で 1 行
        # ずつ列挙して原因切り分けを速くする。
        if diagnostics:
            lines.append("")
            for diag in diagnostics:
                # 各行は呼び出し側が既に整形済み（"Project Memory DB: 未設定
                # (env XXX)" 等）。renderer は italic 化と bullet 化のみ担当。
                # italic は `*...*` を使う: 診断行に含まれる env 変数名（例
                # `HOKUSAI_NOTION_PROJECT_MEMORY_DB_ID`）の `_` が `_..._`
                # 構文の終端と解釈されて表示が崩れるのを避けるため
                # （Issue #92 / M2.4 Copilot Round 1 指摘）。
                lines.append(f"- *{diag}*")
        lines.append("")
        # Prime v2 MVP-2: active context が空でも `--query` 指定時は
        # 検索結果セクションを出力する（過去 workflow のみヒットするケース）
        if query is not None:
            lines.append(f"## 検索結果（query: `{query}`）")
            lines.append("")
            if query_results:
                for r in query_results:
                    lines.extend(_render_query_result_entry(r))
                    lines.append("")
            else:
                lines.append("_該当する記録はありません_")
                lines.append("")
            return "\n".join(lines).rstrip() + "\n"
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

    # Work Items（ready / in_progress 両方を含む。Copilot 指摘で見出しを実態に整合）
    if work_items:
        lines.append("## Work Items（ready / in_progress）")
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

    # Gates（pending / blocked / open を含む。Copilot 指摘で見出しを実態に整合）
    if gates:
        lines.append("## Workflow Gates（pending / blocked / open）")
        lines.append("")
        for gate in gates:
            lines.extend(_render_gate_entry(gate))
            lines.append("")

    # Prime v2 MVP-2: query 検索結果（`--query "..."` 指定時のみ）
    if query is not None:
        lines.append(f"## 検索結果（query: `{query}`）")
        lines.append("")
        if query_results:
            for r in query_results:
                lines.extend(_render_query_result_entry(r))
                lines.append("")
        else:
            lines.append("_該当する記録はありません_")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _render_query_result_entry(result: dict[str, Any]) -> list[str]:
    """`SQLiteStore.search_prime_index()` の 1 件を Markdown 化する。

    形式（Prime v2 MVP-2 の最小フォーマット。詳細な引用整形は MVP-3 で
    citation セクションを設計する想定）:

        ### {title or (untitled)}
        **Source:** `{source_type}` / **Workflow:** `{workflow_id}`
        / **Phase:** `phase{N}` （あれば）
        / **Page:** `{notion_page_id}` （あれば）
        / **PR:** {pr_url} （あれば）
        / **File:** `{file_path}` （あれば）
        > body 各行
    """
    title = (result.get("title") or "").strip() or _UNTITLED
    out: list[str] = [f"### {title}"]
    meta: list[str] = [
        f"**Source:** `{result.get('source_type', '?')}`",
        f"**Workflow:** `{result.get('workflow_id', '?')}`",
    ]
    phase = result.get("phase")
    if isinstance(phase, int) and 1 <= phase <= 10:
        meta.append(f"**Phase:** `phase{phase}`")
    notion_page_id = result.get("notion_page_id")
    if notion_page_id:
        meta.append(f"**Page:** `{notion_page_id}`")
    pr_url = result.get("pr_url")
    if pr_url:
        meta.append(f"**PR:** {pr_url}")
    file_path = result.get("file_path")
    if file_path:
        meta.append(f"**File:** `{file_path}`")
    out.append(" / ".join(meta))
    body = (result.get("body") or "").strip()
    if body:
        for ln in body.splitlines() or [""]:
            out.append(f"> {ln}" if ln else ">")
    return out


def render_prime_json(
    *,
    workflow_id: str,
    profile: str | None,
    current_phase: str | None,
    memories: list[dict],
    work_items: list[dict] | None = None,
    review_issues: list[dict] | None = None,
    gates: list[dict] | None = None,
    diagnostics: list[str] | None = None,
    query: str | None = None,
    query_results: list[dict[str, Any]] | None = None,
) -> str:
    """active Memory + workgraph context を Agent / 自動処理向け JSON へ整形
    する（Workgraph 完成 / Issue #54）。

    Markdown 版との差分: 表示順 / 整形を行わず、各カテゴリ配列に raw 抜き出し
    dict を入れる。

    「未取得」（DB ID 未設定 / 取得 skip）と「取得済みだが 0 件」を呼び出し
    側で区別できるよう、`None` は JSON 上 `null` として保持し、`[]` は空配列
    として保持する（Copilot 指摘: 以前は `or []` で両者を潰していた）。
    呼び出し側が「未取得」を「未対応領域」として扱うか「0 件」と同等に扱うか
    を選択できる。

    `diagnostics` (M2.4 / #92): 各構成要素の「設定有無 / 取得結果」を呼び出し
    側で事前整形した文字列リスト。JSON では Markdown 側と異なり has_any の
    判定をせず常に key として残す（自動処理側が必要に応じて参照できるよう）。
    """
    payload: dict[str, Any] = {
        "workflow_id": workflow_id,
        "profile": profile,
        "current_phase": current_phase,
        "memories": [_extract_memory_dict(page) for page in memories],
        "work_items": (
            [_extract_work_item_dict(p) for p in work_items]
            if work_items is not None
            else None
        ),
        "review_issues": (
            [_extract_review_issue_dict(p) for p in review_issues]
            if review_issues is not None
            else None
        ),
        "gates": (
            [_extract_gate_dict(p) for p in gates]
            if gates is not None
            else None
        ),
        "diagnostics": diagnostics,
        # Prime v2 MVP-2: query 検索結果。v1 互換のため `--query` が未指定
        # なら query/query_results 両方 null。
        "query": query,
        "query_results": query_results,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 内部 helper
# ---------------------------------------------------------------------------


def _render_memory_entry(page: dict, memory_type: str) -> list[str]:
    name = _extract_title(page, "Name") or _UNTITLED
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
    title = _extract_title(page, "Title") or _UNTITLED
    status = _extract_select_name(page, "Status") or _UNKNOWN_STATUS
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
    title = _extract_title(page, "Title") or _UNTITLED
    severity = _extract_select_name(page, "Severity") or _UNKNOWN_STATUS
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
    name = _extract_title(page, "Name") or _UNTITLED
    status = _extract_select_name(page, "Status") or _UNKNOWN_STATUS
    gate_type = _extract_select_name(page, "Gate Type")
    # Required By Phase は Workflow Gates DB 上 number プロパティなので
    # number から読み取って `phase{n}` に整形する（Copilot 指摘で rich_text
    # 読み出しから修正）。1..10 の整数なら `phase{n}`、それ以外は数値そのまま。
    required_phase_num = _extract_number(page, "Required By Phase")
    out: list[str] = [f"### {name}"]
    meta = [f"**Status:** `{status}`"]
    if gate_type:
        meta.append(f"**Type:** `{gate_type}`")
    if required_phase_num is not None:
        if isinstance(required_phase_num, int) and 1 <= required_phase_num <= 10:
            meta.append(f"**Required by:** `phase{required_phase_num}`")
        else:
            meta.append(f"**Required by:** `{required_phase_num}`")
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
        # Required By Phase は number プロパティ。整数で JSON 出力する
        # （Copilot 指摘: rich_text 読み出しは常に空になる schema 不整合）
        "required_by_phase": _extract_number(page, "Required By Phase"),
        "description": _extract_rich_text(page, "Description"),
    }


def _extract_number(page: dict, prop_name: str):
    """Notion `number` プロパティの値を抜き出す（None / 数値）。"""
    prop = (page.get("properties") or {}).get(prop_name) or {}
    return prop.get("number")


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
    """Notion rich_text / title array の全要素を連結する（共通 helper 経由）。

    SonarCloud duplication 対策で `_text_helpers.join_rich_text_items` に
    実装を集約し、本モジュールでは wrapper として呼び出す。詳細は同 module
    の docstring 参照。
    """
    from ._text_helpers import join_rich_text_items
    return join_rich_text_items(items)


def _extract_select_name(page: dict, prop_name: str) -> str | None:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    sel = prop.get("select") or {}
    return sel.get("name")


def _extract_multi_select(page: dict, prop_name: str) -> list[str]:
    prop = (page.get("properties") or {}).get(prop_name) or {}
    items = prop.get("multi_select") or []
    return [opt.get("name") for opt in items if opt.get("name")]
