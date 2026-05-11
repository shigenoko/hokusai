# HOKUSAI Notion メインダッシュボード化 動作確認チェックリスト

**作成日**: 2026-05-05
**対象読者**: 運用担当・テックリード・パイロット運用責任者
**目的**: コード実装が完了した後、本番運用に乗せる前にユーザー側で確認すべき項目を段階的にリスト化したもの

本書は `docs/hokusai-notion-dashboard-implementation-plan.md` および `docs/notion-dashboard-operation-guide.md` に基づく実装の受け入れテスト/動作確認チェックリストとして使用する。

---

## Phase 0: 環境セットアップ（前提条件）

これらが完了しないと以降の確認はできない。

- [ ] **Notion インテグレーションの作成**: 組織管理者が `https://www.notion.so/my-integrations` で HOKUSAI 専用 integration を発行し、Internal Integration Token を取得
- [ ] **Notion 上に DB を作成**:
  - 推奨: `hokusai notion-setup --parent-page-id <PARENT_PAGE_ID>` で一括作成
  - または手動作成（運用ガイド §2.2 のスキーマで Workflows DB / Pull Requests DB を作成）
  - 親ページに HOKUSAI integration を「接続」していれば、配下の DB に自動継承される
- [ ] **環境変数を設定**:
  ```bash
  export HOKUSAI_NOTION_API_TOKEN="secret_xxx"
  export HOKUSAI_NOTION_WORKFLOWS_DB_ID="..."
  export HOKUSAI_NOTION_PR_DB_ID="..."
  ```
- [ ] **YAML 設定**: `notion_dashboard.enabled: true` を有効化、対象 config ファイルに反映
- [ ] **動作前の DB 状態確認**: SQLite `~/.hokusai/workflow.db` の `notion_sync_outbox` / `notion_sync_errors` テーブルが空であること

## Phase 1: 単体動作の sanity check

最小コストで「設定が通っているか」を確認。

- [ ] HOKUSAI Operations Console を起動（`python scripts/dashboard.py`）し、接続状態ページで gh / glab / notion_mcp / codex / claude の状態が表示される
- [ ] SQLite の outbox 件数が起動直後は 0 件（`sqlite3 ~/.hokusai/workflow.db "SELECT COUNT(*) FROM notion_sync_outbox"`）

## Phase 2: End-to-End ワークフロー検証

実ワークフローを 1 件流して、各イベントが Notion に届くか確認。

- [ ] **`workflow_started`**: `hokusai start <task_url>` 直後に Workflows DB に新規レコードが作成され、Status=`Running`、Started At が現在時刻
- [ ] **`phase_changed`**: Phase 進行に応じて Current Phase / Current Phase Name / Last Updated が更新される（Phase 1 → 2 → 3 → 4 と動くたびに反映）
- [ ] **`phase_artifact_linked`**: Phase 2/3/4 完了後、Research Page / Design Page / Plan Page の URL プロパティが埋まる
- [ ] **`pr_created`**:
  - Workflows DB の GitLab MR プロパティに PR URL が入る
  - Pull Requests DB に新規レコードが作成される
  - **Workflow relation が Workflows DB のページを正しく参照している**（Notion 上で双方向に辿れる）
- [ ] **`waiting_for_human`**: 人間判断待ちで停止すると、Status=`Waiting for Human`、Waiting Reason、Next Action（テンプレ文）が反映
- [ ] **`workflow_completed`**: Phase 10 完了時に Status=`Done`、Completed At が反映
- [ ] **Last Sync が常に更新される**: 各イベントで Notion 上の Last Sync 日時が動く
- [ ] **Slack 通知に Notion ディープリンク**: Slack 通知本文に `Dashboard: <URL|Notion で開く>` 行が含まれる（2 回目以降の通知から確実に）

## Phase 3: 障害時の復旧フロー

Notion 障害シミュレーションでワークフロー本体が止まらないこと、復旧が動くことを確認。

- [ ] **API token を意図的に無効化** → ワークフロー実行が止まらず最後まで完走する（ログには warn が出る）
- [ ] **outbox 蓄積を確認**: `sqlite3 ~/.hokusai/workflow.db "SELECT * FROM notion_sync_outbox"` でエントリが積まれている
- [ ] **同期再送ボタン**: HOKUSAI Web Dashboard トップの「同期再送」ボタンを押す → 件数表示が「成功 N 件 / 失敗 N 件 / permanent N 件」
- [ ] **Sync Errors のサマリ表示**: 保留がある間は Notion Workflows DB の Sync Errors プロパティに「保留 N 件 / 永続失敗 N 件」が表示される
- [ ] **Sync Errors の自動クリア**: token を有効に戻して再送 → 全件成功すると Sync Errors プロパティが空になる
- [ ] **永続失敗（max_retry_attempts 超過）の扱い**: わざと max_retry_attempts=1 などで失敗を蓄積させ、permanent error テーブルに移ること、Sync Errors に「永続失敗 N 件」が残ること
- [ ] **Notion DB 削除のシミュレーション**: 一時的に Workflows DB の ID を間違ったものに変えて起動 → ワークフローは動く、warn ログが出る、ID を戻して再送できる

## Phase 4: アクセス制御の確認

- [ ] **BASIC 認証 OFF（デフォルト）**: 既存の dashboard 動作が変わらない（誰でも開ける）
- [ ] **BASIC 認証 ON + 環境変数設定**:
  ```yaml
  web_dashboard:
    auth:
      enabled: true
  ```
  ```bash
  export HOKUSAI_OPS_USERNAME="admin"
  export HOKUSAI_OPS_PASSWORD="strong-password"
  ```
  - ブラウザで認証ダイアログが表示される
  - 正しいクレデンシャルで全ページにアクセスできる
  - 間違ったクレデンシャルで 401 が返る
  - すべての POST API（同期再送、設定保存、ワークフロー操作）でも認証が要求される
- [ ] **BASIC 認証 ON + 環境変数未設定（ロックダウン状態）**: どの認証情報を入れても 401 → 緊急ロックアウトとして使える
- [ ] **localhost bind の確認**: `lsof -i :8765` で `localhost` バインドのみであり、外部 IP からはアクセス不能
- [ ] **HTTPS が必要な場合の判断**: BASIC 認証は平文なので、外部公開する場合はリバースプロキシ（nginx 等）で HTTPS 終端する必要があることをチームで認識

## Phase 5: パフォーマンス・スケール確認

- [ ] **並行ワークフロー耐性**: 5 件同時起動して、Notion API レートリミット（3 req/sec）に詰まらず全件完走
- [ ] **書き込みレイテンシ**: 1 イベントあたりの Notion 書き込みが 1〜3 秒以内
- [ ] **大量 outbox の再送**: outbox に 50 件溜まった状態で再送 → 全件処理される（バッチ + デバウンスが効く）
- [ ] **Notion DB のレコード数増加**: 100 件程度のワークフローレコードがあっても DB 検索が遅くならない（query_database が 1〜2 秒以内）

## Phase 6: 組織側の運用準備

これは技術というよりは運用フロー・体制の整備。

- [ ] **Notion ワークスペース権限**: ビジネスサイド（営業・マーケ）に Workflows DB / Pull Requests DB の閲覧権限を付与済み
- [ ] **編集権限の制限**: Status / Waiting Reason / Next Action など HOKUSAI が書くプロパティを人間が誤編集しない運用ルールの周知（編集権限を PM・テックリードに絞る）
- [ ] **Operations Console アクセス制限の方針確定**: BASIC 認証 / VPN / SSO のいずれを採用するか組織として決定し、運用ガイド §6.2 を反映
- [ ] **Notion DB のビュー整備**: Active / Waiting for Human / Failed / By Business Owner / By Tech Lead など実装計画書 §6.1 のビューを Notion 上で構成
- [ ] **Business Owner / Tech Lead プロパティの初期入力ルール**: 誰がどのタイミングで埋めるか（タスク起票時 PM が埋める、など）
- [ ] **失敗対応の責任分担**: outbox に永続失敗が出た場合、誰が確認・対応するか
- [ ] **README / 社内 Wiki への追記**: 「HOKUSAI を使うチームは Notion で進捗確認、Operations Console で操作」のフローを文書化

## Phase 7: パイロット運用（推奨）

- [ ] **1 チームを選定**: 比較的シンプルなタスクが多いチームから始める
- [ ] **2〜4 週間運用**: 1〜2 サイクルで実際の不具合・運用課題を洗い出す
- [ ] **メトリクスを記録**:
  - Notion 同期失敗率（outbox に積まれた件数 / 全イベント数）
  - 平均完了時間
  - Slack 通知の有用性（人間判断待ち→対応までの時間）
  - ビジネス側の閲覧頻度（Notion アクセスログ）
- [ ] **フィードバックを収集**: ビジネスサイド・エンジニアサイド双方から
- [ ] **DoD の見直し**: 想定外の課題があれば実装計画書 / 運用ガイドに反映

## 確認の優先順位

| 優先度 | フェーズ | 理由 |
|---|---|---|
| 🔴 必須 | Phase 0, 1, 2 | これらが通らないと運用に乗らない |
| 🟠 高 | Phase 3, 4 | 障害時とセキュリティが本番で問題化しやすい |
| 🟡 中 | Phase 5, 6 | 規模拡大時・運用安定化時に重要 |
| 🟢 推奨 | Phase 7 | 全社展開前のリスク低減 |

## 不具合発見時の連絡経路

実装側のバグが見つかったら、以下の情報を添えて連絡いただければ即対応できます:
- どのフェーズの確認項目で発見したか
- 再現手順
- HOKUSAI のログ（`~/.hokusai/logs/`）
- SQLite の outbox / errors の内容（必要に応じて）
- Notion DB のスクリーンショット（書き込みが反映されない場合）

## 推奨される進め方

まずは **Phase 0 → Phase 1 → Phase 2** の順で進めることを推奨する。Phase 0 の準備で詰まった場合は、運用ガイド `docs/notion-dashboard-operation-guide.md` §2 の手順を参照してください。

## 関連ドキュメント

| ドキュメント | 内容 |
|---|---|
| `docs/hokusai-notion-dashboard-implementation-plan.md` | 統合実装計画書 |
| `docs/notion-dashboard-operation-guide.md` | 運用ガイド（セットアップ手順含む） |
| `docs/codex-hokusai-notion-gitlab-operation-policy.md` | Notion / GitLab の役割分担方針 |
