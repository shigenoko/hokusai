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

## 進捗状況

| Phase | 状態 | 主な PR |
|---|---|---|
| Phase A: 接続状態表示 | ✅ 完了 | PR #1 / #3 |
| Phase B-1: 保存前差分プレビュー | ✅ 完了 | PR #5 |
| Phase B-2: `.bak` 復元 UI | ✅ 完了 | PR #5 |
| Phase B-3: トークン混入警告 | ✅ 完了 | PR #4 |
| Phase B-4: 接続状態と config の整合性警告 | ✅ 完了 | PR #4 |
| Phase B-5: パストラバーサル防御の全経路化 | ✅ 完了 | PR #6 |
| Phase B-6: 多世代 `.bak` バックアップ + 選択復元 | ✅ 完了 | PR #7 |
| Phase C: `hokusai connect` CLI（gh / glab） | ✅ 完了 | PR #2 |
| Phase C': `hokusai connect linear` / `jira`（keyring 保存） | 🚫 保留 | クライアント未実装のため |
| Phase D: シークレット管理の Web 連携 | 🚫 保留 | Phase C' 完了後に再検討 |

## 着手済みフェーズの実装サマリ

### Phase A: 接続状態表示

- `hokusai/integrations/connection_status.py` を新設し、サービスレジストリ + TTL キャッシュ + status / severity / category / next_action / message_key を含む共通レスポンス構造を提供
- `GET /api/connections` / `GET /api/connections/{service}` を追加。`?refresh=1` でキャッシュ無視、`?mode=deep` は API のみ保持して UI 非露出
- 設定ページ冒頭に「サービス接続状態」カードと再チェックボタンを実装
- 対応サービス: `claude` / `codex` / `gh` / `glab` / `notion_mcp` / `jira` / `linear`
- ダッシュボード UI の `next_action` は `hokusai connect <service>` を案内（PR #3）

### Phase B: 設定ページの安全性と UX 強化

- **B-1 差分プレビュー**: `POST /api/config/diff` で unified diff を返し、設定ページにモーダル表示。`save_config_yaml` と同じ `yaml.dump` 形式で diff を取って整合性確保
- **B-2 `.bak` 復元 UI**: `GET /api/config/backup` / `POST /api/config/backup/restore` を追加。設定ページにバックアップ一覧と「この世代に戻す」ボタンを設置
- **B-3 トークン直書き警告**: GitHub PAT / GitLab PAT / Anthropic / OpenAI 形式 + キー名ヒューリスティック（`token` / `api_key` / `secret` / `password` 等）で `validate_config` の warnings に追記。`re.fullmatch` ベースの伏字判定（`<placeholder>` / `[*xX-]{4,}` / 4 文字以上同一文字）で false-positive を避ける
- **B-4 接続整合性警告**: `git_hosting` / `task_backend` / `cross_review` の各設定値と `connection_status` の状態を突き合わせ、未認証等のとき warning を出して `hokusai connect <service>` を案内
- **B-5 パストラバーサル防御**: `_safe_config_path` を新設し、`config_name` を `^[A-Za-z0-9_\-]+$` の許可文字集合に制限。`load_config_yaml` / `save_config_yaml` / `compute_config_diff` / `restore_config_backup` / `parse_project_rules` 全経路で CONFIGS_DIR 配下に限定。`glob.escape` で defense in depth
- **B-6 多世代バックアップ**: `.bak` を `.bak.<YYYYMMDD-HHMMSS-microseconds>` 形式に拡張し、`BACKUP_RETAIN_COUNT = 10` で自動 prune。同一秒内の連続保存にも UUID 短縮ハッシュ末尾でユニーク化対応。`_safe_backup_path` で他 config の `.bak` を指定した上書きを 400 で拒否
- **API 一貫性**: 全エンドポイントで client error → 400 / not found → 404 / I/O 失敗 → 500 を明示。`restore_config_backup` は `(success, error_code, error_message)` 形式で構造化エラーを返し、ハンドラ側は dict 引きでステータスを決定（メッセージ文字列の substring match に依存しない）

### Phase C: `hokusai connect` CLI

- `hokusai connect github` / `hokusai connect gitlab` で確認プロンプト後に `gh auth login` / `glab auth login` を自動実行
- 非対話環境（パイプ / `--no-interactive`）では実行コマンドの表示のみ
- `--force` で再認証、`--status` で `connection_status` を CLI から閲覧
- 認証実行前に `connection_status` キャッシュをクリアし、続けて呼ばれる `--status` / ダッシュボードに古い状態が残らないようにする

## 保留中の方針

### Phase C'（Linear / Jira の keyring 保存）

クライアント実装がスケルトンのままなので、keyring に保存しても「保存できるが使えない」状態になる。
**前提条件**: Linear / Jira の API クライアント実装完了。

### Phase D（Web シークレット管理）

Phase C' が動くようになっても、Web UI でのシークレット直接入力は CSRF / origin 検証 / 保存先の権限チェックといった追加責務を伴う。
当面は `hokusai connect` CLI 経由の認証で十分とし、必要性が高まった時点で再検討する。

## 次に検討する候補（必要性が顕在化した時点で着手）

- **`mode=deep` 実装**: Notion MCP サーバへの軽量 ping。現状は設定ファイルの存在確認のみ。MCP の stdio 通信を直接叩く実装が必要で中程度の作業
- **Linear / Jira クライアントの実装**: Phase C' / D 着手の前提。外部依存が大きい
- **保持世代数の config 化**: 現状ハードコード `BACKUP_RETAIN_COUNT = 10`
- **バックアップとの差分表示**: 「この世代と現在の差分を見る」ボタンを各バックアップ行に追加
- **別ページでの全バックアップ閲覧 UI**: 多 config 横断のバックアップ管理画面

## 完了状態のメトリクス（2026-04-27 時点）

- 関連 PR: #1〜#7（マージ済み）
- 累計テスト数: 927 件
- 主要モジュール: `hokusai/integrations/connection_status.py` / `hokusai/cli/commands/connect.py` / `scripts/dashboard.py`
- 公開 API: `/api/connections` / `/api/connections/{service}` / `/api/config/diff` / `/api/config/backup` / `/api/config/backup/restore`
- セキュリティ防御: `_safe_config_path` / `_safe_backup_path` / `glob.escape` の三段階で defense in depth
