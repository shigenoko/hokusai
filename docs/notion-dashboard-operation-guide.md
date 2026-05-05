# HOKUSAI Notion メインダッシュボード 運用ガイド

**対象読者**: PM、テックリード、運用担当エンジニア
**目的**: HOKUSAI のワークフロー進行状況を Notion で確認・操作するための運用手順

本ガイドは `docs/hokusai-notion-dashboard-implementation-plan.md` に基づく実装の使い方を説明する。

---

## 1. 役割分担の概要

| 場所 | 役割 | 主な利用者 |
|---|---|---|
| **Notion メインダッシュボード** | 見る・判断する・共有する | PM、営業、マーケ、QA、エンジニア |
| **HOKUSAI Web Dashboard**（Operations Console） | 動かす・直す・調べる | エンジニア、テックリード、運用担当 |

**起動・継続・再開といった操作は CLI または Operations Console から行う**。Notion からは行わない（誤操作・同期ズレ回避のため）。

## 2. 初期セットアップ手順

### 2.1. Notion インテグレーション作成（組織管理者）

1. https://www.notion.so/my-integrations にアクセス
2. **+ New integration** で HOKUSAI 専用のインテグレーションを作成
3. 必要な権限: Read content / Update content / Insert content
4. **Internal Integration Token** をコピー（`secret_xxx...`）

### 2.2. Notion DB / ページ作成

#### 推奨: 自動セットアップツールを使う

23 個のプロパティと relation を手動で作るのは間違いやすいため、HOKUSAI に同梱の
セットアップ CLI で一括作成することを推奨する。

```bash
# 1. 親ページを Notion 上に作成（例: "HOKUSAI Workspace"）
# 2. 親ページに HOKUSAI integration を接続（右上 ⋯ → Add connections）
# 3. 親ページの URL から page_id を取得（URL 末尾の 32 桁）
# 4. API token を環境変数に設定
export HOKUSAI_NOTION_API_TOKEN="secret_xxxxxxxxxx"

# 5. セットアップ実行（--persist で作成された DB ID を ~/.zshrc に自動追記）
hokusai notion-setup --parent-page-id <PARENT_PAGE_ID> --persist
```

`--persist` 指定で:
- `~/.zshrc`（または SHELL から自動検出された rc）に 3 つの DB / ページ ID が追記される
- マーカーで囲まれたブロックとして書き込まれるため、再実行時は **古いブロックを置き換え**（idempotent）
- 書き込み前に `~/.zshrc.hokusai.bak` バックアップを自動作成（`--no-backup` で無効化可）
- `--shell-rc <PATH>` で書き込み先を指定可能（bash 派、`/etc/profile` 派など）

`--persist` 無しの場合は `export` コマンド例を出力するだけ（手動でコピーして追記）。

実行すると以下のリソースが作成される:
- HOKUSAI Workflows DB（プロパティ 23 個 + Status / Waiting Reason / Priority の Select options）
- HOKUSAI Pull Requests DB（Workflow → Workflows DB の relation 付き）
- HOKUSAI Service Status ページ（HOKUSAI が定期書き換える）

成功時に各リソースの ID と環境変数の export コマンド例が出力される。それを
`~/.zshrc` などに追記する。

冪等性は持たないため、再実行すると新しい DB / ページが作成される。失敗時は Notion 側
で archived/削除してから再実行すること。

#### 手動で作成する場合

以下を Notion ワークスペース内に作成し、HOKUSAI インテグレーションを「接続」する。

#### HOKUSAI Workflows DB
推奨プロパティ（実装計画書 §6.2 と同期）:

| プロパティ名 | 型 |
|---|---|
| Name | Title |
| Workflow ID | Text |
| Status | Select（`Ready` / `Running` / `Waiting for Human` / `Failed` / `Done` / `Canceled`） |
| Current Phase | Number |
| Current Phase Name | Text |
| Waiting Reason | Select |
| Next Action | Text |
| Business Owner | Person |
| Tech Lead | Person |
| GitLab MR | URL |
| Research Page | URL |
| Design Page | URL |
| Plan Page | URL |
| Started At | Date |
| Completed At | Date |
| Last Updated | Date |
| Error Summary | Text |

#### HOKUSAI Pull Requests DB
| プロパティ名 | 型 |
|---|---|
| PR Number | Title |
| URL | URL |
| Repository | Select |
| Status | Select |
| Workflow | Relation（→ Workflows DB） |
| Created At | Date |
| Last Updated | Date |

#### HOKUSAI Service Status ページ
通常のページとして作成。本文は HOKUSAI が自動的に書き換える。

### 2.3. 環境変数の設定

```bash
# ~/.zshrc などに追記
export HOKUSAI_NOTION_API_TOKEN="secret_xxxxxxxxxx"
export HOKUSAI_NOTION_WORKFLOWS_DB_ID="32桁のDB ID"
export HOKUSAI_NOTION_PR_DB_ID="32桁のPR DB ID"
export HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID="32桁のページ ID"
```

DB ID / ページ ID は、Notion の URL の末尾 32 桁から取得できる。

### 2.4. HOKUSAI 設定 YAML

```yaml
notion_dashboard:
  enabled: true
  api_token_env: HOKUSAI_NOTION_API_TOKEN
  workflows_db_id_env: HOKUSAI_NOTION_WORKFLOWS_DB_ID
  pull_requests_db_id_env: HOKUSAI_NOTION_PR_DB_ID
  service_status_page_id_env: HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID
  sync_outbox:
    enabled: true
    max_retry_attempts: 10
  retry:
    max_attempts: 3
    backoff_seconds: 5
  rate_limit:
    requests_per_second: 2
    debounce_ms: 5000
```

⚠️ **API token は YAML に書かない**。環境変数経由で渡す。書き込んだ場合は Operations Console の検証で警告が出る。

### 2.5. 動作確認

```bash
# ワークフローを 1 つ起動
hokusai start <Notion Task URL>

# Notion Workflows DB に新規レコードが作成されることを確認
# Phase 進行に応じて Status / Current Phase が更新されることを確認
```

## 3. 日常運用フロー

### 3.1. ビジネスサイド（営業・マーケ・PM）

**見る場所**: Notion HOKUSAI Workflows DB

| やりたいこと | 見るビュー |
|---|---|
| 全体ロードマップ | Notion 親ページの Timeline ビュー |
| 進行中のタスク | Workflows DB の **Active** ビュー |
| 人間判断待ちの案件 | **Waiting for Human** ビュー |
| 自分が責任者の案件 | **By Business Owner** ビュー |
| 失敗中の案件 | **Failed** ビュー |
| リリース予定 | Workflows DB を Status / Completed At でフィルタ |

### 3.2. エンジニア・テックリード

**見る場所**: Notion HOKUSAI Workflows DB + HOKUSAI Web Dashboard（Operations Console）

#### 通常の作業フロー
```bash
# 1. PM が Notion で Task ページを起票
# 2. エンジニアが Notion Task URL を取得して HOKUSAI 起動
hokusai start <Notion Task URL>

# 3. HOKUSAI が以下を自動実行
#    - Phase 2/3/4 を Notion 子ページとして追記
#    - Phase 8a で GitLab MR を自動作成
#    - Notion Workflows DB に進捗を同期
#    - Slack 通知（Notion ページへのディープリンク付き）

# 4. 人間レビュー待ちで停止 → Slack 通知 + Notion Status=Waiting for Human
# 5. 内容確認 → GitLab MR でレビュー → 承認
# 6. レビュー対応再開
hokusai continue <workflow-id>

# 7. マージ後、HOKUSAI が Notion Status=Done に更新
```

#### Operations Console（HOKUSAI Web Dashboard）の使い分け

| 用途 | 操作 |
|---|---|
| 設定 YAML の編集・バックアップ・復元 | 設定ページ |
| 接続状態（gh / glab / notion / codex / claude）の再チェック | 接続状態ページ |
| Notion 同期失敗の再送 | トップページの「Notion 同期パネル」 |
| Service Status を Notion に反映 | トップページの「Service Status を Notion へ反映」ボタン |
| ワークフローの緊急停止・cleanup | 一覧ページの操作ボタン |
| ログ・checkpoint 詳細確認 | ワークフロー詳細ページ |

### 3.3. Notion 同期が失敗した場合

Notion API が一時的に応答しない、レートリミット超過、認証切れなどで同期が失敗することがある。**ワークフロー本体は止まらず継続**する（best effort 設計）。

#### 確認方法
HOKUSAI Web Dashboard のトップページに「Notion 同期パネル」が表示される:
- **保留 N 件**: SQLite outbox に積まれた失敗イベント数
- **永続失敗 N 件**: 最大リトライ回数を超えて errors テーブルに移されたイベント数

#### 復旧手順
1. **Notion 側の問題を確認** — API ステータス、認証、DB の存在、権限
2. Operations Console の **「同期再送」** ボタンを押す
3. 結果が表示される（成功 N 件 / 失敗 N 件 / permanent N 件）
4. permanent failure に移されたイベントは、Notion 側の問題を解決してから SQL で手動操作するか、設定を見直す

## 4. Human-in-the-loop（人間判断待ち）の対応

### 4.1. 通知の流れ

1. HOKUSAI が `waiting_for_human` 状態で停止
2. **Slack 通知**: Status・理由・Notion ページへのディープリンクを送信
3. **Notion Workflows DB**: Status=`Waiting for Human` + Waiting Reason + Next Action が自動更新

### 4.2. Next Action の標準テンプレート

`waiting_reason` ごとに HOKUSAI が標準化された次アクションを Notion に書き出す:

| Waiting Reason | 推奨アクション |
|---|---|
| `branch_hygiene` | rebase / cherry-pick / merge / ignore のいずれかを選んで `hokusai continue <wf>` |
| `cross_review_blocked` | apply-cross-review-fixes か continue-ignore-cross-review を選択 |
| `review_wait` | GitLab MR を確認後 `hokusai continue <wf>` |
| `copilot_review_wait` | Copilot レビュー完了後 `hokusai continue <wf>` |
| `human_review_wait` | 人間レビュー完了後 `hokusai continue <wf>` |
| `review_fix` | レビュー修正適用後 `hokusai continue <wf>` |

### 4.3. 再開操作

```bash
# CLI から
hokusai continue <workflow-id>

# Operations Console から
# /api/workflow/continue-step または continue-auto を呼ぶ UI ボタンを使用
```

⚠️ **Notion から再開しない**。Notion API の遅延や同期ズレで状態が壊れる可能性があるため、Notion はあくまで「見る」場所として使う。

## 5. トラブルシューティング

### 5.1. Notion 同期が反映されない

確認順序:
1. `notion_dashboard.enabled: true` になっているか（YAML 確認）
2. 環境変数 4 つがすべて設定されているか（`echo $HOKUSAI_NOTION_API_TOKEN` など）
3. HOKUSAI 専用インテグレーションが対象 DB / ページに「接続」されているか
4. Operations Console の Notion 同期パネルで保留・永続失敗の件数を確認
5. 必要なら「同期再送」ボタンを押す
6. それでも失敗する場合は、HOKUSAI のログ（`~/.hokusai/logs/`）で `notion_dashboard` 関連のエラーを確認

### 5.2. Service Status ページが更新されない

- HOKUSAI Web Dashboard トップページの「Service Status を Notion へ反映」ボタンを押す
- ボタンを押しても変化しない場合は環境変数 `HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID` を確認

### 5.3. Slack 通知に Notion ページへのリンクが出ない

- `notion_dashboard.enabled: true` で環境変数が揃っているか確認
- Notion 側でワークフローレコードが作成されたあとの 2 回目以降の通知から URL が含まれる（初回は同期前のため）
- Notion API が応答しない場合は URL 解決をスキップして従来通り通知される（best effort）

### 5.4. レートリミット超過

Notion API は 3 req/sec 程度のレートリミットがある。並行ワークフロー数が多くて詰まる場合:
- `notion_dashboard.rate_limit.requests_per_second` を 1.0 に下げる
- `debounce_ms` を 8000〜10000 に上げる
- 並行起動数を 5 件以下に絞る

## 6. アクセス制限

### 6.1. Notion メインダッシュボード
- ビジネスサイド全員に閲覧権限（Notion ワークスペース側で設定）
- 編集権限は PM・テックリードに絞ることを推奨

### 6.2. HOKUSAI Web Dashboard（Operations Console）
- **管理者・開発者のみ**に制限することを推奨
- 推奨方法: BASIC 認証 / VPN 経由のみ許可 / 社内 SSO
- `start / continue / retry / cleanup` といった破壊的操作を全員が触れる状態は危険

## 7. 定期メンテナンス

### 7.1. Service Status の定期反映

HOKUSAI には専用 CLI コマンドが用意されている:

```bash
hokusai sync-service-status
```

このコマンドを cron / launchd で定期実行する。

#### crontab 例（macOS / Linux）

```cron
# /etc/cron.d/hokusai-sync または crontab -e で追加
# 1 時間ごとに Service Status を Notion へ反映
0 * * * * cd /path/to/your/project && /usr/local/bin/hokusai -c configs/your-config.yaml sync-service-status >> ~/.hokusai/logs/sync-service-status.log 2>&1
```

#### launchd 例（macOS）

`~/Library/LaunchAgents/com.hokusai.sync-service-status.plist` を以下の内容で作成:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.hokusai.sync-service-status</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/hokusai</string>
    <string>-c</string>
    <string>/path/to/configs/your-config.yaml</string>
    <string>sync-service-status</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/your/project</string>
  <key>StartInterval</key>
  <integer>3600</integer>
  <key>StandardOutPath</key>
  <string>/Users/your-name/.hokusai/logs/sync-service-status.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/your-name/.hokusai/logs/sync-service-status.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOKUSAI_NOTION_API_TOKEN</key>
    <string>secret_xxxxxxxxxx</string>
    <key>HOKUSAI_NOTION_WORKFLOWS_DB_ID</key>
    <string>32桁の DB ID</string>
    <key>HOKUSAI_NOTION_PR_DB_ID</key>
    <string>32桁の PR DB ID</string>
    <key>HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID</key>
    <string>32桁のページ ID</string>
  </dict>
</dict>
</plist>
```

ロード:

```bash
launchctl load ~/Library/LaunchAgents/com.hokusai.sync-service-status.plist
```

⚠️ launchd plist には API token を平文で書くため、ファイルパーミッションを `chmod 600` に設定し、リポジトリにコミットしないこと。

#### CLI コマンドの動作仕様

| 状態 | 出力 | 終了コード |
|---|---|---|
| `notion_dashboard.enabled=false` | `notion_dashboard.enabled=false のためスキップしました` | 0 |
| 環境変数未設定 | `Notion 同期の環境変数が未設定のためスキップしました...` | 0 |
| 同期成功 | `✓ Service Status を Notion に反映しました` | 0 |
| 同期失敗 | `✗ Service Status の Notion 反映に失敗しました` | 1 |

`enabled=false` や環境変数未設定では終了コード 0 を返すため、cron が常時 error 通知を送らない設計になっている。

または HOKUSAI Web Dashboard のボタンを手動で押す。

### 7.2. Workflows DB のアーカイブ
完了から 6 ヶ月経過したワークフローを Notion 側で別 DB にアーカイブ。HOKUSAI 側からは何もしない。

### 7.3. SQLite outbox / errors テーブルの掃除
通常は不要。ただし長期間放置されている場合:
```bash
# 必要なら手動で削除
sqlite3 ~/.hokusai/workflow.db "DELETE FROM notion_sync_errors WHERE failed_at < date('now', '-6 month');"
```

## 8. 関連ドキュメント

| ドキュメント | 内容 |
|---|---|
| `docs/hokusai-notion-dashboard-implementation-plan.md` | 統合実装計画書（本書のベース） |
| `docs/codex-hokusai-notion-gitlab-operation-policy.md` | Notion / GitLab の役割分担方針 |
| `docs/codex-slack-notification-implementation-plan.md` | Slack 通知の実装計画 |

## 9. まとめ

- **Notion = 見る・判断する・共有する**（全社員）
- **HOKUSAI Web Dashboard = 動かす・直す・調べる**（管理者・開発者）
- **CLI = 実行と再開**（エンジニア）
- LangGraph と SQLite は実行エンジンの正本、Notion は同期されたビュー
- 同期失敗はワークフロー本体を止めず、Operations Console から再送可能

ビジネスとエンジニアの境界をなくすには、**Notion を主要コンテンツエリアとして全員で見る運用に統一する**のが鍵です。
