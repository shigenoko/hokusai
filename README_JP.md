# HOKUSAI

**HOKUSAI** = **H**uman-**O**rchestrated **K**nowledge & **U**nified **S**ystem for **A**I **I**ntegration

LangGraph をベースにした AI 開発ワークフロー自動化ツール。Claude Code と統合されている。

[English README is here](./README.md)

## 概要

HOKUSAI は調査・計画・実装・検証・レビュー・プルリクエスト管理を自動化する 10 フェーズの開発ワークフローをオーケストレーションする。[LangGraph](https://github.com/langchain-ai/langgraph) 上に構築され、[Claude Code](https://claude.com/claude-code) および GitHub CLI (`gh`) と密に統合されている。

名前は設計思想を反映している。意思決定とレビューは **人間がオーケストレーション** し、AI ツール群を **統合システム** が連携させて実装・検証を担う。各フェーズは人間の判断を待つために一時停止可能で、Phase 8 の統合レビューループは Copilot と人間のレビューコメントを順不同で処理する。これにより **Human-in-the-Loop (HITL)** な開発を安全かつ予測可能に進められる。

## なぜ HOKUSAI なのか

HOKUSAI は、**信頼性・説明責任・コントロール** が重視される組織のために設計された、人間中心の AI ワークフローシステムです。

金融、決済、エンタープライズシステムといった業界では、AI を野放しで動作させることはできません。あらゆる意思決定は **説明可能（explainable）で、監査可能（auditable）で、最終的に人間が責任を負う（owned by a human）** 必要があります。

HOKUSAI はこのギャップを橋渡しします。

断片化した AI の利用を、構造化された再現可能なワークフローに変換します:

- **AI が実行を加速する**
- **人間がコントロールと責任を保持する**
- **知識とプロセスが標準化される**
- **すべてのステップが追跡可能で監査可能**

HOKUSAI は人間を置き換えるのではなく、人間を中心に AI をオーケストレーションします。

実世界の業務に AI を統合する統一フレームワークを、**安全に・透明性をもって・スケーラブルに** 提供します。

## 課題（The Problem）

エンタープライズ環境での AI 活用は、断片的でコントロールが難しい状況にあります。

- チームごとに AI の使い方がばらばら
- プロンプトやワークフローが標準化されていない
- アウトプットが必ずしも追跡可能・監査可能ではない
- 人間の責任範囲が不明瞭

金融や決済のような規制業界では、これが安全に AI 活用をスケールさせる障害になります。

## 解決策（The Solution）

HOKUSAI は、AI 統合のための構造化された Human-in-the-Loop ワークフローを提供します。

場当たり的な AI 利用を、再現可能でコントロール可能なプロセスに変換します:

- AI が実行を加速する
- 人間が意思決定の権限を保持する
- 知識とプロセスが標準化される
- すべてのステップが追跡可能で監査可能

## ワークフロー

HOKUSAI はシンプルでパワフルなワークフローを軸に設計されています:

1. **Research（調査）** — タスクのスコープと既存コードを調査
2. **Design（設計）** — アーキテクチャと方針を計画
3. **Plan（計画）** — 実行チェックリストを段階的に構築
4. **Implement（実装）** — Claude Code 経由で変更を実行
5. **Verify（検証）** — テストと lint で正しさを確認
6. **Review（レビュー）** — 品質チェックリストに沿った最終レビュー
7. **Branch hygiene（ブランチ衛生）** — スコープとベースブランチの整合性を確認
8. **PR draft → 統合レビューループ** — Draft PR を作成し、Copilot/人間のレビューコメントを順不同で処理
9. **Approval（承認）** — マージのために人間が PR を承認
10. **Record（記録）** — トレーサビリティと監査のために結果を永続化

各フェーズは人間の入力を待つために一時停止可能です。人間が遷移を承認したり、修正を要求したり、いつでも介入できます。**責任は人間側に明確に置きながら、AI が実行を担当する**設計になっています。

## 機能

### 標準機能

- 10 フェーズの LangGraph ワークフロー（調査 → 設計 → 計画 → 実装 → 検証 → レビュー → ブランチ衛生 → PR draft → 統合レビューループ → 記録）
- CLI コマンド: `start`、`continue`、`status`、`list`、`cleanup`、`pr-status`
- Web ダッシュボード（`scripts/dashboard.py`）で実行状況を監視
- SQLite による永続化と LangGraph checkpoint
- Claude Code 連携による自律実装
- `gh` CLI 経由の GitHub 連携
- GitHub Issue タスクバックエンド
- Phase 7.5 ブランチ衛生チェック（ファイルスコープ、ベースブランチ同期）
- `prompts/` 配下のカスタマイズ可能なプロンプト

### 実験的機能

以下のコンポーネントはコードベースに含まれるがデフォルトでは有効化されない。挙動は予告なく変更される可能性がある。

- **Notion タスクバックエンド** — `HOKUSAI_SKIP_NOTION=1` で Notion アクセスをスキップする
- **複数リポジトリ対応**（モノレポ風） — デフォルトは単一リポジトリ
- **クロス LLM レビュー** — 複数 LLM のセットアップが必要
- **Jira / Linear / GitLab / Bitbucket 連携** — インターフェースは存在するが未完成

## 前提条件

- **Python**: 3.11 以降
- **`gh` CLI**: `repo` スコープで認証済み（PR 管理とレビューコメント返信に必要）
- **Claude Code CLI**: インストール・設定済み（自律実装に使用）
- **Git**: 2.30 以降

Phase 8 の統合レビューループは `gh` 経由で PR レビューコメントに返信するため、認証ユーザーが対象リポジトリへの write 権限を持っている必要がある。

## インストール

```bash
# uv 推奨
uv pip install hokusai-flow

# または pip
pip install hokusai-flow
```

> 注: GitHub リポジトリ名は `hokusai` だが、PyPI 配布名は `hokusai-flow`。これは PyPI 上で `hokusai` が無関係のプロジェクトに既に取得されているため。

## クイックスタート

```bash
# GitHub Issue URL から新規ワークフローを開始
hokusai -c configs/example-github-issue.yaml start https://github.com/your-org/your-repo/issues/1

# ワークフロー一覧
hokusai list

# レビューで停止したワークフローを再開
hokusai continue <workflow-id>

# 状態確認
hokusai status <workflow-id>

# ダッシュボードを開く
python scripts/dashboard.py
```

状態はデフォルトで `~/.hokusai/` 配下に保存される（`workflow.db`、`checkpoint.db`、`logs/`）。必要に応じて設定の `data_dir` で上書き可能。

## 設定

サンプルは `configs/example-github-issue.yaml` および `configs/example-gitlab.yaml` を参照。最小構成は以下:

```yaml
project_root: ~/repos/my-project
base_branch: main

task_backend:
  type: github_issue

git_hosting:
  type: github
```

## ドキュメント

- 実装プロンプト: `prompts/`
- 各フェーズノードのソース: `hokusai/nodes/`
- 設定モデル: `hokusai/config/models.py`

## 制限事項

- Phase 8 の統合レビューループは現状 GitHub のプルリクエスト前提。GitLab/Bitbucket 対応は実験的。
- CLI はシングルユーザー想定。同一タスク URL に対する並行ワークフローはサポートしない。
- `prompts/` 配下のプロンプトは日本語タスク向けに調整されている。英語タスク向けの調整は進行中。

## ライセンス

Apache License 2.0。[LICENSE](./LICENSE) を参照。

## コントリビューション

このプロジェクトは alpha 段階。Issue と Pull Request は歓迎。大きな変更については、まず Issue を起票して相談してほしい。
