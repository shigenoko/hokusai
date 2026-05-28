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
