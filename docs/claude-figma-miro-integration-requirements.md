# HOKUSAI × Figma / Miro 連携 要件書

**作成日**: 2026-05-07
**対象読者**: プロジェクト責任者・テックリード・デザイナー・ビジネス責任者
**位置付け**: Figma（デザイナー）と Miro（ビジネス）を HOKUSAI の連携対象に追加するための要件定義書

---

## 1. 背景と目的

HOKUSAI は現状、以下のツールと連携している:
- **Notion**: ビジネス × エンジニア共通の情報集約場
- **GitLab**: コード変更・MR・CI
- **Slack**: 即時通知

しかし、組織の実際の業務では以下のツールも使われている:
- **Figma**: デザイナーが UI 仕様を作成
- **Miro**: ビジネスサイドが企画段階のラフスケッチや要件マップを作成

これらが HOKUSAI と連携していないため、以下の課題がある:

- HOKUSAI が実装時に Figma の UI 仕様を参照できない
- ビジネスサイドの Miro 上の議論が実装に反映されない
- デザイナー / ビジネスサイドへの実装完了通知が手動
- 5 つのツールにまたがる情報の整合性を人間が手動で取る必要がある

本要件書では、HOKUSAI と Figma / Miro を統合し、**「ビジネス × デザイン × コードの三者を AI 実装層で繋ぐ」** 状態の実現を定義する。

## 2. ステークホルダーとツール対応

| ステークホルダー | 主に使うツール | HOKUSAI との関係 |
|---|---|---|
| ビジネスサイド（営業・マーケ・PM） | Notion + Miro | タスク起票・企画議論・進捗確認 |
| デザイナー | Figma | UI 仕様作成・コメント対応 |
| エンジニア | GitLab + IDE | コードレビュー・実装 |
| テックリード | 全ツール | 横断的な品質管理 |
| 経営層 | Notion + Slack | 進捗・成果の確認 |

## 3. ゴールと非ゴール

### 3.1. ゴール

- HOKUSAI が **Figma から UI 仕様を読み取り**、Phase 2〜5 の実装精度を上げる
- HOKUSAI が **Miro から企画段階の議論内容を読み取り**、Phase 2 の調査品質を上げる
- 実装完了・レビュー対応・リリース完了を **Figma / Miro にコメントで通知**
- デザイナー / ビジネスサイドが **HOKUSAI / GitLab を直接触らずに** 自分のツール内で完結
- Notion / GitLab / Slack の既存連携と **同じ品質基盤**（best effort 同期、outbox、冪等キー）を踏襲

### 3.2. 非ゴール

- **Miro から Figma への UI 自動移植**（Figma REST API の制約により本要件書スコープ外。§7 制約参照）
- Figma 上のデザイン本体（frame / component）を HOKUSAI が編集する機能
- Miro 上のボード本体を HOKUSAI が編集する機能
- リアルタイム協調編集
- AI による完全な UI 自動生成（HOKUSAI が「実装」を担うが、「デザイン制作」は担わない）

## 4. ツール役割の整理（5 ツール統合後）

```
┌──────────────────────────────────────────────────┐
│ ビジネスサイド: Notion（仕様） + Miro（企画議論）   │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────────────────────────────────────┐
│ デザインサイド: Figma（UI 仕様）                   │
└──────────────────┬──────────────────────────────┘
                   │
                   ↓
┌══════════════════════════════════════════════════┐
║ HOKUSAI（橋渡し + 自動執行）                       ║
║   - Notion / Miro / Figma から情報を集約           ║
║   - LLM が統合的に解釈                              ║
║   - GitLab で実装                                  ║
║   - 各ツールに進捗を書き戻し                        ║
║   - Slack で即時通知                               ║
└══════════════════┬═══════════════════════════════┘
                   │
                   ↓
┌──────────────────────────────────────────────────┐
│ エンジニアサイド: GitLab（コード・MR・CI）          │
└──────────────────────────────────────────────────┘
```

| ツール | 主な情報 | HOKUSAI 関与 |
|---|---|---|
| **Notion** | タスク仕様・要件文書・進捗一覧 | 双方向（読み書き）— タスクの起点 |
| **Miro** | ホワイトボード・企画図・付箋・ラフスケッチ | 読み中心 + コメント書き戻し |
| **Figma** | UI 仕様・デザインスペック・コンポーネント | 読み中心 + コメント書き戻し |
| **GitLab** | コード・MR・CI | 双方向（MR 作成・レビュー応答） |
| **Slack** | 即時通知 | プッシュのみ |

## 5. 機能要件

### 5.1. Figma 連携

#### 5.1.1. Figma → HOKUSAI（読み取り）

HOKUSAI が Figma から取得すべき情報:

- [ ] **File 構造**: ボード全体の frame / component の階層
- [ ] **Frame メタデータ**: 各 frame の位置・サイズ・スタイル・子要素
- [ ] **Frame 画像**: PNG / SVG エクスポート（LLM の vision 入力用）
- [ ] **Component 情報**: 共通コンポーネントの ID・命名・バリアント
- [ ] **Design Tokens / Variables**: 色・余白・タイポグラフィ
- [ ] **Styles**: 命名済みスタイル（テキストスタイル等）
- [ ] **Comments**: デザイナーからの指示・FB

#### 5.1.2. HOKUSAI → Figma（書き戻し）

コメントによる対話のみ（API 制約のため、デザイン本体は編集不可）:

- [ ] **Phase 8a（PR 作成完了時）**: 該当 frame に「実装完了。PR: <URL>」をコメント
- [ ] **Phase 9（レビュー応答時）**: 「ご指摘の修正対応済み: <commit URL>」
- [ ] **Phase 8 待機時**: 「デザインに関する判断が必要: 詳細は Notion <URL>」
- [ ] **Phase 10 完了時**: 「リリース完了」をコメント

#### 5.1.3. Webhook 受信（任意）

- [ ] **FILE_UPDATE**: デザイン変更を検知 → 進行中ワークフローに「デザイン更新あり」フラグ
- [ ] **FILE_COMMENT**: デザイナーからのフィードバック → ワークフローを Waiting for Human 化、または Slack 通知
- [ ] **LIBRARY_PUBLISH**: コンポーネントライブラリ更新 → 影響範囲調査トリガー（任意）

### 5.2. Miro 連携

#### 5.2.1. Miro → HOKUSAI（読み取り）

- [ ] **Board 構造**: フレーム階層・アイテム配置
- [ ] **付箋**: テキスト内容・カラー・位置
- [ ] **テキスト**: ラベル・説明文
- [ ] **Connectors**: アイテム間の接続関係（依存関係・フロー図）
- [ ] **Cards**: タイトル・説明・タグ
- [ ] **Frame**: 区切られたセクション内のアイテム集合
- [ ] **Board 画像 export**: 全体スナップショット（LLM の vision 入力用）
- [ ] **Comments**: ボード上のコメント

#### 5.2.2. HOKUSAI → Miro（書き戻し）

Miro はコメント投稿に加えて **App Card / Sticky Note の作成** が可能:

- [ ] **Phase 8a**: 該当フレームに「実装完了通知」のカード追加 or コメント投稿
- [ ] **Phase 9**: レビュー対応報告
- [ ] **Phase 10**: リリース完了通知

#### 5.2.3. Webhook 受信（任意）

- [ ] **Board 更新**: 企画変更を検知 → 進行中ワークフローに通知
- [ ] **Comment 追加**: ビジネスサイドからの追加要望 → Slack 通知

### 5.3. 横断的な機能要件

#### 5.3.1. Phase 別の情報取得・反映

| Phase | Notion | Miro | Figma | GitLab |
|---|---|---|---|---|
| Phase 1 準備 | タスク URL から起動、Miro / Figma URL を抽出 | — | — | Issue 取得 |
| Phase 2 調査 | 仕様読み取り | 企画ボード読み取り、付箋抽出 | UI 構造・Components 取得 | 既存コード調査 |
| Phase 3 設計 | 設計書を子ページ書き戻し | （任意）議論内容を参照 | Frame 階層・Tokens 参照 | — |
| Phase 5 実装 | 進捗更新 | — | Frame 画像・Tokens を vision 入力 | — |
| Phase 6 検証 | — | — | 実装スクショ vs Figma 差分検証 | ビルド・テスト |
| Phase 8a PR 作成 | Workflows DB 更新 | フレームにカード追加 | frame にコメント | MR 作成 |
| Phase 8 統合レビューループ | — | コメント取得・応答 | コメント取得・応答 | MR コメント応答 |
| Phase 9〜10 完了 | Status=Done | リリース通知 | リリース通知 | マージ |

#### 5.3.2. 統合 LLM プロンプト

Phase 5 の実装時、3 ツール（Notion + Miro + Figma）の情報を 1 つのプロンプトにまとめる:

```
【Notion 仕様】
ユーザー認証フローを刷新する。OAuth 対応必須。

【Miro 企画ボード】
（要件マップ画像 + 付箋抽出）
- 主要ペルソナ: 個人ユーザー / 企業ユーザー
- 競合分析の結論: SSO 必須
- ステークホルダー要望: モバイル対応最優先

【Figma UI モック】
（ログイン画面 frame 画像 + tokens）
- 色: #4F46E5 / 余白: 24px
- コンポーネント: Button, InputField, Divider
- バリアント: default / hover / disabled / loading

【既存コード】
（Phase 2 で取得したコード抜粋）

→ この情報を踏まえて Phase 5 で実装してください
```

#### 5.3.3. Operations Console 統合

HOKUSAI Web Dashboard に以下を追加:

- [ ] Figma 同期状態パネル（outbox / errors 件数、再送ボタン）
- [ ] Miro 同期状態パネル（同上）
- [ ] 両ツールのアクセス権限テスト（Notion 接続テストと同じパターン）

### 5.4. 設定要件

各連携を `WorkflowConfig` の追加 dataclass として表現:

```yaml
figma:
  enabled: true
  api_token_env: HOKUSAI_FIGMA_API_TOKEN
  default_team_id: "..."  # 任意

miro:
  enabled: true
  api_token_env: HOKUSAI_MIRO_API_TOKEN
  default_team_id: "..."  # 任意
```

API token は Notion / Slack と同じく **環境変数経由のみ**、YAML 直書きは `_detect_token_like_values` で警告。

## 6. 非機能要件

### 6.1. 信頼性

- [ ] Figma / Miro 障害時にワークフロー本体は止めない（**best effort 設計**）
- [ ] 同期失敗イベントは SQLite outbox に蓄積し、Operations Console から再送可能
- [ ] 冪等キー（`workflow_id:event_type:phase:revision`）で重複抑止
- [ ] レートリミット超過時はバックオフ + リトライで吸収

### 6.2. セキュリティ

- [ ] API token は環境変数経由でのみ扱い、YAML / Notion / ログには出さない
- [ ] `_detect_token_like_values` の検出パターンに Figma / Miro token 形式を追加
- [ ] Webhook 認証: HMAC 署名（Slack 通知中継サーバーと同じ方式）
- [ ] 各 API への書き戻し範囲を最小権限の integration で限定

### 6.3. パフォーマンス

- [ ] 1 ワークフローあたりの Figma / Miro API 呼び出し回数を抑える（キャッシュ + デバウンス）
- [ ] Figma File 構造は SQLite にキャッシュ（再取得は変更検知時のみ）
- [ ] Miro Board 画像は frame 単位 export を活用（全体 export はサイズ大）

### 6.4. 拡張性

- [ ] 既存の Notion 同期 dispatcher パターンを再利用
- [ ] 将来別ツール（Asana / Jira / etc.）追加時に同じ抽象が使えること

## 7. 制約事項（重要）

### 7.1. Figma REST API の制約

Figma の REST API は **読み取り中心** で、以下は不可:

- ❌ Frame / Shape / Component の作成
- ❌ Frame / Shape / Component の編集
- ❌ ファイル本体の生成

可能なのは:
- ✅ 読み取り（File 構造、Frame、Components、Variables、画像 export）
- ✅ コメント投稿
- ✅ Variables の作成・更新（最近の API 拡張）

**つまり HOKUSAI が「Figma に何かを描く」ことはできない**。デザイン制作はデザイナーの責務として残る。

### 7.2. Miro → Figma 自動移植の制約

組織内で「Miro のラフスケッチを Figma に自動移植したい」要望があるが、上記 7.1 の制約により **完全自動化は本要件書スコープ外**。

代替アプローチ:
- 「Miro 読み取り → LLM が構造化されたデザインブリーフを生成 → Notion に書き出し → デザイナーが Figma で正規制作」
  という **半自動フロー** が現実解
- 完全自動化が必要なら、別プロジェクトとして **Figma Plugin の自作** が必要（HOKUSAI 本体ではなく独立した拡張）

### 7.3. レートリミット

| サービス | レート上限（参考） |
|---|---|
| Figma API | プランによる（無料は制限厳しめ） |
| Miro API | 100 req/min（Free）〜（Enterprise はより高い） |

両者ともキャッシュとバッチが必須。

### 7.4. Miro / Figma 上の構造化前提

Miro は「自由なホワイトボード」のため、HOKUSAI が解釈するには **運用ルール** が必要:

- [ ] フレーム命名規則（例: `要件:`、`設計:`、`KPT:`）
- [ ] 付箋カラーの意味付け（赤=課題、緑=解決済み 等）
- [ ] HOKUSAI が読むべきセクションを明示する

これらは HOKUSAI 側ではなく、**Miro 利用ガイドラインの整備** で対応する。

## 8. ステークホルダーごとの利用シナリオ

### 8.1. PM のシナリオ

```
PM が新機能の企画を Miro でブレインストーミング
  ├─ 付箋でユーザーストーリーを並べる
  ├─ フレームでシナリオごとに整理
  └─ 結論をまとめる

→ Notion でタスクページを起票
  ├─ 概要・目的
  ├─ Miro URL を貼る
  └─ Figma URL を貼る（デザイナーが用意したもの）

→ hokusai start <Notion URL>

→ HOKUSAI が
  - Notion 仕様を読む
  - Miro 議論を読む
  - Figma UI を読む
  - 統合的に実装
  - GitLab MR 作成
  - 各ツールに完了通知

→ PM は Notion で進捗を確認、Slack で完了通知を受け取る
```

### 8.2. デザイナーのシナリオ

```
デザイナーが Figma で UI 仕様を作成
→ Notion タスクに Figma URL を貼って渡す
→ HOKUSAI が Figma の UI を読み取り実装
→ 実装完了時、Figma の該当 frame に HOKUSAI が「実装完了」コメント
→ デザイナーは Figma を開けば実装状況がわかる
→ デザイナーが「ボタンの色違う」と Figma にコメント
→ HOKUSAI が Figma コメントを取得 → 実装修正 → 応答コメント
```

### 8.3. ビジネスサイドのシナリオ

```
ビジネスサイドが Miro で企画を議論
→ Notion タスクに Miro URL を貼って起票
→ HOKUSAI が Miro 内容を実装に反映
→ 実装完了時、Miro の該当フレームに「実装完了カード」追加
→ ビジネスサイドは Miro を見るだけで実装進捗がわかる
```

## 9. 段階的実装スコープ

### Phase 1: Figma 連携 MVP（4〜6 週間）

- Figma API クライアント基盤（`hokusai/integrations/figma/`）
- API token 設定モデル + パーサ
- Phase 2 / 3 / 5 への読み取り統合（File 構造 + Frame 画像 + Tokens）
- Phase 8a での frame コメント投稿
- SQLite outbox 連携
- 単体・結合テスト
- 運用ガイド更新

**リリース判定**: Figma URL を含む Notion タスクで `hokusai start` 実行 → Phase 5 で Figma 情報が LLM プロンプトに含まれ、Phase 8a で Figma にコメントが投稿される

### Phase 2: Figma レビューループ（2〜3 週間）

- Figma コメント取得 → Phase 8 統合レビューループでの応答処理
- Webhook 中継サーバー（Slack ブリッジと同パターン）
- Slack 通知に Figma コメントへのディープリンク追加

**リリース判定**: デザイナーが Figma にコメント → HOKUSAI が修正 → Figma に応答コメントが返る

### Phase 3: Miro 連携 MVP（3〜4 週間）

- Miro API クライアント基盤（`hokusai/integrations/miro/`）
- API token 設定
- Phase 2 への読み取り統合（Board 構造 + 付箋 + 画像 export）
- Phase 8a での Miro カード投稿
- 運用ガイド整備（Miro 利用ガイドライン込み）

**リリース判定**: Miro URL を含む Notion タスクで起動 → Phase 2 の調査入力に Miro 内容が反映される

### Phase 4: Miro レビューループ（2〜3 週間）

- Miro コメント取得・応答
- Webhook 受信

### Phase 5: 視覚回帰テスト（任意・3〜4 週間）

- Phase 6 で実装スクリーンショット vs Figma frame の差分検出
- 差分が大きければ Phase 5 に戻して再実装

### Phase 6: Operations Console 拡張（1〜2 週間）

- Figma / Miro 同期状態パネル追加
- 各ツールへの接続テストボタン

### 工数まとめ

| 段階 | 内容 | 工数 |
|---|---|---|
| 1 | Figma 連携 MVP | 4〜6 週間 |
| 2 | Figma レビューループ | 2〜3 週間 |
| 3 | Miro 連携 MVP | 3〜4 週間 |
| 4 | Miro レビューループ | 2〜3 週間 |
| 5 | 視覚回帰テスト | 3〜4 週間 |
| 6 | Operations Console 拡張 | 1〜2 週間 |

**最小ライン**: 段階 1 + 3 で「読み取り中心の連携」が成立（合計 7〜10 週間）

**完全版**: 全段階で 15〜22 週間（並行作業可能なので実質 12〜16 週間）

## 10. 受け入れ基準（Definition of Done）

### 10.1. 全体 DoD

- [ ] Notion タスクに Figma / Miro URL を貼れば、自動的に HOKUSAI が読み取って実装に活用する
- [ ] 実装完了時、Figma / Miro の該当箇所にコメントが自動投稿される
- [ ] デザイナー / ビジネスサイドが HOKUSAI / GitLab を直接触らずに、自分のツールから完結できる
- [ ] Notion / GitLab / Slack の既存連携と同じ品質基盤（best effort、outbox、冪等）
- [ ] 各 API token は環境変数経由のみで扱われ、YAML 直書き検出が動作
- [ ] Operations Console から各連携の状態確認・再送ができる
- [ ] 全 Phase の単体・結合テストが追加されている
- [ ] 運用ガイドが整備され、Miro / Figma の運用ルールが明示されている

### 10.2. 段階別 DoD

各段階の完了条件は §9 段階的実装スコープ参照。

## 11. オープンクエスチョン（着手前合意項目）

実装着手前に明示的に確定させる項目:

1. **Figma の API token 発行主体**
   - 暫定案: 組織管理者が HOKUSAI 専用 integration を作成、Personal Access Token を発行

2. **Miro の API token 発行主体**
   - 暫定案: 同上

3. **Figma の対象範囲**
   - チーム全体 / 特定プロジェクトのみ / ファイルごとに ID 列挙
   - 暫定案: ファイルごとに Notion タスクから URL を渡す方式（最小スコープ）

4. **Miro の運用ルール策定の責任**
   - 誰がフレーム命名規則・付箋カラーの意味を定義するか
   - 暫定案: ビジネスサイドのテックリードが運用ルールを定義し、HOKUSAI 側で読み取りパターンを実装

5. **Miro → Figma 自動移植の扱い**
   - 本要件書では非ゴール。完全自動化が必要になった場合は別プロジェクト（Figma Plugin 開発）として切り出す
   - 暫定案: 半自動フロー（Miro 読み取り → 構造化ブリーフ → Figma 手動制作）でリリース

6. **Webhook 中継サーバーのホスティング**
   - 開発時はローカル、本番では?
   - 暫定案: Slack ブリッジと同じインフラに相乗り

7. **視覚回帰テスト（段階 5）の優先度**
   - 段階 1〜4 完了後に必要性を評価
   - 暫定案: 段階 5 はオプショナル、必要性が確認されたら実装

各項目の暫定案で進めて差し支えなければ、レビュアからの no-objection をもって着手する。

## 12. リスクと対策

| リスク | 対策 |
|---|---|
| Figma / Miro API 障害で同期が止まる | best effort 設計、SQLite outbox に蓄積、Operations Console から再送 |
| API レートリミット超過 | キャッシュ + バッチ + デバウンス |
| Miro が構造化されておらず LLM が誤解釈 | 運用ルール（フレーム命名・付箋カラー）の整備で吸収 |
| Figma / Miro の API token 漏洩 | 環境変数経由のみ、`_detect_token_like_values` で検出 |
| デザイナーが Figma 編集を期待する | 非ゴールとして明示、HOKUSAI はコメントのみ書き戻すと運用ガイドで周知 |
| 5 ツールの情報が不整合 | 各ツールの責任範囲を明示（Notion = 仕様、Miro = 議論、Figma = UI、GitLab = 実装） |

## 13. 関連ドキュメント

| ドキュメント | 関係 |
|---|---|
| `docs/codex-hokusai-notion-gitlab-operation-policy.md` | Notion / GitLab の役割分担方針（前提） |
| `docs/hokusai-notion-dashboard-implementation-plan.md` | Notion 同期の実装計画（連携基盤の参考） |
| `docs/codex-slack-notification-implementation-plan.md` | Slack 通知の実装計画（連携基盤の参考） |
| `docs/notion-dashboard-operation-guide.md` | Notion 運用ガイド（同様の運用ガイドを Figma / Miro 向けに整備予定） |

## 14. まとめ

| 項目 | 内容 |
|---|---|
| Figma の役割 | デザインサイドとの橋渡し（読み中心 + コメント書き戻し） |
| Miro の役割 | ビジネスサイド議論との橋渡し（読み中心 + コメント / カード書き戻し） |
| 連携の非対称性 | HOKUSAI は Figma / Miro の本体を編集しない（人間の聖域） |
| 統合的価値 | ビジネス × デザイン × コードの三者を AI 実装層で繋ぐ |
| 基盤 | Notion / GitLab / Slack 連携と同じパターン（best effort、outbox、冪等） |
| 工数（最小ライン） | 7〜10 週間（Figma + Miro の MVP） |
| 工数（完全版） | 12〜16 週間（並行作業前提） |

レビュアからの no-objection を得たうえで、**段階 1（Figma 連携 MVP）から着手**することを推奨する。
