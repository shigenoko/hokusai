HOKUSAI Policy Governance Framework 要件定義書

**作成日**: 2026-05-12

**改訂履歴**

| 日付 | 改訂内容 |
|---|---|
| 2026-05-12 | 初版 |
| 2026-05-12 | レビュー指摘反映: §3.4/§3.5 既存実装連動、§4.0 Pack 区分（Built-in/Community/Private）、§6.7 競合解決、§6.8 継承、§7.4 context window 管理、§8.0 Phase マッピング、§8.2 Cross LLM Review の注記、§8.3 LLM 非決定性対策、§8.4 報告書形式、§10.4 dry-run、§10.5 配布・更新、§11.3/§11.4 統合、§11.5 Rule Waiver、§13.2.3 monorepo/container、§13.2.5 タイトル修正。主要なレビュー指摘を反映 |

---

1. 概要

1.1 目的

HOKUSAI において、生成AI（LLM / Agent）によるコード生成・レビュー・実装支援を安全かつ組織的に運用するため、各種セキュリティ規格・品質ルール・業界ガイドラインを「Policy Pack」として管理し、AI Agent に適用可能とするガバナンス基盤を実現する。

本機能は、特定の静的解析製品への依存を目的とせず、

* AIへの事前ルール適用
* AIレビュー
* Human-Orchestrated Governance
* Workflow制御

を統合的に実現する。

1.2 HOKUSAI が担う責務

HOKUSAI は、PCI DSS / SOC2 / HIPAA 等の規格準拠そのものを保証するシステムではない。

HOKUSAI が担う責務は以下とする。

* 組織・案件ごとのルールを Policy Pack として機械可読化する
* AI Agent に対して、実装前・レビュー時に適用すべきルールを明示する
* ルール違反の検出、修正提案、Human Approval への接続を支援する
* どの Policy Pack / mode / version が適用されたかを記録する
* 人間が最終判断を行うための監査証跡を残す

1.3 対象外

以下は本機能の直接的な責務には含めない。

* 法的・監査上の規格準拠の保証
* 特定の静的解析製品と同等の完全な脆弱性検出
* LLM の判断結果のみを根拠とした自動承認
* 機密情報、個人情報、カード情報等の安全な取り扱いを HOKUSAI 単体で保証すること

これらは、外部監査、静的解析、秘密情報管理、CI/CD、Human Review と組み合わせて運用する。

⸻

2. 背景

生成AIによるコード生成は高い生産性を持つ一方で、以下の課題が存在する。

* セキュリティ脆弱性の混入
* 業界規制違反
* 組織ルール逸脱
* LLMごとの品質差異
* AIによる危険コード生成
* AIレビューの非決定性

そのため、

AIを禁止する

ではなく、

AIを組織ルールの下で統制する

ことが重要となる。

HOKUSAI では、Human-Orchestrated の思想に基づき、AI Agent の自由度を維持しながら、組織ポリシーによる統制を実現する。

2.1 実用上の前提

AI Agent に規格名だけを渡しても、安定した検査品質は得られない。

そのため Policy Pack は、OWASP / CWE / PCI DSS / SOC2 等の名称をそのまま実行単位とするのではなく、HOKUSAI が適用可能な具体ルールへ分解する。

例：

* 規格・分類: OWASP Top 10 / Injection
* HOKUSAI ルール: SQL文字列連結によるクエリ生成を禁止する
* 適用方法: 実装プロンプトに制約として注入し、レビュー時にチェック項目として検証する
* 検出方式: LLM review / static scan / human review のいずれで確認するかを明示する

⸻

3. システムコンセプト

3.1 基本思想

HOKUSAI は特定規格を固定実装するのではなく、

Policy Orchestration Platform

として動作する。

⸻

3.2 ポリシー適用モデル

Human
   ↓
Policy Pack選択
   ↓
HOKUSAI
   ↓
AI Agent群
   ↓
実装 / レビュー

⸻

3.3 Human-Orchestrated Governance

最終判断は人間が行う。

AI Agent は以下を支援する。

* 実装
* レビュー
* ルール適用
* 品質監査
* 修正提案

3.4 Profile との関係

Policy Pack は HOKUSAI profile（v0.3.0 で導入）と連動可能とする。

複数案件を並列運用する場合、案件ごとに求められるセキュリティ強度、承認フロー、監査粒度が異なるため、profile 単位で有効化する Policy Pack / mode を切り替えられる必要がある。

例：

* A社: strict + security oriented policy
* B社: balanced + SaaS oriented policy
* OSS案件: speed + OSS policy

3.4.1 profile config への組み込み

既存の `~/.hokusai/profiles.yaml` を拡張し、profile ごとに有効 Policy Pack / mode / waivers を指定できる。

例：

```yaml
profiles:
  a-company:
    config: ~/work/hokusai-configs/a-company.yaml
    data_dir: ~/.hokusai/profiles/a-company
    dashboard:
      port: 8765
    policy:
      enabled_packs:
        - core-security
        - pci-dss
      mode: strict
      waivers_file: ~/work/a-company/policy-waivers.yaml  # 任意
  b-company:
    config: ~/work/hokusai-configs/b-company.yaml
    policy:
      enabled_packs:
        - core-security
        - soc2
      mode: balanced
  oss-project:
    config: ~/work/hokusai-configs/oss.yaml
    policy:
      enabled_packs:
        - core-security
        - oss
      mode: speed
```

profile 個別の Policy 設定が無い場合、HOKUSAI のグローバルデフォルト（後述）を適用する。

3.5 既存 HOKUSAI 機能との関係

本フレームワークは新規追加ではなく、v0.2.x / v0.3.0 で実装済みの機能と整合する形で組み込む。

| 既存機能 | 関係 |
|---|---|
| `cross_review` config（v0.2.0） | §8.2 Cross LLM Review として吸収・統合。既存 cross_review 設定は後方互換として残し、Policy Pack 経由の指定に段階移行 |
| `review_checklist` config（v0.2.x） | review_rule に置き換え可能。既存 checklist は移行ツールで Policy Pack 形式に変換可能とする |
| Notion Workflows DB | Policy 違反 / waiver / 承認結果を専用プロパティ（`Policy Violations` / `Policy Summary` 等）で表示 |
| Operations Console | Policy 適用結果ページを追加（profile / pack / mode / 違反件数の一覧） |
| profile registry（v0.3.0） | §3.4.1 の通り、profile config の `policy:` セクションとして組み込む |
| HITL（`waiting_for_human`） | Policy の `require_human` action が同経路に乗る |

⸻

4. 対象ポリシー

4.0 Policy Pack の供給区分

Policy Pack の出所と保守責任を明示するため、以下の 3 区分を設ける。

| 区分 | 配布元 | 保守 | 例 |
|---|---|---|---|
| **Built-in** | HOKUSAI 同梱 | HOKUSAI リリースで更新 | core-security / cwe-baseline |
| **Community** | コミュニティ提供（pip / npm package、git repo） | 各メンテナ | pci-dss-pack / hipaa-pack |
| **Private** | 組織独自、profile config 配下に配置 | 組織自身 | acme-corp-internal-rules |

HOKUSAI は標準で **Core Policy**（§4.1）を Built-in として同梱する。Optional Pack（§5）は順次 Built-in 同梱または Community 配布で提供する。組織固有ルールは Private として profile config 経由で読み込む。

4.1 Core Policy（常時適用推奨）

4.1.1 OWASP Top 10

対象：

* Web
* API
* Cloud
* Mobile

主な内容：

* Injection
* XSS
* Broken Access Control
* Cryptographic Failure
* SSRF

⸻

4.1.2 CWE

Common Weakness Enumeration

対象：

* ソフトウェア脆弱性分類

主な内容：

* Hardcoded Secret
* Unsafe Deserialization
* Weak Crypto
* Command Injection

⸻

4.1.3 Secure Coding Rules

HOKUSAI独自ルール。

例：

* hardcoded secret禁止
* logging rule
* exception handling rule
* dependency rule
* unsafe crypto禁止

⸻

5. Optional Policy Pack

5.1 PCI DSS Pack

対象：

* 決済
* 金融
* カード情報取扱

追加制約例：

* 強制監査ログ
* strict encryption
* human approval mandatory
* card data masking

⸻

5.2 SOC2 Pack

対象：

* SaaS
* クラウドサービス

内容：

* auditability
* access control
* operational logging

⸻

5.3 Startup Pack

対象：

* 小規模高速開発

特徴：

* speed priority
* minimal blocking
* warning-centric

⸻

5.4 OSS Pack

対象：

* OSSプロジェクト

特徴：

* readability
* contributor friendliness
* maintainability

⸻

5.5 HIPAA Pack（将来対応）

対象：

* 医療

5.6 複数 Pack 同時有効化時の挙動

Pack を組み合わせて使う想定（例: OSS スタートアップ案件で `oss` + `startup` を併用）。同一 rule_id / 同等検出を持つ場合の競合解決は §6.7 に従う。

⸻

6. Policy Pack 構成

6.1 YAML定義

Policy Pack は、規格名の列挙だけでなく、HOKUSAI が実行時に利用できる具体ルールを持つ。

例：

policy_pack:
  id: core-security
  name: Core Security Policy
  version: 1.0.0
  origin: built-in              # built-in / community / private （§4.0）
  extends: []                   # 親 Pack（§6.8）。継承先 rule を inherit
  references:
    - owasp-top10
    - cwe
  rules:
    - id: SEC001
      name: Hardcoded Secret
      category: security
      severity: critical
      action: deny
      applies_to:
        phases: [5, 6]          # HOKUSAI phase の明示（§8.0）
        languages: [any]        # any / python / javascript / java など（§6.x）
        rule_types: [prompt_rule, review_rule, machine_rule]
      mandatory_in_prompt: true # context budget 不足時も省略不可（§7.4）
      prompts:                  # i18n 対応（§6.1.1）
        en: API keys, tokens, passwords must not be hardcoded.
        ja: API キー、トークン、パスワードはコードに直書きしない。
      detection:
        llm_review: true
        static_scan: true       # 例: gitleaks / trufflehog
        human_review: false
    - id: SEC002
      name: SQL Injection
      category: security
      severity: critical
      action: deny
      applies_to:
        phases: [5, 6]
        languages: [python, javascript, typescript, java, ruby, php, go]
        rule_types: [prompt_rule, review_rule]
      mandatory_in_prompt: true
      prompts:
        en: Avoid unsafe SQL string concatenation and use parameterized queries.
        ja: SQL の文字列連結を避け、パラメータ化クエリを使うこと。
      detection:
        llm_review: true
        static_scan: false
        human_review: true
  workflow_controls:
    human_approval: required
    security_review: required

6.1.1 prompt の多言語対応

`prompts` は `en` / `ja` 等の言語キーを持つ map とする。日本語 LLM / 日本語コードベースに対しては `ja` を優先、未定義の場合は `en` にフォールバックする。`prompt` 単一文字列は legacy として `en` 扱いで読み込む。

⸻

6.2 ON/OFF切替

Policy Pack は ON/OFF 可能とする。

例：

enabled_policies:
  - core-security
  - oss

⸻

6.3 モード切替

strict

金融向け。

balanced

通常開発向け。

speed

高速開発向け。

6.4 Rule 種別

Policy Pack 内のルールは、検出・適用方法に応じて分類する。

* prompt_rule: AI Agent への事前指示として適用するルール
* review_rule: AI Review のチェックリストとして適用するルール
* workflow_rule: Human Approval や追加レビューを要求するルール
* machine_rule: 静的解析、secret scan、lint、test 等で機械的に検出するルール
* audit_rule: 証跡として記録すべき情報を定義するルール

6.5 action

ルール違反時の扱いは action で制御する。

* deny: 違反時にワークフローをブロックする
* warn: 警告として記録し、ワークフローは継続可能とする
* require_human: 人間の承認待ちに遷移する
* record_only: 判定には使わず、監査証跡として記録する

6.6 mode による action 解釈

同一 Policy Pack でも mode によって運用強度を変えられる。

* strict: critical / high の deny を必ずブロックし、human approval を強制する
* balanced: critical はブロックし、medium 以下は warn を基本とする
* speed: 明確な危険のみブロックし、それ以外は warn / record_only を基本とする

6.7 複数 Policy Pack 競合時の解決ルール

複数 Pack を同時有効化した場合、同一 rule_id または同等検出を持つ rule の競合解決ルールを以下とする。

**rule_id が同一の場合**:

1. 後勝ち（`enabled_packs` の配列順で後方が上書き）— 一見直感的だが意図しない緩和を招く可能性
2. **strict 勝ち**（より厳しい severity / より厳しい action を採用）— **本書の推奨**
3. merge（detection.llm_review などの真偽値は OR、prompt は連結）

→ 採用方針: **strict 勝ち**（severity: critical > high > medium > low、action: deny > require_human > warn > record_only）。HOKUSAI ログには「どの Pack の rule が優勢になったか」を記録する。

**rule_id が異なるが同等検出の場合**:

検出ロジックの重複（SEC001 と PCI001 が共に Hardcoded Secret を検出する等）は、Pack 設計者の責務として `equivalent_to` フィールドで関連付ける。

```yaml
rules:
  - id: PCI001
    equivalent_to: [SEC001]
    ...
```

HOKUSAI は equivalent_to 群のうち strict 勝ちで 1 つを有効化し、他を suppress する。記録は両方の rule_id を保持する。

6.8 Policy Pack の継承

PCI DSS Pack は実質 Core Security Pack の上に重ねるケースが多いため、明示的な継承機構を提供する。

```yaml
policy_pack:
  id: pci-dss
  version: 1.0.0
  extends:
    - core-security    # 親 Pack（同一 HOKUSAI 環境で enabled 必須）
  rules:
    - id: PCI001       # PCI 独自 rule のみここに記述
      ...
```

`extends` で指定された親 Pack の rule は自動継承される。親と同じ rule_id を子で再定義した場合は **子勝ち**（明示的 override として扱う）。親 Pack が enabled でない時、子 Pack も使えない（依存解決エラー）。

⸻

7. AI Agent 連携

7.1 対応対象

* Claude Code
* Gemini CLI
* Codex
* Cursor
* Devin
* その他 LLM Agent（vendor non-dependency / §12.1）

⸻

7.2 AIへのルール適用

以下の形式でAIへルールを渡す。

* Prompt Template
* CLAUDE.md
* AGENTS.md
* YAML Policy
* JSON Policy

⸻

7.3 実装前ルール適用

AI実装前にポリシーを読み込ませる。

例：

- SQL Injection禁止
- Hardcoded Secret禁止
- PCI DSSを遵守

7.4 実装プロンプトへの注入要件

HOKUSAI は、実装 Agent に対して以下の情報を渡す。

* 有効な Policy Pack 一覧
* 適用 mode
* 実装時に守るべき prompt_rule
* 違反時にブロックされる deny rule
* 人間承認が必要となる workflow_rule

規格名のみを渡すのではなく、Agent が具体的に従える禁止事項・推奨事項・確認事項として展開する。

7.4.1 Context Window 管理

LLM の context window は有限であるため、enabled rule をすべてプロンプトに展開する素朴な実装は、rule 数増加に伴い実装プロンプトを圧迫し品質低下を招く。

HOKUSAI は以下の戦略を組み合わせて context budget を管理する。

* **mandatory / optional の区分**: rule に `mandatory_in_prompt: true` を持つものは context budget 不足時も省略しない。`mandatory_in_prompt: false` は容量に応じて省略可
* **変更ファイルベースの動的選択**: 変更ファイルの言語・パスから `applies_to.languages` / `applies_to.paths` を照合し、関連 rule のみ注入
* **カテゴリ単位の要約展開**: critical / high の deny rule は full prompt、medium / low の warn rule はカテゴリ名 + 件数のみ
* **Phase 別の rule 種別フィルタ**: Phase 5（実装）には `prompt_rule`、Phase 6（検証）には `machine_rule`、Phase 7（最終レビュー）には `review_rule` を中心に渡す（§8.0）

context budget 上限（モデル context window の何 % を rule 列挙に充てるか）は profile config で設定可能とする（デフォルト: 10%）。

⸻

8. AI Review Workflow

8.0 HOKUSAI Phase と Policy 適用のマッピング

HOKUSAI の workflow における Policy 適用箇所を明示する。Phase 名は現行 HOKUSAI 実装に合わせる。

| Phase | 内容 | 適用される rule 種別 | 主な制御 |
|---|---|---|---|
| Phase 1 | branch / worktree 準備 | audit_rule のみ（適用 Pack を記録） | — |
| Phase 2 | research / 要件確認 | workflow_rule（情報収集の制約）| — |
| Phase 3 | design | review_rule（設計選択肢のチェック） | warn / record_only 中心 |
| Phase 4 | plan | review_rule | — |
| **Phase 5** | **implementation** | **prompt_rule（実装前注入）** | deny rule を Agent に明示 |
| **Phase 6** | **verify / test / static-scan** | **machine_rule（lint / test / dependency scan 等）** | 検証失敗または deny → Phase 5 へ差し戻し / block |
| **Phase 7** | **final review** | **review_rule + workflow_rule** | deny → block、warn → 続行、require_human → HITL |
| Phase 7.5 | branch hygiene | machine_rule / audit_rule | PR 前のブランチ衛生チェック |
| Phase 8 | PR 作成・統合レビュー対応 | audit_rule / workflow_rule | 違反 / waiver / 承認結果を PR コメント / Notion / JSONL に記録 |
| Phase 9 | PR review / merge wait | workflow_rule（human_approval 確認） | 承認待ち・差し戻し |
| Phase 10 | cleanup | audit_rule（最終証跡保存） | — |

各 Phase で発火する rule 種別を限定することで、§7.4.1 の context budget 管理にも貢献する。

8.1 Workflow構成

Implementation Agent
        ↓
Security Review Agent
        ↓
Architecture Review Agent
        ↓
Human Approval

⸻

8.2 Cross LLM Review

異なるLLMによる相互レビューを可能とする。

例：

Claude実装
 ↓
Geminiレビュー
 ↓
Codex修正 / または Claude（実装者）に戻して修正

修正の担当 LLM は profile config（mode / cross_review）に従って選択する。HOKUSAI v0.2.0 で実装済みの `cross_review` 設定とは互換維持しつつ、Policy Pack 経由でも指定可能にする（§3.5）。

8.3 レビュー時の Policy 適用

AI Review では、有効な Policy Pack から review_rule を抽出し、レビュー用チェックリストとして適用する。

レビュー結果はルールID単位で記録する。

例：

| rule_id | result | severity | note |
| SEC001 | OK | critical | - |
| SEC002 | NG | critical | user_id を文字列連結して SQL に埋め込んでいる |

NG が発生した場合の制御は、rule の action と mode によって決定する。

8.3.1 LLM Review の非決定性対策

LLM による review は同じコードでも結果が揺らぐ性質を持つ。HOKUSAI は以下の対策を組み合わせて非決定性を緩和する。

* **構造化出力の強制**: LLM judge には JSON Schema を渡し、`{rule_id, result, severity, evidence_lines, note}` 形式での出力を強制する
* **多数決方式（オプション）**: critical rule のみ、同一 LLM に温度を変えて 3 回実行し多数決を取る（コスト次第で mode 設定により切替）
* **Cross check**: 同じ rule が `llm_review: true` と `static_scan: true` の両方を持つ場合、両者が一致しない時は `require_human` に昇格
* **Evidence の必須化**: NG 判定時は `evidence_lines`（該当コード行範囲）を必須項目とし、根拠の無い NG は無効化する

8.4 報告書形式

レビュー結果（NG / warn / record_only）は profile config 経由で出力先と形式を指定可能とする。

* **PR コメント**: GitHub / GitLab に Markdown でテーブル投稿（デフォルト）
* **Notion Workflows DB**: 新規 `Policy Violations` / `Policy Summary` プロパティに JSON または要約テキストで記録
* **Operations Console**: profile ごとの違反履歴ページに表示
* **JSONL log**: profile data_dir 配下に `<workflow_id>-policy.jsonl` として保存（監査・再生・学習データ用途）

⸻

9. Rule と Workflow の分離

9.1 Rule

コード品質・セキュリティルール。

例：

hardcoded_secret: deny

⸻

9.2 Workflow

レビュー・承認フロー。

例：

human_approval: required

⸻

9.3 分離理由

同一規格でも運用強度が異なるため。

例：

* strict PCI
* relaxed PCI

⸻

10. Policy as Code

10.1 方針

組織ルールを機械可読化する。

⸻

10.2 対応形式

* YAML
* JSON
* Markdown

10.2.1 推奨形式

実行時に HOKUSAI が解釈する Policy Pack は YAML または JSON を推奨する。

Markdown は、人間向けの説明、背景、規格解説、運用手順を記述するために使用する。

10.2.2 バージョン管理

Policy Pack は version を持ち、workflow 実行時に適用された version を記録する。

同じ Policy Pack 名でも version が異なる場合、監査上は別の適用結果として扱う。

⸻

10.3 将来構想

* MCP対応
* API経由ルール取得
* Dynamic Policy Resolution
* Agent Runtime Policy Injection

10.4 Policy Pack の validation / dry-run

Policy Pack そのものが誤っているケース（rule が広すぎる / 誤検知が多い / YAML 構文エラー）を検出するため、HOKUSAI は以下のサポートコマンドを提供する。

```bash
# 構文・スキーマ・rule_id 重複・extends の存在等を検証
hokusai policy validate <pack-path>

# 既存リポジトリに当てて検知件数・false positive 候補を見る（実 workflow は走らない）
hokusai policy dry-run <pack-id> --target <repo-path>

# Pack ごとのテストケース実行
hokusai policy test <pack-path>
```

Policy Pack 配布パッケージには `tests/` ディレクトリを置き、`positive_examples`（rule に hit すべきコード）と `negative_examples`（hit しないコード）を含めることを推奨する。これにより Pack の精度を継続的に検証可能とする。

10.5 配布と更新

Policy Pack の配布経路は §4.0 の区分に従う。

| 区分 | 配布方法 | 更新方法 |
|---|---|---|
| Built-in | HOKUSAI 同梱（pip install hokusai-flow） | HOKUSAI バージョン更新で反映 |
| Community | pip / npm パッケージ、git リポジトリ | `hokusai policy fetch <source>` で取得、`hokusai policy update` で更新 |
| Private | profile config 配下にローカル配置 | 組織独自フローで管理 |

`hokusai policy list` で適用中の Pack 名 / version / origin を一覧表示し、`hokusai policy update --check` で新版の有無を確認できるようにする。

⸻

11. Human Governance

11.1 AIの位置づけ

AIは支援者とする。

⸻

11.2 最終責任

人間が持つ。

⸻

11.3 Human Approval

以下を可能とする（ただし §11.4 の制約に従う）。

* **mandatory approval**: 全ワークフローで人間承認を要求
* **conditional approval**: 一定条件下（low severity のみ等）で人間承認を要求
* **auto approval**: AI Review がすべて OK / warn 以下なら自動承認

11.4 Approval の制約

auto approval は、すべての Policy Pack / mode で許可されるものではない。

critical な deny rule、または human_approval: required を含む workflow_rule が有効な場合、AI の判断のみで承認完了としてはならない。

Human Approval では、少なくとも以下を確認可能とする。

* 適用された Policy Pack / version / mode
* NG となった rule
* warn / record_only として記録された rule
* AI Agent による修正提案
* 人間が承認・却下した理由
* 適用された Rule Waiver の一覧（§11.5）

11.5 Rule Waiver（例外申請）

特定のコード変更について、有効な rule を一時的に **warn 化 / skip** する申請手続きを提供する。allowlist（§13.2.6）は dependency vuln 限定の概念であるのに対し、Rule Waiver はコード rule 全般の例外申請を扱う。

waiver は必ず以下を含む。

* rule_id（例外化する rule）
* scope: `pr:<number>` / `file:<path>` / `commit:<sha>` / `period:<until>`
* reason: 例外を許容する理由
* approver: 承認者（人間）
* expires: 有効期限（必須）
* fallback_action: 例外期間中の扱い（warn / record_only / skip）

例（profile 配下の `policy-waivers.yaml`）：

```yaml
waivers:
  - rule_id: SEC042
    scope: "pr:123"
    reason: "本 PR はリファクタリングのみで本来 SEC042 検出対象ではない"
    approver: shigeta
    expires: 2026-05-31
    fallback_action: record_only
  - rule_id: SEC015
    scope: "file:legacy/auth.py"
    reason: "legacy 認証コードは別マイグレーション project（PR #200）で対応中"
    approver: shigeta
    expires: 2026-08-31
    fallback_action: warn
```

waiver の制約：

* **expires は必須**で、過ぎたら自動的に無効
* **critical / deny rule への waiver は、特権ユーザー（profile 管理者）のみ承認可能**
* waiver の追加 / 更新 / 削除はすべて監査ログに記録される
* waiver が適用された rule は、Human Approval 画面で必ず明示される

⸻

12. 非機能要件

12.1 Vendor Non-Dependency

特定LLMへの依存を避ける。

⸻

12.2 Extensibility

Policy Pack追加可能とする。

⸻

12.3 Auditability

適用ポリシー履歴を保持。

保持対象：

* workflow_id
* profile_name
* policy_pack_id
* policy_pack_version
* mode
* 有効化された rule_id
* rule ごとの result
* block / warn / require_human の判定結果
* human approval の結果
* 実行時刻

⸻

12.4 Multi-Agent Support

複数Agent連携を可能とする。

⸻

13. 将来拡張

13.1 AI Security Pack

* prompt injection
* hallucination risk
* agent sandbox rule

⸻

13.2 Supply Chain Security

* SBOM
* SLSA
* Dependency Governance
* Vulnerability Advisory Integration

13.2.1 Dependency Vulnerability Governance

CVE / OSV / GitHub Advisory Database / NVD 等の脆弱性情報を参照し、依存ライブラリのリスクを検出・記録・制御する。

本機能は、脆弱性のあるライブラリを機械的に削除することを目的としない。

依存ライブラリはアプリケーションの実行・ビルド・互換性に影響するため、HOKUSAI は以下を支援する。

* 脆弱性のある依存関係の検出
* 影響を受ける package / ecosystem / version の特定
* fixed version の提示
* update / replace / allowlist / block の判断支援
* Human Approval への接続
* 監査証跡の保存

13.2.2 情報源

利用可能な脆弱性情報源は以下とする。

* OSV.dev
* GitHub Advisory Database
* NVD
* package manager / ecosystem 固有の advisory

CVE は脆弱性識別子として有用だが、依存ライブラリの判定には package name、ecosystem、affected version、fixed version が必要となる。

そのため、CVE ID のみを判定根拠とせず、OSV ID、GHSA ID、affected package、fixed version 等と紐づけて扱う。

13.2.3 対象ファイル

HOKUSAI は、対象リポジトリ内の依存関係ファイルを検出し、脆弱性確認の入力とする。

例：

* package-lock.json
* pnpm-lock.yaml
* yarn.lock
* uv.lock
* poetry.lock
* requirements.txt
* go.sum
* Cargo.lock
* pom.xml
* build.gradle
* composer.lock

将来的には SBOM（CycloneDX / SPDX）を入力として扱えるようにする。

**monorepo / nested project 対応**

pnpm workspaces / lerna / nx / yarn workspaces 等の monorepo 構造では、ルート以外の lock file も再帰的に検出する。例：

```
repo/
├── package.json
├── pnpm-lock.yaml         # ルート
├── packages/
│   ├── app-web/
│   │   └── package.json   # workspace 配下にロックファイルは無いが、ルートが管轄
│   └── app-api/
│       └── package.json
└── tools/
    └── migration-cli/
        ├── pyproject.toml
        └── poetry.lock    # 別エコシステムの独立ロック
```

profile config で `dependency_governance.monorepo_pattern` を指定して走査範囲を制御可能とする（default はリポジトリ全体走査）。

**Container / Dockerfile 対応**

`Dockerfile` の `FROM image:tag` で指定された base image の vulnerability も対象とする。Trivy / Grype 等の image scanner を osv-scanner と並行して呼ぶ構成を想定。

```yaml
dependency_governance:
  scanners:
    library: osv          # ライブラリ依存（package-lock.json 等）
    container: trivy      # base image（Dockerfile）
  containers:
    include:
      - "**/Dockerfile"
      - "**/Containerfile"
```

container 脆弱性は library 脆弱性と異なる重み付けが必要なケースが多いため（base image は短期間で migrate しづらい）、severity 閾値を別途設定可能とする。

13.2.4 Workflow 制御

脆弱性が検出された場合の制御は、Policy Pack の rule / action / mode に従う。

例：

dependency_governance:
  enabled: true
  sources:
    - osv
    - github_advisory
    - nvd
  block_severity:
    - critical
    - high
  on_vulnerable_dependency: require_human
  allowlist:
    - package: example-lib
      ecosystem: npm
      version: 1.2.3
      advisory: CVE-20XX-XXXX
      reason: exploitability not applicable in this service
      expires: 2026-06-30

13.2.5 対応の優先フロー

脆弱性のある依存関係が検出された場合、HOKUSAI は以下のフローで対応を促す（番号は優先順位ではなく分岐フローを意味する）。

1. **fixed version が存在** → fixed version への更新を提案
2. **transitive dependency 由来** → 上位 dependency を更新 / pin / override で解決調整
3. **fixed version が無い / 更新が破壊的** → 代替ライブラリへの置換を検討
4. **影響なしと判断可能（例: 該当 API を呼んでいない）** → 期限付き allowlist で記録
5. **危険度が高く回避策がない** → workflow を block、Human Approval 経路へ

13.2.6 Allowlist の制約

allowlist は、脆弱性を無視するための恒久的な例外として扱ってはならない。

allowlist には以下を必須とする。

* package
* ecosystem
* version または version range
* advisory ID（CVE / GHSA / OSV 等）
* 影響なしと判断した理由
* 承認者
* 有効期限

有効期限を過ぎた allowlist は無効とし、再評価を要求する。

13.2.7 Auditability

Dependency Vulnerability Governance では、少なくとも以下を記録する。

* workflow_id
* profile_name
* repository
* package
* ecosystem
* current version
* affected range
* fixed version
* advisory ID
* severity
* source
* action
* allowlist の有無
* human approval の結果
* 実行時刻

⸻

13.3 AI Quality Scoring

LLMごとの品質分析。

⸻

14. HOKUSAI における位置づけ

HOKUSAI は単なる Agent Orchestrator ではなく、

Human-Orchestrated AI Governance Platform

を目指す。

その中核として、

Policy Pack
+
Workflow Governance
+
Human Approval

を実装する。
