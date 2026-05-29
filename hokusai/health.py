"""Runtime 運用ヘルス集約 (Step 2 / Doctor-Status 一画面化の共通 handler)

`hokusai profile doctor --deep` (CLI) と Operations Console の双方から呼ぶ
共通の health 計算関数を提供する。SQLite から Notion sync outbox の滞留
件数を読み、`prime_gaps.collect_gaps()` を共通 sink として運用ギャップを
集約する。

設計方針:
- 純関数 (I/O は渡された store 経由のみ、live Notion 呼び出しなし)
- 例外は内部で握りつぶし `error` フィールドに記録する best-effort
  (呼び出し側の表示 / パネルを壊さない)
- 返す dict は `hokusai profile doctor --output json` の `runtime_health`
  キーと同一スキーマ (CLI / Console / 機械処理で一貫)
"""
from __future__ import annotations

from typing import Any


def compute_runtime_health(
    store: Any,
    *,
    llm_gateway_enabled: bool,
    workflow_id: str | None = None,
    state: dict | None = None,
) -> dict[str, Any]:
    """SQLite-backed な runtime 運用ヘルスを集約して構造化 dict を返す。

    Args:
        store: SQLiteStore インスタンス
        llm_gateway_enabled: LLM Gateway が有効か (audit_log_silence 判定用)
        workflow_id: workflow 個別 gap に絞る場合の workflow_id
            (profile 横断ヘルスでは None)
        state: workflow state (phase4_plan_missing /
            supersedes_chain_broken 判定用、profile 横断では None)

    Returns:
        {
          "ran": bool,            # 集約を最後まで実行できたか
          "outbox_pending": int,  # notion_sync_outbox の pending 件数
          "outbox_errors": int,   # notion_sync_errors の永続 error 件数
          "gaps": [{"kind": str, "detail": str}],  # collect_gaps の結果
          "error": str | None,    # 集約自体が失敗した場合の例外情報
        }
    """
    from .prime_gaps import collect_gaps

    health: dict[str, Any] = {
        "ran": False,
        "outbox_pending": 0,
        "outbox_errors": 0,
        "gaps": [],
        "error": None,
    }
    try:
        health["outbox_pending"] = store.count_notion_sync_pending()
        health["outbox_errors"] = store.count_notion_sync_errors()
        gaps = collect_gaps(
            store=store,
            review_issues=None,
            llm_gateway_enabled=llm_gateway_enabled,
            workflow_id=workflow_id,
            state=state,
        )
        health["gaps"] = [{"kind": g.kind, "detail": g.detail} for g in gaps]
        health["ran"] = True
    except Exception as e:  # best-effort: 表示を壊さない
        health["error"] = f"{type(e).__name__}: {e}"
    return health
