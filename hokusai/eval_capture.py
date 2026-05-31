"""Eval Capture / export の土台 (Step 4 第1スライス / roadmap Step 4)

prompt や workflow 品質の退行検知に向けた eval fixture を扱う。第1スライスは
**非侵襲**に徹し、LLM Gateway interceptor が既に `audit_logs` へ記録している
LLM 呼び出しの hash/length/metadata を eval fixture として export する層を提供
する（LangGraph phase へのフックや専用 capture テーブルは後続スライス）。

保存方針は LLM Gateway と同じく **prompt 本文を持たず** hash / length /
metadata のみ（secret / PII を fixture にこぼさない）。`compute_content_digest`
は将来の明示 capture（verification / review 結果）でも同じ digest を使うため
の共通ユーティリティ。
"""
from __future__ import annotations

import hashlib
from typing import Any

# LLM Gateway interceptor が audit_logs に書く action 名（固定）。
LLM_DECISION_ACTION = "llm_gateway_decision"


def compute_content_digest(content: str) -> dict[str, Any]:
    """content の sha256 16 桁 hex と length を返す（本文は保存しない）。

    LLM Gateway interceptor と同一の digest 方式（`sha256(...).hexdigest()[:16]`
    + `len`）。eval fixture / capture で secret・PII を残さず内容の同一性だけ
    比較できるようにするための共通ユーティリティ。
    """
    text = content if isinstance(content, str) else str(content)
    return {
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "length": len(text),
    }


def audit_row_to_fixture(row: dict[str, Any]) -> dict[str, Any] | None:
    """`list_audit_logs` の 1 行（LLM Gateway decision）を eval fixture に変換する。

    `audit_logs.details_json`（`list_audit_logs` が parse 済み `details` に格納）
    には interceptor が書いた `context`（provider/model/purpose/phase/metadata）、
    `prompt_hash`、`prompt_length`、`decision`、`policy_hits` が入る。これを
    退行検知で扱いやすい平坦な fixture に整形する。

    action が `llm_gateway_decision` でない / details 不正な行は None を返す
    （呼び出し側で filter）。
    """
    if row.get("action") != LLM_DECISION_ACTION:
        return None
    details = row.get("details")
    if not isinstance(details, dict):
        return None
    context = details.get("context") or {}
    return {
        "kind": "llm_call",
        "audit_id": row.get("id"),
        "workflow_id": row.get("workflow_id"),
        "phase": row.get("phase"),
        "provider": context.get("provider"),
        "model": context.get("model"),
        "purpose": context.get("purpose"),
        "input_hash": details.get("prompt_hash"),
        "input_length": details.get("prompt_length"),
        "decision": details.get("decision"),
        "policy_hits": details.get("policy_hits") or [],
        "created_at": row.get("created_at"),
    }


def audit_rows_to_fixtures(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """audit_logs 行のリストを eval fixture のリストへ変換する（None は除外）。"""
    out = []
    for row in rows or []:
        fixture = audit_row_to_fixture(row)
        if fixture is not None:
            out.append(fixture)
    return out
