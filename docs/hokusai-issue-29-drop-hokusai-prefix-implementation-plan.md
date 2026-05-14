# Issue #29 実装計画書: scaffold / DB から HOKUSAI prefix を削除 + サブページ日本語化

## 1. 背景と目的

親ページ（`--parent-page-id` で指定）が `HOKUSAI Project` 等の HOKUSAI 文脈で配置されることが想定されており、配下のリソースが冗長な `HOKUSAI` prefix を持つ必要が薄い。また、サブページ名は日本語運用に合わせて日本語化したい。

## 2. 命名変更

| 種別 | v0.4.4 | v0.4.5 で変更後 | icon |
|---|---|---|---|
| DB | `HOKUSAI Workflows DB` | `Workflows DB` | — |
| DB | `HOKUSAI Pull Requests DB` | `Pull Requests DB` | — |
| ハブページ | `HOKUSAI Documentation` | `Documentation` | 📚 |
| サブページ | `Discussions` | `議論` | 💬 |
| サブページ | `Operation Guides` | `運用ガイド` | 📖 |
| サブページ | `Requirements` | `要件定義` | 📋 |

## 3. 後方互換

### 3.1 ハブ legacy_aliases（v0.4.3 + v0.4.4 の 2 世代）

```python
_DOCUMENTATION_HUB_TITLE = "Documentation"
_DOCUMENTATION_HUB_LEGACY_TITLES: tuple[str, ...] = (
    "HOKUSAI Documentation",      # v0.4.4
    "📚 HOKUSAI Documentation",   # v0.4.3
)
```

### 3.2 サブページ legacy_aliases（各 2 世代）

```python
_DOCUMENTATION_CHILDREN: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("議論",       "💬", "...", ("Discussions",      "💬 Discussions")),
    ("運用ガイド", "📖", "...", ("Operation Guides", "📖 Operation Guides")),
    ("要件定義",   "📋", "...", ("Requirements",     "📋 Requirements")),
]
```

`_find_existing_child_page` は canonical 優先のため、新名と旧名が共存する場合は新名が選ばれる。

### 3.3 DB

DB は title による既存検出を行わない（毎回新規作成、非冪等）ため legacy alias 不要。env var が DB ID を保持するため Notion 側の DB 名変更は影響なし。

## 4. 変更ファイル

- `hokusai/integrations/notion_dashboard/setup.py`
  - 定数: `WORKFLOWS_DB_TITLE`, `PULL_REQUESTS_DB_TITLE`, `_DOCUMENTATION_HUB_TITLE`, `_DOCUMENTATION_HUB_LEGACY_TITLES`, `_DOCUMENTATION_CHILDREN`
  - DB 説明文（`_WORKFLOWS_DB_DESCRIPTION` / `_PULL_REQUESTS_DB_DESCRIPTION`）の HOKUSAI 自己参照は文脈上残す（「HOKUSAI が自動管理する DB」など）
  - docstring の参照タイトルを更新
- `tests/test_notion_setup.py`
  - 既存テストの assert を新タイトルに更新
  - 2 世代 legacy 検出テストを追加（v0.4.4 旧名のみ存在、v0.4.3 旧名のみ存在、両方存在）
- `README.md` / `README_JP.md`
  - ツリー図表記と説明を新名に更新、v0.4.5 案内を追加
- `docs/notion-dashboard-operation-guide.md`
  - ツリー図と各ページの役割テーブルを更新
- `docs/hokusai-issue-25-notion-setup-scaffold-implementation-plan.md`
  - 補足: v0.4.5 で日本語化された旨を追記
- `CHANGELOG.md`: v0.4.5 エントリ
- `pyproject.toml` / `hokusai/__init__.py`: 0.4.4 → 0.4.5

## 5. テスト方針

- 既存テストの assert を新タイトルに置換
- 新規追加:
  - `test_scaffold_detects_v0_4_4_legacy_hub`: hub が `HOKUSAI Documentation` で既存 → skip
  - `test_scaffold_detects_v0_4_3_legacy_hub`: hub が `📚 HOKUSAI Documentation` で既存 → skip
  - `test_scaffold_detects_v0_4_4_legacy_subpage`: サブが `Discussions` 等で既存 → skip
  - `test_scaffold_prefers_canonical_over_two_generations_of_legacy`: 親に 3 世代共存（議論 / Discussions / 💬 Discussions）→ canonical `議論` を優先

## 6. リスクと対応

| リスク | 対応 |
|---|---|
| 既存ユーザの v0.4.3 / v0.4.4 ページが再実行で重複作成 | legacy_aliases で 2 世代分検出 |
| DB 名が generic 化して workspace 内の他 DB と紛れる | DB 説明文の警告で「HOKUSAI が自動管理」を明示、親ページが `HOKUSAI Project` 等で識別性確保 |
| サブページ名の日本語化で英語環境ユーザに違和感 | profile 設定で命名カスタマイズ可能化は v0.5.x で別途検討（本 PR では行わない） |

## 7. バージョン

- `pyproject.toml`: 0.4.4 → 0.4.5
- `hokusai/__init__.py`: 0.4.4 → 0.4.5
- `uv.lock` も sync
