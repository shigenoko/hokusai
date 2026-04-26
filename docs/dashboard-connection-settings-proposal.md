# 管理ウェブにサービス接続設定機能を持たせる案

## 概要

HOKUSAI が接続するサービス（Claude Code、GitHub、Codex、Notion、GitLab、Bitbucket など）の設定は、現在以下に分散している。

- `configs/*.yaml` のプロジェクト設定
- `claude` / `codex` / `gh` / `glab` など各 CLI の認証状態
- Notion MCP など外部連携の実行時接続状態
- Jira / Linear など、今後本格実装するタスクバックエンド用の API トークン

一方で、管理ウェブ（`scripts/dashboard.py` 由来のダッシュボード）にはすでに `configs/*.yaml` をフォーム / YAML モードで編集する設定ページが存在する。そのため、本提案の主眼は「YAML 設定エディタを新規に作る」ことではなく、**既存の設定ページに接続状態表示と安全な認証導線を追加する**ことに置く。

## 現状整理

| 領域 | 現状 | 評価 |
|---|---|---|
| YAML 設定編集 | 設定ページで `configs/*.yaml` を読み書き可能 | 既に実装済み。改善対象は UX / 安全性 / 対象キー拡張 |
| 設定バリデーション | 必須項目、パス、数値、cross_review、コマンド構文を検証 | 既に実装済み。接続状態とは別の検証として維持 |
| Notion 接続状態 | ワークフロー state に `notion_connected` を保存し、詳細画面で表示 | 実行単位の状態であり、グローバルなヘルスチェックではない |
| CLI 認証 | `gh auth login`、Claude Code、Codex CLI などに依存 | Web UI から直接シークレットを扱うより、状態表示と CLI 誘導が安全 |
| Jira / Linear | クライアントはスケルトン実装 | 設定 UI だけ先行しても実運用できないため、将来対応として扱う |

## 推奨: 3 層アプローチ

| 段階 | 機能 | 書き込み範囲 | 主な対象サービス | 方針 |
|---|---|---|---|---|
| (1) **接続状態表示** | 各サービスの利用可否を表示 | 読み取りのみ | `claude` CLI、`codex` CLI、`gh` CLI、`glab` CLI、Notion MCP | 最優先。副作用のないヘルスチェックとして追加 |
| (2) **既存 YAML 設定エディタの強化** | フォーム対象キー拡張、説明、警告、接続状態との連動 | YAML ファイル | GitHub / GitLab / Bitbucket、タスクバックエンド、cross_review、repositories | 既存実装を拡張する。新規実装ではない |
| (3) **シークレット管理** | API キー等を OS keyring に保存 | OS keyring | Jira、Linear、Notion API トークンなど | Web UI 直入力は避け、`hokusai connect <service>` CLI を基本導線にする |

## 接続状態表示の要件

接続状態表示は、設定保存とは独立した読み取り専用機能として扱う。

| サービス | 判定例 | 備考 |
|---|---|---|
| Claude Code | `claude` コマンド検出、必要なら軽量な非対話コマンド | 認証確認で長時間ブロックしないようタイムアウト必須 |
| Codex | `codex` コマンド検出、モデル実行可能性の軽量チェック | cross_review の選択モデルと連動して警告を出す |
| GitHub | `gh auth status` | PR 作成 / コメント返信に必要 |
| GitLab | `glab auth status` | `git_hosting.type: gitlab` のとき表示優先度を上げる |
| Notion MCP | 既存の Notion 接続確認処理を短時間チェックとして再利用 | 実行単位の `notion_connected` とは別に表示する |
| Jira / Linear | keyring / 環境変数の存在確認、API 実装後に疎通確認 | クライアント実装が完了するまでは「未対応」と明示 |

チェックはダッシュボード表示のたびに重い処理を走らせず、短いタイムアウトとキャッシュを持つ。失敗時は「未インストール」「未認証」「タイムアウト」「未対応」を分けて表示する。

## YAML 設定エディタ強化の注意点

既存の YAML 保存は `yaml.dump` による再生成のため、コメントや細かい整形が失われる可能性がある。今後、YAML モードを主要な編集経路として扱うなら、以下を検討する。

- コメント保持が必要なら `ruamel.yaml` などへの移行
- フォームモードで扱うキーを明示し、未対応キーは YAML モードで編集する設計
- 保存前差分の表示
- `.bak` バックアップの保持数や復元 UI
- API トークンらしいキー名や値を YAML に保存しようとした場合の強い警告

## シークレット管理の方針

シークレットを Web UI で扱うかどうかが最大の判断ポイント。結論として、**初期実装では Web UI に API トークン入力欄を作らない**。

理由:

- ローカル限定でも、ブラウザ経由の書き込み API には CSRF リスクがある
- localhost を狙う悪意あるページやブラウザ拡張を完全には無視できない
- シークレットファイルの権限ミスや誤コミットのリスクがある
- 既存 CLI 認証（`gh auth login` など）に寄せる方がユーザーの期待に合う

推奨する導線:

1. ダッシュボードは「未認証」「CLI で設定してください」を表示する
2. `hokusai connect <service>` を追加し、対話 CLI で認証や keyring 保存を行う
3. ダッシュボードは keyring / CLI 認証の有無だけを表示する

将来的に Web UI でシークレット入力を扱う場合は、少なくとも以下を必須条件にする。

- CSRF トークン
- `Origin` / `Host` ヘッダー検証
- 外部 CDN に依存しない設定ページ
- 保存先を OS keyring に限定し、YAML へは保存しない
- 保存後に画面・ログ・レスポンスへシークレット値を出さない
- 監査ログと保存先権限チェック

## 代替案: CLI ベースのセットアップウィザード

`hokusai connect <service>` 形式の対話 CLI を用意する案を推奨する。

メリット:

- Web UI のシークレット保持責務を避けられる
- OS keyring / `keyring` パッケージに直接保存できる
- `gh auth login` のような既存パターンに近い
- 自動化環境では環境変数を使う、ローカルでは keyring を使う、という切り分けがしやすい

デメリット:

- ダッシュボードだけでセットアップが完結しない
- CLI 実行結果をダッシュボードに反映するための再チェック導線が必要

ハイブリッド構成として、ダッシュボードは状態表示と手順提示、書き込みは CLI に寄せるのが最も安全。

## 着手順序の提案

1. **Phase A: 接続状態表示**
   - 設定ページに「サービス接続状態」セクションを追加する
   - `claude` / `codex` / `gh` / `glab` の存在・認証状態を表示する
   - Notion は実行単位の状態とは別に、軽量な接続確認として表示する

2. **Phase B: 設定ページの安全性と UX 強化**
   - YAML 保存時のコメント消失や未対応キーの扱いを明記する
   - 保存前差分、バックアップ復元、トークン混入警告を追加する
   - 接続状態に応じて cross_review / git_hosting / task_backend の警告を表示する

3. **Phase C: `hokusai connect` CLI**
   - `hokusai connect github` は `gh auth login` の確認・誘導
   - `hokusai connect gitlab` は `glab auth login` の確認・誘導
   - `hokusai connect linear` / `jira` は keyring 保存を実装する。ただし、各クライアントの API 実装完了後に有効化する

4. **Phase D: シークレット管理の Web 連携（任意）**
   - Web UI でのトークン入力は最後に検討する
   - 実装する場合も、保存先は YAML ではなく OS keyring に限定する

## 次のアクション

- 接続状態表示で対象にするサービスを `claude` / `codex` / `gh` / `glab` / Notion MCP に絞る
- 状態チェック API のレスポンス形式を決める
- Jira / Linear は「未実装サービス」として UI 上で誤解なく表示する
- `hokusai connect <service>` のコマンド仕様を別ドキュメントまたは Issue に切り出す
