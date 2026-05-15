# HOKUSAI Human Governance Workgraph 要件定義書

**作成日**: 2026-05-14

**対象読者**: HOKUSAI 運用設計者、PM、テックリード、実装担当エンジニア

---

## 1. 概要

### 1.1 目的

HOKUSAI において、AI Agent の作業・レビュー指摘・承認待ち・案件固有の判断履歴を、人間が確認・判断できる形で Notion 上に構造化する。

本機能は、Agent の自律性を高めること自体を目的としない。

HOKUSAI の Human-Orchestrated 思想に基づき、以下を実現する。

* 作業依存関係を人間が把握できる
* Agent が blocked 作業を勝手に進めない
* レビュー指摘の修正漏れ・重複・再発を追跡できる
* 複数 Agent の作業衝突を防ぐ
* Human Approval / CI / Design / Security などの gate を明示化する
* 案件固有の判断・ルール・記憶を Notion に残し、Agent には要約して渡す

### 1.2 基本方針

HOKUSAI 内部 DB と Notion の責務を分ける。

| 場所 | 責務 |
|---|---|
| HOKUSAI SQLite / checkpoint | 実行状態、再開情報、内部制御、Agent 向け一時情報 |
| Notion | 人間が判断するための状態、blocker、gate、review issue、project memory、承認履歴 |
| Operations Console | ローカル環境診断、再送、復旧、profile 別 runtime 操作 |

Notion は「Agent のための内部キュー」ではなく、**人間が見て判断できる governance layer** として扱う。

### 1.3 非ゴール

以下は本要件の対象外とする。

* HOKUSAI を汎用 issue tracker に置き換えること
* Notion をリアルタイム実行エンジンとして扱うこと
* Agent が Notion 上の情報だけで自律的に承認・解除・waive すること
* 人間承認を不要にする完全自動 routing
* Dolt / beads 相当の DB バージョン管理基盤を HOKUSAI に導入すること

---

## 2. 対象機能

本要件で扱う機能は以下の 5 つとする。

1. Dependency-aware Work Queue
2. Review Issue Graph
3. Agent Claim / Lease
4. Workflow Gates
5. Persistent Project Memory

これらは独立機能ではなく、Notion 上で相互に relation を持つ Human Governance Workgraph として扱う。

---

## 3. 全体モデル

### 3.1 Notion DB 構成

既存の HOKUSAI Workflows DB / Pull Requests DB に加え、以下の DB を追加する。

| DB 名 | 役割 |
|---|---|
| HOKUSAI Work Items DB | 作業キュー、blocker、依存関係、Agent claim を管理 |
| HOKUSAI Review Issues DB | Copilot / human / HOKUSAI review の指摘を構造化 |
| HOKUSAI Gates DB | Human Approval / CI / Design / Security などの gate を管理 |
| HOKUSAI Project Memory DB | 案件ルール、設計判断、避けるべき実装、運用メモを管理 |

### 3.2 既存 DB との関係

| 既存 DB | 追加 relation |
|---|---|
| Workflows DB | Work Items / Review Issues / Gates / Project Memory |
| Pull Requests DB | Review Issues / Gates |

### 3.3 profile との関係

すべての新規 DB は profile 単位で分離できる必要がある。

各レコードには `Profile` または `Profile Name` を持たせる。

複数案件を並列運用する場合、A 社の blocker / gate / memory が B 社の workflow に混入してはならない。

---

## 4. Dependency-aware Work Queue

### 4.1 目的

作業単位を依存関係つきで Notion に表示し、人間が blocker を確認できるようにする。

Agent は `blocked` の作業を勝手に進めてはならない。

### 4.2 対象となる作業

Work Item として管理する対象は以下とする。

* Phase 4 で作成された実装計画の個別ステップ
* Phase 6 verification failure から発生した修正作業
* Phase 7 final review から発生した修正作業
* Phase 8 review loop で発生した Copilot / human review 対応
* Policy / Security / Design gate により追加された作業
* 人間が手動で追加した補助タスク

### 4.3 状態

Work Item は以下の状態を持つ。

| Status | 意味 |
|---|---|
| `todo` | 未着手 |
| `blocked` | 依存関係または gate により着手不可 |
| `ready` | 着手可能 |
| `in_progress` | Agent または人間が対応中 |
| `waiting_for_human` | 人間判断待ち |
| `done` | 完了 |
| `canceled` | 不要または取り下げ |

### 4.4 依存関係

Work Item は他の Work Item / Review Issue / Gate に依存できる。

依存種別:

* `blocks`
* `blocked_by`
* `parent_child`
* `discovered_from`
* `related`
* `duplicates`
* `supersedes`

### 4.5 ready 判定

HOKUSAI は以下を満たす Work Item のみ `ready` とする。

* すべての `blocked_by` Work Item が `done` または `canceled`
* 関連 gate が `open` または `not_required`
* active な lease が存在しない
* waiver / human approval が必要な場合は承認済み

### 4.6 Notion プロパティ案

| Property | Type | 説明 |
|---|---|---|
| Name | Title | 作業名 |
| Work Item ID | Text | HOKUSAI 内部 ID |
| Profile | Select / Text | profile 名 |
| Workflow | Relation | Workflows DB |
| Status | Select | `todo` / `blocked` / `ready` / `in_progress` / `waiting_for_human` / `done` / `canceled` |
| Priority | Select | `P0` / `P1` / `P2` / `P3` |
| Type | Select | `implementation` / `verification_fix` / `review_fix` / `design` / `security` / `manual` |
| Blocked By | Relation | Work Items DB self relation |
| Blocks | Relation | Work Items DB self relation |
| Related Review Issue | Relation | Review Issues DB |
| Related Gate | Relation | Gates DB |
| Claimed By | Text / Person | Agent 名または人間 |
| Lease Expires At | Date | lease 期限 |
| Source Phase | Number | 発生元 Phase |
| Created At | Date | 作成日時 |
| Updated At | Date | 更新日時 |

### 4.7 必須要件

* HOKUSAI は blocked Work Item を Agent に渡してはならない
* Agent が処理できる Work Item は `ready` のみとする
* Notion 上で blocker の理由を人間が追えること
* blocker 解消時に `ready` へ遷移できること
* 手動で `blocked` / `waiting_for_human` に変更された場合、HOKUSAI はそれを尊重すること

---

## 5. Review Issue Graph

### 5.1 目的

Copilot / human / HOKUSAI final review の指摘を Notion DB に構造化し、修正漏れ、重複、再発を追跡できるようにする。

### 5.2 対象

Review Issue として扱う対象:

* GitHub Copilot review comment
* 人間の PR review comment
* HOKUSAI Phase 7 final review の NG rule
* Cross-LLM review の指摘
* Policy Governance Framework 由来の違反
* Dependency Governance 由来の脆弱性指摘

### 5.3 状態

| Status | 意味 |
|---|---|
| `open` | 未対応 |
| `triaged` | 内容確認済み |
| `in_progress` | 修正中 |
| `fixed` | 修正済み |
| `verified` | 修正確認済み |
| `waived` | 人間判断で例外化 |
| `duplicate` | 重複指摘 |
| `wontfix` | 対応しない |

### 5.4 Graph link

Review Issue は以下の link を持つ。

* `blocks`
* `duplicates`
* `resolved_by_pr`
* `introduced_by_pr`
* `waived_by`
* `related_to`
* `supersedes`

### 5.5 Notion プロパティ案

| Property | Type | 説明 |
|---|---|---|
| Name | Title | 指摘タイトル |
| Review Issue ID | Text | HOKUSAI 内部 ID |
| Profile | Select / Text | profile 名 |
| Workflow | Relation | Workflows DB |
| Pull Request | Relation | Pull Requests DB |
| Source | Select | `copilot` / `human` / `hokusai` / `cross_llm` / `policy` / `dependency` |
| Status | Select | 状態 |
| Severity | Select | `critical` / `high` / `medium` / `low` / `info` |
| File | Text | 対象ファイル |
| Line | Number | 対象行 |
| Comment URL | URL | GitHub review comment 等 |
| Body | Rich Text | 指摘本文 |
| Blocks | Relation | Review Issues DB self relation |
| Duplicates | Relation | Review Issues DB self relation |
| Resolved By PR | Relation | Pull Requests DB |
| Waiver Reason | Rich Text | waived 理由 |
| Waiver Approver | Person / Text | 承認者 |
| Created At | Date | 作成日時 |
| Updated At | Date | 更新日時 |

### 5.6 必須要件

* 同じ PR review comment を重複登録しないこと
* GitHub comment ID / URL を保持し、再同期時に同一性を判定できること
* `waived` は人間承認なしに設定してはならない
* `fixed` だけでは完了扱いにせず、必要に応じて `verified` を要求できること
* 同一内容の指摘が再発した場合、過去の Review Issue と relation を持てること

---

## 6. Agent Claim / Lease

### 6.1 目的

複数 Agent / 複数人が並列で作業する場合に、同じ Work Item / Review Issue を同時に処理して衝突することを防ぐ。

### 6.2 Claim

Claim は「誰がこの作業を処理しているか」を表す。

Claim 主体:

* Claude Code
* Codex
* Gemini CLI
* GitHub Copilot
* human
* external agent

### 6.3 Lease

Lease は claim の有効期限を表す。

Agent が作業中に停止・失敗・放置された場合、lease 期限切れにより再割当できる。

### 6.4 Notion 表示

Notion では以下を見えるようにする。

* この作業は誰が処理中か
* いつ claim されたか
* lease はいつ切れるか
* 期限切れかどうか
* 再割当が必要か

### 6.5 Notion プロパティ案

Work Items DB / Review Issues DB に以下を追加する。

| Property | Type | 説明 |
|---|---|---|
| Claimed By | Text / Person | Agent 名または人間 |
| Claim Type | Select | `agent` / `human` |
| Lease Status | Select | `active` / `expired` / `released` |
| Lease Started At | Date | claim 開始時刻 |
| Lease Expires At | Date | claim 期限 |
| Lease Token | Text | HOKUSAI 内部用 token |

### 6.6 必須要件

* `ready` の Work Item を Agent が取得する際、HOKUSAI は lease を作成する
* active lease が存在する Work Item を別 Agent に渡してはならない
* lease が期限切れの場合、人間または HOKUSAI が再割当できる
* Agent が正常完了した場合、lease は release される
* lease の作成・延長・解放・期限切れは audit log に残す

---

## 7. Workflow Gates

### 7.1 目的

Human Approval、CI passed、Design approved、Security approved など、workflow の進行可否を決める条件を Notion 上の明示的な gate として管理する。

gate が開くまで、HOKUSAI は次の phase または対象 Work Item に進んではならない。

### 7.2 Gate 種別

| Gate Type | 意味 |
|---|---|
| `human_approval` | 人間承認 |
| `ci_passed` | CI 成功 |
| `design_approved` | デザイン承認 |
| `security_approved` | セキュリティ承認 |
| `policy_waiver_approved` | policy waiver 承認 |
| `dependency_risk_accepted` | 依存脆弱性リスク受容 |
| `timer` | 一定時間待機 |
| `external` | 外部システム判断 |

### 7.3 Gate 状態

| Status | 意味 |
|---|---|
| `not_required` | 不要 |
| `pending` | 判定待ち |
| `open` | 通過可能 |
| `blocked` | 通過不可 |
| `expired` | 期限切れ |
| `canceled` | 取り下げ |

### 7.4 Notion プロパティ案

| Property | Type | 説明 |
|---|---|---|
| Name | Title | Gate 名 |
| Gate ID | Text | HOKUSAI 内部 ID |
| Profile | Select / Text | profile 名 |
| Workflow | Relation | Workflows DB |
| Pull Request | Relation | Pull Requests DB |
| Work Item | Relation | Work Items DB |
| Review Issue | Relation | Review Issues DB |
| Gate Type | Select | gate 種別 |
| Status | Select | gate 状態 |
| Required By Phase | Number | どの Phase の前提か |
| Approver | Person / Text | 承認者 |
| Decision Reason | Rich Text | 判断理由 |
| Due At | Date | 期限 |
| Created At | Date | 作成日時 |
| Updated At | Date | 更新日時 |

### 7.5 必須要件

* `pending` / `blocked` の gate がある場合、対象 workflow は先に進まない
* `open` にする操作は human action または信頼できる外部結果に限定する
* CI gate は GitHub Actions / GitLab CI 等の結果と同期できること
* Design gate は Figma / Miro review 状態または人間承認に接続できること
* Security gate は Policy Governance Framework / Dependency Governance と接続できること
* gate の判断理由と承認者を audit trail として残すこと

---

## 8. Persistent Project Memory

### 8.1 目的

案件固有のルール、過去の設計判断、避けるべき実装、運用上の注意点を Notion に保存し、Agent に必要な分だけ要約して渡す。

### 8.2 Memory 種別

| Type | 例 |
|---|---|
| `project_rule` | この案件では API token を DB に保存しない |
| `architecture_decision` | 認証は既存 Gateway に寄せる |
| `avoidance` | Legacy Auth module は直接編集しない |
| `domain_knowledge` | 決済ステータスの業務上の意味 |
| `operations_note` | リリース時は B 社担当者の承認が必要 |
| `policy_note` | SOC2 監査対象ログの扱い |
| `handover_note` | A が Phase 5 で詰まった理由 / B 用に整理した「試したこと」一覧 / 未確認の前提 |

### 8.3 Notion プロパティ案

| Property | Type | 説明 |
|---|---|---|
| Name | Title | Memory タイトル |
| Memory ID | Text | HOKUSAI 内部 ID |
| Profile | Select / Text | profile 名 |
| Type | Select | memory 種別（§8.2 の表に挙げた値: `project_rule` / `architecture_decision` / `avoidance` / `domain_knowledge` / `operations_note` / `policy_note` / `handover_note`） |
| Status | Select | `draft` / `active` / `deprecated` / `rejected` |
| Content | Rich Text | 本文 |
| Summary | Rich Text | Agent に渡す短い要約 |
| Applies To | Multi-select | `phase2` / `phase3` / `phase5` / `phase7` / `phase8` 等 |
| Related Workflow | Relation | Workflows DB |
| Related PR | Relation | Pull Requests DB |
| Approved By | Person / Text | 承認者 |
| Approved At | Date | 承認日時 |
| Expires At | Date | 任意の期限 |
| Created At | Date | 作成日時 |
| Updated At | Date | 更新日時 |

### 8.4 Agent への注入

HOKUSAI は Agent 実行前に、対象 profile / phase / repository に関連する active memory を要約して prompt に注入できる。

想定コマンド:

```bash
hokusai prime <workflow-id>
hokusai --profile a-company prime <workflow-id>
```

`prime` は以下を出力する。

* workflow の現在状態
* ready Work Item
* open Review Issue
* pending Gate
* active Project Memory summary（`handover_note` を含む全 Type の active レコードが対象）
* 適用中 Policy Pack
* 次に人間判断が必要な事項

`handover_note` の注入は、対象 workflow が `Supersedes` リレーション（§9.3）で
旧 workflow に紐づいている場合に、その旧 workflow の Related Workflow に紐づく
active な `handover_note` を優先的に prompt 先頭付近に要約注入する。これにより
新オペレータ B は旧オペレータ A の経緯・試行・未確認前提を Agent が把握した状態で
作業を再開できる。

### 8.5 制約

* `draft` memory は Agent に渡さない
* `active` にするには人間承認を必要とする
* `deprecated` memory は履歴として残し、Agent には渡さない
* memory の編集・承認・廃止は audit log に残す
* Agent が自動生成した memory は必ず `draft` から開始する
* `handover_note` は、起票者 A が `draft` で残し、引き継ぎ先 B 自身または PM が
  `active` 化する。引き継ぎ完了後（新 workflow が一定 phase 進行後）に
  `deprecated` 化することを推奨し、無期限の active 化を避ける。

### 8.6 `handover_note` の使い分け基準

`handover_note` は他の memory 種別と寿命・対象範囲・承認フローが異なる。
混同しないよう以下の基準で使い分ける。

| 観点 | `handover_note` | `project_rule` / `architecture_decision` | `operations_note` / `policy_note` |
|---|---|---|---|
| 寿命 | 一過性（引き継ぎ完了で `deprecated`） | 恒久（明示的な廃止まで `active`） | 案件継続中は恒久 |
| 想定読者 | 次のオペレータ（B） | 案件横断の全 Agent / 全オペレータ | 運用担当 / コンプライアンス担当 |
| 起票者 | 引き継ぐ側のオペレータ A | PM / Tech Lead / Architect | 運用担当 / 法務 |
| 承認フロー | B 自身または PM が `active` 化 | PM / Tech Lead レビュー必須 | 案件責任者承認 |
| Prompt 注入スコープ | 同一 workflow chain（旧 wf を Supersedes で繋ぐ chain）に限定 | profile 全体 / 該当 phase | profile 全体 |
| 典型的内容 | 試したこと一覧、詰まった理由、未確認の前提、推奨次手 | 守るべきルール、設計判断、避けるべき実装 | リリース承認手順、監査ログ要件 |

判断に迷う場合の指針:

* 「次のオペレータだけが読めばよい一時情報」→ `handover_note`
* 「同じ案件で今後も全員が守るべきルール」→ `project_rule`
* 「設計上の決定で後から覆すとシステム整合性が壊れる」→ `architecture_decision`
* 「業務上の手順 / 担当者連絡」→ `operations_note`

---

## 9. HOKUSAI Workflow との統合

### 9.1 Phase 別の利用

| Phase | 利用する情報 |
|---|---|
| Phase 2 Research | active Project Memory、関連 Work Item、pending Gate |
| Phase 3 Design | architecture_decision、design gate、review issue history |
| Phase 4 Plan | Work Item 生成、依存関係設定、gate 生成 |
| Phase 5 Implement | ready Work Item のみ Agent に渡す、claim / lease 作成 |
| Phase 6 Verify | verification failure から Work Item / Review Issue を生成 |
| Phase 7 Review | Review Issue Graph 更新、security/design gate 更新 |
| Phase 8 PR Review Loop | Copilot / human comment を Review Issue として同期 |
| Phase 10 Record | memory candidate、gate result、audit trail を保存 |

### 9.2 Notion からの操作範囲

Notion から許可する操作:

* Work Item の優先度変更
* Work Item の manual block / unblock
* Gate の承認 / 却下
* Review Issue の triage / waive
* Project Memory の承認 / deprecated 化（`handover_note` 含む）
* Workflows DB レコードの Status=Canceled 化 / Cancel Reason 記入（§9.3 引き継ぎ運用）
* `Supersedes` リレーション設定（新 wf → 旧 wf）

Notion から許可しない操作:

* workflow start / continue の直接実行
* checkpoint の書き換え
* Agent 実行の直接開始
* secret 値の入力

実行系操作は CLI または Operations Console に限定する。

### 9.3 引き継ぎ運用フロー（A → B）

複数エンジニアで同じ profile を共有運用する場合、Engineer A が止めた workflow を
Engineer B が引き継ぐケースが発生する。本節はその標準フローを定義する。

#### 9.3.1 前提

* A と B は同一 profile を共有している（profile 共有テンプレートは v0.4.7 で導入済み、Issue #22）
* 旧 workflow と新 workflow は別 `workflow_id` を持つ
* Notion 上の Workflows DB レコードは `Supersedes` リレーション（self-link）で連結する

#### 9.3.2 ステップ

```
[Engineer A]
   ↓ Phase X で停止 or 別タスクへ移る判断
1. 旧 workflow を canceled 化
   - CLI:    hokusai status <wf-A>            # 現状確認
             hokusai cleanup <wf-A> --cancel  # 状態を Canceled に
   - Notion: Workflows DB の対象レコードで Status=Canceled、Cancel Reason を記入

2. handover_note を Project Memory に draft で作成（§8.6 参照）
   - Type=handover_note
   - Status=draft
   - Profile=対象 profile
   - Related Workflow=旧 wf-A
   - Content / Summary に「経緯」「試したこと」「詰まった理由」「未確認前提」「推奨次手」を記載
   - Applies To に該当 phase を選択（例: phase5）

[Engineer B]
3. handover_note を Status=active 化
   - B 自身または PM が承認（§8.5 制約に従う）
   - Notion 上で Approved By / Approved At を記入

4. 新規 workflow を起動
   - CLI:    hokusai --profile <p> start <task_url>
   - 起動後、Workflows DB の新レコードで Supersedes プロパティを旧 wf-A に設定する
     （Notion 側で手動 / または HOKUSAI が自動補完）

5. Agent prompt 注入
   - hokusai prime <wf-B> 実行時、Supersedes 経由で旧 wf-A に紐づく
     active handover_note が prompt 先頭付近に要約注入される（§8.4 参照）

[両者で監査]
6. audit log に supersedes link 設定と handover_note 承認の双方向が記録される
   （§11 監査要件参照）

[引き継ぎ完了後]
7. 引き継ぎ完了の判断（新 workflow が安定して進行 / Phase 完了等）後、A 起票の
   handover_note を Status=deprecated に変更する（無期限の active 化を避ける）
```

#### 9.3.3 Workflows DB schema 追加

引き継ぎ運用のため、Workflows DB に self-link relation を追加する:

| Property | Type | 説明 |
|---|---|---|
| `Supersedes` | Relation（Workflows DB 自身を指す self-link） | 引き継ぎ元 workflow（旧 wf-A）。引き継ぎが無い場合は空 |
| `Superseded By` | Relation（`Supersedes` の synced backref、Notion が対応する場合） | 引き継ぎ後 workflow（新 wf-B） |
| `Cancel Reason` | Rich Text | Status=Canceled 時の理由（任意、引き継ぎ時は推奨） |

Notion が self-link の synced backref をサポートしない場合、`Superseded By` は片方向 relation として実装し、双方向の整合性は audit log で補完する。

#### 9.3.4 失敗ケース

* A が `handover_note` を残さずに canceled しただけの場合: B は通常の `hokusai start` で開始し、`Supersedes` は設定されない。audit log にも引き継ぎイベントは残らない。これは正規フローではないが運用上発生し得るため、ガード（B 側で `Supersedes` 設定漏れを警告）は v0.5.x 本実装時に検討する。
* `handover_note` が draft のまま B が新 workflow を起動した場合: `prime` は draft を渡さない（§8.5）。B または PM が active 化するまで Agent には伝わらない。

---

## 10. 同期・冪等性

### 10.1 同期方向

HOKUSAI と Notion の同期は双方向要素を持つ。

| 方向 | 内容 |
|---|---|
| HOKUSAI → Notion | Work Item / Review Issue / Gate / Memory draft の作成・更新 |
| Notion → HOKUSAI | 人間による status / approval / waiver / priority の反映 |

### 10.2 冪等キー

各レコードは HOKUSAI 内部 ID を持ち、Notion 側にも保存する。

例:

* `work_item_id`
* `review_issue_id`
* `gate_id`
* `memory_id`

HOKUSAI はこの ID により upsert し、同じ対象を重複登録しない。

### 10.3 outbox

Notion 更新失敗時は既存 Notion sync outbox パターンを使い、workflow 本体は必要に応じて継続する。

ただし、gate / blocker の同期失敗が workflow 進行判断に影響する場合は `waiting_for_human` に遷移できること。

---

## 11. 監査要件

以下は audit log として保存する。

* Work Item 作成・状態変更・依存関係変更
* Review Issue 作成・triage・fixed・verified・waived
* Claim / lease 作成・延長・解放・期限切れ
* Gate 作成・承認・却下・期限切れ
* Project Memory 作成・編集・承認・deprecated 化（`handover_note` 含む全 Type）
* Notion からの手動変更を HOKUSAI が取り込んだ事実
* Agent に渡した prime context の hash または version
* 引き継ぎイベント（§9.3）:
  - `workflow.canceled`（旧 wf を Canceled 化、Cancel Reason 含む）
  - `workflow.handover_started`（新 wf の `Supersedes` プロパティ設定）
  - `memory.handover_note.created`（draft 作成、起票者 A）
  - `memory.handover_note.activated`（active 化、承認者 B または PM）
  - `memory.handover_note.deprecated`（引き継ぎ完了後の deprecated 化）

監査ログには最低限以下を含める。

* workflow_id
* profile_name
* entity_type
* entity_id
* action
* actor
* before
* after
* reason
* created_at

---

## 12. 権限・安全性

### 12.1 人間承認が必要な操作

以下は Agent 単独で実行してはならない。

* Gate を `open` にする
* Review Issue を `waived` にする
* Project Memory を `active` にする
* critical / high severity の blocker を解除する
* lease を強制解除して別 Agent に再割当する

### 12.2 secret の扱い

Notion DB には secret 値を書かない。

Notion に保存してよいのは env var 名、接続先名、承認者、判断理由、公開可能な URL のみとする。

### 12.3 profile 境界

profile を跨ぐ relation は原則禁止する。

例外的に cross-profile relation が必要な場合は、明示的に `external_profile` として記録し、HOKUSAI は自動実行判断には使わない。

---

## 13. UI / View 要件

### 13.1 Notion 推奨ビュー

Work Items DB:

* Ready Work
* Blocked Work
* Waiting for Human
* By Agent
* By Workflow

Review Issues DB:

* Open Review Issues
* Waived Issues
* By PR
* Repeated Issues
* Security / Policy Issues

Gates DB:

* Pending Gates
* Blocked Gates
* Due Soon
* Approved This Week

Project Memory DB:

* Active Memory
* Draft Memory
* Deprecated Memory
* By Type

### 13.2 Operations Console

Operations Console では以下を表示する。

* Notion sync status
* pending outbox count
* expired lease count
* blocked workflow count
* gate pending count
* profile 別 workgraph health

復旧操作は Operations Console に集約する。

---

## 14. 設定要件

設定例:

```yaml
human_governance:
  enabled: true
  notion:
    work_items_db_id_env: HOKUSAI_NOTION_WORK_ITEMS_DB_ID
    review_issues_db_id_env: HOKUSAI_NOTION_REVIEW_ISSUES_DB_ID
    gates_db_id_env: HOKUSAI_NOTION_GATES_DB_ID
    project_memory_db_id_env: HOKUSAI_NOTION_PROJECT_MEMORY_DB_ID
  work_queue:
    block_on_unresolved_dependencies: true
  lease:
    default_minutes: 60
    max_renewals: 4
  gates:
    block_on_pending_required_gate: true
  project_memory:
    inject_into_agent_prompt: true
    max_items: 20
    require_human_approval: true
```

profile config では案件ごとに DB ID env var 名を分離できる。

---

## 15. 受け入れ基準

* Work Item が Notion に同期され、`todo / blocked / ready / in_progress / done` を人間が確認できる
* blocker がある Work Item を Agent が処理しない
* Copilot / human / HOKUSAI final review の指摘が Review Issues DB に構造化される
* Review Issue の duplicate / waived / resolved_by_pr を追跡できる
* Agent claim / lease により同一作業の重複着手が防止される
* lease 期限切れ作業を Notion / Operations Console で確認できる
* pending / blocked gate がある場合、workflow が先に進まない
* Human Approval / CI / Design / Security gate の判断理由が残る
* Project Memory は human approval 後にのみ Agent prompt へ注入される
* `hokusai prime <workflow_id>` 相当の context 出力で、Agent に必要な要約を渡せる
* すべての状態変更が audit log に残る
* profile を跨いで Work Item / Gate / Memory が混在しない

