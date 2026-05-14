# Issue #25 実装計画: notion-setup に --scaffold オプションを追加

**Issue**: [#25 hokusai notion-setup に --scaffold オプションを追加し、ドキュメントツリーを自動作成](https://github.com/shigenoko/hokusai/issues/25)
**作成日**: 2026-05-14
**対象バージョン**: v0.4.3（patch リリース想定）

---

## 1. 背景

v0.4.2 までの `hokusai notion-setup` は親ページ直下に `HOKUSAI Workflows` / `HOKUSAI Pull Requests` の 2 DB だけを作成する。複数のドキュメント（議論記録 / 運用ガイド / 要件定義書）を Notion で管理し始めると、トップに人間のドキュメントと HOKUSAI 自動 DB が混在し、視覚的・運用的にばらつく。

`Notion 議論ページ「複数エンジニアによる開発の課題」` で「📚 ドキュメント」「💬 Discussions」のようなツリー構造を手で切り出した経緯があり、これを `notion-setup` で再現可能にしたい。

## 2. ゴール

### 2.1 やること

- `hokusai notion-setup` に `--scaffold` オプションを追加（オプトイン）
- 指定時に親ページ配下に標準ドキュメントツリーを作成（v0.4.4 で title 形式更新）:
  - HOKUSAI Documentation（ハブ、icon 📚）
    - Discussions（icon 💬）
    - Operation Guides（icon 📖）
    - Requirements（icon 📋）
- 各ページに placeholder 文を入れる（意図と運用ルールの説明）
- Idempotent（既存同名ページは skip、v0.4.3 旧タイトル legacy_aliases 検出含む）

### 2.2 やらないこと

- profile config 駆動のカスタマイズ（Phase 2 / v0.5.x で扱う）
- Notion view（推奨ビュー）の自動作成（Phase 3）
- Project Memory DB / Work Items DB の自動作成（Phase 3）
- 既存ページの上書き / 移動

## 3. 設計

### 3.1 標準ツリー構造

```
<親ページ>
├── HOKUSAI Workflows (DB)            ← 既存
├── HOKUSAI Pull Requests (DB)        ← 既存
└── HOKUSAI Documentation             ← 新規 (icon 📚)
    ├── Discussions                   ← 新規 (icon 💬)
    ├── Operation Guides              ← 新規 (icon 📖)
    └── Requirements                  ← 新規 (icon 📋)
```

> v0.4.3 では title に絵文字 prefix（`📚 HOKUSAI Documentation` 等）を付けて
> いたが、Notion UI で page icon と二重表示される問題のため v0.4.4 (Issue #27)
> で title 文字列から絵文字を外し icon 側のみで表現するよう変更。後方互換で
> v0.4.3 のページも検出される。

### 3.2 ページ仕様

| ページ | icon | 役割 | placeholder 文の要旨 |
|---|---|---|---|
| HOKUSAI Documentation | 📚 | 人間が書くドキュメントのハブ | 「HOKUSAI の Notion governance layer 上で人間が管理するドキュメント。HOKUSAI が自動同期する DB（Workflows / Pull Requests）とは別の領域。」 |
| Discussions | 💬 | 議論・設計判断 | 「コード変更を伴う前段の検討・要件議論。決定したら関連 GitHub Issue を本文に追加。」 |
| Operation Guides | 📖 | 運用手順 | 「日常運用の手順書（profile 切り替え、token 更新、復旧手順など）。」 |
| Requirements | 📋 | 要件定義書 | 「リポジトリの docs/*.md（要件定義書類）の Notion 版または GitHub へのリンク。」 |

### 3.3 オプトイン フラグ

```bash
hokusai --profile hokusai notion-setup \
  --parent-page-id <ID> \
  --scaffold \
  --persist
```

CLI フラグ:
- `--scaffold`: ドキュメントツリーを scaffold する（default: False）

### 3.4 Idempotent 戦略

ページ作成前に親ページの children をクエリし、同名（title 完全一致）のページが既にあれば skip:

```python
def _find_existing_child_page(api_client, parent_page_id: str, title: str) -> str | None:
    """親ページの子から同名 page block を検索（pagination 全走査、idempotency 失敗時 raise）。

    Notion API は 1 レスポンス最大 100 件のため has_more を見て全ページ走査する。
    途中で API エラーが発生したら NotionSetupError を投げる（fail-open で重複ページを
    作るのを避ける）。
    """
    cursor = None
    while True:
        try:
            blocks = api_client.list_block_children(parent_page_id, start_cursor=cursor)
        except Exception as e:
            raise NotionSetupError(
                f"親ページの子要素取得に失敗（idempotent チェック不能）: {e}"
            ) from e
        for block in blocks.get("results", []):
            if block.get("type") == "child_page" \
               and block.get("child_page", {}).get("title") == title:
                return block["id"]
        if not blocks.get("has_more"):
            return None
        cursor = blocks.get("next_cursor")
        if not cursor:
            return None
```

「同名のページが既に存在しています、skip しました」というメッセージを出力。

### 3.5 ページ作成 API

NotionAPIClient に新メソッドを追加:

```python
def create_page(self, payload: dict) -> dict:
    """既存メソッド（parent + properties + optional children）"""
    return self._request("POST", "/pages", body=payload)
```

これは既に存在（[client.py:79](hokusai/integrations/notion_dashboard/client.py:79)）。child_page を作成するには:

```python
# Notion Create Page API: page_id parent では properties.title は
# rich-text array 直接（DB 行用の {"title": {"title": [...]}} 形式は使えない）
payload = {
    "parent": {"page_id": parent_id},
    "icon": {"type": "emoji", "emoji": "📚"},
    "properties": {
        "title": [{"type": "text", "text": {"content": "HOKUSAI Documentation"}}]
    },
    "children": [
        # placeholder paragraph
        {"type": "paragraph", "paragraph": {"rich_text": [...]}}
    ]
}
```

### 3.6 scaffold_notion_workspace 関数

`hokusai/integrations/notion_dashboard/setup.py` に追加:

```python
def scaffold_notion_workspace(
    api_token: str,
    parent_page_id: str,
    *,
    api_client: NotionAPIClient | None = None,
) -> dict[str, Any]:
    """親ページ配下に標準ドキュメントツリーを作成（idempotent）。

    実装は入力検証以外で raise しない。実行時 API エラーは結果 dict 内に
    partial state として記録する（呼び出し側が復旧手順を判断できるように）。

    Returns:
        {
            "created": [{"title": str, "id": str}, ...],
            "skipped": [{"title": str, "id": str}, ...],
            "failed":  [{"title": str, "error": str}, ...],   # 個別サブページの失敗
            # ハブ作成失敗 / 子要素取得失敗（idempotent チェック不能）等の
            # 致命的失敗時のみ含まれる:
            "error":   "ExceptionType: message",
        }
    """
```

`setup_notion_workspace` から呼び出し時のオプション引数として組み込み。

## 4. 実装ステップ

### Step 1: setup.py 拡張

**対象ファイル**: `hokusai/integrations/notion_dashboard/setup.py`

- `_DOCUMENTATION_TREE` 定数（標準ツリー定義）
- `_find_existing_child_page` ヘルパ
- `_create_documentation_page` ヘルパ（icon + placeholder 込み）
- `scaffold_notion_workspace` 関数
- `setup_notion_workspace` に `scaffold: bool = False` 引数追加

### Step 2: cli_main.py 拡張

**対象ファイル**: `hokusai/cli_main.py`

- `notion_setup_parser` に `--scaffold` フラグ追加
- `_handle_notion_setup` で flag を読み、`setup_notion_workspace(..., scaffold=...)` に渡す
- scaffold 結果（created / skipped）を成功メッセージに含める

### Step 3: テスト追加

**対象ファイル**: `tests/test_notion_setup.py` (既存) / 新規

- `scaffold_notion_workspace` 単体: tree 作成、idempotent
- `setup_notion_workspace(scaffold=True)`: DB + ツリー両方
- CLI handler: `--scaffold` フラグの解釈
- 既存挙動の互換性: `--scaffold` 未指定なら DB のみ

### Step 4: README / docs / CHANGELOG 更新

**対象ファイル**:
- `README.md` / `README_JP.md`: `notion-setup` セクションに `--scaffold` の使い方を追記
- `docs/notion-dashboard-operation-guide.md`: 標準ツリーの説明と用途
- `CHANGELOG.md`: `## [0.4.3]` エントリ

### Step 5: 統合確認

- ローカル実行で動作確認（HOKUSAI workspace でテスト不可なので mock 確認）
- 全 tests pass / lint pass

## 5. ファイル別変更概要

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `hokusai/integrations/notion_dashboard/setup.py` | scaffold ロジック | +120 行 |
| `hokusai/integrations/notion_dashboard/__init__.py` | export 追加 | +5 行 |
| `hokusai/cli_main.py` | `--scaffold` フラグと handler 拡張 | +30 行 |
| `tests/test_notion_setup.py` | scaffold ケース追加 | +120 行 |
| `README.md` / `README_JP.md` | 使い方追記 | +15 行 × 2 |
| `docs/notion-dashboard-operation-guide.md` | 標準ツリー説明 | +30 行 |
| `CHANGELOG.md` | v0.4.3 エントリ | +20 行 |

合計: コード 〜155 行 / テスト 〜120 行 / docs 〜80 行

## 6. 後方互換性

| ケース | 動作 |
|---|---|
| 既存ユーザーが `--scaffold` なしで実行 | 従来通り DB 2 つだけ作成（変更なし） |
| `--scaffold` 指定で既存ページなし | 標準ツリー作成 |
| `--scaffold` 指定で既存ページあり（同名） | skip して既存を尊重 |
| `--scaffold` + `--persist` 併用 | DB 作成 → ツリー作成 → rc 書き込み |

破壊的変更なし。

## 7. リスクと対策

| リスク | 対策 |
|---|---|
| Notion API rate limit（ページ作成 4 つ追加）| 既存と同じ `_send` の rate limit 機構を流用、特別対応不要 |
| 中途半端な失敗状態（DB は作成、ツリー途中で失敗）| 各ページ作成は独立。失敗時は partial result を返してログに残す |
| icon emoji が Notion 上で表示されない | Notion API 仕様上、emoji icon は標準サポート |
| ユーザーが既存ページを手で削除した後の再実行 | idempotent 検出により skip されないため、削除済みなら再作成される（期待動作） |
| 親ページに HOKUSAI integration が接続されていない | 既存の DB 作成と同じエラーパス（NotionAPIError → CLI で原因表示） |

## 8. 受け入れ基準（Issue #25 と対応）

- [ ] `hokusai notion-setup --parent-page-id <ID> --scaffold` で標準ツリーが作成される
- [ ] `--scaffold` 未指定なら従来通り DB 作成のみ
- [ ] 既存に同名ページがあれば skip（再実行で重複作成しない）
- [ ] 標準ツリーのページ名 / icon / placeholder が一貫している
- [ ] テスト追加（DB 作成のみ / scaffold あり / 再実行で skip）
- [ ] README / 運用ガイドに使用例を追加
- [ ] CHANGELOG に v0.4.3 エントリ

## 9. ロールアウト

- patch リリース（v0.4.3）として扱う
- 既存ユーザーへの影響なし（オプトイン）
- v0.5.x で profile config 駆動のカスタマイズに拡張（別 Issue）

## 10. 関連ドキュメント

- Issue: https://github.com/shigenoko/hokusai/issues/25
- Notion 議論ページ（配置構成検討）: workspace 内で管理（外部公開しない private discussion ページ）
- 前提となる PR: #18（profile-aware notion-setup, v0.4.1）/ #20（識別パネル, v0.4.2）
- 関連 Issue: #21〜#24（governance workgraph 関連実装タスク）
