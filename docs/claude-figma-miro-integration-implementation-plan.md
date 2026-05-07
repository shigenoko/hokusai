# HOKUSAI × Figma / Miro 連携 実装計画書

**作成日**: 2026-05-08
**対象読者**: テックリード・実装担当エンジニア・運用担当
**位置付け**: `docs/claude-figma-miro-integration-requirements.md` の要件書に基づく実装計画書。本書は実装の唯一の真実とする。

---

## 1. 目的

要件書で定義された Figma / Miro 連携を、HOKUSAI に段階的に組み込む。Notion 連携で構築した同期基盤（best effort、SQLite outbox、冪等キー、Operations Console 統合）を最大限再利用しながら、Figma の API 制約とハイレベルな Miro 構造の解釈という新しい課題に対応する。

## 2. ゴールと非ゴール

### 2.1. ゴール

- `hokusai/integrations/figma/` と `hokusai/integrations/miro/` を、Notion 連携と同じパターンで実装する
- WorkflowRunner の Phase 2 / 3 / 5 / 6 / 8 に Figma / Miro フックを挿入する
- Notion / GitLab / Slack の既存連携を破壊しない
- 既存テスト（1078 件）を壊さない
- 段階的にリリースし、各段階で実用価値を出す

### 2.2. 非ゴール

- Figma 上のデザイン本体（frame / component）の編集
- Miro ボード本体の編集
- Miro → Figma の自動 UI 移植（要件書 §7.2 参照）
- リアルタイム双方向同期
- LLM による完全な UI 自動生成

## 3. アーキテクチャ

### 3.1. 既存基盤の再利用

Notion 同期で構築済みの以下を流用する:

| 基盤 | 流用内容 |
|---|---|
| `NotionSyncDispatcher` | パターンを継承して `FigmaSyncDispatcher` / `MiroSyncDispatcher` を作る |
| SQLite `notion_sync_outbox` / `notion_sync_errors` | 同じテーブル設計を `figma_sync_*` / `miro_sync_*` に複製 |
| 冪等キー方式 | `workflow_id:event_type:resource:revision` |
| `_safe_*_dispatch` | WorkflowRunner 上で Slack / Notion と同じパターン |
| `_detect_token_like_values` | Figma / Miro token のパターンを追加 |
| Operations Console の同期パネル | Figma / Miro 同期状態カードを追加 |

### 3.2. パッケージ構造

```text
hokusai/integrations/
├─ figma/
│   ├─ __init__.py          ← 公開 API (FigmaAPIClient, FigmaSyncDispatcher 等)
│   ├─ client.py            ← Figma REST API HTTP クライアント
│   ├─ files.py             ← File 構造・Frame・Variables 取得
│   ├─ images.py            ← Frame 画像 export
│   ├─ comments.py          ← Comments 取得・投稿
│   ├─ dispatcher.py        ← イベント発行と outbox 連携
│   └─ cache.py             ← SQLite ベースの File 構造キャッシュ
│
├─ miro/
│   ├─ __init__.py
│   ├─ client.py            ← Miro REST API HTTP クライアント
│   ├─ boards.py            ← Board / Items 取得
│   ├─ images.py            ← Board / Frame 画像 export
│   ├─ comments.py          ← Comments 取得・投稿
│   ├─ cards.py             ← App Card / Sticky Note 投稿
│   ├─ dispatcher.py
│   └─ cache.py
│
└─ webhook_bridge/          ← (Phase 2 / 4 で追加) Slack ブリッジと同居
    └─ figma_miro_handlers.py
```

### 3.3. 依存

両連携とも **標準ライブラリ urllib のみ**（Notion と同じ方針）。新規依存パッケージは追加しない。

## 4. 設定モデル

### 4.1. `WorkflowConfig` への追加

```python
@dataclass
class FigmaIntegrationConfig:
    """Figma 連携設定。デザイン参照とコメント書き戻し。"""
    enabled: bool = False
    api_token_env: str = "HOKUSAI_FIGMA_API_TOKEN"
    cache_ttl_seconds: int = 1800
    sync_outbox: NotionSyncOutboxConfig = ...  # 既存型を再利用
    retry: NotionSyncRetryConfig = ...
    rate_limit: NotionSyncRateLimitConfig = ...


@dataclass
class MiroIntegrationConfig:
    """Miro 連携設定。企画ボード参照とカード書き戻し。"""
    enabled: bool = False
    api_token_env: str = "HOKUSAI_MIRO_API_TOKEN"
    default_team_id_env: str = "HOKUSAI_MIRO_TEAM_ID"
    cache_ttl_seconds: int = 1800
    sync_outbox: NotionSyncOutboxConfig = ...
    retry: NotionSyncRetryConfig = ...
    rate_limit: NotionSyncRateLimitConfig = ...


@dataclass
class WorkflowConfig:
    # 既存フィールド...
    figma: FigmaIntegrationConfig = field(default_factory=FigmaIntegrationConfig)
    miro: MiroIntegrationConfig = field(default_factory=MiroIntegrationConfig)
```

### 4.2. YAML 設定例

```yaml
figma:
  enabled: true
  api_token_env: HOKUSAI_FIGMA_API_TOKEN
  cache_ttl_seconds: 1800
  rate_limit:
    requests_per_second: 1.5

miro:
  enabled: true
  api_token_env: HOKUSAI_MIRO_API_TOKEN
  default_team_id_env: HOKUSAI_MIRO_TEAM_ID
  cache_ttl_seconds: 1800
  rate_limit:
    requests_per_second: 1.5
```

### 4.3. 環境変数

```bash
# Figma
export HOKUSAI_FIGMA_API_TOKEN="figd_xxx..."

# Miro
export HOKUSAI_MIRO_API_TOKEN="..."
export HOKUSAI_MIRO_TEAM_ID="..."  # 任意
```

`_detect_token_like_values` に追加するパターン:
- `^figd_[A-Za-z0-9_-]{40,}$` → Figma Personal Access Token
- Miro token は形式が画一的でないためキー名（`miro` を含む）ベースで警告

## 5. データモデル

### 5.1. SQLite スキーマ追加

既存の `notion_sync_outbox` / `notion_sync_errors` と同じ構造を Figma / Miro 用に複製。

```sql
CREATE TABLE IF NOT EXISTS figma_sync_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE NOT NULL,
    workflow_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS figma_sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    failed_at TEXT NOT NULL
);

-- Miro も同様の 2 テーブル
CREATE TABLE IF NOT EXISTS miro_sync_outbox (...);
CREATE TABLE IF NOT EXISTS miro_sync_errors (...);

-- File 構造キャッシュ（Figma）
CREATE TABLE IF NOT EXISTS figma_file_cache (
    file_key TEXT PRIMARY KEY,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Board 構造キャッシュ（Miro）
CREATE TABLE IF NOT EXISTS miro_board_cache (
    board_id TEXT PRIMARY KEY,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
```

### 5.2. SQLiteStore の操作 API

`SQLiteStore` に以下のメソッドを追加（Notion 同期 API と同じシグネチャ）:

- `enqueue_figma_sync(idempotency_key, workflow_id, event_type, payload)`
- `list_pending_figma_sync(limit)`
- `mark_figma_sync_succeeded(idempotency_key)`
- `mark_figma_sync_failed(idempotency_key, error, next_attempt_at)`
- `move_figma_sync_to_error(idempotency_key, error)`
- `count_figma_sync_pending()` / `count_figma_sync_errors()`
- `cache_figma_file(file_key, raw_json)` / `get_cached_figma_file(file_key, max_age_seconds)`

Miro 用も同じ命名で `miro_*`。

### 5.3. ワークフロー state への追加

```python
class WorkflowState(TypedDict):
    # 既存...
    figma_url: str | None        # Phase 1 で抽出した Figma URL
    figma_file_key: str | None
    figma_target_node_id: str | None
    miro_url: str | None         # Phase 1 で抽出した Miro URL
    miro_board_id: str | None
```

## 6. Figma 連携の実装詳細

### 6.1. `FigmaAPIClient`（client.py）

**責務**: Figma REST API への HTTP リクエストの共通レイヤー。

```python
class FigmaAPIClient:
    BASE_URL = "https://api.figma.com/v1"

    def __init__(self, api_token, *, max_attempts=3, backoff_seconds=5,
                 requests_per_second=1.5, timeout=15):
        ...

    # 読み取り系
    def get_file(self, file_key, *, depth=None, ids=None) -> dict
    def get_file_nodes(self, file_key, ids: list[str]) -> dict
    def get_image(self, file_key, ids: list[str], format="png", scale=2) -> dict
    def get_image_fills(self, file_key) -> dict
    def get_components(self, file_key) -> dict
    def get_local_variables(self, file_key) -> dict
    def get_comments(self, file_key) -> dict

    # 書き戻し系
    def post_comment(self, file_key, message, *, client_meta=None) -> dict

    # 内部
    def _request(self, method, path, *, params=None, body=None) -> dict
```

レートリミット・リトライは Notion クライアントと同様に urllib + exponential backoff。

### 6.2. ドメインクライアント

#### `files.py` - File 構造取得

```python
class FigmaFileClient:
    def __init__(self, api: FigmaAPIClient, store: SQLiteStore, ttl_seconds: int):
        ...

    def get_file_summary(self, file_key) -> dict:
        """File 構造をキャッシュ込みで取得。ttl 内ならキャッシュから返す。"""

    def get_target_frame(self, file_key, node_id) -> dict:
        """特定 frame の構造を取得。"""

    def get_design_tokens(self, file_key) -> dict:
        """Variables を取得して { "color/primary": "#xxx" } 形式に整形。"""
```

#### `images.py` - 画像 export

```python
class FigmaImageClient:
    def export_frame(self, file_key, node_id, *, scale=2) -> bytes:
        """Frame を PNG として export。LLM の vision 入力用。"""
```

#### `comments.py` - コメント投稿・取得

```python
class FigmaCommentClient:
    def list_comments(self, file_key) -> list[dict]:
        """ボード上のコメント一覧を取得。"""

    def post_comment(self, file_key, message, *, node_id=None, x=None, y=None) -> dict:
        """指定 frame / 座標にコメント投稿。"""
```

### 6.3. `FigmaSyncDispatcher`（dispatcher.py）

Notion dispatcher と同じパターン:
- `is_configured()` で enabled / token を確認
- `dispatch(event_type, payload)` で送信を試み、失敗時は outbox へ
- `retry_pending(limit)` で Operations Console から再送

イベント種別:
| event_type | 内容 | ルーティング先 |
|---|---|---|
| `comment_post` | Frame にコメント投稿 | `FigmaCommentClient.post_comment` |
| `webhook_ack` | Webhook 受信応答 | （ログのみ） |

### 6.4. URL → file_key / node_id 抽出

Figma URL の例:
```
https://www.figma.com/file/<file_key>/<title>?node-id=<node_id>
https://www.figma.com/design/<file_key>/<title>?node-id=<node_id>
```

`hokusai/integrations/figma/url_parser.py`:
```python
def parse_figma_url(url: str) -> tuple[str | None, str | None]:
    """(file_key, node_id) を返す。失敗時は (None, None)。"""
```

## 7. Miro 連携の実装詳細

### 7.1. `MiroAPIClient`（client.py）

```python
class MiroAPIClient:
    BASE_URL = "https://api.miro.com/v2"

    def __init__(self, api_token, *, max_attempts=3, backoff_seconds=5,
                 requests_per_second=1.5, timeout=15):
        ...

    # 読み取り系
    def get_board(self, board_id) -> dict
    def list_items(self, board_id, *, type=None, cursor=None) -> dict
    def get_item(self, board_id, item_id) -> dict
    def get_image(self, board_id, item_id) -> bytes  # Frame 単位 export
    def list_comments(self, board_id) -> dict

    # 書き戻し系
    def create_sticky_note(self, board_id, content, *, position, parent=None) -> dict
    def create_card(self, board_id, title, *, description=None, position) -> dict
    def create_app_card(self, board_id, title, *, description=None, fields=None) -> dict
    def create_comment(self, board_id, item_id, content) -> dict
```

### 7.2. ドメインクライアント

#### `boards.py` - Board / Items 取得

```python
class MiroBoardClient:
    def get_board_summary(self, board_id) -> dict:
        """Board 内のフレーム・付箋・テキストを構造化して返す。"""

    def list_items_in_frame(self, board_id, frame_id) -> list[dict]:
        """特定フレーム内のアイテムのみ取得（運用ルールでフレーム命名を活用）。"""

    def extract_sticky_notes_by_color(self, board_id) -> dict[str, list[str]]:
        """付箋を色別に分類して返す（運用ルール: 色 = 意味）。"""
```

#### `cards.py` - Card / Sticky Note 投稿

```python
class MiroCardClient:
    def post_implementation_card(self, board_id, *, frame_id, pr_url,
                                 title="HOKUSAI: 実装完了") -> dict:
        """指定フレームに実装完了カードを投稿。"""
```

### 7.3. URL → board_id 抽出

Miro URL の例:
```
https://miro.com/app/board/<board_id>/
https://miro.com/app/board/<board_id>/?moveToWidget=<item_id>
```

`hokusai/integrations/miro/url_parser.py`:
```python
def parse_miro_url(url: str) -> tuple[str | None, str | None]:
    """(board_id, item_id) を返す。"""
```

## 8. WorkflowRunner への統合

### 8.1. フック挿入箇所

既存の `_safe_notify` / `_safe_notion_dispatch` と同列で `_safe_figma_*` / `_safe_miro_*` を追加。

| Phase | 既存処理 | 追加するフック |
|---|---|---|
| Phase 1 | タスク URL 取得 | task 本文から Figma URL / Miro URL を抽出して state に保存 |
| Phase 2 | 既存コード調査 | Figma File 取得、Miro Board 取得 → LLM プロンプトに含める |
| Phase 3 | 設計書作成 | 取得済み Figma / Miro 情報を参照に渡す |
| Phase 5 | LLM 実装 | Figma 画像を vision 入力、Miro 付箋を context に含める |
| Phase 6 | 検証 | （Phase 5 拡張）Figma frame と実装スクリーンショットを比較 |
| Phase 8a | PR 作成 | Figma にコメント、Miro にカード投稿 |
| Phase 8 統合レビューループ | コメント応答 | Figma / Miro コメント取得 → 応答コメント |
| Phase 9 / 10 | 完了処理 | Figma / Miro に完了通知 |

### 8.2. WorkflowRunner.__init__ の拡張

```python
self.figma_dispatcher = FigmaSyncDispatcher(
    store=self.store, config=self.config.figma
)
self.miro_dispatcher = MiroSyncDispatcher(
    store=self.store, config=self.config.miro
)
```

### 8.3. ヘルパ関数

```python
def _safe_figma_dispatch(self, event_type, payload):
    try:
        self.figma_dispatcher.dispatch(event_type, payload)
    except Exception as e:
        logger.debug(f"Figma 同期で例外を抑制: event={event_type}, error={e}")

def _safe_miro_dispatch(self, event_type, payload):
    # 同様
```

### 8.4. `_enrich_state_with_design_context()`

既存の `_enrich_state_with_notion_url()` と同じパターンで、Figma / Miro 情報を state に追加する関数を追加:

```python
def _enrich_state_with_design_context(self, state: dict) -> dict:
    """Phase 5 等で LLM 入力に渡すため、state に design context を追加。"""
    enriched = dict(state)
    if state.get("figma_file_key"):
        enriched["figma_summary"] = self._figma_files.get_file_summary(...)
        enriched["figma_image_url"] = ...
    if state.get("miro_board_id"):
        enriched["miro_summary"] = self._miro_boards.get_board_summary(...)
    return enriched
```

## 9. Operations Console 拡張

### 9.1. 同期状態パネル追加

`scripts/dashboard.py` に Figma / Miro セクションを追加:

```python
def render_figma_dashboard_panel():
    """Notion パネルと同じ構造で Figma 同期状態を表示。"""
    # outbox 件数 / errors 件数 / 同期再送ボタン

def render_miro_dashboard_panel():
    """同上。"""
```

### 9.2. POST API 追加

| エンドポイント | 用途 |
|---|---|
| `/api/figma/retry-pending` | Figma outbox 再送 |
| `/api/miro/retry-pending` | Miro outbox 再送 |
| `/api/figma/test-connection` | API token の有効性確認 |
| `/api/miro/test-connection` | 同上 |

### 9.3. トップページレイアウト

```
[Notion メインダッシュボード パネル]
[Figma 同期状態 パネル]    ← 新規
[Miro 同期状態 パネル]     ← 新規
[ワークフロー一覧]
[設定 / 接続状態]
```

## 10. セキュリティ

### 10.1. token 管理

- `HOKUSAI_FIGMA_API_TOKEN` / `HOKUSAI_MIRO_API_TOKEN` は **環境変数経由のみ**
- YAML 直書きは `_detect_token_like_values` で警告
- ログ・例外メッセージ・Notion 本文・Slack 本文に出さない
- Operations Console から token を表示しない（接続テストのみ）

### 10.2. コメント投稿の権限分離

- Figma integration / Miro integration は **コメント投稿に必要な最小権限のみ**
- ワークスペース全体の編集権限は付与しない

### 10.3. キャッシュの取り扱い

- File 構造キャッシュは平文で SQLite に保存（既存方針と同じ）
- 機密情報を含む可能性を踏まえ、SQLite ファイルのパーミッションを 600 に設定（既存と同じ）
- キャッシュ TTL を超えたら自動削除

### 10.4. Webhook 認証

Phase 2 / 4 で Webhook 受信を実装する場合、Slack ブリッジと同じパターン:
- HMAC-SHA256 署名検証
- 共有シークレットを環境変数経由で渡す
- 全リクエストを監査ログに記録

## 11. テスト戦略

### 11.1. 単体テスト

| テスト対象 | カバー範囲 |
|---|---|
| `FigmaAPIClient` | 各エンドポイントの正常系、429 / 5xx リトライ、token をログに出さない |
| `MiroAPIClient` | 同上 |
| `FigmaSyncDispatcher` / `MiroSyncDispatcher` | dispatch 成功 / 失敗 / outbox 蓄積 / 再送 / 永続失敗 |
| URL parser | 各種 Figma / Miro URL からの ID 抽出、不正 URL の扱い |
| `_detect_token_like_values` | Figma token パターン検出 |
| SQLite outbox API | enqueue / list / mark / move を Figma / Miro 別に検証 |
| WorkflowRunner フック | 各 Phase でフックが呼ばれること、例外が抑制されること |

### 11.2. 結合テスト

- Notion + Figma + Miro を全て enabled にした状態で 1 ワークフロー実行
- 全イベントが各ツールに反映されること
- 1 ツール障害（例: Figma 401）でも他は影響なし

### 11.3. 既存テストへの影響

- 既存 1078 件は壊さない
- enabled=False の場合に既存挙動と完全互換であることを確認

### 11.4. テスト件数の見込み

| パッケージ | 想定テスト件数 |
|---|---|
| `tests/test_figma_integration.py` | ~40 件 |
| `tests/test_miro_integration.py` | ~35 件 |
| `tests/test_dashboard_design_panels.py` | ~10 件 |
| WorkflowRunner フック追加分 | ~15 件 |
| **合計追加** | **~100 件** |

## 12. 段階的実装ステップ

### Phase A: Figma 連携 MVP（4〜6 週間）

#### A-1: 設定モデル + SQLite 拡張（3〜5 日）
- `FigmaIntegrationConfig` 追加、loader / manager 統合
- `_detect_token_like_values` に Figma パターン追加
- SQLite に `figma_sync_outbox` / `figma_sync_errors` / `figma_file_cache` テーブル + 操作 API
- `WorkflowState` に `figma_url` / `figma_file_key` / `figma_target_node_id` 追加

#### A-2: API クライアント + キャッシュ層（5〜7 日）
- `FigmaAPIClient`（HTTP 基盤、レートリミット、リトライ）
- `FigmaFileClient`（File 構造取得、キャッシュ込み）
- `FigmaImageClient`（Frame 画像 export）
- `FigmaCommentClient`（コメント投稿・取得）
- URL parser

#### A-3: SyncDispatcher（3〜4 日）
- `FigmaSyncDispatcher`（outbox 連携、冪等キー、retry_pending）
- `_safe_figma_dispatch` を WorkflowRunner に追加

#### A-4: WorkflowRunner 統合（5〜7 日）
- Phase 1: URL 抽出
- Phase 2 / 3: Figma File 構造を LLM プロンプトに含める
- Phase 5: Frame 画像を vision 入力
- Phase 8a: 該当 frame にコメント投稿

#### A-5: テスト + ドキュメント（3〜5 日）
- 単体テスト ~40 件
- 結合テスト
- 運用ガイド `docs/figma-integration-operation-guide.md`

#### Phase A の DoD
- [ ] Notion タスクに Figma URL を含めて `hokusai start` 実行 → Phase 5 LLM プロンプトに Figma 情報が入る
- [ ] Phase 8a 完了時、該当 frame にコメントが投稿される
- [ ] Figma 障害時もワークフロー本体は止まらず完走する
- [ ] outbox 蓄積・再送が動作する
- [ ] 全テストが通る

### Phase B: Figma レビューループ（2〜3 週間）

#### B-1: コメント取得・応答処理（4〜6 日）
- Phase 8 統合レビューループに Figma コメント取得を追加
- 応答処理（修正完了報告）
- Slack 通知に Figma コメントへのディープリンク追加

#### B-2: Webhook 中継サーバー（5〜7 日）
- 既存 Slack ブリッジと同居する形で Figma Webhook ハンドラを追加
- HMAC 認証
- FILE_COMMENT イベント受信 → ワークフローを Waiting for Human 化

#### B-3: テスト + ドキュメント（3〜4 日）

#### Phase B の DoD
- [ ] デザイナーが Figma にコメント → HOKUSAI が修正 → Figma に応答コメント
- [ ] Webhook 経由でコメント追加が即時に検知される
- [ ] HMAC 認証が動作する

### Phase C: Miro 連携 MVP（3〜4 週間）

#### C-1: 設定 + SQLite 拡張（2〜3 日）
- `MiroIntegrationConfig` 追加
- SQLite に Miro 用テーブル
- `WorkflowState` に `miro_url` / `miro_board_id`

#### C-2: API クライアント + キャッシュ層（5〜7 日）
- `MiroAPIClient`
- `MiroBoardClient`（Board / Items 取得）
- `MiroCardClient`（Card / Sticky Note 投稿）
- URL parser

#### C-3: SyncDispatcher（3〜4 日）
- `MiroSyncDispatcher`
- WorkflowRunner 統合

#### C-4: WorkflowRunner 統合（4〜6 日）
- Phase 1: URL 抽出
- Phase 2: Miro Board の付箋・図解を LLM プロンプトに含める
- Phase 8a: 該当フレームに実装完了カード投稿

#### C-5: テスト + ドキュメント（3〜5 日）
- 運用ガイド `docs/miro-integration-operation-guide.md`
- Miro ボード命名規則・付箋カラー意味付けのガイド整備

#### Phase C の DoD
- [ ] Notion タスクに Miro URL を含めて起動 → Phase 2 の調査入力に Miro 内容が反映される
- [ ] Phase 8a 完了時に Miro カードが投稿される

### Phase D: Miro レビューループ（2〜3 週間）

- D-1: コメント取得・応答処理
- D-2: Webhook 中継サーバー拡張
- D-3: テスト

### Phase E: 視覚回帰テスト（任意・3〜4 週間）

- E-1: Phase 6 で実装スクリーンショット取得（Playwright 等）
- E-2: Figma frame と差分検出（pixel diff or LLM vision 比較）
- E-3: 差分が大きければ Phase 5 リトライ
- E-4: 差分レポートを Notion DB に記録

### Phase F: Operations Console 拡張（1〜2 週間）

- F-1: Figma / Miro 同期状態パネル追加
- F-2: 接続テストボタン
- F-3: 再送ボタン

### 工数まとめ

| 段階 | 内容 | 工数 |
|---|---|---|
| A | Figma MVP | 4〜6 週間 |
| B | Figma レビューループ | 2〜3 週間 |
| C | Miro MVP | 3〜4 週間 |
| D | Miro レビューループ | 2〜3 週間 |
| E | 視覚回帰テスト（任意） | 3〜4 週間 |
| F | Operations Console 拡張 | 1〜2 週間 |

**最小ライン（A + C + F）**: 8〜12 週間
**完全版（A 〜 F）**: 15〜22 週間（並行作業前提で 12〜16 週間）

## 13. 設定例（最小実装後の YAML）

```yaml
# 既存
notion_dashboard:
  enabled: true

# 新規
figma:
  enabled: true
  api_token_env: HOKUSAI_FIGMA_API_TOKEN
  cache_ttl_seconds: 1800
  retry:
    max_attempts: 3
    backoff_seconds: 5
  rate_limit:
    requests_per_second: 1.5

miro:
  enabled: true
  api_token_env: HOKUSAI_MIRO_API_TOKEN
  cache_ttl_seconds: 1800
  retry:
    max_attempts: 3
    backoff_seconds: 5
  rate_limit:
    requests_per_second: 1.5
```

## 14. パフォーマンス想定

| 項目 | Figma | Miro |
|---|---|---|
| 1 ワークフローあたり API 呼び出し | 5〜15 回 | 3〜10 回 |
| File / Board 構造取得レイテンシ | 1〜3 秒 | 1〜3 秒 |
| 画像 export レイテンシ | 2〜5 秒 | 2〜5 秒 |
| キャッシュヒット時の応答 | <50ms | <50ms |
| レートリミット | プラン依存 | 100 req/min（Free） |
| 並行ワークフロー上限 | 5〜10 件 | 5〜10 件 |

キャッシュ + デバウンスで API 呼び出しを最小化。

## 15. リスクと対策

| リスク | 対策 |
|---|---|
| Figma / Miro API 障害でワークフロー停止 | best effort 設計、SQLite outbox に蓄積、Operations Console から再送 |
| API レートリミット超過 | キャッシュ TTL + デバウンス + リトライ |
| Miro が構造化されておらず LLM が誤解釈 | 運用ルール（フレーム命名・付箋カラー）の整備で吸収。LLM への入力を限定 |
| Figma / Miro token 漏洩 | 環境変数経由のみ、`_detect_token_like_values` で検出、Operations Console で token を表示しない |
| デザイナーが Figma 編集を期待 | 非ゴールとして運用ガイドに明記、HOKUSAI はコメントのみ書き戻す |
| 5 ツールの情報が不整合 | 各ツールの責任範囲を明示（Notion = 仕様、Miro = 議論、Figma = UI、GitLab = 実装、Slack = 通知） |
| 画像 export のサイズ過大 | scale=1〜2 を上限、必要 frame のみ取得 |
| キャッシュが古くなる | TTL（既定 30 分）で自動失効、Phase 2 開始時に refresh |

## 16. Open Questions

実装着手前に明示的に確定させる項目:

1. **Figma の対象スコープ**
   - チーム全体 / 特定プロジェクト / ファイルごとに ID 列挙
   - 暫定案: ファイルごとに Notion タスクから URL を渡す方式（最小スコープ）

2. **Miro の運用ルール策定責任**
   - フレーム命名規則・付箋カラー意味付けを誰が定義するか
   - 暫定案: ビジネスサイドのテックリードが運用ルールを定義

3. **Webhook 中継サーバーのホスティング**
   - 暫定案: Slack ブリッジと同居（同じインフラ）

4. **視覚回帰テスト（Phase E）の優先度**
   - 暫定案: A〜D 完了後に必要性を再評価、必要なら実装

5. **Figma / Miro の Token 発行主体**
   - 暫定案: 組織管理者が HOKUSAI 専用 integration を作成、Personal Access Token を発行

6. **Token 統一管理**
   - 暫定案: Notion / Slack token と同じく、組織管理者が一元管理

7. **キャッシュ TTL のデフォルト値**
   - 暫定案: 1800 秒（30 分）。デザイン更新頻度に応じて運用で調整

各項目の暫定案で進めて差し支えなければ、レビュアからの no-objection をもって着手する。

## 17. 受け入れ基準（Definition of Done）統合

### 17.1. 全体 DoD

- [ ] Notion タスクに Figma / Miro URL を貼れば、HOKUSAI が読み取って実装に活用する
- [ ] Phase 8a 完了時、Figma / Miro の該当箇所にコメント / カードが自動投稿される
- [ ] Figma / Miro 障害でワークフロー本体は止まらない
- [ ] 同期失敗は SQLite outbox に保存され、Operations Console から再送できる
- [ ] 全 token は環境変数経由のみ、`_detect_token_like_values` の警告対象になる
- [ ] 全 Phase の単体・結合テストが追加されている（~100 件追加）
- [ ] 既存 1078 件のテストが通り続ける
- [ ] 運用ガイドが整備されている（Figma / Miro それぞれ）

### 17.2. 段階別 DoD

各段階の完了条件は §12 段階的実装ステップ参照。

## 18. 関連ドキュメント

| ドキュメント | 関係 |
|---|---|
| `docs/claude-figma-miro-integration-requirements.md` | 本書の前提となる要件書 |
| `docs/hokusai-notion-dashboard-implementation-plan.md` | Notion 連携の実装計画（再利用元） |
| `docs/codex-slack-notification-implementation-plan.md` | Slack 連携の実装計画 |
| `docs/notion-dashboard-operation-guide.md` | Notion 運用ガイド（同様の構成で Figma / Miro 用を整備予定） |
| `docs/notion-dashboard-verification-checklist.md` | Notion 動作確認チェックリスト（同様の構成で Figma / Miro 用を整備予定） |

## 19. まとめ

| 項目 | 内容 |
|---|---|
| 既存基盤の再利用 | Notion 連携の dispatcher / outbox / 冪等キー / Operations Console パターンを流用 |
| 新規依存 | なし（urllib のみ） |
| ファイル構成 | `hokusai/integrations/figma/` と `hokusai/integrations/miro/` を新設 |
| WorkflowRunner 統合 | Phase 1 / 2 / 3 / 5 / 8a / 8 にフック追加 |
| 連携の非対称性 | Figma / Miro は読み中心 + コメント書き戻し（本体編集なし） |
| 工数（最小ライン） | 8〜12 週間（A + C + F） |
| 工数（完全版） | 15〜22 週間（A〜F、並行作業前提で 12〜16 週間） |
| 受け入れ基準 | Phase 別 DoD + 全体 DoD（§17） |

レビュアからの no-objection を得たうえで、**Phase A（Figma MVP）から着手**することを推奨する。Phase A 完了後、運用フィードバックを踏まえて Phase B 以降を順次進める。
