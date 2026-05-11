# Notion メインダッシュボード化 + HOKUSAI Web Dashboard の運用コンソール化 設計書

**作成日**: 2026-05-05

> ⚠️ **2026-05-11 改訂**: Service Status を Notion ページに同期する案は廃止しました（複数ユーザー環境で last-writer-wins になるため）。サービス接続状態は HOKUSAI Operations Console（`scripts/dashboard.py`）でのみ参照します。本書中の Service Status 関連の章は歴史的経緯として残しますが、現行コードには存在しません。

**対象読者**: プロジェクト責任者・テックリード・実装担当エンジニア
**目的**: ビジネス側を含めた組織横断の情報共有基盤として Notion をメインダッシュボードに、HOKUSAI Web Dashboard を管理者向け運用コンソールとして再定義する

---

## 1. 背景と目的

HOKUSAI は現在、Web Dashboard（`scripts/dashboard.py`）にワークフロー状態・接続状態・設定編集をすべて集約している。一方、組織方針として「ビジネスサイドとエンジニアサイドの壁をなくし、Notion を主要コンテンツエリアとして活用する」が決定された。

このため、ダッシュボード機能を以下の 2 層に再定義する。

- **Notion（メインダッシュボード）**: 全社員が日常的に「見る・動かす」場所
- **HOKUSAI Web Dashboard（運用コンソール）**: 管理者が「設定・診断・緊急操作」を行う場所

## 2. ゴールと非ゴール

### 2.1. ゴール

- ビジネスサイド（営業・マーケ）が Notion だけでワークフロー進捗を把握できる
- エンジニアが Notion からワークフロー起動・継続を実行できる
- HOKUSAI Web Dashboard はトークン管理・設定編集・障害対応に特化する
- 既存の SQLite を内部状態の正本として維持し、Notion は同期ビューとする
- LangGraph による実行エンジンは現状維持（外さない）

### 2.2. 非ゴール

- LangGraph を Notion で代替する
- Notion で設定 YAML を直接編集する
- ワークフローの内部状態の正本を Notion に移す
- 全機能を Notion に移行する（一部は HOKUSAI 側に残す）

## 3. 現状の課題

| 課題 | 説明 |
|---|---|
| ビジネス側が Web Dashboard を見ない | Notion 中心の文化と異なる UI を使うことに抵抗 |
| 進捗把握が分散 | 営業はリリース予定を別途エンジニアに聞く必要がある |
| Web Dashboard の責任範囲が広すぎる | 閲覧と設定編集が混在し、誤操作リスク |
| モバイル対応なし | 出先からの確認は Notion アプリの方が便利 |

## 4. ターゲット状態

### 4.1. レイヤー定義

```
┌──────────────────────────────────────────────────────┐
│ レイヤー A: 全社員向けダッシュボード                      │
│ Notion（メインダッシュボード）                            │
│ ├─ Workflows DB（進捗 Kanban / Table / Calendar）      │
│ ├─ Pull Requests DB（PR 一覧）                         │
│ ├─ Service Status ページ（接続状態スナップショット）       │
│ └─ 起動・継続ボタン（Webhook 経由）                      │
└──────────────────────────────────────────────────────┘
                        ↑ 同期書き込み
┌──────────────────────────────────────────────────────┐
│ レイヤー B: 管理者向け運用コンソール                       │
│ HOKUSAI Web Dashboard（縮退・特化）                    │
│ ├─ 設定 YAML 編集（バリデーション・バックアップ）          │
│ ├─ トークン直書き警告                                   │
│ ├─ 接続状態の再チェック                                 │
│ ├─ 緊急停止・デバッグ                                   │
│ └─ Notion 同期の状態確認・再送                          │
└──────────────────────────────────────────────────────┘
                        ↑ 内部状態の正本
┌──────────────────────────────────────────────────────┐
│ レイヤー C: 実行エンジン                                  │
│ HOKUSAI ワーカー + LangGraph + SQLiteStore             │
└──────────────────────────────────────────────────────┘
```

### 4.2. 各レイヤーの責任分界

| 機能カテゴリ | Notion（メイン） | HOKUSAI Dashboard（運用） |
|---|:---:|:---:|
| ワークフロー一覧閲覧 | ✅ | △（縮退、リンクのみ） |
| Phase 進捗閲覧 | ✅ | △ |
| PR 一覧閲覧 | ✅ | △ |
| 接続状態スナップショット閲覧 | ✅ | ✅ |
| 接続状態の再チェック実行 | △（ボタン） | ✅ |
| ワークフロー起動 | ✅（ボタン） | ❌ |
| ワークフロー継続（continue） | ✅（ボタン） | ❌ |
| 緊急停止・kill | ❌ | ✅ |
| 設定 YAML 編集 | ❌ | ✅ |
| 設定バックアップ・復元 | ❌ | ✅ |
| トークン直書き警告 | ❌ | ✅ |
| 監査ログ閲覧 | △（要約のみ） | ✅ |
| Notion 同期エラー対応 | ❌ | ✅ |

## 5. Notion 側の設計

### 5.1. Workspace 構造

```
HOKUSAI Workspace（既存の Notion ワークスペース内）
├─ 📊 Workflows DB
│   ├─ Status ビュー（Kanban: Phase 別）
│   ├─ Active ビュー（Table: 進行中のみ）
│   ├─ Recent ビュー（Calendar: 開始日でソート）
│   └─ All ビュー（完了・失敗を含む全件）
│
├─ 🔀 Pull Requests DB
│   ├─ Open PRs（Status: Draft / Review / Approved）
│   ├─ Recent Merged
│   └─ By Workflow（Workflow ごとの PR 集約）
│
├─ 🔌 Service Status ページ
│   └─ gh / glab / notion_mcp / codex / claude の最新状態
│
└─ 🛠️ HOKUSAI Operations ページ（管理者向けポータル）
    ├─ HOKUSAI Web Dashboard へのリンク
    ├─ 同期状態（最終同期時刻・失敗件数）
    └─ 緊急時の操作手順
```

### 5.2. Workflows DB スキーマ

| プロパティ名 | 型 | 用途 | 更新主体 |
|---|---|---|---|
| `Workflow ID` | Title | 一意識別子（例: wf-1234abcd） | HOKUSAI |
| `Task` | URL | Notion タスクページ URL | HOKUSAI |
| `Task Title` | Text | タスクタイトル | HOKUSAI |
| `Status` | Select | running / waiting_for_human / failed / completed | HOKUSAI |
| `Current Phase` | Number | 1〜10 | HOKUSAI |
| `Phase Name` | Text | 「Phase 5: 実装」など | HOKUSAI |
| `Branch` | Text | feature/xxx | HOKUSAI |
| `PRs` | Relation | Pull Requests DB へのリレーション | HOKUSAI |
| `Started At` | Date | 開始日時 | HOKUSAI |
| `Updated At` | Date | 最終更新日時 | HOKUSAI |
| `Repository` | Multi-select | Backend / Frontend など | HOKUSAI |
| `Continue` | Button | クリックで `hokusai continue` を実行 | 人間 → Webhook |
| `Last Sync` | Date | Notion 同期成功時刻 | HOKUSAI |
| `Sync Errors` | Text | 同期失敗時のエラー | HOKUSAI |

### 5.3. Pull Requests DB スキーマ

| プロパティ名 | 型 | 用途 |
|---|---|---|
| `PR Number` | Title | PR 番号 |
| `URL` | URL | PR URL |
| `Repository` | Select | Backend / Frontend |
| `Status` | Select | Draft / Open / Approved / Merged / Closed |
| `Workflow` | Relation | Workflows DB へのリレーション |
| `Reviewer` | Multi-select | レビュアー |
| `Created At` | Date | 作成日時 |
| `Last Updated` | Date | 最終更新 |

### 5.4. Service Status ページ

サービスごとの最新接続状態を表として表示。

| サービス | 状態 | 最終チェック | メモ |
|---|---|---|---|
| gh | ✅ Connected | 2026-05-05 10:30 | scope: repo, write |
| glab | ⚠️ Not Authenticated | 2026-05-05 10:30 | `glab auth login` 必要 |
| notion_mcp | ✅ Connected | 2026-05-05 10:30 | |
| codex | ✅ Installed | 2026-05-05 10:30 | |
| claude | ✅ Installed | 2026-05-05 10:30 | |

「再チェック実行は HOKUSAI Web Dashboard から」と案内するリンクを併記。

## 6. HOKUSAI Web Dashboard 側の再定義

### 6.1. 残す機能

| 機能 | 現状 | 再定義後 |
|---|---|---|
| 設定 YAML 編集 | ✅ | ✅ 残す（メイン機能化） |
| 設定バリデーション・警告 | ✅ | ✅ 残す |
| 設定バックアップ・復元 | ✅ | ✅ 残す |
| トークン直書き警告 | ✅ | ✅ 残す |
| 接続状態の再チェックボタン | ✅ | ✅ 残す |
| ワークフロー一覧 | ✅ | △ 縮退（Notion へのリンクのみ） |
| ワークフロー詳細 | ✅ | △ 縮退（緊急時のデバッグ用） |
| 緊急停止・kill | ✅ | ✅ 残す |

### 6.2. 追加する機能

| 機能 | 用途 |
|---|---|
| **Notion 同期状態パネル** | Notion DB への書き込み成功/失敗・最終同期時刻・失敗件数を表示 |
| **同期再送ボタン** | 失敗した同期を手動で再送 |
| **Notion 接続テスト** | Workflows DB / Pull Requests DB へのアクセス権限を検証 |
| **Webhook 受信ログ** | Notion ボタンからの Webhook 受信履歴と実行結果 |

### 6.3. 削除・縮退する機能

| 機能 | 対応 |
|---|---|
| ワークフロー一覧（Kanban 表示） | Notion へリダイレクト |
| ワークフロー詳細の進捗バー | Notion ページへのリンクに置き換え |
| PR 一覧 | Notion へリダイレクト |

## 7. 同期メカニズム

### 7.1. HOKUSAI → Notion（書き込み）

書き込みポイント:

| タイミング | 書き込み先 | 内容 |
|---|---|---|
| `WorkflowRunner.start()` 直後 | Workflows DB（新規作成） | 初期状態 |
| 各 Phase ノード完了時 | Workflows DB（更新） | current_phase, phase_name, updated_at |
| Phase 8a 完了時 | Pull Requests DB（新規作成）+ Workflows DB（リレーション更新） | PR 情報 |
| `_run_stream_loop()` 終了時 | Workflows DB（更新） | status: waiting / failed / completed |
| 接続状態チェック実行時 | Service Status ページ | 各サービスの状態 |

実装方針:

- 既存の `_run_stream_loop()` の永続化箇所（`save_workflow`）にフックを追加
- Slack 通知と同様に **best effort**（失敗してもワークフロー本体は止めない）
- 失敗した同期は `sync_errors` にスタック → ダッシュボードから再送可能
- レートリミット対策として **バッチ更新 + 5 秒デバウンス**

### 7.2. Notion → HOKUSAI（イベント受信）

Notion ボタン → 中継サーバー → HOKUSAI CLI 実行

```
[Notion ボタン押下]
   ↓
[Notion Automation: Webhook 送信]
   ↓
[中継サーバー（FastAPI）: 認証検証]
   ├─ 認証 OK → HOKUSAI CLI 実行（subprocess）
   │   └─ 実行結果を Notion に書き戻し
   └─ 認証 NG → 拒否 + ログ
```

### 7.3. データ正本の原則

- **HOKUSAI SQLiteStore = 内部状態の正本**
- **Notion DB = 同期されたビュー**
- 不整合時は SQLite を信じる
- 復旧時は SQLite から Notion を再構築

## 8. 中継サーバー（Webhook ブリッジ）

### 8.1. 役割

Notion からの Webhook を受け取り、HOKUSAI CLI を呼び出す薄い HTTP サーバー。

### 8.2. 構成

```python
# scripts/notion_webhook_bridge.py（新規）
# FastAPI ベース、ポート 8765 想定

POST /webhook/start
  body: { workflow_url: str, signature: str }
  action: hokusai start <workflow_url>

POST /webhook/continue
  body: { workflow_id: str, action: str | null, signature: str }
  action: hokusai continue <workflow_id> [--action <action>]
```

### 8.3. セキュリティ

| 項目 | 対策 |
|---|---|
| Webhook 認証 | HMAC-SHA256 署名（共有シークレット） |
| 共有シークレット保存 | 環境変数 `HOKUSAI_WEBHOOK_SECRET` |
| IP 制限 | （任意）Notion の出口 IP のみ許可 |
| 重複リクエスト対策 | リクエスト ID + 5 分間のキャッシュ |
| 監査ログ | 全リクエストをログに記録（ペイロード含む） |

### 8.4. 運用

- ローカル開発: `python scripts/notion_webhook_bridge.py`
- 本番運用: systemd / Docker / k8s 等で常駐
- 失敗時のリトライ: Notion Automation 側のリトライに任せる（中継サーバーは冪等に設計）

## 9. 実装ステップ

### Phase A: Notion 同期の基盤（2 週間）

- A-1: `hokusai/integrations/notion_dashboard.py` 新規作成
  - Workflows DB 書き込み・更新クライアント
  - Pull Requests DB 書き込みクライアント
  - レート制限対応・リトライ
- A-2: `WorkflowConfig.notion_dashboard` 設定追加
  - DB ID（workflows / pull_requests）
  - 認証情報の参照（環境変数）
- A-3: `WorkflowRunner` に Notion 書き込みフックを追加
  - 既存の `save_workflow` 後に呼び出し
- A-4: 単体テスト + 結合テスト

### Phase B: Service Status の Notion 反映（1 週間）

- B-1: `connection_status` の結果を Notion に書き出すスクリプト
- B-2: 定期実行（cron / launchd）
- B-3: HOKUSAI Web Dashboard から手動実行ボタン

### Phase C: Notion ボタン → Webhook → HOKUSAI 起動（2 週間）

- C-1: 中継サーバー `scripts/notion_webhook_bridge.py` 実装
- C-2: HMAC 認証・監査ログ
- C-3: Notion Automation 設定（テンプレート提供）
- C-4: 起動・継続のドキュメント整備

### Phase D: HOKUSAI Web Dashboard の再定義（1〜2 週間）

- D-1: ワークフロー一覧の縮退（Notion へのリンク化）
- D-2: Notion 同期状態パネルの追加
- D-3: 同期再送ボタンの実装
- D-4: ナビゲーション再構成

### Phase E: ドキュメント・パイロット運用（1 週間）

- E-1: 運用ガイド作成
- E-2: 1 チームでパイロット運用
- E-3: フィードバックを反映
- E-4: 全社展開

**合計工数: 7〜9 週間（実働 1.5〜2 ヶ月）**

## 10. 設定例

### 10.1. WorkflowConfig 拡張案

```yaml
notion_dashboard:
  enabled: true
  workspace_id_env: HOKUSAI_NOTION_WORKSPACE_ID
  workflows_db_id_env: HOKUSAI_NOTION_WORKFLOWS_DB_ID
  pull_requests_db_id_env: HOKUSAI_NOTION_PR_DB_ID
  service_status_page_id_env: HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID
  # 同期失敗時のリトライ設定
  retry:
    max_attempts: 3
    backoff_seconds: 5
  # レートリミット対策
  rate_limit:
    requests_per_second: 2
    debounce_ms: 5000

webhook_bridge:
  enabled: true
  port: 8765
  secret_env: HOKUSAI_WEBHOOK_SECRET
  audit_log_path: ~/.hokusai/logs/webhook_audit.log
```

### 10.2. 環境変数

```bash
# Notion
export HOKUSAI_NOTION_WORKSPACE_ID="..."
export HOKUSAI_NOTION_WORKFLOWS_DB_ID="..."
export HOKUSAI_NOTION_PR_DB_ID="..."
export HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID="..."

# Webhook
export HOKUSAI_WEBHOOK_SECRET="$(openssl rand -hex 32)"
```

## 11. セキュリティ設計

### 11.1. Notion アクセス権限

- HOKUSAI 用 Notion インテグレーションを作成し、対象 DB / ページのみに権限付与
- 個人ユーザーアカウントに依存しない（インテグレーション専用）

### 11.2. Webhook 認証

- HMAC-SHA256 署名必須
- 共有シークレットは環境変数経由（YAML 直書き禁止）
- `_detect_token_like_values` の警告対象に追加

### 11.3. 機密情報の扱い

- ワークフロー state に含まれる機密情報（API キー等）は Notion に書き込まない
- Notion DB のアクセス権限を組織単位で適切に設定
- 監査ログには Webhook ペイロードを記録するが、認証情報はマスク

## 12. 障害時の挙動

| 障害シナリオ | 挙動 | 復旧方法 |
|---|---|---|
| Notion API ダウン | 書き込み失敗を `sync_errors` に蓄積、ワークフロー本体は継続 | Notion 復旧後、HOKUSAI Web Dashboard の「同期再送」ボタン |
| Notion DB が削除された | 書き込みが 404 で失敗、warn ログ | DB を再作成し、ID を環境変数に再設定 |
| 中継サーバーダウン | Notion ボタンが 5xx で失敗 | サーバー再起動。CLI 直接実行で代替可能 |
| Notion 認証切れ | 401 で書き込み失敗 | インテグレーションを再認証 |
| HOKUSAI 側ダウン | 中継サーバーが subprocess 起動失敗 | HOKUSAI 再起動 |

**設計原則: Notion 障害が HOKUSAI のワークフロー実行を止めないこと。**

## 13. パフォーマンス想定

| 項目 | 想定値 | 備考 |
|---|---|---|
| Notion 書き込みレイテンシ | 1〜3 秒 | API 呼び出し |
| 1 ワークフローあたりの書き込み回数 | 10〜30 回 | 各 Phase + PR + 完了 |
| Notion API レートリミット | 3 req/sec | バッチ + デバウンスで吸収 |
| 並行ワークフロー上限（同期込み） | 5〜10 件 | レートリミットを考慮 |
| Webhook → CLI 起動レイテンシ | 5〜10 秒 | subprocess 起動コスト |

## 14. 受け入れ基準（Definition of Done）

### 14.1. Phase A（Notion 同期基盤）

- [ ] HOKUSAI 起動時に Workflows DB に新規レコードが作成される
- [ ] 各 Phase 完了時に Notion DB が更新される
- [ ] PR 作成時に Pull Requests DB にレコードが作成される
- [ ] Notion 書き込み失敗でワークフロー本体が止まらない
- [ ] レートリミット超過時もリトライで吸収される
- [ ] 単体テスト・結合テストが追加されている

### 14.2. Phase B（Service Status）

- [ ] Service Status ページに最新の接続状態が反映される
- [ ] 定期実行（cron）が動作する

### 14.3. Phase C（Webhook ブリッジ）

- [ ] Notion ボタンから `hokusai start` が起動できる
- [ ] Notion ボタンから `hokusai continue` が実行できる
- [ ] HMAC 認証なしのリクエストは拒否される
- [ ] 監査ログが記録される

### 14.4. Phase D（Web Dashboard 再定義）

- [ ] ワークフロー一覧が Notion へのリンクに置き換わっている
- [ ] Notion 同期状態パネルが追加されている
- [ ] 同期再送ボタンが動作する
- [ ] 既存の設定編集機能はそのまま使える

### 14.5. Phase E（運用）

- [ ] 運用ガイドが作成されている
- [ ] 1 チームのパイロット運用が完了している
- [ ] ビジネスサイドからのフィードバックが反映されている

## 15. オープンクエスチョン

実装着手前に合意すべき事項。

1. **Notion インテグレーションの作成主体**
   - 暫定案: 組織管理者が作成し、HOKUSAI 用専用権限を付与

2. **Workflows DB の保持期間**
   - 完了済みワークフローを永続的に Notion に残すか、3 ヶ月後にアーカイブするか
   - 暫定案: 完了から 6 ヶ月で別 DB にアーカイブ

3. **Notion ライセンス**
   - ビジネスサイド全員に閲覧権限が必要 → ライセンス費用の試算
   - 暫定案: Notion Plus プラン（チーム単位課金）

4. **中継サーバーのホスティング**
   - 開発時はローカル実行、本番はどこに置くか
   - 暫定案: 自社 VPN 内の常駐サーバー or k8s クラスタ

5. **既存ワークフローのマイグレーション**
   - 既に進行中のワークフローを Notion DB に流し込むか
   - 暫定案: 流し込まない（新規ワークフローから適用）

6. **HOKUSAI Web Dashboard のアクセス制限**
   - 全員が触れる現状から、管理者のみに制限すべきか
   - 暫定案: BASIC 認証 or VPN 経由のみ許可

7. **Webhook 中継サーバーの開発言語**
   - HOKUSAI 本体に統合するか、独立した FastAPI サーバーにするか
   - 暫定案: 独立 FastAPI サーバー（責務分離・障害分離）

各項目の暫定案で進めて差し支えなければ、レビュアからの no-objection をもって着手する。

## 16. 将来拡張

- Slack 通知メッセージ内に Notion DB へのディープリンクを含める
- Notion AI を活用したワークフロー要約の自動生成
- 複数プロジェクト横断での Workflows DB 集約
- ロール別ビュー（PM 用 / エンジニア用 / 営業用）の整備
- Notion 内ボットによる対話的な操作（チャット形式）

## 17. 関連ドキュメント

- `docs/codex-hokusai-notion-gitlab-operation-policy.md`: Notion / GitLab の役割分担方針（本設計の前提）
- `docs/claude-hokusai-notion-gitlab-roles.md`: プロジェクト責任者向けの役割整理
- `docs/dashboard-connection-settings-proposal.md`: 既存 Dashboard の接続状態パネル提案
- `docs/codex-slack-notification-implementation-plan.md`: Slack 通知の実装計画

## 18. まとめ

| 項目 | 内容 |
|---|---|
| Notion の役割 | 全社員向けメインダッシュボード（閲覧 + ボタン操作） |
| HOKUSAI Web Dashboard の役割 | 管理者向け運用コンソール（設定・診断・障害対応） |
| LangGraph | 現状維持（実行エンジン） |
| SQLiteStore | 現状維持（内部状態の正本） |
| Notion DB | 同期されたビュー（正本ではない） |
| 工数 | 7〜9 週間（実働 1.5〜2 ヶ月） |
| 受け入れ基準 | 5 つの Phase それぞれに DoD を設定 |

**ビジネスとエンジニアの壁をなくす目的に対して、Notion を「見る・動かす」場、HOKUSAI Web Dashboard を「管理する」場として明確に分離するのが、最もリスクが低く効果が大きい設計です。**

レビュアからの no-objection を得たうえで Phase A から着手することを推奨します。
