# HOKUSAI Figma / Miro 書き戻し（Phase E）運用ガイド

**対象バージョン**: v0.4.0 以降

**対象読者**: HOKUSAI 運用担当エンジニア、PM、デザイナー

**前提**:
- Phase A〜D + F（Figma/Miro 読み取り MVP）は v0.3.0 までに実装済み
- 詳細設計: `docs/hokusai-figma-miro-writeback-implementation-plan.md`

---

## 1. 概要

Phase E は HOKUSAI から Figma / Miro **へ** 進捗を返す書き戻し機能を提供する。

- Phase 8a（PR / MR 作成）完了時に、対象 frame / board に自動コメント / カード投稿
- 投稿失敗は SQLite outbox に蓄積され、Operations Console から手動再送可能
- 同一 payload で再送しても重複作成されない（冪等性）
- v0.3.0 profile 機能と完全に整合（profile 別 outbox 分離）

## 2. 有効化手順

### 2.1. config YAML への設定追加

profile の config（例: `~/work/hokusai-configs/a-company.yaml`）に `figma.writeback` / `miro.writeback` 節を追加する:

```yaml
figma:
  enabled: true
  api_token_env: A_HOKUSAI_FIGMA_API_TOKEN
  writeback:
    enabled: true              # 書き戻し ON
    on_failure: warn           # warn | block | skip（既定: warn）

miro:
  enabled: true
  api_token_env: A_HOKUSAI_MIRO_API_TOKEN
  writeback:
    enabled: true
    on_failure: warn
```

`writeback` 節が無い既存 config はそのまま動作する（無効扱い）。

### 2.2. on_failure ポリシー

| 値 | 動作 |
|---|---|
| `warn`（既定） | 投稿失敗時 outbox に積む + warning ログ + workflow 継続 |
| `block` | outbox に積む + workflow を Waiting for Human に遷移（重要案件向け） |
| `skip` | outbox にも積まない + warning のみ（明示的に書き戻し不要） |

### 2.3. token 設定

Figma / Miro の API token を環境変数で設定する:

```bash
export A_HOKUSAI_FIGMA_API_TOKEN="figd_xxx"
export A_HOKUSAI_MIRO_API_TOKEN="miro_xxx"
```

writeback enabled でも token が未設定なら silent に skip する（既存運用を壊さない）。

## 3. 投稿内容

### 3.1. Figma frame コメント（pin）

主 frame に **pin コメント** として投稿:

```
✅ Phase 8a 完了 / MR: <PR/MR URL> / commit: <commit_sha_short>
```

- pin 位置: frame 左上（`client_meta.node_offset = {x:0, y:0}`）
- 文面はハードコード、日本語のみ（i18n は v0.4.1 以降）

### 3.2. Miro card

主 frame の **右側 50px** に card を配置:

| フィールド | 値 |
|---|---|
| title | `✅ Phase 8a 完了` |
| description | `MR: <url>\ncommit: <sha>` |
| style.fillColor | `#4FCC8B`（薄緑） |

## 4. 主 frame の決定（Phase 3）

`state.figma_target_node_id` / `state.miro_board_id` を優先採用。なければ design_context の screens 先頭。

state に保存される 6 フィールド:

- `primary_figma_file_key`
- `primary_figma_frame_id`（参照用、Console 表示）
- `primary_figma_node_id`（API client_meta.node_id）
- `primary_figma_node_offset`（既定 `{"x":0,"y":0}`）
- `primary_miro_frame_id`
- `primary_miro_board_id`

未設定なら該当 target を silent に skip。

## 5. Operations Console

### 5.1. API 一覧（v0.4.0）

GET:

| パス | 動作 |
|---|---|
| `/api/figma/outbox?limit=100&profile=<name>` | Figma 投稿失敗待ち一覧 |
| `/api/figma/errors?limit=100&profile=<name>` | 5 回失敗で諦め済の一覧 |
| `/api/miro/outbox` / `errors` | Miro 版 |

POST:

| パス | body | 動作 |
|---|---|---|
| `/api/figma/retry-pending` | `{}` | pending 全件再送 |
| `/api/figma/retry-pending` | `{"id": <int>}` | 個別 id を再送 |
| `/api/figma/retry-pending` | `{"force": true}` | errors にあっても再試行 |
| `/api/figma/move-to-errors` | `{"id": <int>}` | 強制 errors 移動 |
| `/api/miro/*` | 同上 | Miro 版 |

### 5.2. 再送のフロー

```
作成された outbox 行
    ↓ (Operations Console で再送ボタンクリック)
attempt_count +1
    ↓ (再 API 呼び出し)
成功 → idempotency に記録、outbox から削除
失敗 → outbox 維持、last_error 更新
    ↓ (attempt_count == 5)
errors テーブルへ自動移動（自動経路では再投稿しない）
    ↓ (運用者が必要なら)
/api/figma/retry-pending body {"id": X, "force": true} で errors を無視して再試行
```

### 5.3. UI 拡張（v0.4.1 以降）

計画書 §10.1 のパネル統合（HTML 表示）は v0.4.1 以降の対応。現状は API 経由で外部ツール（curl / jq）から確認:

```bash
# pending 件数確認
curl http://localhost:8765/api/figma/outbox | jq '.count'

# errors を見て手動再送
curl http://localhost:8765/api/figma/errors | jq '.items[] | {id, idempotency_key, error_message}'
curl -X POST -d '{"id":1,"force":true}' http://localhost:8765/api/figma/retry-pending
```

## 6. 冪等性

冪等キー: `{workflow_id}:{event_type}:{resource}:{revision}`

例: `wf_20260513_001:phase8a_completed:figma_node-abc:a1b2c3d4`

3 段階チェック（計画書 §9.2.2）:

1. `design_writeback_idempotency` にヒット → 既に投稿済み、skip
2. `figma_sync_outbox` / `miro_sync_outbox` にヒット → pending、skip
3. `figma_sync_errors` / `miro_sync_errors` にヒット → 諦め済、`force=true` でない限り skip

これにより:
- Phase 8a を再実行しても重複投稿されない
- workflow resume / プロセス再起動も安全
- 同一 commit に対する複数回 dispatch も冪等

## 7. cleanup（30 日経過）

```bash
hokusai cleanup --stale
```

実行時:
- worktree（既存）
- Notion outbox（既存）
- **figma_sync_errors / miro_sync_errors（30 日経過）**（Phase E 追加）
- **design_writeback_idempotency（30 日経過）**（Phase E 追加）

outbox 本体は TTL なし（成功時に即削除、5 回失敗で errors 移動）。

## 8. profile 別の独立性（v0.3.0 整合）

profile ごとに `data_dir` が分離されているため、outbox / errors / idempotency も自然に分離される:

```
~/.hokusai/profiles/a-company/workflow.db
  ├── figma_sync_outbox  (company-a のみ)
  ├── miro_sync_outbox
  └── design_writeback_idempotency

~/.hokusai/profiles/b-company/workflow.db
  ├── figma_sync_outbox  (company-b のみ)
  └── ...
```

加えて全テーブルに `profile_name` 列を持つため、`/api/figma/outbox?profile=company-a` のような profile フィルタが効く。

## 9. トラブルシューティング

### 9.1. 投稿されない

確認手順:
1. config の `writeback.enabled: true` が設定されているか
2. token 環境変数が設定されているか（`hokusai profile show <name>` で env var 名を確認）
3. state に `primary_figma_*` / `primary_miro_*` が設定されているか
4. Operations Console の outbox を見て、enqueue されているなら API エラーを確認

### 9.2. 403 Forbidden（権限不足）

- Figma: PAT が target file に書き込み権限を持っているか
- Miro: app token が board に access できるか

errors テーブルに溜まっている場合は token を修正後、`force=true` で再送。

### 9.3. 重複投稿

設計上、idempotency_key で抑止されるため通常は起きない。発生した場合:
- 異なる commit_sha / revision で dispatch していないか確認
- API 成功直後にクラッシュしたケースは idempotency 記録漏れの可能性あり（Operations Console から手動で重複削除）

### 9.4. workflow が止まる

`on_failure: block` の場合、書き戻し失敗で workflow が Waiting for Human に遷移する。Operations Console から outbox を確認し、再送 or skip 判断。

## 10. 関連ドキュメント

- `docs/hokusai-figma-miro-writeback-implementation-plan.md` - 詳細設計
- `docs/hokusai-figma-miro-integration-implementation-plan.md` - Phase A〜H 全体計画
- `docs/figma-miro-integration-operation-guide.md` - MVP（読み取り）運用ガイド
- `docs/hokusai-profile-parallel-execution-implementation-plan.md` - v0.3.0 profile 機能
