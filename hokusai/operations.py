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

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# scope: 第1スライスでは read_only のみ実在。mutating は将来の registry 拡張で
# 副作用つき operation を表すための予約値 (run 時に scope guard で弾く)。
READ_ONLY = "read_only"
MUTATING = "mutating"


class OperationError(Exception):
    """operation 実行の契約違反。

    共通 guard/sink（`resolve_read_only_operation` / `invoke_operation` /
    `execute_operation`）から送出され、CLI / Dashboard / 将来 MCP が共通の
    例外型として捕捉する。
    """


class UnknownOperationError(OperationError):
    """登録されていない operation 名を実行しようとした。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"未知の operation: {name}")


class ScopeViolationError(OperationError):
    """read-only でない operation を read-only 経路で実行しようとした。"""

    def __init__(self, name: str, scope: str) -> None:
        self.name = name
        self.scope = scope
        super().__init__(
            f"operation '{name}' は scope={scope} のため実行できません"
        )


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

    # frozen=True は dataclass に __hash__ を生成させるが、input_schema (dict)
    # は hash 不可能なので、Operation を set/dict key に入れると hash 時に
    # TypeError になる。hash は使わない前提なので明示的に unhashable にして
    # 潜在バグを防ぐ (PR #143 Copilot Round 4 指摘)。frozen の immutability は
    # 維持される。
    __hash__ = None

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


class ReadOnlyStore:
    """副作用なしの read-only sqlite アクセサ (Operation Registry 用)。

    `SQLiteStore.__init__()` は WAL PRAGMA / CREATE / ALTER を必ず実行し、
    DB ファイルが無ければ新規作成までしてしまう。read-only operation の
    実行でこれが走ると「読むだけ」契約に反するため、sqlite3 URI の
    `mode=ro` で接続し SELECT のみ実行する read-only 専用アクセサを用意する
    (既存 `hokusai/config/profiles.py::_workflow_exists_readonly` と同方針。
     PR #143 Copilot Round 5 指摘)。

    DB ファイル不在 / テーブル不在 / sqlite でないファイル等は安全側の既定値
    (0 / [] / False) を返す。`collect_gaps()` 側も各呼び出しを try/except で
    包むため、best-effort な runtime health 集約を壊さない。
    """

    def __init__(self, db_path: Any) -> None:
        self._db_path = Path(db_path)

    def _read(self, fn: Callable[[sqlite3.Connection], Any], default: Any) -> Any:
        # URI 構築は Path.as_uri() を使う (スペース / # / ? 等の予約文字を
        # percent-encode して silent な接続失敗を防ぐ)。接続不可 / SQL 失敗は
        # すべて安全側の default に倒す。
        try:
            uri = f"{self._db_path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as conn:
                return fn(conn)
        except (sqlite3.Error, ValueError, OSError):
            return default

    def count_notion_sync_pending(self) -> int:
        return self._read(
            lambda c: int(
                (c.execute("SELECT COUNT(*) FROM notion_sync_outbox").fetchone()
                 or [0])[0]
            ),
            0,
        )

    def count_notion_sync_errors(self) -> int:
        return self._read(
            lambda c: int(
                (c.execute("SELECT COUNT(*) FROM notion_sync_errors").fetchone()
                 or [0])[0]
            ),
            0,
        )

    def has_failed_workflow_started(self, workflow_id: str) -> bool:
        return self._read(
            lambda c: c.execute(
                "SELECT 1 FROM notion_sync_errors "
                "WHERE workflow_id = ? AND event_type = 'workflow_started' LIMIT 1",
                (workflow_id,),
            ).fetchone()
            is not None,
            False,
        )

    def list_active_workflows(self) -> list[dict[str, Any]]:
        keys = ("workflow_id", "task_url", "task_title", "current_phase", "updated_at")
        return self._read(
            lambda c: [
                dict(zip(keys, row))
                for row in c.execute(
                    "SELECT workflow_id, task_url, task_title, current_phase, "
                    "updated_at FROM workflows WHERE current_phase < 10 "
                    "ORDER BY updated_at DESC"
                ).fetchall()
            ],
            [],
        )

    def list_audit_logs(
        self, *, workflow_id: str | None = None, limit: int = 50, **_: Any
    ) -> list[dict[str, Any]]:
        # collect_gaps は workflow_id + limit のみ使う (audit_log_silence 判定)。
        # 余剰フィルタ kwargs は read-only 用途では無視する。
        if limit < 1:
            raise ValueError("limit は 1 以上を指定してください")
        keys = (
            "id", "workflow_id", "phase", "action", "status", "details", "created_at"
        )
        where = "WHERE workflow_id = ?" if workflow_id is not None else ""
        params: list[Any] = []
        if workflow_id is not None:
            params.append(workflow_id)
        params.append(limit)

        def _run(c: sqlite3.Connection) -> list[dict[str, Any]]:
            import json

            rows = c.execute(
                f"SELECT id, workflow_id, phase, action, status, details_json, "
                f"created_at FROM audit_logs {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
            out = []
            for row in rows:
                row = list(row)
                row[5] = json.loads(row[5]) if row[5] else None
                out.append(dict(zip(keys, row)))
            return out

        return self._read(_run, [])

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        """単一 workflow のメタ情報を返す（不在 / 読めない場合は None）。"""
        keys = (
            "workflow_id", "task_url", "task_title", "branch_name",
            "current_phase", "updated_at", "profile_name",
        )
        return self._read(
            lambda c: (
                lambda row: dict(zip(keys, row)) if row else None
            )(
                c.execute(
                    "SELECT workflow_id, task_url, task_title, branch_name, "
                    "current_phase, updated_at, profile_name FROM workflows "
                    "WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
            ),
            None,
        )

    def list_open_review_issues(
        self, *, workflow_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """未解決 (status が未設定 / 解決系でない) review issue を返す。"""
        if limit < 1:
            raise ValueError("limit は 1 以上を指定してください")
        keys = ("dedupe_key", "workflow_id", "source", "rule", "file",
                "message", "repository", "severity", "status", "updated_at")
        clauses = [
            "(status IS NULL OR LOWER(status) NOT IN "
            "('resolved', 'closed', 'done'))"
        ]
        params: list[Any] = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        params.append(limit)
        where = "WHERE " + " AND ".join(clauses)
        return self._read(
            lambda c: [
                dict(zip(keys, row))
                for row in c.execute(
                    "SELECT dedupe_key, workflow_id, source, rule, file, "
                    "message, repository, severity, status, updated_at "
                    f"FROM review_issues {where} "
                    "ORDER BY updated_at DESC LIMIT ?",
                    params,
                ).fetchall()
            ],
            [],
        )

    def list_open_work_items(
        self, *, workflow_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """未完了 (status が未設定 / 完了系でない) work item を返す。"""
        if limit < 1:
            raise ValueError("limit は 1 以上を指定してください")
        keys = ("dedupe_key", "workflow_id", "title", "phase", "status",
                "updated_at")
        clauses = [
            "(status IS NULL OR LOWER(status) NOT IN "
            "('done', 'closed', 'completed'))"
        ]
        params: list[Any] = []
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        params.append(limit)
        where = "WHERE " + " AND ".join(clauses)
        return self._read(
            lambda c: [
                dict(zip(keys, row))
                for row in c.execute(
                    "SELECT dedupe_key, workflow_id, title, phase, status, "
                    f"updated_at FROM work_items {where} "
                    "ORDER BY updated_at DESC LIMIT ?",
                    params,
                ).fetchall()
            ],
            [],
        )

    def audit_summary(
        self, *, workflow_id: str | None = None
    ) -> dict[str, Any]:
        """audit_logs を action / status 別に集約する（件数は SQL で正確）。

        全件 scan を避けつつ truncation も避けるため、LIMIT 無しの
        `GROUP BY` 集約で件数を出す（一覧ではなく集計なので行数は種別数に
        収まる）。
        """
        where = "WHERE workflow_id = ?" if workflow_id is not None else ""
        params: list[Any] = []
        if workflow_id is not None:
            params.append(workflow_id)

        def _run(c: sqlite3.Connection) -> dict[str, Any]:
            total = int(
                (c.execute(
                    f"SELECT COUNT(*) FROM audit_logs {where}", params
                ).fetchone() or [0])[0]
            )
            by_action = {
                str(a): int(n)
                for a, n in c.execute(
                    f"SELECT action, COUNT(*) FROM audit_logs {where} "
                    "GROUP BY action ORDER BY action",
                    params,
                ).fetchall()
            }
            by_status = {
                str(s): int(n)
                for s, n in c.execute(
                    f"SELECT status, COUNT(*) FROM audit_logs {where} "
                    "GROUP BY status ORDER BY status",
                    params,
                ).fetchall()
            }
            return {"total": total, "by_action": by_action,
                    "by_status": by_status}

        return self._read(
            _run, {"total": 0, "by_action": {}, "by_status": {}}
        )


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


def _coerce_limit(params: dict[str, Any], default: int = 50) -> int:
    """`limit` パラメータを int へ変換する（未指定なら default）。

    CLI の `--param limit=50` は文字列で渡るため int 化する。1 未満 /
    非数値は ValueError にして呼び出し側 (CLI) が stderr + exit 1 にできる。
    """
    raw = params.get("limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"limit は整数で指定してください: {raw!r}") from None
    if value < 1:
        raise ValueError("limit は 1 以上を指定してください")
    return value


def _op_workflow_status(
    params: dict[str, Any], *, store: Any, config: Any
) -> dict[str, Any]:
    """単一 workflow のメタ情報 (phase / branch / profile 等) を返す。"""
    workflow_id = params.get("workflow_id")
    if not workflow_id:
        raise ValueError("workflow_id は必須です")
    return {"workflow": store.get_workflow(workflow_id)}


def _op_review_issues_list_open(
    params: dict[str, Any], *, store: Any, config: Any
) -> dict[str, Any]:
    """未解決の review issue 一覧を返す。"""
    return {
        "review_issues": store.list_open_review_issues(
            workflow_id=params.get("workflow_id"),
            limit=_coerce_limit(params),
        )
    }


def _op_workgraph_list_open_items(
    params: dict[str, Any], *, store: Any, config: Any
) -> dict[str, Any]:
    """未完了の work item 一覧を返す。"""
    return {
        "work_items": store.list_open_work_items(
            workflow_id=params.get("workflow_id"),
            limit=_coerce_limit(params),
        )
    }


def _op_llm_gateway_audit_summary(
    params: dict[str, Any], *, store: Any, config: Any
) -> dict[str, Any]:
    """audit_logs を action / status 別に集約したサマリを返す。"""
    return store.audit_summary(workflow_id=params.get("workflow_id"))


# input schema の部品: workflow_id でフィルタする read-only operation で共通。
_WORKFLOW_ID_PROP = {
    "type": "string",
    "description": "対象 workflow に絞る場合に指定 (未指定なら profile 横断)",
}
_LIMIT_PROP = {
    "type": "integer",
    "description": "返す最大件数 (未指定なら 50)",
}


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
    reg.register(
        Operation(
            name="workflow.status",
            summary="単一 workflow のメタ情報 (phase / branch / profile 等) を返す",
            scope=READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "対象 workflow の ID (必須)",
                    }
                },
                "required": ["workflow_id"],
            },
            handler=_op_workflow_status,
        )
    )
    reg.register(
        Operation(
            name="review_issues.list_open",
            summary="未解決の review issue 一覧を返す",
            scope=READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": _WORKFLOW_ID_PROP,
                    "limit": _LIMIT_PROP,
                },
                "required": [],
            },
            handler=_op_review_issues_list_open,
        )
    )
    reg.register(
        Operation(
            name="workgraph.list_open_items",
            summary="未完了の work item 一覧を返す",
            scope=READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {
                    "workflow_id": _WORKFLOW_ID_PROP,
                    "limit": _LIMIT_PROP,
                },
                "required": [],
            },
            handler=_op_workgraph_list_open_items,
        )
    )
    reg.register(
        Operation(
            name="llm_gateway.audit_summary",
            summary="audit_logs を action / status 別に集約したサマリを返す",
            scope=READ_ONLY,
            input_schema={
                "type": "object",
                "properties": {"workflow_id": _WORKFLOW_ID_PROP},
                "required": [],
            },
            handler=_op_llm_gateway_audit_summary,
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


def resolve_read_only_operation(
    registry: OperationRegistry, name: str
) -> Operation:
    """operation を引き、read-only であることを検証して返す共通 guard。

    `execute_operation` の lookup → scope guard 部分を切り出したもの。CLI は
    `--param` の形式検証より**前**にこの guard を通すことで「未知 / scope 違反
    を param エラーより先に報告する」従来挙動を保てる（PR #164 Copilot
    Round 2）。

    - 未知の operation 名 → `UnknownOperationError`
    - read-only でない operation → `ScopeViolationError`
    """
    op = registry.get(name)
    if op is None:
        raise UnknownOperationError(name)
    if not op.is_read_only:
        raise ScopeViolationError(name, op.scope)
    return op


def invoke_operation(
    op: Operation,
    params: dict[str, Any],
    *,
    config: Any,
    store: Any = None,
) -> dict[str, Any]:
    """検証済み read-only operation を実行する（store 解決 + handler 呼び出し）。

    `store` 未指定時は read-only 契約を守る `ReadOnlyStore(config.database_path)`
    を構築する（test 等で明示 store を渡せば優先）。handler の入力検証エラー
    （`ValueError`）はそのまま伝播する。
    """
    if store is None:
        store = ReadOnlyStore(config.database_path)
    return op.handler(params, store=store, config=config)


def execute_operation(
    registry: OperationRegistry,
    name: str,
    params: dict[str, Any],
    *,
    config: Any,
    store: Any = None,
) -> dict[str, Any]:
    """read-only operation を「単一経路」で実行する共通 sink。

    CLI (`operations run`) / Dashboard / 将来の read-only MCP・HTTP admin が
    **同じ lookup → scope guard → store 解決 → handler 呼び出し** の契約を
    共有するための実行関数。各呼び出し側で重複しがちな「未知 operation /
    scope 違反 / read-only store 解決 / 入力検証」の扱いを 1 箇所に集約する
    （Step 3 第3スライス）。

    契約:
    - 未知の operation 名 → `UnknownOperationError`
    - read-only でない operation → `ScopeViolationError`（第3スライスでも
      read-only のみ実行可。mutating は確認フロー込みで後続スライス）
    - handler の入力検証エラーは handler が送出する `ValueError` をそのまま
      伝播（呼び出し側が利用者向けに整形する）

    scope guard を **store 解決より前** に通すことで、未知 / scope 違反の
    operation では DB に一切触れない（無効入力で副作用や DB 接続を起こさない）。
    `store` 未指定時は read-only 契約を守る `ReadOnlyStore(config.database_path)`
    を構築する（test 等で明示 store を渡せば優先）。戻り値は JSON 直列化可能な
    dict。

    Dashboard / MCP のように params が既に dict で揃っている one-shot 呼び出し
    向けの便宜関数。CLI のように guard と param parse の順序を制御したい場合は
    `resolve_read_only_operation` + `invoke_operation` を個別に使う。
    """
    op = resolve_read_only_operation(registry, name)
    return invoke_operation(op, params, config=config, store=store)
