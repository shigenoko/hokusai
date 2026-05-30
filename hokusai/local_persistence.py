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
     `repository` / `severity` / `status`)。`dedupe_key` が無い payload も
    skip せず、dispatch (`_prepare_review_issue_dispatch`) と**同じ fallback**
    `build_dedupe_key(source, rule, file, message, repository, workflow_id)` で
    決定的に算出する（fallback で dispatch される payload が永続化されず
    transient に残らないようにする。PR #147 Copilot Round 1）。

    Returns:
        永続化に成功した件数。
    """
    from .integrations.notion_dashboard.review_issues_db import build_dedupe_key

    persisted = 0
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        dedupe_key = payload.get("dedupe_key")
        if not dedupe_key:
            # dispatch と同一の fallback で stable key を生成（skip しない）
            dedupe_key = build_dedupe_key(
                source=str(payload.get("source") or ""),
                rule=payload.get("rule"),
                file=payload.get("file"),
                message=str(payload.get("message") or ""),
                repository=payload.get("repository"),
                workflow_id=payload.get("workflow_id") or None,
            )
        try:
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
    `status` / `description`、内部 marker `_event` 等)。`dedupe_key` は payload
    に無いため、dispatch と同じ `build_dedupe_key(workflow_id, phase, title)`
    で決定的に算出する。title が無い payload は skip する。

    Returns:
        永続化に成功した件数。
    """
    from .integrations.notion_dashboard.work_items_db import build_dedupe_key

    persisted = 0
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        title = payload.get("title")
        if not title:
            continue
        workflow_id = payload.get("workflow_id")
        phase = payload.get("phase")
        try:
            dedupe_key = build_dedupe_key(
                workflow_id=workflow_id, phase=phase, title=title
            )
            store.upsert_work_item(
                dedupe_key=dedupe_key,
                workflow_id=workflow_id,
                title=title,
                phase=phase,
                status=payload.get("status"),
                description=payload.get("description"),
            )
            persisted += 1
        except Exception as e:  # best-effort
            logger.debug(f"work_item のローカル永続化を抑制: {e}")
    return persisted
