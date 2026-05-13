# Changelog

HOKUSAI のすべての特筆すべき変更をこのファイルに記録する。

書式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠し、
バージョニングは [Semantic Versioning](https://semver.org/lang/ja/) に従う。

開発状況は `Development Status :: 3 - Alpha`（v0.x はマイナーバージョン更新で
破壊的変更を含む可能性あり）。

---

## [Unreleased]

### 追加 / 変更 / 削除予定
- 未定

---

## [0.4.0] - 2026-05-13

Figma / Miro **書き戻し機能（Phase E）** を追加。Phase 8a（PR 作成）完了時に、
対象 frame / board へ進捗コメント / カードを自動投稿する。

詳細は `docs/hokusai-figma-miro-writeback-implementation-plan.md` に対応。

### Added

- **SQLite スキーマ**（Step 1）
  - `figma_sync_outbox` / `figma_sync_errors`
  - `miro_sync_outbox` / `miro_sync_errors`
  - `design_writeback_idempotency`（成功済み投稿の冪等キー保存）
  - 全テーブルに `profile_name` 列（v0.3.0 整合）
  - 計 5 テーブル + 9 index（errors 側の idempotency_key index 2 本を含む）
- **outbox 操作 API**（Step 2）
  - `OutboxStore` クラス: enqueue / list / get / mark_succeeded /
    increment_attempt / move_to_errors / cleanup_old_errors
  - 3 段階 should_skip（idempotency / outbox / errors、`force=true` で errors 無視）
  - 冪等キー `{workflow_id}:{event_type}:{resource}:{revision}`
- **Figma post_comment**（Step 3）
  - `FigmaClient.post_comment(file_key, message, node_id, node_offset)`
  - `POST /v1/files/{file_key}/comments` に `client_meta` 付きで pin 投稿
  - `FigmaWritebackDispatcher.dispatch / retry`
- **Miro create_card**（Step 4）
  - `MiroClient.create_card(board_id, title, description, position, style)`
  - 主 frame の右側 50px に薄緑 card を配置
  - `MiroWritebackDispatcher.dispatch / retry`
- **WorkflowState 拡張**（Step 5）
  - `primary_figma_file_key` / `primary_figma_frame_id` / `primary_figma_node_id` /
    `primary_figma_node_offset`
  - `primary_miro_frame_id` / `primary_miro_board_id`
  - 既存 state は後方互換（未設定なら writeback skip）
- **Phase 8a への組み込み**（Step 5）
  - PR 作成成功直後に Figma / Miro へ dispatch
  - 失敗は outbox に積み workflow を継続（best effort）
- **Operations Console API**（Step 6）
  - `GET /api/{figma,miro}/{outbox,errors}` 一覧（limit / profile フィルタ）
  - `POST /api/{figma,miro}/retry-pending` 個別 / 全件 / force 再送
  - `POST /api/{figma,miro}/move-to-errors` 強制移動
- **cleanup 統合**（Step 7）
  - `hokusai cleanup --stale` で errors / idempotency の 30 日経過行を自動削除
- **運用ガイド**: `docs/figma-miro-writeback-operation-guide.md`

### Behavior

- 投稿先 frame / board は Phase 3 で `state.primary_*` に確定
- `figma.writeback.enabled` / `miro.writeback.enabled` が `false` の既存 config はそのまま動作
- `on_failure`: `warn`（既定） / `block`（Waiting for Human 遷移） / `skip` の 3 モード
- 自動 retry なし。失敗は outbox に積み、Operations Console から手動再送
- 5 回手動再送で errors テーブルへ自動移動（自動経路では再投稿しない）
- 冪等性: Figma / Miro API には idempotency key 受け渡し機構が無いため、
  HOKUSAI 側で成功済み idempotency_key を `design_writeback_idempotency` に永続化し、
  dispatcher 入口で 3 段階チェック（idempotency / outbox / errors）

### Tests

- `tests/test_design_writeback_outbox.py`（12 件）
- `tests/test_design_writeback_api.py`（18 件）
- `tests/test_figma_writeback.py`（17 件）
- `tests/test_miro_writeback.py`（10 件）
- `tests/test_writeback_integration.py`（20 件）
- `tests/test_dashboard_writeback.py`（4 件）
- 合計 81 件、全 pass

### v0.4.1 以降のフォローアップ

- Operations Console UI への HTML パネル統合（API は v0.4.0 で揃っている）
- i18n（日本語 / 英語切替）
- 投稿テンプレートの config 化（card 色 / position offset 等）
- 複数 frame / 複数 board への投稿
- Phase 5（Implement）/ Phase 10（Record）のトリガー
- 自動 retry（exponential backoff）

---

## [0.3.0] - 2026-05-12

複数案件（A 社・B 社・C 社）を安全に並列運用するための **profile 機能** を追加。
1 PC 上で複数の Notion / Figma / Miro / GitHub / Slack token を使い分けながら、
DB / worktree / dashboard を完全分離して並行開発できる基盤を提供。

詳細は `docs/hokusai-profile-parallel-execution-implementation-plan.md` の
Phase A〜F に対応。

### Added

- **Profile Registry**（Phase A）
  - `~/.hokusai/profiles.yaml` で複数 profile を定義
  - `HOKUSAI_PROFILES_FILE` 環境変数で registry パス override 可
  - profile 名 validation（英小文字始まり、英数字/ハイフン/アンダースコア）
  - `ProfileConfig` / `ProfileRegistry` データクラス
- **CLI `--profile` グローバルオプション**（Phase B）
  - `hokusai --profile <name> start | continue | status | list | cleanup | pr-status`
  - `-c/--config` と排他（同時指定はエラー）
- **`hokusai profile` サブコマンド**（Phase B）
  - `profile list`: 登録 profile 一覧
  - `profile show <name>`: 解決結果を表示（シークレット非表示）
  - `profile doctor <name> [--deep]`: 設定整合性診断（config file 存在 /
    data_dir / dashboard port 衝突 / data_dir 衝突）
- **Data Dir 自動補完**（Phase C）
  - profile registry の `data_dir` から `database_path` / `checkpoint_db_path` /
    `worktree_root` を自動補完
  - config file の明示値が registry 補完より優先
  - 補完先の親ディレクトリを自動作成
- **`hokusai dashboard` サブコマンド**（Phase D）
  - `hokusai dashboard --profile <name> --port <port>`
  - profile registry の `dashboard.port` を fallback として使用
  - port 衝突を起動前に検出（`DashboardPortInUseError`）
  - dashboard HTML ヘッダに profile バッジ表示
  - `scripts/dashboard.py` を環境変数（`HOKUSAI_DASHBOARD_PORT` /
    `HOKUSAI_DASHBOARD_DB_PATH` / `HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH` /
    `HOKUSAI_DASHBOARD_PROFILE`）で外部制御可能化
- **Workflow profile_name 保存**（Phase E）
  - `workflows` テーブルに `profile_name` カラム追加
  - 既存 v0.2.x DB は ALTER TABLE で自動マイグレーション（NULL 行は `(legacy)` 扱い）
  - `SQLiteStore.get_workflow_profile_name()` / `workflow_exists()` API
- **他 profile 横断探索**（Phase E）
  - `find_workflow_in_other_profiles()`: workflow_id not found 時に
    他 profile に存在するかを探索（current profile は除外）
  - 壊れた DB / data_dir 不在の profile は silent skip
- **配布 / 運用ガイド**（Phase F）
  - `docs/profile-operation-guide.md`: profile 設定手順、移行ガイド
  - `configs/example-profiles.yaml` / `configs/example-profile-company.yaml`: 雛形

### Changed

- **`create_config_from_env_and_file()`** に `profile_name` キーワード引数追加
  - `profile_name` 指定時は registry から config_path を解決
  - `--profile` と `--config` 排他チェック
- **`SQLiteStore.save_workflow()`** が `state["profile_name"]` を DB に保存
  - UPDATE 時は `COALESCE` で既存値を保持（state に無くても上書きしない）
- **`scripts/dashboard.py`** の PORT / DB_PATH をモジュール定数 → env 解決関数に
  （`HOKUSAI_DASHBOARD_*` 環境変数が最優先）

### Breaking Changes

なし。既存の `-c/--config` 運用、`python scripts/dashboard.py` 直接起動、
v0.2.x で作成された DB はすべて互換。

### Documentation

- `docs/hokusai-profile-parallel-execution-implementation-plan.md`
  実装計画書（Phase A〜F、DoD、テスト計画、移行計画、Open Questions）

---

## [0.2.0] - 2026-05-11

v0.1.0 から約 2 週間で多数の機能追加と運用性改善を実施。HOKUSAI を Notion
ベースの組織横断ダッシュボードとして再定義し、Operations Console を
管理者向け運用コンソールとして分離した。

### Added

- **Notion メインダッシュボード同期** (PR #6 系列)
  - HOKUSAI 専用 Notion Integration 経由で Workflows DB / Pull Requests DB
    へワークフロー状態を書き込み
  - `hokusai notion-setup --parent-page-id <ID> --persist` で DB 一括作成
    + 環境変数の rc ファイル自動追記
  - SQLite outbox による失敗イベントの自動再送（Operations Console から
    手動再送も可能）
  - Workflows DB と Pull Requests DB の relation 自動構築
  - Notion ページ URL 解決を Slack 通知に統合
- **Figma / Miro 連携 MVP** (PR #9, read-only)
  - Notion タスクから Figma / Miro URL を抽出し、Phase 2/3/4 のコンテキスト
    として LangGraph に注入
  - Figma: API token + 共有リンクからの read、コメント取得、画像エクスポート
  - Miro: API token + (optional) MCP 経由でのボード読み取り
  - SQLite キャッシュ（TTL 30 分、Operations Console から手動リフレッシュ可）
  - レートリミット / リトライ / 失敗時の `warn|block|skip` ポリシー
- **Slack 通知統合** (PR after #8)
  - Incoming Webhook 経由で `workflow_started` / `waiting_for_human` /
    `workflow_failed` / `pr_created` / `workflow_completed` を通知
  - イベントごとの通知 ON/OFF 切替
- **Operations Console（HOKUSAI Web Dashboard）の強化** (PR #1〜#7)
  - サービス接続状態の一覧と再チェック（claude / codex / gh / glab /
    notion / figma / miro / jira / linear）
  - `hokusai connect <service>` CLI で接続セットアップ導線を統一
  - 設定 YAML の編集・保存差分プレビュー・多世代 `.bak` 復元 UI
  - BASIC 認証（環境変数 `HOKUSAI_OPS_USERNAME` / `HOKUSAI_OPS_PASSWORD`）
  - パストラバーサル防御を含む設定保存の安全化
  - トークン直書き警告・接続整合性警告
- **README ドキュメントの再構成**
  - Why HOKUSAI? / The Problem / The Solution / Workflow セクション
  - Architecture セクション + mermaid 図
  - HOKUSAI を Claude Code 専用ではなく複数 LLM 対応として表記

### Changed

- **`_str_or_default` を共通ヘルパに集約**（YAML パースの DRY 化）
- **トークン直書き警告のロジックを強化**（`*_env` フィールドへの直接代入を検出）

### Removed

- **Notion Service Status 同期を廃止** (PR #10)
  - 複数ユーザー環境で各自のローカル CLI 状態が共有 Notion ページを
    last-writer-wins で上書きする問題を解消するため
  - `hokusai sync-service-status` CLI、`ServiceStatusPageClient`、
    `HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID` 設定、Operations Console の
    「Service Status を Notion へ反映」ボタンを削除
  - 旧バージョンが SQLite outbox に積んだ `service_status_checked`
    エントリは `retry_pending()` で no-op として drain される後方互換あり
  - サービス接続状態は Operations Console の接続状態ページでのみ参照

### Fixed

- PR #1〜#10 の各レビュー指摘に随時対応（情報露出排除、property_not_found
  自動回復、Notion API レートリミット遵守、property 抽出の堅牢化など多数）

### Documentation

- `docs/notion-dashboard-operation-guide.md`：運用ガイド
- `docs/notion-dashboard-verification-checklist.md`：検証チェックリスト
- `docs/hokusai-notion-dashboard-implementation-plan.md`：統合実装計画書
  （Service Status 関連は履歴として保持）
- `docs/claude-notion-main-dashboard-design.md`：設計案
  （同上）
- `docs/figma-miro-integration-requirements.md`：Figma/Miro 連携要件書
- `docs/figma-miro-integration-implementation-plan.md`：Figma/Miro 実装計画
- `docs/figma-miro-integration-operation-guide.md`：Figma/Miro 運用ガイド

### Breaking Changes

- **`HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID` 環境変数は廃止**
  - 設定 YAML に `service_status_page_id_env` を書いていた場合、YAML パース時に
    無視される（エラーにはならないが値も読まれない）
  - rc ファイル（`~/.zshrc` 等）にこの export 行が残っている場合は、
    `hokusai notion-setup --persist` の次回実行時にブロックが置き換わるため、
    手動削除しなくても害はないが、不要なので削除を推奨
- **`hokusai sync-service-status` CLI コマンドは廃止**
  - 既存の cron / launchd 登録がある場合は外す必要あり
  - サービス接続状態は Operations Console の接続状態ページから個別に確認

---

## [0.1.0] - 2026-04-25

HOKUSAI 初回リリース。LangGraph ベースの 10 phase AI 開発ワークフローと
Notion / GitHub Issue / Jira / Linear 連携の最小機能セット。

### Added

- 10 phase ワークフロー（research / design / plan / implement / review /
  test / fix / pr_create / merge_wait / cleanup）
- Notion / GitHub Issue / Jira / Linear バックエンドのタスク管理
- GitHub / GitLab / Bitbucket Git ホスティング対応
- Claude Code / OpenAI Codex / Aider 等の複数 LLM 対応
- Operations Console (Web Dashboard) の基盤
- SQLite による checkpoint / outbox 永続化
- Worktree ベースの並行ワークフロー実行

[Unreleased]: https://github.com/shigenoko/hokusai/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/shigenoko/hokusai/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/shigenoko/hokusai/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/shigenoko/hokusai/releases/tag/v0.1.0
