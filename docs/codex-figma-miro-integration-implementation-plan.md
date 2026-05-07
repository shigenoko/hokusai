# HOKUSAI Figma / Miro 連携実装計画書

**作成日**: 2026-05-08
**対象読者**: プロジェクト責任者、PM、テックリード、実装担当エンジニア
**前提要件書**: `docs/codex-figma-miro-integration-requirements.md`

## 1. 目的

HOKUSAI に Figma / Miro 連携を追加し、Notion、Miro、Figma、GitLab を横断した開発ワークフローを実現する。

本実装では、Miro をビジネス側の業務フロー・ラフスケッチの入力元、Figma を UI / UX デザインの正本、Notion を要件・進捗共有の正本、GitLab を実装・レビューの正本として扱う。

HOKUSAI は各ツールを置き換えず、Notion タスクを起点に Miro / Figma の情報を読み取り、調査・設計・実装・レビュー・MR へ反映する。

## 2. ゴールと非ゴール

### 2.1. ゴール

- Notion タスクに Miro URL / Figma URL を指定できる
- HOKUSAI が Miro / Figma URL を検出し、概要情報を取得できる
- Phase 2 / Phase 3 の調査・設計チェックに Miro / Figma 情報を反映できる
- Phase 5 の実装プロンプトに Figma の画面仕様を参照情報として渡せる
- GitLab MR に Notion / Miro / Figma の関連リンクを自動記載できる
- Notion Dashboard に Miro / Figma の連携状態、同期時刻、確認状態を表示できる
- Miro / Figma 連携が無効、未設定、取得失敗の場合でも既存ワークフローを壊さない

### 2.2. 非ゴール

- Figma 上で完成デザインを自動生成する
- Miro の手描きスケッチを完全な UI デザインへ自動変換する
- Figma と実装画面のピクセル完全一致判定を行う
- Figma / Miro への双方向コメント同期を実装する
- Figma Variables から production design token を自動更新する
- Notion から Miro / Figma を直接編集する

## 3. 実装方針サマリ

MVP では「読み取り中心」「best effort」「既存ワークフロー非破壊」を基本方針とする。

| 領域 | 方針 |
|---|---|
| Miro | API / MCP で取得できる範囲のボード概要、frame、付箋、テキスト、図形情報を取得する |
| Figma | REST API で file / node / image / comments の概要を取得する |
| Notion | URL と同期状態を Workflows DB に追加し、詳細は Phase 子ページに保存する |
| GitLab | MR description に Notion / Miro / Figma リンクとデザイン確認状態を追記する |
| HOKUSAI Runtime | Phase 2 / 3 / 5 / 8 / 10 へ段階的に接続する |
| 失敗時 | 外部 API 取得失敗は警告扱いにし、必要な場合だけ Waiting for Human にする |

## 4. 全体アーキテクチャ

```text
Notion Task
  ├─ Miro URL
  ├─ Figma URL
  └─ GitLab Issue URL
       ↓
HOKUSAI Runtime
  ├─ External Design Context Resolver
  │   ├─ MiroClient
  │   └─ FigmaClient
  ├─ Phase 2 Research
  ├─ Phase 3 Design Check
  ├─ Phase 5 Implement
  ├─ Phase 8 MR / Review Loop
  └─ Phase 10 Record
       ↓
Outputs
  ├─ Notion Phase subpages
  ├─ Notion Workflows DB
  ├─ GitLab MR description
  └─ Slack / Waiting for Human notification
```

## 5. データモデル

### 5.1. WorkflowState 追加候補

`hokusai/state.py` に、外部デザイン情報を保存するフィールドを追加する。

| フィールド | 型 | 内容 |
|---|---|---|
| `miro_url` | `Optional[str]` | Notion タスクから抽出した Miro URL |
| `figma_url` | `Optional[str]` | Notion タスクから抽出した Figma URL |
| `miro_context` | `Optional[dict]` | Miro から取得・要約した情報 |
| `figma_context` | `Optional[dict]` | Figma から取得・要約した情報 |
| `design_integration_status` | `Optional[str]` | `not_configured` / `synced` / `partial` / `failed` |
| `design_review_required` | `bool` | デザイン確認が必要か |
| `design_review_result` | `Optional[str]` | `pending` / `approved` / `changes_requested` |
| `design_sync_errors` | `list[str]` | Miro / Figma 同期時の警告・エラー |

### 5.2. 共通コンテキスト形式

Figma / Miro の生データを直接プロンプトに渡さず、HOKUSAI 内部で短い共通形式に正規化する。

```python
{
    "source": "figma" | "miro",
    "url": "...",
    "title": "...",
    "updated_at": "...",
    "summary": "...",
    "screens": [
        {
            "name": "...",
            "node_id": "...",
            "description": "...",
            "texts": ["..."],
            "components": ["..."],
            "notes": ["..."],
        }
    ],
    "comments": [
        {
            "author": "...",
            "body": "...",
            "resolved": false,
        }
    ],
    "warnings": ["..."],
}
```

この形式にすることで、Phase 2 / 3 / 5 のプロンプト差し込みとテストを安定させる。

## 6. 設定設計

### 6.1. YAML 設定

`WorkflowConfig` に `figma` / `miro` 設定を追加する。

```yaml
figma:
  enabled: true
  api_token_env: HOKUSAI_FIGMA_API_TOKEN
  sync_comments: true
  export_images: true
  timeout: 10
  on_failure: warn  # warn | block | skip

miro:
  enabled: true
  api_token_env: HOKUSAI_MIRO_API_TOKEN
  use_mcp: false
  timeout: 10
  on_failure: warn  # warn | block | skip
```

### 6.2. 追加する config dataclass

`hokusai/config/models.py` に以下を追加する。

- `FigmaIntegrationConfig`
- `MiroIntegrationConfig`
- `DesignIntegrationConfig` または `WorkflowConfig.figma` / `WorkflowConfig.miro`

初期実装では `WorkflowConfig.figma` / `WorkflowConfig.miro` として直接持たせる方が単純である。

### 6.3. 設定パーサ

`hokusai/config/loaders.py` に以下を追加する。

- `_parse_figma_config(config_dict)`
- `_parse_miro_config(config_dict)`

`hokusai/config/manager.py` では `figma` / `miro` を parse し、不要キーの pop 対象にも追加する。

## 7. URL 抽出

### 7.1. 抽出対象

Notion タスク本文と Notion Dashboard DB のプロパティから以下を抽出する。

- `https://www.figma.com/file/...`
- `https://www.figma.com/design/...`
- `https://www.figma.com/proto/...`
- `https://miro.com/app/board/...`

### 7.2. 実装場所

新規モジュールを追加する。

- `hokusai/integrations/design/url_parser.py`

責務:

- Figma URL から `file_key` / `node_id` を抽出する
- Miro URL から `board_id` を抽出する
- 複数 URL がある場合は優先順位を決める
- 抽出できない場合は警告として返す

## 8. Figma 連携実装

### 8.1. 追加ファイル

- `hokusai/integrations/design/figma.py`
- `tests/integrations/test_figma_client.py`
- `tests/integrations/test_design_url_parser.py`

### 8.2. FigmaClient の責務

MVP では read-only とする。

- API token の存在確認
- file 情報の取得
- node 情報の取得
- 画像 export URL の取得
- コメント取得
- 取得結果の共通コンテキスト化
- API エラー、権限エラー、rate limit の警告化

### 8.3. MVP で取得する情報

- file 名
- frame / node 名
- node id
- 最終更新日時
- テキスト
- 主要レイアウト情報
- component / instance 名
- コメント件数、未解決コメント
- 画像 export URL

### 8.4. 実装上の注意

- Figma API の生レスポンスをそのまま state に保存しない
- プロンプトに渡す情報は要約済みの `figma_context` に限定する
- token はログ、Notion、GitLab、Slack に出さない
- 画像取得は失敗しても text / node 情報の取得ができれば `partial` とする

## 9. Miro 連携実装

### 9.1. 追加ファイル

- `hokusai/integrations/design/miro.py`
- `tests/integrations/test_miro_client.py`

### 9.2. MiroClient の責務

MVP では read-only とする。

- API token または MCP 利用可否の確認
- board 情報の取得
- item 一覧の取得
- frame / sticky note / text / shape / connector の抽出
- 業務フローとラフ画面構成の要約
- 取得結果の共通コンテキスト化
- API / MCP エラーの警告化

### 9.3. MVP で取得する情報

- board 名
- frame 名
- sticky note のテキスト
- text item
- shape 名またはテキスト
- connector の関係
- コメントまたは補足メモ
- 最終更新日時

### 9.4. MCP と API の扱い

初期実装では API クライアントを標準経路とする。MCP は将来の自然言語操作やボード生成に向いているため、設定として `use_mcp` を残すが、MVP では必須にしない。

## 10. Design Context Resolver

### 10.1. 追加ファイル

- `hokusai/integrations/design/context.py`
- `tests/integrations/test_design_context.py`

### 10.2. 責務

`DesignContextResolver` を追加し、Phase ノードから個別に Figma / Miro API を直接呼ばない構造にする。

責務:

- state / task content から Miro / Figma URL を抽出する
- 設定が無効なら `not_configured` とする
- FigmaClient / MiroClient を呼び出す
- 取得結果を `miro_context` / `figma_context` に格納する
- 失敗時の挙動を `on_failure` に従って決める
- プロンプト差し込み用 Markdown を生成する

### 10.3. 失敗時ポリシー

| `on_failure` | 挙動 |
|---|---|
| `warn` | 警告を state に記録し、ワークフローは続行 |
| `block` | Waiting for Human にして停止 |
| `skip` | 取得をスキップし、既存ワークフロー通り続行 |

MVP のデフォルトは `warn` とする。

## 11. Phase 別実装計画

### Phase 2: Research

対象ファイル:

- `hokusai/nodes/phase2_research.py`
- `prompts/phase2/task_research.md`
- `prompts/phase2/task_research_retry.md`

実装内容:

- Phase 2 開始前に `DesignContextResolver` を呼ぶ
- Notion タスク内の Miro / Figma URL を抽出する
- Miro / Figma の概要を取得する
- 調査プロンプトに `## 外部デザイン・業務フロー情報` セクションを追加する
- 調査結果に Miro / Figma の参照情報と不足情報を含める

完了条件:

- Miro URL があるタスクで、Phase 2 出力に業務フロー概要が含まれる
- Figma URL があるタスクで、Phase 2 出力に対象画面概要が含まれる
- 取得失敗しても Phase 2 が既存通り完了できる

### Phase 3: Design Check

対象ファイル:

- `hokusai/nodes/phase3_design.py`
- `prompts/phase3/design_check.md`
- `prompts/phase3/design_check_retry.md`

実装内容:

- `_build_design_check_prompt()` に design context セクションを渡せるようにする
- Notion 要件、Miro 業務フロー、Figma UI 仕様の突き合わせ観点を追加する
- ズレ検知結果を Phase 3 出力に含める
- `on_failure=block` または重大な不整合時に Waiting for Human へ遷移できるようにする

完了条件:

- Figma / Miro ありのタスクで Phase 3 に整合性チェックが出力される
- 不足 URL、未解決コメント、古い更新日時が警告として出力される

### Phase 4: Plan

対象ファイル:

- `hokusai/nodes/phase4_plan.py`
- `prompts/phase4/dev_plan*.md`

実装内容:

- 実装計画に参照すべき Miro / Figma URL を含める
- デザインレビューが必要なタイミングを明記する
- 実装上 Figma と差分が出る可能性がある場合の記録ルールを追加する

完了条件:

- Phase 4 出力に Miro / Figma 参照対象が明記される

### Phase 5: Implement

対象ファイル:

- `hokusai/nodes/phase5_implement.py`
- `prompts/phase5/implementation.md`
- `prompts/phase5/retry_fix.md`

実装内容:

- 実装プロンプトに Figma の画面構成、テキスト、コンポーネント、スタイル概要を渡す
- Miro の業務フローを仕様補足として渡す
- デザインとの差分が発生した場合は実装結果に記録するよう指示する

完了条件:

- 実装プロンプトに design context が含まれる
- design context がない場合でも既存プロンプトと互換性がある

### Phase 7: Review

対象ファイル:

- `hokusai/nodes/phase7_review.py`
- `hokusai/review_checklist.md`
- `prompts/phase7/final_review.md`

実装内容:

- UI / UX レビューに Figma 参照観点を追加する
- Figma URL がある場合、実装が対象画面に沿っているか確認する
- 未解決デザインコメントがある場合は warning または NG にできるようにする

完了条件:

- Figma URL ありのタスクで UX レビュー観点に Figma 確認が含まれる

### Phase 8: MR / Review Loop

対象ファイル:

- `hokusai/nodes/phase8/*`
- `hokusai/integrations/git_hosting/github.py`
- `hokusai/integrations/git_hosting/gitlab.py`

実装内容:

- MR description に Notion / Miro / Figma リンクを追加する
- デザインレビューが必要な場合は MR description に明記する
- 未解決 Figma コメントがある場合は Waiting for Human の理由に含める
- GitLab / GitHub の両方で既存 MR 作成ロジックを壊さない

完了条件:

- GitLab MR 本文に Miro / Figma リンクが入る
- リンクなしのタスクでは既存の MR 本文と同等に動作する

### Phase 10: Record

対象ファイル:

- `hokusai/nodes/phase10_record.py`
- `hokusai/utils/notion_helpers.py`

実装内容:

- 最終記録に Miro / Figma / GitLab の対応関係を含める
- デザイン差分、代替判断、未解決事項を Notion に記録する

完了条件:

- 完了時に Notion に Miro / Figma 関連情報が残る

## 12. Notion Dashboard 実装

### 12.1. Workflows DB プロパティ追加

対象ファイル:

- `hokusai/integrations/notion_dashboard/setup.py`
- `hokusai/integrations/notion_dashboard/workflows_db.py`
- `docs/notion-dashboard-operation-guide.md`
- `docs/notion-dashboard-verification-checklist.md`

追加プロパティ:

| プロパティ | 型 | 更新主体 |
|---|---|---|
| Miro URL | url | 人間 / HOKUSAI |
| Figma URL | url | 人間 / HOKUSAI |
| Design Status | select | HOKUSAI |
| Design Review Required | checkbox | HOKUSAI |
| Design Review Result | select | 人間 / HOKUSAI |
| Miro Last Synced At | date | HOKUSAI |
| Figma Last Synced At | date | HOKUSAI |
| Miro Summary Page | url | HOKUSAI |
| Figma Summary Page | url | HOKUSAI |
| Design Notes | rich_text | HOKUSAI |

### 12.2. 同期 payload 追加

`workflow.py` の `_build_notion_payload()` に以下を追加する。

- `miro_url`
- `figma_url`
- `design_integration_status`
- `design_review_required`
- `design_review_result`
- `design_sync_errors`
- `miro_last_synced_at`
- `figma_last_synced_at`

### 12.3. 表示方針

Notion DB にはサマリだけを書く。長い Miro / Figma 要約は Phase 2 / 3 子ページ、または専用の summary 子ページに保存する。

## 13. 接続状態と CLI

### 13.1. Connection Status

対象ファイル:

- `hokusai/integrations/connection_status.py`
- `hokusai/cli/commands/connect.py`
- `scripts/dashboard.py`

実装内容:

- `figma` / `miro` を connection registry に追加する
- 環境変数の有無、API 到達性、権限エラーを判定する
- `hokusai connect --status` に Figma / Miro を表示する
- Operations Console の接続状態ページに Figma / Miro を表示する

### 13.2. `hokusai connect`

MVP では token 入力 UI は作らない。CLI では以下を案内する。

- Figma: `HOKUSAI_FIGMA_API_TOKEN` の設定
- Miro: `HOKUSAI_MIRO_API_TOKEN` の設定、または MCP 設定

シークレットは YAML に保存しない。

## 14. Slack 通知

対象ファイル:

- `hokusai/integrations/notifications/slack.py`

MVP では既存イベントに design context を追加する。

- `waiting_for_human`: デザイン確認待ち、未解決コメント、URL 不足を通知
- `pr_created`: MR と一緒に Miro / Figma リンクを通知
- `workflow_failed`: Miro / Figma 同期失敗が原因の場合に要約を含める

新イベント追加は MVP では必須としない。

## 15. テスト計画

### 15.1. Unit Test

追加・更新するテスト:

- URL parser
- FigmaClient の正常系 / 認証なし / 404 / rate limit
- MiroClient の正常系 / 認証なし / 404 / rate limit
- DesignContextResolver の `warn` / `block` / `skip`
- config parser
- Notion payload 変換
- MR description 生成

### 15.2. Integration Test

- Miro / Figma 連携無効時に既存ワークフローが壊れない
- Notion タスクに Figma URL だけある場合
- Notion タスクに Miro URL だけある場合
- Notion タスクに Miro / Figma 両方ある場合
- API 取得失敗時に warning として続行する
- `on_failure=block` で Waiting for Human になる

### 15.3. Contract Test

外部 API に依存しないよう、Figma / Miro の代表レスポンス fixture を用意する。

追加候補:

- `tests/fixtures/figma_file.json`
- `tests/fixtures/figma_node.json`
- `tests/fixtures/figma_comments.json`
- `tests/fixtures/miro_board.json`
- `tests/fixtures/miro_items.json`

### 15.4. Manual Verification

手動検証は以下の最小ケースで行う。

1. Notion タスクに Miro URL と Figma URL を貼る
2. `hokusai start <Notion Task URL>` を実行する
3. Phase 2 / 3 出力に Miro / Figma 情報が含まれることを確認する
4. MR description に Miro / Figma リンクが含まれることを確認する
5. Notion Dashboard に Design Status と Last Synced At が反映されることを確認する

## 16. 実装フェーズ

### Phase A: 土台

作業:

- config dataclass / parser を追加
- URL parser を追加
- DesignContextResolver の空実装を追加
- connection status に Figma / Miro を追加
- example config を更新

完了条件:

- 設定読み込みテストが通る
- URL 抽出テストが通る
- Figma / Miro 無効時に既存テストが壊れない

### Phase B: Read-only クライアント

作業:

- FigmaClient を追加
- MiroClient を追加
- fixture ベースの単体テストを追加
- DesignContextResolver で両クライアントを呼ぶ

完了条件:

- API レスポンスを共通コンテキストへ正規化できる
- 認証なし、権限なし、取得失敗を警告化できる

### Phase C: Workflow 注入

作業:

- Phase 2 / 3 / 5 に design context を注入
- Phase 4 / 7 のプロンプトに参照観点を追加
- `on_failure=block` の Waiting for Human を実装

完了条件:

- Phase 2 / 3 出力に Miro / Figma 情報が反映される
- design context なしでも既存出力が壊れない

### Phase D: Notion / GitLab 表示

作業:

- Notion Workflows DB プロパティを追加
- `_build_notion_payload()` を拡張
- MR description に Miro / Figma リンクを追加
- Slack 通知に design context サマリを追加

完了条件:

- Notion Dashboard で連携状態を確認できる
- GitLab MR で Miro / Figma への導線を確認できる

### Phase E: 運用ドキュメントと検証

作業:

- Notion Dashboard 運用ガイドを更新
- verification checklist を更新
- サンプル設定を更新
- 手動検証を実施

完了条件:

- PM / デザイナー / エンジニア向けの使い方が説明できる
- MVP 完了条件を満たす

## 17. 影響範囲

### 17.1. 主な変更ファイル

- `hokusai/config/models.py`
- `hokusai/config/loaders.py`
- `hokusai/config/manager.py`
- `hokusai/state.py`
- `hokusai/workflow.py`
- `hokusai/nodes/phase2_research.py`
- `hokusai/nodes/phase3_design.py`
- `hokusai/nodes/phase4_plan.py`
- `hokusai/nodes/phase5_implement.py`
- `hokusai/nodes/phase7_review.py`
- `hokusai/nodes/phase8/*`
- `hokusai/nodes/phase10_record.py`
- `hokusai/integrations/connection_status.py`
- `hokusai/integrations/notion_dashboard/setup.py`
- `hokusai/integrations/notion_dashboard/workflows_db.py`
- `hokusai/integrations/notifications/slack.py`
- `configs/example-gitlab.yaml`

### 17.2. 新規ファイル

- `hokusai/integrations/design/__init__.py`
- `hokusai/integrations/design/url_parser.py`
- `hokusai/integrations/design/context.py`
- `hokusai/integrations/design/figma.py`
- `hokusai/integrations/design/miro.py`
- `tests/integrations/test_design_url_parser.py`
- `tests/integrations/test_design_context.py`
- `tests/integrations/test_figma_client.py`
- `tests/integrations/test_miro_client.py`

## 18. リスクと対策

| リスク | 対策 |
|---|---|
| 外部 API の rate limit で workflow が不安定になる | 取得は best effort、timeout、retry、必要最小限の取得にする |
| Figma / Miro の生データが大きすぎてプロンプトが肥大化する | 共通コンテキストへ要約し、長文は Notion 子ページへ分離する |
| デザイン情報を過信して誤実装する | Phase 3 で不明点を明示し、重大な不整合は Waiting for Human にする |
| シークレットがログや Notion に漏れる | token は環境変数参照のみ、ログ出力時にマスクする |
| Notion DB スキーマ変更で既存導入先が壊れる | 新規プロパティは後方互換で追加し、未設定時はスキップする |
| Miro のラフスケッチ解釈が不正確 | MVP では下書き生成を対象外にし、読み取りと要約に限定する |
| Figma 画面と実装の完全一致を期待される | MVP の説明で「参照・支援・ズレ検知」であり完全自動変換ではないことを明示する |

## 19. MVP 完了条件

- `figma.enabled=false` / `miro.enabled=false` で既存ワークフローが変わらず動く
- Notion タスクから Figma / Miro URL を抽出できる
- Figma / Miro の概要情報を共通コンテキストに変換できる
- Phase 2 / 3 / 5 のプロンプトに design context を注入できる
- GitLab MR に Notion / Miro / Figma のリンクが入る
- Notion Dashboard に Miro / Figma 連携状態が表示される
- API 取得失敗時に warning として継続できる
- `on_failure=block` で Waiting for Human にできる
- 主要ユニットテストと統合テストが追加されている

## 20. 導入手順

1. Figma / Miro の利用権限と API token 発行可否を確認する
2. HOKUSAI 実行環境に `HOKUSAI_FIGMA_API_TOKEN` / `HOKUSAI_MIRO_API_TOKEN` を設定する
3. 対象 config で `figma.enabled` / `miro.enabled` を有効化する
4. Notion Dashboard スキーマを更新する
5. Notion タスクテンプレートに Miro URL / Figma URL 欄を追加する
6. サンプルタスクで Phase 2 / 3 / 5 / 8 を検証する
7. PM、デザイナー、エンジニア向けに運用ルールを共有する

## 21. 将来拡張

MVP 完了後、以下を検討する。

- Miro のラフスケッチから Figma ワイヤーフレーム下書きを生成する
- Figma Plugin 経由で frame / rectangle / text を作成する
- Figma コメントへの返信と GitLab MR との紐づけ
- Figma / Miro Webhook による更新検知
- デザイン更新後の MR stale 判定
- Figma Variables / design token 連携
- Playwright screenshot と Figma export image の visual diff
- Notion 上の Design Status を PM 向けビューに整理する

