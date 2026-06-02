# HOKUSAI dogfooding findings — wf-dbe7b6cd

**対象読者**: HOKUSAI 本体開発者、Phase 2 enforcement 設計を検討する人
**目的**: HOKUSAI 本体を題材に 1 workflow を完走させて、`start` / `prime` / Notion 同期 / `cleanup` 経路の運用穴を直接観察し、Phase 2 enforcement の優先順位判定材料を残す
**位置付け**: コード変更を伴わない観察ログ。改善は別タスクで切り出す前提のメモ

本ドキュメントは Issue [#72](https://github.com/shigenoko/hokusai/issues/72) の deliverable として作成された。

---

## 0. メタ情報

| 項目 | 値 |
|---|---|
| Workflow ID | `wf-dbe7b6cd` |
| Branch | `feature/dogfood-hokusai-workflow` |
| Profile | `hokusai`（`~/.hokusai/profiles.yaml` 既定） |
| 実行日 | 2026-05-23 22:52 開始 / 2026-05-24 観察 |
| Repo | `shigenoko/hokusai` |
| Target slot | `Default` |
| Worktree | `~/.hokusai/profiles/hokusai/worktrees/Default_wf-dbe7b6cd` |
| workflow.db | `~/.hokusai/profiles/hokusai/workflow.db` |

### 環境変数（観察時のスナップショット）

| Env | 状態 |
|---|---|
| `HOKUSAI_NOTION_API_TOKEN_4HOKUSAI` | 設定済み（profile config から参照） |
| `HOKUSAI_NOTION_WORKFLOWS_DB_ID_4HOKUSAI` | 設定済み（`36085495-565d-8187-ba56-f5bf5a8d3abd`） |
| `HOKUSAI_NOTION_PR_DB_ID_4HOKUSAI` | 設定済み |
| `HOKUSAI_NOTION_REVIEW_ISSUES_DB_ID_4HOKUSAI` | **未設定** |
| Project Memory / Work Items / Workflow Gates DB ID（4hokusai） | **未設定** |
| `HOKUSAI_SKIP_NOTION` | **`1`**（プロセス全体で Notion 書き込みを skip するフラグが立っている） |
| `WORKFLOW_WORKTREE_ROOT` | 未設定（profile data_dir 配下に worktree が生える設計どおり） |
| LLM Gateway 関連 env | 設定なし（`LLMGatewayConfig` 既定値: `log_only=True`, `audit_log_enabled=True`） |

---

## 1. 観察ポイント 1: `hokusai start <issue_url>`

`hokusai start https://github.com/shigenoko/hokusai/issues/72` 相当の実行（実際はこの worktree が既に該当の workflow に紐付いた状態で観察開始）。

### 期待挙動

- worktree が `profile.data_dir/worktrees/{repo_slot}_{wf-id}` に作成される
- SQLite `workflows` テーブルに新規レコードが入る（`profile_name=hokusai`）
- Notion Workflows DB に対応ページが作成され、Phase 2-4 の子ページがリンクされる
- LLM Gateway audit log に `decision=log` / `reason=phase1_log_only` の構造化エントリが流れる
- dispatcher 失敗時は outbox に enqueue され、`max_retry_attempts=10` でリトライされる

### 実観察

- ✅ worktree 配置: `~/.hokusai/profiles/hokusai/worktrees/Default_wf-dbe7b6cd` に正しく作成。`WORKFLOW_WORKTREE_ROOT` 未設定で `data_dir/worktrees` 配下が選ばれる挙動は `hokusai/config/manager.py:118` の既定動作どおり。
- ✅ SQLite: `workflows` 行が 1 件（`workflow_id=wf-dbe7b6cd`, `profile_name=hokusai`, `current_phase=4`, `branch_name=feature/dogfood-hokusai-workflow`）。
- ❌ Notion Workflows DB 同期: **全て失敗**。`notion_sync_outbox` に 4 件（`workflow_started`, `phase_changed` x2, `terminal_status_changed`）が残留し、`last_error` は全て：

  ```
  NotionAPIError(404): Could not find database with ID:
  36085495-565d-8187-ba56-f5bf5a8d3abd. Make sure the relevant pages and
  databases are shared with your integration "HOKUSAI".
  ```

  つまり DB ID は env に設定済みだが、Notion 側で integration "HOKUSAI" に Workflows DB が **share されていない**。
- ⚠ LLM Gateway audit log: `audit_logs` テーブルは存在するが行数 **0**。`hokusai/llm_gateway/interceptor.py:212` の実装は Python `logger.info("llm_gateway_audit %s", json.dumps(entry))` の 1 行出力のみで、**SQLite / Notion へは何も書かない**（コード上のコメントで Phase 5+ で永続化予定と明記）。実行時 logger 設定によっては stderr にすら現れない。

### 不足点 / 運用穴

1. **dogfooding 環境で Notion 同期が全滅していた**。`hokusai notion-setup` で DB は作られたが、Notion 側で「親ページ + DB を integration に share」する手順が抜けると、CLI は env が揃っているように見えて 404 連発になる。`hokusai start` の冒頭で `is_configured()` だけでなく **接続性チェック（DB ID で軽量 query を 1 回投げる）** が欲しい。
2. **outbox の `last_error` を確認する CLI 経路が無い**。`workflow.db` を `sqlite3` 直接打ちでないと気づけない。`hokusai status` や `hokusai pr-status` に outbox 失敗件数のサマリを出す導線が欲しい。
3. **`HOKUSAI_SKIP_NOTION=1` が profile を跨いで global に残っている**。これが立っていると `hokusai/utils/notion_helpers.py` 系（Phase 2/3 ノードからの Notion 書き込み）と `dispatcher` 経路（Workflows/PR DB）が **整合せずに片方だけ動く** 状態になる。profile-aware な skip フラグ（例: `HOKUSAI_SKIP_NOTION_4HOKUSAI`）化、あるいは profile 切替時に warning を出す改善が望ましい。
4. **LLM Gateway audit log が dogfooding では「事実上消える」**。`audit_logs` テーブルは schema が用意されているのに interceptor は logger.info しか叩かない。Phase 2 enforcement に進むには「何を block したか」の audit が永続化されている必要があるため、enforcement より先に **audit sink の永続化（最低でも SQLite `audit_logs` への挿入）が前提**。

---

## 2. 観察ポイント 2: `hokusai prime <workflow_id>`

`hokusai prime wf-dbe7b6cd --profile hokusai` を Markdown / JSON 両方で実行。

### 期待挙動

- Project Memory / Review Issues / Work Items / Gates / handover_note の 5 ソースが統合されて出力される
- 空 DB / 未接続時は graceful に空セクションを省略
- 起点 workflow の Supersedes リレーションを辿り、旧 workflow 側の active handover_note が混ぜ込まれる

### 実観察

- ✅ 空状態出力（Markdown）:

  ```
  # HOKUSAI Prime Context — workflow `wf-dbe7b6cd`

  profile: `hokusai` / current_phase: `phase4`

  _active な workgraph context はありません_
  ```

  - 文字数: **132 chars / 148 bytes (UTF-8) / 8 行**
  - 概算 token 数: **約 44 tokens**（日本語混在で `len/3` の雑見積もり）
  - 例外なし、exit 0
- ✅ JSON 出力:

  ```json
  {
    "workflow_id": "wf-dbe7b6cd",
    "profile": "hokusai",
    "current_phase": "phase4",
    "memories": [],
    "work_items": null,
    "review_issues": null,
    "gates": null
  }
  ```

  - サイズ: **約 168 bytes**
  - `memories` は `[]`、それ以外は `null`（Markdown 側では None と [] を同一視するが JSON は区別保持 — `prime_renderer.py:80` のコメント参照）
- ✅ handover_note 遡及: `_collect_handover_notes` は `find_workflow_page_id` が page_id を返さない（= Workflows DB share 未完了で 404 になっている）状態だと早期 return で `[]`。例外漏れなし。

### 不足点 / 運用穴

1. **空状態の prime はそのまま LLM に渡しても情報量ゼロ**。`current_phase=phase4` だけは伝わるが、「なぜ memory が空か」（DB share 未完了 / Project Memory DB ID env 未設定 / 本当に空 / Notion 障害）が分からない。空でも構成要素ごとの設定有無を 1 行ずつ書いておく（例: `_Project Memory DB: not configured_`）と原因切り分けが速い。
2. **prime 出力サイズの数値**: 空状態で 132 chars。**context 圧迫は問題にならない規模**。むしろ「空すぎて使えない」側の問題が先。
3. **`--profile hokusai` を毎回付け忘れる**。CLI default は `~/.hokusai/profiles.yaml` の `default_profile` を見るはずだが、`hokusai list` を `--profile` 無しで叩いたら「アクティブなワークフローはありません」と出た。profile 解決が CLI 側で行われていない経路が混在している（`hokusai prime` は workflow_id から profile を逆引きする `state.get("profile_name")` フォールバックがあるが、`list` 系は未対応）。`default_profile` の挙動が CLI 全体で一貫していない疑い。
4. **handover_note 遡及は Notion 接続前提**。Workflows DB が share されていないと chain 0 件で終わるため、Supersedes 機能の dogfooding 検証ができない。次回は健全な接続を確保した上で別 dogfooding workflow を立てる必要あり。

---

## 3. 観察ポイント 3: PR push 後の Pull Requests DB 同期

実 PR push は本ドキュメント commit 後に実施。**コード読みベースの予測 + 観察可能な範囲の実観察** を併記する。

### 期待挙動

- `hokusai workflow` の PR 作成イベント → dispatcher `_handle_pr_created` 経由で Pull Requests DB に新規レコード作成
- `Workflow` リレーションプロパティに `wf-dbe7b6cd` の Workflows DB ページが紐付く
- dispatcher 同期失敗時は `notion_sync_outbox` に `pr_created` event が enqueue され、`backoff_seconds=5.0` x attempt の遅延でリトライ

### 実観察

- ⚠ コード上の経路: `dispatcher.py:444 _handle_pr_created` は **まず Workflows DB から workflow page_id を解決**してから Pull Requests DB に書く設計。Workflows DB の share が壊れている本環境では、PR DB レコード自体は作れても **Workflow リレーションが張れない可能性が高い**（page_id 解決失敗時の挙動はコード追跡では「workflow_started 同期未完了なら deferred ループ送り」とある）。
- ⚠ 現時点の outbox: dispatcher が PR 作成イベントを受ける前なので `pr_created` row は無い。push 後に追記観察すべきだが、Workflows DB 404 が継続している限り **PR レコードが孤児になるか deferred で永遠に retry を続けるか** のどちらかになる予測。

### 不足点 / 運用穴

1. **Workflows DB 同期未完了 → PR DB 書き込みも巻き込まれる依存関係**。理屈は分かるが、Workflows DB share が永久に治らない環境（dogfooding 失敗環境そのもの）では PR DB outbox が膨張し続ける。`max_retry_attempts=10` を超えた時点で `notion_sync_errors` に flush される設計ではあるが、**「workflow_started 失敗 → 子イベントを全て errors に直送する fail-fast モード」** が欲しい局面がある。
2. **PR 同期成否を CLI から覗く経路が `hokusai pr-status` で十分か未検証**。outbox / errors のテーブルを覗かないと「Notion 側に PR が作られていない」事実に気付けない可能性。
3. **PR 作成自体は Notion 同期と独立で進む**ので、Notion 障害が PR コラボレーションをブロックしない設計は正しい（fail-open に倒す方針）。Phase 2 enforcement で audit を block 条件にする際は、**Notion 障害を理由に PR 作成を止めない方針** を明示的に維持すべき。

---

## 4. 観察ポイント 4: `hokusai cleanup` 動作

実行はしない（本 workflow がまだ active）が、コード読みベースで挙動を確定させる。

### 期待挙動

- `hokusai cleanup wf-dbe7b6cd --cancel-reason "dogfood: 観察完了"` → worktree 物理削除 + Notion Workflows DB Status=Canceled / Cancel Reason 記入
- `--stale` モード: 完了済み workflow（current_phase==10）の worktree を一括削除 + `git worktree prune`

### 実観察（コード）

- `_handle_cleanup` (`cli_main.py:1785-1835`):
  - workflow state を SQLite から load → `repositories[].worktree_created` の各 worktree を `GitClient.remove_worktree(force=True)` で削除
  - `cancel_reason` が strip 後に空文字でなければ `_sync_workflow_cancel_reason` を呼ぶ
  - **workflow.db の `workflows` 行は cleanup では削除されない**（state は保持され、`hokusai list` は `current_phase < 10` でフィルタするので「アクティブ外」になるだけ）
- `_stale` ブランチ (`cli_main.py:1837-1877`):
  - `worktree_root` を走査し、ディレクトリ名 `{repo}_wf-{id}` から workflow_id を抽出
  - `list_active_workflows` に無い id は `shutil.rmtree`、最後に各リポジトリで `git worktree prune`
- `_sync_workflow_cancel_reason` (`cli_main.py:1708`): Notion 接続が無い / API 失敗時は warning + skip（worktree 削除は完了済み）

### 不足点 / 運用穴

1. **workflow.db のレコードが cleanup 後に残る**。これは history 残しとしては正しいが、`workflow.db` が肥大化していくのに **GC コマンドが存在しない**（30 日経過 errors / idempotency の GC は `_handle_cleanup` 末尾にあるが workflows 本体は対象外）。
2. **`--cancel-reason` 無しの cleanup で Notion Workflows DB Status が更新されない**。worktree だけ消えて Notion 側は Phase 4 のまま残る → ダッシュボードに「アクティブだが worktree 無い」状態のゴーストが残る可能性。`cleanup` 時に reason 必須化、または「reason 未指定なら `cleanup-without-reason`」のようなデフォルト値を流す改善が考えられる。
3. **`--stale` は active 外 worktree を全削除する強い動作**。`--cancel-reason` のような Notion 同期ステップが無く、Notion 側は arbitrarily な状態のまま放置される。dogfooding 中に間違えて叩くと履歴がズレる。`--stale` に `--dry-run` 相当の表示を入れる、または `--sync-notion` フラグで Notion 側を Canceled 化するオプションが欲しい。

---

## 5. LLM Gateway audit log の観察

Phase 2 enforcement 判断の直接材料となるので独立セクションで掘る。

### 出力先 / 粒度

- 実装: `hokusai/llm_gateway/interceptor.py:_emit_audit`
- **出力先**: Python 標準 logger の `logger.info("llm_gateway_audit %s", json.dumps(entry, ...))` 1 行のみ
- **永続化先**: 無し（SQLite `audit_logs` テーブルは schema 存在するが interceptor からは挿入されない）
- **対象 event**: 全 LLM 呼び出し（`enabled=True` 時）、`decision=log` / `reason=phase1_log_only` を吐く
- **記録内容**:
  - `event="llm_gateway_decision"`, `timestamp`, `decision`, `reason`
  - `context`: provider / model / purpose / workflow_id / phase / metadata
  - `prompt_length`, `prompt_hash`（**本文は保存しない**、SHA256 16桁 hex のみ。secret / PII を log にこぼさない設計）
  - `policy_hits`（Phase 1 では log のみ、Phase 2 enforcement の前段として hit list を残す）
  - `config_snapshot`（enabled / log_only / dry_run / audit_log_enabled の実値スナップショット）

### dogfooding でのサンプル

- 本 workflow の `audit_logs` テーブルは **0 行**。
- 実行時 logger 設定によっては stderr にも出ない（root logger level / handler 次第）。dogfooding で実際の log line を抽出するには `--log-file` 指定が必要。

### Phase 2 enforcement に向けた評価

- 現状: **audit log の永続化が無いまま enforcement 機能を実装しても、なぜ block されたかを後追いできない**
- 必須前提: SQLite `audit_logs` への挿入（`workflow_id`, `phase`, `action='llm_gateway_decision'`, `status=decision`, `details_json` にエントリ全体）
- 望ましい追加: Operations Console から audit 一覧を見られる UI（既に dashboard モジュールがあるので拡張可能）
- 副次: prompt_hash だけでは debug にならないので、**「Hash 衝突時に元 prompt を別の保護領域に保存する optional モード」** も別途検討対象

---

## 6. 結論: Phase 2 enforcement の優先順位判定材料

Issue #72 の本来の目的は「観察結果から Phase 2 enforcement の優先順位を決める」こと。観察を踏まえた箇条書き判定材料：

### Phase 2 enforcement 着手前に潰すべき前提条件

- [ ] **audit log の永続化**: `LLMGatewayInterceptor._emit_audit` を SQLite `audit_logs` へ挿入する経路を追加。これ無しに enforcement に進むと運用調査が成立しない。**最優先**。
- [ ] **Notion DB share 健全性チェック**: `hokusai start` 冒頭で Workflows / PR / Memory DB 各 ID に対し軽量 query を投げ、404 を早期検出して warning を出す。dogfooding 失敗の最大原因がここだった。**高優先**。
- [ ] **outbox 失敗の可視化**: `hokusai status` に「outbox pending / failed 件数」サマリを表示。`workflow.db` 直叩きでしか分からない状態は運用穴。**中優先**。

### Phase 2 enforcement と独立に直しても良い小穴

- [ ] `HOKUSAI_SKIP_NOTION` の profile 化 / 切替時 warning
- [ ] `hokusai cleanup` で `--cancel-reason` 未指定時に Notion Status 更新を一切しない挙動の見直し（ゴースト発生源）
- [ ] `hokusai list` の `--profile` 解決の一貫性（default_profile 適用範囲）
- [ ] `hokusai prime` の「空状態」表示で構成要素ごとの設定有無を明示
- [ ] workflow.db `workflows` 行の長期 GC 経路

### Phase 2 enforcement そのもの

- 上記 audit 永続化が済んだ後、`policy_hits` を `decision=block` に切り替えるロジックは **`interceptor.py:91-99` の数行**で済む見込み（`_config.log_only=False` 時に decision を変える）。
- enforcement 起動条件は `LLMGatewayConfig.log_only=False` を profile config で切り替えられるようにし、profile 単位で段階的に on にする設計が現実的（`hokusai` profile のみ先行 enforce、他は log_only 維持など）。
- enforcement 適用後の **fail-open 原則**（Notion 障害が PR 作成を止めないのと同じ思想）を明文化しないと、dogfooding session が止まるリスクあり。

### dogfooding 自体の改善メモ

- 次回 dogfooding 前に Workflows DB の share を完了させて、Notion 同期が成功する状態で再観察する必要がある（本 session では同期穴の観察にしかなっていない）
- `prime` の handover_note 遡及検証は **Supersedes を意図的に張った 2 世代 workflow** で別途試す必要あり

---

## 7. Phase 2 enforcement 再観察 (2026-05-27, v0.5.0)

v0.5.0 リリース後の再観察。前回 (§1-6) で挙げた enforcement 前提条件のうち、audit log 永続化 / `HOKUSAI_SKIP_NOTION` profile 化 / cleanup `--stale --dry-run` / fail-fast モードは v0.5.0 までに解消済み（PR #80 / #108 / #110 / #112 / #114）。残る穴を「enforcement 経路を実際に通して確認する」観点で観察した。

### 観察手順

| Step | 操作 | 目的 |
|---|---|---|
| 1 | `~/.hokusai/configs/hokusai.yaml` に `llm_gateway: { enabled: true, log_only: true, audit_log_enabled: true }` を一時追加 | audit log 永続化が機能するか |
| 1 | `dispatch_via_gateway(workflow_id="wf-dbe7b6cd", phase=7, provider="claude_code", ...)` を 1 回 Python から呼ぶ | interceptor 経路を確実に通す |
| 1 | `sqlite3 ~/.hokusai/profiles/hokusai/workflow.db "SELECT * FROM audit_logs"` で行確認（実際の DB は profile の `config.database_path` を参照、カレント `workflow.db` 誤参照を避けるためフルパスで書く） | SQLite 永続化の裏取り |
| 2 | yaml を `log_only: false`, `allowed_providers: ["codex"]` に変更 | enforcement on の状態を作る |
| 2 | `provider="claude_code"` (allowlist 非含) で dispatch | `LLMGatewayBlockedError` raise を確認 |
| 2 | `provider="codex"` (allowlist 含) で dispatch | 透過動作（例外なし、audit `decision=log`）を確認 |

### 実観察

#### Step 1: audit log SQLite 永続化

- ✅ `audit_logs` テーブルに 1 行記録された:

  ```json
  {
    "event": "llm_gateway_decision",
    "decision": "log",
    "reason": "phase1_log_only",
    "context": {
      "provider": "claude_code",
      "model": "claude-sonnet-4",
      "purpose": "dogfood_observation_step1",
      "workflow_id": "wf-dbe7b6cd",
      "phase": 7,
      "metadata": {"observation_id": "step1", "source": "dogfood-findings reobservation"}
    },
    "prompt_length": 48,
    "prompt_hash": "f80dce72c7aa3519",
    "policy_hits": [],
    "config_snapshot": {"enabled": true, "log_only": true, "dry_run": false, "audit_log_enabled": true}
  }
  ```

- ✅ prompt 本文は保存されず、`prompt_length` + 16桁 hex hash のみ（§5 の PII 防御方針どおり）
- ✅ `workflow_id` / `phase` が context に正しく載る → 後追い分析の単位として利用可能

#### Step 2: enforcement 配線（block raise + 透過の両方）

audit_logs の SQLite 列 / details_json 対応関係: テーブル列 `status` に decision 値が入り、`details_json` 内にも同値が `decision` フィールドとして格納される（SELECT 例: `status='block'` ＝ `json_extract(details_json, '$.decision')='block'`）。以下は SQLite 列名で記載。

- ✅ Test A (`provider="claude_code"`, allowlist=`["codex"]`):
  - `LLMGatewayBlockedError` が raise された
  - `policy_hits=('unknown_provider',)`, `reason="phase2_policy_block"`
  - audit_logs に `status='block'`（= `details_json.decision='block'`）行が記録される
  - 例外メッセージは `provider` / `purpose` / `policy_hits` / `reason` のみで prompt 本文を含まない（§5 の PII 防御方針どおり）
- ✅ Test B (`provider="codex"`, allowlist=`["codex"]`):
  - 例外なし、透過動作
  - audit_logs に `status='log'`（= `details_json.decision='log'`）, `policy_hits=[]` 行が記録される
  - `config_snapshot.log_only=False` が `details_json` に記録される（後追いで「enforce 環境下の log だった」と分かる）

### v0.5.0 で確認できたこと

| 項目 | 状態 | コード位置 |
|---|---|---|
| `_emit_audit` → SQLite `audit_logs` INSERT | ✅ 機能 | `hokusai/llm_gateway/interceptor.py:209-` (Issue #80 / M0.1) |
| `decision="block"` → `LLMGatewayBlockedError` 上位伝播 | ✅ 機能 | `hokusai/llm_gateway/dispatch.py:181-187` (Issue #102) |
| 許可 provider 透過 + audit 残存 | ✅ 機能 | `hokusai/llm_gateway/interceptor.py:138-153`（decision 判定 138-148 + `_emit_audit` 呼び出し 150-153）(M1.1 / #86) |
| fail-open（gateway 内部例外を握り潰す） | ✅ コード上明示 | `hokusai/llm_gateway/dispatch.py:193-198` (要件 §4.4) |

### 運用穴のステータス

§7 観察時点（v0.5.0）で確認した F1–F4 は **全て後続 PR で解消済み**。残る未解消なし。

### 解消済み（後続 PR）

- **F1: LLM Gateway を env で一時 enable できない** → **PR #122 で解消**。`HOKUSAI_LLM_GATEWAY_ENABLED` を truthy/falsy で指定すると yaml/default を上書きする env override を `_parse_llm_gateway_config` に追加（truthy: `1` / `true` / `yes` / `on`、falsy: `0` / `false` / `no` / `off`、case-insensitive）。dogfooding 観察時に yaml 編集なしで一時 enable / disable できる。
- **F2: policy 未設定で enforce on にすると事実上 no-op** → **PR #125 で解消**。`hokusai llm-gateway-setup` サブコマンドを追加し、現 profile の LLM Gateway 設定を診断する。判定は interceptor の真の policy_hits 生成経路に基づく: **`allowed_providers` が `None` なら no-op 警告**（`allowed_models.*` のみ設定しても `interceptor._evaluate_policy_hits` は `context.model=""` のとき allowed_models 系評価を skip するため、ClaudeCodeClient (model="") 経由の呼び出しは policy_hits 常時空になる、PR #125 Copilot Round 6 指摘）。`[]`（明示空 = deny-all 意図）は別カテゴリの警告（全 LLM 呼び出しが block される）として扱う（Round 1 指摘）。`allowed_providers` 設定済みで `allowed_models.*` 両方空なら ℹ️ info 注記（provider allowlist のみで動作）。yaml 直接書き込みは安全のため避け、user 自身の編集を促す設計。warning ありは `exit 1` で CI フックにも組み込み可能。
- **F3: audit_logs を CLI から覗く経路が無い** → **PR #123 で解消**。`hokusai audit list` / `hokusai audit show <id>` サブコマンドを追加し、`SQLiteStore` に `list_audit_logs(workflow_id/phase/action/status/limit)` と `get_audit_log(id)` の helper を実装。`--output json` で raw 表示も可能。`sqlite3` 直叩きから解放され、運用調査・自動化テストの導線が整った。
- **F4: 3 client の workflow_id 伝播未配線** → **PR #120 / #121 で解消**。PR #120 (案 A) で 3 client (claude_code / codex / gemini) の `execute_skill` / `execute_prompt` / `review_document` / `generate` メソッドに `workflow_id: str | None = None` / `phase: int | None = None` 引数を追加し、`_invoke_llm_gateway_interceptor` 経由で `dispatch_via_gateway` まで伝播する経路を整備。PR #121 (案 A2) で各 phase node (`phase2_research` / `phase3_design` / `phase4_plan` / `phase5_implement` / `phase7_review` / `phase8/review_fix` / `utils/cross_review`) から `state["workflow_id"]` と該当 phase 番号を client に渡す配線を完成。これにより実 phase node 経由の LLM 呼び出しでも `audit_logs.workflow_id` が SQLite に書き込まれる状態に到達した（dispatch.py docstring の「後続 PR の課題」は両 PR で閉じた）。

### 次のアクション候補（優先順）

1. ~~F2 の wizard~~ → **PR #125 で解消**。
2. ~~**F1 / F3 / F4 解消後の再観察 dogfooding**~~ → **§8 で軽量 end-to-end 検証完了**。実 `hokusai start` で 1 workflow 完走させる重い dogfooding は引き続き次マイルストーンとして残る（優先度: 中）。

§7 で記録した運用穴 F1–F4 は本 PR (#125) で全て閉じた。**v0.5.0 dogfooding サイクルは一段落**。

### Phase 2 enforcement の v0.5.0 評価

- **コード上は完成**: audit 永続化 / block raise / fail-open / 許可透過の 4 経路は全て期待通り動作
- **運用は未完成**: F1-F4 が揃って初めて「safely enforce on にできる profile」が成立する
- **段階導入の妥当性**: profile 単位での切り替え方針 (§6) は引き続き正しい。次は `hokusai-enforce` のような専用 profile で 1 workflow 実走させる dogfooding が次マイルストーン。

---

## 8. F1 / F3 / F4 解消後の end-to-end 再観察 (2026-05-28)

PR #122 (F1: env override) + PR #123 (F3: audit CLI) + PR #120 / #121 (F4: workflow_id 伝播) のマージ後、3 つの運用穴が揃って埋まったことを実機で end-to-end 検証した。**§7 で「次マイルストーン」と書いた `hokusai-enforce` 専用 profile を作らずとも、3 PR の組み合わせだけで実用的な観察パスが成立する** ことを確認できた。

### 観察手順（再現可能）

1. **F1**: yaml を編集せず env 経由で gateway を一時 enable
   ```bash
   export HOKUSAI_LLM_GATEWAY_ENABLED=1
   export HOKUSAI_ACTIVE_PROFILE=hokusai
   ```
2. **F4**: 実 `ClaudeCodeClient.execute_prompt` を呼び（subprocess のみ mock）、`workflow_id` / `phase` を末端 helper まで伝播させる
   ```python
   from unittest.mock import patch, MagicMock
   from hokusai.integrations.claude_code import ClaudeCodeClient

   client = ClaudeCodeClient.__new__(ClaudeCodeClient)
   client._claude_path = "/bin/true"
   client.working_dir = "/tmp"

   with patch("hokusai.integrations.claude_code.ShellRunner") as runner_cls:
       runner_cls.return_value.run.return_value = MagicMock(
           returncode=0, stdout="ok output", stderr="",
           duration_ms=0, success=True,
       )
       client.execute_prompt(
           prompt="end-to-end reobservation",
           workflow_id="wf-reobservation-001",
           phase=2,
       )
   ```
3. **F3**: 結果を CLI 経由で確認（`sqlite3` 直叩き不要）
   ```bash
   hokusai audit list --workflow-id wf-reobservation-001 --action llm_gateway_decision
   hokusai audit show <id>
   ```

### 実観察

- ✅ `hokusai audit list --workflow-id wf-reobservation-001 --action llm_gateway_decision` で 1 行が表示された
  ```
      id  created_at           workflow_id     phase  status    action
  --------------------------------------------------------------------------------
       4  2026-05-28T15:17:07  wf-reobservati      2  log       llm_gateway_decision
  ```
- ✅ `hokusai audit show 4` で `details_json` を整形表示。**4 つの観点が同時確認**:
  - `config_snapshot.enabled=true` → **F1 の env override が機能**
  - `context.workflow_id="wf-reobservation-001"` / `context.phase=2` → **F4 の client→helper 配線が機能**
  - SQLite 行として CLI から見える → **F3 の CLI helper が機能**
  - `prompt_hash` 16 桁 hex のみ保存（本文なし）→ §5 の PII 防御方針も維持

### 評価 + 次マイルストーン

- **§7 で「コード上は完成、運用は未完成」と書いた状態が、3 PR で「運用上も実用 OK」まで到達**した。専用 enforce profile を作らずとも、env override 1 行で観察開始 → CLI でトレース可能。
- **残る運用穴**は §7 「残る運用穴」サブセクションで列挙した F2 (policy wizard) のみ。これは「policy 未設定で `log_only=false` にすると enforcement が事実上 no-op」という事故 1 回が踏まれてから対応で十分な優先度。
- **次マイルストーン**は「実 `hokusai start` で 1 workflow 完走させ、各 phase の `audit_logs` 行が PR #121 の配線通り `workflow_id`/`phase` で埋まることを観察」する **重い dogfooding**。本 §8 で軽量検証は完了しているため、優先度は中。
- ↑ **§9 で着手**: 実 `hokusai start` を 90 秒だけ起動して、**実 phase node (`phase2_research_node`) entry point から** `audit_logs` に `workflow_id`/`phase` が記録されることを実証した（subprocess 起動「前」までの配線通過を厳密に検証、CLI 実起動は間接的証拠のみ）。

---

## 9. 重い dogfooding: 実 phase node entry point からの audit 配線観察 (2026-05-28, v0.5.1)

§8 で「`ClaudeCodeClient.execute_prompt()` を **Python から直接** 呼んだ end-to-end（`ShellRunner` のみ mock）」を完了したのに対し、本 §9 では `hokusai start` から **実 phase node (`phase2_research_node`) entry point 経由** で audit_logs まで届くことを観察した。F4 配線 (PR #120 / #121) が **実 phase node の呼び出しチェーン** で end-to-end 動作することを実証する位置付け。

**厳密な範囲** (PR #128 Copilot Round 1 指摘): `_run_claude_code()` は `_invoke_llm_gateway_interceptor()` を `ShellRunner.run()` の **前** に呼ぶ（`hokusai/integrations/claude_code.py:217-237`）。したがって audit 行の存在だけからは「Claude CLI subprocess が実際に起動した」とは厳密には言えない。本 §9 で実証したのは **「phase node → client → helper → SQLite まで配線が通過した」** ことで、subprocess 起動は別途間接的な観察（worktree 作成 / 90 秒間 background が継続実行 / Phase 2 完走しなかった）で蓋然性が高いと評価する。

### 観察手順

| Step | 操作 |
|---|---|
| 1 | dogfood-test 用の軽量 GitHub Issue を作成 (`gh issue create` で [#127](https://github.com/shigenoko/hokusai/issues/127)) |
| 2 | `HOKUSAI_LLM_GATEWAY_ENABLED=1 HOKUSAI_ACTIVE_PROFILE=hokusai uv run hokusai start <issue_url>` を 90 秒だけ background 実行 |
| 3 | 90 秒経過後、`hokusai audit list --action llm_gateway_decision` で audit_logs を観察 |
| 4 | `hokusai audit show <id>` で details_json を確認 |
| 5 | background プロセスを `TaskStop` で kill |
| 6 | `hokusai cleanup <new_wf>` で worktree 削除、`gh issue close` で test Issue を閉じる |

### 実観察結果

新規 workflow `wf-f373fac6` が Phase 2 まで到達し、`hokusai audit list` で 1 行記録を確認:

```
    id  created_at           workflow_id     phase  status    action
--------------------------------------------------------------------------------
     5  2026-05-28T18:06:16  wf-f373fac6         2  log       llm_gateway_decision
```

`hokusai audit show 5` の `details_json` 抜粋:

```json
{
  "id": 5,
  "workflow_id": "wf-f373fac6",
  "phase": 2,
  "action": "llm_gateway_decision",
  "status": "log",
  "details": {
    "decision": "log",
    "reason": "phase1_log_only",
    "context": {
      "provider": "claude_code",
      "model": "",
      "purpose": "execute_prompt",
      "workflow_id": "wf-f373fac6",
      "phase": 2,
      "metadata": {}
    },
    "prompt_length": 806,
    "prompt_hash": "0451be88c0b573d6",
    "policy_hits": [],
    "config_snapshot": {
      "enabled": true,
      "log_only": true,
      "dry_run": false,
      "audit_log_enabled": true
    }
  }
}
```

### 確認できたこと（§8 との差分）

- ✅ **実 phase node entry point からの prompt 組み立て**: `prompt_length=806` / `prompt_hash="0451be88c0b573d6"` は **phase2_research_node が組み立てた本物の research prompt**（task_url + 構成テンプレート）の SHA256 16 桁。§8 は `ClaudeCodeClient.execute_prompt` を Python から直接呼んでいたため prompt は固定文字列 (`"end-to-end reobservation..."`, length=70) だったが、§9 では実 phase node から実 prompt が helper まで届いていることが分かる。
- ✅ **phase node → client → helper → SQLite の完全 e2e（subprocess 起動「前」まで）**: `phase2_research_node` が `state["workflow_id"] = "wf-f373fac6"` を `claude.execute_prompt(workflow_id=..., phase=2)` で渡し、`_invoke_llm_gateway_interceptor` → `dispatch_via_gateway` → `LLMGatewayInterceptor.intercept()` → `_emit_audit()` → SQLite `audit_logs` INSERT までの全配線が **3 段階で検証完了**（unit test / 軽量 e2e / 実 phase node e2e）。
- ⚠ **subprocess 起動証明は間接的**: 上記の通り audit 行は subprocess 起動 **前** に書かれる仕様 (`hokusai/integrations/claude_code.py:217-237`) なので、audit log の存在だけからは Claude CLI が実起動したとは厳密に言えない。間接的証拠としては (a) Phase 1 完了で `~/.hokusai/profiles/hokusai/worktrees/Default_wf-f373fac6` が作成された、(b) 90 秒間 background プロセスが継続実行された（audit INSERT のみなら数秒で完了するはず）、(c) Phase 2 が完走しなかった = LLM 呼び出しを待っていた蓋然性が高い、の 3 点。完全証明には `Claude Code実行完了:` ログ確認が必要だが、本 §9 では 90 秒で kill したため未確認。
- ✅ **F1 env override も同時動作**: `config_snapshot.enabled=true` だが `hokusai.yaml` には `llm_gateway` セクションなし → `HOKUSAI_LLM_GATEWAY_ENABLED=1` env override (PR #122) が yaml/default を上書きして enable した結果。
- ✅ **F3 CLI helper も同時動作**: `hokusai audit list/show` (PR #123) で sqlite3 直叩きなしに観察完了。

### Phase 1 観察に関する補足

90 秒では Phase 2 の Claude Code 呼び出しが進行中の段階で kill された（Phase 2 完了せず）。Phase 1 (worktree 作成 + Notion 同期) は LLM Gateway 経路を通らない（直接 LLM 呼び出しは Phase 2 から）ため、`audit_logs` には Phase 1 の行は記録されない（PR #121 配線の対象外。仕様通り）。

### 既知の運用穴の再観察

cleanup 時に Notion 404 警告（`Could not find database with ID: 36085495-565d-8187-ba56-f5bf5a8d3abd`）が出た。これは §1 で記録した「Workflows DB が integration "HOKUSAI" に share されていない」という運用穴で、本 dogfooding とは無関係。F1-F4 の解消対象外で、別途 Notion 側の手作業（DB share）が必要。

### v0.5.1 dogfooding サイクルの完結 (F1-F4 範囲)

§7 で記録した **F1-F4 は後続 PR (#120-#125) で全て解消**され、§8 で軽量 e2e、§9 で実 phase node entry point からの audit 配線通過を実証。**「Phase 2 enforcement のコード配線 + 運用配線 + 実 phase node からの audit 配線」が 3 段階で全て検証完了**した。これで v0.5.1 dogfooding サイクルが一段落。

**F1-F4 に関する残る運用穴なし**（PR #128 Copilot Round 1 指摘で narrow）。ただし以下は別管轄で未解消:

- §1 で記録した **Notion DB share 未完了**: `Workflows DB ID` が integration "HOKUSAI" に share されていないため、`hokusai cleanup` 時等で 404 警告が発生する。本 dogfooding §9 でも cleanup 時に再現（[#127](https://github.com/shigenoko/hokusai/issues/127) close 前の cleanup ログ参照）。これは Notion 側の手作業（DB share 設定）が必要で、F1-F4 の解消対象外。
- Claude CLI subprocess 起動の完全証明: 本 §9 では時間切り (90 秒) のため `Claude Code実行完了:` ログを観察せず終了。完全証明には完走 dogfooding（数十分〜数時間）が必要だが、3 段階検証で配線品質は十分。

### 次マイルストーン (v0.6 以降) の議論起点

v0.5.1 で F1-F4 が解消され Phase 2 enforcement の配線が 3 段階で検証された後、**次に取り組むべき強化点の方向性議論**は別ドキュメント [docs/roadmap-gbrain-inspirations.md](roadmap-gbrain-inspirations.md) に分離した。GBrain (AI agent 用長期記憶エンジン) 調査から「Prime v2 (引用つき合成 / gap analysis)」「Doctor / Status 一画面化」「Operation Registry」等の優先順位を整理しており、v0.6 のスコープ合意形成の土台として参照する。

---

## 10. Prime v2 (MVP-1/2/4) の実環境 dogfooding (2026-05-29)

[docs/design-prime-v2.md](design-prime-v2.md) §8.1 の MVP-1 (FTS5 index 土台, PR #134) / MVP-2 (`--query` + active context backfill, PR #135) / MVP-4 (`--include-gaps` gap analysis 3 種, PR #136) をマージ後、HOKUSAI 本体 profile (`hokusai`) の既存 workflow `wf-dbe7b6cd` (current_phase=7) を対象に実 `hokusai prime` を走らせて観察した。**コード変更なしの観察ログ**。

### 観察手順

| Step | 操作 |
|---|---|
| 1 | `wf-dbe7b6cd` の SQLite 状態を確認 (`audit_logs` 5 件 / `notion_sync_outbox` 12 件 pending / `prime_index` テーブル不在) |
| 2 | `uv run hokusai prime wf-dbe7b6cd --profile hokusai --include-gaps` |
| 3 | `uv run hokusai prime wf-dbe7b6cd --profile hokusai --query "dogfooding"` |
| 4 | `HOKUSAI_LLM_GATEWAY_ENABLED=1 ... --include-gaps --output json` で `audit_log_silence` の誤検出有無を確認 |

### 実観察結果

- ✅ **MVP-1 migration が既存 DB で無事走った**: `wf-dbe7b6cd` の `workflow.db` は MVP-1 マージ前 (2026-05-28 18:07) に最終更新されており `prime_index` テーブルが無かったが、prime 初回起動時に `SQLiteStore._init_db()` が FTS5 virtual table + shadow tables (`prime_index_data` / `_idx` / `_content` / `_docsize` / `_config`) + `prime_index_meta` を作成。DDL に `source_type UNINDEXED` (PR #134 Round 1 修正) が含まれることも確認。**既存ユーザー DB の前方互換が実証された**。
- ✅ **MVP-4 `notion_outbox_pending` gap が正しく発火**: outbox に 12 件 pending がある状態で `--include-gaps` を実行すると、「未確定 / 不足情報 (gap analysis)」セクションに `notion_outbox_pending` が 1 件出力され、件数 (12 件) と Operations Console 同期再送ボタンへの導線が表示された。
- ✅ **gap section が active context 空でも出力される**: 本 workflow は Notion DB ID env が未設定 (`HOKUSAI_NOTION_PROJECT_MEMORY_DB_ID` 等) で active context が全カテゴリ空 (`_active な workgraph context はありません_`) だが、has_any=False の早期 return パスでも gap section が描画された (MVP-4 で early-return パスに `_render_gap_section` を追加した修正が実環境で機能)。
- ✅ **`audit_log_silence` の誤検出なし**: `wf-dbe7b6cd` は `audit_logs` を 3 件持つ (workflow 別集計: dbe7b6cd=3 / f373fac6=1 / reobservation-001=1)。`HOKUSAI_LLM_GATEWAY_ENABLED=1` で Gateway を有効化して `--include-gaps` を再実行しても gap kinds は `["notion_outbox_pending"]` のみで、`audit_log_silence` は発火しなかった。**scoped 検出 (workflow_id 絞り) が正しく機能し、audit 行がある workflow を誤って silent 判定しない**ことを実証。
- ✅ **MVP-2 `--query` は動作するが検索対象が空**: `--query "dogfooding"` は例外なく完走し「検索結果（query: `dogfooding`）」セクションを出力したが、結果は「_該当する記録はありません_」。これは active context (Notion DB) が未設定で `prime_index` に backfill される entry が 0 件のため。**正しい挙動**だが、§1 で記録した運用穴 (Notion DB share / env 未設定) が解消されない限り MVP-2 の検索価値は出ない。

### 評価 + 次のアクション候補

- **SQLite-backed な gap (`notion_outbox_pending` / `audit_log_silence`) は Notion 接続なしで即座に価値を出す**。現 dogfooding 環境 (Notion DB 未配線) でも outbox の滞留を 1 コマンドで可視化できた点は実用的。
- **Notion-backed な機能 (`--query` の検索対象 / `unresolved_review_issue_open` gap) は §1 運用穴に依存**。Prime v2 の「引用つき合成」価値をフルに引き出すには、Notion DB ID env の設定 + integration share が前提。これは [#130](https://github.com/shigenoko/hokusai/pull/130) で手順を docs 化済みだが、実環境ではまだ env 未設定。
- ⚠ **小さな観察**: prime diagnostics が `HOKUSAI_NOTION_PROJECT_MEMORY_DB_ID` (汎用名) を表示している。profile config で `_4HOKUSAI` 等の profile-tagged env 名を使う設定にしていない場合の既定動作と思われる。誤誘導ではないが、profile 運用時に「どの env を設定すべきか」が分かりにくい可能性 (別途検証対象)。
- **次マイルストーン候補**: (a) 残り gap 4 種 (MVP-5: `missing_verification_command` / `pending_gate_blocking` / `phase4_plan_missing` / `supersedes_chain_broken`) のうち SQLite-backed で完結するもの (`phase4_plan_missing` 等) を優先実装、(b) MVP-3 (citation リッチ整形) は Notion DB 配線後に dogfooding して必要要件を固めてから着手。

---

## 11. v0.7–v0.10 新コマンド (operations / graph / eval) の実環境 dogfooding (2026-05-31)

v0.7.0–v0.10.0 で追加した **Step 2/3/4/5** の CLI（`operations` / `graph` / `eval` と `profile doctor --deep`）を、HOKUSAI 本体 profile (`hokusai`) の既存 DB に対して実行して観察した。**コード変更なしの観察ログ**。叩いたコマンドは [Appendix A.3](#appendix-a3-11-で叩いたコマンド-2026-05-31) を参照。

### 観察手順

| Step | 操作 |
|---|---|
| 1 | `hokusai operations list` / `operations run runtime.health` / `operations run notion.outbox_status` |
| 2 | `hokusai graph status` / `graph recurring` |
| 3 | `hokusai eval list` / `eval export > baseline.json` / `eval gate --baseline baseline.json` |
| 4 | `hokusai profile doctor hokusai --deep`（Step 2 の runtime ヘルス統合）|
| 5 | 実 DB に v0.8–v0.10 の新テーブル（`workgraph_edges` / `review_issues` / `work_items` / `eval_captures`）が migration されているか確認 |

### 実観察結果

- ✅ **新テーブル 4 種すべてが既存 DB に前方互換で migration された**: MVP-1（prime_index）と同様、`SQLiteStore.__init__` 起動時に `workgraph_edges` / `review_issues` / `work_items` / `eval_captures` が `CREATE TABLE IF NOT EXISTS` で作成された。既存ユーザー DB を壊さずに v0.8–v0.10 機能が乗ることを実証。
- ✅ **runtime ヘルスの検出ロジックが CLI 横断で一貫**: `operations run runtime.health`（Step 3）/ `profile doctor --deep`（Step 2）/ Operations Console が**同一の `notion_outbox_pending` gap（pending 12 件）を返す**。Step 2 第3スライスで導入した共通 handler `compute_runtime_health()` が単一の真実源として効いていることを実環境で確認。`operations run` の stdout は JSON 専用（warning は stderr）で pipe 可能、未知 operation は stderr + 案内表示も確認。
- ✅ **eval export → gate サイクルが実 audit データで完結**: `eval list` は LLM Gateway が `audit_logs` に残した 5 件の `llm_call` fixture を phase / purpose / decision 別に集約表示。`eval export > baseline.json`（5 fixtures）→ `eval gate --baseline baseline.json` で「regressions 0 / improvements 0」を得た。**LLM Gateway audit が既にある環境では追加配線なしで即座に eval gate が回る**。
- ⚠ **graph / recurring / eval-verification は「既存 workflow」では空**: `graph status` は `has_work_item: 38` のみ（`has_pr` / `supersedes` / `has_review_issue` / `resolved_by` は 0）、`graph recurring` は空、`eval list` の `verification` capture も 0 件。これは **durable な `review_issues` / `work_items` / `eval_captures` への永続化が drain hook（v0.8 第3 / v0.10 第2）に依存**するため。drain は新規 workflow 実行時にしか走らず、**それ以前に完了/保存された既存 workflow の state JSON はこれらの durable テーブルにバックフィルされない**。`has_work_item` だけ 38 件あるのは、`graph build` が state の `pending_work_items` からも抽出する経路（v0.8 第2）を持つため（Notion 未配線でこの workflow は drain されず pending が state に残存）。

### 評価 + 次のアクション候補

- **SQLite-backed な運用可視化（operations / doctor --deep / eval gate）は Notion 接続なしで即値**。outbox 滞留・運用ギャップ・LLM 呼び出し fixture を 1 コマンドで可視化でき、CI 退行 gate（`eval gate --fail-on-regression`）も既存 audit で回る。Step 2/3/4 の「既存配線を活かす」方針が実環境で結実している。
- ⚠ **最大の運用ギャップ = 既存データのバックフィル不在**: v0.8–v0.10 の durable テーブル（review_issues / work_items / eval_captures）は**前向き（forward-only）**にしか埋まらない。既存ユーザーが `graph recurring` / durable な `has_review_issue` / `resolved_by` / verification capture の価値を得るには、(a) 新規 workflow を回す、または (b) **`workflows.state_json` から一括バックフィルする one-shot コマンド**が要る。後者は決定的・SQLite-backed・既存配線（`extract_*` 純関数の再利用）で 1 PR に収まる、**次スライスの有力候補**。
- ⚠ **Notion-backed な edge / gap は引き続き §1 運用穴に依存**: `has_pr` edge（state の `pull_requests`）/ `unresolved_review_issue_open` gap / `--query` 検索は Notion DB 配線・PR 実績が前提。dogfooding 環境ではこれらが空のままなのは §10 と同じ構造。
- **次マイルストーン候補**: (a) 上記**バックフィルコマンド**（`hokusai graph backfill` / `eval backfill` 等、既存 workflow の state から durable テーブルを再構築）、(b) `operations` registry の mutating operation 解禁・read-only MCP/HTTP 化（Step 3 後続）、(c) Step 4 の phase 入出力 capture 拡張（verification 以外の Phase 2/3/4/7/8）。

---

## 12. `backfill` 後の再観察 — durable データ投入後の graph / recurring (2026-05-31)

§11 で挙げた「durable テーブル forward-only」ギャップを `hokusai backfill`（PR #157）で解消したので、**実 DB に backfill を適用した後**の `graph status` / `graph recurring` / durable テーブルの中身を再観察した。

### 観察手順

| Step | 操作 |
|---|---|
| 1 | `hokusai backfill`（本体 profile の全 workflow を durable 化、実行済み）|
| 2 | `hokusai graph status` / `graph recurring` |
| 3 | `review_issues` / `work_items` の中身を SQLite から直接確認 |

### 実観察結果

- ✅ **backfill で `has_review_issue` edge が出現**: `graph status` が `has_work_item: 38` のみ（§11）から **`has_review_issue: 4` + `has_work_item: 38`（計 42 edge）** に増えた。forward-only だった durable データが後追いで graph に反映されることを実証。
- ✅ **review_issues が想定より rich**: backfill された 4 件は **verification_failure 1 件 + Phase 7 `final_review` 3 件（review rule `HQ05` / `HQ04` / `HQ02`）**。`persist_review_issue_payloads` は source を問わず汎用に永続化するため、`pending_review_issues` に乗っていた Phase 7 のコードレビュー指摘（HQ ルール）も durable 化された。**Step 4 第2 の verification capture より広いカバレッジ**が backfill 経由で得られた。
- ⚠ **`graph recurring` は単一 workflow では発火しない（仕様通り）**: backfill 後も recurring は空。4 件の review issue がすべて 1 workflow（`wf-dbe7b6cd`）由来で `COUNT(DISTINCT workflow_id)=1` のため。recurring は **同一 content signature が ≥2 の異なる workflow にまたがる**ときのみ検出する設計なので正しい挙動だが、dogfooding 環境は workflow が 2 件（うち 1 件は review issue 0）しかなく recurring の価値を実証できない。**複数 workflow を回す or 複数 DB を集約しないと recurring は観測できない**（§10 と同じ「データ量依存」構造）。
- ⚠ **backfill の `work_items` カウント（380）は「処理 payload 数」で、実 unique 行は 38**: `hokusai backfill` の出力は `work_items 380` だが、`list_work_items` の実行数は **38**（status 全て `done` / phase 全て 4）。`pending_work_items` には同一 `(workflow_id, phase, title)` の work item イベント（upsert / claim / status_change / lease_release × retry）が**約 10 倍重複**して積まれており、durable table は `dedupe_key` で 38 行に正規化している。backfill 出力の件数は `persist_*_payloads` の戻り値（= upsert 呼び出し回数）であり、**実際に作られた unique 行数ではない**。誤誘導ではないが、「380 件の work item がある」と誤読され得る。

### 評価 + 次のアクション候補

- **backfill は実データで価値を実証**: 既存 workflow の Phase 4 plan / Phase 7 review 指摘が durable 化され、`graph status` に反映された。§11 の最大ギャップは実証的にクローズ。
- ⚠ **小改善候補**: `backfill` 出力の件数を「処理 payload 数」ではなく **durable table の実 row 増分（before/after の差）** にすると、`work_items 380 → 実 38 行` の乖離が解消し UX が正確になる。1 関数の戻り値変更 + 表示調整で収まる軽微な改善。
- **recurring / resolved_by のフル価値には複数 workflow が前提**: 単一案件の dogfooding DB では再発・解決の関係が出ない。実運用で複数案件を回すか、将来 `hokusai graph status` を複数 DB 横断で集約する機能があると recurring の真価が出る（中長期候補）。
- **次マイルストーン候補**: (a) backfill 件数の実 row 増分化（上記小改善）、(b) Step 4 の Phase 7 review 結果 capture（backfill で review_issues に既に入っているので、eval_captures 側にも `kind=review` で取り込めば eval gate が review 退行も拾える）、(c) §11 で挙げた operations の MCP/HTTP 化。

---

## 13. Step 3 read-only HTTP admin の実環境 dogfooding (PR #165, 2026-06-01)

§11/§12 で next-action 候補に挙げ続けた「operations の MCP/HTTP 化」が PR #162（operation 拡充）→ #164（共通実行 sink `execute_operation()`）→ #165（依存ゼロの HTTP admin）で実装され、GBrain ロードマップ Step 3 が完了した。マージ直後の HTTP admin を**初めて実環境で観察**し、ロードマップの核「**CLI / Dashboard / HTTP admin が同一 handler を単一経路で叩く**」が実データで成立するかを検証した。

### 観察手順

| Step | 操作 |
|---|---|
| 1 | `hokusai operations list` / `hokusai operations run runtime.health`（CLI 側ベースライン）|
| 2 | `hokusai operations serve --port 8765`（HTTP admin 起動、既定 `127.0.0.1` bind）|
| 3 | `GET /operations`（一覧）/ `GET /operations/runtime.health`・`GET /operations/workflow.list`（実行）|
| 4 | 異常系: 400（必須 param 欠落）/ 404（未知 op）/ 405（POST・HEAD）/ 400 redaction（`limit=evil<script>xyz`）|

### 実観察結果

- ✅ **同一 handler・単一経路をバイト一致で実証**: `runtime.health` の JSON は CLI（`operations run`）と HTTP admin で **`result` が完全一致**（`outbox_pending: 12` / `gaps[0].kind: notion_outbox_pending` まで同一）。ロードマップ §P1 の中核命題（CLI / Dashboard / HTTP admin が `resolve_read_only_operation` → `invoke_operation` の単一経路を共有）を実データで確認。
- ✅ **`GET /operations` が `input_schema` 込みの stable schema を返す**: CLI `list` がテキスト（name / scope / summary）なのに対し、HTTP は各 operation の `input_schema`（JSON Schema）まで返す。MCP / 外部 admin UI が動的にフォームを組める粒度で、HTTP 側の方が機械可読性が高い。
- ✅ **実 workflow データを read-only で取得**: `workflow.list` が実 DB の 2 workflow（`wf-f373fac6` phase 2 / `wf-dbe7b6cd` phase 7）を返却。副作用なしの「読むだけ」契約を維持。
- ✅ **異常系がすべて契約通り**: 400（`workflow.status` の `workflow_id は必須です` = static 文言を保持）/ 404（`available` に registry 由来の安全な op 名一覧）/ 405（POST）。
- ✅ **PR #165 の 2 修正が実環境で機能**: (1) **HEAD が 405 + 空ボディ**で返る（`do_HEAD` 追加。既定 501 を回避）。(2) **400 redaction が実値で機能**: `limit=evil<script>xyz` → `limit は整数で指定してください: '<redacted>'`。query param 生値が reflected されないこと（SonarCloud S5131）を実トラフィックで確認。

### 不足点 / 運用穴

- ⚠ **F13-1: server 自身の bound profile / config / version を introspect する手段が無い**: HTTP レスポンスにも起動バナー（`read-only operations HTTP admin: http://127.0.0.1:8765`）にも **どの profile / DB を読んでいる admin か**の情報が出ない。CLI は `Profile: hokusai (default_profile)` を毎回表示するのに対し、HTTP admin は無記名。複数 profile を別ポートで並走させると、どのポートがどの DB かを HTTP 越しに判別できない。`GET /operations` のメタに `profile` / `database_path`（または専用 `GET /meta`）を足すと運用調査が成立する。**優先度: 中**。
- ⚠ **F13-2: 軽量 liveness endpoint が無い**: `/` や `/healthz` は 404 になり、ヘルスチェックは `/operations`（全 operation を registry から構築して返す）に依存する。現状 registry 構築は軽いので実害は小さいが、uptime 監視・LB ヘルスチェックの定石（`/healthz` で 200）に乗れない。**優先度: 低**。
- 注: 認証なし・localhost bind・read-only は**設計通り**（ネットワーク到達性で保護）。外部公開する段階で初めて F13-1 の profile 表示と併せて認証が論点になる。

### 評価 + 次のアクション候補

- **Step 3 は実環境で価値を実証**: CLI と HTTP admin が同一結果を返すことで「単一経路」設計が机上でなく実データで成立。GBrain ロードマップ全 5 Step の主要機能が dogfooding 観察まで到達した。
- ⚠ **Notion-backed operation は依然 §1 運用穴に依存**: `workflow.status` / `review_issues.list_open` 等は SQLite で完結し観察できたが、Notion DB ID env 未設定のため Notion 横断の値（outbox の実 drain 等）はダミーのまま。§10/§11/§12 と同じ構造で、**次の本丸は推奨手順 ② の Notion env 設定 + integration share**。
- **次マイルストーン候補**: (a) **F13-1 introspection**（`GET /operations` メタに profile / database_path、または `GET /meta`）— 複数 profile 運用の前提、1 PR スコープ。(b) **Notion DB env 設定 + share**（推奨手順 ②、本丸ブロッカー。これ無しに Prime v2 `--query` / `has_pr` edge / Notion 横断 operation が実データで観察できない）。(c) read-only MCP 化（HTTP admin の次段、SDK 依存を足す判断とセット）。

---

## 14. Notion 同期が数週間 stuck していた真因 — env がゴミ箱の DB を指していた + health check の false positive (2026-06-01〜06-03)

推奨手順 ②（Notion env 設定 + integration share）の検証として、§10〜13 で一貫して「outbox に 12 件 pending が滞留」「Notion-backed 値がダミー」と記録してきた状態の**真因を特定し、解消まで完遂**した。本節は **初回の誤った診断と、その訂正までを正直に記録**する（dogfooding の学びの本体）。

> **訂正履歴**: 当初 (06-01) 本節は真因を「integration が DB に未接続」と結論したが、これは **誤り**だった。`POST /v1/search` の結果に DB が出なかったのを「未接続」と解釈したが、実際は **フィルタ無し search の先頭ページに入っていなかっただけ**で、`filter={object:database}` 付き search では両 DB が出る＝**接続済み**だった。06-03 の再調査で真因が **「env が Notion ゴミ箱(in_trash)の DB ID を指したまま」** と確定。PR #167（probe を query 化）と本節を本 PR で訂正する。

### 観察手順

| Step | 操作 |
|---|---|
| 1 | `~/.zshrc` の Notion env 永続化状況を確認 |
| 2 | API token で Workflows / PR DB を `retrieve` / `query` / `search` 比較（生 API）|
| 3 | 2 つの "HOKUSAI" integration（`_API_TOKEN` / `_4HOKUSAI`）と各 DB の `created_by` を照合 |
| 4 | `Notion-Version: 2025-09-03` で DB の `in_trash` / `data_sources` を確認 |
| 5 | env を live DB へ張替 → `check_db_share_health()` → `retry_pending()` で実 drain |

### 実観察結果（真因の特定）

- ⚠ **「env 未設定」は誤認だった**: `HOKUSAI_NOTION_*_4HOKUSAI`（token / DB ID）は **`~/.zshrc` に永続化済み**で token も有効。§10〜13 で「未設定」に見えたのは、**dogfooding 観察シェル（Bash tool = bash）が zsh の env を読まなかった**観察アーティファクト。
- ✅ **integration は接続済みだった（当初の「未接続」結論は誤り）**: config が使う `_4HOKUSAI` integration（bot id `35f8…`）は対象 DB の `created_by` 当人で、`filter={object:database}` 付き search にも対象 DB が出る。retrieve / page read / block children すべて 200。**接続は欠けていなかった**。
- ⚠ **真因 = env が Notion ゴミ箱(in_trash)の DB を指していた**: env の DB ID（`…8187…` / `…815b…`、title `HOKUSAI Workflows/Pull Requests DB`）を `Notion-Version: 2025-09-03` で retrieve すると **`in_trash: True`**。Notion のゴミ箱 DB は **retrieve は 200（メタデータは残る）だが query は 404 `object_not_found`** になる。これが「retrieve OK / query 404」の正体で、2026-05-23 以降 outbox を無言で滞留させていた真因。
- ⚠ **同一スキーマの live DB が別 id で存在**: ゴミ箱 DB と**プロパティ完全一致**（Workflows 23 / PR 8）の live DB（`in_trash: False`、title `Workflows DB` / `Pull Requests DB`、ともに 2026-05-14 作成）が別 id（`…812b…` / `…8192…`）で存在し query も 200。ゴミ箱化された旧 DB を env が指したままだった。
- ⚠ **`check_db_share_health()` が false positive を返していた**: この health check は `retrieve_database`（GET）で probe していたため、**ゴミ箱 DB を OK と誤報告**。`hokusai start` 冒頭の事前警告がこの誤設定を見逃し、数週間サイレントに同期を止めていた。**health check が、同期が実際に必要とする capability（query/create）を試していなかった**のが盲点。
- 補足: Notion ワークスペースは新データモデル（database → data source）へ移行済み（live DB は `data_sources` を持ち、`/v1/data_sources/{id}/query` でも 200）。ただし今回の 404 は data source 移行が原因ではなく **in_trash** が原因（live DB は旧 `/databases/{id}/query` でも 200）。

### 解消（実 drain 成功）

- env を live DB へ張替（`~/.zshrc`、バックアップ取得の上）: `WORKFLOWS_DB_ID_4HOKUSAI=…812b…` / `PR_DB_ID_4HOKUSAI=…8192…`。
- **`check_db_share_health()` が両 DB とも OK**（query probe が live DB で通過）。
- **`retry_pending(limit=50)` → `{succeeded: 12, failed: 0, moved_to_error: 0}`、outbox 12 → 0**。live Workflows DB に **3 workflow レコード**が同期（12 イベント＝workflow_started 3 / phase_changed 6 / terminal 3 がイベントソーシングで 3 workflow に集約）。推奨手順 ③（Notion 配線後の再観察）が実データで完遂。
- ✅ **drain 機構は堅牢**: ゴミ箱 DB に対しても 404 で `attempts` を進めるだけ（`max_retry_attempts=10`）で破損なし。張替後は 1 発で全件成功。

### 評価 + アクション

- **HOKUSAI 本体の改善（PR #167 + 本 PR）**: `check_db_share_health()` の probe を `retrieve_database` → **`query_database(page_size=1)`（read-only・副作用なし）** に変更（PR #167）。同期の書き込み経路と同じ capability を試すことで「retrieve は通るが sync は 404」（= env がゴミ箱/古い DB を指す、削除済み、未接続のいずれか）を正しく NG 検知する。本 PR で 404 メッセージを **原因を断定しない汎用文言**（"DB not queryable (query 404) — env の DB ID が古い/誤り、DB がゴミ箱・削除済み、または integration 未接続"）に訂正。回帰テスト `test_check_db_share_health_catches_query_404_when_retrieve_would_pass`（retrieve 成功・query 404 → NG）で固定。
- **§10〜13 の「Notion 未配線」記述の訂正**: ブロッカーは「env 未設定」でも「integration 未接続」でもなく、「**env がゴミ箱の DB ID を指したまま + health check の false positive**」だった。
- **dogfooding の収穫**: (1) 「観察ツールがユーザー環境の env を読めない」、(2) 「health check が誤った capability（retrieve）を probe し OK を返し続ける」、(3) 「`search` のフィルタ無し先頭ページの空振りを"未接続"と早合点した初回診断ミス」——の 3 つを、**実 drain を試みて初めて**炙り出せた。read-only 観察だけでは真因に到達できず、誤診断（未接続）に流れた。**書き込み経路を実際に叩き、生 API で in_trash まで確認する**ことの価値を示す事例。

---

## 15. 重い dogfooding（実 workflow 完走の試み）で炙り出した運用穴 (2026-06-03)

推奨手順 ④（実 workflow を 1 本完走させ運用穴を観察）として、Notion 配線が通った状態で実 workflow を起動した。**Phase 2 で構造的ブロッカーバグに当たり完走不能**となったが、その過程で複数の運用穴を観測した。本 PR は最重要の Phase 2 ブロッカーを修正する。

### 観察手順

| Step | 操作 |
|---|---|
| 1 | 既存 stranded workflow（wf-dbe7b6cd Phase7 / wf-f373fac6 Phase2）の resume を試行 |
| 2 | 極小 dogfooding issue (#169, docs 改善) を作成し `hokusai start` で新規 workflow 起動 |
| 3 | Phase 1（準備）→ Phase 2（研究）の挙動・エラーを観測 |

### 実観察結果

- 🔴 **Phase 2 ブロッカーバグ（本 PR で修正）**: `task_backend: github_issue` + `notion_dashboard: enabled`（hokusai profile の正式構成）で、Phase 2 の研究出力を Notion 子ページとして保存する `save_to_subpage_or_create` → `create_phase_subpage` が **`task_url`（GitHub issue URL）を Notion 親ページとして `_extract_page_id` で parse** しようとして `Invalid Notion page URL or ID` で `RuntimeError`。**この構成ではどの workflow も Phase 2 で必ずクラッシュし完走不能**。Phase 1（worktree + AI 命名ブランチ作成）と Phase 2 の LLM 研究自体（Claude Code 60s, 必須セクション 4/4）は成功していたため、Notion 子ページ保存だけが落としていた。修正: task_url が Notion ページでない場合は subpage 保存を graceful に skip（workflow は継続。ダッシュボード同期＝status イベントは別経路で影響なし）。
- 🔴 **stranded workflow は resume も完走もできない**: 既存 2 workflow は worktree がクリーンアップ済みのため `continue` が `Worktree が存在しません` でハードエラー。wf-dbe7b6cd は実作業が PR #79 で main にマージ済み・リモートブランチも削除済みなのに、**workflow レコードは Phase 7 のまま**で、「完了」にする正規経路が無い（cleanup＝削除 or 新規作成のみ）。状態機械と worktree/ブランチのライフサイクルが乖離すると workflow が永久に取り残される。
- 🟡 **live Workflows DB のスキーマ drift**: §14 で env を張り替えた live DB に `Operator` プロパティが無く、workflow_started 同期で「`Operator` プロパティが存在しないため除外して再試行」。HOKUSAI は除外して継続するが、live DB と HOKUSAI が期待するスキーマがずれている（旧 `notion-migrate-schema` 未適用の可能性）。
- 🟡 **GitHub ラベル欠落**: `進行中` ラベルがリポジトリに存在せず、issue へのラベル付与が失敗（継続）。`hokusai start` が前提とするラベルが repo に無い。
- ⚠ **Notion 接続確認が重い**: `hokusai start` / `continue` 冒頭の「Notion接続確認」が毎回 Claude Code を起動（~21s）。単純な API 疎通確認に LLM CLI を使うのは過剰で、起動レイテンシを押し上げる。
- ⚠ **ローカル main 同期失敗（継続）**: 「ローカル main の同期に失敗、続行します」。Dropbox 配下の作業ツリーや未コミット状態が影響している可能性（観察のみ）。

### 評価 + 次のアクション候補

- **本 PR で修正**: Phase 2 ブロッカー（`save_to_subpage_or_create` の非 Notion task_url クラッシュ）。これにより github_issue backend でも Phase 2 以降へ進めるようになる。回帰テスト `TestSaveToSubpageNonNotionTaskUrl` を追加。
- **後続候補**: (a) stranded workflow を「マージ済み/完了」として閉じる経路（`hokusai cleanup --mark-merged` 等）。(b) live Workflows DB へ `notion-migrate-schema` 適用（`Operator` 等の欠落プロパティ補完）。(c) `進行中` ラベルの自動作成 or 欠落時の graceful skip 明示。(d) Notion 接続確認を LLM CLI でなく直接 API 疎通に置換（起動高速化）。
- **完走の継続**: Phase 2 修正後に #169 workflow を再開し、Phase 3〜10（設計/計画/実装/検証/レビュー/PR/記録）の運用穴を継続観察する（本 PR マージ後）。

---

## Appendix A: 観察用に叩いたコマンド

```bash
# 環境確認
cat ~/.hokusai/profiles.yaml
cat ~/.hokusai/configs/hokusai.yaml
env | grep -E "HOKUSAI_NOTION|HOKUSAI_LLM|WORKFLOW_WORKTREE"

# 状態確認
hokusai --profile hokusai list
hokusai status wf-dbe7b6cd --profile hokusai

# prime 観察
hokusai prime wf-dbe7b6cd --profile hokusai
hokusai prime wf-dbe7b6cd --profile hokusai --output json
hokusai prime wf-dbe7b6cd --profile hokusai | wc -c

# state DB 直接観察
sqlite3 ~/.hokusai/profiles/hokusai/workflow.db \
  "SELECT workflow_id, task_title, current_phase, branch_name, profile_name FROM workflows WHERE workflow_id='wf-dbe7b6cd';"
sqlite3 ~/.hokusai/profiles/hokusai/workflow.db \
  "SELECT id, event_type, attempts, last_error FROM notion_sync_outbox WHERE workflow_id='wf-dbe7b6cd';"
sqlite3 ~/.hokusai/profiles/hokusai/workflow.db "SELECT COUNT(*) FROM audit_logs;"
```

### Appendix A.2: §7 再観察で叩いたコマンド (2026-05-27)

Step 1 と Step 2 で `~/.hokusai/configs/hokusai.yaml` の設定を切り替えてから Python を実行する。`purpose` 文字列を Step 別に変えておくと audit_logs から後で `purpose` でフィルタして確認できる。

#### Step 1: log_only=true で audit 永続化を確認

```bash
# yaml: llm_gateway: { enabled: true, log_only: true, audit_log_enabled: true }
$EDITOR ~/.hokusai/configs/hokusai.yaml

# 非許可 provider を 1 回呼ぶが log_only=true のため block されず audit decision=log になる
HOKUSAI_ACTIVE_PROFILE=hokusai uv run python -c "
from hokusai.llm_gateway.dispatch import dispatch_via_gateway
dispatch_via_gateway(
    provider='claude_code', model='claude-sonnet-4',
    purpose='dogfood_observation_step1', prompt='...',
    workflow_id='wf-dbe7b6cd', phase=7,
)
print('Step 1 dispatch returned (expected decision=log)')
"

# 期待: 1 行追加 / status='log' / log_only=1 (SQLite は bool を 1/0 で返す)
sqlite3 ~/.hokusai/profiles/hokusai/workflow.db \
  "SELECT id, workflow_id, phase, status,
          json_extract(details_json, '\$.context.purpose') AS purpose,
          json_extract(details_json, '\$.config_snapshot.log_only') AS log_only
   FROM audit_logs
   WHERE json_extract(details_json, '\$.context.purpose')='dogfood_observation_step1'
   ORDER BY id DESC LIMIT 5;"
```

#### Step 2: log_only=false + allowed_providers で enforcement 経路を確認

```bash
# yaml: llm_gateway: { enabled: true, log_only: false, audit_log_enabled: true, allowed_providers: ["codex"] }
$EDITOR ~/.hokusai/configs/hokusai.yaml

# Test A (block) / Test B (pass) を 1 shot で実行
HOKUSAI_ACTIVE_PROFILE=hokusai uv run python -c "
from hokusai.llm_gateway.dispatch import dispatch_via_gateway, LLMGatewayBlockedError

# Test A: 非許可 provider → block 期待
try:
    dispatch_via_gateway(
        provider='claude_code', model='claude-sonnet-4',
        purpose='dogfood_observation_step2_block', prompt='...',
        workflow_id='wf-dbe7b6cd', phase=7,
    )
    print('A: UNEXPECTED no exception')
except LLMGatewayBlockedError as e:
    print(f'A: blocked hits={e.policy_hits} reason={e.reason}')

# Test B: 許可 provider → 透過期待
try:
    dispatch_via_gateway(
        provider='codex', model='gpt-4',
        purpose='dogfood_observation_step2_pass', prompt='...',
        workflow_id='wf-dbe7b6cd', phase=7,
    )
    print('B: passed through (expected)')
except LLMGatewayBlockedError as e:
    print(f'B: UNEXPECTED block hits={e.policy_hits}')
"

# 期待: Test A 側 status='block' / Test B 側 status='log' / log_only=0 を purpose で絞って個別に確認
sqlite3 ~/.hokusai/profiles/hokusai/workflow.db \
  "SELECT id, status, json_extract(details_json, '\$.policy_hits') AS policy_hits,
          json_extract(details_json, '\$.context.purpose') AS purpose,
          json_extract(details_json, '\$.config_snapshot.log_only') AS log_only
   FROM audit_logs
   WHERE json_extract(details_json, '\$.context.purpose')
         IN ('dogfood_observation_step2_block', 'dogfood_observation_step2_pass')
   ORDER BY id DESC LIMIT 10;"

# 詳細 JSON を Test A 側に絞って確認（block decision の中身を観察）
sqlite3 ~/.hokusai/profiles/hokusai/workflow.db \
  "SELECT details_json FROM audit_logs
   WHERE json_extract(details_json, '\$.context.purpose')='dogfood_observation_step2_block'
   ORDER BY id DESC LIMIT 1;" | python3 -m json.tool
```

### Appendix A.3: §11 で叩いたコマンド (2026-05-31)

```bash
# Step 3: Operation Registry
uv run hokusai operations list
uv run hokusai operations run runtime.health
uv run hokusai operations run notion.outbox_status
uv run hokusai operations run no.such          # 未知 op → stderr + exit 1

# Step 5: Local Workgraph Edges
uv run hokusai graph status
uv run hokusai graph recurring

# Step 4: Eval Capture
uv run hokusai eval list
uv run hokusai eval export > /tmp/df_base.json
uv run hokusai eval gate --baseline /tmp/df_base.json

# Step 2: Doctor/Status 統合
uv run hokusai profile doctor hokusai --deep

# 新テーブルの migration 確認
uv run python -c "
from hokusai.config import create_config_from_env_and_file
import sqlite3
cfg=create_config_from_env_and_file(profile_name='hokusai')
conn=sqlite3.connect(cfg.database_path)
want=['workgraph_edges','review_issues','work_items','eval_captures']
have=[r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")]
print({t:(t in have) for t in want})
"
```

### Appendix A.4: §13 HTTP admin で叩いたコマンド (2026-06-01)

```bash
# CLI 側ベースライン（同一 handler の比較対象）
hokusai operations list
hokusai operations run runtime.health

# HTTP admin 起動（既定 127.0.0.1 bind / read-only / 認証なし）
# PID を保持し、スクリプト終了時に必ず停止する（プロセス残り / ポート競合を防ぐ）
hokusai operations serve --port 8765 &
SERVE_PID=$!
trap 'kill "$SERVE_PID" 2>/dev/null' EXIT
sleep 1  # bind 完了待ち

# 正常系: 一覧（input_schema 込み）/ 実行（CLI と result 一致を確認）
python - <<'PY'
import urllib.request, urllib.error
B = "http://127.0.0.1:8765"
def hit(path, method="GET"):
    req = urllib.request.Request(B + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

for path, method in [
    ("/operations", "GET"),                  # 200 一覧（stable schema）
    ("/operations/runtime.health", "GET"),   # 200 CLI と result バイト一致
    ("/operations/workflow.list", "GET"),    # 200 実 workflow データ
    ("/operations/workflow.status", "GET"),  # 400 必須 param 欠落（static 文言）
    ("/operations/no.such.op", "GET"),       # 404 available 列挙（registry 由来）
    ("/operations", "POST"),                 # 405 method not allowed
    ("/operations", "HEAD"),                 # 405 + 空ボディ（do_HEAD）
    ("/operations/review_issues.list_open?limit=evil<script>xyz", "GET"),  # 400 redaction
]:
    code, body = hit(path, method)
    print(method, path, "->", code, body[:120])
PY
```

## Appendix B: 参照したコード位置

| ファイル | 行 | 観察対象 |
|---|---|---|
| `hokusai/cli_main.py` | 1044-1290 | `_handle_prime` 実装 |
| `hokusai/cli_main.py` | 1292-1362 | `_collect_handover_notes`（Supersedes 遡及） |
| `hokusai/cli_main.py` | 1708-1782 | `_sync_workflow_cancel_reason` |
| `hokusai/cli_main.py` | 1785-1877 | `_handle_cleanup` / `--stale` |
| `hokusai/llm_gateway/interceptor.py` | 156-214 | `_emit_audit`（audit log 構造） |
| `hokusai/integrations/notion_dashboard/dispatcher.py` | 73-99 | `NotionSyncDispatcher` 構造 |
| `hokusai/integrations/notion_dashboard/dispatcher.py` | 444-471 | `_handle_pr_created` |
| `hokusai/integrations/notion_dashboard/prime_renderer.py` | 54-155 | `render_prime_markdown` 構造 |
| `hokusai/config/manager.py` | 95-132 | `WORKFLOW_WORKTREE_ROOT` 解決 |
| `hokusai/config/models.py` | 332-360 | `LLMGatewayConfig` 既定値 |
