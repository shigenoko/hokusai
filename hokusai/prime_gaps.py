"""Prime v2 MVP-4: gap analysis (決定的検出)

docs/design-prime-v2.md §6.1 / §8.1 MVP-4 のスコープで、現在の workflow 状態
から「次の phase に進むのに不足している情報」を **決定的に** (LLM 不要で)
検出する。

MVP-4 で実装する 3 種:
- `unresolved_review_issue_open`: 起点 workflow に紐づく Review Issue で
  Status=Open
- `notion_outbox_pending`: `notion_sync_outbox` に pending 行あり
- `audit_log_silence`: LLM Gateway 有効なのに `audit_logs` 行 0 件
  (interceptor 経路が無効になっている疑い)

残り 4 種 (`missing_verification_command` / `pending_gate_blocking` /
`phase4_plan_missing` / `supersedes_chain_broken`) は MVP-5 以降。

設計方針:
- 各 detector は純関数 (副作用なし、決定的) で、SQLiteStore / Notion data /
  config を受け取って `Gap` のリストを返す
- `LLM` 呼び出しは行わない (Phase 2 enforcement との一貫性、§3.3)
- 検出ロジックは「閾値判定」「DB 存在判定」のような単純なものに限る
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Gap:
    """gap analysis で検出される 1 件の不足項目。

    Attributes:
        kind: gap の種別 (`unresolved_review_issue_open` 等の固定 ID)
        phase: 関連 phase (あれば、整数 1..10。なければ None)
        detail: 人間向けの説明文 (Markdown / JSON 両方で使う)
    """
    kind: str
    detail: str
    phase: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "phase": self.phase, "detail": self.detail}


def detect_unresolved_review_issues(
    review_issues: list[dict] | None,
) -> list[Gap]:
    """起点 workflow に紐づく Review Issue で Status=Open のものを検出する。

    Notion の Review Issues DB 経路 (`list_open_review_issues_for_workflow`)
    は既に「open のみ」を返す設計なので、ここでは渡された全件を gap として
    記録する。Status を別途見て filter する設計ではない (二重 filter 防止)。

    Args:
        review_issues: prime CLI で取得済みの review issue page list (None なら
            未取得＝検出 skip)

    Returns:
        Gap list (各 1 件 = 1 review issue)
    """
    if not review_issues:
        return []
    gaps: list[Gap] = []
    for issue in review_issues:
        title = _extract_text(issue, "Title") or "(untitled review issue)"
        severity = _extract_select(issue, "Severity") or "?"
        gaps.append(Gap(
            kind="unresolved_review_issue_open",
            detail=f"未解消の review issue: '{title}' (severity={severity})",
            phase=None,
        ))
    return gaps


def detect_notion_outbox_pending(
    store: Any, workflow_id: str | None = None
) -> list[Gap]:
    """`notion_sync_outbox` の pending 件数を gap として記録する。

    現状の `SQLiteStore.count_notion_sync_pending()` は global 件数を返す
    (workflow 絞りなし)。1 件以上あれば「Notion 同期が滞っている」状態
    として 1 件の Gap を返す。0 件なら空リスト。

    Args:
        store: SQLiteStore インスタンス
        workflow_id: (reserved) 将来 workflow 絞り版 helper を追加した際に
            利用する。MVP-4 段階では未使用。

    Returns:
        Gap list (0 or 1 件)
    """
    try:
        pending = store.count_notion_sync_pending()
    except Exception:
        # store API 失敗時は best-effort で空を返す (Prime 本来の表示を
        # 阻害しない設計、docs/design-prime-v2.md §8.1 と整合)
        return []
    if pending <= 0:
        return []
    return [Gap(
        kind="notion_outbox_pending",
        detail=(
            f"notion_sync_outbox に {pending} 件の pending 行があります。"
            f"Notion 同期が完了するか、Operations Console の同期再送ボタンで "
            f"drain してください"
        ),
        phase=None,
    )]


def detect_audit_log_silence(
    store: Any, llm_gateway_enabled: bool, workflow_id: str | None = None
) -> list[Gap]:
    """LLM Gateway が有効なのに `audit_logs` が空 (interceptor 経路が無効化
    されている疑い) を検出する。

    判定:
    - LLM Gateway が disable なら検出 skip (gap なし)
    - workflow_id 指定があれば、その workflow_id の audit_logs を見る。
      0 件なら gap (interceptor を経由する phase まだ到達していないだけの
      可能性もあるが、注意喚起として記録する)
    - workflow_id なしなら全体件数を見る

    Args:
        store: SQLiteStore インスタンス
        llm_gateway_enabled: 現 config の llm_gateway.enabled (フラグ)
        workflow_id: 絞り込み workflow_id (None なら全体)

    Returns:
        Gap list (0 or 1 件)
    """
    if not llm_gateway_enabled:
        return []
    try:
        # list_audit_logs(workflow_id=None) は全件、limit=1 で「1 件でもあれば
        # 静音ではない」を判定。limit=1 で空ならほぼ確実に「audit silence」
        rows = store.list_audit_logs(workflow_id=workflow_id, limit=1)
    except Exception:
        return []
    if rows:
        return []
    scope = f"workflow {workflow_id}" if workflow_id else "全 workflow"
    return [Gap(
        kind="audit_log_silence",
        detail=(
            f"LLM Gateway は enabled だが、{scope} の audit_logs は 0 件です。"
            f"interceptor 経路が無効化されている疑いがあります "
            f"(profile config / env override / phase node 配線を確認)"
        ),
        phase=None,
    )]


# 計画フェーズ (Phase 4) を通過したとみなす最小 phase。
# current_phase == 4 は「計画を生成している最中」で work_plan が未確定でも
# 正常なため、誤検出を避けて Phase 5 (implement) 以降を「計画があるべき」
# 状態とする。docs/design-prime-v2.md §6.1 の「current_phase ≥ 4」を、
# 実装上の transient 状態を除外する形で >= 5 に精緻化したもの。
_PLAN_EXPECTED_MIN_PHASE = 5


def detect_phase4_plan_missing(state: dict | None) -> list[Gap]:
    """Phase 5 以降に到達しているのに Phase 4 の plan (`work_plan`) が空、を
    検出する (docs/design-prime-v2.md §6.1 `phase4_plan_missing`)。

    workflow state のみで完結する決定的検出 (Notion 非依存)。

    判定:
    - `state` が None / current_phase 不明なら skip
    - `current_phase < 5` (計画フェーズ通過前) なら skip — Phase 4 実行中に
      work_plan が未確定なのは正常なので誤検出を避ける
    - `current_phase >= 5` かつ `work_plan` が空/空白のみなら gap

    Phase 4 は意図的に skip される運用もある (既存ワークフローで Phase 4
    を経ずに implement へ進むケース) ため、本 gap は「計画なしで実装フェーズ
    に到達している」という注意喚起であり、必ずしもエラーではない。

    Args:
        state: workflow state dict (`SQLiteStore.load_workflow` の戻り値)

    Returns:
        Gap list (0 or 1 件)
    """
    if not isinstance(state, dict):
        return []
    raw_phase = state.get("current_phase")
    # current_phase は int (1..10) 想定。文字列 "phase5" 等は範囲外として扱う
    if not isinstance(raw_phase, int):
        return []
    if raw_phase < _PLAN_EXPECTED_MIN_PHASE:
        return []
    work_plan = state.get("work_plan")
    if isinstance(work_plan, str) and work_plan.strip():
        return []
    return [Gap(
        kind="phase4_plan_missing",
        detail=(
            f"current_phase={raw_phase} (Phase 5 implement 以降) に到達して "
            f"いますが Phase 4 の開発計画 (work_plan) が空です。Phase 4 が "
            f"意図的に skip された可能性もありますが、計画なしで実装が進んで "
            f"いないか確認してください"
        ),
        phase=4,
    )]


def detect_supersedes_chain_broken(
    store: Any, state: dict | None
) -> list[Gap]:
    """Supersedes 元 workflow の Notion 同期が永続失敗している (chain 切れ) を
    検出する (docs/design-prime-v2.md §6.1 `supersedes_chain_broken`)。

    `state["supersedes_workflow_id"]` で指定された旧 workflow の
    `workflow_started` イベントが `notion_sync_errors` に flush 済みなら、
    旧 workflow の Workflows DB レコードが存在せず Supersedes リレーションを
    張れない (handover_note の世代遡及が途切れる) 疑いがある。

    SQLite (`notion_sync_errors`) + state のみで完結する決定的検出
    (live Notion 呼び出しなし)。

    Args:
        store: SQLiteStore インスタンス (`has_failed_workflow_started` を使う)
        state: workflow state dict

    Returns:
        Gap list (0 or 1 件)
    """
    if not isinstance(state, dict):
        return []
    superseded = state.get("supersedes_workflow_id")
    if not superseded or not isinstance(superseded, str):
        return []
    try:
        failed = store.has_failed_workflow_started(superseded)
    except Exception:
        return []
    if not failed:
        return []
    return [Gap(
        kind="supersedes_chain_broken",
        detail=(
            f"supersedes 元 workflow '{superseded}' の workflow_started 同期が "
            f"notion_sync_errors に永続失敗として記録されています。旧 workflow の "
            f"Workflows DB レコードが存在せず Supersedes リレーション / handover "
            f"遡及が途切れている疑いがあります"
        ),
        phase=None,
    )]


def collect_gaps(
    *,
    store: Any,
    review_issues: list[dict] | None,
    llm_gateway_enabled: bool,
    workflow_id: str | None = None,
    state: dict | None = None,
) -> list[Gap]:
    """MVP-4 (3 種) + MVP-5 (2 種) の detector をまとめて呼ぶ便宜 helper。

    各 detector の例外は best-effort で握りつぶす (Prime 本来の表示を
    阻害しないため、docs/design-prime-v2.md §8.1 と整合)。

    MVP-4 (Notion/SQLite-backed): unresolved_review_issue_open /
    notion_outbox_pending / audit_log_silence
    MVP-5 (state/SQLite-backed, Notion 非依存): phase4_plan_missing /
    supersedes_chain_broken
    """
    gaps: list[Gap] = []
    gaps.extend(detect_unresolved_review_issues(review_issues))
    gaps.extend(detect_notion_outbox_pending(store, workflow_id=workflow_id))
    gaps.extend(detect_audit_log_silence(
        store, llm_gateway_enabled, workflow_id=workflow_id
    ))
    gaps.extend(detect_phase4_plan_missing(state))
    gaps.extend(detect_supersedes_chain_broken(store, state))
    return gaps


# ---------------------------------------------------------------------------
# 内部 helper (Notion page property の薄い取り出し)
# ---------------------------------------------------------------------------

def _extract_text(page: dict, prop_name: str) -> str:
    """Notion page の title / rich_text プロパティから plain_text を取り出す。"""
    if not isinstance(page, dict):
        return ""
    prop = (page.get("properties") or {}).get(prop_name)
    if not isinstance(prop, dict):
        return ""
    for key in ("title", "rich_text"):
        items = prop.get(key)
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                return str(first.get("plain_text") or "")
    return ""


def _extract_select(page: dict, prop_name: str) -> str | None:
    """Notion page の select プロパティから name を取り出す。"""
    if not isinstance(page, dict):
        return None
    prop = (page.get("properties") or {}).get(prop_name)
    if not isinstance(prop, dict):
        return None
    sel = prop.get("select")
    if isinstance(sel, dict):
        name = sel.get("name")
        if isinstance(name, str):
            return name
    return None
