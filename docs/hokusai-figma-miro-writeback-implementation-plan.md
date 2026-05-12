# HOKUSAI Figma / Miro 書き戻し（Phase E）実装計画書

**作成日**: 2026-05-13

**Target Version**: v0.4.0（v0.3.0 直後の次マイナーリリース）

**対象読者**: HOKUSAI 運用設計者、Figma/Miro 統合の実装担当エンジニア、PM・デザイナー

**位置付け**: 本ドキュメントは、`docs/hokusai-figma-miro-integration-implementation-plan.md` の Phase E（書き戻し）を、推奨デフォルトを全採用したスコープで実装するための詳細計画。

**前提**:

- Phase A〜D + F（MVP）は v0.3.0 までに実装済み
- 既存 `docs/hokusai-figma-miro-integration-implementation-plan.md` §16 「Phase E: 書き戻し」を踏襲
- 既存 `hokusai/integrations/notion_dashboard` の `notion_sync_outbox` パターンを流用
- v0.3.0 で導入した profile 機能（`workflows.profile_name`）と整合

---

## 1. 背景

v0.3.0 までで Figma / Miro の**読み取り**統合は完成しており、Phase 2 (Research) / 3 (Design Check) / 7 (Review) / 8 (MR) で frame / sticky / コメント情報を活用できる。

しかし、現状は **HOKUSAI 側からデザイナー / PM 側へ進捗を返す経路が無い**。

- Phase 8a で MR が出ても、Figma / Miro 上で誰も気付かない
- デザイナーは GitLab / Slack を見に行く必要がある
- HOKUSAI が「どのフレームを実装したか」の trail が外部ツールに残らない

Phase E はこの片道通信を解消し、**書き戻し（コメント・カード投稿）** を実現する。

## 2. 目的

Phase 8a 完了時に、Figma / Miro の該当 frame / board に HOKUSAI が自動でコメント / カードを投稿し、デザイナー / PM への可視性を確保する。

### 2.1. ゴール

- Phase 8a 完了時に Figma frame コメントが自動投稿される
- Phase 8a 完了時に Miro card が自動投稿される
- 投稿失敗は SQLite outbox に蓄積され、Operations Console から再送できる
- 同一 payload で再送しても重複作成されない（冪等性）
- v0.3.0 profile 機能（`data_dir` 分離 / `profile_name` 列）と完全に整合する

### 2.2. 非ゴール

- 双方向同期（GitLab PR コメント → Figma など）は実装しない
- コメント編集・削除・スレッド返信・リアクション付与は実装しない
- ファイル添付・投稿予約は実装しない
- Phase 5（Implement）/ Phase 10（Record）のトリガーは v0.4.0 では実装しない
- Webhook 駆動のレビューループ（Phase G）は別フェーズ

### 2.3. 推奨デフォルト適用

要件整理セッション（2026-05-13）で確認した推奨デフォルトを全採用:

| 項目 | 採用案 |
|---|---|
| Figma コメント本文 | 単一行 `✅ Phase 8a 完了 / MR: {url} / commit: {sha}`、日本語のみ |
| Miro 投稿形式 | card、design frame 隣に配置 |
| 投稿対象 | 主 frame のみ（state に `primary_figma_frame_id` / `primary_miro_frame_id` を保持） |
| `on_failure` 既定値 | `warn` |
| 自動 retry | 無し（手動再送のみ） |
| outbox 削除ルール | 成功時に即削除、5 回失敗で errors へ移動 |
| errors 保持期間 | 30 日（経過後 cleanup で自動削除） |
| `profile_name` 列 | 追加（v0.3.0 整合） |
| データ保存先 | 既存 `workflow.db` 同居 |
| Operations Console | 既存 Figma/Miro パネルへ統合 |
| 再送ボタン粒度 | 個別 + 全件一括 |
| 投稿トリガー | Phase 8a 完了のみ |

## 3. 結論サマリ

```text
Phase 8a 完了
    ↓
WorkflowRunner._safe_design_writeback_dispatch()
    ↓
DesignWritebackDispatcher
    ├─ Figma post_comment(frame_id, body)
    │       ├─ 成功 → audit_log
    │       └─ 失敗 → figma_sync_outbox enqueue
    └─ Miro create_card(board_id, position, body)
            ├─ 成功 → audit_log
            └─ 失敗 → miro_sync_outbox enqueue

Operations Console
    ├─ outbox 件数表示
    ├─ /api/figma/retry-pending（個別 / 全件）
    └─ /api/miro/retry-pending（個別 / 全件）
```

既存 `NotionSyncDispatcher` パターンを完全踏襲し、同じ抽象で Figma / Miro を扱う。

## 4. アーキテクチャ

### 4.1. 既存基盤の流用

| 基盤 | 流用内容 |
|---|---|
| `NotionSyncDispatcher` | `DesignWritebackDispatcher` として複製、Figma / Miro 各サブクラス |
| SQLite `notion_sync_outbox` / `notion_sync_errors` | `figma_sync_outbox` / `figma_sync_errors` / `miro_sync_outbox` / `miro_sync_errors` として複製 |
| 冪等キー方式 | `workflow_id:event_type:resource:revision` |
| `_safe_*_dispatch` | `_safe_design_writeback_dispatch` を WorkflowRunner に追加 |
| `_detect_token_like_values` | 既存で対応済み（v0.3.0 までに Figma / Miro token パターン追加済み） |
| Operations Console | 既存 Figma / Miro パネルに outbox / 再送 UI を追加 |

### 4.2. 新規モジュール

```text
hokusai/integrations/design/
├── writeback/                          # 新規
│   ├── __init__.py
│   ├── dispatcher.py                   # DesignWritebackDispatcher
│   ├── figma_writeback.py              # Figma post_comment 実装
│   ├── miro_writeback.py               # Miro create_card 実装
│   ├── outbox.py                       # outbox / errors テーブル操作
│   ├── idempotency.py                  # 冪等キー生成・検証
│   └── templates.py                    # コメント本文テンプレート
```

### 4.3. WorkflowRunner への組み込み

```python
# hokusai/workflow.py (既存ファイルへの追加)

async def _safe_design_writeback_dispatch(
    self, event_type: str, **kwargs
) -> None:
    """Phase 8a 完了などのイベントを Figma / Miro 書き戻しに dispatch する。

    既存 _safe_notion_dispatch / _safe_slack_dispatch と同じパターン。
    on_failure: warn を既定とし、失敗時は outbox に積んで継続する。
    """
    if not self.design_writeback_dispatcher:
        return
    try:
        await self.design_writeback_dispatcher.dispatch(event_type, **kwargs)
    except Exception as e:
        log.warning(f"design writeback dispatch failed: {e}")
        # 失敗は dispatcher 内で outbox 記録済み。ここでは workflow を継続。
```

### 4.4. データモデル

```python
@dataclass
class WritebackPayload:
    """書き戻し 1 件分のペイロード"""
    workflow_id: str
    profile_name: str | None              # v0.3.0 整合
    event_type: str                       # 例: "phase8a_completed"
    resource: str                          # frame_id / board_id
    revision: str                          # commit sha / MR iid
    body: str                              # 投稿本文（テンプレート展開済み）
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass
class OutboxEntry:
    id: int
    idempotency_key: str
    workflow_id: str
    profile_name: str | None
    event_type: str
    payload_json: str
    attempt_count: int
    last_error: str | None
    created_at: str
    updated_at: str
```

## 5. SQLite スキーマ

### 5.1. 新規テーブル

`notion_sync_outbox` と同構造を **Figma / Miro 各 2 テーブル** ずつ複製。`profile_name` 列を追加。

```sql
-- Figma 同期用 outbox
CREATE TABLE IF NOT EXISTS figma_sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    workflow_id TEXT NOT NULL,
    profile_name TEXT,                          -- v0.3.0 整合
    event_type TEXT NOT NULL,                   -- "phase8a_completed" 等
    payload_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_figma_outbox_workflow
    ON figma_sync_outbox(workflow_id);
CREATE INDEX IF NOT EXISTS idx_figma_outbox_event
    ON figma_sync_outbox(event_type);

-- Figma 同期用 errors（恒久的に保持、再送時の参照用）
CREATE TABLE IF NOT EXISTS figma_sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    profile_name TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error_message TEXT NOT NULL,
    failed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_figma_errors_workflow
    ON figma_sync_errors(workflow_id);

-- Miro 同期用 outbox（構造は figma と同じ）
CREATE TABLE IF NOT EXISTS miro_sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    workflow_id TEXT NOT NULL,
    profile_name TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_miro_outbox_workflow
    ON miro_sync_outbox(workflow_id);
CREATE INDEX IF NOT EXISTS idx_miro_outbox_event
    ON miro_sync_outbox(event_type);

CREATE TABLE IF NOT EXISTS miro_sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    profile_name TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error_message TEXT NOT NULL,
    failed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_miro_errors_workflow
    ON miro_sync_errors(workflow_id);

-- 冪等キー記録（§9.2 参照、API call 後の重複抑止用）
CREATE TABLE IF NOT EXISTS design_writeback_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    profile_name TEXT,
    target TEXT NOT NULL,                       -- "figma" | "miro"
    resource TEXT NOT NULL,                     -- frame_id / board_id
    response_id TEXT,                           -- 投稿成功時の comment_id / card_id
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_writeback_idempotency_workflow
    ON design_writeback_idempotency(workflow_id);
```

### 5.2. マイグレーション

`hokusai/persistence/sqlite_store.py::_init_db()` に上記 **5 テーブル + 5 index** を `CREATE TABLE IF NOT EXISTS` で追加。新規 DB / 既存 v0.3.x DB の両方で無害に動作する（既存テーブルへの ALTER は不要）。

### 5.3. 保持期間と cleanup

| テーブル | 削除タイミング | TTL |
|---|---|---|
| `figma_sync_outbox` / `miro_sync_outbox` | 投稿成功時に即削除 / 5 回失敗で errors へ移動 | TTL なし（恒久 pending は手動再送 / 強制移動で解消） |
| `figma_sync_errors` / `miro_sync_errors` | `hokusai cleanup` で 30 日経過行を削除 | **30 日** |
| `design_writeback_idempotency`（§9.2 参照） | `hokusai cleanup` で 30 日経過行を削除 | **30 日** |

cleanup は `hokusai cleanup` コマンドに統合（既存の Notion outbox cleanup と同じ paths）。

**outbox に長期 pending な行が残った場合**: 手動再送 / `move-to-errors` API（§10.2）で解消する。outbox には TTL を設けないが、5 回再送で errors に自動移動されるため、Operations Console から errors を見れば失敗履歴を辿れる。

## 6. 投稿テンプレート

### 6.1. Figma frame コメント

#### 6.1.1. 本文（単一行・日本語のみ）

```
✅ Phase 8a 完了 / MR: {mr_url} / commit: {commit_sha_short}
```

**例**:

```
✅ Phase 8a 完了 / MR: https://gitlab.com/foo/bar/-/merge_requests/123 / commit: a1b2c3d
```

#### 6.1.2. Figma API payload 構造

Figma REST API `POST /v1/files/{file_key}/comments` は **`message` + `client_meta`** を要求する。`client_meta` を渡さないとフローティングコメントになり、特定 frame に pin できない。

```json
{
  "message": "✅ Phase 8a 完了 / MR: ... / commit: ...",
  "client_meta": {
    "node_id": "{primary_figma_node_id}",
    "node_offset": {"x": 0, "y": 0}
  }
}
```

参考: [Figma REST API - Comments endpoints](https://developers.figma.com/docs/rest-api/comments-endpoints/)

**`file_key` / `node_id` / `node_offset` の確定**:

- `file_key`: state.primary_figma_file_key（§7 参照、Phase 3 で Figma URL から抽出）
- `node_id`: state.primary_figma_node_id（§7 参照、design_context から取得した frame の node_id）
- `node_offset`: state.primary_figma_node_offset（既定 `{x: 0, y: 0}` で frame 左上に pin、§7.2 で確定）

frame_id だけでは pin 投稿は再現できないため、state に 3 フィールド + offset を保持する。

#### 6.1.3. 実装

```python
# templates.py
def render_figma_comment(payload: WritebackPayload) -> str:
    """コメント本文の文字列のみ返す（client_meta は別レイヤーで組み立て）"""
    mr_url = payload.extra.get("mr_url", "(MR URL 不明)")
    commit_sha = payload.extra.get("commit_sha", "(commit 不明)")
    commit_short = commit_sha[:7] if commit_sha else "(commit 不明)"
    return f"✅ Phase 8a 完了 / MR: {mr_url} / commit: {commit_short}"


def build_figma_payload(payload: WritebackPayload, state: WorkflowState) -> dict:
    """Figma REST API に送る payload（message + client_meta）を構築"""
    return {
        "message": render_figma_comment(payload),
        "client_meta": {
            "node_id": state.primary_figma_node_id,
            "node_offset": state.primary_figma_node_offset or {"x": 0, "y": 0},
        },
    }
```

### 6.2. Miro card

**配置位置**: 主 design frame の **右側 50px** に card を配置する。frame 位置（`x`, `y`, `width`）を design context から取得して座標計算。

**body フォーマット** (Miro card は title + description の 2 段構造):

| フィールド | 値 |
|---|---|
| title | `✅ Phase 8a 完了` |
| description | `MR: {mr_url}\ncommit: {commit_sha_short}` |
| style.fillColor | `#4FCC8B`（薄い緑） |

**実装**:

```python
# templates.py
def render_miro_card_payload(payload: WritebackPayload, frame_meta: dict) -> dict:
    mr_url = payload.extra.get("mr_url", "(MR URL 不明)")
    commit_sha = payload.extra.get("commit_sha", "(commit 不明)")
    commit_short = commit_sha[:7] if commit_sha else "(commit 不明)"
    return {
        "data": {
            "title": "✅ Phase 8a 完了",
            "description": f"MR: {mr_url}\ncommit: {commit_short}",
        },
        "position": {
            "x": frame_meta["x"] + frame_meta["width"] + 50,
            "y": frame_meta["y"],
        },
        "style": {"fillColor": "#4FCC8B"},
    }
```

## 7. 投稿対象の決定方法

### 7.1. State 拡張

`workflow_state` に新規キーを追加（既存 state には影響なし、未設定なら投稿スキップ）:

```python
# state.py (既存 dataclass への field 追加)
@dataclass
class WorkflowState:
    ...
    # Figma: client_meta（pin 投稿位置）を再現するために 3 フィールド必要
    primary_figma_file_key: str | None = None       # 追加（Figma URL から抽出）
    primary_figma_frame_id: str | None = None       # 追加（参照用、ログ / Operations Console 表示）
    primary_figma_node_id: str | None = None        # 追加（API client_meta.node_id に渡す）
    primary_figma_node_offset: dict | None = None   # 追加（既定 {"x":0,"y":0}、frame 左上 pin）

    # Miro: card 作成に frame_id + board_id 必須
    primary_miro_frame_id: str | None = None        # 追加
    primary_miro_board_id: str | None = None        # 追加
```

**`frame_id` と `node_id` の関係**: Figma の frame は node の一種で、`node_id` は frame ID と同じ値になることが多いが、Figma URL の `?node-id=` パラメータと design_context の `nodes[].id` を厳密に使い分けるため、両方を state に保持する（実装上の混乱回避）。

### 7.2. 決定タイミング

| Phase | 動作 |
|---|---|
| Phase 2 (Research) | design_context から候補 Figma frame 一覧（file_key / node_id 含む）/ 候補 Miro frame + board ペア一覧を取得 |
| Phase 3 (Design Check) | 主 frame / board を確定し state に保存:<br>・Figma: `primary_figma_file_key` / `primary_figma_frame_id` / `primary_figma_node_id` / `primary_figma_node_offset`<br>・Miro: `primary_miro_frame_id` / `primary_miro_board_id` |
| Phase 8a 完了時 | dispatcher へ以下を渡して呼ぶ:<br>・Figma: `file_key` / `node_id` / `node_offset` / `message`（API `POST /v1/files/{file_key}/comments` 用）<br>・Miro: `primary_miro_frame_id` + `primary_miro_board_id` |

**主 frame の決定ルール** (Phase 3 内):

#### Figma

1. `design_context.figma_frames` が空 → `primary_figma_file_key` / `primary_figma_frame_id` / `primary_figma_node_id` を `None`（書き戻しスキップ）
2. `design_context.figma_frames` が 1 件 → それを採用
3. `design_context.figma_frames` が複数件 → **state.task_url の Figma URL に含まれる `?node-id=` パラメータ** を優先、なければリスト先頭

**`file_key` / `node_id` / `node_offset` の決定**:

- `file_key`: Figma URL `https://www.figma.com/file/{file_key}/...` から抽出（既存 `hokusai/integrations/design/url_parser.py` を流用）
- `node_id`: 採用 frame の node_id（design_context.figma_frames[i].id）
- `node_offset`: 既定 `{"x": 0, "y": 0}`（frame 左上 pin）。v0.4.0 では設定不可、v0.4.1 以降に config 化検討
- `frame_id`: 参照用に node_id と同じ値を保持（Operations Console 表示用）

#### Miro

Miro card 作成 API（`POST /v2/boards/{board_id}/cards`）は board_id が必須のため、**frame_id と board_id をペアで確定** する:

1. `design_context.miro_frames` が空 → `primary_miro_frame_id = None` / `primary_miro_board_id = None`（書き戻しスキップ）
2. `design_context.miro_frames` が 1 件 → そのフレームの `frame_id` を `primary_miro_frame_id` に、所属 board の `board_id` を `primary_miro_board_id` に採用
3. `design_context.miro_frames` が複数件 → **state.task_url の Miro URL に含まれる board_id / frame_id** を優先、なければリスト先頭の frame とその所属 board を採用
4. design_context が frame に board_id を含まない場合（後方互換）→ `design_context.miro_boards[0].id` をフォールバック採用

**複数 board へ跨る投稿は v0.4.0 ではスコープ外**。主 frame の所属 board のみを対象とする。複数 board 対応は v0.4.1 以降のフォローアップ。

複数 frame への投稿も同様に v0.4.0 ではスコープ外。

### 7.3. 未設定時の動作

dispatcher は以下のケースでスキップし、audit_log に `skipped: no primary frame/board` を残して return。エラーにはしない:

- Figma: `primary_figma_file_key` または `primary_figma_node_id` が `None`（**両方揃って初めて API 呼び出し可能**）
- Miro: `primary_miro_frame_id` または `primary_miro_board_id` が `None`（**両方揃って初めて投稿可能**）

## 8. 失敗時ポリシー（on_failure）

### 8.1. 既定値: `warn`

config file で上書き可:

```yaml
# claude-workflow.yaml
figma:
  enabled: true
  api_token_env: HOKUSAI_FIGMA_API_TOKEN
  writeback:
    on_failure: warn          # warn | block | skip（既定: warn）
    enabled: true             # 書き戻し機能の on/off

miro:
  enabled: true
  api_token_env: HOKUSAI_MIRO_API_TOKEN
  writeback:
    on_failure: warn
    enabled: true
```

### 8.2. ポリシー別動作

| on_failure | 投稿失敗時の動作 |
|---|---|
| `warn`（既定） | outbox に積む + warning ログ + workflow 継続 |
| `block` | outbox に積む + workflow を Waiting for Human に遷移 |
| `skip` | outbox にも積まない + warning ログ + workflow 継続（運用上、明示的に書き戻し不要のケースで使う） |

### 8.3. 自動 retry なし

v0.4.0 では自動 retry は実装しない。理由:

- Figma / Miro の rate limit は読み取りより緩いが、書き込みで連続失敗するケースは恒久的な問題（権限・token・frame 削除など）が多い
- 自動 retry の設計（backoff、最大試行回数、デッドレターキュー）は実装コストが大きい
- 手動再送（Operations Console）で十分実用的

Phase G（Webhook 受信）で自動 retry が必要になった時点で再設計する。

### 8.4. attempt_count

outbox 行の `attempt_count` は **手動再送のたびに +1**。再送 API で `attempt_count >= 5` の行は再送拒否し、errors テーブルに移動する。

## 9. 冪等性

### 9.1. 冪等キー

```text
{workflow_id}:{event_type}:{resource}:{revision}
```

例:

```text
wf_20260513_001:phase8a_completed:figma_frame_abc123:a1b2c3d4
wf_20260513_001:phase8a_completed:miro_frame_xyz789:a1b2c3d4
```

### 9.2. 重複抑止フロー

**重要**: Figma REST API (`POST /v1/files/{file_key}/comments`) / Miro REST API
(`POST /v2/boards/{board_id}/cards`) の**いずれにも idempotency key 受け渡しの
仕組みは存在しない**ため、重複抑止を API 任せにはできない。

代わりに、**HOKUSAI 側で成功済み idempotency_key を永続化** し、dispatcher の
入口で事前チェックする。これは Stripe の `idempotency_key` 設計に近いが、
リクエスト前後のローカル記録のみで完結する。

#### 9.2.1. 専用テーブル

```sql
CREATE TABLE IF NOT EXISTS design_writeback_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    profile_name TEXT,                    -- v0.3.0 整合
    target TEXT NOT NULL,                 -- "figma" | "miro"
    resource TEXT NOT NULL,               -- frame_id / board_id
    response_id TEXT,                     -- 投稿成功時の comment_id / card_id
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_writeback_idempotency_workflow
    ON design_writeback_idempotency(workflow_id);
```

§5.1 のテーブル一覧にも追加（4 テーブル + 4 index + idempotency 1 テーブル + 1 index）。`hokusai cleanup` で 30 日経過行を削除する（§5.3）。

#### 9.2.2. フロー

```text
dispatch()
    ↓
冪等キー生成: {workflow_id}:{event_type}:{resource}:{revision}
    ↓
1. SELECT FROM design_writeback_idempotency WHERE idempotency_key = ?
    ├─ ヒット → 既に投稿済み、return（重複投稿しない）
    └─ 未ヒット →
2. SELECT FROM {outbox} WHERE idempotency_key = ?
    ├─ ヒット → 既に pending、return（手動再送は別途 Operations Console から）
    └─ 未ヒット →
3. SELECT FROM {errors} WHERE idempotency_key = ?（force=true でない時のみ）
    ├─ ヒット → 既に「諦め済」、return（5 回失敗で errors 移動済の payload）
    └─ 未ヒット →
4. Figma/Miro API call（POST）
    ├─ 成功 →
       a. INSERT INTO design_writeback_idempotency（response_id 含む）
       b. INSERT INTO audit_logs (status='success')
    └─ 失敗 → INSERT INTO {outbox}（attempt_count=0, last_error 付き）
```

**3 段階チェックの意図**:

| チェック対象 | 意味 |
|---|---|
| `design_writeback_idempotency` | **投稿済み**。再投稿は無条件に禁止 |
| `{outbox}` | **pending**。同じ冪等キーでの並列 dispatch を抑止 |
| `{errors}` | **諦め済**（5 回失敗）。Phase 8a 再実行 / workflow resume / プロセス再起動による自動再 dispatch でも、過去に諦めた投稿が蘇って同じ idempotency_key で再送されないようにする |

errors にある行は「過去に投稿を試みて 5 回失敗した」記録なので、自動経路では再投稿しない。Operations Console から運用者が **明示的に強制再送** する場合のみ、`force=true` フラグを付けて errors チェックを skip する経路を用意（§10.2 の API を参照）。

#### 9.2.3. 設計上の注意点

- API call 直前にレコードを **先行 INSERT しない**（INSERT 後に API 失敗するとロールバック必要、SQLite トランザクション複雑化）。事後 INSERT で「成功した投稿のみ idempotency に残る」設計
- API 成功直後のクラッシュで idempotency 記録漏れの可能性はあるが、その場合の再送は同じ revision なら API 側に重複投稿を許容するしかない（実害は二重コメントのみ、Operations Console で気付ける）
- 手動再送の場合も outbox から取り出した payload で同じ冪等キーが生成されるため、再送先 API call 前に上記 1〜2 のチェックが効く

### 9.3. 成功した投稿の trail

成功した投稿は outbox に残さず、`audit_logs` テーブル + `design_writeback_idempotency` テーブルに記録する:

```sql
-- audit_logs（既存テーブル流用、人が時系列で追える）
INSERT INTO audit_logs (workflow_id, phase, action, status, details_json, created_at)
VALUES (?, 8, 'design_writeback', 'success',
        '{"target":"figma","frame_id":"...","idempotency_key":"...","response_id":"..."}',
        ?);

-- design_writeback_idempotency（重複抑止用、PRIMARY KEY で高速ルックアップ）
INSERT INTO design_writeback_idempotency
       (idempotency_key, workflow_id, profile_name, target, resource, response_id, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?);
```

`hokusai list <workflow_id>` の出力で書き戻し履歴が確認できる。

## 10. Operations Console UI

### 10.1. 既存 Figma / Miro パネルへの統合

`scripts/dashboard.py` の Figma / Miro パネルに以下のセクションを追加:

```text
┌─ Figma 連携状態 ───────────────────────────┐
│  接続状態:   ✓ OK                          │
│  cache:      hit 80% / TTL 1800s          │
│  cache refresh ボタン                      │
│                                            │
│  Writeback Outbox:                         │  ← NEW
│  - pending: 3 件                           │
│  - errors:  1 件                           │
│  [個別再送]  [pending 全件再送]            │  ← NEW
└────────────────────────────────────────────┘
```

### 10.2. 新規 API

| メソッド | パス | 動作 |
|---|---|---|
| GET | `/api/figma/outbox` | outbox 行一覧（最新 100 件） |
| GET | `/api/figma/errors` | errors 行一覧（最新 100 件） |
| POST | `/api/figma/retry-pending` | body に `{"id": <int>}` で個別再送、body 空で全件再送 |
| POST | `/api/figma/move-to-errors` | body に `{"id": <int>}` で outbox → errors 強制移動 |

Miro 側は同じ paths を `/api/miro/...` で複製。

### 10.3. 表示の primary フレーム

各 outbox 行で `payload_json` 内の `resource`（frame_id）を抜粋して表示し、Figma / Miro へのリンクを生成する:

```text
| id | event_type            | resource              | attempts | last_error           |
|----|-----------------------|----------------------|----------|----------------------|
|  1 | phase8a_completed     | figma:abc123 [link]  | 2        | 403 forbidden        |
|  2 | phase8a_completed     | miro:xyz789 [link]   | 1        | rate limit           |
```

## 11. 実装ステップ

### Step 1: SQLite スキーマ追加（0.5 週間）

**対象ファイル**:
- `hokusai/persistence/sqlite_store.py`
- `tests/test_design_writeback_outbox.py`（新規）

**実装内容**:
- 4 テーブル + 4 index の `CREATE TABLE IF NOT EXISTS` を `_init_db()` に追加
- 新規 DB / 既存 v0.3.x DB の両方で動作する init を確認

**DoD**:
- 新規 DB で 4 テーブルが作成される
- 既存 v0.3.x DB を v0.4.0 で開いても壊れない
- profile_name 列が含まれている

### Step 2: outbox 操作 API（0.5 週間）

**対象ファイル**:
- `hokusai/integrations/design/writeback/outbox.py`
- `hokusai/integrations/design/writeback/idempotency.py`
- `tests/test_design_writeback_outbox.py`

**実装内容**:
- `OutboxStore` クラス: enqueue / get / list / mark_succeeded / move_to_errors / increment_attempt
- 冪等キー生成 / 検証
- profile_name の自動注入

**DoD**:
- outbox CRUD 操作のテストが pass
- 冪等キーで重複が抑止される
- profile_name が正しく保存される

### Step 3: Figma post_comment 実装（0.5 週間）

**対象ファイル**:
- `hokusai/integrations/design/writeback/figma_writeback.py`
- `hokusai/integrations/design/figma.py`（`post_comment` メソッド追加）
- `tests/test_figma_writeback.py`

**実装内容**:
- Figma REST API `POST /v1/files/{file_key}/comments` の wrapper
- レート制限 / 403 / 404 などのエラーハンドリング
- 成功時の audit_log、失敗時の outbox enqueue
- テンプレート展開（`templates.py`）

**DoD**:
- ローカルで Figma frame に test コメントが投稿できる
- 失敗時に outbox に蓄積される
- 同一 payload を再送しても重複投稿しない

### Step 4: Miro create_card 実装（0.5 週間）

**対象ファイル**:
- `hokusai/integrations/design/writeback/miro_writeback.py`
- `hokusai/integrations/design/miro.py`（`create_card` メソッド追加）
- `tests/test_miro_writeback.py`

**実装内容**:
- Miro REST API `POST /v2/boards/{board_id}/cards` の wrapper
- 配置位置の座標計算（frame_meta から相対位置）
- 同上のエラーハンドリング / outbox / audit_log
- テンプレート展開

**DoD**:
- ローカルで Miro board に test card が投稿できる
- 主 frame の右側に正しい座標で配置される
- 同一 payload を再送しても重複作成しない

### Step 5: WorkflowRunner 統合（0.5 週間）

**対象ファイル**:
- `hokusai/workflow.py`
- `hokusai/state.py`（`primary_figma_frame_id` / `primary_miro_frame_id` / `primary_miro_board_id` フィールド追加）
- `hokusai/nodes/phase3_design.py`（primary frame の決定ロジック）
- `hokusai/nodes/phase8/pr_creation.py`（Phase 8a 完了時の dispatcher 呼び出し）
- `tests/test_workflow_design_writeback.py`

**実装内容**:
- WorkflowState への 3 フィールド追加（既存 state は後方互換）
- Phase 3 で primary frame を決定（§7.2 のルール）
- Phase 8a 完了時に `_safe_design_writeback_dispatch()` を呼ぶ
- `on_failure: block` の場合に Waiting for Human 遷移

**DoD**:
- End-to-end で Phase 8a 完了時に Figma / Miro へ投稿される
- on_failure: warn / block / skip の全パターンで期待動作になる
- 既存 v0.3.x workflow（primary frame 未設定）は壊れない

### Step 6: Operations Console UI（0.5 週間）

**対象ファイル**:
- `scripts/dashboard.py`（Figma / Miro パネル拡張）
- `hokusai/integrations/design/writeback/api.py`（新規、Console から呼ぶ API）
- `tests/test_dashboard_design_writeback.py`

**実装内容**:
- outbox / errors 件数表示
- `/api/figma/outbox`, `/api/figma/errors`, `/api/figma/retry-pending`, `/api/figma/move-to-errors`
- Miro 側も同様
- 個別 / 全件再送ボタン

**DoD**:
- Console で outbox / errors 件数が正しく見える
- 再送ボタンで再投稿が走る
- 再送 5 回で自動的に errors に移動する

### Step 7: cleanup 統合 + ドキュメント（0.5 週間）

**対象ファイル**:
- `hokusai/cli_main.py::_handle_cleanup`（30 日経過 errors を削除する処理を追加）
- `docs/figma-miro-writeback-operation-guide.md`（新規）
- `docs/hokusai-figma-miro-integration-implementation-plan.md`（Phase E 完了の記載追加）
- `CHANGELOG.md`

**実装内容**:
- `hokusai cleanup` で 30 日経過 errors の自動削除
- 運用ガイド作成（投稿テンプレートの設定、on_failure の説明、再送手順、トラブルシューティング）
- v0.4.0 リリースノート

**DoD**:
- 運用ガイドだけで PM / デザイナー / エンジニアが使い方を理解できる
- cleanup コマンドで古い errors が削除される

**合計工数**: 3.5 週間（Step 1〜7、並列作業で 2 週間まで圧縮可）

## 12. テスト計画

### 12.1. 追加テスト件数

| 領域 | 件数 |
|---|---|
| outbox CRUD（Step 2） | ~10 件 |
| 冪等キー生成 / 検証 | ~5 件 |
| Figma writeback（Step 3） | ~8 件 |
| Miro writeback（Step 4） | ~8 件 |
| WorkflowRunner 統合（Step 5） | ~10 件 |
| Operations Console API（Step 6） | ~7 件 |
| profile_name 整合（v0.3.0） | ~3 件 |
| **合計** | **~51 件** |

実装計画書原本（§15.1）の見積もり「Phase E 追加分 ~15 件」より実態に近づけた。

### 12.2. 主要テストケース

| カテゴリ | テスト名 |
|---|---|
| 冪等性 | `test_duplicate_payload_skipped` |
| 冪等性 | `test_retry_does_not_duplicate_post` |
| outbox | `test_failed_post_enqueued` |
| outbox | `test_attempt_count_increments_on_retry` |
| outbox | `test_max_attempts_moves_to_errors` |
| profile 整合 | `test_outbox_writes_profile_name` |
| profile 整合 | `test_outbox_isolated_per_profile_data_dir` |
| state 互換 | `test_workflow_without_primary_frame_skips_writeback` |
| on_failure | `test_on_failure_warn_continues_workflow` |
| on_failure | `test_on_failure_block_transitions_to_waiting` |
| Console | `test_retry_pending_api` |
| Console | `test_outbox_listing_filters_by_profile` |

### 12.3. Manual QA

- 実 Figma file に test コメント投稿（権限・rate limit 確認）
- 実 Miro board に test card 投稿（座標・スタイル確認）
- profile A / B で別 outbox に書き込まれることを確認
- Operations Console から再送 → 重複投稿されないことを確認

## 13. リスクと対策

| リスク | 対策 |
|---|---|
| Figma / Miro の権限不足で 403 連発 | `profile doctor` で書き込み権限の事前チェック（v0.4.1 で追加検討） |
| 主 frame の特定ロジックが想定外 frame を選ぶ | Phase 3 で `primary_*_frame_id` を state に明示記録、Operations Console で確認可能にする |
| rate limit 超過 | 自動 retry なし、outbox 蓄積 + 手動再送で運用回避 |
| outbox 肥大化 | 30 日 cleanup で errors 自動削除、成功は audit_log に集約（outbox に残さない） |
| profile 切替時の混在 | profile_name 列で完全分離、`data_dir` 別 DB なら物理的にも分離済み |
| 投稿テンプレート変更要求 | `templates.py` に集約、将来 i18n / YAML 設定化は v0.4.1 以降 |
| Phase 8a が複数回呼ばれた場合 | 冪等キーに `revision`（commit sha）を含めるため、commit が同一なら重複しない |
| Notion 同期との race | dispatcher は独立、同じ event を Notion / design 両方に投げても問題なし |

## 14. v0.3.0 profile 機能との整合

### 14.1. data_dir 分離

profile ごとに `data_dir` が分離されているため、`workflow.db` 内の outbox / errors テーブルも profile ごとに完全に独立する。実装側で profile を意識する必要はない。

### 14.2. profile_name 列

全 4 テーブルに `profile_name` 列を含める。WorkflowRunner が dispatcher に渡す state から自動注入する（既存の `workflows.profile_name` と同じパターン）。

### 14.3. Operations Console の profile 表示

既存パネルが `Profile: a-company` バッジを既に表示しているため、追加対応は不要。outbox 一覧も自然と profile 別になる。

### 14.4. cleanup の profile 対応

`hokusai cleanup` は既に profile 別に動作するため、追加対応は不要。`hokusai --profile a-company cleanup` で該当 profile の outbox のみクリーンアップされる。

## 15. Open Questions

| # | 質問 | 暫定案 |
|---|---|---|
| 1 | Figma コメントに reply スレッドを使うか、新規コメントとして毎回投稿するか | v0.4.0 は **新規コメントのみ**（reply は v0.4.1 以降） |
| 2 | Miro card のサイズ・色を config で変えられるようにするか | v0.4.0 は **ハードコード**（薄緑 #4FCC8B、デフォルトサイズ）、config 化は v0.4.1 以降 |
| 3 | 投稿先 frame を複数指定可能にするか | v0.4.0 は **主 frame のみ**、複数対応は v0.4.1 以降 |
| 4 | 投稿テンプレートの i18n 対応 | v0.4.0 は **日本語のみ**、英語切替は v0.4.1 以降 |
| 5 | 自動 retry の有無 | v0.4.0 は **無し**、Phase G 着手時に再検討 |
| 6 | Phase 5 / Phase 10 にも書き戻しトリガーを追加するか | v0.4.0 は **Phase 8a のみ**、要望次第で v0.4.1 以降 |
| 7 | dashboard port 衝突時の挙動 | 既存 v0.3.0 の Operations Console 仕様を踏襲（追加対応不要） |
| 8 | コメント投稿失敗で MR 作成自体を止めるか | **止めない**（best effort、on_failure 既定 warn）。重要案件は `block` で運用 |

## 16. DoD（v0.4.0 リリース条件）

| # | 項目 |
|---|---|
| 1 | Phase 8a 完了時に Figma frame コメントが自動投稿される |
| 2 | Phase 8a 完了時に Miro card が自動投稿される |
| 3 | 投稿失敗は outbox に蓄積され、Operations Console から確認できる |
| 4 | Operations Console から個別 / 全件再送ができる |
| 5 | 同一 payload で再送しても重複投稿されない（冪等性） |
| 6 | `on_failure: warn / block / skip` の全パターンで期待動作になる |
| 7 | profile A / B の outbox が完全に分離される |
| 8 | 既存 v0.3.x workflow（primary frame 未設定）は壊れない |
| 9 | 既存テスト（1078 件以上）が全 pass |
| 10 | 追加テスト ~51 件が全 pass |
| 11 | 運用ガイド `docs/figma-miro-writeback-operation-guide.md` が公開されている |
| 12 | CHANGELOG.md に v0.4.0 リリースノートが記載されている |
| 13 | 実 Figma / Miro での manual QA が完了している |

## 17. リリース計画

| マイルストーン | 期日（暫定） | 内容 |
|---|---|---|
| Step 1〜2 完了 | v0.4.0 + 1 週 | SQLite スキーマ + outbox 操作 API |
| Step 3〜4 完了 | v0.4.0 + 2 週 | Figma / Miro writeback 実装 |
| Step 5 完了 | v0.4.0 + 2.5 週 | WorkflowRunner 統合 |
| Step 6〜7 完了 | v0.4.0 + 3.5 週 | Operations Console + ドキュメント |
| v0.4.0 RC | v0.4.0 + 3.5 週 | feature freeze, manual QA 開始 |
| v0.4.0 リリース | v0.4.0 + 4 週 | manual QA 完了 |

並列実装（Figma / Miro を別エンジニアに割当）で 2〜2.5 週間まで圧縮可能。

## 18. 関連ドキュメント

| ドキュメント | 内容 |
|---|---|
| `docs/hokusai-figma-miro-integration-implementation-plan.md` | Phase A〜H 全体計画（本書のベース） |
| `docs/hokusai-profile-parallel-execution-implementation-plan.md` | v0.3.0 profile 機能（本書が整合する基盤） |
| `docs/figma-miro-integration-operation-guide.md` | MVP 運用ガイド（Phase A〜D + F 範囲） |
| `docs/figma-miro-writeback-operation-guide.md` | 本書実装後の運用ガイド（Step 7 で新規作成） |
| `CHANGELOG.md` | v0.4.0 リリースノート |
