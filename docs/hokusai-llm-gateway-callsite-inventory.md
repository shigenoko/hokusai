# HOKUSAI LLM Gateway Callsite Inventory

**作成日**: 2026-05-15

**関連要件**: `docs/hokusai-llm-gateway-requirements.md`

---

## 1. 目的

LLM Gateway を実装する前に、HOKUSAI 内で LLM / Agent に prompt、context、tool input を渡している箇所を棚卸しする。

本ドキュメントでは、各 callsite について以下を整理する。

* 呼び出し元ファイル / 関数
* 実行 runtime / provider
* 渡している入力
* LLM Gateway を挟むべき位置
* MVP 対応優先度
* 注意点

---

## 2. 結論

LLM Gateway の MVP では、まず **クライアント境界** に interceptor を入れるのが最も現実的である。

優先順位:

1. `ClaudeCodeClient._run_claude_code()`
2. `CodexClient.review_document()`
3. `GeminiClient.review_document()` / `GeminiClient.generate()`
4. Notion MCP / task backend の Claude Code 経由 callsite
5. Phase node 側の prompt builder は、個別 metadata 付与のために段階対応

Phase node ごとに個別に Gateway を挟むより、まず client boundary に集約した方が抜け漏れが少ない。

ただし、audit log に `workflow_id` / `phase` / `profile` / `request_kind` を正しく残すには、Phase node 側から metadata を渡せる API 拡張が必要になる。

---

## 3. Runtime 別の主な入口

| Runtime | 主な入口 | Gateway 優先度 | 備考 |
|---|---|---:|---|
| Claude Code | `ClaudeCodeClient.execute_prompt()` / `execute_skill()` | P0 | Phase 2/3/4/5/7/8、Notion MCP、branch name 生成で広く利用 |
| Codex CLI | `CodexClient.review_document()` | P0 | Cross-LLM Review 用。prompt が argv に載っているため改善余地あり |
| Gemini CLI | `GeminiClient.review_document()` / `generate()` | P0 | prompt は stdin。model validation 済み |
| Notion MCP via Claude Code | `NotionMCPClient` / `NotionTaskClient` / `notion_helpers` | P1 | LLM というより tool execution だが Claude Code prompt 経由なので対象 |
| Git branch suffix generation | `GitClient.generate_branch_suffix()` | P2 | 小さい prompt だが task title を LLM に送る |

---

## 4. P0: Client Boundary

### 4.1 Claude Code Client

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/integrations/claude_code.py` |
| 関数 | `execute_skill()`, `execute_prompt()`, `_run_claude_code()` |
| runtime | Claude Code CLI |
| provider | Anthropic / Claude Code |
| 入力 | skill 名、args、任意 prompt、append_system_prompt |
| 実行方法 | `claude -p <prompt> --output-format text --permission-mode ...` |
| Gateway 位置 | `_run_claude_code()` の直前 |
| MVP 対応 | 必須 |

現状:

* すべての Claude Code 実行は `_run_claude_code()` に集約されている。
* prompt は `-p` の argv として渡されている。
* `allow_mcp_tools` / `allow_file_operations` により permission mode が変わる。
* provider / model 指定はこの層には存在しない。

Gateway 要件:

* `_run_claude_code()` で prompt を Gateway に渡す。
* `request_kind` として `claude_prompt` / `claude_skill` を区別できるようにする。
* `permission_mode`、`disallowed_tools`、`append_system_prompt` を audit metadata に含める。
* PII / secret 検出後、`block` / `redact` / `warn` / `require_human_approval` を反映する。
* 将来的には prompt を argv ではなく stdin / file 経由にできるか検討する。

注意:

* MVP では `ClaudeCodeClient` の model usage / token usage は正確に取れない可能性が高い。
* まずは prompt length、estimated tokens、request count、decision を記録する。
* Claude Code が内部でさらに tool / LLM を使うため、Gateway は外側の prompt 統制から始める。

### 4.2 Codex Client

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/integrations/codex.py` |
| 関数 | `CodexClient.review_document()` |
| runtime | OpenAI Codex CLI |
| provider | OpenAI |
| 入力 | `review_prompt` + review 対象 document + schema_path |
| 実行方法 | `codex exec <full_prompt> --model <model>` |
| Gateway 位置 | `full_prompt` 構築後、`subprocess.run()` 前 |
| MVP 対応 | 必須 |

現状:

* Cross-LLM Review 用の document 全文が `full_prompt` に入る。
* `full_prompt` が argv に載っている。
* model は `self.model` で指定される。
* schema_path は CLI option として渡される。

Gateway 要件:

* `provider=openai`, `model=self.model`, `request_kind=cross_review` として policy check する。
* document に含まれる repository content / design context / review findings を detector 対象にする。
* high cost model gate と spend estimate の対象にする。
* prompt を argv ではなく stdin / temp file 経由に変更することを実装計画で検討する。

注意:

* Codex CLI の usage 情報は現状取得していない。
* MVP では token estimate と request count による概算にする。

### 4.3 Gemini Client

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/integrations/gemini.py` |
| 関数 | `GeminiClient.review_document()`, `GeminiClient.generate()` |
| runtime | Gemini CLI |
| provider | Google |
| 入力 | `review_prompt` + document、または任意 prompt + files |
| 実行方法 | `gemini -m <model>` + stdin prompt |
| Gateway 位置 | `full_prompt` 構築後、`_run_with_stdin_prompt()` 前 |
| MVP 対応 | 必須 |

現状:

* prompt は stdin で渡されている。
* model 名は `_MODEL_NAME_PATTERN` で検証されている。
* `generate()` は将来の主 Agent 抽象化に使える汎用 API として用意されている。

Gateway 要件:

* `provider=google`, `model=self.model`, `request_kind=cross_review` または `generate` として policy check する。
* `files` が指定される場合、読み込まれた file content も detector 対象にする。
* schema content を prompt に埋め込む場合も hash / metadata に反映する。

注意:

* Gemini は Codex より Gateway 適用が容易。prompt がすでに stdin 経由である。

---

## 5. P0: Workflow Phase Callsites

### 5.1 Phase 2 Research

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/nodes/phase2_research.py` |
| 関数 | `phase2_research_node()` |
| client | `ClaudeCodeClient.execute_prompt()` |
| prompt builder | `_build_task_research_prompt()`, `_build_research_retry_prompt()` |
| 入力 | task URL、design context、前回出力、validation error |
| Gateway 位置 | client boundary + Phase metadata 付与 |
| MVP 対応 | 必須 |

注意:

* `allow_mcp_tools=True`, `allow_file_operations=True` で実行される。
* Notion write tools は `disallowed_tools` で禁止されている。
* design context に Figma / Miro 情報が含まれる可能性がある。
* retry prompt には前回の LLM 出力が含まれるため、redaction 対象にする。

### 5.2 Phase 3 Design

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/nodes/phase3_design.py` |
| 関数 | `phase3_design_node()` |
| client | `ClaudeCodeClient.execute_prompt()` + `execute_cross_review()` |
| prompt builder | `_build_design_check_prompt()`, `_build_design_retry_prompt()` |
| 入力 | task URL、research result、cross-review context、design context、前回出力 |
| Gateway 位置 | client boundary + Cross-LLM client boundary |
| MVP 対応 | 必須 |

注意:

* Phase 2 の research result が prompt に入る。
* Figma / Miro context が prompt に入る。
* Phase 3 後に Cross-LLM Review が走るため、Claude / Codex / Gemini の二重経路がある。

### 5.3 Phase 4 Plan

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/nodes/phase4_plan.py` |
| 関数 | `phase4_plan_node()` |
| client | `ClaudeCodeClient.execute_skill()` / retry 時 `execute_prompt()` |
| prompt builder | `/dev-plan` skill args、`_build_dev_plan_retry_prompt()` |
| 入力 | task URL、cross-review context、research result、前回出力 |
| Gateway 位置 | `execute_skill()` と `execute_prompt()` の client boundary |
| MVP 対応 | 必須 |

注意:

* `/dev-plan` skill は skill 名 + args として prompt 化される。
* `append_system_prompt=read_prompt_file("phase4.append_system_prompt")` が渡される。
* skill args に cross-review context が含まれる場合がある。
* retry prompt には前回出力が含まれる。

### 5.4 Phase 5 Implement

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/nodes/phase5_implement.py` |
| 関数 | `_execute_implementation()` |
| client | `ClaudeCodeClient.execute_prompt()` |
| prompt builder | `_build_implementation_prompt()`, `_build_retry_prompt()` |
| 入力 | work plan、repository config、coding rules、review issues、verification errors |
| Gateway 位置 | client boundary + repository metadata |
| MVP 対応 | 必須 |

注意:

* `allow_file_operations=True` で実装を実行する。
* repository ごとに `working_dir` が変わる。
* retry prompt には Phase 6 / Phase 7 の失敗内容が含まれる。
* `logger.debug` で prompt 冒頭 500 文字を出しているため、LLM Gateway 導入時にログ redaction 方針を合わせる必要がある。

### 5.5 Phase 7 Final Review

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/nodes/phase7_review.py` |
| 関数 | `_review_single_repo()`, `phase7_review_node()` |
| client | `ClaudeCodeClient.execute_prompt()` |
| prompt builder | `_build_review_prompt()` |
| 入力 | review checklist、state、design context、repository context |
| Gateway 位置 | client boundary + repository metadata |
| MVP 対応 | 必須 |

注意:

* repository ごとに Claude Code が実行される。
* review checklist / project-specific rules が prompt に入る。
* Final review の結果は Phase 5 retry prompt に再利用される。

### 5.6 Phase 8 Review Fix

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/nodes/phase8/review_fix.py` |
| 関数 | `fix_review_comments()` |
| client | `ClaudeCodeClient.execute_prompt()` |
| prompt builder | `_build_review_fix_prompt()` |
| 入力 | Copilot / human review comments、PR number、repo name |
| Gateway 位置 | client boundary + PR metadata |
| MVP 対応 | 必須 |

注意:

* GitHub review comment 本文が prompt に入る。
* `allow_file_operations=True` で修正する。
* PR review comment に secret / PII が含まれる可能性を考慮する。

---

## 6. P1: Cross-LLM Review Orchestration

### 6.1 execute_cross_review

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/utils/cross_review.py` |
| 関数 | `execute_cross_review()` |
| client | `CodexClient.review_document()` または `GeminiClient.review_document()` |
| 入力 | review target document、review prompt、schema |
| Gateway 位置 | Codex / Gemini client boundary |
| MVP 対応 | 必須 |

現状:

* `config.cross_review.provider` で `codex` / `gemini` を切り替える。
* `config.cross_review.model` が provider client に渡る。
* 失敗時の挙動は `on_failure=warn|block|skip`。

Gateway 要件:

* `cross_review.provider` / `cross_review.model` を LLM Gateway policy と照合する。
* `on_failure=block` と LLM Gateway `block` / `require_human_approval` の状態遷移を整理する。
* Cross-review 結果そのものも次回 prompt に入るため、audit metadata に残す。

---

## 7. P1: Notion MCP / Task Backend 経由の Claude Code

### 7.1 NotionMCPClient

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/integrations/notion_mcp.py` |
| 関数 | `check_connection()`, `insert_after_existing()`, `append_to_page()` 等 |
| client | `ClaudeCodeClient.execute_prompt(..., allow_mcp_tools=True)` |
| 入力 | Notion page ID / URL、Markdown content、operation instruction |
| Gateway 位置 | Claude client boundary |
| MVP 対応 | P1 |

注意:

* Notion OAuth は Claude Code 側が管理している。
* prompt に Notion page ID、Markdown content、operation instruction が入る。
* LLM Gateway の対象ではあるが、実行目的は LLM 推論ではなく MCP tool orchestration に近い。
* secret 実値を Notion prompt に入れない方針を明確化する必要がある。

### 7.2 NotionTaskClient

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/integrations/task_backend/notion.py` |
| 関数 | `fetch_task()`, `update_status()`, `prepend_content()`, `append_progress()`, `update_checkboxes()` 等 |
| client | `ClaudeCodeClient.execute_prompt(..., allow_mcp_tools=True)` |
| 入力 | task URL、status、progress、Markdown content |
| Gateway 位置 | Claude client boundary |
| MVP 対応 | P1 |

注意:

* Notion API 直接呼び出しではなく Claude Code 経由。
* prompt に task content / progress summary が含まれる。
* LLM Gateway の audit 上は `request_kind=notion_mcp_tool` として通常の code generation と区別する。

### 7.3 notion_helpers

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/utils/notion_helpers.py` |
| 関数 | `save_to_subpage_or_create()`, `update_subpage_content()`, `append_to_subpage()` |
| client | `notion.claude.execute_prompt(..., allow_mcp_tools=True)` |
| 入力 | phase page content、子ページ title、Notion page ID |
| Gateway 位置 | Claude client boundary |
| MVP 対応 | P1 |

注意:

* Phase 2 / 3 / 4 の成果物本文が prompt に入る。
* LLM Gateway の prompt redaction により Notion 保存本文が変わると問題になるため、ここは「送信前検査」と「Notion保存内容の忠実性」のバランスが必要。
* MVP では block / audit を中心にし、automatic redaction は慎重に扱う。

---

## 8. P2: 補助的な LLM Callsite

### 8.1 Git branch suffix generation

| 項目 | 内容 |
|---|---|
| ファイル | `hokusai/integrations/git.py` |
| 関数 | `GitClient.generate_branch_suffix()` |
| client | `ClaudeCodeClient.execute_prompt()` |
| 入力 | task title |
| Gateway 位置 | Claude client boundary |
| MVP 対応 | P2 |

注意:

* prompt は小さいが、task title が外部 LLM に送信される。
* strict profile では task title も送信対象として audit すべき。
* block 時は既存の deterministic fallback を用意する必要がある。

---

## 9. Prompt Builder Inventory

Gateway は client boundary に置くが、以下の prompt builder は metadata 付与・テスト・redaction 境界のために把握しておく。

| File | Function | 内容 |
|---|---|---|
| `hokusai/nodes/phase2_research.py` | `_build_task_research_prompt()` | task research prompt |
| `hokusai/nodes/phase2_research.py` | `_build_research_retry_prompt()` | Phase 2 retry prompt |
| `hokusai/nodes/phase3_design.py` | `_build_design_check_prompt()` | design check prompt |
| `hokusai/nodes/phase3_design.py` | `_build_design_retry_prompt()` | Phase 3 retry prompt |
| `hokusai/nodes/phase4_plan.py` | `_build_dev_plan_retry_prompt()` | Phase 4 retry prompt |
| `hokusai/nodes/phase5_implement.py` | `_build_implementation_prompt()` | implementation prompt |
| `hokusai/nodes/phase5_implement.py` | `_build_retry_prompt()` | implementation retry prompt |
| `hokusai/nodes/phase7_review.py` | `_build_review_prompt()` | final review prompt |
| `hokusai/nodes/phase8/review_fix.py` | `_build_review_fix_prompt()` | review comment fix prompt |
| `hokusai/prompts/loader.py` | `get_prompt()` | template loading |

実装計画では、まず client boundary で共通検査し、その後に prompt builder 単位で `request_kind` / `phase` / `source` を付与する。

---

## 10. Config / Profile / Audit Inventory

### 10.1 Config

| ファイル | 内容 |
|---|---|
| `hokusai/config/models.py` | `WorkflowConfig`, `CrossReviewConfig`, `NotionDashboardConfig` 等の dataclass |
| `hokusai/config/manager.py` | env / config file / profile から `WorkflowConfig` を生成 |
| `hokusai/config/loaders.py` | YAML dict から各 config dataclass への parse |
| `hokusai/config/profiles.py` | profile registry / profile config path / data_dir 解決 |

LLM Gateway 実装では、`LLMGatewayConfig` dataclass と `_parse_llm_gateway_config()` を追加する必要がある。

### 10.2 Audit

| ファイル | 内容 |
|---|---|
| `hokusai/state.py` | in-memory `add_audit_log()` |
| `hokusai/persistence/sqlite_store.py` | SQLite `audit_logs` table と `add_audit_log()` |

現状の `state.add_audit_log()` は workflow state に entry を追加するだけで、LLM request 単位の詳細 audit には不足する。

LLM Gateway MVP では以下のどちらかを選ぶ必要がある。

1. 既存 `audit_logs` table に `action=llm_gateway_decision` として summary を保存する
2. 新規 `llm_gateway_requests` table を作り、request 単位で保存する

推奨は 2。理由は request_id、provider、model、prompt_hash、detectors、estimated_cost などの構造化 query が必要になるため。

---

## 11. MVP 対応表

| 優先度 | 対象 | 対応内容 |
|---|---|---|
| P0 | `ClaudeCodeClient._run_claude_code()` | すべての Claude Code prompt / skill に Gateway を適用 |
| P0 | `CodexClient.review_document()` | Cross-review prompt を Gateway 対象にする |
| P0 | `GeminiClient.review_document()` / `generate()` | Cross-review / future generate を Gateway 対象にする |
| P0 | config | `LLMGatewayConfig` と profile config parse を追加 |
| P0 | audit | `llm_gateway_requests` または audit summary を保存 |
| P0 | detector | secret-like / email / phone / credit card / My Number |
| P1 | Notion MCP callsites | `request_kind=notion_mcp_tool` として監査・block |
| P1 | Phase metadata | phase / workflow_id / repo_name / request_kind を client に渡す |
| P1 | Operations Console | spend / block / redaction summary 表示 |
| P2 | Git branch suffix generation | block 時 fallback と request_kind 整理 |

---

## 12. 実装上の注意点

### 12.1 自動 redaction の適用範囲

Phase 2 / 3 / 4 / Notion 保存系では、redaction によって成果物の忠実性が変わる可能性がある。

MVP では以下を推奨する。

* code generation / review prompt: `redact` を許可
* Notion MCP save prompt: `block` / `warn` を優先し、自動 redaction は慎重に扱う
* branch name generation: `block` 時は deterministic fallback

### 12.2 Prompt が argv に載る経路

現状、以下は prompt が argv に載る。

* `ClaudeCodeClient._run_claude_code()`: `claude -p <prompt>`
* `CodexClient.review_document()`: `codex exec <full_prompt>`

Gemini は stdin 経由である。

LLM Gateway の範囲ではないが、secret / PII の観点では argv 経路の改善を別 Issue にする価値がある。

### 12.3 Usage / cost の精度

Claude Code / Codex CLI / Gemini CLI は provider API response の usage を直接返さない可能性がある。

MVP では以下を記録する。

* prompt length
* estimated input tokens
* output length
* estimated output tokens
* provider / model
* pricing table version
* `usage_source=estimated`

### 12.4 Metadata propagation

client boundary だけでは phase / workflow / repo / PR が分からない場合がある。

実装計画では、以下のような metadata object を追加する。

```python
LLMGatewayContext(
    workflow_id=state.get("workflow_id"),
    phase=5,
    request_kind="implementation",
    repo_name=repo.name,
    profile_name=config.profile_name,
)
```

---

## 13. 未確認事項

* `WorkflowConfig` が現在 profile 名を保持しているか
* Claude Code CLI に stdin prompt または file prompt の安定した渡し方があるか
* Codex CLI に stdin prompt の安定した渡し方があるか
* Operations Console の現在の API 構成
* Notion Dashboard DB に LLM Gateway summary を追加するか、Operations Console のみに留めるか
* `llm_gateway_requests` table を新設するか、既存 audit log に載せるか

---

## 14. 次の作業

1. `LLMGatewayConfig` の config schema 実装計画を作る
2. `LLMGatewayContext` と client API 拡張方針を決める
3. `llm_gateway_requests` table の schema を設計する
4. detector / redaction MVP の実装計画を作る
5. Claude / Codex / Gemini client boundary への interceptor 導入順を決める

