"""Review Issue / Work Item のローカル永続化ヘルパ (Step 5 第3スライス)

drain layer (`workflow.py`) は `pending_review_issues` / `pending_work_items`
を Notion dispatch 後に clear するため、これらは従来 transient だった。
本モジュールは drain の payload を **clear 前に** SQLite の durable table へ
冪等 upsert するヘルパを提供する。これにより recurring review issue 検出や
durable な workgraph edge を後続スライスで構築できる。

純関数的（I/O は渡された store 経由のみ）。各 payload の永続化は best-effort
で、1 件の失敗が他の永続化や drain 本体を止めない。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def persist_review_issue_payloads(
    store: Any, payloads: list[dict[str, Any]]
) -> int:
    """`pending_review_issues` の payload 群を `review_issues` へ upsert する。

    payload は dispatcher の review_issue_raised 形式
    (`dedupe_key` / `workflow_id` / `source` / `rule` / `file` / `message` /
     `repository` / `severity` / `status`)。

    dispatcher (`_handle_review_issue_raised`) は **source と message の両方**を
    必須とし、欠ける payload は skip する。ここでも同じ guard をかけ、dispatch
    されない malformed payload を durable table に残さない（PR #147 Copilot
    Round 2）。`dedupe_key` は明示があればそれを優先し、無い場合のみ dispatch
    (`_prepare_review_issue_dispatch`) と**同じ fallback**で算出する
    （fallback dispatch される payload も永続化する。Round 1）。

    Returns:
        永続化に成功した件数。
    """
    from .integrations.notion_dashboard.review_issues_db import build_dedupe_key

    persisted = 0
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        # dispatcher guard を mirror: source / message が無ければ dispatch されない
        if not payload.get("source") or not payload.get("message"):
            continue
        try:
            dedupe_key = payload.get("dedupe_key")
            if not dedupe_key:
                # dispatch と同一の fallback で stable key を生成（skip しない）。
                # build_dedupe_key も per-payload try の内側に置き、1 件の
                # malformed payload が他の永続化や drain を止めないようにする
                # (PR #147 Copilot Round 4)。
                dedupe_key = build_dedupe_key(
                    source=str(payload.get("source") or ""),
                    rule=payload.get("rule"),
                    file=payload.get("file"),
                    message=str(payload.get("message") or ""),
                    repository=payload.get("repository"),
                    workflow_id=payload.get("workflow_id") or None,
                )
            store.upsert_review_issue(
                dedupe_key=dedupe_key,
                workflow_id=payload.get("workflow_id"),
                source=payload.get("source"),
                rule=payload.get("rule"),
                file=payload.get("file"),
                message=payload.get("message"),
                repository=payload.get("repository"),
                severity=payload.get("severity"),
                status=payload.get("status"),
            )
            persisted += 1
        except Exception as e:  # best-effort: drain 本体を止めない
            logger.debug(f"review_issue のローカル永続化を抑制: {e}")
    return persisted


def persist_work_item_payloads(
    store: Any, payloads: list[dict[str, Any]]
) -> int:
    """`pending_work_items` の payload 群を `work_items` へ upsert する。

    payload は work_item_upsert 形式 (`workflow_id` / `title` / `phase` /
    `status` / `description`、内部 marker `_event` 等)。

    `dedupe_key` は dispatch (`_prepare_work_item_dispatch`) と同じ優先順位で
    決定する: **明示 `dedupe_key` があればそれを優先**し（status/claim/
    lease_release 等が custom key を渡す契約）、無い場合のみ
    `build_dedupe_key(workflow_id, phase, title)` で算出する。これにより durable
    row が Notion/outbox の identity と乖離しない（PR #147 Copilot Round 2）。
    明示 key も title も無い payload は同定できないため skip する。

    Returns:
        永続化に成功した件数。
    """
    from .integrations.notion_dashboard.work_items_db import build_dedupe_key

    persisted = 0
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        title = payload.get("title")
        workflow_id = payload.get("workflow_id")
        phase = payload.get("phase")
        dedupe_key = payload.get("dedupe_key")
        if not dedupe_key and not title:
            # 明示 key も title も無ければ同定できない
            continue
        try:
            if not dedupe_key:
                # build_dedupe_key は per-payload try の内側に置く。dispatcher
                # (`_prepare_work_item_dispatch`) と同じく title を str cast して
                # から hash し、非 str title の `.strip()` 例外が drain 全体を
                # 止めないようにする (PR #147 Copilot Round 4)。
                dedupe_key = build_dedupe_key(
                    workflow_id=workflow_id, phase=phase, title=str(title)
                )
            # 保存 title も dispatch と同じく str 正規化する。非 str title
            # (dict 等) を生のまま渡すと sqlite3 が bind できず行が skip され
            # transient に残るため (PR #147 Copilot Round 5)。明示 key のみで
            # title が無い event は None を維持する。
            normalized_title = None if title is None else str(title)
            store.upsert_work_item(
                dedupe_key=dedupe_key,
                workflow_id=workflow_id,
                title=normalized_title,
                phase=phase,
                status=payload.get("status"),
                description=payload.get("description"),
            )
            persisted += 1
        except Exception as e:  # best-effort
            logger.debug(f"work_item のローカル永続化を抑制: {e}")
    return persisted


def persist_verification_captures(
    store: Any, workflow_id: str | None, verification_errors: list[dict[str, Any]]
) -> int:
    """Phase 6 verification 失敗を eval_captures へ best-effort 永続化する。

    `build_verification_captures` で capture dict を作り `record_eval_capture`
    へ流す。1 件の失敗が drain 本体を止めない（Step 4 第2スライス）。
    """
    from .eval_capture import build_verification_captures

    persisted = 0
    try:
        captures = build_verification_captures(workflow_id, verification_errors)
    except Exception as e:
        logger.debug(f"verification capture の構築を抑制: {e}")
        return 0
    for capture in captures:
        try:
            store.record_eval_capture(**capture)
            persisted += 1
        except Exception as e:  # best-effort
            logger.debug(f"eval_capture の永続化を抑制: {e}")
    return persisted
