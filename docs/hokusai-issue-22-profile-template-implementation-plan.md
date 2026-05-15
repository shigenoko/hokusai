# Issue #22 実装計画書: profile 共有テンプレートのリポジトリ追加

## 1. 背景と目的

[Notion 議論「複数エンジニアによる開発の課題」](https://www.notion.so/35f85495565d80b1b15aefee4fe44c18) §D-2 由来。

複数エンジニアが同じ profile を共有して使うとき、各自のマシンで profile registry / config を手で作る運用は属人的になりがち。リポジトリ内に共有テンプレートをコミットし、運用ガイドで配布方法を明示する。

## 2. example と template の位置づけ整理

| ファイル | 役割 | 想定読者 |
|---|---|---|
| `configs/example-profiles.yaml` | profile registry の **サンプル**（複数案件の見本） | 新規ユーザの学習用 |
| `configs/example-profile-company.yaml` | profile config の **サンプル**（全フィールド網羅） | 新規ユーザの学習用 |
| `configs/profile-template.yaml`（新規） | profile registry の **実運用テンプレート**（プレースホルダで埋まった最小構成） | 案件チームの新メンバー |
| `configs/profile-config-template.yaml`（新規） | profile config の **実運用テンプレート**（プレースホルダ） | 案件チームの新メンバー |

`example-*` は読みやすさ重視、`*-template.yaml` は **コピー → プレースホルダ置換だけで動く** ことを重視する。

## 3. 変更内容

### 3.1 新規ファイル

#### `configs/profile-template.yaml`

profile registry（`~/.hokusai/profiles.yaml`）の実運用テンプレート。`<TODO: ...>` 形式のプレースホルダで、置換すべき箇所が grep で見つけやすい構造にする。

#### `configs/profile-config-template.yaml`

個別 profile config の実運用テンプレート。Issue #22 本文では「registry のテンプレート」のみが明示されているが、registry だけ作っても profile config が無いと `hokusai profile doctor` が通らないため、セットでコミットするのが整合的。

### 3.2 更新ファイル

- **`README.md` / `README_JP.md`**: profile セクションに「新メンバー向け展開手順」を追加
- **`docs/notion-dashboard-operation-guide.md`**: 該当箇所があれば case study として追記
- **`CHANGELOG.md`**: v0.4.7 エントリ
- **`pyproject.toml` / `hokusai/__init__.py`**: 0.4.6 → 0.4.7
- **`uv.lock`**: 同期

## 4. プレースホルダ設計

`<TODO:...>` 形式で、grep で `<TODO:` を検索すれば置換漏れが分かる。例:

```yaml
profiles:
  <TODO:profile_name>:
    label: "<TODO:human readable label>"
    config: "<TODO:absolute path to project-specific config>"
    data_dir: "~/.hokusai/profiles/<TODO:profile_name>"
    dashboard:
      port: <TODO:8765 などユニークな port>
```

## 5. 受入条件（Issue #22 由来）

- [ ] `configs/profile-template.yaml` がリポジトリに存在する
- [ ] README / 運用ガイドに展開手順が記載される
- [ ] template 内のシークレットはすべて env 変数名のみ（値は含まない）
- [ ] `hokusai profile doctor` でテンプレ展開後の検証が通る（手動確認 + テストで担保）

## 6. テスト方針

config パーサが template を読み込めることを確認するテストを追加:

- `tests/test_config_templates.py`（新規）
  - `configs/profile-template.yaml` が valid YAML である
  - `configs/profile-config-template.yaml` が valid YAML である
  - プレースホルダを置換した後、profile loader / config loader でエラー無くロードできる
  - シークレット実値（"sk-..." 形式等）が含まれていないことを正規表現で検証

## 7. バージョン

- 既存機能への影響なし（追加のみ）
- patch リリース（v0.4.6 → v0.4.7）

## 8. 関連

- Issue: #22
- 関連 Issue: #21（Operator プロパティ、別途）/ #23 / #24（引き継ぎ運用、別途）
- Notion 議論: https://www.notion.so/35f85495565d80b1b15aefee4fe44c18 §D-2
