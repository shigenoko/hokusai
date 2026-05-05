# HOKUSAI 導入における Notion / GitLab Epic / Issue / MR の役割分担

**対象読者**: プロジェクト責任者・PM・テックリード
**目的**: HOKUSAI を導入先企業で活用するにあたり、既存の Notion・GitLab Epic / Issue / MR をどう使い分けるかの判断材料を示す
**前提**: 開発環境として GitLab と Notion を併用しているチーム

---

## 1. エグゼクティブサマリ

HOKUSAI は「タスク管理」と「コード管理」を独立した軸として扱うため、Notion と GitLab はそれぞれの強みを活かして併用するのが最適です。

**結論: 4 つすべて使う。ただし担当レイヤーが異なる。**

| レイヤー | 担当ツール | 主な用途 |
|---|---|---|
| 企画 | **GitLab Epic** | 複数タスクを束ねる上位計画 |
| タスク（中身） | **Notion** | 仕様・調査・設計・進捗の蓄積 |
| タスク（チケット） | **GitLab Issue** | Epic と MR をつなぐ実装チケット |
| 実装 | **GitLab MR** | コード変更・レビュー・マージ |

HOKUSAI が直接読み書きするのは **Notion と GitLab MR の 2 つ**。Epic と Issue は人間の運用レイヤーとして残す形になります。

---

## 2. 役割整理表

| 機能 | 使う？ | 用途 | HOKUSAI の関与 | 永続性 |
|---|---|---|---|---|
| **Notion** | ✅ | タスクの「中身」の蓄積場所。仕様、事前調査レポート（Phase 2）、詳細設計書（Phase 3）、作業計画書（Phase 4）、進捗履歴 | 直接読み書きする（`task_backend: notion`）。HOKUSAI の入力点 | 高（タスク完了後も参照） |
| **GitLab Epic** | ✅ | 複数タスクを束ねる「企画」レイヤー。ロードマップ、進捗の俯瞰、四半期テーマ | 触らない（人間が手動管理） | 中（プロジェクト期間中） |
| **GitLab Issue** | ✅ | Epic ↔ MR の橋渡し。Notion タスクと 1:1 対応させる「実装チケット」 | 触らない（人間が Notion タスク作成と並行で作成） | 中（クローズで役割終了） |
| **GitLab MR** | ✅ | コード変更そのもの。CI、コードレビュー、マージ。Issue を `Closes #N` で参照 | Phase 8 で自動作成（`git_hosting: gitlab`） | 低（マージで役割終了） |

---

## 3. なぜこの分担になるのか（構造的な理由）

### 理由 1: GitLab Epic は Issue だけを子要素にできる

```
Epic（企画）
  └─ Issue（タスク）  ← Notion タスクと 1:1 対応
       └─ MR（実装）  ← HOKUSAI が自動作成、Issue を Closes 参照
```

MR は Epic に直接紐付かないため、Epic を活用するなら Issue を介在させる以外の方法がありません。

### 理由 2: 設計書を Notion に置く価値

- **構造化された子ページ階層**で、Phase 別ドキュメントが整理される
- **非エンジニアも閲覧・編集可能**（PM・デザイナー・QA）
- **検索性・再利用性が高い**（過去案件の設計判断を引き出せる）
- HOKUSAI は Phase 2/3/4 で自動的にこの構造を作る

GitLab Issue のコメントに同じ内容を書くと、時系列で流れてしまい後から読みにくくなります。

### 理由 3: HOKUSAI が「コード変更」を担う以上 MR は GitLab 側に必要

HOKUSAI は Phase 8 で MR を自動作成し、Phase 8 統合レビューループで Copilot レビュー / 人間レビューに自動応答します。CI・コードレビュー・マージは GitLab MR の標準機能をそのまま使えるので、ここを Notion に持ってくる理由はありません。

---

## 4. 各レイヤーの使い分け

### 4.1. Notion（中身の蓄積）

| 項目 | 内容 |
|---|---|
| 書く内容 | 仕様、要件、調査レポート、設計判断とその根拠、作業計画 |
| 読む人 | PM、デザイナー、エンジニア、レビュアー、QA |
| 書く人 | PM が起票 → HOKUSAI が Phase 2/3/4 のレポートを追記 |
| 主な機能 | 子ページ階層、ステータスマッピング、チェックリスト |

### 4.2. GitLab Epic（企画の俯瞰）

| 項目 | 内容 |
|---|---|
| 書く内容 | 企画名、概要、期日、子 Issue 一覧（自動）、進捗バーンダウン |
| 読む人 | PM、マネージャ、ステークホルダー |
| 書く人 | PM・テックリードが起票・更新 |
| 主な機能 | Roadmap、子 Issue ツリー、進捗バー |

### 4.3. GitLab Issue（実装チケット）

| 項目 | 内容 |
|---|---|
| 書く内容 | 一行サマリ、**Notion タスクページ URL（必須）**、Epic 紐付け、優先度ラベル |
| 読む人 | エンジニア、レビュアー |
| 書く人 | PM・テックリードが起票（Notion タスクと並行で作成） |
| 書かないもの | 詳細設計、調査レポート（→ Notion へ） |

### 4.4. GitLab MR（実装）

| 項目 | 内容 |
|---|---|
| 書く内容 | 変更概要、`Closes #<issue-number>`、テスト計画、Notion タスク URL |
| 読む人 | レビュアー、CI |
| 書く人 | HOKUSAI が Phase 8 で自動作成、Phase 9 でレビュー対応も自動 |

---

## 5. HOKUSAI 設定例

```yaml
task_backend:
  type: notion
  # Notion タスクページ URL を入力に動く

git_hosting:
  type: gitlab
  base_url: https://gitlab.com
  project_path: your-group/your-repo
  # Phase 8 で MR を作成する
```

---

## 6. 運用フローの例

### ステップ 1: 企画起票（人間 / PM）

1. GitLab で Epic を作成（例: 「2026-Q2: 認証基盤刷新」）
2. Epic 配下に複数の子 Issue を作成（例: 「OAuth 連携追加」「セッション管理改善」）
3. 各 Issue に対応する Notion タスクページを作成し、Issue 本文に Notion URL を記載

### ステップ 2: HOKUSAI 起動（人間 → HOKUSAI）

```bash
hokusai start <Notion タスクページの URL>
```

- HOKUSAI は Notion タスクを起点に Phase 1〜10 を実行
- Phase 2/3/4 のレポートは Notion 子ページに追記される
- Phase 8 で GitLab MR を自動作成

### ステップ 3: GitLab MR の Issue 紐付け（HOKUSAI）

- MR 作成時、HOKUSAI が MR description に `Closes #<issue-number>` を記載するよう設定
- これにより MR マージ時に Issue が自動クローズされ、Epic の進捗も更新

### ステップ 4: レビュー（HOKUSAI + 人間）

- Phase 8 統合レビューループが自動でレビューコメントに対応
- 重要な判断が必要な箇所では HOKUSAI が停止し、Slack 通知で人間に判断を求める
- 人間の判断後、`hokusai continue <workflow-id>` で再開

### ステップ 5: マージ後（自動）

- MR マージ → Issue クローズ → Epic 進捗更新（GitLab 標準機能）
- Notion タスクのステータスを HOKUSAI が更新

---

## 7. 留意事項・制限事項

### 7.1. Phase 8 統合レビューループの GitLab 対応は実験的

HOKUSAI の Phase 8 統合レビューループは GitHub を前提として実装されています。GitLab MR でも MR 作成自体は動作しますが、以下は手動補完が必要になる可能性があります:

- MR レビューコメントへの自動返信
- Copilot 相当のレビュー Bot 連携
- レビュー状態の自動同期

> 📌 **対応策**: 導入初期は GitHub プロジェクトでパイロット運用 → GitLab Phase 8 対応を順次拡張する形が現実的。

### 7.2. Issue と Notion タスクの「二重管理」コスト

各タスクで Issue + Notion タスクページの両方を作る必要があります。

> 📌 **対応策**:
> - Notion タスクテンプレートに「対応 GitLab Issue URL」フィールドを設ける
> - PM 側で「Issue 起票 → Notion ページ作成」を 1 つの作業として運用ルール化
> - 将来的にはスクリプトで自動化可能（Issue 起票時に Notion ページを自動生成）

### 7.3. Notion 接続が必須

HOKUSAI は Notion MCP 経由で Notion にアクセスします。`HOKUSAI_SKIP_NOTION=1` でスキップも可能ですが、その場合は Phase 2/3/4 のレポート蓄積機能が無効化されます。

### 7.4. 機密情報の取り扱い

- Notion タスクページに機密情報を書く場合は、Notion 側のページ権限を適切に設定
- GitLab Issue / MR にも Notion URL が貼られるため、Notion 側の閲覧権限が GitLab メンバーに付与されているか確認

---

## 8. 導入チェックリスト

### 8.1. 環境準備

- [ ] GitLab で対象プロジェクトに Epic 機能が有効（GitLab Premium 以上）
- [ ] Notion ワークスペースが用意され、HOKUSAI 用の DB / ページが作成済み
- [ ] Notion MCP が Claude Code に接続されている（`claude mcp list` で確認）
- [ ] `glab` CLI がインストール・認証済み（`glab auth status`）
- [ ] HOKUSAI の `git_hosting.type: gitlab` 設定が動作確認済み

### 8.2. 運用ルール策定

- [ ] Notion タスクと GitLab Issue の対応付けルール（URL 双方向リンク）
- [ ] Issue 起票時の必須項目（Notion URL、Epic 紐付け、優先度ラベル）
- [ ] MR description テンプレートに `Closes #<issue>` と Notion URL を含める
- [ ] レビュー時の人間判断ポイント（どこで HOKUSAI を停止するか）

### 8.3. 通知運用

- [ ] Slack Incoming Webhook を `HOKUSAI_SLACK_WEBHOOK_URL` に設定
- [ ] 通知対象イベント（最低でも `waiting_for_human` / `workflow_failed` / `pr_created`）の合意
- [ ] 通知先 Slack チャンネルの周知

### 8.4. パイロット運用

- [ ] 小さい Epic（子 Issue 2〜3 個）で 1 サイクル試す
- [ ] HOKUSAI のレポート品質を Notion 上で確認
- [ ] GitLab MR の自動作成・レビュー対応の挙動を観察
- [ ] 二重管理（Issue + Notion）のコストを実測し、運用ルールを微調整

---

## 9. まとめ

| 質問 | 回答 |
|---|---|
| Notion を使う？ | **Yes** — タスクの中身の蓄積場所。HOKUSAI の入力点 |
| GitLab Epic を使う？ | **Yes** — 企画レイヤー。HOKUSAI は触らない |
| GitLab Issue を使う？ | **Yes** — Epic と MR の橋渡し |
| GitLab MR を使う？ | **Yes** — HOKUSAI が Phase 8 で自動作成 |

**HOKUSAI は「Notion で起票 → GitLab で実装」のフローを自動化するツール**であり、既存の Notion / GitLab Epic / Issue / MR の役割を奪うのではなく、それらの間の作業を自動でつなぐ位置付けです。

導入時は二重管理コストと Phase 8 GitLab 対応の制限事項を理解した上で、小さい Epic でのパイロット運用から始めることを推奨します。
