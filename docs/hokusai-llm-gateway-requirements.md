# HOKUSAI LLM Gateway 要件定義書

**作成日**: 2026-05-15

**対象読者**: HOKUSAI 運用設計者、PM、テックリード、実装担当エンジニア

---

## 1. 概要

### 1.1 目的

HOKUSAI における LLM / Agent 実行を、profile 単位の利用ポリシー、コスト上限、PII / secret redaction、Human Approval、audit log によって統制する。

LLM Gateway は、HOKUSAI が外部 LLM provider や Agent runtime に prompt / context / tool input を渡す前段で動作する governance layer である。

本機能により、以下を実現する。

* profile ごとに利用可能な provider / model を制御する
* workflow / phase / profile 単位で LLM 利用量とコストを記録する
* PII / secret らしき値を送信前に検出し、block / warn / redact できる
* 高額モデル利用や高リスク送信に Human Approval gate を要求できる
* どの prompt / context が、どの policy に基づき、どの判断で送信されたかを audit log に残す

### 1.2 基本方針

LLM Gateway は、Agent の能力を高めるための機能ではなく、HOKUSAI を企業案件・規制業界・複数 profile 運用で安全に使うためのガードレールである。

| 領域 | 正本 |
|---|---|
| provider / model allowlist | profile config |
| spend / usage の実測値 | SQLite / audit log |
| Human Approval / override 判断 | Notion Gate または CLI / Operations Console の human action |
| secret 実値 | OS keychain / env / external secret manager |
| secret 名、判断理由、承認者 | Notion / audit log |

Notion は LLM Gateway の実行エンジンではなく、人間が判断・承認・監査するための view として扱う。

### 1.3 非ゴール

以下は本要件の対象外とする。

* LLM provider と完全互換の proxy server を最初から実装すること
* すべての PII / secret 漏洩を HOKUSAI 単体で完全に防止すること
* 法的・監査上のコンプライアンス準拠を保証すること
* Agent runtime 全体を microVM sandbox に移行すること
* Auth Proxy による secret の実行時分離を MVP に含めること

Auth Proxy は LLM Gateway の後続機能として扱う。

---

## 2. 対象範囲

### 2.1 対象となる呼び出し

LLM Gateway は、HOKUSAI が管理する以下の呼び出しを対象とする。

* Claude Code 実行
* Codex 実行
* Gemini CLI / Cross-LLM Review 実行
* OpenAI / Anthropic / Google 等の直接 API 呼び出し
* HOKUSAI final review
* `hokusai prime` による Agent context 生成
* 将来の Notion / GitHub / Figma / Miro 連携で LLM が生成する tool input

### 2.2 MVP 対象

MVP では以下に絞る。

* profile config による provider / model allowlist
* workflow 単位 / profile 単位の spend cap
* secret-like token detector
* email / phone / credit card / Japanese My Number などの基本 PII detector
* `block` / `warn` / `log` / `redact` の decision
* audit log への記録
* dry-run モード
* Operations Console での基本表示

### 2.3 MVP 対象外

MVP では以下を対象外とする。

* 完全な outbound HTTP proxy
* secret manager との動的連携
* provider API の完全な usage 課金精度保証
* 画像 / 音声 / 添付ファイル内の PII 検出
* LLM Gateway policy の Notion からの直接編集
* 自動 waiver

---

## 3. システムコンセプト

### 3.1 実行前チェック

LLM Gateway は、LLM / Agent に入力が渡る直前に以下を実行する。

```text
prompt / context / tool input
↓
profile policy load
↓
provider / model allowlist check
↓
spend cap check
↓
PII / secret detector
↓
decision: allow / redact / warn / block / require_human_approval
↓
audit log
↓
LLM / Agent runtime
```

### 3.2 Human-Orchestrated Governance

LLM Gateway は、危険な入力を勝手に承認してはならない。

高リスク送信、高額モデル利用、policy override、redaction 無視は、人間の承認を必要とする。

HOKUSAI は、承認が必要な場合に Workflow Gate を作成し、gate が open になるまで対象 workflow / phase を進めない。

### 3.3 profile との関係

LLM Gateway policy は profile 単位で切り替えられる必要がある。

例:

* A 社: strict、外部 LLM 送信制限あり、高額モデルは都度承認
* B 社: balanced、redaction は warn、月間上限あり
* OSS: speed、cost cap は低め、PII detector は warn

profile 境界を跨いで usage / policy / approval が混在してはならない。

---

## 4. Policy Model

### 4.1 profile config 例

```yaml
llm_gateway:
  enabled: true

  allowed_providers:
    - openai
    - anthropic
    - google

  allowed_models:
    default:
      - gpt-5.4
      - claude-sonnet-4.5
      - gemini-2.5-pro
    high_cost_requires_gate:
      - gpt-5.5
      - claude-opus-4.5

  spend_cap:
    monthly_jpy: 50000
    daily_jpy: 5000
    per_workflow_jpy: 500
    per_phase_jpy: 200
    fail_mode: block

  pii_redaction:
    enabled: true
    rules:
      - email
      - jp_phone_number
      - credit_card
      - jp_my_number
      - secret_like_token
    default_action: redact
    fail_mode: block

  approvals:
    high_cost_model: required
    pii_send_without_redaction: required
    policy_override: required

  audit:
    store_prompt_hash: true
    store_redacted_preview: true
    store_full_prompt: false
```

### 4.2 policy key

| Key | 必須 | 説明 |
|---|---|---|
| `enabled` | 必須 | LLM Gateway を有効化するか |
| `allowed_providers` | 必須 | 利用可能 provider |
| `allowed_models.default` | 必須 | 通常利用可能 model |
| `allowed_models.high_cost_requires_gate` | 任意 | Human Approval が必要な高額 model |
| `spend_cap.monthly_jpy` | 任意 | profile 月間上限 |
| `spend_cap.daily_jpy` | 任意 | profile 日次上限 |
| `spend_cap.per_workflow_jpy` | 任意 | workflow 単位上限 |
| `spend_cap.per_phase_jpy` | 任意 | phase 単位上限 |
| `pii_redaction.enabled` | 必須 | PII / secret detector を有効化するか |
| `pii_redaction.rules` | 任意 | 適用 detector |
| `pii_redaction.default_action` | 任意 | `redact` / `warn` / `block` / `log` |
| `approvals.*` | 任意 | Human Approval が必要な条件 |
| `audit.*` | 任意 | audit 保存粒度 |

### 4.3 decision

LLM Gateway は、各 request に対して以下の decision を返す。

| Decision | 意味 |
|---|---|
| `allow` | 送信可能 |
| `redact` | 検出値をマスクして送信可能 |
| `warn` | 警告を出して送信可能 |
| `log` | 記録のみ行い送信可能 |
| `block` | 送信不可 |
| `require_human_approval` | Human Approval gate が開くまで送信不可 |

---

## 5. Provider / Model 制御

### 5.1 必須要件

* profile config に存在しない provider は使用してはならない
* `allowed_models.default` に存在しない model は使用してはならない
* `high_cost_requires_gate` に該当する model は Human Approval gate を要求できること
* provider / model check の結果を audit log に残すこと
* dry-run では block せず、想定 decision を記録できること

### 5.2 fallback

指定 model が policy により block された場合、HOKUSAI は自動で別 model に切り替えてはならない。

fallback は以下のいずれかとする。

* workflow を `waiting_for_human` にする
* Human Approval gate を作成する
* CLI / Operations Console に明示的な再実行候補を出す

---

## 6. Spend Control

### 6.1 目的

LLM 利用コストを profile / workflow / phase / provider / model 単位で記録し、設定された上限を超える実行を防ぐ。

### 6.2 記録単位

| 単位 | 説明 |
|---|---|
| profile | 案件 / 顧客 / 環境単位 |
| workflow | HOKUSAI workflow 実行単位 |
| phase | Phase 2 Research / Phase 5 Implement 等 |
| run | Agent 実行単位 |
| provider | OpenAI / Anthropic / Google 等 |
| model | 使用 model |

### 6.3 cost estimation

MVP では、provider が返す usage 情報と、HOKUSAI 側の model pricing table による概算を併用する。

精度要件:

* 実請求額との完全一致は保証しない
* audit / guardrail に使える概算を目的とする
* pricing table の version を記録する
* usage が取得できない場合は `unknown_usage` として記録し、policy に応じて warn / block できること

### 6.4 必須要件

* spend cap 到達時は `block` または `require_human_approval` にできること
* cost estimate を audit log に残すこと
* Operations Console で profile / workflow の spend を確認できること
* Notion にコスト概要を同期する場合、詳細 prompt は含めないこと

---

## 7. PII / Secret Detection

### 7.1 対象 detector

MVP では以下を対象とする。

| Detector | 例 |
|---|---|
| `email` | `user@example.com` |
| `jp_phone_number` | 日本の電話番号形式 |
| `credit_card` | Luhn check を含むカード番号候補 |
| `jp_my_number` | 日本の個人番号候補 |
| `secret_like_token` | API key / token / private key らしき文字列 |
| `env_secret_reference` | `GITHUB_TOKEN` 等の secret 名参照 |

### 7.2 action

| Action | 意味 |
|---|---|
| `redact` | 値を `[REDACTED:<type>]` に置換する |
| `warn` | 警告し、送信は許可する |
| `block` | 送信しない |
| `log` | 記録のみ行う |
| `require_human_approval` | gate を要求する |

### 7.3 redaction 要件

* secret / PII 実値を audit log / Notion / Operations Console に保存してはならない
* audit log には detector 種別、件数、位置情報、hash のみ保存できる
* redacted prompt preview を保存する場合、実値が含まれないことを保証する
* redaction 後の prompt を Agent に渡す場合、Agent が実値を復元できないこと

### 7.4 限界

LLM Gateway は、すべての PII / secret 検出を保証しない。

特に以下は MVP では限定的な対応とする。

* 画像 / PDF / 添付ファイル内の情報
* 自然文に埋め込まれた個人情報
* 顧客固有の内部 ID
* 業界固有の機密語彙

必要に応じて profile ごとの custom detector を追加できる設計にする。

---

## 8. Human Approval / Workflow Gates

### 8.1 gate が必要な条件

以下の場合、LLM Gateway は Workflow Gate を作成または要求できる。

* 高額 model を使用する
* spend cap を超過する
* PII / secret 検出値を redaction せず送信しようとする
* policy override を行う
* `unknown_usage` の provider / model を使う
* strict profile で外部 LLM に repository context を送信する

### 8.2 gate 連携

Gate の例:

| Gate Type | 内容 |
|---|---|
| `llm_high_cost_model_approval` | 高額 model 利用承認 |
| `llm_spend_cap_override` | spend cap 超過承認 |
| `llm_pii_send_approval` | PII / secret 検出後の送信承認 |
| `llm_policy_override` | profile policy override |

### 8.3 必須要件

* gate が `pending` / `blocked` の場合、対象 request を送信してはならない
* gate を `open` にする操作は人間または信頼できる外部結果に限定する
* 承認者、理由、期限、対象 request hash を audit log に残す
* approval 後も secret 実値を Notion に保存してはならない

---

## 9. Audit Log

### 9.1 保存する情報

LLM Gateway は request ごとに以下を保存する。

| Field | 説明 |
|---|---|
| `request_id` | LLM Gateway request ID |
| `profile_name` | profile |
| `workflow_id` | workflow |
| `phase` | phase |
| `run_id` | Agent run |
| `worker_id` | worker / Agent |
| `provider` | provider |
| `model` | model |
| `prompt_hash` | prompt / context の hash |
| `redacted_prompt_hash` | redaction 後の hash |
| `detectors_triggered` | 発火した detector |
| `redaction_count` | redaction 件数 |
| `estimated_input_tokens` | 入力 token 概算 |
| `estimated_output_tokens` | 出力 token 概算 |
| `estimated_cost_jpy` | コスト概算 |
| `pricing_table_version` | pricing table version |
| `decision` | allow / redact / warn / block / require_human_approval |
| `gate_id` | 関連 gate |
| `created_at` | 作成日時 |

### 9.2 保存してはならない情報

以下は原則として保存しない。

* secret 実値
* PII 実値
* private key 本文
* access token
* 完全な prompt 本文

full prompt 保存は default off とし、明示的な debug mode でも redaction 済みのみ許可する。

---

## 10. Notion / Operations Console 表示

### 10.1 Notion

Notion には、人間判断に必要な概要のみ同期する。

同期候補:

* profile
* workflow
* phase
* provider / model
* decision
* spend estimate
* detector 種別と件数
* gate status
* approval reason
* approver

同期しないもの:

* secret 実値
* PII 実値
* full prompt
* private repository content の全文

### 10.2 Operations Console

Operations Console では以下を表示する。

* profile / workflow 別 spend
* blocked request count
* pending LLM approval gate
* redaction event count
* provider / model usage
* unknown usage count
* LLM Gateway policy load status

復旧操作:

* dry-run 結果の確認
* policy reload
* blocked request の詳細確認
* human approval gate への導線
* audit log export

---

## 11. CLI 要件

### 11.1 想定コマンド

```bash
# profile の LLM Gateway policy を確認
hokusai llm-gateway policy --profile a-company

# prompt / context の dry-run 検査
hokusai llm-gateway check --profile a-company --input prompt.txt

# workflow の LLM usage を確認
hokusai llm-gateway usage --workflow <workflow-id>

# audit log を出力
hokusai llm-gateway audit --workflow <workflow-id>
```

### 11.2 dry-run

dry-run は実際の LLM 送信を行わず、以下を出力する。

* policy load 結果
* provider / model check 結果
* detector 結果
* 想定 decision
* redacted preview
* estimated cost

---

## 12. 既存機能との関係

| 既存 / 検討中機能 | 関係 |
|---|---|
| Profile | LLM Gateway policy の適用単位 |
| Policy Governance Framework | policy violation / waiver / mode と接続 |
| Human Governance Workgraph | LLM approval gate、blocked Work Item と接続 |
| Persistent Project Memory | Agent に渡す context の送信前チェック対象 |
| `hokusai prime` | 出力 context の policy / redaction 対象 |
| Operations Console | spend / redaction / block / approval の表示 |
| Notion sync outbox | gate / audit summary の再送 |
| Auth Proxy | secret 実値を Agent から分離する後続機能 |

---

## 13. 実装方針

### 13.1 module 構成案

```text
hokusai/
  llm_gateway/
    __init__.py
    config.py
    policy.py
    interceptor.py
    detectors.py
    redaction.py
    spend.py
    pricing.py
    audit.py
    decisions.py
```

### 13.2 integration point

最初に棚卸しすべき箇所:

* Claude Code 起動箇所
* Codex 起動箇所
* Gemini CLI / Cross-LLM Review 起動箇所
* prompt 組み立て箇所
* profile config loader
* audit log writer
* Operations Console API
* Notion gate / sync outbox

### 13.3 rollout

段階的に導入する。

1. policy schema と config loader
2. dry-run checker
3. detector / redaction
4. audit log
5. provider / model allowlist
6. spend tracking
7. block / warn / log decision
8. Human Approval gate 連携
9. Operations Console 表示

---

## 14. 受け入れ基準

* profile config で LLM Gateway を有効 / 無効にできる
* provider / model allowlist に違反する request が block される
* high cost model 利用時に Human Approval gate を要求できる
* workflow / phase / profile 単位で usage / cost estimate が記録される
* spend cap 超過時に block または approval required にできる
* email / phone / credit card / My Number / secret-like token を検出できる
* redaction 後の prompt を生成できる
* secret / PII 実値が audit log / Notion / Operations Console に保存されない
* dry-run で送信前 decision を確認できる
* audit log に request_id / profile / workflow / phase / provider / model / decision が残る
* Operations Console で spend / redaction / blocked request を確認できる
* Notion gate が pending の場合、対象 request が送信されない

---

## 15. 未決事項

* pricing table をどこで管理するか
* provider usage が取れない場合の default fail mode
* full prompt を debug 用に保存する余地を残すか
* custom detector の定義形式
* LLM Gateway をどの phase から mandatory にするか
* Auth Proxy との境界と導入時期
* GitHub Copilot / Claude Code 等、外部 CLI の usage 推定方法

