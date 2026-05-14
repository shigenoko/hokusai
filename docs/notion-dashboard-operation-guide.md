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
- `~/.zshrc`（または SHELL から自動検出された rc）に 2 つの DB ID（`HOKUSAI_NOTION_WORKFLOWS_DB_ID` / `HOKUSAI_NOTION_PR_DB_ID`）が追記される
- マーカーで囲まれたブロックとして書き込まれるため、再実行時は **古いブロックを置き換え**（idempotent）
- 書き込み前に `~/.zshrc.hokusai.bak` バックアップを自動作成（`--no-backup` で無効化可）
- `--shell-rc <PATH>` で書き込み先を指定可能（bash 派、`/etc/profile` 派など）

`--persist` 無しの場合は `export` コマンド例を出力するだけ（手動でコピーして追記）。

実行すると以下のリソースが作成される:
- HOKUSAI Workflows DB（プロパティ 23 個 + Status / Waiting Reason / Priority の Select options）
- HOKUSAI Pull Requests DB（Workflow → Workflows DB の relation 付き）

##### ドキュメントツリーも同時に scaffold する（v0.4.3〜）

`--scaffold` オプションを付けると、DB 作成に加えて Notion governance layer の標準ドキュメントツリーも作成される。複数プロジェクトを Notion で並走させる場合に、人間が書くドキュメントの置き場所を統一できる。

```bash
hokusai notion-setup \
  --parent-page-id <PARENT_PAGE_ID> \
  --scaffold \
  --persist
```

作成されるツリー:

```
<親ページ>
├── HOKUSAI Workflows (DB)
├── HOKUSAI Pull Requests (DB)
└── 📚 HOKUSAI Documentation        ← Notion icon は 📚、title は素のテキスト
    ├── 💬 Discussions               ← 議論・設計判断（icon 💬）
    ├── 📖 Operation Guides          ← 運用手順（icon 📖）
    └── 📋 Requirements              ← 要件定義書 / GitHub リンク集（icon 📋）
```

> v0.4.4（Issue #27）以降: title 文字列は素のテキスト（`HOKUSAI Documentation` /
> `Discussions` / `Operation Guides` / `Requirements`）で、絵文字は Notion page icon
> でのみ表現。v0.4.3 で作成された絵文字 prefix 付きタイトルのページは後方互換で
> skip 検出される（重複作成しない）。UI 二重表示を解消したい場合は Notion 側で
> 手動リネーム（title から絵文字を削る）を推奨。

各ページの役割:

| ページ（icon / title） | 用途 | リポジトリ内の対応 |
|---|---|---|
| 💬 Discussions | コード変更前の議論・設計判断（決定後は関連 GitHub Issue を本文にリンク）| なし（議論記録は Notion） |
| 📖 Operation Guides | 日常運用手順（profile 切替、token 更新、復旧手順）| `docs/*-operation-guide.md` |
| 📋 Requirements | 要件定義書の Notion 版 / GitHub リンク集 | `docs/hokusai-*-requirements.md` |

設計原則:
- **オプトイン**: `--scaffold` 未指定なら従来通り DB だけ作成
- **scaffold ページのみ idempotent（配置先パスごと）**:
  - ハブ `HOKUSAI Documentation` は **親ページ直下** で既存検出 → 有れば skip
  - サブ 3 ページは **ハブ配下** で既存検出 → 有れば skip
  - 親ページ直下にサブと同名ページがあっても skip 対象にならない（パス違いのため）
  - 旧タイトル（絵文字 prefix 付き）も legacy alias として検出対象
- **partial success**: 個別サブページ作成失敗で全体が止まらない（ハブ作成失敗のみ致命）

> 旧版に存在した「HOKUSAI Service Status ページ」は、複数ユーザー環境で各自のローカル状態を上書きしてしまう問題（last-writer-wins）があるため廃止。サービス接続状態は HOKUSAI Operations Console（`scripts/dashboard.py`）でのみ参照する。

成功時に各リソースの ID と環境変数の export コマンド例が出力される。それを
`~/.zshrc` などに追記する。

冪等性の適用範囲は scaffold ページのみで、**DB 作成（Workflows / Pull Requests）は冪等ではない**: `notion-setup` を再実行すると新しい DB が毎回作成される。DB 作成をやり直したい場合は Notion 側で旧 DB を archive/削除してから再実行すること。

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

### 2.3. 環境変数の設定

```bash
# ~/.zshrc などに追記
export HOKUSAI_NOTION_API_TOKEN="secret_xxxxxxxxxx"
export HOKUSAI_NOTION_WORKFLOWS_DB_ID="32桁のDB ID"
export HOKUSAI_NOTION_PR_DB_ID="32桁のPR DB ID"
```

DB ID は、Notion の URL の末尾 32 桁から取得できる。

### 2.4. HOKUSAI 設定 YAML

```yaml
notion_dashboard:
  enabled: true
  api_token_env: HOKUSAI_NOTION_API_TOKEN
  workflows_db_id_env: HOKUSAI_NOTION_WORKFLOWS_DB_ID
  pull_requests_db_id_env: HOKUSAI_NOTION_PR_DB_ID
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
| **接続先 Notion の識別**（v0.4.2〜） | トップページの「Notion 同期パネル」内「接続先 Notion」セクション |
| ワークフローの緊急停止・cleanup | 一覧ページの操作ボタン |
| ログ・checkpoint 詳細確認 | ワークフロー詳細ページ |

##### 「接続先 Notion」セクションの見方（v0.4.2〜）

複数の Notion ワークスペースを profile 単位で使い分けている場合、dashboard 起動時に
「自分は今どの Notion につながっているか」を一目で確認できる:

| 項目 | 例 | 意味 |
|---|---|---|
| Profile | `hokusai` | 現在 active な profile 名 |
| API token env | `HOKUSAI_NOTION_API_TOKEN_4HOKUSAI` | 使用中の token env 変数名 |
| Workflows DB | `35f85495...82ff`（リンク） | Workflows DB の ID（マスク済み、クリックで Notion に遷移） |
| Pull Requests DB | `35f85495...c0dc`（リンク） | PR DB の ID（同上） |
| Bot user | `HOKUSAI Integration (bot)` | Notion API `GET /users/me` から取得した integration 名 |

DB ID は先頭 8 桁 + 末尾 4 桁のマスク表示。完全 ID はリンクの `title` 属性に持って
おり、マウスホバーで確認可能。Bot user 情報は 5 分間 process memory にキャッシュ
される（Notion API の rate limit 消費を抑えるため）。

> Service Status は各ユーザーのローカル CLI 状態（claude / codex / gh / glab 認証、Figma / Miro / Notion / Jira / Linear の token 設定）の集合。複数ユーザーで共有する Notion ページに同期するのは last-writer-wins になるため Notion 連携は廃止。Operations Console の「接続状態ページ」で各自確認する。

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
2. 環境変数 3 つ（`HOKUSAI_NOTION_API_TOKEN` / `HOKUSAI_NOTION_WORKFLOWS_DB_ID` / `HOKUSAI_NOTION_PR_DB_ID`）がすべて設定されているか
3. HOKUSAI 専用インテグレーションが対象 DB に「接続」されているか
4. Operations Console の Notion 同期パネルで保留・永続失敗の件数を確認
5. 必要なら「同期再送」ボタンを押す
6. それでも失敗する場合は、HOKUSAI のログ（`~/.hokusai/logs/`）で `notion_dashboard` 関連のエラーを確認

### 5.2. Slack 通知に Notion ページへのリンクが出ない

- `notion_dashboard.enabled: true` で環境変数が揃っているか確認
- Notion 側でワークフローレコードが作成されたあとの 2 回目以降の通知から URL が含まれる（初回は同期前のため）
- Notion API が応答しない場合は URL 解決をスキップして従来通り通知される（best effort）

### 5.3. レートリミット超過

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

### 7.1. Workflows DB のアーカイブ
完了から 6 ヶ月経過したワークフローを Notion 側で別 DB にアーカイブ。HOKUSAI 側からは何もしない。

### 7.2. SQLite outbox / errors テーブルの掃除
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
