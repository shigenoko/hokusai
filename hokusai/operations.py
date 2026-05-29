"""Operation Registry (Step 3 第1スライス / roadmap-gbrain-inspirations.md §P1)

GBrain の `operations.ts` 同様、operation 名・説明・scope・入力 schema・
handler を 1 箇所に集約する registry。CLI / Dashboard / 将来の read-only
MCP・HTTP admin が同じ handler を呼ぶ単一経路を作るのが狙い。

第1スライスのスコープ:
- read-only operation のみを registry 化する (mutating は後続スライス)
- 既存の SQLite-backed な read-only 関数 (compute_runtime_health /
  store カウント / list_active_workflows) を handler として束ねる
- `hokusai operations list` / `hokusai operations run <name>` を提供する

CLI handler 全体を registry 経由へ寄せる / MCP・HTTP 化は後続スライス。

handler 契約:
    handler(params: dict, *, store, config) -> dict
  - params: 入力 schema に従う dict (CLI からは --param k=v で組み立てる)
  - store / config: 呼び出し側 (CLI / Console) が解決して渡す
  - 戻り値: JSON 直列化可能な dict (機械処理 / 表示で一貫)
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# scope: 第1スライスでは read_only のみ実在。mutating は将来の registry 拡張で
# 副作用つき operation を表すための予約値 (run 時に scope guard で弾く)。
READ_ONLY = "read_only"
MUTATING = "mutating"


@dataclass(frozen=True)
class Operation:
    """1 つの operation の contract。

    Attributes:
        name: 名前空間つき operation 名 (例 "notion.outbox_status")
        summary: 1 行説明 (一覧表示用)
        scope: READ_ONLY / MUTATING
        input_schema: JSON-schema 風の入力定義 (object 固定)
        handler: handler(params, *, store, config) -> dict
    """

    name: str
    summary: str
    scope: str
    input_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]]

    @property
    def is_read_only(self) -> bool:
        return self.scope == READ_ONLY


class OperationRegistry:
    """operation を名前で引ける registry (重複登録は禁止)。"""

    def __init__(self) -> None:
        self._ops: dict[str, Operation] = {}

    def register(self, op: Operation) -> None:
        if op.name in self._ops:
            raise ValueError(f"operation already registered: {op.name}")
        self._ops[op.name] = op

    def get(self, name: str) -> Operation | None:
        return self._ops.get(name)

    def names(self) -> list[str]:
        return sorted(self._ops)

    def list(self) -> list[Operation]:
        return [self._ops[n] for n in self.names()]


# --- seed handlers (read-only) -------------------------------------------
# いずれも既存の SQLite-backed 関数を薄くラップするだけ。live API 呼び出しは
# 行わない (Doctor / Console と同じ「読むだけ」契約)。


def _llm_gateway_enabled(config: Any) -> bool:
    return bool(getattr(getattr(config, "llm_gateway", None), "enabled", False))


def _op_notion_outbox_status(
    params: dict[str, Any], *, store: Any, config: Any
) -> dict[str, Any]:
    """Notion sync outbox の pending / 永続 error 件数を返す。

    キー名は `compute_runtime_health()` / `profile doctor --output json` の
    `runtime_health` と同じ `outbox_pending` / `outbox_errors` に揃える
    (Operation Registry は CLI / Dashboard / 将来の API の共通契約なので、
     同じ概念は同じキー名にして利用者の混乱を避ける。PR #143 Copilot
     Round 3 指摘)。`outbox_errors` は永続 error 件数を指す。
    """
    return {
        "outbox_pending": store.count_notion_sync_pending(),
        "outbox_errors": store.count_notion_sync_errors(),
    }


def _op_runtime_health(
    params: dict[str, Any], *, store: Any, config: Any
) -> dict[str, Any]:
    """SQLite-backed な runtime 運用ヘルス (outbox + 運用ギャップ) を集約する。

    Doctor / Operations Console と共通の `compute_runtime_health()` を呼ぶ。
    """
    from .health import compute_runtime_health

    return compute_runtime_health(
        store,
        llm_gateway_enabled=_llm_gateway_enabled(config),
        workflow_id=params.get("workflow_id"),
    )


def _op_workflow_list(
    params: dict[str, Any], *, store: Any, config: Any
) -> dict[str, Any]:
    """アクティブな workflow の一覧を返す。"""
    return {"workflows": store.list_active_workflows()}


def build_default_registry() -> OperationRegistry:
    """seed の read-only operation を登録した registry を構築する。"""
    reg = OperationRegistry()
    reg.register(
        Operation(
            name="notion.outbox_status",
            summary="Notion sync outbox の pending / 永続 error 件数を返す",
            scope=READ_ONLY,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=_op_notion_outbox_status,
        )
    )
    reg.register(
        Operation(
            name="runtime.health",
            summary=(
                "SQLite-backed な runtime 運用ヘルス (outbox + 運用ギャップ) "
                "を集約する"
            ),
            scope=READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": (
                            "個別 workflow の gap に絞る場合に指定 "
                            "(未指定なら profile 横断)"
                        ),
                    }
                },
                "required": [],
            },
            handler=_op_runtime_health,
        )
    )
    reg.register(
        Operation(
            name="workflow.list",
            summary="アクティブな workflow の一覧を返す",
            scope=READ_ONLY,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=_op_workflow_list,
        )
    )
    return reg


_DEFAULT_REGISTRY: OperationRegistry | None = None


def default_registry() -> OperationRegistry:
    """プロセス内で共有する既定 registry (遅延構築・シングルトン)。"""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY
