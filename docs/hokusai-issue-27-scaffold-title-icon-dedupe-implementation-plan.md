# Issue #27 実装計画書: scaffold ページのタイトル絵文字を icon と二重化解消

## 1. 背景と目的

PR #26 / v0.4.3 で `hokusai notion-setup --scaffold` を実装した結果、Notion UI で **タイトル文字列の絵文字と page icon の絵文字が二重に表示** されることが判明した。

| 場所 | 現在 | 表示例 |
|---|---|---|
| Title | `📚 HOKUSAI Documentation` | 📚 📚 HOKUSAI Documentation |
| Icon | 📚 | （title 左に） |

Notion の慣用に従い、**icon に絵文字、title は素のテキスト** に統一する。

## 2. 後方互換の方針（B1）

旧タイトル（絵文字 prefix 付き）で既に作成済みのページがある環境向けに、idempotency 検出を「新タイトル」「旧タイトル」両方で行う。

- 旧タイトル `📚 HOKUSAI Documentation` のページがあれば skip 検出する
- 自動リネームはしない（破壊的変更を避ける）
- 二重表示を解消したいユーザは Notion 側で手動リネーム

## 3. 変更内容

### 3.1 定数

`hokusai/integrations/notion_dashboard/setup.py`:

```python
# 旧
_DOCUMENTATION_HUB_TITLE = "📚 HOKUSAI Documentation"

_DOCUMENTATION_CHILDREN: list[tuple[str, str, str]] = [
    ("💬 Discussions", "💬", "..."),
    ("📖 Operation Guides", "📖", "..."),
    ("📋 Requirements", "📋", "..."),
]

# 新
_DOCUMENTATION_HUB_TITLE = "HOKUSAI Documentation"
_DOCUMENTATION_HUB_LEGACY_TITLES: tuple[str, ...] = (
    "📚 HOKUSAI Documentation",  # v0.4.3 で作成されたページ
)

_DOCUMENTATION_CHILDREN: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("Discussions",      "💬", "...", ("💬 Discussions",)),
    ("Operation Guides", "📖", "...", ("📖 Operation Guides",)),
    ("Requirements",     "📋", "...", ("📋 Requirements",)),
]
```

### 3.2 `_find_existing_child_page` 拡張

```python
def _find_existing_child_page(
    api_client: NotionAPIClient,
    parent_page_id: str,
    title: str,
    *,
    legacy_aliases: tuple[str, ...] = (),
) -> str | None:
    """親ページの子ブロック一覧から、title または legacy_aliases に一致する
    child_page の id を探す。

    canonical title 完全一致を最優先で返し、見つからなければ legacy_aliases
    の最初の一致を返す。新旧両方のページが親に共存する場合は canonical を
    選び、サブが legacy hub 配下に作られて重複ツリーになるのを防ぐ。
    """
    legacy_set = set(legacy_aliases)
    legacy_match_id: str | None = None
    cursor: str | None = None
    while True:
        try:
            blocks = api_client.list_block_children(parent_page_id, start_cursor=cursor)
        except Exception as e:
            raise NotionSetupError(...) from e
        for block in blocks.get("results", []):
            if block.get("type") != "child_page":
                continue
            block_title = block.get("child_page", {}).get("title")
            # canonical 完全一致は即返し
            if block_title == title:
                return block.get("id")
            # legacy は最初のヒットを覚えるが走査継続（canonical 優先）
            if legacy_match_id is None and block_title in legacy_set:
                legacy_match_id = block.get("id")
        if not blocks.get("has_more"):
            return legacy_match_id
        cursor = blocks.get("next_cursor")
        if not cursor:
            return legacy_match_id
```

### 3.3 `_resolve_hub_page` / `_create_or_skip_subpage`

`_find_existing_child_page` 呼び出しに legacy_aliases を渡すよう更新。`_DOCUMENTATION_CHILDREN` は 4-tuple に拡張。

## 4. テスト追加

`tests/test_notion_setup.py`:

1. **新タイトルでの基本動作確認**: 既存ページが空のとき、新タイトル `HOKUSAI Documentation` 等で作成される
2. **legacy alias 検出**: 親ページに旧タイトル `📚 HOKUSAI Documentation` のページがある場合、scaffold は skip 扱いになり重複作成しない
3. **混在パターン**: ハブが旧タイトル、サブの一部が新タイトル / 残りが未作成 → それぞれ正しく skip / create
4. **canonical 優先**: 親に新旧両ハブが共存するとき canonical hub が選ばれサブもその下に作られる（重複ツリー回避）

## 5. ドキュメント更新

- `README.md` / `README_JP.md`: ツリー図の表記を新タイトルに更新（icon 表記を明示）
- `docs/notion-dashboard-operation-guide.md`: 同上
- `docs/hokusai-issue-25-notion-setup-scaffold-implementation-plan.md`: 補足セクションで Issue #27 で変更されたことを脚注
- `CHANGELOG.md`: v0.4.4 エントリ追加

## 6. バージョン

- `pyproject.toml`: 0.4.3 → 0.4.4
- `hokusai/__init__.py`: 0.4.3 → 0.4.4

## 7. リスクと対応

| リスク | 対応 |
|---|---|
| 旧タイトル既存ページが skip された場合、UI 二重表示は解消されない | CLI / docs で「title から絵文字を削るリネームを推奨」と案内 |
| サブページの parent id が新旧で異なる場合 | `_resolve_hub_page` が返す hub_id を使うため発生しない |
| legacy_aliases に意図せず一致するページがあった場合 | 絵文字 prefix を含むタイトルは HOKUSAI の予約名と判断、誤検知は実質ゼロ |

## 8. 受入確認

- [ ] 全テスト pass（lint クリーン含む）
- [ ] CHANGELOG / version 整合
- [ ] PR レビュー（Copilot）対応収束

## 9. 関連

- Issue: #27
- 前提 PR: #26（v0.4.3 で scaffold 実装）
- Issue #25（scaffold 機能の元 issue）
