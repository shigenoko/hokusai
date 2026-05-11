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

[Unreleased]: https://github.com/shigenoko/hokusai/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/shigenoko/hokusai/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/shigenoko/hokusai/releases/tag/v0.1.0
