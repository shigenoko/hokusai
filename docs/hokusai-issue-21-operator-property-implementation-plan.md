# Issue #21 実装計画書: Workflows DB に Operator プロパティを追加（部分実装）

## 1. 背景と目的

Notion 議論「複数エンジニアによる開発の課題」§D-1 由来。

複数エンジニアが同じ profile を共有する場合、現状の Workflows DB レコードは「誰が `hokusai start` を叩いたか」を識別する情報がない。GitHub PR の author に頼る暗黙的な追跡しかできず、引き継ぎ時のコンタクト先や監査ログに不足がある。

Issue #22（profile-template、v0.4.7 merged）で複数エンジニア共有運用の基盤が整ったため、Operator プロパティ追加で「誰が動かしたか」の即時可視化を実現する。

## 2. スコープ（部分実装）

Issue #21 本文では 3 DB（Workflows / Work Items / Review Issues）への Operator 追加が期待されているが、後 2 者は **v0.5.x の Human Governance Workgraph 本実装で新規作成される計画機能**。本 PR は v0.4.x patch リリースとして **Workflows DB のみ** を対象とする。

Work Items DB / Review Issues DB への Operator は Workgraph 本実装時に同じパターンで追加する（要件定義書 §3.1 / §4 / §5 のスキーマ案に組み込む）。

## 3. 変更内容

### 3.1 新規ファイル

- `hokusai/integrations/notion_dashboard/operator.py`: Operator 名解決ユーティリティ
  - `resolve_operator_name() -> str`
  - 優先順位: `HOKUSAI_OPERATOR_NAME` 環境変数 → `whoami` コマンド → `"(unknown)"`
- `tests/test_operator.py`: 解決ロジック単体テスト
- `docs/hokusai-issue-21-operator-property-implementation-plan.md`: 本実装計画書

### 3.2 変更ファイル

#### `hokusai/integrations/notion_dashboard/setup.py`
- `_WORKFLOWS_DB_PROPERTIES` に `"Operator": {"rich_text": {}}` を追加
- DB 説明文（`_WORKFLOWS_DB_DESCRIPTION`）に `Operator` を「HOKUSAI が書き込むプロパティ」一覧に追加

#### `hokusai/integrations/notion_dashboard/client.py`
- `update_database(database_id, payload)` メソッドを追加（PATCH `/v1/databases/{id}`）。既存 DB へのプロパティ追加 migration で使用

#### `hokusai/integrations/notion_dashboard/workflows_db.py`
- `_build_properties` に `operator` payload key → `Operator` rich_text property のマッピングを追加

#### `hokusai/workflow.py`
- `WorkflowRunner.start()` の `_safe_notion_dispatch("workflow_started", ...)` 呼び出しで payload に `operator=resolve_operator_name()` を含める
- Notion 同期が未設定（`notion_dispatcher.is_configured() == False`）の場合は operator 解決自体を skip（whoami の余計な遅延を回避）

#### `hokusai/cli_main.py`
- 新規サブコマンド `hokusai notion-migrate-schema`（既存 Workflows DB に Operator プロパティを idempotent に追加）

#### docs / version
- `docs/notion-dashboard-operation-guide.md`: Operator プロパティの説明追加、migration 手順
- `CHANGELOG.md`: v0.4.8 エントリ
- `pyproject.toml` / `hokusai/__init__.py`: 0.4.7 → 0.4.8
- `uv.lock`: 同期

## 4. 動作仕様

### 4.1 Operator 値の解決

```python
def resolve_operator_name() -> str:
    # 1. HOKUSAI_OPERATOR_NAME env が空でない場合はそれを使う
    # 2. whoami コマンドの出力（3 秒 timeout）
    # 3. いずれも失敗したら "(unknown)"
```

### 4.2 書き込みタイミング

- `workflow_started` event で 1 回のみ送信
- 以降の phase_changed / pr_created 等の event では送信しない（Notion 側の値を温存）
- 既存ワークフローの再開（continue）でも上書きしない

### 4.3 既存 DB への対応

- 新規 DB（`hokusai notion-setup` 実行時）: schema に Operator 含まれて作成される
- 既存 DB（v0.4.7 以前で作成済み）:
  - 送信時に property_not_found エラーが返るが、`_submit_with_property_pruning` で該当プロパティを除いて再試行する既存ロジックでフォールバック
  - migration コマンド `hokusai notion-migrate-schema --workflows-db-id <id>` を提供し、`update_database` API で property を追加できる

### 4.4 後方互換

- 既存レコードは破壊しない（Operator プロパティが空のまま残る）
- Workflows DB の Operator プロパティが無い環境では、新規 workflow_started でも sync は成功し、Operator のみ書き込まれない（Notion API の property_not_found pruning）

## 5. テスト方針

- `tests/test_operator.py`（新規）:
  - env 設定時にその値が返る
  - env 未設定 + whoami 成功時に whoami の出力が返る
  - env 未設定 + whoami 失敗時に `(unknown)` が返る
  - env が空白のみの場合は env 値を採用せず whoami / fallback に進む

- `tests/test_notion_setup.py`:
  - `_WORKFLOWS_DB_PROPERTIES` に `Operator` が含まれることを確認

- `tests/test_workflows_db.py`（または既存テストを拡張）:
  - payload に `operator` が含まれる場合、`Operator` property が rich_text として生成される
  - payload に `operator` が含まれない場合、`Operator` property は出力されない

- `tests/test_notion_dashboard.py` または新規:
  - `update_database` メソッドが期待 URL に PATCH を投げる

## 6. 受入条件（Issue #21）

- [ ] Workflows DB schema に `Operator` プロパティが追加される
- [ ] `hokusai start` で workflow を起動した実行者の名前が記録される
- [ ] 既存レコードは破壊しない（migration コマンドを用意）
- [ ] `hokusai notion-setup` の DB 自動作成にも反映される

## 7. バージョン

- patch リリース（v0.4.7 → v0.4.8）
- 既存挙動に対する破壊的変更なし

## 8. 関連

- Issue: #21
- 前提 Issue / PR: #22 (profile template、v0.4.7 merged)、#23 / #24 (handover 運用、PR #34 merged)
- 後続 Issue: Work Items DB / Review Issues DB への Operator 追加（v0.5.x Workgraph 本実装と合流）
- Notion 議論: https://www.notion.so/35f85495565d80b1b15aefee4fe44c18 §D-1
- 要件定義書: `docs/hokusai-human-governance-workgraph-requirements.md` §3.2
