# LangChain Interrupt 2026 発表内容から見た HOKUSAI 取り込み候補レポート

**作成者**: Codex
**作成日**: 2026-05-15

---

## 1. 目的

LangChain の Interrupt 2026 発表内容に含まれる機能群を、HOKUSAI に取り込む価値があるかという観点で評価する。

HOKUSAI は、AI Agent を完全自律で動かす基盤ではなく、人間が判断・承認・介入できる Human-Orchestrated な開発オーケストレーションを基本コンセプトとする。

そのため、本レポートでは以下の観点で評価する。

* HOKUSAI の基本コンセプトと整合するか
* 複数案件・複数 Agent・複数 reviewer の実運用に効くか
* Notion / GitHub / Operations Console による人間中心の運用に落とし込めるか
* HOKUSAI が自前で持つべき機能か、外部サービスに任せるべき機能か

---

## 2. 発表内容の要約

メールで言及されていた主な機能は以下である。

| 機能 | 概要 |
|---|---|
| LangSmith Engine | production trace を監視し、失敗を issue としてクラスタリングし、修正案・eval・dataset example を提案する |
| SmithDB | deeply nested trace / long-running span / large payload 向けの observability backend |
| Managed Deep Agents | durable execution、managed memory、sandbox、autoscaling を含む hosted agent runtime |
| LangSmith Sandboxes | isolated microVM、filesystem、shell、package manager、snapshot、auth proxy を持つ安全な code execution 環境 |
| LLM Gateway | spend control、data governance、PII / secret redaction を LLM provider 前段で行う |
| Context Hub | `AGENTS.md`、skills、policies、examples を versioned / collaborative に管理する |
| Prebuilt Agents | coding、GTM、executive assistant などの production-ready agent templates |
| Sandboxes for Fleet | Fleet agents に code execution 環境を付与する |
| Deep Agents v0.6 | lightweight code interpreter、typed streaming、DeltaChannel checkpoints |

---

## 3. HOKUSAI への取り込み価値

| 発表機能 | 取り込み価値 | HOKUSAI での解釈 |
|---|---:|---|
| LangSmith Engine | 高 | CI 失敗、レビュー指摘、Policy 違反、CVE などをクラスタリングし、Notion 上の改善候補にする |
| Context Hub | 高 | 案件ルール、`AGENTS.md`、Policy Pack、過去判断、スキル、事例を管理する |
| LLM Gateway | 中〜高 | profile ごとのモデル制限、コスト上限、PII / secret redaction を実行前 gate にする |
| Sandboxes | 中 | Agent 実行環境の隔離。ローカル実行、CI 実行、コンテナ実行との整理が必要 |
| Durable execution / managed memory | 中 | 既存の checkpoint / profile / SQLite 設計を強化する参考にする |
| Typed streaming / DeltaChannel | 中 | Operations Console の進捗表示や checkpoint 差分更新に活用できる |
| Prebuilt Agents | 低〜中 | 汎用 agent template ではなく、HOKUSAI 専用の role template として一部参考にする |
| SmithDB | 低 | HOKUSAI が大規模 trace DB を自前実装する必要性は低い |

---

## 4. 優先して取り込むべき機能

### 4.1 Failure Intelligence

LangSmith Engine の考え方は、HOKUSAI に最も取り込む価値が高い。

HOKUSAI では以下の入力を扱える。

* CI failure
* GitHub Copilot review comment
* human review comment
* HOKUSAI final review の NG
* Policy Governance Framework の違反
* CVE / dependency vulnerability
* lint / typecheck / test failure

これらを個別のログやコメントとして扱うだけではなく、類似原因ごとにクラスタリングし、Notion の Review Issues DB / Work Items DB に構造化する。

想定フロー:

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

この機能は、HOKUSAI の Review Issue Graph、Dependency-aware Work Queue、Workflow Gates と相性がよい。

### 4.2 Context / Policy Hub

Context Hub は、HOKUSAI の Persistent Project Memory と Policy Governance Framework に近い。

HOKUSAI では以下を管理対象にする価値がある。

* 案件ごとの `AGENTS.md`
* profile ごとの運用ルール
* 設計判断
* 禁止実装
* reviewer からの過去指摘
* Policy Pack
* security / dependency waiver
* よく使う prompt / agent instruction
* 過去の失敗と回避策

Notion は人間の編集・承認 UI として使い、承認済みの実行用 context は repository に同期できる形が望ましい。

責務分担:

| 場所 | 役割 |
|---|---|
| Notion | 編集、レビュー、承認、履歴、業務文脈 |
| repository | 実行時に再現可能な context source |
| SQLite / checkpoint | どの context version を使ったかの記録 |
| Operations Console | context version、同期状態、差分の確認 |

### 4.3 LLM Governance Gateway

LLM Gateway の考え方は、HOKUSAI を企業案件で使う場合に有効である。

HOKUSAI では以下を profile 単位で管理する。

* 利用可能な LLM provider / model
* 高額モデル利用時の Human Approval
* 日次 / 月次 / workflow 単位のコスト上限
* PII / secret redaction
* 外部 LLM に送信禁止のファイル種別
* policy 違反時の gate
* provider 別の実行ログ

これは便利機能ではなく、本格運用時のガードレールとして扱うべきである。

---

## 5. 条件付きで検討する機能

### 5.1 Sandboxed Code Execution

LangSmith Sandboxes の isolated microVM / filesystem / shell / package manager / snapshot / auth proxy は、Agent がコードを実行する場合の安全性を高める。

ただし HOKUSAI は、現時点ではローカル repository と既存開発環境を前提にしているため、いきなり managed sandbox を前提にする必要はない。

段階的には以下が現実的である。

1. local worktree isolation
2. profile ごとの workspace 分離
3. Docker / devcontainer での検証実行
4. CI 上での再現実行
5. 必要に応じた remote sandbox 連携

### 5.2 Durable Execution / Managed Memory

Managed Deep Agents の durable execution / managed memory は、HOKUSAI の checkpoint / resume / profile 並列実行の強化に参考になる。

ただし、HOKUSAI が hosted agent runtime になる必要はない。

取り込むべきなのは以下である。

* workflow の中断・再開の確実性
* 実行中 Agent の状態確認
* failed step からの復旧
* profile / run_id / worker_id 単位の履歴管理
* Agent に渡した context version の記録

### 5.3 Typed Streaming / DeltaChannel

Deep Agents v0.6 の typed streaming と DeltaChannel は、HOKUSAI の Operations Console に有用である。

活用候補:

* phase progress の構造化表示
* Agent output の種類別 streaming
* approval request のリアルタイム表示
* checkpoint 差分更新
* Notion sync outbox の進捗表示

優先度は中程度であり、まずはログと状態遷移の構造化が先である。

---

## 6. 取り込み優先度が低い機能

### 6.1 SmithDB

SmithDB は agent observability 向けの高性能 backend であり、大規模 trace や deeply nested span を扱う用途に向いている。

HOKUSAI がこのレイヤーを自前で持つ必要性は低い。

HOKUSAI では、まず以下で十分である。

* SQLite による workflow / checkpoint / audit log
* Notion による人間向け governance view
* 必要に応じた JSONL / OpenTelemetry export

将来的に trace volume が増えた場合は、SmithDB 相当を自作するのではなく、OpenTelemetry、LangSmith、外部 observability backend への export を検討する。

### 6.2 Prebuilt Agents

Fleet の prebuilt agents は、HOKUSAI にそのまま取り込む価値は高くない。

HOKUSAI では汎用 agent template よりも、workflow phase に沿った role definition の方が重要である。

例:

* Research Agent
* Design Review Agent
* Implementation Agent
* Verification Agent
* Security Review Agent
* PR Review Response Agent
* Notion Sync Agent

これらは Human-Orchestrated workflow の中で claim / lease / gate に従って動くべきであり、自律的に自由行動する agent 群にはしない。

---

## 7. HOKUSAI 向け推奨機能セット

HOKUSAI に取り込むなら、以下の 3 つに絞るのがよい。

### 7.1 Failure Intelligence

目的:

* 失敗やレビュー指摘を散在させず、Notion 上の構造化 issue にする
* 同じ原因の再発を追跡する
* 修正候補、eval 追加候補、テスト追加候補を人間が判断できるようにする

主要要件:

* CI / review / policy / dependency source から issue を生成する
* 類似 issue を cluster として扱う
* Review Issue Graph と連携する
* Work Item を自動生成できる
* `waived` / `duplicate` / `resolved_by_pr` を追跡できる
* 自動修正はせず、人間の gate / priority を尊重する

### 7.2 Context / Policy Hub

目的:

* Agent に渡す context を属人的・一時的な prompt から、承認済みの案件資産にする
* Notion 上で人間が編集・承認し、実行時には versioned context として使う

主要要件:

* `AGENTS.md`、Policy Pack、Project Memory、禁止実装、過去判断を管理する
* draft / active / deprecated の状態を持つ
* active 化には human approval を必要とする
* Agent 実行時に context summary を注入する
* 使用した context version を checkpoint / audit log に残す

### 7.3 LLM Governance Gateway

目的:

* profile ごとの LLM 利用ルールを明確にする
* コスト、モデル、データ送信、secret / PII を制御する
* 企業案件での安全な LLM 利用を支える

主要要件:

* profile ごとの allowed providers / models を定義する
* spend cap を持つ
* 高リスク操作や高額モデル利用には gate を要求する
* prompt / tool input 送信前に secret / PII redaction を行う
* redaction / block / approval の履歴を audit log に残す

---

## 8. 既存 HOKUSAI 設計との接続

本レポートの推奨機能は、既存または検討中の HOKUSAI 機能と以下のように接続する。

| 推奨機能 | 接続先 |
|---|---|
| Failure Intelligence | Review Issue Graph、Dependency-aware Work Queue、Workflow Gates、Policy Governance Framework、Dependency Governance |
| Context / Policy Hub | Persistent Project Memory、Policy Pack、profile、`hokusai prime` |
| LLM Governance Gateway | Workflow Gates、profile config、audit log、Operations Console |
| Sandboxed Execution | profile 並列実行、worker_id / run_id、CI verification |
| Typed Streaming | Operations Console、checkpoint、Notion sync outbox |

---

## 9. 実装順序案

### Step 1: Failure Intelligence MVP

* CI failure / HOKUSAI final review / PR review comment を Review Issues DB に登録する
* 類似指摘を cluster できる最低限の rule を持つ
* Work Item への変換を可能にする

### Step 2: Context / Policy Hub MVP

* Project Memory / Policy Pack / `AGENTS.md` を profile 単位で管理する
* Notion 上で draft / active / deprecated を扱う
* `hokusai prime` 相当の context summary を出力する

### Step 3: LLM Governance Gateway MVP

* profile config に allowed models / spend cap / redaction policy を追加する
* prompt 送信前の secret scan を行う
* block / approval / redaction を audit log に残す

### Step 4: Execution Safety

* local worktree isolation を強化する
* Docker / devcontainer / CI sandbox との接続を検討する
* worker_id / run_id / lease と統合する

### Step 5: Operations Console 強化

* streaming progress を構造化する
* failure cluster、pending gate、LLM spend、redaction event を表示する
* Notion sync outbox と audit trail を確認できるようにする

---

## 10. 結論

Interrupt 2026 の発表内容から HOKUSAI に取り込むべきなのは、agent hosting や大規模 observability backend そのものではない。

HOKUSAI にとって価値が高いのは、以下の 3 つである。

1. Failure Intelligence
2. Context / Policy Hub
3. LLM Governance Gateway

これらは HOKUSAI の Human-Orchestrated という基本コンセプトを崩さず、むしろ本格運用時に必要な人間の判断、承認、監査、再発防止を強化する。

一方で、SmithDB や Managed Deep Agents のような大規模基盤は、HOKUSAI に直接内包するよりも、外部サービスや将来の integration target として扱う方がよい。
