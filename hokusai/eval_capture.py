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


def build_review_captures(
    workflow_id: str | None,
    review_issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """review issue を eval capture dict 群（kind="review"）へ変換する。

    入力は review issue の dict（drain の `pending_review_issues` payload でも、
    durable `review_issues` 行でも同じ `source` / `rule` / `file` / `message` /
    `repository` / `status` フィールドを持つ）。`source="verification_failure"`
    は `build_verification_captures`（kind="verification"）が担当するので
    **除外**し、Phase 7 の `final_review` 等コードレビュー指摘のみ kind="review"
    で取り込む。これにより `eval gate` が review rule の退行も拾える。

    output digest は message（指摘内容）から取り、本文は保存しない。phase は
    最終レビュー相当の 7。`record_eval_capture(**capture)` に渡せる dict を返す。
    """
    captures: list[dict[str, Any]] = []
    for ri in review_issues or []:
        if not isinstance(ri, dict):
            continue
        # dispatcher / persist_review_issue_payloads と同じ guard:
        # source + message が無い malformed payload は capture しない
        # （source 無しが eval gate の regression に誤検知されるのを防ぐ。
        #  PR #160 Copilot Round 1）。
        if not ri.get("source") or not ri.get("message"):
            continue
        if ri.get("source") == "verification_failure":
            continue  # verification capture が担当（二重取り込み回避）
        message = ri.get("message")
        rule = ri.get("rule") or ""
        repo = ri.get("repository") or ""
        label = f"{repo}:{rule}" if repo and rule else (
            rule or repo or str(ri.get("source") or "review")
        )
        out = compute_content_digest(message)
        # gate の語彙にマップ: 未解決 (open / 未設定) の review 指摘は失敗相当
        # として "fail" にし、新規発生時に eval gate の regression として拾える
        # ようにする。解決済みはそのまま (resolved)。元 status は metadata に残す。
        review_status = ri.get("status")
        gate_status = "fail" if review_status in (None, "open") else review_status
        captures.append({
            "capture_key": build_capture_key(
                workflow_id=workflow_id, phase=7, kind="review",
                label=label, output_hash=out["hash"],
            ),
            "workflow_id": workflow_id,
            "phase": 7,
            "kind": "review",
            "label": label,
            "input_hash": None,
            "input_length": None,
            "output_hash": out["hash"],
            "output_length": out["length"],
            "status": gate_status,
            "metadata": {
                "source": ri.get("source"), "rule": rule,
                "file": ri.get("file"), "repository": repo,
                "review_status": review_status,
            },
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
    *,
    window_start: str | None = None,
) -> dict[str, Any]:
    """baseline fixture 集合と現 fixture 集合を diff して退行を検出する。

    `fixture_identity` で両者を索引し、現側のみに在る fixture を `added`、
    baseline 側のみに在るものを `removed` とする。`status="fail"` の
    `added` を **regressions**（新たな失敗）、`status="fail"` の `removed` を
    **improvements**（解消された失敗）とする。決定的・I/O なし。

    `window_start` (ISO) を渡すと、現側集合が `--limit` truncation 等で
    観測ウィンドウ下限を持つ場合に、それより古い baseline fixture を `removed`/
    `improvements` から除外し `out_of_window` に分類する。これにより「単に
    limit で現側に現れなかっただけ」の fixture を「解消された失敗」と
    誤検知しない（PR #154 Copilot Round 1）。

    Returns:
        {baseline_count, current_count, added, removed, regressions,
         improvements, out_of_window}
    """
    base_by = {fixture_identity(f): f for f in baseline or []}
    cur_by = {fixture_identity(f): f for f in current or []}
    added = [cur_by[k] for k in cur_by if k not in base_by]

    removed: list[dict[str, Any]] = []
    out_of_window: list[dict[str, Any]] = []
    for k in base_by:
        if k in cur_by:
            continue
        f = base_by[k]
        created = f.get("created_at")
        # 観測ウィンドウ下限より古い baseline fixture は「現側に現れなかった」
        # ことが limit truncation 由来かもしれず、解消/削除と断定できない。
        if window_start is not None and created is not None \
                and created < window_start:
            out_of_window.append(f)
        else:
            removed.append(f)
    return {
        "baseline_count": len(base_by),
        "current_count": len(cur_by),
        "added": added,
        "removed": removed,
        "out_of_window": out_of_window,
        "regressions": [f for f in added if f.get("status") == "fail"],
        "improvements": [f for f in removed if f.get("status") == "fail"],
    }


def fixture_input_identity(fixture: dict[str, Any]) -> str:
    """fixture の「入力」同定キーを返す（output を含まない。eval replay 用）。

    `fixture_identity` は capture_key（output_hash を内包）や audit_id で
    同定するため、**同じ入力でも出力が変われば別 identity** になる。replay は
    「同じ入力に対する出力が変わったか（drift）」を見たいので、output を除いた
    入力次元で同定する別キーを使う。

    - capture（verification / review 等）: kind / workflow_id / phase / label
      / input_hash（label は repo:command や repo:rule の入力種別。input_hash
      も含め、同 label でも記録入力が異なる fixture を別入力として扱う。
      output_hash は含めない。PR #163 Copilot Round 1）
    - llm_call: provider / model / purpose / input_hash（prompt 入力の同一性）
    - その他: kind / workflow_id / phase / input_hash
    """
    if fixture.get("capture_key"):
        return "cap:" + "\x1f".join(
            str(fixture.get(k))
            for k in ("kind", "workflow_id", "phase", "label", "input_hash")
        )
    if fixture.get("audit_id") is not None:
        return "llm:" + "\x1f".join(
            str(fixture.get(k))
            for k in ("provider", "model", "purpose", "input_hash")
        )
    return "fx:" + "\x1f".join(
        str(fixture.get(k))
        for k in ("kind", "workflow_id", "phase", "input_hash")
    )


def _fixture_output_token(fixture: dict[str, Any]) -> Any:
    """fixture の「出力」を表す比較トークンを返す。

    capture 系は `output_hash`（出力本文の digest）。llm_call は output_hash を
    持たないため、gateway の `decision` を出力相当として使う。
    """
    h = fixture.get("output_hash")
    if h is not None:
        return h
    return fixture.get("decision")


def _fixture_recency(fixture: dict[str, Any]) -> str:
    """fixture の「最後に観測された時刻」を表す recency キーを返す。

    eval_captures は `record_eval_capture` が同一 `capture_key`（= 同一 input
    かつ同一 output）の再観測で `created_at` を初回値のまま保ち `updated_at`
    のみ進める。よって「同一入力の最新出力」を選ぶ recency は `created_at`
    ではなく `updated_at` が正しい（古い output が再観測されたら updated_at が
    進み、現在その入力が産む出力であることを示す。PR #163 Copilot Round 1）。
    `updated_at` が無い fixture（baseline 旧 export / llm_call）は `created_at`
    にフォールバックする。
    """
    return fixture.get("updated_at") or fixture.get("created_at") or ""


def build_eval_replay_result(
    baseline: list[dict[str, Any]],
    current: list[dict[str, Any]],
    *,
    window_start: str | None = None,
) -> dict[str, Any]:
    """baseline fixture を現 fixture へ「replay」し、入力ごとの出力 drift を見る。

    実 LLM を再実行する侵襲的 replay ではなく、`eval export` で保存した baseline
    fixture の各**入力**（`fixture_input_identity`）について、現 DB が保持する
    同一入力の fixture と**出力**（`_fixture_output_token`）を突き合わせ、決定的に
    分類する:

    - `stable`: 同一入力が現側にも在り、出力が一致（退行なし）
    - `drift`: 同一入力が現側に在るが、出力が変化（prompt / review rule 変更等の
      退行 or 改善の signal。baseline / current の出力を併記）
    - `missing`: baseline の入力が現側に無い（再観測されていない）
    - `out_of_window`: 現側に無いが、recency（updated_at→created_at）が
      `window_start` より古く `--limit` truncation で現側ウィンドウから外れた
      だけかもしれない baseline 入力（missing と断定しない。`eval gate` の
      out_of_window と同方針。`window_start` は呼び出し側が recency 軸で
      渡す。PR #163 Copilot Round 1 / Round 2）

    同一入力に複数 fixture（失敗→修正で別 output の行）がある場合は、各側で
    `_fixture_recency`（updated_at→created_at。同点は `fixture_identity`）の
    最大を採り、最後に観測された出力で比較する。I/O なし・決定的。
    `eval gate`（集合の add/remove 退行）と相補的に、**同一入力の出力変化**を
    捉える。

    Returns:
        {baseline_count, current_count, stable, drift, missing, out_of_window}
    """
    def _index(fixtures: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        by: dict[str, dict[str, Any]] = {}
        for f in fixtures or []:
            iid = fixture_input_identity(f)
            prev = by.get(iid)
            if prev is None:
                by[iid] = f
                continue
            fk = (_fixture_recency(f), fixture_identity(f))
            pk = (_fixture_recency(prev), fixture_identity(prev))
            if fk >= pk:
                by[iid] = f
        return by

    base_by = _index(baseline)
    cur_by = _index(current)
    stable: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    out_of_window: list[dict[str, Any]] = []
    for iid in base_by:
        bf = base_by[iid]
        cf = cur_by.get(iid)
        if cf is None:
            # 現側に無い baseline 入力。観測ウィンドウ下限 (window_start) より
            # 古ければ、現側に現れないのは limit truncation 由来かもしれず
            # 「再観測されていない (missing)」と断定できない。比較軸は recency
            # と統一する: window_start も呼び出し側で recency (updated_at→
            # created_at) 基準に揃えて渡す（created_at と updated_at の軸混在を
            # 避ける。PR #163 Copilot Round 2）。
            recency = _fixture_recency(bf)
            if (window_start is not None and recency
                    and recency < window_start):
                out_of_window.append(bf)
            else:
                missing.append(bf)
        elif _fixture_output_token(bf) == _fixture_output_token(cf):
            stable.append(cf)
        else:
            drift.append({
                "input_identity": iid,
                "kind": cf.get("kind"),
                "label": cf.get("label") or cf.get("purpose"),
                "baseline_output": _fixture_output_token(bf),
                "current_output": _fixture_output_token(cf),
                "baseline_status": bf.get("status"),
                "current_status": cf.get("status"),
            })
    return {
        "baseline_count": len(base_by),
        "current_count": len(cur_by),
        "stable": stable,
        "drift": drift,
        "missing": missing,
        "out_of_window": out_of_window,
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
        # updated_at は eval replay の recency キー（同一入力の最新出力を選ぶ）
        # に使う。created_at は初回 insert 値で固定されるため（PR #163 Round 1）。
        "updated_at": row.get("updated_at"),
    }
