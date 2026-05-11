# HOKUSAI Figma / Miro 連携 運用ガイド

このドキュメントは Figma / Miro 連携の MVP（Phase A〜D + F）に関する運用手順をまとめたものです。

実装計画書: [hokusai-figma-miro-integration-implementation-plan.md](./hokusai-figma-miro-integration-implementation-plan.md)

---

## 1. 連携の前提

| 項目 | 設定 |
|---|---|
| 連携モード | read-only（取得のみ） |
| 書き戻し | Phase E（任意拡張）で実装、MVP には含まれない |
| 必須サービス | Notion（タスク管理）、Figma または Miro のいずれか |
| 推奨サービス | Slack（waiting_for_human / pr_created 通知に design URL を載せる） |

HOKUSAI が Figma / Miro の中身を書き換えることは MVP では一切ありません。

---

## 2. セットアップ

### 2.1. Token の発行

| サービス | Token 発行ページ | 環境変数 |
|---|---|---|
| Figma | [Personal access tokens](https://www.figma.com/developers/api#access-tokens) | `HOKUSAI_FIGMA_API_TOKEN` |
| Miro | [REST API Reference](https://developers.miro.com/reference/api-reference) | `HOKUSAI_MIRO_API_TOKEN` |
| Miro Team ID（任意） | ワークスペース URL から確認 | `HOKUSAI_MIRO_TEAM_ID` |

```bash
export HOKUSAI_FIGMA_API_TOKEN="figd_xxx..."
export HOKUSAI_MIRO_API_TOKEN="..."
export HOKUSAI_MIRO_TEAM_ID="..."  # 任意
```

### 2.2. `claude-workflow.yaml` の最小設定

```yaml
figma:
  enabled: true
  on_failure: warn      # warn | block | skip

miro:
  enabled: true
  on_failure: warn
```

完全な設定例は実装計画書 §6.1 を参照。

### 2.3. Notion Workflows DB に追加するプロパティ（任意）

連携情報を Notion ダッシュボードに表示したい場合、Workflows DB に以下プロパティを手動追加してください（プロパティが無くても連携自体は動作します）。

| プロパティ名 | 型 |
|---|---|
| Miro URL | URL |
| Figma URL | URL |
| Design Status | Select（`ok` / `partial` / `failed` / `skipped` / `not_configured` / `no_url`） |
| Design Review Required | Checkbox |
| Design Review Result | Select（`pending` / `approved` / `changes_requested`） |
| Miro Last Synced At | Date |
| Figma Last Synced At | Date |
| Design Notes | Text |

### 2.4. 接続確認

```bash
# 接続状態 API（Operations Console と同じ判定ロジック）
curl -s http://localhost:8765/api/connections | jq '.services[] | select(.id == "figma" or .id == "miro")'
```

期待値: `status: connected`、または `status: disabled`（`enabled=false` のまま運用する場合）。

---

## 3. タスクへの URL 紐づけ

Notion タスク本文に Figma / Miro の URL を貼り付けてください。HOKUSAI は Phase 2 開始時に下記パターンを抽出します:

- `https://www.figma.com/file/<FILE_KEY>/...`
- `https://www.figma.com/design/<FILE_KEY>/...`
- `https://www.figma.com/proto/<FILE_KEY>/...`
- `https://miro.com/app/board/<BOARD_ID>/...`
- `https://miro.com/board/<BOARD_ID>/...`

複数 URL がある場合は **最初に見つかった URL** が使われます（MVP）。

---

## 4. ワークフロー上での挙動

| Phase | 挙動 |
|---|---|
| Phase 2: Research | URL 抽出 → Figma / Miro から要約取得 → state に保存・調査プロンプトに差し込み |
| Phase 3: Design Check | Notion 要件 / Miro 業務フロー / Figma UI の整合性確認をプロンプトに追加 |
| Phase 4: Plan | 実装計画書に Miro / Figma 参照対象を含めるよう指示（プロンプト変更なし） |
| Phase 5: Implement | 実装プロンプトに Figma の画面構成・テキスト・コンポーネントを差し込み |
| Phase 7: Review | レビューチェックリストに Figma 確認観点を追加 |
| Phase 8: MR / PR 作成 | MR 本文に Miro / Figma リンク、未解決コメントがあれば警告を追記 |
| Phase 10: Record | Notion タスクページに連携結果のサマリを追記 |

---

## 5. 失敗時のポリシー（`on_failure`）

| 値 | 挙動 | 推奨ケース |
|---|---|---|
| `warn`（既定） | 警告を `state.design_sync_errors` に記録し、ワークフロー継続 | 通常運用 |
| `block` | Waiting for Human へ遷移し、人間の確認を待つ | デザイン確認が必須なプロジェクト |
| `skip` | 取得をスキップし、design context なしで続行 | デザイン無依存なバックエンドタスク |

`figma` と `miro` で個別に設定できます。

---

## 6. キャッシュ

- TTL: 既定 30 分（`cache_ttl_seconds`）
- 保存先: `~/.hokusai/workflow.db` の `figma_file_cache` / `miro_board_cache`
- 期限切れは自動的に再取得されます。

### 強制リフレッシュ

#### 推奨: Operations Console 経由

```bash
# Figma キャッシュをクリア
curl -X POST http://localhost:8765/api/figma/refresh-cache

# Miro キャッシュをクリア
curl -X POST http://localhost:8765/api/miro/refresh-cache
```

レスポンス例:
```json
{ "success": true, "source": "figma", "deleted_rows": 12 }
```

これで `figma_file_cache` / `miro_board_cache` の全行が削除され、次回 fetch で必ず実 API へ問い合わせが行われます。`connection_status` のキャッシュも同時にクリアされます。

#### 手動クリア（緊急時）

API 経由が使えない場合は SQLite を直接操作してください:

```sql
DELETE FROM figma_file_cache;
DELETE FROM miro_board_cache;
```

---

## 7. 通知

### Slack

`pr_created` / `waiting_for_human` / `workflow_failed` イベントの通知本文末尾に、design URL と連携エラーが追記されます（state に URL がある場合のみ）。

### Notion Dashboard

Workflows DB に該当プロパティを追加していれば、`Last Updated` のタイミングで Figma / Miro の URL と連携状態が同期されます。

---

## 8. トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `connection.figma.not_authenticated` | `HOKUSAI_FIGMA_API_TOKEN` 未設定 | 環境変数を設定して shell rc に永続化 |
| `connection.figma.disabled` | `figma.enabled: false` | YAML を `true` に変更 |
| `figma url parse failed` | URL 形式が想定外 | `/file/<key>/...` か `/design/<key>/...` か確認 |
| 連携が常に `failed` | token 権限不足、ファイル非公開 | Figma / Miro 側の権限を確認、または `on_failure: skip` を一時設定 |
| キャッシュが古い | TTL 内で再取得されない | TTL を短くする、または cache テーブルをクリア |
| Workflows DB に URL が出ない | DB に該当プロパティが無い | §2.3 を参照して手動追加 |

---

## 9. 制約事項（MVP）

- Figma / Miro への書き戻し（コメント / カード投稿）は MVP に含まれていません（Phase E で実装予定）
- Webhook 受信によるレビューループは Phase G で実装予定
- 視覚回帰テスト（実装後の Figma 比較）は Phase H で実装予定
- 1 タスクで複数 URL を受け取った場合、最初の 1 件のみ採用します
- Figma の生レスポンスは state に保存しません（要約のみ）。詳細はキャッシュから個別に取得してください

---

## 10. 動作確認チェックリスト

実装変更後に以下を順に確認してください。

### 設定
- [ ] `HOKUSAI_FIGMA_API_TOKEN` / `HOKUSAI_MIRO_API_TOKEN` を `env` で確認
- [ ] `claude-workflow.yaml` の `figma.enabled` / `miro.enabled` を `true`
- [ ] `curl /api/connections` で `figma` / `miro` が `connected`

### Phase 2 / 3 / 5
- [ ] Notion タスク本文に Figma URL を貼ったテストタスクで HOKUSAI を起動
- [ ] Phase 2 実行後、Notion 子ページに「外部デザイン・業務フロー情報」セクションが含まれる
- [ ] Phase 3 / 5 のプロンプトログに `### Figma UI 仕様` が出る

### Phase 8 / 10
- [ ] 作成された MR / PR 本文に「## デザイン / 業務フロー参照」が含まれる
- [ ] Phase 10 完了後、Notion タスクに「### デザイン / 業務フロー連携」が追記される

### 失敗時
- [ ] `on_failure: warn` で token を一時的に外す → ワークフローが継続し、`state.design_sync_errors` に記録される
- [ ] `on_failure: block` で同操作 → Waiting for Human に遷移する

### キャッシュ
- [ ] 同一タスクで 2 回連続実行し、2 回目で Figma / Miro API への HTTP リクエストが 0 件であること
  - `integrations.design.cache` ロガーを DEBUG レベルで有効化すると、`figma cache hit (cache_key=...)` / `miro cache hit (cache_key=...)` のログが出力される
  - 例: `LOG_LEVEL=DEBUG hokusai run ...` または `logging.getLogger("integrations.design.cache").setLevel(logging.DEBUG)` を設定

---

## 11. 関連ドキュメント

- 実装計画書: `docs/hokusai-figma-miro-integration-implementation-plan.md`
- 要件定義: `docs/hokusai-figma-miro-integration-requirements.md`
- Notion Dashboard 運用: `docs/notion-dashboard-operation-guide.md`
- Slack 通知: `docs/slack-notification-operation-guide.md`
