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


def compute_content_digest(content: Any) -> dict[str, Any]:
    """content の sha256 16 桁 hex と length を返す（本文は保存しない）。

    str 以外が渡された場合は `str()` で文字列化してから digest を取る
    （annotation を `Any` にして実装と整合させる。PR #151 Copilot Round 1）。

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
    # context が dict でない (None / 文字列 / 破損) ケースでも .get で落ちない
    # よう空 dict 扱いにする (PR #151 Copilot Round 1)。
    context = details.get("context")
    if not isinstance(context, dict):
        context = {}
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


def build_capture_key(
    *,
    workflow_id: str | None,
    phase: int | None,
    kind: str,
    label: str,
    output_hash: str,
) -> str:
    """eval capture の決定的 dedupe key を生成する（sha256 16 桁 hex）。

    同一 (workflow / phase / kind / label / output) の観測を 1 行にまとめ、
    出力が変われば別 key = 別 fixture（失敗→修正の履歴が別行で残る）。
    """
    parts = "\x1f".join((
        workflow_id or "",
        "" if phase is None else str(phase),
        kind or "",
        label or "",
        output_hash or "",
    ))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


def build_verification_captures(
    workflow_id: str | None,
    verification_errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Phase 6 の `verification_errors` を eval capture dict 群へ変換する。

    各失敗エントリ（`repository` / `command` / `error_output` /
    `full_output_hash`）から 1 capture を作る。入力＝command、出力＝error_output
    の digest（本文非保存、`full_output_hash` があれば優先）。status="fail"。
    `record_eval_capture(**capture)` にそのまま渡せる dict を返す。

    注意: `VerificationErrorEntry.command` は実コマンド文字列ではなく
    **コマンド種別**（build / test / lint）である（hokusai/state.py 参照）。
    label / input digest もこの種別文字列に基づく。
    """
    captures: list[dict[str, Any]] = []
    for entry in verification_errors or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("success"):
            continue
        repo = entry.get("repository") or ""
        command = entry.get("command") or ""
        error_output = entry.get("error_output") or ""
        label = f"{repo}:{command}" if repo else str(command)
        # output digest は full_output_hash（truncate 前の全文 hash）を優先。
        # その場合 length は手元に全文が無く不明なので None にする（hash 対象 =
        # 全文、length = truncate 後 という不整合を避ける。PR #152 Copilot
        # Round 2）。full_output_hash が無ければ error_output 自身から hash +
        # length を取り、両者を整合させる。
        full_hash = entry.get("full_output_hash")
        if full_hash:
            output_hash = full_hash
            output_length = None
        else:
            out_digest = compute_content_digest(error_output)
            output_hash = out_digest["hash"]
            output_length = out_digest["length"]
        in_digest = compute_content_digest(command)
        captures.append({
            "capture_key": build_capture_key(
                workflow_id=workflow_id, phase=6, kind="verification",
                label=label, output_hash=output_hash,
            ),
            "workflow_id": workflow_id,
            "phase": 6,
            "kind": "verification",
            "label": label,
            "input_hash": in_digest["hash"],
            "input_length": in_digest["length"],
            "output_hash": output_hash,
            "output_length": output_length,
            "status": "fail",
            "metadata": {"repository": repo, "command": command},
        })
    return captures


def fixture_identity(fixture: dict[str, Any]) -> str:
    """fixture の安定した同定キーを返す（eval gate の diff 用）。

    - capture（verification 等）は `capture_key`（workflow + phase + label +
      output_hash を内包）→ 出力が変われば別 identity = add/remove で現れる。
    - llm_call は `audit_id`。
    - どちらも無い場合は kind/workflow/phase/input_hash の合成。
    """
    ck = fixture.get("capture_key")
    if ck:
        return f"capture:{ck}"
    aid = fixture.get("audit_id")
    if aid is not None:
        return f"audit:{aid}"
    return "fx:" + "\x1f".join(
        str(fixture.get(k))
        for k in ("kind", "workflow_id", "phase", "input_hash")
    )


def build_eval_gate_result(
    baseline: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> dict[str, Any]:
    """baseline fixture 集合と現 fixture 集合を diff して退行を検出する。

    `fixture_identity` で両者を索引し、現側のみに在る fixture を `added`、
    baseline 側のみに在るものを `removed` とする。`status="fail"` の
    `added` を **regressions**（新たな失敗）、`status="fail"` の `removed` を
    **improvements**（解消された失敗）とする。決定的・I/O なし。

    Returns:
        {baseline_count, current_count, added, removed, regressions,
         improvements}
    """
    base_by = {fixture_identity(f): f for f in baseline or []}
    cur_by = {fixture_identity(f): f for f in current or []}
    added = [cur_by[k] for k in cur_by if k not in base_by]
    removed = [base_by[k] for k in base_by if k not in cur_by]
    return {
        "baseline_count": len(base_by),
        "current_count": len(cur_by),
        "added": added,
        "removed": removed,
        "regressions": [f for f in added if f.get("status") == "fail"],
        "improvements": [f for f in removed if f.get("status") == "fail"],
    }


def eval_capture_to_fixture(row: dict[str, Any]) -> dict[str, Any]:
    """`list_eval_captures` の 1 行を export 用 fixture 形へ整形する。"""
    return {
        "kind": row.get("kind"),
        "capture_key": row.get("capture_key"),
        "workflow_id": row.get("workflow_id"),
        "phase": row.get("phase"),
        "label": row.get("label"),
        "input_hash": row.get("input_hash"),
        "input_length": row.get("input_length"),
        "output_hash": row.get("output_hash"),
        "output_length": row.get("output_length"),
        "status": row.get("status"),
        "metadata": row.get("metadata"),
        "created_at": row.get("created_at"),
    }
