# LangChain Interrupt 2026 発表機能の HOKUSAI への取り込み評価

**評価者**: Claude (Anthropic)
**評価日**: 2026-05-15
**評価対象**: LangChain Interrupt 2026 で発表された各機能
**HOKUSAI 側の前提**: v0.4.6 時点（profile / Notion governance / LangGraph checkpoint / Claude Code + Codex + Gemini cross-review / Operations Console）

---

## 1. 評価方針

HOKUSAI は、AI Agent を完全自律で動かす hosted agent platform ではなく、人間が判断・承認・介入できる Human-Orchestrated な開発オーケストレーションを基本コンセプトとする。

そのため、Interrupt 2026 の発表機能は以下の観点で評価する。

* HOKUSAI の Human-Orchestrated 方針と整合するか
* profile / Notion governance / GitHub / Operations Console に自然に接続できるか
* 複数案件・複数 Agent・複数 reviewer の実運用で事故を減らすか
* 自前実装すべき機能か、外部連携や将来課題に留めるべき機能か
* HOKUSAI の責務を agent hosting や巨大 observability backend に広げすぎないか

> **注記（既存機能 / 計画中機能の区別）**
> 本書中で言及される下記項目は **v0.4.6 時点では未実装の計画機能** であり、別途設計中である。
> 本書はそれらと将来接続する前提で各 LangChain 機能を評価している。
>
> * Review Issue Graph（CI / Copilot / human review / policy / CVE 指摘の構造化）
> * Dependency-aware Work Queue（修正 Work Item の依存関係解決）
> * Workflow Gates（human_approval / security gate 等の停止点）
> * Persistent Project Memory（案件横断の判断履歴）
> * Policy Governance Framework（実行ポリシーと waiver の管理）
> * `hokusai prime` コマンド（active context summary の出力）
>
> 上記の要件定義は `docs/hokusai-human-governance-workgraph-requirements.md` を参照。
> 一方、v0.4.6 時点で既に存在する機能は profile / Notion governance / LangGraph checkpoint /
> Claude Code + Codex + Gemini cross-review / Operations Console である。

---

## 2. サマリー

| 優先度 | 機能 | HOKUSAI への取り込み価値 |
|---|---|---|
| 高 | LLM Gateway パターン | 支出統制 + PII / secret redaction。規制業界・企業案件向けの必須ガードレール |
| 高 | Sandboxes の auth proxy パターン | secret を Agent 実行時メモリから物理分離。監査要件で重要 |
| 高 | LangSmith Engine パターン | CI / review / policy / CVE 失敗をクラスタリングし、Notion 上の改善候補にする |
| 中 | Context Hub パターン | `AGENTS.md`、prompts、skills、policies、examples、Project Memory の版管理 |
| 中 | DeltaChannel 概念 | LangGraph checkpoint の delta 化、Operations Console streaming の将来課題 |
| 中 | Sandboxed execution の一部 | local worktree / devcontainer / CI sandbox 強化として段階導入 |
| 低 | SmithDB | HOKUSAI が大規模 trace DB を自前で持つ必要性は低い |
| 低 | Managed Deep Agents / Prebuilt Agents Fleet | HOKUSAI の local-first / human-orchestrated 設計とは別カテゴリ |

最終的な推奨着手順序は以下とする。

1. LLM Gateway
2. Auth Proxy
3. Failure Intelligence
4. Context / Policy Hub
5. Delta checkpoint / sandbox / observability export

---

## 3. 責務分担

LangChain の発表機能を HOKUSAI に取り込む場合、Notion / repository / SQLite / Operations Console の責務を混同しないことが重要である。

| 場所 | 役割 |
|---|---|
| SQLite / checkpoint | workflow 実行状態、phase progress、retry、run_id、worker_id、audit log、使用 context version |
| Notion | 人間が判断するための governance view、approval、waiver、議論、Project Memory、改善候補 |
| repository | 実行時に再現可能な prompts、policies、`AGENTS.md`、設定ファイル |
| Operations Console | 実行状況、Notion sync outbox、LLM spend、redaction event、expired lease、復旧操作 |
| GitHub | PR、review comment、CI result、resolved_by_pr、コード差分レビュー |

Notion は実行エンジンではなく、人間の判断・承認・監査のための UI として扱う。

---

## 4. 高優先: LLM Gateway パターン

### 4.1 LangChain の発表

> LLM Gateway: Spend controls and data governance between your agents and LLM providers.
> Enforces hard spend caps at the org, workspace, user, or API key level and redacts PII and secrets
> before requests leave your environment.

### 4.2 HOKUSAI 現状

* profile 別 / phase 別の LLM コスト追跡がない
* prompt 内 PII / secret 漏出を防ぐ仕組みがない
* 高額モデル利用や外部 LLM 送信の approval gate がない
* provider / model ごとの利用ポリシーが profile config に集約されていない

### 4.3 取り込み価値

HOKUSAI のターゲットである金融 / 決済 / コンプライアンス重視業界では必須要件である。

これは便利機能ではなく、企業案件で HOKUSAI を安全に使うためのガードレールとして扱う。

### 4.4 設計案

```text
hokusai/integrations/llm_gateway/
  interceptor.py    # 全 LLM 呼び出しに割り込んで spend / PII / policy を検査
  pii_detector.py   # クレジットカード番号 / メールアドレス / 個人番号等を検出
  spend_tracker.py  # profile / phase / workflow / provider ごとの累積コスト
  policies.py       # spend_cap / redaction_rules / allowed_models を config から読み込み
  audit.py          # block / warn / redact / approve の履歴保存
```

profile config 拡張案:

```yaml
# モデル ID は各 provider の最新識別子に置き換えること（例は概念のみを示す）
llm_gateway:
  enabled: true
  allowed_providers: [openai, anthropic, google]
  allowed_models:
    default: ["<provider-default-model>"]            # 例: claude-sonnet-4-6, gemini-2.5-pro
    high_cost_requires_gate: ["<provider-flagship>"] # 例: claude-opus-4-7, 高額モデル
  spend_cap:
    monthly_jpy: 50000
    per_workflow_jpy: 500
  pii_redaction:
    enabled: true
    rules: [credit_card, email, jp_phone_number, jp_my_number, secret_like_token]
  fail_mode: block  # block | warn | log
```

### 4.5 Notion / Operations Console 連携

* 高額モデル利用は Workflow Gates の `human_approval` と連携する
* redaction / block / approval は audit log に残す
* Operations Console に spend、redaction count、blocked request を表示する
* Notion には secret 実値を出さず、env var 名、判断理由、承認者のみを保存する

---

## 5. 高優先: Auth Proxy パターン

### 5.1 LangChain の発表

> Sandboxes: ... an auth proxy that keeps secrets out of the runtime

### 5.2 HOKUSAI 現状

* Claude Code / Codex / Gemini が実行されるサブプロセスは環境変数を直接読める
* Phase 5 中の prompt / log に secret が漏れるリスクは「prompt 注意 + log 確認」で運用している
* PR #32 の SonarCloud taint flow 修正と同じ思想だが、HOKUSAI 全体の secret 取り扱いポリシーとしては未整理

### 5.3 取り込み価値

規制業界では「secret が Agent の実行時メモリに載らない」保証が監査上重要になる。

auth proxy 経由化により、以下を実現できる。

* log 流出時の被害最小化
* prompt template に secret 名を埋め込んでも実値を Agent に渡さない
* Claude Code / Codex / Gemini にトークンを直接渡さず、API 直前で HOKUSAI が仲介する
* profile ごとの外部接続権限を中央管理する

### 5.4 設計案

```python
# placeholder で prompt 構築
prompt = "Use ${secrets.GITHUB_TOKEN} to call GitHub API through HOKUSAI proxy"

# Agent は secret placeholder のみを見る
# HOKUSAI の proxy が外向き HTTP 直前で secret を解決する
# secret 実値は prompt / Agent log / Notion には残さない
```

段階導入:

1. secret placeholder syntax の導入
2. prompt / log の secret scanner
3. GitHub / Notion / Figma / Miro など主要 API の proxy 化
4. profile ごとの allowed secret scope
5. proxy 経由でない外部 API 呼び出しの検出・block

実装難度は高いため、LLM Gateway の redaction / audit を先に入れ、その後に auth proxy を拡張するのが現実的である。

---

## 6. 高優先: LangSmith Engine パターン / Failure Intelligence

### 6.1 LangChain の発表

> LangSmith Engine: Agent improvement, autonomously. Engine watches your production traces,
> clusters failures into named issues, and proposes code fixes, eval coverage, and dataset examples.

### 6.2 HOKUSAI 現状

* `audit_log` / `phases.*.error_message` / cross-review findings が SQLite に蓄積されているが、横断分析手段がない
* Notion governance 層に分析結果を流す仕組みがない
* CI failure、Copilot review、human review、Policy violation、CVE warning が別々の場所に散在しやすい

### 6.3 取り込み価値

HOKUSAI の Human-Orchestrated 思想と直結する。

HOKUSAI では「Agent が勝手に改善する」のではなく、失敗や指摘をクラスタリングして、人間が判断できる形で Notion に出すことが重要である。

対象入力:

* CI failure
* GitHub Copilot review comment
* human review comment
* HOKUSAI final review の NG
* Cross-LLM review finding
* Policy Governance Framework の違反
* CVE / dependency vulnerability
* lint / typecheck / test failure

### 6.4 想定フロー

```text
CI失敗 / review指摘 / policy違反 / CVE警告
↓
原因・対象ファイル・severity・再発性でクラスタリング
↓
Review Issues DB に登録
↓
必要に応じて Work Items DB に修正タスクを作成
↓
人間が priority / waive / assign / gate を判断
↓
Agent が ready な Work Item のみ修正
```

### 6.5 設計案

```bash
# 30 日分の workflow 履歴を分析して Notion 議論ページに改善案を投稿
hokusai analyze --since 30d --post-to-notion

# PR review / CI failure を Review Issues DB に同期
hokusai analyze pr --pr 123 --sync-review-issues
```

出力例:

```text
- Phase 4 で「外部 API spec の確認待ち」が 12 件中 9 件発生
  → research checklist 追加を提案
- Phase 8 で「型安全性に関する Copilot 指摘」が 8 件発生
  → implement prompt に TypeScript hint 追加を提案
- dependency vulnerability の waiver が同じ package に 3 回発生
  → dependency policy の見直しを提案
```

### 6.6 HOKUSAI 既存設計との接続

| 接続先 | 内容 |
|---|---|
| Review Issue Graph | 指摘・失敗・CVE を構造化し、duplicates / resolved_by_pr / waived を追跡する |
| Dependency-aware Work Queue | 改善候補から修正 Work Item を生成する |
| Workflow Gates | high severity / security issue は gate として workflow を止める |
| Project Memory | Decided になった改善提案を memory candidate にする |
| Operations Console | failure cluster、open issue、repeated issue を表示する |

---

## 7. 中優先: Context Hub パターン

### 7.1 LangChain の発表

> Context Hub: Versioned, collaborative control over AGENTS.md files, skills, policies, and examples,
> all in one place.

### 7.2 HOKUSAI 現状

* `prompts/` は git で版管理されているが、PM / ビジネスサイドが編集しづらい
* CLAUDE.md / 各 phase prompt は repository に閉じる
* Project Memory、Policy Pack、案件ルール、過去判断を一体で扱う仕組みは未完成

### 7.3 取り込み価値

Context Hub は、HOKUSAI の Persistent Project Memory と Policy Governance Framework に近い。

管理対象:

* 案件ごとの `AGENTS.md`
* profile ごとの運用ルール
* phase prompt
* skills / agent role definition
* Policy Pack
* 過去の設計判断
* 禁止実装
* reviewer からの過去指摘
* security / dependency waiver
* よく使う prompt / examples

### 7.4 設計案

Notion は編集・議論・承認 UI として使い、承認済みの実行用 context は repository に同期する。

| 場所 | 役割 |
|---|---|
| Notion | draft、コメント、承認、業務文脈、非エンジニアレビュー |
| repository | 実行時に再現可能な `prompts/`、`AGENTS.md`、policies |
| SQLite / checkpoint | どの context version を使ったかの記録 |
| Operations Console | context version、同期状態、差分の確認 |

具体案:

* Notion の prompt / policy ページに変更が入ったら PR を自動作成する
* repo 側の prompt が変更されたら Notion 側に同期通知する
* `draft / active / deprecated` の状態を持つ
* `active` 化には human approval を必要とする
* `hokusai prime <workflow-id>` で active context summary を出力する

---

## 8. 中優先: DeltaChannel / checkpoint delta 化

### 8.1 LangChain の発表

> Deep Agents v0.6: ... DeltaChannel for efficient checkpoints.

### 8.2 HOKUSAI 現状

* LangGraph checkpoint は full snapshot
* 長期 workflow で SQLite サイズが肥大化する懸念がある
* Operations Console の進捗表示は、将来的により構造化された streaming が必要になる

### 8.3 取り込み価値

現状の規模では問題が顕在化していないため、将来の最適化候補とする。

活用候補:

* checkpoint の delta encoding
* phase progress の構造化 streaming
* Agent output の種類別 streaming
* approval request のリアルタイム表示
* Notion sync outbox の進捗表示

---

## 9. 中優先: Sandboxed Execution

LangSmith Sandboxes の isolated microVM / filesystem / shell / package manager / snapshot は、Agent がコードを実行する場合の安全性を高める。

ただし HOKUSAI は local-first の開発支援ツールであり、いきなり managed microVM を前提にする必要はない。

段階導入:

1. local worktree isolation
2. profile ごとの workspace 分離
3. Docker / devcontainer での検証実行
4. CI 上での再現実行
5. 必要に応じた remote sandbox 連携

auth proxy は高優先だが、microVM sandbox 全体は中優先または将来課題として扱う。

---

## 10. 取り込み価値が低い項目

| 機能 | 不採用理由 |
|---|---|
| SmithDB | 専有インフラ。HOKUSAI は CLI / SQLite ベースで十分。将来は OpenTelemetry / LangSmith / 外部 observability backend への export を検討する |
| Managed Deep Agents | LangChain の hosted runtime。HOKUSAI は local-first / human-orchestrated 設計であり、同じカテゴリに寄せるべきではない |
| Prebuilt Agents Fleet | HOKUSAI 自体が coding workflow orchestrator。汎用 agent template より phase 別 role definition が重要 |
| Sandboxes 全体 | local worktree / CI / devcontainer で段階的に達成可能。完全 microVM 化は現時点では overkill |

HOKUSAI で必要なのは大規模 agent hosting ではなく、以下のような HOKUSAI 専用 role definition である。

* Research Agent
* Design Review Agent
* Implementation Agent
* Verification Agent
* Security Review Agent
* PR Review Response Agent
* Notion Sync Agent

これらは claim / lease / gate に従って動くべきであり、自律的に自由行動する agent 群にはしない。

---

## 11. 推奨着手順序

### Phase 1: LLM Gateway

対象バージョン目安: v0.5.x

* profile config に spend cap / allowed models / PII redaction を追加する
* LLM 呼び出しを interceptor 経由に集約する
* block / warn / redact / approval を audit log に残す
* Operations Console に LLM spend / blocked request を表示する

### Phase 2: Auth Proxy

対象バージョン目安: v0.6.x

* secret placeholder syntax を導入する
* GitHub / Notion / Figma / Miro など主要 API の proxy 化を検討する
* Agent 実行環境へ secret 実値を直接渡さない運用を作る
* profile ごとの allowed secret scope を設定する

### Phase 3: Failure Intelligence

対象バージョン目安: v0.7.x

* `hokusai analyze` サブコマンドを追加する
* CI failure / PR review / HOKUSAI final review / policy violation を Review Issues DB に同期する
* 類似 issue を cluster 化する
* Notion 議論 / Review Issues / Work Items に改善候補を出す

### Phase 4: Context / Policy Hub

対象バージョン目安: v0.8.x

* Notion と repository の prompt / policy 同期を設計する
* Project Memory / Policy Pack / `AGENTS.md` を profile 単位で管理する
* `hokusai prime` で active context summary を出力する
* 使用 context version を checkpoint / audit log に残す

### Phase 5: Execution Safety / Observability

対象バージョン目安: v0.9.x 以降

* local worktree isolation を強化する
* Docker / devcontainer / CI sandbox との接続を検討する
* checkpoint delta / structured streaming を検討する
* OpenTelemetry / LangSmith 等への export を検討する

---

## 12. 関連 HOKUSAI Issue 候補

* LLM Gateway 機能の追加（profile spend cap + PII / secret redaction）
* Auth Proxy 機能の追加（secret placeholder + 実行時差し替え）
* Workflow 履歴の自動分析と Notion 議論への投稿
* Review Issue Graph への CI / Copilot / human review / policy / CVE 同期
* Context Hub: Notion と repository の `prompts/` / `AGENTS.md` / Policy Pack 同期
* `hokusai prime` による active context summary 出力
* Operations Console への LLM spend / redaction / failure cluster 表示

---

## 13. 結論

Interrupt 2026 の発表内容から HOKUSAI に取り込むべきなのは、Managed Deep Agents や SmithDB のような hosted infrastructure そのものではない。

HOKUSAI にとって価値が高いのは、以下の 4 つである。

1. LLM Gateway
2. Auth Proxy
3. Failure Intelligence
4. Context / Policy Hub

これらは HOKUSAI の Human-Orchestrated という基本コンセプトを崩さず、本格運用に必要な人間の判断、承認、監査、コスト統制、再発防止を強化する。

一方で、SmithDB、Managed Deep Agents、Prebuilt Agents Fleet は、HOKUSAI に直接内包するよりも、外部連携候補または将来の export target として扱う方がよい。

---

## 参考

* LangChain Interrupt 2026 announcement（原典: marketing email）
* HOKUSAI 設計判断: `docs/notion-dashboard-operation-guide.md`
* HOKUSAI cross-review 設計: `docs/hokusai-issue-31-gemini-cli-cross-review-implementation-plan.md`
* HOKUSAI Human Governance Workgraph 要件: `docs/hokusai-human-governance-workgraph-requirements.md`
* Codex 評価メモ: `docs/codex-langchain-interrupt-2026-hokusai-feature-report.md`
