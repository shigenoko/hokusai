# Issue #23 + #24 実装計画書: 引き継ぎ運用手順と handover_note 型の追加

## 1. 背景と目的

Notion 議論「複数エンジニアによる開発の課題」§D-3 / §D-4 由来の **docs 拡張**。

A → B の引き継ぎが発生したときに:

- どういう Notion / CLI 操作で旧 workflow を canceled にして新 workflow を起ち上げるか（**Issue #23 / §D-3**）
- 引き継ぎ時の「ここまでで分かったこと」「気をつけるべき罠」を Project Memory に構造化して残すための型（**Issue #24 / §D-4**）

の 2 点を `docs/hokusai-human-governance-workgraph-requirements.md` に追記する。

両者は handover_note 概念で相互参照するため **同一 PR でセット実装** する。

## 2. 変更ファイル

- `docs/hokusai-human-governance-workgraph-requirements.md`: 要件定義書本体
- `docs/hokusai-issue-23-24-handover-implementation-plan.md`: 本実装計画書（新規）

コード変更なし、テスト追加なし、version bump なし（docs のみ）。

## 3. 要件定義書の編集内容

### 3.1 §8.2 Memory 種別表に `handover_note` を追加（Issue #24）

| Type | 例 |
|---|---|
| ...既存行... | ... |
| `handover_note` | A が Phase 5 で詰まった理由 / B 用に整理した試したこと一覧 / 未確認の前提 |

### 3.2 §8.6（新規）`handover_note` 型の使い分け基準（Issue #24）

`handover_note` が他の型（`project_rule` / `architecture_decision` / `operations_note` 等）と
どう違うかを表で整理する:

- 寿命（一過性 vs 恒久）
- 想定読者（次のオペレータ vs 案件横断）
- 承認フロー（B が active 化 vs PM が active 化）
- prompt 注入のスコープ（同一 workflow chain vs profile 全体）

### 3.3 §8.4 / §8.5 への補足追記（Issue #24）

- §8.4 Agent への注入: 既存の「active Project Memory summary」に `handover_note` が含まれる旨を明記
- §8.5 制約: 「A が draft で残し、B または PM が active 化」のフローを明記

### 3.4 §9.3（新規）引き継ぎ運用フロー（Issue #23）

ステップ詳細（テキストフロー図つき）:

```
[Engineer A]
   ↓ Phase X で停止 / 別タスクへ移る判断
1. 旧 workflow を canceled にする
   - hokusai status <wf-A>  で現状確認
   - Notion で Status=Canceled、Cancel Reason 記入
2. handover_note を Project Memory に draft で作成（§8.6 参照）
   - Type=handover_note, Status=draft
   - 経緯・試行・未確認前提を Content / Summary に残す
   - Related Workflow=旧 workflow

[Engineer B]
3. handover_note を Status=active にする（B 自身または PM 承認）
4. hokusai --profile X start <task_url> で新規 workflow を起動
   - Workflows DB の Supersedes プロパティを旧 wf-A に設定
5. hokusai prime <wf-B> 実行時、Workflows DB の Supersedes リレーションを
   辿って旧 wf-A に紐づく active な handover_note が自動で prompt に要約注入
   される（要件定義書 §8.4 参照、追加 CLI フラグは不要）
6. 新規 workflow が起動後、handover_note は `applied_at` に記録

[両者で監査]
7. audit log に supersedes / handover の双方向リンクが残る
```

### 3.5 §3.1 / §4.6（Workflows DB schema）への `Supersedes` プロパティ追加（Issue #23）

Workflows DB 既存スキーマに以下を追加:

| Property | Type | 説明 |
|---|---|---|
| Supersedes | Relation（self-link） | 引き継ぎ元 workflow（旧 wf-A） |
| Superseded By | Relation（self-link、Supersedes の synced） | 引き継ぎ後 workflow（新 wf-B） |

self-link は Notion の relation で同一 DB を指す形（synced backref 不可の場合は片方向 + audit ログで補完）。

### 3.6 §11 監査要件への追記（Issue #23）

audit log に以下を必ず残す:

- `workflow.canceled`（旧 wf）
- `workflow.handover_started`（旧 → 新の supersedes link 設定）
- `memory.handover_note.created`（draft 作成）
- `memory.handover_note.activated`（B 承認）

## 4. 受入条件

### Issue #23

- [ ] §9.3 セクションが要件定義書に追加される
- [ ] 引き継ぎフロー図（テキストでもよい）を含む
- [ ] 関連プロパティ（`supersedes` relation）の schema 変更も明記
- [ ] 既存セクションとの整合（§9.1 Phase 別の利用 / §11 audit 要件）が取れている

### Issue #24

- [ ] §8.2 の Memory 種別表に `handover_note` が追加される
- [ ] Project Memory DB schema の `Type` Select 選択肢に `handover_note` が加わる（§8.3 で言及）
- [ ] `hokusai prime` で active な `handover_note` が prompt に要約注入される旨を §8.4 で明記
- [ ] 既存の `project_rule` / `architecture_decision` 等との使い分け基準が §8.6 に明記される

## 5. 関連

- Issue: #23, #24
- 親 Notion 議論: https://www.notion.so/35f85495565d80b1b15aefee4fe44c18 §D-3 / §D-4
- 要件定義書本体: `docs/hokusai-human-governance-workgraph-requirements.md`
- 関連 PR #21（Operator プロパティ）/ #22（profile template、merged）

## 6. 注記

本 PR は **docs のみ**。実装（Project Memory DB の Type Select 拡張 / Workflows DB schema migration / `hokusai prime` の handover_note 抽出）は v0.5.x の Human Governance Workgraph 本実装で行う。
