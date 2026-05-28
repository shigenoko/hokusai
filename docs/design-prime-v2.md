# Design Discussion: Prime v2 (引用つき合成 + gap analysis)

**作成日**: 2026-05-28
**ステータス**: Draft / 設計議論段階（実装未着手）
**位置付け**: [docs/roadmap-gbrain-inspirations.md](roadmap-gbrain-inspirations.md) §P0 Prime v2 を、実装可能な粒度の設計選択肢にブレークダウンする議論ドキュメント。具体的な API / DB スキーマ / CLI フラグの確定はこの議論を経て次フェーズで行う。

## 1. 目的

GBrain 調査からの最大の学びは「検索結果を並べるだけではなく、Agent が次に使える答えへ合成する」点。HOKUSAI の現 `hokusai prime` は **Notion DB の active context をフィルタしてフォーマット出力する** ところで止まっており、以下を満たさない:

1. **検索**: 過去 workflow / Review Issue / Project Memory を `--query` で串刺し検索できない
2. **合成**: 取得結果を「要点 / 引用元 / 未確定情報 / phase 注意点」のように再構成しない
3. **追跡可能性**: 出力中の事実が **どの workflow / phase / PR / Notion page / file path から来たか** を明示しない
4. **能動的な gap analysis**: 「この workflow の Phase 4 に進む前に判断材料が不足している」を検出する仕組みがない

これらを **HOKUSAI に閉じた範囲で**（外部 RAG スタックを足さずに）段階導入するのが Prime v2 の射程。

## 2. 現状の Prime（v1 相当）

### 2.1 実装位置

| 要素 | 場所 |
|---|---|
| CLI handler | `hokusai/cli_main.py:1484` `_handle_prime` |
| Renderer | `hokusai/integrations/notion_dashboard/prime_renderer.py` (412 行) |
| 出力フォーマット | Markdown / JSON |
| データソース | Notion Project Memory / Work Items / Review Issues / Workflow Gates + handover_note |
| 検索能力 | active filter のみ（query 検索なし） |
| 引用 | Notion page ID は含むが、出力本文中で参照しない |

### 2.2 既存 SQLite テーブル

`hokusai/persistence/sqlite_store.py` 現状:

- `workflows` — workflow メタ
- `audit_logs` — LLM Gateway 経由の決定ログ（v0.5.1〜実用）
- `notion_sync_outbox` / `notion_sync_errors` — Notion 同期 outbox
- `figma_sync_*` / `miro_sync_*` / `design_writeback_idempotency` / `figma_file_cache` / `miro_board_cache`

**FTS5 / 全文検索系のテーブルは現時点で存在しない**。`grep -rn "FTS\|fts5\|MATCH" hokusai/persistence/` ヒット 0。

### 2.3 v1 が満たしている機能（壊さないこと）

- 空状態でも graceful（132 chars Markdown / 168 bytes JSON）
- `--output json` で stable schema を返す
- `_collect_handover_notes` による Supersedes 遡及

Prime v2 は **v1 の出力契約を破壊しない方向**で additive に設計する。

## 3. 機能スコープ（Prime v2）

### 3.1 必須機能（P0）

1. **`hokusai prime <workflow-id> --query "..."`**: 既存 active context に加えて、query で串刺し検索した上位 N 件を引用つきで返す
2. **`hokusai recall "..."`**: workflow を指定せず、現在の profile 全体から query で検索する standalone コマンド
3. **引用 (citation)**: 各事実に `source` フィールドを付ける（workflow_id / phase / PR URL / Notion page ID / file path のいずれか）
4. **gap analysis**: 「次の phase に進むのに不足している情報の種類」を列挙するセクションを追加

### 3.2 任意機能（P1 / 議論余地あり）

- **`--include-similar`**: 起点 workflow に似た過去 workflow を類似度上位で混ぜ込む
- **phase-specific 出力**: `--for-phase 4` 等で、phase に応じた注入セットに絞る（Project-memory-first の前段）
- **`--max-tokens N`**: 出力上限を tokens 単位で抑える

### 3.3 やらないこと（明示的に除外）

- pgvector / embeddings / reranker の導入（roadmap §P0 末尾通り、初期実装では不要）
- LLM 呼び出し（要約 / 合成は決定的ロジック + テンプレートで行う、Phase 2 enforcement 経路には乗せない）
- GBrain 自体への依存（将来の外部 backend 連携は P2 以降）

## 4. 検索バックエンド設計

### 4.1 選択肢比較

| 案 | 実装コスト | 精度 | 運用負荷 | 既存配線との整合 |
|---|---|---|---|---|
| **A. SQLite FTS5 + BM25** | 小 (SQLite 標準モジュール) | 中（日本語は ICU トークナイザ要） | 小 | ◎（既存 store と同 DB） |
| **B. LIKE / GLOB 線形検索** | 極小 | 低 | 極小 | ○ |
| **C. 外部 RAG (pgvector 等)** | 大 | 高 | 大 | ×（依存追加） |
| **D. Notion search API** | 小 | 中（Notion 側のインデックス精度依存） | 中（rate limit） | ○（既存 client 流用） |

**推奨: A (SQLite FTS5)**

理由:
- `workflow.db` に閉じた構造で、profile 分離も既存ロジックがそのまま使える
- 日本語精度が問題になったら trigram tokenizer / `unicode61` `remove_diacritics` で調整可能
- Notion 側のデータを定期的に SQLite にミラーする小バッチが必要だが、`notion_sync_outbox` の逆経路として実装できる

### 4.2 FTS5 を選んだ場合の追加テーブル案

```sql
CREATE VIRTUAL TABLE prime_index USING fts5(
    workflow_id UNINDEXED,
    source_type,        -- 'memory' | 'work_item' | 'review_issue' | 'gate' | 'pr' | 'handover_note'
    source_id UNINDEXED,
    phase UNINDEXED,    -- 関連 phase（あれば）
    title,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE prime_index_meta (
    workflow_id TEXT,
    source_type TEXT,
    source_id TEXT,
    notion_page_id TEXT,
    pr_url TEXT,
    file_path TEXT,
    updated_at TIMESTAMP,
    PRIMARY KEY (workflow_id, source_type, source_id)
);
```

**議論余地**:
- FTS5 virtual table と meta table を分ける構成 vs 一体化
- 同期戦略: Notion 同期成功時に prime_index も即時更新するか、別 worker で eventual consistent にするか
- `tokenize='unicode61'` で日本語が割れる場合の対処（trigram / ICU）

## 5. 引用 (Citation) のデータモデル

### 5.1 出力フォーマット案 (Markdown)

```markdown
## 要点

- 過去 workflow `wf-abc123` で同種の Notion outbox 404 を観察済み。原因は DB share 未完了 [^1]
- Phase 4 plan で `dispatcher.retry_pending()` 経路の単体テストが追加されていない [^2]

## 引用元

[^1]: `wf-abc123` / phase=4 / source=review_issue / notion_page_id=`...`
[^2]: `wf-def456` / phase=5 / source=pr / pr_url=`https://github.com/.../pull/119`

## 未確定 / 不足情報

- Workflows DB share の自動修復経路は無く、user 手作業のまま（[docs/notion-dashboard-operation-guide.md §2.2.1](notion-dashboard-operation-guide.md#221-...)）
- Phase 6 の verification command が `claude-workflow.yaml` 未定義
```

### 5.2 JSON フォーマット案

```json
{
  "workflow_id": "wf-current",
  "summary": [
    {
      "text": "過去 workflow wf-abc123 で同種の Notion outbox 404 を観察済み",
      "citations": [
        {
          "source_type": "review_issue",
          "workflow_id": "wf-abc123",
          "phase": 4,
          "notion_page_id": "..."
        }
      ]
    }
  ],
  "gaps": [
    {
      "kind": "missing_verification_command",
      "phase": 6,
      "detail": "claude-workflow.yaml に verification_commands セクション未定義"
    }
  ]
}
```

### 5.3 議論余地

- 引用元の **何を一意キーとするか**: `(workflow_id, source_type, source_id)` の三つ組で十分か / `notion_page_id` を別系統で持つか
- 引用元の **どこまで表示するか**: PR URL や Notion page ID をそのまま貼ると prime の token 量が膨らむ。短縮表記が必要か
- 引用が **複数の source を指すケース**（同じ事実が複数 workflow で観察）の正規化

## 6. Gap Analysis のロジック

### 6.1 決定的に検出できる gap（LLM 不要）

| gap 種別 | 検出条件 | データソース |
|---|---|---|
| `missing_verification_command` | `claude-workflow.yaml` に `verification_commands` 未定義 / 空 | config |
| `unresolved_review_issue_open` | 起点 workflow に紐づく Review Issue で `Status=Open` | Notion |
| `pending_gate_blocking` | Workflow Gates DB に `Status=Pending` で起点 workflow に紐付くもの | Notion |
| `notion_outbox_pending` | `notion_sync_outbox` に起点 workflow の pending 行あり | SQLite |
| `phase4_plan_missing` | current_phase ≥ 4 だが plan 出力が空 | workflow state |
| `audit_log_silence` | LLM Gateway 有効なのに `audit_logs` 行 0 = interceptor 経路無効 | SQLite |
| `supersedes_chain_broken` | Supersedes リレーション参照先の workflow が `notion_sync_errors` に flush 済み | SQLite |

### 6.2 LLM が必要な gap（除外）

- 「設計上の矛盾」「意図の不一致」等は決定的に検出できない → Prime v2 の射程外。Phase 2/3 のレビュー prompt 側の責務

### 6.3 議論余地

- gap の **threshold**: 例えば `audit_log_silence` で何件以下を silent と判定するか
- gap の **優先度ラベル**: P0/P1 のような重み付けを出力に含めるか
- 「gap が無い」状態を **明示的に表示するか沈黙するか**

## 7. CLI 設計

### 7.1 既存 v1 との互換

```
hokusai prime <workflow-id>                # v1 と完全互換（active context のみ）
hokusai prime <workflow-id> --output json  # v1 と完全互換
```

### 7.2 v2 追加フラグ

```
hokusai prime <workflow-id> --query "Notion outbox"     # query 検索 + 引用付き要点
hokusai prime <workflow-id> --include-gaps              # gap analysis セクション追加
hokusai prime <workflow-id> --for-phase 4               # phase 4 で必要な context に絞る
hokusai prime <workflow-id> --max-tokens 2000           # 出力上限
hokusai prime <workflow-id> --include-similar           # 類似 workflow を混ぜる（P1）

hokusai recall "..."                                     # standalone search（workflow_id 不要）
hokusai recall "..." --profile <name>                    # profile 指定
hokusai recall "..." --source-type review_issue,pr       # source 絞り込み
```

### 7.3 議論余地

- `--include-gaps` を **default on にするか off にするか**: gap が無いときは静かなので default on でもよさそうだが、token 量増加リスク
- `--for-phase` の **意味論**: 「その phase の prompt にそのまま注入してよい形」か「その phase に関連する事実」か
- `recall` を **prime のサブコマンドに統合するか別コマンドにするか**: GBrain は `think` / `search` を別にしているので、HOKUSAI も別が自然

## 8. 実装ステップ分割

### 8.1 最小 MVP (1 PR スコープ目安)

| Step | スコープ | 検証方法 |
|---|---|---|
| **MVP-1** | `prime_index` FTS5 テーブル追加 + 既存 active context を index に書き込む | `sqlite3 ... "SELECT count(*) FROM prime_index"` |
| **MVP-2** | `hokusai prime --query "..."` を実装（active context 内のみ検索） | unit test + 軽量 e2e |
| **MVP-3** | 引用フォーマット (Markdown / JSON) を v1 互換のまま追加 | snapshot test |
| **MVP-4** | gap analysis を §6.1 表のうち 3 種 (`unresolved_review_issue_open` / `notion_outbox_pending` / `audit_log_silence`) で実装 | dogfooding |

MVP-1〜4 は **1 PR ずつ** を想定。各 PR は v1 出力を壊さないこと（Phase 2 enforcement の v0.5.1 サイクルと同じく、出力契約は additive に）。

### 8.2 拡張ステップ (MVP 完了後)

- 過去 workflow を `prime_index` に流し込むバッチ
- `hokusai recall` standalone コマンド
- `--for-phase` の phase-specific filter
- `--include-similar` の類似度算出 (cosine on token bag / Jaccard 等)
- Notion → SQLite ミラー逆経路（`notion_sync_outbox` 同様の outbox-pattern で安全に）

### 8.3 議論余地

- MVP-1 と MVP-2 を **同一 PR にまとめるか分けるか**: dogfooding-findings F1-F4 の進め方を踏襲するなら 1 PR 1 トピックが安全
- 既存 Notion データの **初回 backfill** をどう走らせるか（CLI 経由 or 自動 / 失敗時の再実行）

## 9. 既存実装との関係

### 9.1 影響を受けるモジュール

| 既存 | 影響 |
|---|---|
| `prime_renderer.py` (412 行) | `_render_*` を query 結果 / gap section にも拡張。v1 ヘルパは保持 |
| `cli_main.py:1484 _handle_prime` | フラグ追加。v1 パスは早期 return で残す |
| `sqlite_store.py` | `prime_index` / `prime_index_meta` 追加。`__init__` の DDL に追記 |
| `integrations/notion_dashboard/dispatcher.py` | Notion 同期成功時に `prime_index` 更新フックを足す（同期失敗を prime_index 失敗にしない設計が必須） |

### 9.2 audit_logs との関係

`hokusai prime` 自体は LLM Gateway 経路を通らないので `audit_logs` には記録されない（仕様）。ただし **Prime v2 が将来 LLM 呼び出しを伴う合成を追加した場合**は LLM Gateway 経路に乗せること（Phase 2 enforcement との一貫性）。

### 9.3 profile 分離

`hokusai recall` は profile 全体を検索するが、**他 profile への染み出しは禁止**。既存の `--profile` 解決と同じ経路（`create_config_from_env_and_file(profile_name=...)` 経由）を使う。

## 10. 未解決の設計問題（議論募集）

1. **MVP-1 の同期戦略**: Notion 同期成功時に `prime_index` を即時更新するべきか、別 worker で eventual にするべきか
2. **日本語トークナイズ**: FTS5 `unicode61` で「Phase 2 enforcement の dogfooding」のような複合句が割れる挙動を許容するか、trigram に切り替えるか
3. **gap analysis の優先度**: 出力に P0/P1 ラベルを含めるか、種別表記だけにするか
4. **`hokusai recall` の検索範囲**: profile 全体（active + closed workflow）を default にするか、active のみ default にするか
5. **`--for-phase` の意味論**: 「prompt 注入用最小セット」か「phase 関連事実の全列挙」か
6. **過去 workflow backfill の実行タイミング**: MVP-1 で全件 backfill する設計か、incremental に増やす設計か
7. **`--include-similar` の類似度関数**: BM25 スコア再利用か、別途 token bag Jaccard を計算するか

## 11. 参考

- [docs/roadmap-gbrain-inspirations.md](roadmap-gbrain-inspirations.md) §P0 Prime v2
- [docs/dogfooding-findings.md](dogfooding-findings.md) §2 観察ポイント 2: `hokusai prime` の現状の限界
- `hokusai/integrations/notion_dashboard/prime_renderer.py` (v1 renderer 実装)
- `hokusai/cli_main.py:1484` (v1 CLI handler)
- SQLite FTS5: https://www.sqlite.org/fts5.html
