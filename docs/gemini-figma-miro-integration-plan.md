# HOKUSAI: Figma / Miro 連携実装計画書

## 1. 概要
本ドキュメントは「HOKUSAI: Figma / Miro 連携要件定義書」に基づき、具体的な技術実装ステップ、開発優先順位、および検証方針を定義する。

## 2. 実装フェーズ
開発効率とリスク管理のため、以下の3フェーズで段階的に実装を進める。

### フェーズ1：基礎インテグレーションの構築
FigmaおよびMiroから情報を「読み取る」ための基盤を整備する。
- [ ] **Figma Client 実装**: `hokusai/integrations/figma.py`
    - Figma REST API (GET file / GET file nodes) のラッパー作成。
    - デザインスペック（スタイル、ノードツリー）の抽出ロジック。
- [ ] **Miro Client 実装**: `hokusai/integrations/miro.py`
    - Miro API / MCP クライアントの実装。
    - ボード上の全アイテムとコネクタ情報の取得ロジック。
- [ ] **設定モデル拡張**: `hokusai/config/models.py`
    - `FigmaConfig`, `MiroConfig` の追加（APIトークン、チームID等）。

### フェーズ2：ワークフローへの統合（Phase 2 / 3 / 5）
既存の10フェーズ開発ワークフローにデザイン・企画情報を組み込む。
- [ ] **Phase 2 (Research) ノード更新**:
    - タスク（Notion/GitLab）からURLを検知し、Miro/Figmaの情報を自動取得・コンテキストへ保存。
- [ ] **Phase 3 (Design) プロンプト更新**:
    - 取得したデザイン情報を踏まえ、アーキテクチャ設計を行うようプロンプト（`prompts/phase3/design_check.md`）を調整。
- [ ] **Phase 5 (Implement) 連携**:
    - デザインスペックを Claude Code 等の実装エージェントに「デザイン制約」として引き渡す仕組みの追加。

### フェーズ3：高度な変換機能とPR連携（Phase 8 / 10）
Miroからの自動変換機能と、最終的な成果物へのフィードバック。
- [ ] **Miro ➔ Figma 変換エンジン実装**:
    - Miroの座標系とアイテム種別を、Figmaのコンポーネント配置指示へ変換するロジック。
- [ ] **Phase 8 (PR Draft) 拡張**:
    - MR作成時にFigmaのプレビュー画像を自動埋め込みする機能の追加。
- [ ] **Phase 10 (Record) 拡張**:
    - 最終的な実装結果をMiro/Figmaのコメントとして書き戻す処理。

## 3. 詳細実装仕様

### 3.1. デザインスペックの抽出（Figma）
Figma APIから得られる巨大なJSONから、以下の優先度で情報を抽出し、LLMが理解しやすい「スペックシート（Markdown/JSON）」に変換する。
1. **Typography & Colors**: 文書化されたドキュメントスタイル。
2. **Auto Layout**: `padding`, `gap`, `flex-direction` の数値。
3. **Component Properties**: インスタンスに渡されているProps値。

### 3.2. Miro からの意図解釈（Prompt Engineering）
Miroの「付箋と図形の集合」をLLMに渡し、以下のプロンプト戦略で解釈させる。
- 「付箋のクラスタリングから、機能要件を抽出せよ」
- 「図形と矢印の繋がりから、ユーザーの遷移フローをMarkdownテーブルで出力せよ」

## 4. 検証・テスト方針

### 4.1. 単体テスト
- `tests/integrations/test_figma.py`: APIレスポンスのパース処理の検証（Mockを使用）。
- `tests/integrations/test_miro.py`: Miroデータからの要件抽出ロジックの検証。

### 4.2. 結合テスト
- 実際のFigma URLを含むNotionタスクを用意し、Phase 2〜5までを通して実行し、生成されるコードにデザインスペックが反映されているかを確認する。

## 5. スケジュール（目安）
- **1週目**: フェーズ1（インテグレーション基盤）
- **2週目**: フェーズ2（ワークフローノード連携・プロンプト調整）
- **3週目**: フェーズ3（Miro➔Figma変換、PR連携強化）

## 6. リスクと対策
- **APIレートリミット**: Figma APIの取得頻度を最適化し、キャッシュ機構を導入する。
- **デザインの複雑性**: 初回リリースでは「特定のデザインシステム（例：MDS）」を利用したプロジェクトに限定し、汎用的な変換は順次強化する。
