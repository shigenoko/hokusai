# HOKUSAI Figma / Miro 連携 統合実装計画書

**作成日**: 2026-05-08
**対象読者**: プロジェクト責任者・PM・テックリード・実装担当エンジニア
**位置付け**: 本ドキュメントは、以下 2 つの実装計画案を統合したものである。本書を実装の唯一の真実とする。

- ベース設計: `docs/codex-figma-miro-integration-implementation-plan.md`（既存コードベース統合・DesignContextResolver 抽象化・`on_failure` 戦略・Phase 横断統合）
- 補完: `docs/claude-figma-miro-integration-implementation-plan.md`（Phase 別工数試算・SQLite outbox 連携・Open Questions・性能試算・テスト件数試算）

前提要件書: `docs/codex-figma-miro-integration-requirements.md`（Figma / Miro 連携要件書）

---

## 1. 目的

HOKUSAI に Figma / Miro 連携を追加し、Notion / Miro / Figma / GitLab を横断した開発ワークフローを実現する。

役割分担:
- **Notion**: 要件・進捗共有の正本、HOKUSAI のタスク起点
- **Miro**: ビジネス側の業務フロー・ラフスケッチの入力元
- **Figma**: UI / UX デザインの正本
- **GitLab**: 実装・レビューの正本

HOKUSAI は各ツールを置き換えず、Notion タスクを起点に Miro / Figma の情報を読み取り、調査・設計・実装・レビュー・MR へ反映する。MVP では読み取り失敗を warning / cache refresh で扱い、Phase E 以降の書き戻しでは Notion 連携で構築済みの同期基盤（best effort、SQLite outbox、冪等キー、Operations Console 統合）を再利用する。

## 2. ゴールと非ゴール

### 2.1. ゴール

- Notion タスクに Miro URL / Figma URL を指定できる
- HOKUSAI が Miro / Figma URL を検出し、概要情報を取得できる
- Phase 2 / Phase 3 の調査・設計チェックに Miro / Figma 情報を反映できる
- Phase 5 の実装プロンプトに Figma の画面仕様を参照情報として渡せる
- GitLab MR に Notion / Miro / Figma の関連リンクを自動記載できる
- Notion Dashboard に Miro / Figma の連携状態、同期時刻、確認状態を表示できる
- Miro / Figma 連携が無効、未設定、取得失敗の場合でも既存ワークフローを壊さない
- Notion 連携で構築済みの品質基盤（best effort、outbox、冪等キー）を、書き戻し・再送が必要な Phase E 以降で踏襲する
- 既存テスト（1078 件）を壊さない

### 2.2. 非ゴール

- Figma 上で完成デザインを自動生成する
- Miro の手描きスケッチを完全な UI デザインへ自動変換する（Figma REST API 制約）
- Figma と実装画面のピクセル完全一致判定を行う
- Figma / Miro への双方向コメント同期を実装する（MVP では未解決コメントの読み取りのみ行い、書き戻しは Phase E 以降）
- Figma Variables から production design token を自動更新する
- Notion から Miro / Figma を直接編集する
- リアルタイム双方向同期

## 3. 実装方針サマリ

MVP では「読み取り中心」「best effort」「既存ワークフロー非破壊」を基本方針とする。

| 領域 | 方針 |
|---|---|
| Miro | API / MCP で取得できる範囲のボード概要、frame、付箋、テキスト、図形情報を取得 |
| Figma | REST API で file / node / image / comments の概要を取得 |
| Notion | URL と同期状態を Workflows DB に追加し、詳細は Phase 子ページに保存 |
| GitLab | MR description に Notion / Miro / Figma リンクとデザイン確認状態を追記 |
| HOKUSAI Runtime | Phase 2 / 3 / 4 / 5 / 7 / 8 / 10 へ段階的に接続 |
| 失敗時 | 外部 API 取得失敗は warning 扱い、必要な場合だけ Waiting for Human |
| 読み取り失敗 | MVP では state の `design_sync_errors` に記録し、再取得 / cache refresh で復旧 |
| 書き戻し失敗 | Phase E 以降は SQLite outbox に蓄積し、Operations Console から再送可能 |

## 4. 全体アーキテクチャ

### 4.1. データフロー

```text
Notion Task
  ├─ Miro URL
  ├─ Figma URL
  └─ GitLab Issue URL
       ↓
HOKUSAI Runtime
  ├─ DesignContextResolver
  │   ├─ MiroClient
  │   └─ FigmaClient
  ├─ Phase 2 Research
  ├─ Phase 3 Design Check
  ├─ Phase 4 Plan
  ├─ Phase 5 Implement
  ├─ Phase 7 Review
  ├─ Phase 8 MR / Review Loop
  └─ Phase 10 Record
       ↓
Outputs
  ├─ Notion Phase subpages
  ├─ Notion Workflows DB
  ├─ GitLab MR description
  └─ Slack / Waiting for Human notification
```

### 4.2. 既存基盤の再利用

Notion 同期で構築済みの以下を流用する:

| 基盤 | 流用内容 |
|---|---|
| `NotionSyncDispatcher` | 書き戻し・再送が必要な Phase E 以降で、同じ dispatcher パターンを継承する |
| SQLite `notion_sync_outbox` / `notion_sync_errors` | Phase E 以降で同じテーブル設計を `figma_sync_*` / `miro_sync_*` に複製 |
| 冪等キー方式 | `workflow_id:event_type:resource:revision` |
| `_safe_*_dispatch` | WorkflowRunner 上で Slack / Notion と同じパターン |
| `_detect_token_like_values` | Figma / Miro token のパターンを追加 |
| Operations Console | MVP では接続状態・cache refresh、Phase E 以降では outbox 再送パネルを追加 |

### 4.3. 依存

新規依存パッケージは追加しない。**標準ライブラリ urllib のみ**（Notion / Slack 連携と同じ方針）。

## 5. データモデル

### 5.1. WorkflowState 追加フィールド

`hokusai/state.py` に以下を追加する。

| フィールド | 型 | 内容 |
|---|---|---|
| `miro_url` | `Optional[str]` | Notion タスクから抽出した Miro URL |
| `figma_url` | `Optional[str]` | Notion タスクから抽出した Figma URL |
| `miro_board_id` | `Optional[str]` | パース済み Miro Board ID |
| `figma_file_key` | `Optional[str]` | パース済み Figma File Key |
| `figma_target_node_id` | `Optional[str]` | パース済み Figma 対象 Node ID |
| `miro_context` | `Optional[dict]` | Miro から取得・要約した情報 |
| `figma_context` | `Optional[dict]` | Figma から取得・要約した情報 |
| `design_integration_status` | `Optional[str]` | `not_configured` / `synced` / `partial` / `failed` |
| `design_review_required` | `bool` | デザイン確認が必要か |
| `design_review_result` | `Optional[str]` | `pending` / `approved` / `changes_requested` |
| `design_sync_errors` | `list[str]` | Miro / Figma 同期時の警告・エラー |

### 5.2. 共通コンテキスト形式

Figma / Miro の生データを直接プロンプトに渡さず、HOKUSAI 内部で短い共通形式に正規化する。これにより Phase 2 / 3 / 5 のプロンプト差し込みとテストが安定する。

```python
{
    "source": "figma" | "miro",
    "url": "...",
    "title": "...",
    "updated_at": "...",
    "summary": "...",
    "screens": [
        {
            "name": "...",
            "node_id": "...",
            "description": "...",
            "texts": ["..."],
            "components": ["..."],
            "notes": ["..."],
        }
    ],
    "comments": [
        {
            "author": "...",
            "body": "...",
            "resolved": false,
        }
    ],
    "warnings": ["..."],
}
```

### 5.3. SQLite スキーマ追加

Phase E 以降で書き戻しを実装する際は、既存の `notion_sync_outbox` / `notion_sync_errors` と同じ構造を Figma / Miro 用に複製する。MVP の読み取り失敗はこの outbox ではなく、`design_sync_errors` と cache refresh で扱う。

```sql
-- Figma 同期用 outbox
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

-- Miro 同期用 outbox
CREATE TABLE IF NOT EXISTS miro_sync_outbox (
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

CREATE TABLE IF NOT EXISTS miro_sync_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    failed_at TEXT NOT NULL
);

-- File / Board 構造キャッシュ（API 呼び出し抑制用、TTL 30 分）
CREATE TABLE IF NOT EXISTS figma_file_cache (
    file_key TEXT PRIMARY KEY,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS miro_board_cache (
    board_id TEXT PRIMARY KEY,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
```

`SQLiteStore` に既存 Notion 同期 API と同じシグネチャで以下を追加:
- `enqueue_figma_sync()` / `list_pending_figma_sync()` / `mark_figma_sync_succeeded()` / `mark_figma_sync_failed()` / `move_figma_sync_to_error()` / `count_figma_sync_pending()` / `count_figma_sync_errors()`
- `cache_figma_file()` / `get_cached_figma_file(max_age_seconds)`
- Miro 用も同じ命名で `miro_*`

## 6. 設定設計

### 6.1. YAML 設定例

```yaml
figma:
  enabled: true
  api_token_env: HOKUSAI_FIGMA_API_TOKEN
  fetch_comments: true
  export_images: true
  cache_ttl_seconds: 1800
  timeout: 10
  on_failure: warn  # warn | block | skip
  retry:
    max_attempts: 3
    backoff_seconds: 5
  rate_limit:
    requests_per_second: 1.5

miro:
  enabled: true
  api_token_env: HOKUSAI_MIRO_API_TOKEN
  default_team_id_env: HOKUSAI_MIRO_TEAM_ID
  use_mcp: false
  cache_ttl_seconds: 1800
  timeout: 10
  on_failure: warn
  retry:
    max_attempts: 3
    backoff_seconds: 5
  rate_limit:
    requests_per_second: 1.5
```

### 6.2. 追加する config dataclass

`hokusai/config/models.py` に以下を追加:

- `FigmaIntegrationConfig`
- `MiroIntegrationConfig`
- `WorkflowConfig.figma` / `WorkflowConfig.miro`

`hokusai/config/loaders.py` に以下を追加:
- `_parse_figma_config(config_dict)`
- `_parse_miro_config(config_dict)`

`hokusai/config/manager.py` で parse + 不要キー pop 対象に追加。

### 6.3. 環境変数

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

## 7. URL 抽出

### 7.1. 抽出対象

Notion タスク本文と Notion Dashboard DB のプロパティから以下を抽出:

- `https://www.figma.com/file/...`
- `https://www.figma.com/design/...`
- `https://www.figma.com/proto/...`
- `https://miro.com/app/board/...`

### 7.2. 実装場所

新規モジュール: `hokusai/integrations/design/url_parser.py`

責務:
- Figma URL から `file_key` / `node_id` を抽出
- Miro URL から `board_id` / `item_id` を抽出
- 複数 URL がある場合の優先順位付け
- 抽出できない場合は警告として返す

## 8. Figma 連携実装

### 8.1. 追加ファイル

```text
hokusai/integrations/design/
├─ __init__.py
├─ url_parser.py
├─ context.py            ← DesignContextResolver
├─ figma.py              ← FigmaClient
├─ miro.py               ← MiroClient
└─ cache.py              ← SQLite キャッシュ層

tests/integrations/
├─ test_design_url_parser.py
├─ test_design_context.py
├─ test_figma_client.py
└─ test_miro_client.py
```

### 8.2. FigmaClient の責務

MVP では read-only とする。

- API token の存在確認
- file 情報の取得（キャッシュ込み）
- node 情報の取得
- 画像 export URL の取得
- コメント取得
- 取得結果の共通コンテキスト化
- API エラー、権限エラー、rate limit の警告化

コメント投稿は MVP には含めず、Phase E の書き戻し機能で `post_comment` として追加する。

### 8.3. MVP で取得する情報

- file 名
- frame / node 名
- node id
- 最終更新日時
- テキスト
- 主要レイアウト情報
- component / instance 名
- コメント件数、未解決コメント
- 画像 export URL

### 8.4. 実装上の注意

- Figma API の生レスポンスをそのまま state に保存しない
- プロンプトに渡す情報は要約済みの `figma_context` に限定する
- token はログ、Notion、GitLab、Slack に出さない
- 画像取得は失敗しても text / node 情報の取得ができれば `partial` とする
- File 構造は SQLite にキャッシュ（TTL 30 分）して API 呼び出しを抑制

## 9. Miro 連携実装

### 9.1. MiroClient の責務

MVP では read-only とする。

- API token または MCP 利用可否の確認
- board 情報の取得（キャッシュ込み）
- item 一覧の取得
- frame / sticky note / text / shape / connector の抽出
- 業務フローとラフ画面構成の要約
- 取得結果の共通コンテキスト化
- API / MCP エラーの警告化

### 9.2. MVP で取得する情報

- board 名
- frame 名
- sticky note のテキスト
- text item
- shape 名またはテキスト
- connector の関係
- コメントまたは補足メモ
- 最終更新日時

### 9.3. MCP と API の扱い

初期実装では API クライアントを標準経路とする。MCP は将来の自然言語操作やボード生成に向いているため、設定として `use_mcp` を残すが、MVP では必須にしない。

## 10. Design Context Resolver

### 10.1. 追加ファイル

- `hokusai/integrations/design/context.py`
- `tests/integrations/test_design_context.py`

### 10.2. 責務

`DesignContextResolver` を追加し、Phase ノードから個別に Figma / Miro API を直接呼ばない構造にする。

責務:
- state / task content から Miro / Figma URL を抽出する
- 設定が無効なら `not_configured` とする
- FigmaClient / MiroClient を呼び出す
- 取得結果を `miro_context` / `figma_context` に格納する
- 失敗時の挙動を `on_failure` に従って決める
- プロンプト差し込み用 Markdown を生成する

### 10.3. 失敗時ポリシー

| `on_failure` | 挙動 |
|---|---|
| `warn` | 警告を state に記録し、ワークフローは続行 |
| `block` | Waiting for Human にして停止 |
| `skip` | 取得をスキップし、既存ワークフロー通り続行 |

MVP のデフォルトは `warn` とする。

## 11. Phase 別実装計画

### Phase 2: Research

対象ファイル:
- `hokusai/nodes/phase2_research.py`
- `prompts/phase2/task_research.md`
- `prompts/phase2/task_research_retry.md`

実装内容:
- Phase 2 開始前に `DesignContextResolver` を呼ぶ
- Notion タスク内の Miro / Figma URL を抽出する
- Miro / Figma の概要を取得する
- 調査プロンプトに `## 外部デザイン・業務フロー情報` セクションを追加する
- 調査結果に Miro / Figma の参照情報と不足情報を含める

完了条件:
- Miro URL があるタスクで、Phase 2 出力に業務フロー概要が含まれる
- Figma URL があるタスクで、Phase 2 出力に対象画面概要が含まれる
- 取得失敗しても Phase 2 が既存通り完了できる

### Phase 3: Design Check

対象ファイル:
- `hokusai/nodes/phase3_design.py`
- `prompts/phase3/design_check.md`
- `prompts/phase3/design_check_retry.md`

実装内容:
- `_build_design_check_prompt()` に design context セクションを渡せるようにする
- Notion 要件、Miro 業務フロー、Figma UI 仕様の突き合わせ観点を追加する
- ズレ検知結果を Phase 3 出力に含める
- `on_failure=block` または重大な不整合時に Waiting for Human へ遷移できるようにする

完了条件:
- Figma / Miro ありのタスクで Phase 3 に整合性チェックが出力される
- 不足 URL、未解決コメント、古い更新日時が警告として出力される

### Phase 4: Plan

対象ファイル:
- `hokusai/nodes/phase4_plan.py`
- `prompts/phase4/dev_plan*.md`

実装内容:
- 実装計画に参照すべき Miro / Figma URL を含める
- デザインレビューが必要なタイミングを明記する
- 実装上 Figma と差分が出る可能性がある場合の記録ルールを追加する

完了条件:
- Phase 4 出力に Miro / Figma 参照対象が明記される

### Phase 5: Implement

対象ファイル:
- `hokusai/nodes/phase5_implement.py`
- `prompts/phase5/implementation.md`
- `prompts/phase5/retry_fix.md`

実装内容:
- 実装プロンプトに Figma の画面構成、テキスト、コンポーネント、スタイル概要を渡す
- Miro の業務フローを仕様補足として渡す
- デザインとの差分が発生した場合は実装結果に記録するよう指示する

完了条件:
- 実装プロンプトに design context が含まれる
- design context がない場合でも既存プロンプトと互換性がある

### Phase 7: Review

対象ファイル:
- `hokusai/nodes/phase7_review.py`
- `hokusai/review_checklist.md`
- `prompts/phase7/final_review.md`

実装内容:
- UI / UX レビューに Figma 参照観点を追加する
- Figma URL がある場合、実装が対象画面に沿っているか確認する
- 未解決デザインコメントがある場合は warning または NG にできるようにする

完了条件:
- Figma URL ありのタスクで UX レビュー観点に Figma 確認が含まれる

### Phase 8: MR / Review Loop

対象ファイル:
- `hokusai/nodes/phase8/*`
- `hokusai/integrations/git_hosting/github.py`
- `hokusai/integrations/git_hosting/gitlab.py`

実装内容:

**MVP（Phase D まで）**:
- MR description に Notion / Miro / Figma リンクを追加する
- デザインレビューが必要な場合は MR description に明記する
- 未解決 Figma コメントがある場合は Waiting for Human の理由に含める（読み取り情報を活用）
- GitLab / GitHub の両方で既存 MR 作成ロジックを壊さない

**Phase E（書き戻し追加時）**:
- Phase 8a 完了時に Figma の該当 frame にコメント投稿
- Phase 8a 完了時に Miro の該当フレームに完了カード投稿

完了条件:
- GitLab MR 本文に Miro / Figma リンクが入る
- リンクなしのタスクでは既存の MR 本文と同等に動作する

### Phase 10: Record

対象ファイル:
- `hokusai/nodes/phase10_record.py`
- `hokusai/utils/notion_helpers.py`

実装内容:
- 最終記録に Miro / Figma / GitLab の対応関係を含める
- デザイン差分、代替判断、未解決事項を Notion に記録する

完了条件:
- 完了時に Notion に Miro / Figma 関連情報が残る

## 12. Notion Dashboard 実装

### 12.1. Workflows DB プロパティ追加

対象ファイル:
- `hokusai/integrations/notion_dashboard/setup.py`
- `hokusai/integrations/notion_dashboard/workflows_db.py`
- `docs/notion-dashboard-operation-guide.md`
- `docs/notion-dashboard-verification-checklist.md`

追加プロパティ:

| プロパティ | 型 | 更新主体 |
|---|---|---|
| Miro URL | url | 人間 / HOKUSAI |
| Figma URL | url | 人間 / HOKUSAI |
| Design Status | select | HOKUSAI |
| Design Review Required | checkbox | HOKUSAI |
| Design Review Result | select | 人間 / HOKUSAI |
| Miro Last Synced At | date | HOKUSAI |
| Figma Last Synced At | date | HOKUSAI |
| Miro Summary Page | url | HOKUSAI |
| Figma Summary Page | url | HOKUSAI |
| Design Notes | rich_text | HOKUSAI |

### 12.2. 同期 payload 追加

`workflow.py` の `_build_notion_payload()` に以下を追加:
- `miro_url`
- `figma_url`
- `design_integration_status`
- `design_review_required`
- `design_review_result`
- `design_sync_errors`
- `miro_last_synced_at`
- `figma_last_synced_at`

### 12.3. 表示方針

Notion DB にはサマリだけを書く。長い Miro / Figma 要約は Phase 2 / 3 子ページ、または専用の summary 子ページに保存する。

## 13. 接続状態と CLI

### 13.1. Connection Status

対象ファイル:
- `hokusai/integrations/connection_status.py`
- `hokusai/cli/commands/connect.py`
- `scripts/dashboard.py`

実装内容:
- `figma` / `miro` を connection registry に追加
- 環境変数の有無、API 到達性、権限エラーを判定
- `hokusai connect --status` に Figma / Miro を表示
- Operations Console の接続状態ページに Figma / Miro を表示

### 13.2. `hokusai connect`

MVP では token 入力 UI は作らない。CLI では以下を案内する:
- Figma: `HOKUSAI_FIGMA_API_TOKEN` の設定
- Miro: `HOKUSAI_MIRO_API_TOKEN` の設定、または MCP 設定

シークレットは YAML に保存しない。

### 13.3. Operations Console

MVP では `scripts/dashboard.py` に Figma / Miro の接続状態と cache refresh 導線を追加する:

- `render_figma_dashboard_panel()` - Figma 接続状態、最終取得時刻、cache refresh ボタン
- `render_miro_dashboard_panel()` - Miro 接続状態、最終取得時刻、cache refresh ボタン
- POST API: `/api/figma/test-connection`, `/api/miro/test-connection`, `/api/figma/refresh-cache`, `/api/miro/refresh-cache`

Phase E 以降で書き戻しを追加する場合は、同じパネルに outbox / errors 件数と同期再送ボタンを追加する:

- POST API: `/api/figma/retry-pending`, `/api/miro/retry-pending`

## 14. Slack 通知

対象ファイル:
- `hokusai/integrations/notifications/slack.py`

MVP では既存イベントに design context を追加する:
- `waiting_for_human`: デザイン確認待ち、未解決コメント、URL 不足を通知
- `pr_created`: MR と一緒に Miro / Figma リンクを通知
- `workflow_failed`: Miro / Figma 同期失敗が原因の場合に要約を含める

新イベント追加は MVP では必須としない。

## 15. テスト計画

### 15.1. Unit Test

追加・更新するテスト:

| テスト対象 | カバー範囲 | 想定件数 |
|---|---|---|
| `FigmaClient` | 各エンドポイントの正常系、429 / 5xx リトライ、token をログに出さない | ~20 件 |
| `MiroClient` | 同上 | ~20 件 |
| `DesignContextResolver` | `warn` / `block` / `skip` ポリシー、URL 抽出失敗時の挙動 | ~15 件 |
| URL parser | 各種 Figma / Miro URL からの ID 抽出、不正 URL の扱い | ~15 件 |
| config parser | `FigmaIntegrationConfig` / `MiroIntegrationConfig` | ~10 件 |
| SQLite cache API | cache / get / TTL expiry を Figma / Miro 別に検証 | ~10 件 |
| `_detect_token_like_values` | Figma token パターン検出 | ~3 件 |
| WorkflowRunner フック | 各 Phase でフックが呼ばれること、例外が抑制されること | ~15 件 |
| Notion payload 変換 | 新規プロパティの payload 生成 | ~5 件 |
| MR description 生成 | Miro / Figma リンク含めた MR body | ~5 件 |
| Operations Console パネル | 接続状態表示 / cache refresh API | ~10 件 |
| Phase E outbox API（任意） | enqueue / list / mark / move を Figma / Miro 別に検証 | ~15 件 |
| **合計（MVP 追加分）** | | **~115 件** |
| **合計（Phase E 追加分）** | | **~15 件** |

### 15.2. Integration Test

- Miro / Figma 連携無効時に既存ワークフローが壊れない
- Notion タスクに Figma URL だけある場合
- Notion タスクに Miro URL だけある場合
- Notion タスクに Miro / Figma 両方ある場合
- API 取得失敗時に warning として続行する
- `on_failure=block` で Waiting for Human になる
- Notion + Figma + Miro を全 enabled で 1 ワークフロー完走

### 15.3. Contract Test

外部 API に依存しないよう、Figma / Miro の代表レスポンス fixture を用意:

- `tests/fixtures/figma_file.json`
- `tests/fixtures/figma_node.json`
- `tests/fixtures/figma_comments.json`
- `tests/fixtures/miro_board.json`
- `tests/fixtures/miro_items.json`

### 15.4. 既存テストへの影響

- 既存 1078 件は壊さない
- enabled=False の場合に既存挙動と完全互換であることを確認

### 15.5. Manual Verification

手動検証は以下の最小ケースで行う:

1. Notion タスクに Miro URL と Figma URL を貼る
2. `hokusai start <Notion Task URL>` を実行する
3. Phase 2 / 3 出力に Miro / Figma 情報が含まれることを確認する
4. MR description に Miro / Figma リンクが含まれることを確認する
5. Notion Dashboard に Design Status と Last Synced At が反映されることを確認する

## 16. 実装フェーズと工数

各 Phase は独立してリリース可能。各段階で実用価値が出る構成。

### Phase A: 土台（1〜2 週間）

作業:
- config dataclass / parser を追加
- URL parser を追加
- `DesignContextResolver` の空実装を追加
- File / Board cache テーブル追加 + 操作 API
- connection status に Figma / Miro を追加
- example config を更新
- `_detect_token_like_values` に Figma パターン追加

完了条件:
- 設定読み込みテストが通る
- URL 抽出テストが通る
- Figma / Miro 無効時に既存テストが壊れない
- File / Board cache API テストが通る

### Phase B: Read-only クライアント（2〜3 週間）

作業:
- `FigmaClient` を追加（File / Node / Image / Comments 取得）
- `MiroClient` を追加（Board / Items / Comments 取得）
- fixture ベースの単体テストを追加
- File / Board キャッシュ層（TTL 30 分）
- `DesignContextResolver` で両クライアントを呼ぶ
- 失敗時の `on_failure` ポリシー実装

完了条件:
- API レスポンスを共通コンテキストへ正規化できる
- 認証なし、権限なし、取得失敗を警告化できる
- レートリミット時もリトライで吸収される

### Phase C: Workflow 注入（2〜3 週間）

作業:
- Phase 2 / 3 / 5 に design context を注入
- Phase 4 / 7 のプロンプトに参照観点を追加
- `on_failure=block` の Waiting for Human を実装
- Phase 10 record に design 情報追加

完了条件:
- Phase 2 / 3 出力に Miro / Figma 情報が反映される
- design context なしでも既存出力が壊れない
- `on_failure=block` で Waiting for Human になる

### Phase D: Notion / GitLab / Slack 表示（1〜2 週間）

作業:
- Notion Workflows DB プロパティを追加（10 項目）
- `_build_notion_payload()` を拡張
- MR description に Miro / Figma リンクを追加
- Slack 通知に design context サマリを追加
- Operations Console に Figma / Miro 接続状態と cache refresh 導線を追加

完了条件:
- Notion Dashboard で連携状態を確認できる
- GitLab MR で Miro / Figma への導線を確認できる
- Slack 通知に Figma / Miro リンクが含まれる
- Operations Console から接続状態確認と cache refresh ができる

### Phase E: 書き戻し（コメント・カード投稿）（2〜3 週間・任意）

作業:
- Figma `post_comment` 実装（Phase 8a で frame コメント投稿）
- Miro `create_card` / `create_comment` 実装（Phase 8a でカード投稿）
- SQLite outbox / errors テーブル追加 + 操作 API
- Operations Console に Figma / Miro 同期パネル追加（outbox / errors 件数 / 同期再送ボタン）
- SQLite outbox 連携で best effort 化
- 冪等キー（`workflow_id:event_type:resource:revision`）で重複抑止

完了条件:
- Phase 8a 完了時に Figma / Miro へコメント / カードが投稿される
- 投稿失敗は outbox に蓄積され、Operations Console から再送できる
- 同じ payload で再送しても重複作成されない

### Phase F: 運用ドキュメントと検証（1 週間）

作業:
- `docs/figma-integration-operation-guide.md` 作成
- `docs/miro-integration-operation-guide.md` 作成
- `docs/figma-miro-integration-verification-checklist.md` 作成
- Notion Dashboard 運用ガイド更新
- サンプル設定を更新
- 手動検証を実施

完了条件:
- PM / デザイナー / エンジニア向けの使い方が説明できる
- §22.1 の MVP 完了条件（全 13 項目）を満たし、MVP リリース可能な状態になっている

### Phase G: Webhook 受信 + レビューループ（2〜3 週間・任意）

作業:
- Webhook 中継サーバーを Slack ブリッジと同居
- Figma `FILE_COMMENT` / Miro `comment_added` を受信
- HMAC 認証
- Phase 8 統合レビューループに Figma / Miro コメント取得・応答処理を追加

完了条件:
- デザイナー / ビジネスサイドのコメント追加が即時検知される
- HOKUSAI が修正後、応答コメントが返る

### Phase H: 視覚回帰テスト（3〜4 週間・任意）

作業:
- Phase 6 で実装スクリーンショット取得（Playwright 等）
- Figma frame と差分検出（pixel diff or LLM vision 比較）
- 差分が大きければ Phase 5 リトライ
- 差分レポートを Notion DB に記録

完了条件:
- デザイン乖離が早期検出される

### 工数まとめ

| Phase | 内容 | 工数 |
|---|---|---|
| A | 土台 | 1〜2 週間 |
| B | Read-only クライアント | 2〜3 週間 |
| C | Workflow 注入 | 2〜3 週間 |
| D | Notion / GitLab / Slack 表示 | 1〜2 週間 |
| E | 書き戻し（コメント投稿） | 2〜3 週間（任意） |
| F | 運用ドキュメントと検証 | 1 週間 |
| G | Webhook + レビューループ | 2〜3 週間（任意） |
| H | 視覚回帰テスト | 3〜4 週間（任意） |

**MVP（A 〜 D + F）**: 7〜11 週間
**完全版（A 〜 H）**: 14〜21 週間（並行作業前提で 11〜15 週間）

## 17. パフォーマンス想定

| 項目 | Figma | Miro |
|---|---|---|
| 1 ワークフローあたり API 呼び出し | 5〜15 回 | 3〜10 回 |
| File / Board 構造取得レイテンシ | 1〜3 秒 | 1〜3 秒 |
| 画像 export レイテンシ | 2〜5 秒 | 2〜5 秒 |
| キャッシュヒット時の応答 | <50ms | <50ms |
| レートリミット | プラン依存 | 100 req/min（Free） |
| 並行ワークフロー上限 | 5〜10 件 | 5〜10 件 |

キャッシュ TTL（30 分）+ デバウンスで API 呼び出しを最小化。

## 18. セキュリティ

### 18.1. token 管理

- `HOKUSAI_FIGMA_API_TOKEN` / `HOKUSAI_MIRO_API_TOKEN` は **環境変数経由のみ**
- YAML 直書きは `_detect_token_like_values` で警告
- ログ・例外メッセージ・Notion 本文・Slack 本文に出さない
- Operations Console から token を表示しない（接続テストのみ）

### 18.2. コメント投稿の権限分離

- Figma integration / Miro integration は **コメント投稿に必要な最小権限のみ**
- ワークスペース全体の編集権限は付与しない

### 18.3. キャッシュの取り扱い

- File 構造キャッシュは平文で SQLite に保存（既存方針と同じ）
- 機密情報を含む可能性を踏まえ、SQLite ファイルのパーミッションを 600 に設定（既存と同じ）
- キャッシュ TTL を超えたら自動削除

### 18.4. Webhook 認証（Phase G で実装）

- HMAC-SHA256 署名検証
- 共有シークレットを環境変数経由で渡す
- 全リクエストを監査ログに記録

## 19. 影響範囲

### 19.1. 主な変更ファイル

- `hokusai/config/models.py`
- `hokusai/config/loaders.py`
- `hokusai/config/manager.py`
- `hokusai/state.py`
- `hokusai/workflow.py`
- `hokusai/persistence/sqlite_store.py`
- `hokusai/nodes/phase2_research.py`
- `hokusai/nodes/phase3_design.py`
- `hokusai/nodes/phase4_plan.py`
- `hokusai/nodes/phase5_implement.py`
- `hokusai/nodes/phase7_review.py`
- `hokusai/nodes/phase8/*`
- `hokusai/nodes/phase10_record.py`
- `hokusai/integrations/connection_status.py`
- `hokusai/integrations/notion_dashboard/setup.py`
- `hokusai/integrations/notion_dashboard/workflows_db.py`
- `hokusai/integrations/notifications/slack.py`
- `scripts/dashboard.py`
- `configs/example-gitlab.yaml`
- `prompts/phase2/task_research*.md`
- `prompts/phase3/design_check*.md`
- `prompts/phase4/dev_plan*.md`
- `prompts/phase5/implementation.md`
- `prompts/phase7/final_review.md`

### 19.2. 新規ファイル

- `hokusai/integrations/design/__init__.py`
- `hokusai/integrations/design/url_parser.py`
- `hokusai/integrations/design/context.py`
- `hokusai/integrations/design/figma.py`
- `hokusai/integrations/design/miro.py`
- `hokusai/integrations/design/cache.py`
- `tests/integrations/test_design_url_parser.py`
- `tests/integrations/test_design_context.py`
- `tests/integrations/test_figma_client.py`
- `tests/integrations/test_miro_client.py`
- `tests/fixtures/figma_*.json`
- `tests/fixtures/miro_*.json`
- `docs/figma-integration-operation-guide.md`（Phase F）
- `docs/miro-integration-operation-guide.md`（Phase F）
- `docs/figma-miro-integration-verification-checklist.md`（Phase F）

## 20. リスクと対策

| リスク | 対策 |
|---|---|
| 外部 API の rate limit で workflow が不安定になる | 取得は best effort、timeout、retry、必要最小限の取得、キャッシュ TTL |
| Figma / Miro の生データが大きすぎてプロンプトが肥大化する | 共通コンテキストへ要約し、長文は Notion 子ページへ分離する |
| デザイン情報を過信して誤実装する | Phase 3 で不明点を明示し、重大な不整合は Waiting for Human にする |
| シークレットがログや Notion に漏れる | token は環境変数参照のみ、ログ出力時にマスクする、`_detect_token_like_values` で検出 |
| Notion DB スキーマ変更で既存導入先が壊れる | 新規プロパティは後方互換で追加し、未設定時はスキップする |
| Miro のラフスケッチ解釈が不正確 | MVP では下書き生成を対象外にし、読み取りと要約に限定する。運用ルール（フレーム命名・付箋カラー）の整備で吸収 |
| Figma 画面と実装の完全一致を期待される | MVP の説明で「参照・支援・ズレ検知」であり完全自動変換ではないことを明示する |
| 読み取り失敗で重要情報が欠落する | `design_sync_errors` に記録し、Operations Console から接続状態確認と cache refresh を行えるようにする |
| 書き戻し失敗で通知・コメントが失われる | Phase E 以降は SQLite outbox に蓄積し、Operations Console から再送 |
| 5 ツールの情報が不整合 | 各ツールの責任範囲を明示（Notion = 仕様、Miro = 議論、Figma = UI、GitLab = 実装、Slack = 通知） |
| 画像 export のサイズ過大 | scale=1〜2 を上限、必要 frame のみ取得 |
| キャッシュが古くなる | TTL（既定 30 分）で自動失効、Phase 2 開始時に refresh 判定 |

## 21. Open Questions（着手前合意項目）

実装着手前に明示的に方針を確定させる項目。各暫定案で進めて差し支えなければ、レビュアからの no-objection をもって着手する。

1. **Figma / Miro の Token 発行主体**
   - 暫定案: 組織管理者が HOKUSAI 専用 integration を作成、Personal Access Token を発行

2. **Figma の対象スコープ**
   - チーム全体 / 特定プロジェクト / ファイルごとに ID 列挙
   - 暫定案: ファイルごとに Notion タスクから URL を渡す方式（最小スコープ）

3. **Miro の運用ルール策定責任**
   - フレーム命名規則・付箋カラー意味付けを誰が定義するか
   - 暫定案: ビジネスサイドのテックリードが運用ルールを定義し、HOKUSAI 側で読み取りパターンを実装

4. **`on_failure` のデフォルト**
   - warn / block / skip のどれを推奨にするか
   - 暫定案: `warn`（既存ワークフロー非破壊優先）。重要案件は組織判断で `block` に変更

5. **キャッシュ TTL のデフォルト値**
   - 暫定案: 1800 秒（30 分）。デザイン更新頻度に応じて運用で調整

6. **Webhook 中継サーバーのホスティング（Phase G）**
   - 暫定案: Slack ブリッジと同居（同じインフラ）

7. **視覚回帰テスト（Phase H）の優先度**
   - 暫定案: A〜F 完了後に必要性を再評価。必要が確認されたら実装

8. **書き戻し（Phase E）の MVP 含有**
   - コメント / カード投稿を MVP に含めるか、Phase E として後追いするか
   - 暫定案: MVP は読み取りまで（Phase A〜D + F）。書き戻しは Phase E として別フェーズ

## 22. MVP 完了条件（Definition of Done）

### 22.1. 全体 DoD

- [ ] `figma.enabled=false` / `miro.enabled=false` で既存ワークフローが変わらず動く
- [ ] Notion タスクから Figma / Miro URL を抽出できる
- [ ] Figma / Miro の概要情報を共通コンテキストに変換できる
- [ ] Phase 2 / 3 / 5 のプロンプトに design context を注入できる
- [ ] GitLab MR に Notion / Miro / Figma のリンクが入る
- [ ] Notion Dashboard に Miro / Figma 連携状態が表示される
- [ ] API 取得失敗時に warning として継続できる
- [ ] `on_failure=block` で Waiting for Human にできる
- [ ] 主要ユニットテストと統合テストが追加されている（MVP 範囲で ~115 件）
- [ ] 既存 1078 件のテストが通り続ける
- [ ] 全 token は環境変数経由のみ、`_detect_token_like_values` の警告対象になる
- [ ] Operations Console から各連携の状態確認ができる
- [ ] 運用ガイドが整備されている（Figma / Miro それぞれ）

### 22.2. 段階別 DoD

各段階の完了条件は §16 段階的実装ステップ参照。

## 23. 導入手順

1. Figma / Miro の利用権限と API token 発行可否を確認する
2. HOKUSAI 実行環境に `HOKUSAI_FIGMA_API_TOKEN` / `HOKUSAI_MIRO_API_TOKEN` を設定する
3. 対象 config で `figma.enabled` / `miro.enabled` を有効化する
4. Notion Dashboard スキーマを更新する。新規ワークスペースでは `hokusai notion-setup` の更新後版で作成し、既存 Workflows DB には手動追加または将来追加する `hokusai notion-migrate` 相当の migration コマンドで追加プロパティを反映する
5. Notion タスクテンプレートに Miro URL / Figma URL 欄を追加する
6. サンプルタスクで Phase 2 / 3 / 5 / 8 を検証する
7. PM、デザイナー、エンジニア向けに運用ルールを共有する

## 24. 関連ドキュメント

| ドキュメント | 関係 |
|---|---|
| `docs/codex-figma-miro-integration-implementation-plan.md` | 本書のベース設計（既存コードベース統合・DesignContextResolver・on_failure） |
| `docs/claude-figma-miro-integration-implementation-plan.md` | 本書の補完（Phase 別工数・SQLite outbox・Open Questions・性能試算） |
| `docs/codex-figma-miro-integration-requirements.md` | 前提となる要件書 |
| `docs/claude-figma-miro-integration-requirements.md` | 補完要件書 |
| `docs/hokusai-notion-dashboard-implementation-plan.md` | Notion 連携の実装計画（再利用元） |
| `docs/codex-slack-notification-implementation-plan.md` | Slack 連携の実装計画（基盤参考） |
| `docs/notion-dashboard-operation-guide.md` | Notion 運用ガイド（同様の構成で Figma / Miro 用を整備予定） |

## 25. 将来拡張

MVP 完了後、以下を検討する:

- Miro のラフスケッチから Figma ワイヤーフレーム下書きを生成する
- Figma Plugin 経由で frame / rectangle / text を作成する（Figma REST API 制約の回避）
- Figma コメントへの返信と GitLab MR との紐づけ（Phase G）
- Figma / Miro Webhook による更新検知（Phase G）
- デザイン更新後の MR stale 判定
- Figma Variables / design token 連携
- Playwright screenshot と Figma export image の visual diff（Phase H）
- Notion 上の Design Status を PM 向けビューに整理する
- Miro / Figma へのコメント書き戻し（Phase E）

## 26. まとめ

| 項目 | 内容 |
|---|---|
| Notion / GitLab / Slack 既存連携への影響 | なし（後方互換、enabled=False で既存挙動と完全一致） |
| 新規依存 | なし（urllib のみ） |
| アーキテクチャの核 | `DesignContextResolver`（Phase ノードから直接 API を呼ばない抽象化） |
| 失敗時ポリシー | `on_failure: warn / block / skip` で運用に応じて選択可 |
| 連携の非対称性 | 読み中心 + コメント書き戻し（Phase E）。本体編集は不可 |
| 同期基盤 | MVP は cache refresh と warning 記録、Phase E 以降は Notion 連携の SQLite outbox / 冪等キー / Operations Console パターンを流用 |
| 工数（MVP） | 7〜11 週間（A〜D + F） |
| 工数（完全版） | 14〜21 週間（A〜H、並行作業で 11〜15 週間） |
| 追加テスト件数 | MVP ~115 件、Phase E 追加 ~15 件 |
| MVP DoD | 13 項目（§22.1） |
| Open Questions | 8 項目（§21、暫定案付き） |

レビュアからの no-objection を得たうえで、**Phase A（土台）から着手**することを推奨する。Phase D 完了で MVP リリース、Phase E 以降は運用フィードバックを踏まえて順次進める。
