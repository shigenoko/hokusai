# HOKUSAI Figma / Miro 連携要件書

## 1. 目的

HOKUSAI に Figma / Miro 連携を追加し、ビジネス、デザイン、エンジニアリングの情報を一貫したワークフローで扱えるようにする。

現行の HOKUSAI は、Notion を企画・要件・判断・進捗共有の場所、GitLab を実装・レビュー・CI の場所として扱う。ここに Figma と Miro を加え、以下を実現する。

- Miro 上のラフスケッチ、業務フロー、画面構成を HOKUSAI が読み取る
- Figma 上の UI / UX デザイン、画面仕様、デザインレビュー状態を HOKUSAI が読み取る
- Notion タスクを起点に、Miro / Figma / GitLab の関連情報を集約する
- HOKUSAI が要件、業務フロー、デザイン、実装のズレを検知する
- GitLab 上の実装や MR に、関連する Miro / Figma 情報を反映する

## 2. 位置付け

| ツール | 主な利用者 | HOKUSAI での扱い | 正本として扱う情報 |
|---|---|---|---|
| Notion | PM、ビジネス、全体共有 | ワークフロー起点、進捗共有、判断ログ | 要件、背景、仕様、進捗、意思決定 |
| Miro | ビジネス、PM | 企画・検討内容の入力元 | 業務フロー、ラフスケッチ、画面構成、アイデア |
| Figma | デザイナー | UI / UX デザインの入力元 | 画面仕様、UI デザイン、プロトタイプ、デザインレビュー |
| GitLab | エンジニア | 実装・レビューの実行場所 | Issue、MR、コード差分、CI、コードレビュー |
| HOKUSAI | 横断基盤 | 各ツールの連携、同期、検証 | 実行状態、連携状態、ズレ検知結果 |

HOKUSAI は各ツールを置き換えない。各ツールを正本のまま維持し、必要な情報を読み取り、相互にリンクし、開発ワークフローへ反映する。

## 3. 対象スコープ

### 3.1. MVP スコープ

初期実装では、連携の価値が大きく、実装リスクが低い範囲に絞る。

- Notion タスクに Miro URL / Figma URL を登録できる
- HOKUSAI が Miro URL / Figma URL を検出できる
- HOKUSAI が Miro の主要情報を取得し、Phase 2 調査と Phase 3 設計チェックに反映する
- HOKUSAI が Figma の主要情報を取得し、Phase 3 設計チェックと Phase 5 実装に反映する
- GitLab MR に Miro / Figma リンクを自動記載する
- Notion Dashboard に Miro / Figma 連携状態を表示する
- デザイン確認が必要な場合、HOKUSAI が Waiting for Human として停止できる

### 3.2. 将来スコープ

- Miro のラフスケッチを Figma のワイヤーフレーム下書きに変換する
- Figma コメントへ HOKUSAI から返信する
- Figma / Miro の更新 Webhook を受け取り、Notion / Slack / HOKUSAI 状態へ反映する
- Figma デザインと実装画面の差分確認を行う
- Figma Variables / design token を実装へ反映する
- Miro / Figma / GitLab / Notion 間のリンクを双方向に自動同期する

## 4. Miro 連携要件

### 4.1. Miro から取得する情報

Miro は、ビジネス側の検討内容、業務フロー、ラフな画面構成を扱う入力元とする。

HOKUSAI は以下の情報を取得する。

- ボード URL
- ボード名
- frame 名
- 付箋のテキスト
- テキストオブジェクト
- 図形、矢印、コネクタ
- 業務フローや画面遷移を示す構造
- ラフスケッチ、ワイヤーフレーム相当の画面構成
- コメント、補足メモ
- 最終更新日時

### 4.2. Miro 情報の利用方法

HOKUSAI は Miro から取得した情報を以下に使う。

- Notion タスクの要件理解を補完する
- Phase 2 調査で業務フロー、背景、検討メモを整理する
- Phase 3 設計チェックで画面構成やユーザーフローを確認する
- Figma デザインとの対応関係を確認する
- GitLab MR に関連 Miro リンクを記載する
- 実装前に不明点がある場合、Notion または Slack に確認事項を出す

### 4.3. Miro から Figma への変換

Miro のラフスケッチを HOKUSAI 経由で Figma に移すことは可能とする。ただし初期段階では、完成デザインへの自動変換ではなく、デザイナーが調整するための下書き生成として扱う。

想定フロー:

1. ビジネス側が Miro にラフスケッチや業務フローを書く
2. Notion タスクに Miro URL を貼る
3. HOKUSAI が Miro の内容を読み取る
4. HOKUSAI が画面、ボタン、入力欄、説明文、遷移を構造化する
5. HOKUSAI が Figma にワイヤーフレーム下書きを作成する
6. デザイナーが Figma 上で最終デザインへ整える

この機能は MVP ではなく将来スコープとする。

## 5. Figma 連携要件

### 5.1. Figma から取得する情報

Figma は、UI / UX デザインと画面仕様の正本として扱う。

HOKUSAI は以下の情報を取得する。

- Figma ファイル URL
- page / section / frame URL
- node id
- frame 名、画面名
- デザイン画像
- 画面構成
- レイアウト情報
- テキスト
- 色、フォント、余白、サイズ
- コンポーネント、variant、instance
- UI 状態
- プロトタイプリンク、画面遷移
- デザイナーコメント
- 未解決コメント
- 最終更新日時
- 実装可能状態、デザインレビュー状態

### 5.2. Figma 情報の利用方法

HOKUSAI は Figma から取得した情報を以下に使う。

- Phase 3 設計チェックで UI 仕様として参照する
- Phase 5 実装で画面構成、レイアウト、コンポーネント、色、テキストを参照する
- GitLab MR に対応する Figma リンクを記載する
- デザインレビュー未完了の場合、実装やマージ判断で警告する
- Figma が更新された後に実装が古いままになっていないか確認する
- Notion Dashboard に Figma 連携状態とデザイン確認状態を表示する

### 5.3. HOKUSAI から Figma へ返す情報

MVP では Figma への書き込みは必須としない。将来的には以下を扱う。

- GitLab Issue URL
- GitLab MR URL
- Notion タスク URL
- 実装ステータス
- デザインレビュー依頼
- 実装差分サマリ
- 実装上再現できなかった点
- HOKUSAI からの確認コメント

## 6. Notion 連携要件

Notion は、PM と関係者が状況を確認する中心画面として扱う。

### 6.1. Notion タスクに追加する項目

Notion タスクまたは HOKUSAI Workflows DB に以下の項目を追加する。

- Miro URL
- Figma URL
- Design Status
- Design Review Required
- Design Review Result
- Miro Last Synced At
- Figma Last Synced At
- Miro Summary Page
- Figma Summary Page
- Design Notes

### 6.2. Notion に表示する情報

- Miro / Figma のリンク
- Miro から抽出した業務フロー概要
- Figma から抽出した画面仕様概要
- デザイン確認状況
- 未解決コメント件数
- GitLab Issue / MR との対応
- HOKUSAI が検知したズレ
- 次に人間が判断すべき内容

## 7. GitLab 連携要件

GitLab は実装・レビュー・CI の正本として扱う。

HOKUSAI は GitLab Issue / MR に以下を反映する。

- Notion タスク URL
- Miro URL
- Figma URL
- 実装対象画面
- デザイン確認状況
- デザインレビューが必要かどうか
- 実装時に参照した Figma frame
- Miro / Figma と実装の差分メモ

MR 作成時には、レビュアが確認しやすいように Miro / Figma へのリンクを本文に含める。

## 8. HOKUSAI ワークフローへの組み込み

### Phase 2: Research

- Notion タスクから Miro / Figma URL を検出する
- Miro の業務フロー、付箋、図解を読み取る
- Figma URL がある場合は対象画面の概要を取得する
- 調査レポートに Miro / Figma の参照情報を含める

### Phase 3: Design Check

- Notion 要件、Miro 業務フロー、Figma UI 仕様を突き合わせる
- 要件と画面仕様の矛盾を検出する
- Miro の画面構成と Figma の画面が対応しているか確認する
- 不明点があれば Human Review として停止できる

### Phase 4: Plan

- 実装計画に Miro / Figma の参照対象を明記する
- デザイン確認が必要なタイミングを計画に含める

### Phase 5: Implement

- Figma の画面構成、テキスト、スタイル、コンポーネント情報を実装コンテキストに含める
- Miro の業務フローを実装仕様の補助情報として使う
- デザインと実装の差分が出る場合は記録する

### Phase 7: Review

- 実装が Figma の対象画面に沿っているか確認する
- UI / UX レビュー項目に Figma 参照を含める

### Phase 8: MR / Review Loop

- MR に Notion / Miro / Figma リンクを含める
- デザインレビューが必要な場合は Waiting for Human にできる
- 未解決コメントがある場合は警告する

### Phase 10: Record

- Notion に最終的な Miro / Figma / GitLab の対応関係を記録する
- 実装時に発生したデザイン差分や代替判断を記録する

## 9. ズレ検知要件

HOKUSAI は以下のズレを検知できること。

- Notion の要件と Miro の業務フローが一致していない
- Miro のラフ画面に対応する Figma 画面が存在しない
- Figma の対象 frame が更新された後、GitLab MR が古い設計のまま進んでいる
- Figma のデザインレビューが未完了のまま MR が Ready for Review になっている
- Figma の未解決コメントが残っている
- GitLab MR に対応 Figma URL が記載されていない
- Notion タスクに必要な Miro / Figma URL が不足している

## 10. 認証・権限要件

### 10.1. Figma

- Figma API token または OAuth 認証を利用する
- 対象ファイルへの read 権限を必須とする
- Figma への書き込みは MVP では任意とする
- token は Notion や GitLab には保存しない
- token は環境変数、OS keyring、または既存の HOKUSAI 接続管理方針に従って扱う

### 10.2. Miro

- Miro API / MCP の認証を利用する
- 対象ボードへの read 権限を必須とする
- Miro への書き込みは MVP では任意とする
- Enterprise 環境では管理者による MCP / API 利用許可が必要になる場合がある

## 11. 非機能要件

- Notion / Miro / Figma 同期が失敗しても、HOKUSAI 本体のワークフローは即時停止しない
- 同期失敗は retry 可能にする
- API rate limit を考慮する
- 取得したデザイン情報は必要最小限にする
- シークレットは Notion / Miro / Figma / GitLab の本文に書き込まない
- 外部ツールの更新日時を記録し、古い情報を使った実装を検知できるようにする
- 連携状態は HOKUSAI Web Dashboard または Notion Dashboard で確認できるようにする

## 12. 初期実装タスク

1. 設定 YAML に `figma` / `miro` 連携設定を追加する
2. `hokusai connect figma` / `hokusai connect miro` の接続確認方針を定義する
3. Notion タスクから Miro / Figma URL を抽出する
4. Miro 情報取得クライアントを追加する
5. Figma 情報取得クライアントを追加する
6. Phase 2 / Phase 3 のプロンプトに Miro / Figma 情報を注入する
7. MR description に Miro / Figma リンクを追加する
8. Notion Dashboard に Miro / Figma 連携項目を追加する
9. 同期失敗時のエラー記録と retry 方針を追加する
10. 連携なしでも既存ワークフローが壊れないことをテストする

## 13. MVP 完了条件

- Notion タスクに Miro URL / Figma URL を指定できる
- HOKUSAI が Miro / Figma URL を検出できる
- HOKUSAI が Miro / Figma の概要情報を取得できる
- Phase 2 / Phase 3 の出力に Miro / Figma 情報が反映される
- GitLab MR に Miro / Figma リンクが記載される
- Notion Dashboard に Miro / Figma 連携状態が表示される
- Miro / Figma 連携が無効でも既存の Notion / GitLab ワークフローが動作する

## 14. 対象外

MVP では以下を対象外とする。

- Figma 上で完成デザインを自動生成する
- Miro の手描きスケッチを完全な UI デザインへ自動変換する
- Figma と実装画面のピクセル完全一致判定
- Figma / Miro への双方向コメント同期
- Figma Variables から production design token を自動更新する
- Notion から Miro / Figma を直接編集する

## 15. 導入先への説明

導入先企業には、以下のように説明する。

> HOKUSAI は、Notion を要件と進捗の正本、Miro をビジネス側の業務フローやラフスケッチの正本、Figma を UI / UX デザインの正本、GitLab を実装とレビューの正本として扱います。HOKUSAI はこれらの情報を読み取り、実装計画や MR に反映し、要件・デザイン・実装のズレを検知します。

