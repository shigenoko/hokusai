# HOKUSAI Notion メインダッシュボード化 統合実装計画書

**作成日**: 2026-05-05
**対象読者**: プロジェクト責任者・テックリード・実装担当エンジニア
**位置付け**: 本ドキュメントは、以下 2 つの設計案を統合した実装計画書である。本書を実装の唯一の真実とする。

- ベース設計: `docs/codex-notion-main-dashboard-operation-console-design.md`（責務分離の哲学・安全性判断）
- 補完: `docs/claude-notion-main-dashboard-design.md`（実装ステップ・工数・DoD・Open Questions）

---

## 1. 目的

HOKUSAI を Notion 中心のプロダクト開発運用に合わせて再設計する。Notion をビジネスサイドとエンジニアサイドが共通で参照するメインダッシュボードとし、既存の HOKUSAI Web Dashboard は開発者向けのオペレーションコンソールとして再定義する。

### 達成したいこと

- Notion を主要コンテンツエリアとして活用する
- HOKUSAI の進行状況を PM、営業、マーケ、QA、エンジニアが同じ場所で確認できる
- 調査、設計、作業計画、意思決定ログを Notion に集約する
- 実行・再開・復旧・ログ確認・設定確認などの開発者向け操作は HOKUSAI Web Dashboard に残す
- Notion と Web Dashboard の責務を明確にし、二重管理を避ける

## 2. ゴールと非ゴール

### 2.1. ゴール

- Notion メインダッシュボードでビジネスサイドが日常的に進捗を把握できる
- ワークフロー状態・PR 状態・接続状態が Notion 上で閲覧できる
- HOKUSAI Web Dashboard は管理者向けの設定・診断・障害対応に特化する
- LangGraph と SQLiteStore は現状維持（実行エンジンの正本）
- 既存の `save_to_subpage_or_create()` による Phase 2/3/4 の子ページ保存ロジックを正本として尊重する

### 2.2. 非ゴール

- LangGraph を Notion で置き換える
- Notion を workflow 実行エンジンにする
- Notion からの操作トリガーで `start` / `continue` を実行する（誤操作・同期ズレリスク回避）
- Notion で設定 YAML を直接編集する
- HOKUSAI Web Dashboard を即時廃止する
- シークレット管理を Notion に移す

## 3. 結論サマリ

HOKUSAI の画面系機能を以下の 2 層に分離する。

| 層 | 位置付け | 主な利用者 | 主な用途 |
|---|---|---|---|
| **Notion メインダッシュボード** | プロダクト開発の共有ビュー | PM、営業、マーケ、QA、テックリード、エンジニア | 進捗確認、要件、設計、判断ログ、関連 GitLab 情報の共有 |
| **HOKUSAI Web Dashboard**（Operations Console に再定義） | 開発者向けオペレーションコンソール | エンジニア、テックリード、運用担当 | start / continue / retry / cleanup、ログ、接続状態、設定、復旧 |

| 場所 | キーワード |
|---|---|
| Notion | **見る、判断する、共有する** |
| HOKUSAI Web Dashboard | **動かす、直す、調べる** |

## 4. 基本方針

### 4.1. Notion はメインダッシュボード

Notion には HOKUSAI のワークフロー状況と成果物を集約する。

**Notion に置くもの**:
- ワークフロー一覧、タスク状態、現在 Phase、Waiting for Human の理由、Next Action
- Phase 2 調査レポート、Phase 3 設計書、Phase 4 作業計画、意思決定ログ
- GitLab Issue / MR リンク、Slack 通知先や運用メモ
- ビジネスサイド向けの進捗共有

**Notion に置かないもの**:
- LangGraph checkpoint の詳細
- ローカル worktree の詳細
- 長大なログ
- CLI 認証トークンやシークレット
- 設定 YAML の直接編集 UI
- start / continue / retry / cleanup の実行ボタン

### 4.2. HOKUSAI Web Dashboard はオペレーションコンソール

既存の HOKUSAI Web Dashboard は、開発者向けの操作・復旧・診断画面として残す。

**Web Dashboard に残すもの**:
- workflow start / continue / retry phase / cleanup / delete workflow
- PR review action、cross-review の再実行
- Notion 書き込み失敗の retry
- workflow 詳細状態、ログ確認
- 接続状態表示、再チェック実行
- 設定 YAML の確認・編集・差分・バックアップ復元
- ローカル環境や CLI 認証状態の確認

**Web Dashboard から外す（または優先度を下げる）もの**:
- PM / ビジネス向けの一覧ビュー
- 長文成果物の閲覧（Phase 2/3/4 の調査・設計・作業計画）
- ステークホルダー向け進捗共有

### 4.3. 実行エンジンは現状維持

- LangGraph: ワークフロー実行エンジン（外さない）
- SQLiteStore: 内部状態の正本（外さない）
- Notion DB: 同期されたビュー（正本ではない）
- 不整合時は SQLite を信じる
- Dashboard 同期は HOKUSAI 専用 Notion Integration + Notion API で実装する
- 既存の Notion MCP は Phase 2/3/4 子ページ保存の経路として維持し、Dashboard DB 同期の認証・書き込み経路とは分ける

## 5. 全体アーキテクチャ

```text
┌─────────────────────────────────────────────────┐
│ レイヤー A: 全社員向けメインダッシュボード             │
├─────────────────────────────────────────────────┤
│ Notion                                           │
│   ├─ HOKUSAI Workflows DB                       │
│   ├─ HOKUSAI Pull Requests DB                   │
│   ├─ HOKUSAI Service Status ページ              │
│   ├─ Task / Feature ページ                       │
│   ├─ Phase 2 / 3 / 4 子ページ                    │
│   ├─ GitLab Issue / MR リンク                    │
│   └─ 人間判断・意思決定ログ                        │
└─────────────────────────────────────────────────┘
                ↑ 同期書き込み（best effort）
┌─────────────────────────────────────────────────┐
│ レイヤー B: 開発者向けオペレーションコンソール          │
├─────────────────────────────────────────────────┤
│ HOKUSAI Web Dashboard（縮退・特化）                │
│   ├─ workflow start / continue / retry / cleanup │
│   ├─ logs / workflow detail                      │
│   ├─ connection status / 再チェック               │
│   ├─ config settings / backup / 復元              │
│   ├─ Notion 同期状態確認・再送                     │
│   └─ cross-review actions                        │
└─────────────────────────────────────────────────┘
                ↑ 内部状態の正本
┌─────────────────────────────────────────────────┐
│ レイヤー C: 実行エンジン                            │
├─────────────────────────────────────────────────┤
│ HOKUSAI Runtime                                  │
│   ├─ LangGraph workflow                          │
│   ├─ SQLite workflow DB                          │
│   ├─ LangGraph checkpoint DB                     │
│   └─ Claude Code / Codex / GitLab / Notion 連携   │
└─────────────────────────────────────────────────┘
```

## 6. Notion 側のデータ設計

### 6.1. Workspace 構造

```
HOKUSAI Workspace
├─ 📊 HOKUSAI Workflows DB
│   ├─ Active ビュー
│   ├─ Waiting for Human ビュー
│   ├─ Failed ビュー
│   ├─ Ready to Start ビュー
│   ├─ Done ビュー
│   ├─ By Business Owner
│   ├─ By Tech Lead
│   └─ By GitLab Project
│
├─ 🔀 HOKUSAI Pull Requests DB
│   ├─ Open PRs
│   ├─ Recent Merged
│   └─ By Workflow
│
├─ 🔌 HOKUSAI Service Status ページ
│   └─ gh / glab / notion_mcp / codex / claude の最新状態
│
└─ 🛠️ HOKUSAI Operations ページ（管理者向けポータル）
    ├─ HOKUSAI Web Dashboard へのリンク
    ├─ 同期状態（最終同期時刻・失敗件数）
    └─ 緊急時の操作手順
```

### 6.2. HOKUSAI Workflows DB スキーマ

| プロパティ | 型 | 内容 | 更新主体 |
|---|---|---|---|
| Name | title | タスク名または施策名 | HOKUSAI |
| Workflow ID | text | `wf-...` | HOKUSAI |
| Status | select | `Ready` / `Running` / `Waiting for Human` / `Failed` / `Done` / `Canceled` | HOKUSAI |
| Current Phase | number | 現在の Phase | HOKUSAI |
| Current Phase Name | text | Phase 名（例: Phase 5: 実装） | HOKUSAI |
| Waiting Reason | select | `branch_hygiene` / `cross_review_blocked` / `review_wait` 等 | HOKUSAI |
| Next Action | rich text | 人間が次に取るべき行動 | HOKUSAI |
| Assignee | person | 担当者 | 人間 |
| **Business Owner** | person | ビジネス側責任者 | 人間 |
| **Tech Lead** | person | 技術側責任者 | 人間 |
| Priority | select | 優先度 | 人間 |
| GitLab Epic | url | 関連 Epic | 人間 |
| GitLab Issue | url | 関連 Issue | 人間 |
| GitLab MR | url | 関連 MR | HOKUSAI |
| Research Page | relation/url | Phase 2 子ページ | HOKUSAI |
| Design Page | relation/url | Phase 3 子ページ | HOKUSAI |
| Plan Page | relation/url | Phase 4 子ページ | HOKUSAI |
| Last Updated | date | HOKUSAI 最終更新日時 | HOKUSAI |
| Started At | date | 開始日時 | HOKUSAI |
| Completed At | date | 完了日時 | HOKUSAI |
| Error Summary | rich text | 失敗時の要約 | HOKUSAI |
| Last Sync | date | Notion 同期成功時刻 | HOKUSAI |
| Sync Errors | text | 同期失敗時のエラー | HOKUSAI |

### 6.3. HOKUSAI Pull Requests DB スキーマ

| プロパティ | 型 | 内容 | 更新主体 |
|---|---|---|---|
| PR Number | title | PR 番号 | HOKUSAI |
| URL | url | PR URL | HOKUSAI |
| Repository | select | Backend / Frontend 等 | HOKUSAI |
| Status | select | Draft / Open / Approved / Merged / Closed | HOKUSAI |
| Workflow | relation | Workflows DB へのリレーション | HOKUSAI |
| Reviewer | multi-select | レビュアー | HOKUSAI |
| Created At | date | 作成日時 | HOKUSAI |
| Last Updated | date | 最終更新 | HOKUSAI |

### 6.4. Notion タスクページ構成（推奨テンプレート）

```text
タスクページ
  ├─ 背景
  ├─ 目的 / KPI
  ├─ 要件
  ├─ 仕様
  ├─ HOKUSAI 実行状態
  │   ├─ Workflow ID
  │   ├─ Status
  │   ├─ Current Phase
  │   └─ Next Action
  ├─ 関連 GitLab
  │   ├─ Epic
  │   ├─ Issue
  │   └─ MR
  ├─ Phase 2: 調査レポート（HOKUSAI 自動生成・子ページ）
  ├─ Phase 3: 設計書（HOKUSAI 自動生成・子ページ）
  ├─ Phase 4: 作業計画（HOKUSAI 自動生成・子ページ）
  └─ 意思決定ログ
```

Phase 2 / 3 / 4 は既存の `save_to_subpage_or_create()` による子ページ保存を正本とする。

## 7. 状態同期設計

### 7.1. HOKUSAI から Notion へ書き込むタイミング

| タイミング | 書き込み先 | 同期内容 |
|---|---|---|
| `WorkflowRunner.start()` 直後 | Workflows DB（新規作成） | Workflow ID、Status=`Running`、Started At、Current Phase |
| 各 Phase 遷移時 | Workflows DB（更新） | Current Phase、Current Phase Name、Last Updated |
| Phase 2 完了時 | Workflows DB（更新） | Research Page |
| Phase 3 完了時 | Workflows DB（更新） | Design Page |
| Phase 4 完了時 | Workflows DB（更新） | Plan Page |
| Phase 8a の PR 作成時 | Pull Requests DB（新規作成）+ Workflows DB（リレーション・URL 更新） | PR 情報、GitLab MR URL |
| Waiting for Human 到達時 | Workflows DB（更新） | Status=`Waiting for Human`、Waiting Reason、Next Action |
| 失敗時 | Workflows DB（更新） | Status=`Failed`、Error Summary、Next Action |
| 完了時 | Workflows DB（更新） | Status=`Done`、Completed At、最終サマリ |
| 接続状態チェック実行時 | Service Status ページ | 各サービスの状態 |

### 7.2. Notion から HOKUSAI が読む情報（最小限）

**読む情報**:
- タスク URL、タスク本文、要件・仕様
- 人間が記録した判断メモ
- 関連 GitLab Issue / MR URL

**読まない情報**:
- Notion の表示用ステータスだけを根拠にした内部再開判断
- LangGraph checkpoint の代替情報
- シークレット

### 7.3. データ正本原則

- **HOKUSAI SQLiteStore = 内部状態の正本**
- **Notion DB = 同期されたビュー**
- 不整合時は SQLite を信じる
- 復旧時は SQLite から Notion を再構築

### 7.4. 同期失敗時の挙動

- Notion 書き込み失敗はワークフロー本体を止めない（**best effort**）
- 失敗した同期イベントは SQLite 側の `notion_sync_outbox` / `notion_sync_errors` に保存する
- Notion の `Sync Errors` プロパティは、次回同期成功時に反映される表示用サマリとし、復旧の正本にはしない
- HOKUSAI Web Dashboard の「同期再送」ボタンは SQLite の outbox / error queue を再送する
- レートリミット対策として **バッチ更新 + 5 秒デバウンス**

### 7.5. 同期イベント設計

Notion 同期は `save_workflow` の全保存処理に直接フックしない。`save_workflow` は頻繁に呼ばれるため、そこを同期起点にすると過剰書き込み、重複更新、レートリミット超過が起きやすい。

同期イベントは以下のような意味のある境界で発行する。

| イベント | 発行タイミング | 主な同期先 |
|---|---|---|
| `workflow_started` | workflow 開始直後 | Workflows DB |
| `phase_changed` | Current Phase が変わった時 | Workflows DB |
| `phase_artifact_linked` | Phase 2/3/4 子ページ URL が確定した時 | Workflows DB |
| `pr_created` | Phase 8a で PR / MR が作成された時 | Pull Requests DB / Workflows DB |
| `terminal_status_changed` | Waiting / Failed / Done / Canceled 到達時 | Workflows DB |
| `service_status_checked` | 接続状態チェック実行時 | Service Status ページ |

各イベントには `workflow_id:event_type:phase:revision` などの冪等キーを付与し、同じイベントを再送しても Notion 側の重複レコードが増えないようにする。

## 8. Human-in-the-loop 設計

### 8.1. Notion に表示する内容

人間判断待ちは Notion に明示する。

- `Status = Waiting for Human`
- `Waiting Reason`（branch_hygiene / cross_review_blocked / review_wait 等）
- `Next Action`（人間が取るべき具体的な行動）
- 判断対象の Phase
- 関連する調査 / 設計 / 作業計画ページ
- 対象 GitLab MR
- `hokusai continue <workflow-id>` の案内

### 8.2. 重要な設計判断: Notion から自動再開しない

**Notion 上のプロパティ変更だけで自動 continue する設計にはしない。** 再開操作は CLI または HOKUSAI Web Dashboard から行う。

理由:
- 誤操作による再開を避ける
- Notion API / MCP の遅延や同期ズレに影響されないようにする
- LangGraph checkpoint と SQLite state の整合性を HOKUSAI 側で保つ

将来的に Notion からの操作トリガー機能が必要になった場合は、別 PR で慎重に検討する。

## 9. HOKUSAI Web Dashboard の再定義

### 9.1. 新しい位置付け

HOKUSAI Web Dashboard は、PM 向けダッシュボードではなく、**開発者向けオペレーションコンソール**とする。

推奨名称（実装時に決定）:
- HOKUSAI Operations Console
- HOKUSAI Developer Console
- HOKUSAI Runtime Console

### 9.2. 機能の再配置

#### 残す機能

| 機能 | 理由 |
|---|---|
| workflow start / continue | 実行操作は HOKUSAI Runtime 側で行う必要がある |
| retry phase | 状態補正や checkpoint 整合性が必要 |
| cleanup / delete | ローカル DB / worktree / checkpoint に関わる |
| workflow detail | デバッグに必要 |
| logs | Notion に長大ログを置かない |
| connection status / 再チェック | CLI / MCP / GitLab / Codex / Claude の実行環境診断 |
| config settings | YAML、接続状態、バックアップ、差分確認 |
| cross-review actions | HOKUSAI 内部状態との整合が必要 |

#### 追加する機能

| 機能 | 用途 |
|---|---|
| **Notion 同期状態パネル** | Notion DB への書き込み成功/失敗・最終同期時刻・失敗件数を表示 |
| **同期再送ボタン** | 失敗した同期を手動で再送 |
| **Notion 接続テスト** | Workflows DB / Pull Requests DB へのアクセス権限を検証 |

#### Notion に移す機能

| 機能 | 移行先 |
|---|---|
| ワークフロー一覧 | HOKUSAI Workflows DB |
| PM 向け進捗確認 | Notion view |
| Phase 2 / 3 / 4 成果物閲覧 | Notion 子ページ |
| Waiting for Human 一覧 | Notion view |
| GitLab Issue / MR 関連一覧 | Notion DB / relation / URL |
| ステークホルダー向け共有 | Notion |

## 10. 実装ステップ

各 Phase は独立してリリース可能。Phase A だけでも閲覧価値が出る。

### Phase A: Notion 表示モデルと同期基盤（2〜3 週間）

**作業内容**:
- A-1: HOKUSAI Workflows DB のスキーマ定義（Notion 上で実 DB を作成）
- A-2: Notion タスクページテンプレート定義
- A-3: `hokusai/integrations/notion_dashboard.py` 新規実装
  - HOKUSAI 専用 Notion Integration の API token を使った直接 API クライアント
  - Workflows DB 書き込み・更新クライアント
  - Pull Requests DB 書き込みクライアント
  - レート制限対応・リトライ・デバウンス
- A-4: SQLite の `notion_sync_outbox` / `notion_sync_errors` スキーマ追加
- A-5: `WorkflowConfig.notion_dashboard` 設定追加
- A-6: 既存 `phase_subpages` と Notion DB プロパティの対応マッピング

**完了条件（DoD）**:
- [ ] HOKUSAI Workflows DB / Pull Requests DB が Notion 上に作成されている
- [ ] HOKUSAI 専用 Notion Integration が作成され、対象 DB / ページだけに権限付与されている
- [ ] `notion_dashboard.py` のクライアントが単体テスト含めて動作する
- [ ] Notion API token は環境変数から読み込まれ、YAML や Notion 本文には保存されない
- [ ] `notion_sync_outbox` / `notion_sync_errors` に失敗イベントを保存できる
- [ ] レートリミット超過時もリトライで吸収される
- [ ] `WorkflowConfig.notion_dashboard` の設定が読み込める
- [ ] Notion タスクページテンプレートと既存子ページ保存ロジックの対応が文書化されている

### Phase B: HOKUSAI から Notion への同期実装（3 週間）

**作業内容**:
- B-1: `WorkflowRunner.start()` 直後に Workflows DB へ新規レコード作成
- B-2: `phase_changed` / `phase_artifact_linked` / `terminal_status_changed` などの明示的な同期イベントを発行
- B-3: Phase 2/3/4 の子ページリンクを Notion DB に書き戻し
- B-4: Phase 8a の PR 作成時に Pull Requests DB へ反映
  - 初期スコープは PR URL、番号、作成日時、初期 Status の同期まで
  - Approved / Merged / Closed の追跡は GitLab polling または webhook を実装する場合のみ含める
- B-5: Waiting / Failed / Done の terminal status 同期
- B-6: 同期イベントの冪等キー、重複抑止、再送処理
- B-7: 単体テスト + 結合テスト

**完了条件（DoD）**:
- [ ] HOKUSAI 起動時に Workflows DB に新規レコードが作成される
- [ ] 各 Phase 完了時に Notion DB が更新される
- [ ] PR 作成時に Pull Requests DB にレコードが作成される
- [ ] PR の Approved / Merged / Closed を初期スコープに含めるか、後続スコープに明示的に切り出している
- [ ] Waiting for Human 到達時に Status と Next Action が反映される
- [ ] Notion 書き込み失敗でワークフロー本体が止まらない
- [ ] Notion 書き込み失敗時に SQLite outbox / error queue へ保存される
- [ ] 同じ同期イベントを再送しても Notion レコードが重複しない
- [ ] Notion 上の Active / Waiting / Failed / Done ビューが運用できる
- [ ] CLI / Web Dashboard どちらから実行しても Notion の状態が更新される

### Phase C: Service Status の Notion 反映（1 週間）

**作業内容**:
- C-1: `connection_status` の結果を Notion Service Status ページに書き出すスクリプト
- C-2: 定期実行（cron / launchd）の設定
- C-3: HOKUSAI Web Dashboard から手動実行ボタン

**完了条件（DoD）**:
- [ ] Service Status ページに最新の接続状態が反映される
- [ ] 定期実行が動作する
- [ ] HOKUSAI Web Dashboard から手動更新できる

### Phase D: HOKUSAI Web Dashboard の再定義（2 週間）

**作業内容**:
- D-0: Operations Console のアクセス制限方針を確定し、初期リリース前に適用
- D-1: ナビゲーションの再構成（Operations Console としての位置付け明示）
- D-2: ワークフロー一覧の縮退（Notion へのリンク化）
- D-3: workflow detail に対応 Notion ページリンクを表示
- D-4: Notion 同期状態パネルの追加
- D-5: 同期再送ボタンの実装
- D-6: 設定・接続状態・ログ・復旧操作を中心に再配置

**完了条件（DoD）**:
- [ ] Operations Console は管理者・開発者だけがアクセスできる状態になっている
- [ ] Web Dashboard の用途が「共有ビュー」ではなく「操作・復旧コンソール」として明確になっている
- [ ] Notion メインダッシュボードへの導線がトップに表示される
- [ ] ワークフロー一覧画面に Notion へのリンクが明示されている
- [ ] Notion 同期状態パネルが追加されている
- [ ] 同期再送ボタンが動作する
- [ ] 既存の設定編集機能はそのまま使える

### Phase E: Human-in-the-loop 運用改善 + パイロット運用（2 週間）

**作業内容**:
- E-1: Waiting for Human 時の Notion 表示文言の標準化
- E-2: `Next Action` テンプレート整備
- E-3: Slack 通知メッセージに Notion ページへのディープリンクを含める
- E-4: 運用ガイド作成
- E-5: 1 チームでパイロット運用
- E-6: フィードバックを反映

**完了条件（DoD）**:
- [ ] 人間判断待ちが Notion と Slack で把握できる
- [ ] 再開操作は CLI / Operations Console で安全に行える
- [ ] 運用ガイドが作成されている
- [ ] パイロット運用が完了し、ビジネスサイドからのフィードバックが反映されている

### 工数まとめ

**合計: 10〜12 週間（実働 2.5〜3 ヶ月）**

| Phase | 作業 | 工数 |
|---|---|---|
| A | Notion 表示モデルと同期基盤 | 2〜3 週間 |
| B | HOKUSAI → Notion 同期実装 | 3 週間 |
| C | Service Status の Notion 反映 | 1 週間 |
| D | Web Dashboard の再定義 | 2 週間 |
| E | HITL 運用改善 + パイロット | 2 週間 |

優先度を下げる場合、Phase A + B だけでも閲覧価値が出る。ただし、専用 Notion Integration、SQLite outbox、冪等な同期イベントまで含めると A + B は 5〜6 週間を見込む。

PR の Approved / Merged / Closed 追跡を初期スコープ外にし、PR 作成時の初期同期だけに限定する場合は、全体を 9 週間前後まで圧縮できる可能性がある。

## 11. 設定例

### 11.1. WorkflowConfig 拡張

```yaml
notion_dashboard:
  enabled: true
  api_token_env: HOKUSAI_NOTION_API_TOKEN
  workflows_db_id_env: HOKUSAI_NOTION_WORKFLOWS_DB_ID
  pull_requests_db_id_env: HOKUSAI_NOTION_PR_DB_ID
  service_status_page_id_env: HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID
  sync_outbox:
    enabled: true
    max_retry_attempts: 10
  # 同期失敗時のリトライ設定
  retry:
    max_attempts: 3
    backoff_seconds: 5
  # レートリミット対策
  rate_limit:
    requests_per_second: 2
    debounce_ms: 5000
```

### 11.2. 環境変数

```bash
# Notion Dashboard
export HOKUSAI_NOTION_API_TOKEN="..."
export HOKUSAI_NOTION_WORKFLOWS_DB_ID="..."
export HOKUSAI_NOTION_PR_DB_ID="..."
export HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID="..."
```

Dashboard DB 同期は、既存の Notion MCP 認証を再利用しない。HOKUSAI 専用 Notion Integration を作成し、API token を環境変数で渡す。既存の Notion MCP 経路は Phase 2/3/4 子ページ保存の互換性維持に限定する。

## 12. パフォーマンス想定

| 項目 | 想定値 | 備考 |
|---|---|---|
| Notion 書き込みレイテンシ | 1〜3 秒 | API 呼び出し |
| 1 ワークフローあたりの書き込み回数 | 10〜30 回 | 各 Phase + PR + 完了 |
| Notion API レートリミット | 3 req/sec | バッチ + デバウンスで吸収 |
| 並行ワークフロー上限（同期込み） | 5〜10 件 | レートリミットを考慮 |

## 13. セキュリティ方針

- Notion にシークレットを保存しない（API token、CLI 認証情報、Webhook URL 等）
- HOKUSAI 専用 Notion Integration の API token は環境変数でのみ扱い、設定 YAML、Notion、ログには出さない
- Web Dashboard でもシークレット入力 UI は初期スコープ外とする
- Notion には共有に適した情報だけを書く
- ログやエラー詳細は要約だけ Notion に出し、詳細ログは Operations Console / ローカルファイルを参照する
- Notion のページ権限は GitLab メンバー、ビジネスサイド、外部共有範囲に合わせて設計する
- HOKUSAI 用 Notion インテグレーションを作成し、対象 DB / ページのみに権限付与（個人ユーザーアカウントに依存しない）

## 14. 障害時の挙動

| 障害シナリオ | 挙動 | 復旧方法 |
|---|---|---|
| Notion API ダウン | 書き込み失敗を SQLite outbox / error queue に蓄積、ワークフロー本体は継続 | Notion 復旧後、Web Dashboard の「同期再送」ボタン |
| Notion DB が削除された | 書き込みが 404 で失敗、warn ログ | DB を再作成し、ID を環境変数に再設定 |
| Notion 認証切れ | 401 で書き込み失敗 | インテグレーションを再認証 |
| HOKUSAI 側ダウン | Notion への書き込みは止まるが、SQLite が正本のため復旧後に再同期可能 | HOKUSAI 再起動 + 「同期再送」 |

**設計原則: Notion 障害が HOKUSAI のワークフロー実行を止めないこと。**

## 15. リスクと対策

| リスク | 対策 |
|---|---|
| Notion と SQLite state が不一致になる | SQLite / LangGraph checkpoint を実行正本とし、Notion は表示・共有用にする |
| Notion 更新失敗で workflow が止まる | Notion 同期は best effort とし、失敗時は SQLite outbox / error queue に保存して retry Notion action で復旧 |
| Notion ページが複雑化する | DB プロパティはサマリに限定し、詳細は子ページに分ける |
| GitLab Epic / Issue と Notion の二重管理 | GitLab には実行情報と Notion リンクのみ置き、要件・設計は Notion に寄せる |
| Notion から誤って再開操作される | Notion は操作トリガーにせず、再開は CLI / Operations Console に限定する |
| ビジネスサイドに開発内部情報が見えすぎる | Notion には要約を出し、詳細ログや checkpoint は Operations Console に残す |
| Notion API レートリミット超過 | バッチ更新 + 5 秒デバウンス + リトライで吸収 |

## 16. 運用ルール

### Notion に記録する
- 要件、仕様、調査、設計、作業計画
- 判断ログ、現在状態、次アクション
- 関連 GitLab リンク

### GitLab に記録する
- Issue、MR、コード差分、レビューコメント、CI 結果、マージ判断

### Operations Console（HOKUSAI Web Dashboard）に残す
- 実行操作、再開操作、復旧操作
- 設定確認、接続状態、ログ
- checkpoint / SQLite state に関わる詳細

## 17. Open Questions（着手前に合意すべき事項）

実装着手前に明示的に方針を確定させる項目。各暫定案で進めて差し支えなければ、レビュアからの no-objection をもって着手する。

1. **Notion インテグレーションの作成主体**
   - 暫定案: 組織管理者が作成し、HOKUSAI 用専用権限を付与

2. **Workflows DB の保持期間**
   - 完了済みワークフローを永続的に Notion に残すか、一定期間でアーカイブするか
   - 暫定案: 初期リリースでは別 DB への移動は行わず、Done / Archived ビューで整理する。6 ヶ月運用後に件数と検索性を見て別 DB アーカイブを判断する

3. **Notion ライセンス**
   - ビジネスサイド全員に閲覧権限が必要 → ライセンス費用の試算
   - 暫定案: Notion Plus プラン（チーム単位課金）

4. **既存ワークフローのマイグレーション**
   - 既に進行中のワークフローを Notion DB に流し込むか
   - 暫定案: 流し込まない（新規ワークフローから適用）

5. **HOKUSAI Web Dashboard のアクセス制限**
   - 全員が触れる現状から、管理者のみに制限すべきか
   - 暫定案: Phase D の完了条件として、BASIC 認証、VPN、社内 SSO のいずれかで管理者・開発者のみに制限する。初期リリース後の別フェーズには回さない

6. **Web Dashboard の名称変更**
   - HOKUSAI Operations Console / Developer Console / Runtime Console のいずれか
   - 暫定案: HOKUSAI Operations Console

7. **Notion からの操作トリガー機能（将来検討）**
   - 本計画では非ゴール。将来必要性が出た場合は別 PR で慎重に検討
   - 暫定案: 初期スコープ外、将来拡張として残す

## 18. 受け入れ基準（Definition of Done）統合まとめ

### 全体 DoD

- [ ] ビジネスサイド（営業・マーケ）が Notion だけでワークフロー進捗を把握できる
- [ ] エンジニアの実行操作（start / continue / retry / cleanup）はすべて Operations Console / CLI から行える
- [ ] LangGraph と SQLiteStore は変更なく動作している
- [ ] Dashboard DB 同期は HOKUSAI 専用 Notion Integration + API token で実装されている
- [ ] 既存の Notion MCP による Phase 2/3/4 子ページ保存は互換性を維持している
- [ ] Notion 障害が HOKUSAI のワークフロー実行を止めない
- [ ] Notion 同期失敗は SQLite outbox / error queue に保存され、Operations Console から再送できる
- [ ] Operations Console は管理者・開発者だけがアクセスできる
- [ ] 全 Phase の単体テスト・結合テストが追加されている
- [ ] 運用ガイドが作成され、パイロット運用のフィードバックが反映されている
- [ ] 既存の Notion 子ページ保存ロジックが正常動作している

### Phase 別 DoD

各 Phase の「完了条件」セクション参照（Section 10）。

## 19. 将来拡張

- Slack 通知メッセージ内に Notion DB へのディープリンクを含める（Phase E に含む）
- Notion AI を活用したワークフロー要約の自動生成
- 複数プロジェクト横断での Workflows DB 集約
- ロール別ビュー（PM 用 / エンジニア用 / 営業用）の整備
- Notion からの操作トリガー機能（Webhook bridge 経由）
   - 必要性が確認され、誤操作リスクが許容できると判断された場合のみ
- 監査ログのダッシュボード化

## 20. 関連ドキュメント

| ドキュメント | 関係 |
|---|---|
| `docs/codex-notion-main-dashboard-operation-console-design.md` | 本書のベース設計（責務分離・安全性判断） |
| `docs/claude-notion-main-dashboard-design.md` | 本書の補完（実装ステップ・工数・DoD） |
| `docs/codex-hokusai-notion-gitlab-operation-policy.md` | Notion / GitLab の役割分担方針（前提） |
| `docs/claude-hokusai-notion-gitlab-roles.md` | プロジェクト責任者向けの役割整理 |
| `docs/dashboard-connection-settings-proposal.md` | 既存 Dashboard の接続状態パネル提案 |
| `docs/codex-slack-notification-implementation-plan.md` | Slack 通知の実装計画（連携対象） |

## 21. まとめ

| 項目 | 内容 |
|---|---|
| Notion の役割 | 全社員向けメインダッシュボード（**見る・判断・共有**） |
| HOKUSAI Web Dashboard の役割 | 開発者向けオペレーションコンソール（**動かす・直す・調べる**） |
| LangGraph | 現状維持（実行エンジン） |
| SQLiteStore | 現状維持（内部状態の正本） |
| Notion DB | 同期されたビュー（正本ではない） |
| Notion からの操作トリガー | 初期スコープ外（誤操作・同期ズレリスク回避） |
| 工数 | 10〜12 週間（PR 状態追跡を初期スコープ外にする場合は 9 週間前後まで圧縮余地あり） |
| 受け入れ基準 | 5 つの Phase それぞれに DoD を設定 |

ビジネスとエンジニアの壁をなくす目的に対して、**Notion を「見る・判断・共有する」場、HOKUSAI Web Dashboard を「動かす・直す・調べる」場として明確に分離**するのが、最もリスクが低く効果が大きい設計である。

レビュアからの no-objection を得たうえで Phase A から着手することを推奨する。
