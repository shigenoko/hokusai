# Notion メインダッシュボード / HOKUSAI Web Dashboard 再定義 設計書

## 目的

HOKUSAI の利用体験を、Notion を中心としたプロダクト開発運用に合わせて再設計する。

本設計では、Notion をビジネスサイドとエンジニアサイドが共通で参照するメインダッシュボードとし、既存の HOKUSAI Web Dashboard は開発者向けのオペレーションコンソールとして再定義する。

目的は以下の通り。

- Notion を主要コンテンツエリアとして活用する
- HOKUSAI の進行状況を PM、営業、マーケ、QA、エンジニアが同じ場所で確認できるようにする
- 調査、設計、作業計画、意思決定ログを Notion に集約する
- 実行、再開、復旧、ログ確認、設定確認などの開発者向け操作は HOKUSAI Web Dashboard に残す
- Notion と Web Dashboard の責務を明確にし、二重管理を避ける

## 結論

HOKUSAI の画面系機能は、以下の 2 層に分ける。

| 層 | 位置付け | 主な利用者 | 主な用途 |
|---|---|---|---|
| Notion メインダッシュボード | プロダクト開発の共有ビュー | PM、営業、マーケ、QA、テックリード、エンジニア | 進捗確認、要件、設計、判断ログ、関連 GitLab 情報の共有 |
| HOKUSAI Web Dashboard | 開発者向けオペレーションコンソール | エンジニア、テックリード、運用担当 | start / continue / retry / cleanup、ログ、接続状態、設定、復旧 |

Notion は「見る、判断する、共有する」場所とする。
HOKUSAI Web Dashboard は「動かす、直す、調べる」場所とする。

## 基本方針

### Notion はメインダッシュボード

Notion には、HOKUSAI のワークフロー状況と成果物を集約する。

Notion に置くもの:

- ワークフロー一覧
- タスクの状態
- 現在 Phase
- Waiting for Human の理由
- 次に人間が取るべきアクション
- Phase 2 調査レポート
- Phase 3 設計書
- Phase 4 作業計画
- 意思決定ログ
- GitLab Issue / MR リンク
- Slack 通知先や運用メモ
- ビジネスサイド向けの進捗共有

Notion に置かないもの:

- LangGraph checkpoint の詳細
- ローカル worktree の詳細
- 長大なログ
- CLI 認証トークンやシークレット
- 設定 YAML の直接編集 UI
- start / continue / retry / cleanup の実行ボタン

### HOKUSAI Web Dashboard はオペレーションコンソール

既存の HOKUSAI Web Dashboard は、開発者向けの操作・復旧・診断画面として残す。

Web Dashboard に残すもの:

- workflow start / continue
- retry phase
- cleanup / delete workflow
- PR review action
- cross-review の再実行 / 修正適用
- Notion 書き込み失敗の retry
- workflow 詳細状態
- ログ確認
- 接続状態表示
- 設定 YAML の確認・編集・差分・バックアップ復元
- ローカル環境や CLI 認証状態の確認

Web Dashboard から外す、または優先度を下げるもの:

- PM / ビジネス向けの一覧ビュー
- 長文成果物の閲覧
- ステークホルダー向け進捗共有
- Notion 子ページで見れば十分な調査・設計・作業計画の表示

## 全体アーキテクチャ

```text
Notion
  - HOKUSAI Workflows DB
  - HOKUSAI Task / Feature ページ
  - Phase 2 / 3 / 4 子ページ
  - GitLab Issue / MR リンク
  - 人間判断・意思決定ログ

HOKUSAI Runtime
  - LangGraph workflow
  - SQLite workflow DB
  - LangGraph checkpoint DB
  - Claude Code / Codex / GitLab / Notion 連携

HOKUSAI Web Dashboard
  - 開発者向け操作画面
  - 設定・接続状態
  - 実行・再開・復旧
  - ログ・詳細状態

GitLab
  - Issue
  - MR
  - Review
  - CI
```

Notion は実行エンジンにはしない。
HOKUSAI Runtime は引き続き LangGraph でワークフローを実行し、その結果を Notion に同期する。

## Notion 側のデータ設計

### HOKUSAI Workflows DB

HOKUSAI の実行単位を一覧する Notion Database を用意する。

推奨プロパティ:

| プロパティ | 型 | 内容 |
|---|---|---|
| Name | title | タスク名または施策名 |
| Workflow ID | text | `wf-...` |
| Status | select | `Ready` / `Running` / `Waiting for Human` / `Failed` / `Done` / `Canceled` |
| Current Phase | number | 現在の Phase |
| Current Phase Name | text | Phase 名 |
| Waiting Reason | select/text | `branch_hygiene` / `cross_review_blocked` / `review_wait` など |
| Next Action | rich text | 人間が次に取るべき行動 |
| Assignee | person | 担当者 |
| Business Owner | person | ビジネス側責任者 |
| Tech Lead | person | 技術側責任者 |
| Priority | select | 優先度 |
| GitLab Epic | url | 関連 Epic |
| GitLab Issue | url | 関連 Issue |
| GitLab MR | url | 関連 MR |
| Research Page | relation/url | Phase 2 子ページ |
| Design Page | relation/url | Phase 3 子ページ |
| Plan Page | relation/url | Phase 4 子ページ |
| Last Updated | date | HOKUSAI 最終更新日時 |
| Started At | date | 開始日時 |
| Completed At | date | 完了日時 |
| Error Summary | rich text | 失敗時の要約 |

### 推奨ビュー

| ビュー | 用途 |
|---|---|
| Active | `Running` / `Waiting for Human` / `Failed` を表示 |
| Waiting for Human | 人間判断が必要なものだけ表示 |
| Failed | 失敗したワークフローの復旧対象 |
| Ready to Start | HOKUSAI 実行待ちのタスク |
| Done | 完了履歴 |
| By Business Owner | ビジネス責任者別 |
| By Tech Lead | 技術責任者別 |
| By GitLab Project | GitLab プロジェクト別 |

### Notion タスクページ構成

各タスクページは、以下の構成を推奨する。

```text
タスクページ
  - 背景
  - 目的 / KPI
  - 要件
  - 仕様
  - HOKUSAI 実行状態
      - Workflow ID
      - Status
      - Current Phase
      - Next Action
  - 関連 GitLab
      - Epic
      - Issue
      - MR
  - Phase 2: 調査レポート
  - Phase 3: 設計書
  - Phase 4: 作業計画
  - 意思決定ログ
```

Phase 2 / 3 / 4 は既存の `save_to_subpage_or_create()` による子ページ保存を正本とする。

## 状態同期設計

### HOKUSAI から Notion へ同期するタイミング

| タイミング | 同期内容 |
|---|---|
| workflow 作成時 | Workflow ID、Status=`Running`、Started At、Current Phase |
| 各 Phase 完了時 | Current Phase、Current Phase Name、Last Updated |
| Phase 2 完了時 | Research Page |
| Phase 3 完了時 | Design Page |
| Phase 4 完了時 | Plan Page |
| PR / MR 作成時 | GitLab MR URL、Status 更新 |
| Waiting for Human 到達時 | Status=`Waiting for Human`、Waiting Reason、Next Action |
| 失敗時 | Status=`Failed`、Error Summary、Next Action |
| 完了時 | Status=`Done`、Completed At、最終サマリ |

### Notion から HOKUSAI へ読む情報

Notion は主に入力と共有の場として使う。
HOKUSAI が読む情報は最小限にする。

読む情報:

- タスク URL
- タスク本文
- 要件・仕様
- 人間が記録した判断メモ
- 関連 GitLab Issue / MR URL

読まない情報:

- Notion の表示用ステータスだけを根拠にした内部再開判断
- LangGraph checkpoint の代替情報
- シークレット

## Human-in-the-loop 設計

人間判断待ちは Notion に明示する。

Notion に表示する内容:

- `Status = Waiting for Human`
- `Waiting Reason`
- `Next Action`
- 判断対象の Phase
- 関連する調査 / 設計 / 作業計画ページ
- 対象 GitLab MR
- `hokusai continue <workflow-id>` の案内

ただし、Notion 上のプロパティ変更だけで自動 continue する設計にはしない。
再開操作は、CLI または HOKUSAI Web Dashboard から行う。

理由:

- 誤操作による再開を避ける
- Notion API / MCP の遅延や同期ズレに影響されないようにする
- LangGraph checkpoint と SQLite state の整合性を HOKUSAI 側で保つ

## HOKUSAI Web Dashboard の再定義

### 新しい位置付け

HOKUSAI Web Dashboard は、PM 向けダッシュボードではなく、開発者向けオペレーションコンソールとする。

推奨名称:

- HOKUSAI Operations Console
- HOKUSAI Developer Console
- HOKUSAI Runtime Console

### 残す機能

| 機能 | 理由 |
|---|---|
| workflow start / continue | 実行操作は HOKUSAI Runtime 側で行う必要がある |
| retry phase | 状態補正や checkpoint 整合性が必要 |
| cleanup / delete | ローカル DB / worktree / checkpoint に関わる |
| workflow detail | デバッグに必要 |
| logs | Notion に長大ログを置かない |
| connection status | CLI / MCP / GitLab / Codex / Claude の実行環境診断 |
| config settings | YAML、接続状態、バックアップ、差分確認 |
| cross-review actions | HOKUSAI 内部状態との整合が必要 |
| retry Notion actions | Notion 書き込み失敗時の復旧 |

### Notion に移す機能

| 機能 | 移行先 |
|---|---|
| ワークフロー一覧 | HOKUSAI Workflows DB |
| PM 向け進捗確認 | Notion view |
| Phase 2 / 3 / 4 成果物閲覧 | Notion 子ページ |
| Waiting for Human 一覧 | Notion view |
| GitLab Issue / MR 関連一覧 | Notion DB / relation / URL |
| ステークホルダー向け共有 | Notion |

## 実装方針

### Phase 1: Notion 表示モデルの追加

- HOKUSAI Workflows DB のスキーマを定義する
- Notion タスクページテンプレートを定義する
- `workflow_id`、`status`、`current_phase`、`next_action` などの書き戻し項目を確定する
- 既存 `phase_subpages` と Notion DB プロパティの対応を整理する

完了条件:

- Notion 上で HOKUSAI 実行状況を一覧できる
- Phase 2 / 3 / 4 の子ページに Notion DB からたどれる

### Phase 2: HOKUSAI から Notion への同期

- workflow 開始時に Notion DB を更新する
- Phase 遷移時に `Current Phase` / `Status` を更新する
- Waiting / Failed / Done の terminal status を同期する
- GitLab MR 作成時に MR URL を同期する

完了条件:

- CLI / Web Dashboard から実行しても Notion の状態が更新される
- Notion 上の Active / Waiting / Failed / Done ビューが運用できる

### Phase 3: Web Dashboard の表現変更

- Web Dashboard の説明文やナビゲーションを Operations Console に寄せる
- PM 向け一覧の優先度を下げ、Notion への導線を追加する
- workflow detail に対応 Notion ページリンクを表示する
- 設定、接続状態、ログ、復旧操作を中心に再配置する

完了条件:

- Web Dashboard の用途が「共有ビュー」ではなく「操作・復旧コンソール」として明確になる
- Notion メインダッシュボードへのリンクがある

### Phase 4: Human-in-the-loop 運用改善

- Waiting for Human 時の Notion 表示を改善する
- `Next Action` の文言を標準化する
- Slack 通知と Notion のリンクを連携する
- 人間判断のメモを Notion に残す運用を定義する

完了条件:

- 人間判断待ちが Notion と Slack で把握できる
- 再開操作は CLI / Operations Console で安全に行える

## セキュリティ方針

- Notion にシークレットを保存しない
- Webhook URL、API token、CLI 認証情報は Notion に書かない
- Web Dashboard でもシークレット入力 UI は初期スコープ外とする
- Notion には共有に適した情報だけを書く
- ログやエラー詳細は要約だけ Notion に出し、詳細ログは Operations Console / ローカルファイルを参照する
- Notion のページ権限は GitLab メンバー、ビジネスサイド、外部共有範囲に合わせて設計する

## 運用ルール

### Notion に記録する

- 要件
- 仕様
- 調査
- 設計
- 作業計画
- 判断ログ
- 現在状態
- 次アクション
- 関連 GitLab リンク

### GitLab に記録する

- Issue
- MR
- コード差分
- レビューコメント
- CI 結果
- マージ判断

### Operations Console に残す

- 実行操作
- 再開操作
- 復旧操作
- 設定確認
- 接続状態
- ログ
- checkpoint / SQLite state に関わる詳細

## リスクと対策

| リスク | 対策 |
|---|---|
| Notion と SQLite state が不一致になる | SQLite / LangGraph checkpoint を実行正本とし、Notion は表示・共有用にする |
| Notion 更新失敗で workflow が止まる | Notion 同期は best effort とし、失敗時は retry Notion action で復旧する |
| Notion ページが複雑化する | DB プロパティはサマリに限定し、詳細は子ページに分ける |
| GitLab Epic / Issue と Notion の二重管理 | GitLab には実行情報と Notion リンクのみ置き、要件・設計は Notion に寄せる |
| Notion から誤って再開操作される | Notion は操作トリガーにせず、再開は CLI / Operations Console に限定する |
| ビジネスサイドに開発内部情報が見えすぎる | Notion には要約を出し、詳細ログや checkpoint は Operations Console に残す |

## 非ゴール

- LangGraph を Notion で置き換えること
- Notion を workflow 実行エンジンにすること
- Notion から直接ローカルコマンドを実行すること
- Web Dashboard を即時廃止すること
- シークレット管理を Notion に移すこと

## 最終判断

HOKUSAI では、Notion をメインダッシュボードとして使うべきである。
ただし、Notion は実行エンジンではなく、共有・判断・進捗確認の場として扱う。

既存 HOKUSAI Web Dashboard は廃止せず、開発者向けのオペレーションコンソールとして再定義する。

| 領域 | 推奨先 |
|---|---|
| PM / ビジネス向け進捗確認 | Notion |
| 要件・設計・判断ログ | Notion |
| HOKUSAI 実行状態サマリ | Notion |
| start / continue / retry / cleanup | Operations Console / CLI |
| ログ・checkpoint・ローカル状態確認 | Operations Console |
| 設定・接続状態・復旧 | Operations Console |

