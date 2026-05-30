# Changelog

HOKUSAI のすべての特筆すべき変更をこのファイルに記録する。

書式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準拠し、
バージョニングは [Semantic Versioning](https://semver.org/lang/ja/) に従う。

開発状況は `Development Status :: 3 - Alpha`（v0.x はマイナーバージョン更新で
破壊的変更を含む可能性あり）。

---

## [Unreleased]

### Added

- **Step 5 (Local Workgraph Edges) 第 3 スライス: review issue / work item のローカル永続化**: 従来 `pending_review_issues` / `pending_work_items` は Notion dispatch 後に drain layer が `[]` へ clear するため transient だった（#146 Copilot 指摘の根本原因）。これを解消し、recurring review issue 検出・durable な workgraph edge・`resolved_by` edge 等を後続スライスで構築できる土台を作る。SQLite に durable table `review_issues`（`dedupe_key TEXT NOT NULL PRIMARY KEY` + source/rule/file/message/repository/severity/status）と `work_items`（`dedupe_key TEXT NOT NULL PRIMARY KEY` + title/phase/status/description）を追加し（SQLite は非 INTEGER PRIMARY KEY に NOT NULL を自動付与しないため明示。NULL key 行の多重挿入で idempotency が壊れるのを防ぐ）、`SQLiteStore` に `upsert_review_issue()` / `list_review_issues()` / `upsert_work_item()` / `list_work_items()`（dedupe_key 冪等 upsert・workflow_id/status フィルタ・`workflow_id`/`status` index）を実装。`_WORKFLOW_DEPENDENT_TABLES` にも追加し completed workflow の cascade-delete 対象に。永続化フックは `hokusai/local_persistence.py` の純関数 `persist_review_issue_payloads()` / `persist_work_item_payloads()`（payload → store の best-effort マッピング）で、`workflow.py` の両 drain が **clear 前に** 呼ぶ。dispatcher の契約を mirror する: review issue は `source` + `message` の両方が無い malformed payload を skip（dispatch されない行を durable table に残さない）、`dedupe_key` は明示があれば優先しなければ dispatch (`_prepare_*_dispatch`) と**同じ fallback**で算出（明示 key を無視して再計算すると Notion/outbox identity と乖離するのを防ぐ）。Notion 配線/dispatch の成否に関わらずローカルに残り、1 件の永続化失敗が drain 本体を止めない。upsert は `COALESCE(excluded.x, table.x)` で更新するため、Phase 5 の lifecycle イベント（`status_change` の後に optional フィールドを省略した `lease_release` 等）が来ても既存の status / 値を NULL で消さない。recurring 検出・新 edge 種別はこの durable データを使う後続スライス。回帰防止テスト 15 件 (`tests/test_local_persistence.py`: upsert/list/冪等/status 更新/COALESCE 省略保持 ×2/フィルタ/limit reject/dedupe_key NOT NULL/GC cascade/persist ヘルパ・review fallback dedupe_key・malformed skip・work item 明示 key 優先)。設計: [docs/roadmap-gbrain-inspirations.md §P1 / Step 5](docs/roadmap-gbrain-inspirations.md)。

- **Step 5 (Local Workgraph Edges) 第 2 スライス: `graph status` 集約ビュー + work_item edge**: 第 1 スライスの edge table を活かした運用状態の可視化と、抽出 edge 種別の追加。決定的 extractor (`hokusai/workgraph_edges.py::extract_edges_from_state`) に 3 種目の edge `workflow -> has_work_item -> work_item` を追加 (`state["pending_work_items"][].title` から抽出、phase / status を metadata に。work item の node identity は title = Phase 4 plan と Phase 5 implement が同一 identity として扱う挙動と整合)。`SQLiteStore.count_workgraph_edges_by_type()` を追加 (edge_type index を使った GROUP BY で全件 scan 回避)。CLI `hokusai graph status [--workflow-id] [--output text|json]` を追加し、`workgraph_edges` を集約して **edge 種別ごとの本数 / supersedes チェーン / has_pr edge の github_status 別件数** を表示する (live API 呼び出しなし・決定的)。json は stable schema (`{workflow_id, total_edges, edge_type_counts, supersedes_chains:[{from,to}], supersedes_chains_truncated, pr_status_counts}`)。件数系 (total / edge_type_counts / pr_status_counts) は SQL 集約で常に正確 (`pr_status_counts` は `SQLiteStore.count_workgraph_pr_status()` の `json_extract` + GROUP BY で cap なし)、一覧の `supersedes_chains` のみ表示 cap (1000) を持ち超過時は `supersedes_chains_truncated` で明示する。**既知の制約**: `has_work_item` edge は `state["pending_work_items"]` 由来だが、これは Notion dispatch 後に drain layer が `[]` に clear・永続化するため transient（drain 済み work item は edge 化されず、再 build で失われ得る）。work item の完全な履歴には durable な永続化を前提とする後続スライスが必要。recurring review issue 検出も review issue のローカル永続化が前提のため後続。回帰防止テスト 6 件追加 (`tests/test_workgraph_edges.py`: has_work_item 抽出 / edge_type 集計 + workflow 絞り / pr_status SQL 集約 / status 集約ロジック + truncated フラグ / `graph status --output json`)。設計: [docs/roadmap-gbrain-inspirations.md §P1 / Step 5](docs/roadmap-gbrain-inspirations.md)。

---

## [0.7.0] - 2026-05-30

GBrain 調査から起こした **v0.6 ロードマップ** の Step 2 / Step 3 / Step 5 と Prime v2 の仕上げを束ねた minor リリース。蓄積した運用ヘルス可視化・Operation Registry・Local Workgraph Edges を一括で提供する。設計議論は [docs/roadmap-gbrain-inspirations.md](docs/roadmap-gbrain-inspirations.md)。

このリリースの軸は **「既に蓄積している workflow 履歴・review issue・gate・運用状態を、SQLite-backed で決定的に検査・可視化する」** こと。新機能はすべて additive で、新フラグ / 新サブコマンド未指定時の既存挙動は不変（`profile doctor` の text 出力は完全後方互換、新コマンド `operations` / `graph` の追加のみ）。

主な内容:

- **Step 2 (Doctor/Status 一画面化)** — `hokusai profile doctor <name> --deep` に runtime 運用ヘルス検査 (Notion sync outbox の滞留 / 運用ギャップを SQLite から集約)、`--output json` で stable schema 化、Operations Console と共通の純関数 `compute_runtime_health()` 化。CLI / Console / 機械処理で検出ロジックを一本化 (#140 / #141 / #142)。
- **Step 3 (Operation Registry) 第 1 スライス** — operation 名・説明・scope・入力 schema・handler を 1 箇所へ集約する read-only registry (`hokusai operations list / run`)。CLI / Dashboard / 将来の MCP・HTTP admin が同じ handler を呼ぶ単一経路。read-only op は副作用なしの `ReadOnlyStore` (`mode=ro` 接続) で実行 (#143)。
- **Step 5 (Local Workgraph Edges) 第 1 スライス** — SQLite `workgraph_edges` テーブル + LLM 不要・決定的な extractor (`workflow -> supersedes -> workflow` / `workflow -> has_pr -> pull_request`)。CLI `hokusai graph build / list`。`build` は単一トランザクションの atomic replace + `--dry-run` 非 mutate (#144)。
- **Prime v2 MVP-5** — `hokusai prime --include-gaps` に Notion 非依存の決定的 gap 検出 2 種 (`phase4_plan_missing` / `supersedes_chain_broken`) を追加 (#139)。

### Added

- **Step 5 (Local Workgraph Edges) 第 1 スライス: SQLite edge table + 決定的 extractor**: HOKUSAI の Workgraph をローカルに graph query できる形へ寄せる土台。SQLite に `workgraph_edges` テーブル (`src_type/src_id/edge_type/dst_type/dst_id` + `workflow_id` + `metadata_json`、`UNIQUE(src_type,src_id,edge_type,dst_type,dst_id)` で冪等) を追加し、`SQLiteStore` に `upsert_workgraph_edge()` / `list_workgraph_edges()` (workflow_id / edge_type / src フィルタ + `limit<1` を ValueError reject) / `clear_workgraph_edges_for_workflow()` を実装 (`_WORKFLOW_DEPENDENT_TABLES` にも追加し completed workflow の cascade-delete 対象に)。抽出は **LLM なし・決定的** な純関数 `hokusai/workgraph_edges.py::extract_edges_from_state(state)` で、ローカル state のみから 2 種の edge を生成: `workflow -> supersedes -> workflow` (`state["supersedes_workflow_id"]`) と `workflow -> has_pr -> pull_request` (`state["pull_requests"][].url` + number/repo/status を metadata に)。CLI `hokusai graph build <workflow-id>` (state から抽出 → `replace_workgraph_edges_for_workflow()` で該当 workflow の既存 edge を**単一トランザクション**で置換。途中失敗時は rollback で旧 edge set を保持し、clear だけ走って空になる中間状態を作らない。`--dry-run` 時は SQLite を mutate せず preview のみ) と `hokusai graph list [--workflow-id] [--edge-type] [--output text|json]` を追加。Notion DB relation / review comment 由来の edge (`resolved_by` / `duplicates` / `touches_file`) や recurring review issue 検出は後続スライス。公開フィルタ (`workflow_id` / `edge_type`) と clear / workflow GC が full-scan しないよう `workgraph_edges(workflow_id)` / `(edge_type)` に index を張る。回帰防止テスト 17 件 (`tests/test_workgraph_edges.py`: extractor 6 種 + 永続化/GC cascade/index/atomic replace 10 種 + CLI `--dry-run` 非 mutate 1 種、`Operation` 同様 `Edge` も unhashable)。設計: [docs/roadmap-gbrain-inspirations.md §P1 / Step 5](docs/roadmap-gbrain-inspirations.md)。
- **Step 3 (Operation Registry) 第 1 スライス: read-only operation registry の土台**: GBrain `operations.ts` 同様に operation 名・説明・scope・入力 schema・handler を 1 箇所へ集約する `hokusai/operations.py` を追加 (`Operation` dataclass + `OperationRegistry` + `build_default_registry()` / `default_registry()` シングルトン)。第 1 スライスは **read-only operation のみ**を登録し、既存の SQLite-backed 関数を薄くラップ: `notion.outbox_status` (outbox pending / 永続 error 件数)、`runtime.health` (Step 2 共通の `compute_runtime_health()` 委譲)、`workflow.list` (`list_active_workflows()`)。CLI 入口として `hokusai operations list [--output text|json]` (name / scope / summary / input_schema を列挙) と `hokusai operations run <name> [--param KEY=VALUE]` (read-only のみ実行可、戻り値を JSON で stdout に出力) を追加。`run` は mutating scope を scope guard で reject し、未知 operation / 不正 `--param` は exit 1。CLI / Dashboard / 将来の read-only MCP・HTTP admin が同じ handler を呼ぶ単一経路を作るのが狙い (CLI handler 全体の registry 移行・MCP/HTTP 化は後続スライス)。回帰防止テスト 13 件 (`tests/test_operations.py`: registry 登録/取得/一覧/重複拒否/シングルトン、seed handler 3 種、`--param` パーサ 5 種)。設計: [docs/roadmap-gbrain-inspirations.md §P1 / Step 3](docs/roadmap-gbrain-inspirations.md)。
- **Step 2 (Doctor/Status 一画面化) 第 3 スライス: Operations Console 共通 handler 化**: runtime 運用ヘルス集約を純関数 `hokusai/health.py::compute_runtime_health(store, *, llm_gateway_enabled, workflow_id=None, state=None)` に抽出し、CLI `profile doctor --deep` と Operations Console の Notion 同期パネルの**双方が同一関数を呼ぶ**ようにした。返り値は `profile doctor --output json` の `runtime_health` キーと同一スキーマ (`{ran, outbox_pending, outbox_errors, gaps:[{kind,detail}], error}`) で、CLI / Console / 機械処理で検出ロジックを一本化。Console パネルは従来の outbox 件数表示 (「保留 N 件 / 永続失敗 N 件」) を共通 handler 経由に置換し、運用ギャップ検出時は additive に「⚠ 運用ギャップ N 件」リストを表示 (gap なしなら従来の見た目を維持)。例外は内部で握りつぶし `error` に記録する best-effort (live Notion 呼び出しなし)。回帰防止テスト 6 件 (`tests/test_health.py`: clean / outbox pending+gap / 永続 error / audit silence の gateway フラグ依存 / store 例外 best-effort / スキーマ一致)。
- **Step 2 (Doctor/Status 一画面化) 第 2 スライス: `profile doctor --output json` で stable schema 化**: `hokusai profile doctor <name> [--deep] --output json` を追加。人間向け text 出力の代わりに固定スキーマの JSON を 1 つ stdout に出す（CI / 運用監視 / 将来の Operations Console からの機械処理用）。schema: `{profile, checks: [{id, ok, detail}], runtime_health: {ran, outbox_pending, outbox_errors, gaps: [{kind, detail}], error}|null, issues: [...], healthy: bool}`。静的検査を `record(id, ok, detail)` ヘルパで構造化し、`_run_profile_deep_health()` は構造化 dict を返す形にリファクタ（text モードの出力は完全後方互換）。exit code は text/json で同一（healthy なら 0、issues ありで 1）。profile 不在時は `error` フィールド付き JSON + exit 1。次スライスで Operations Console との共通 handler 化を予定。回帰防止テスト 4 件追加 (`tests/test_cli_profiles.py`: json clean / json 静的 issue / json --deep runtime_health / json profile 不在)。
- **Step 2 (Doctor/Status 一画面化) 第 1 スライス: `profile doctor --deep` に runtime 運用ヘルス検査**: `hokusai profile doctor <name> --deep` の従来プレースホルダ (「Phase E で実装予定」) を実装に置換。profile の config を解決して SQLite を開き、Notion sync outbox の pending / 永続 error 件数と、`prime_gaps.collect_gaps()` を**共通 sink として再利用**した運用ギャップ (`notion_outbox_pending` / `audit_log_silence`、Notion 非依存) を集約表示する。検出した gap は doctor の `issues` に積まれ exit code に反映 (gap ありで exit 1)。live Notion 呼び出しは行わず、検査自体の失敗は static 検査結果を壊さず warning 行で graceful degrade (best-effort)。これにより Prime v2 の gap analysis と Doctor で検出ロジックが一本化された。次スライスで `--output json` による stable schema 化 + Operations Console との共通 handler 化を予定。回帰防止テスト 4 件 (`tests/test_cli_profiles.py`: clean / outbox pending 検出 / 永続 error のみ検出 / audit silence 検出)。
- **Prime v2 MVP-5: gap analysis 残り 2 種 (state/SQLite-backed)**: `hokusai prime <wf> --include-gaps` の決定的検出に Notion 非依存の 2 種を追加 (MVP-4 の 3 種に追加)。`phase4_plan_missing` (current_phase ≥ 5 = Phase 5 implement 以降に到達しているのに Phase 4 の `work_plan` が空 — 計画なしで実装が進んでいないかの注意喚起。Phase 4 実行中の transient を避けるため閾値を設計 docs の「≥4」から ≥5 に精緻化)、`supersedes_chain_broken` (`state["supersedes_workflow_id"]` の旧 workflow が `notion_sync_errors` に `workflow_started` 永続失敗として記録 = Supersedes リレーション / handover 遡及が途切れている疑い、`SQLiteStore.has_failed_workflow_started()` で判定)。両 detector とも `hokusai/prime_gaps.py` の純関数で、`collect_gaps()` に `state` 引数を追加して合流。live Notion 呼び出しなしで完結 ([docs/dogfooding-findings.md §10](docs/dogfooding-findings.md) の「SQLite-backed gap を優先」方針に沿う)。残り 2 種 (`missing_verification_command` は config に build/test/lint の既定値が常に入り clean な signal が無いため見送り、`pending_gate_blocking` は Notion 依存) は未実装。回帰防止テスト 13 件追加 (`tests/test_prime_gaps.py` 11 件 + `tests/test_prime_v2_query.py` 1 件 + collect_gaps 合流 1 件)。詳細: [docs/design-prime-v2.md §6.1](docs/design-prime-v2.md)。

---

## [0.6.0] - 2026-05-29

GBrain (AI agent 用長期記憶エンジン) 調査から起こした **v0.6 ロードマップ Step 1「Prime v2」** の MVP-1 / MVP-2 / MVP-4 を実装した minor リリース。`hokusai prime` を「active context の整形出力」から「**過去 workflow 資産を検索し、不足情報を能動的に検出する**」方向へ拡張した。設計議論は [docs/roadmap-gbrain-inspirations.md](docs/roadmap-gbrain-inspirations.md) / [docs/design-prime-v2.md](docs/design-prime-v2.md)、実環境 dogfooding は [docs/dogfooding-findings.md §10](docs/dogfooding-findings.md) を参照。

すべて additive。新フラグ `--query` / `--include-gaps` 未指定時、**Markdown 出力は v0.5.x と完全に同一**（検索結果 / gap section は現れない）。**JSON 出力は additive なキー `query` / `query_results` / `gaps` が増える**が、いずれも未指定時は `null` のため、既存キーを参照する consumer は影響を受けない（byte 単位では非同一だが、後方互換）。

### Added

- **Prime v2 MVP-1: FTS5 検索インデックスの土台** ([#134](https://github.com/shigenoko/hokusai/pull/134)): `SQLiteStore` に FTS5 virtual table `prime_index` + meta table `prime_index_meta` を追加し、`upsert_prime_index()` / `search_prime_index()` / `clear_prime_index_for_workflow()` の 3 メソッドを実装。`_WORKFLOW_DEPENDENT_TABLES` にも追加し completed workflow の cascade-delete で孤児化しない。検索バックエンドは `unicode61 remove_diacritics 2`、引用 (citation) 用に `notion_page_id` / `pr_url` / `file_path` を meta に保持。既存 DB を開いた際は `_init_db()` で migration が走り、旧 DDL (`source_type` indexed) は case-insensitive 判定で検出して rebuild する。回帰防止テスト 16 件 (`tests/test_prime_index.py`)。
- **Prime v2 MVP-2: `hokusai prime --query` + active context backfill** ([#135](https://github.com/shigenoko/hokusai/pull/135)): `hokusai prime <workflow-id> --query "..."` フラグを追加。指定時は Notion から取得した active context (memories / work_items / review_issues / gates) を `prime_index` に backfill した上で FTS5 MATCH 検索を実行し、上位 N 件 (既定 10、`--query-limit` で調整) を Markdown / JSON 出力に引用付きで追加する。`extract_prime_index_entries()` 純関数で Notion data → index entries の変換を分離。`clear_prime_index_for_workflow()` に `source_types` 引数を追加して部分 fetch 時の他 source_type 保護 / `--type` フィルタ時の memory 保護を実装。FTS5 入力は `_sanitize_fts5_query()` で phrase 化して `:` / 括弧 / 先頭 `-` 等の予約構文を回避、`--query-limit` は `_positive_int()` で parse 時に `>=1` を保証。`--dry-run` 時は backfill / search を skip して SQLite を mutate しない。`--query` 未指定時は v0.5.x と完全互換 (検索結果セクション非出力、JSON も `query` / `query_results` は `null`)。回帰防止テスト 34 件 (`tests/test_prime_v2_query.py`)。
- **Prime v2 MVP-4: gap analysis (決定的検出 3 種)** ([#136](https://github.com/shigenoko/hokusai/pull/136)): `hokusai prime <wf> --include-gaps` で「未確定 / 不足情報」セクションを追加。LLM 不要・決定的に検出する 3 種を `hokusai/prime_gaps.py` に純関数として実装: `unresolved_review_issue_open` (起点 workflow に紐づく Review Issue で Status=Open)、`notion_outbox_pending` (`notion_sync_outbox` に pending 行あり)、`audit_log_silence` (LLM Gateway 有効なのに `audit_logs` 0 件 = interceptor 経路無効化の疑い)。各 detector は失敗を best-effort で握りつぶし prime 本来の出力を阻害しない。Markdown / JSON 両形式で出力、`--include-gaps` 未指定時は完全互換 (gap section 非出力、JSON も `gaps: null`)。残り 4 種 (`missing_verification_command` / `pending_gate_blocking` / `phase4_plan_missing` / `supersedes_chain_broken`) は MVP-5 以降。回帰防止テスト 23 件 (`tests/test_prime_gaps.py` 15 件 + `tests/test_prime_v2_query.py` 追加 8 件)。

### Documentation

- **v0.6 ロードマップ草案** ([#131](https://github.com/shigenoko/hokusai/pull/131)): GBrain 調査メモを `docs/roadmap-gbrain-inspirations.md` に整理。Prime v2 / Doctor-Status 統合 / Operation Registry / Eval Capture / Local Workgraph Edges の 5 Step に分解。
- **Prime v2 設計議論** ([#133](https://github.com/shigenoko/hokusai/pull/133)): `docs/design-prime-v2.md` で検索バックエンド選定 (SQLite FTS5)、引用データモデル、gap analysis 7 種、MVP 4 段階分割、未解決の設計問題 7 件を整理。
- **Notion DB share 手順** ([#130](https://github.com/shigenoko/hokusai/pull/130)): `docs/notion-dashboard-operation-guide.md` §2.2.1 に生成 DB を integration に share する手順 + トラブルシューティングを追記 (dogfooding-findings §1 運用穴)。
- **§10 Prime v2 実環境 dogfooding** ([#137](https://github.com/shigenoko/hokusai/pull/137)): MVP-1/2/4 を既存 workflow に対し実 `hokusai prime` で観察。MVP-1 migration の前方互換、`notion_outbox_pending` gap 発火、`audit_log_silence` 誤検出なし、`--query` は Notion 未設定で空 (§1 運用穴依存) を実証。

### 統計

- 計 8 PR (#130–#137): 新機能 3 (#134 / #135 / #136) + docs 4 (#130 / #131 / #133 / #137) + chore 1 (#132 `.gitignore`)
- 回帰防止テスト 計 73 件追加 (index 16 / query 34 / gaps 23 — 一部重複カウントあり)
- Copilot review: PR #135 単体で 7 round 16 件の指摘を全 resolve
- 全 PR で CI 全 pass 維持、`hokusai prime` の後方互換を保持

---

## [0.5.1] - 2026-05-28

v0.5.0 リリース後の **dogfooding-findings §7 再観察サイクル** で記録した運用穴 F1–F4 を全て解消した patch リリース。`docs/dogfooding-findings.md` §7 / §8 と合わせて、Phase 2 enforcement の運用前提条件が揃った状態に到達。

7 PR 構成: F4 配線 (#120 / #121) → F1 env override (#122) → F3 audit CLI (#123) → §8 end-to-end 再観察 (#124) → F2 policy wizard (#125)。

### Added

- **F4 解消 (1/2)** ([#120](https://github.com/shigenoko/hokusai/pull/120)): 3 client (`ClaudeCodeClient` / `CodexClient` / `GeminiClient`) の `execute_skill` / `execute_prompt` / `review_document` / `generate` メソッドに `workflow_id: str | None = None` / `phase: int | None = None` 引数を追加し、`_invoke_llm_gateway_interceptor` → `dispatch_via_gateway` まで伝播する経路を整備。default `None` で後方互換 100%。回帰防止テスト 6 件。
- **F4 解消 (2/2)** ([#121](https://github.com/shigenoko/hokusai/pull/121)): 各 phase node (`phase2_research` / `phase3_design` / `phase4_plan` / `phase5_implement` / `phase7_review` / `phase8/review_fix` / `utils/cross_review`) から `state["workflow_id"]` と該当 phase 番号を client に渡す配線を完成。これで実 phase node 経由の LLM 呼び出しでも `audit_logs.workflow_id` が SQLite に書き込まれる。回帰防止テスト 5 件 + 既存 phase テスト 3 件への kwargs assert 追加。
- **F1 解消** ([#122](https://github.com/shigenoko/hokusai/pull/122)): `HOKUSAI_LLM_GATEWAY_ENABLED` env override を `_parse_llm_gateway_config` に追加。truthy (`1` / `true` / `yes` / `on`) / falsy (`0` / `false` / `no` / `off`) 認識（case-insensitive）で yaml/default を上書き。yaml 編集なしの dogfooding 一時 enable / disable が可能に。env 隔離 fixture を `tests/conftest.py` に集約してテストスイート全体の非決定性を排除。
- **F3 解消** ([#123](https://github.com/shigenoko/hokusai/pull/123)): `hokusai audit list [--workflow-id wf-...] [--phase N] [--action ...] [--status block|log|skipped] [--limit N] [--output table|json]` と `hokusai audit show <id>` サブコマンドを追加。`SQLiteStore.list_audit_logs()` / `get_audit_log()` 新規 helper を実装。`sqlite3` 直叩きから解放、運用調査・自動化テスト両方の導線が整った。`limit < 1` を early reject（SQLite negative LIMIT = 全件返却の事故防止）。回帰防止テスト 17 件。
- **F2 解消** ([#125](https://github.com/shigenoko/hokusai/pull/125)): `hokusai llm-gateway-setup` 診断 wizard を追加。`interceptor._evaluate_policy_hits` の真の policy 経路（`allowed_providers` が `None` なら model 系も skip される仕様）に従い、`allowed_providers is None` なら必ず no-op 警告 + 推奨設定例を提示。`[]`（明示空 = deny-all 意図）は別カテゴリ警告。`enabled=true, log_only=false` で即座 no-op になる状態ならより強い文言。yaml 直接書き込みは安全のため避け、stdout 案内 + `hokusai profile show <name>` で実パス確認を促す設計。warning ありは `exit 1` で CI フックにも組み込み可能。回帰防止テスト 13 件。

### Documentation

- **§7 dogfooding 再観察** ([#119](https://github.com/shigenoko/hokusai/pull/119)): `docs/dogfooding-findings.md` に v0.5.0 後の Phase 2 enforcement 再観察セクション §7 を追加。F1–F4 を新規 finding として記録、Phase 2 enforcement 配線 4 経路（audit 永続化 / block raise / fail-open / 許可透過）が実環境で期待通り動作することを実証。
- **§8 end-to-end 再観察** ([#124](https://github.com/shigenoko/hokusai/pull/124)): F1 + F4 解消後の end-to-end 検証を §8 として追加。`HOKUSAI_LLM_GATEWAY_ENABLED=1` + `ClaudeCodeClient.execute_prompt(workflow_id, phase)` で `audit_logs` に行が落ち、`hokusai audit list` で CLI から確認できることを実機で実証（実観察結果: `id=4 / workflow_id="wf-reobservation-001" / phase=2 / config_snapshot.enabled=true`）。

### 統計

- 計 7 PR、24 commit、追加: ~1500 行（コード + テスト + docs）
- Copilot review 計 40+ 件の指摘を全 resolve
- 全 7 PR で CI 全 pass 維持

---

## [0.5.0] - 2026-05-26

LLM Gateway **Phase 2 enforcement ロードマップ** を完成。Phase 1 audit log-only から、profile policy 違反で LLM 実送信を抑止する Phase 2 enforcement までの全配線を入れた（[ロードマップ](https://www.notion.so/LLM-Gateway-Phase-2-Enforcement-36985495565d81949239fd2bdc831e00)、出典: `docs/dogfooding-findings.md`）。

ロードマップ構成: M0 前提条件（3 PR）→ M1 enforcement 切替（3 PR）→ M2 独立小穴（5 PR）→ 本体配線（1 PR）の 12 PR + README 整合（1 PR）。`LLMGatewayConfig.log_only=False` を opt-in した profile でのみ block が発火し、`log_only=True`（default）では後方互換 100%。

その後、Phase 2 enforcement 公開リリース前の **dogfooding-findings 残課題対応** として、M2.6 (cleanup --stale 改善) + A (fail-fast モード) + C (SKIP_NOTION profile 化、core + follow-up) の 4 PR を追加。これで `docs/dogfooding-findings.md` の独立小穴は全項目完了。

### Added

#### M0: Phase 2 enforcement の前提条件

- **M0.1** ([#81](https://github.com/shigenoko/hokusai/pull/81)): `LLMGatewayInterceptor._emit_audit` を SQLite `audit_logs` に永続化。`workflow_id` が埋まる呼び出しのみ INSERT（orphan / FK 違反回避）。`_AUDIT_STORE_CACHE` モジュールレベル cache で DDL 再実行を抑止。
- **M0.2** ([#83](https://github.com/shigenoko/hokusai/pull/83)): `hokusai start` 冒頭で Notion DB share 健全性チェック（`NotionSyncDispatcher.check_db_share_health`）。404 を構造的検出（`e.status == 404`）、preflight 専用 client（`max_attempts=1`）で retry 抑制。
- **M0.3** ([#85](https://github.com/shigenoko/hokusai/pull/85)): `hokusai status` に Notion outbox 失敗の可視化追加。`SQLiteStore.fetch_recent_outbox_with_errors` で `next_attempt_at ASC` 安定 sort。

#### M1: Enforcement 切替経路

- **M1.1** ([#87](https://github.com/shigenoko/hokusai/pull/87)): `interceptor.intercept()` に `decision="block"` 経路を追加（`log_only=False` + `policy_hits` 非空）。実装は `DECISION_BLOCK = "block"` 定数で既存 vocabulary と整合。
- **M1.2** ([#89](https://github.com/shigenoko/hokusai/pull/89)): `configs/profile-config-template.yaml` / `example-profile-company.yaml` に `llm_gateway:` セクション追加。`log_only` ロールアウト戦略（自社 profile 先行 enforce / 案件 profile 維持）をコメントで明示。
- **M1.3** ([#91](https://github.com/shigenoko/hokusai/pull/91)): `docs/hokusai-llm-gateway-requirements.md` に **§4.4 fail-open 原則** を新設。明示的 `block` / `require_human_approval` 以外の Gateway 内部異常は workflow を止めない契約を文書化、§14 受け入れ基準にも反映。

#### M2: 独立小穴

- **M2.1** ([#97](https://github.com/shigenoko/hokusai/pull/97)): `HOKUSAI_SKIP_NOTION=1` pre-set 時に profile 整合性 warning を `main()` で出す。Notion 設定済み profile での mismatch を可視化。
- **M2.2** ([#99](https://github.com/shigenoko/hokusai/pull/99)): `hokusai cleanup` `--cancel-reason` 未指定時の Notion ゴースト警告（worktree 削除済み but Notion Status 未更新の発生防止）。
- **M2.3** ([#95](https://github.com/shigenoko/hokusai/pull/95)): `default_profile` を CLI 全体で implicit 解決。`hokusai/config/profiles.py::try_resolve_default_profile_name()` を fail-safe（`is_file()` + 1 バイト read 検証）で実装。`--profile` 未指定 + `-c/--config` 未指定で registry の `default_profile` を自動適用。CLI 表示で `(default_profile)` suffix で明示と区別。
- **M2.4** ([#93](https://github.com/shigenoko/hokusai/pull/93)): `hokusai prime` 空状態に構成要素別 diagnostics 行を追加（`*Project Memory DB: 未設定 (env XXX)*` 等の原因切り分けライン）。`_build_prime_diagnostics` 純粋関数で実装。
- **M2.5** ([#101](https://github.com/shigenoko/hokusai/pull/101)): `hokusai cleanup --gc-workflows [--retention-days N]`（default 90 日）。完了済み workflow（`current_phase >= 10`）を 9 dependent table と cascade 削除。`sqlite_master` existence check + argparse `type` で `retention >= 1` 検証。
- **M2.6** ([#108](https://github.com/shigenoko/hokusai/pull/108) / Issue [#107](https://github.com/shigenoko/hokusai/issues/107)): `hokusai cleanup --stale` に `--dry-run` (誤操作防止) と `--sync-notion` (Notion ゴースト残留防止) を opt-in で追加。`--dry-run` は `shutil.rmtree` / `git worktree prune` / writeback cleanup を全て skip、`--sync-notion` は stale 削除した workflow に `cancel_reason="stale cleanup"` で `_sync_workflow_cancel_reason` を呼ぶ。両フラグ default off で完全後方互換、argparse `dest="cleanup_dry_run"` でトップレベル `--dry-run` との衝突回避。

#### dogfooding-findings 残課題（Phase 2 enforcement リリース前対応）

- **A. fail-fast モード** ([#110](https://github.com/shigenoko/hokusai/pull/110) / Issue [#109](https://github.com/shigenoko/hokusai/issues/109) / findings §3.1): `NotionSyncOutboxConfig.fail_fast_on_workflow_started_error: bool = False` を追加（opt-in）。同一 workflow の `workflow_started` が既に `notion_sync_errors` に永続失敗で入っている場合、後続子イベントを outbox 経由の retry に乗せず errors に直送する。`SQLiteStore.has_failed_workflow_started` / `record_permanent_notion_sync_failure` helper を新設し、`notion_sync_errors` に `(idempotency_key)` と `(workflow_id, event_type)` の専用 index を追加。Workflows DB share 未完了など永続障害環境での outbox 膨張を抑止。
- **C. SKIP_NOTION profile 化 (core)** ([#112](https://github.com/shigenoko/hokusai/pull/112) / Issue [#111](https://github.com/shigenoko/hokusai/issues/111) / findings §1.3): `HOKUSAI_SKIP_NOTION` がプロセス全体に効く問題を解消。`hokusai/utils/skip_notion.py` に `is_skip_notion(profile_name=None)` / `active_skip_env_name()` / `set_active_profile()` helper を新設。評価順は (1) 明示引数 `HOKUSAI_SKIP_NOTION_<SLUG>` → (2) `HOKUSAI_ACTIVE_PROFILE` 経由の同 key → (3) legacy global `HOKUSAI_SKIP_NOTION`。`main()` で解決済み profile を `HOKUSAI_ACTIVE_PROFILE` に setenv（既存値は上書きしない `setdefault`）。core パス 6 ファイル（`state.py` / `workflow.py` / `integrations/connection_status.py` / `integrations/task_backend/notion.py` / `cli/services/environment.py`）を置換。
- **C. SKIP_NOTION profile 化 (follow-up)** ([#114](https://github.com/shigenoko/hokusai/pull/114) / Issue [#113](https://github.com/shigenoko/hokusai/issues/113)): 残箇所を helper 経由に統一。`hokusai/utils/notion_helpers.py` 6 箇所 / `hokusai/cli_main.py` 4 箇所 / `hokusai/nodes/phase2_research.py` 2 箇所 / `hokusai/nodes/phase3_design.py` 1 箇所を `is_skip_notion()` / `active_skip_env_name()` に置換。これで `os.environ.get("HOKUSAI_SKIP_NOTION")` 直接参照はコードから完全消滅。`_warn_if_skip_notion_pre_set` の警告文言は `active_skip_env_name()` で動的化（profile suffix env が立っていれば正しい env 名を案内、legacy + suffix の両方 set なら両方を `unset` 列挙）。

#### 本体配線

- **[#103](https://github.com/shigenoko/hokusai/pull/103)**: `LLMGatewayBlockedError` 例外クラスを `dispatch.py` に新設。`dispatch_via_gateway` で `decision="block"` のとき raise、3 client（claude_code / codex / gemini）の except 句で fail-closed 伝播（M1.3 §4.4 例外）。Gateway 内部の予期せぬ例外は引き続き fail-open。`prompt` 本文は例外メッセージ / attributes に **含めない**（要件 §14 secret / PII 保護）。

### Docs

- **[#105](https://github.com/shigenoko/hokusai/pull/105)**: README (英日両版) を v0.4.8 / Phase 2 enforcement / 新 CLI コマンドに整合化。LLM Gateway / `cleanup` 新フラグ / Notion 環境変数 6 件 / `default_profile` implicit 解決 / v0.4.8 タグを反映。

### 後方互換

- `LLMGatewayConfig.log_only=True`（default）では Phase 2 enforcement の挙動は一切発動しない（M1.1 仕様で BLOCK 判定が起きない）
- profile registry を持たない環境では従来通り `claude-workflow.yaml` 探索にフォールバック（M2.3 fail-safe）
- legacy DB（古いスキーマ）でも `--gc-workflows` は `sqlite_master` existence check で skip（M2.5）
- `--stale` の `--dry-run` / `--sync-notion` は default off で従来挙動と完全同一（M2.6）
- `NotionSyncOutboxConfig.fail_fast_on_workflow_started_error` は default False で従来挙動と完全同一（A. fail-fast）
- legacy global `HOKUSAI_SKIP_NOTION=1` は引き続き有効、`HOKUSAI_SKIP_NOTION_<SLUG>` opt-in で profile-aware に拡張（C. SKIP_NOTION profile 化）

### バージョン

- `pyproject.toml` / `hokusai/__init__.py`: **0.4.8 → 0.5.0** に bump（minor リリース）。Phase 2 enforcement 完成 + dogfooding-findings 残課題完了の節目として明示。`log_only=True` default は維持されており、既存環境では実害なし。

---

## [0.4.8] - 2026-05-15

Workflows DB に `Operator` プロパティを追加し、複数エンジニア共有 profile 運用で
「誰が `hokusai start` を叩いたか」を可視化
（[#21](https://github.com/shigenoko/hokusai/issues/21) 部分実装 / Notion 議論 §D-1）。

Issue #21 で期待されていた 3 DB（Workflows / Work Items / Review Issues）のうち、
後 2 者は v0.5.x の Human Governance Workgraph 本実装で新規作成される計画機能のため、
本リリースでは **Workflows DB のみ** を対象とする。

### Added

- `hokusai/integrations/notion_dashboard/operator.py`:
  - `resolve_operator_name() -> str`: env `HOKUSAI_OPERATOR_NAME` → `whoami` → `"(unknown)"` の順で解決
- `_WORKFLOWS_DB_PROPERTIES` に `Operator` (rich_text) を追加（`hokusai notion-setup` で新規 DB は自動反映）
- `NotionAPIClient.update_database(database_id, payload)`: 既存 DB スキーマ更新用 API
- `hokusai notion-migrate-schema` サブコマンド: 既存 Workflows DB に v0.4.8+ の新プロパティを idempotent に追加
  - `--workflows-db-id`、`--api-token-env`、`--dry-run` をサポート
  - profile 解決にも対応
- `tests/test_operator.py`: Operator 解決ロジック 11 ケース
- 既存テスト拡張:
  - `tests/test_notion_setup.py`: Workflows DB schema に `Operator` が含まれることを検証
  - `tests/test_notion_dashboard.py`: payload に operator がある時 / 無い時 / 空文字の時の挙動、`update_database` の PATCH URL 検証

### Changed

- `hokusai/workflow.py`: `WorkflowRunner.start` の `workflow_started` event payload
  に `operator=resolve_operator_name()` を含める（以降の event では送信せず Notion
  側を温存）。Notion 同期が未設定の場合は operator 解決自体を skip して whoami
  の余計な遅延を回避する。
- `_build_properties` で `event_type == "workflow_started"` を明示的にガードし、
  後段の event で誤って operator が混入しても Notion 側の既存値を温存する（invariant 強制）
- `hokusai/integrations/notion_dashboard/workflows_db.py`: `_build_properties` で
  payload の `operator` キーを `Operator` rich_text property にマッピング
- `_WORKFLOWS_DB_DESCRIPTION`: 「HOKUSAI が書き込むプロパティ」一覧に `Operator` を追加

### 後方互換

- 既存レコードは破壊しない（既存ページの `Operator` は空のまま）
- 既存 DB（v0.4.7 以前で作成済み）に `Operator` プロパティが無い場合:
  - `_submit_with_property_pruning` の既存ロジックで自動的に該当プロパティを除去して再試行
  - migration したい場合は `hokusai notion-migrate-schema` で追加
- 新規 DB（`hokusai notion-setup`）: schema に `Operator` 含まれて作成される

### 使い方

```bash
# 環境変数で明示
export HOKUSAI_OPERATOR_NAME="alice"
hokusai --profile a-company start <issue-url>

# 既存 DB に Operator プロパティを追加
hokusai --profile a-company notion-migrate-schema --dry-run  # 確認
hokusai --profile a-company notion-migrate-schema            # 実行
```

### バージョン

- `pyproject.toml`: 0.4.7 → 0.4.8
- `hokusai/__init__.py`: 0.4.7 → 0.4.8

### 関連

- 後続: Work Items DB / Review Issues DB への Operator 追加は v0.5.x の Human Governance Workgraph 本実装で行う

---

## [0.4.7] - 2026-05-15

profile 共有テンプレートをリポジトリに追加
（[#22](https://github.com/shigenoko/hokusai/issues/22) 対応 / Notion 議論「複数エンジニアによる開発の課題」§D-2）。

### Added

- `configs/profile-template.yaml`: profile registry の実運用テンプレート
  - `<TODO:...>` プレースホルダ形式で、`grep "<TODO:"` で残置検出可能
  - `cp` → 置換 → `hokusai profile doctor` で動作確認可能
- `configs/profile-config-template.yaml`: 個別案件 profile config の実運用テンプレート
  - 全主要セクション（task_backend / git_hosting / notion_dashboard / figma / miro
    / notifications / web_dashboard / cross_review）を含む
  - v0.4.6 で導入された `cross_review.provider` にも対応
- `tests/test_config_templates.py`: テンプレートの妥当性検証
  - YAML として valid
  - シークレット実値（OpenAI / Notion / GitHub PAT / Slack token / webhook URL）が
    混入していないことを正規表現でチェック
  - プレースホルダ置換後に YAML loader でロード可能

### Changed

- README.md / README_JP.md: profile セクションに「新メンバー向け展開手順」を追加
- `example-*` と `*-template.yaml` の位置づけを整理:
  - `example-*` は学習用サンプル
  - `*-template.yaml` はコピー → プレースホルダ置換だけで動く実運用ベース

### バージョン

- `pyproject.toml`: 0.4.6 → 0.4.7
- `hokusai/__init__.py`: 0.4.6 → 0.4.7

---

## [0.4.6] - 2026-05-14

クロスレビュー LLM として **Google Gemini CLI** に対応
（[#31](https://github.com/shigenoko/hokusai/issues/31) 対応）。

### Added

- `hokusai/integrations/gemini.py`: `GeminiClient`
  - `gemini` CLI を subprocess 経由で実行
  - `review_document()`: CodexClient と同インターフェースの cross-review API
  - `generate(prompt, files=None)`: 汎用テキスト生成 API（B 案で再利用予定）
  - `_find_gemini_command()`: PATH / `GEMINI_PATH` / 一般的 install パスから検出
- `tests/test_gemini_client.py`: 13 件（コマンド検出 / review_document / generate / singleton）
- `tests/test_codex.py::TestProviderDispatch`: provider 切替テスト 3 件
- `hokusai connect gemini`: 接続状態確認 + 認証導線（`gemini` 起動で OAuth 開始）
- `connection_status` に Gemini 検出を追加（`hokusai connect --status` で表示）

### Changed

- `CrossReviewConfig.provider: str = "codex"` フィールド追加（"codex" / "gemini"）
  - 既定 "codex" で **後方互換性 100%**（既存 config はそのまま動作）
- `hokusai/utils/cross_review.py`: provider 別 client を `_create_review_client()` で
  dispatch する設計に変更。これ以降の処理は client 非依存（duck typing）
- 不明な provider 指定は config loader 側で既定 "codex" に fallback、cross_review 実行時は
  `waiting_for_human` で停止して config 修正を促す（致命扱い）
- エラーメッセージ / ログを provider 一般化（Codex 固定文言から `{provider}` ベースに）

### 設定例

```yaml
cross_review:
  enabled: true
  provider: gemini              # 新規追加（v0.4.6〜）
  model: gemini-2.5-pro
  phases: [2, 4]
  on_failure: warn
```

### 後方互換

- `provider` 未指定の既存 config は "codex" 扱いで従来挙動を維持
- `CrossReviewConfig` のフィールド順序を変更していないため、位置引数による初期化も互換
- 主コーディングエージェント（Phase 2-7）は引き続き Claude Code 固定。B 案（v0.5.x 予定）で抽象化

### バージョン

- `pyproject.toml`: 0.4.5 → 0.4.6
- `hokusai/__init__.py`: 0.4.5 → 0.4.6

---

## [0.4.5] - 2026-05-14

`hokusai notion-setup` のリソース名から `HOKUSAI` prefix を削除し、
scaffold サブページタイトルを日本語化
（[#29](https://github.com/shigenoko/hokusai/issues/29) 対応）。

### Changed

- DB タイトル: `HOKUSAI Workflows DB` → `Workflows DB`、`HOKUSAI Pull Requests DB` → `Pull Requests DB`
- scaffold ハブ: `HOKUSAI Documentation` → `Documentation`（英語のまま、icon 📚）
- scaffold サブページ: 日本語化
  - `Discussions` → `議論`（icon 💬）
  - `Operation Guides` → `運用ガイド`（icon 📖）
  - `Requirements` → `要件定義`（icon 📋）

### 後方互換

- `_DOCUMENTATION_HUB_LEGACY_TITLES` / 各サブの `legacy_aliases` に **2 世代分** の旧タイトルを追加:
  - v0.4.4 旧名: `HOKUSAI Documentation` / `Discussions` / `Operation Guides` / `Requirements`
  - v0.4.3 旧名: `📚 HOKUSAI Documentation` / `💬 Discussions` / `📖 Operation Guides` / `📋 Requirements`
- 旧バージョンで作成されたページは canonical 優先で skip 検出（重複作成なし）
- DB は title 検出を行わない（env var が DB ID を保持）ため legacy alias 不要

### バージョン

- `pyproject.toml`: 0.4.4 → 0.4.5
- `hokusai/__init__.py`: 0.4.4 → 0.4.5

---

## [0.4.4] - 2026-05-14

`hokusai notion-setup --scaffold` のページタイトル形式を更新
（[#27](https://github.com/shigenoko/hokusai/issues/27) 対応）。

### Changed

- scaffold で作成されるページの title から絵文字 prefix を削除し、絵文字は
  Notion page icon でのみ表現するよう変更
  - 旧: title=`📚 HOKUSAI Documentation`, icon=📚 → Notion UI で二重表示
  - 新: title=`HOKUSAI Documentation`, icon=📚 → icon のみ
- 4 ページすべて同様（`Discussions` / `Operation Guides` / `Requirements`）

### 後方互換

- `_find_existing_child_page` に `legacy_aliases` パラメータを追加
- v0.4.3 で作成された絵文字 prefix 付きタイトルのページは legacy alias として
  検出され、`--scaffold` 再実行時に重複ページが作成されない
- 既存ページの自動リネームは行わない（破壊的変更を回避）
- UI 二重表示を解消したい場合は Notion 側で手動リネーム推奨

### バージョン

- `pyproject.toml`: 0.4.3 → 0.4.4
- `hokusai/__init__.py`: 0.4.3 → 0.4.4

---

## [0.4.3] - 2026-05-14

`hokusai notion-setup` に `--scaffold` オプションを追加
（[#25](https://github.com/shigenoko/hokusai/issues/25) 対応）。

新規 profile / 新規 workspace 立ち上げ時に、Notion governance layer の
標準ドキュメントツリーをワンコマンドで scaffold できる。HOKUSAI が自動同期する
DB 領域と、人間が書くドキュメント領域を視覚的・運用的に分離する。

### Added

- `hokusai notion-setup --scaffold` オプション
- 親ページ配下に以下のページツリーを作成（scaffold 部分のみ idempotent）:
  - 📚 HOKUSAI Documentation（ハブ）
    - 💬 Discussions
    - 📖 Operation Guides
    - 📋 Requirements
- 各ページに役割を説明する placeholder paragraph
- `scaffold_notion_workspace()` 関数（`setup_notion_workspace(..., scaffold=True)` から呼び出し）
- `NotionAPIClient.list_block_children()` を `start_cursor` 対応に拡張（pagination でツリーを全件走査）
- scaffold 結果に `failed` を追加し、サブページ作成失敗を呼び出し側で確認できるように
- `__version__` / `pyproject.toml` のバージョン番号を `0.4.3` に同期

### 設計

- **オプトイン**: `--scaffold` 未指定なら従来通り DB 作成のみ
- **Idempotent**: 既存に同名ページがあれば skip（再実行で重複しない）
- **partial success**: 個別サブページの作成失敗で全体を止めない（ハブ作成失敗のみ致命）
- **DB 作成と独立**: scaffold エラーは DB 作成結果に影響しない

### 後方互換

- `--scaffold` 未指定での既存挙動は変わらない
- `setup_notion_workspace()` の戻り値は scaffold=True のときのみ `scaffold` キーが追加される

詳細は `docs/hokusai-issue-25-notion-setup-scaffold-implementation-plan.md` に対応。

---

## [0.4.2] - 2026-05-14

Operations Console の Notion 接続状態パネルに **「どの Notion か」識別情報を表示**
（[#19](https://github.com/shigenoko/hokusai/issues/19) 対応）。

v0.4.1 の profile-aware notion-setup（#17 / #18）の続編。複数 Notion ワークスペース
を profile 単位で使い分けるときに、現在 dashboard がどの Notion につながっているかを
一目で確認できるようにする。

### Added

- Notion 接続状態パネルに「接続先 Notion」セクションを追加:
  - 現在 active な profile 名
  - 使用中の env 変数名（`api_token_env`）
  - Workflows DB / Pull Requests DB の ID（先頭 8 桁 + 末尾 4 桁マスク）
  - 各 DB の Notion URL（クリック可能なリンク、title 属性に完全 ID）
  - Bot user 名（Notion API `GET /users/me` から取得）
- `NotionAPIClient.get_bot_info()` メソッドを追加。
- `hokusai.integrations.notion_dashboard.identification` モジュール（`mask_db_id` /
  `notion_db_url` / `get_bot_info`（5 分 TTL キャッシュ）/ `build_notion_identification`）。

### 後方互換

- Notion 連携が disabled の場合、Notion パネル自体が表示されない（従来通り）。
- Notion API への bot info 取得に失敗しても panel は落とさず、`(unable to fetch)`
  と表示して他項目（profile / env 名 / DB ID / DB URL）は通常通り表示する。

詳細は `docs/hokusai-issue-19-notion-dashboard-panel-identification-implementation-plan.md` に対応。

---

## [0.4.1] - 2026-05-14

`hokusai notion-setup` の **profile-aware 化**（[#17](https://github.com/shigenoko/hokusai/issues/17) 対応）。

複数の Notion ワークスペースを profile 単位で使い分けるユースケースで、
profile config の env 変数名を自動採用する。

### Changed

- `hokusai notion-setup` で `--profile <name>` が指定されたとき、profile config の
  `notion_dashboard.api_token_env` / `workflows_db_id_env` / `pull_requests_db_id_env`
  を自動採用するようになった。
- `--api-token-env` の既定値を `None` に変更。明示指定 / profile config / 既定値
  （`HOKUSAI_NOTION_API_TOKEN`）の優先順位で env 名を解決する。
- `--persist` で rc に書き込む env 名も profile config に追従するようになった。
- `--profile` 指定時に config 読み込みが失敗した場合は、原則として中断する
  ようになった（既定 `HOKUSAI_NOTION_API_TOKEN` で続行すると別案件用の token を
  誤って使うリスクがあるため）。`--api-token-env` が明示指定されている場合のみ
  警告のうえ既定 env フォールバックで続行する。

### Added

- profile 別マーカー（`# === HOKUSAI Notion Dashboard ... profile=<name> ===`）。
  同じ rc ファイルに複数 profile の env ブロックを並列保存できる。
- `persist_env_vars()` に `workflows_env_name` / `pull_requests_env_name` /
  `profile_name` 引数を追加（後方互換あり）。

### 後方互換

- `--profile` を指定しない実行は従来通り `HOKUSAI_NOTION_API_TOKEN` /
  `HOKUSAI_NOTION_WORKFLOWS_DB_ID` / `HOKUSAI_NOTION_PR_DB_ID` を使う。
- `--profile` 未指定（`profile_name=None`）での `--persist` は従来マーカー
  （`# === HOKUSAI Notion Dashboard ... ===`）を使うため、既存ブロックは
  従来通り置換される。
- `--profile <name>` を指定した実行は profile 別マーカー（`profile=<name>`）を
  使うため、既存の legacy ブロックを上書きせず、同じ rc ファイル内に共存できる。

詳細は `docs/hokusai-issue-17-notion-setup-profile-aware-implementation-plan.md` に対応。

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

[Unreleased]: https://github.com/shigenoko/hokusai/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/shigenoko/hokusai/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/shigenoko/hokusai/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/shigenoko/hokusai/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/shigenoko/hokusai/compare/v0.2.0...v0.5.0
[0.3.0]: https://github.com/shigenoko/hokusai/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/shigenoko/hokusai/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/shigenoko/hokusai/releases/tag/v0.1.0
