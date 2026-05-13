# Issue #17 実装計画: notion-setup の profile-aware 化

**Issue**: [#17 notion-setup が --profile 指定時に profile config の env 名を自動採用しない](https://github.com/shigenoko/hokusai/issues/17)
**作成日**: 2026-05-14
**対象バージョン**: v0.4.1（patch リリース想定）

---

## 1. 背景

profile 機能（v0.3.0）で複数の Notion ワークスペースを案件単位で使い分ける運用において、`hokusai notion-setup` が profile config の env 変数名を尊重しない問題がある。

### 1.1 現状の動作（v0.4.0 時点、コード根拠つき）

以下は本 PR で修正対象となる **v0.4.0 時点** の挙動。本 PR 適用後はこれらが変更される点に注意（修正後の状態は §3 / §6 参照）。

| 箇所（v0.4.0） | 現状 |
|---|---|
| `cli_main.py:245-249` | `--api-token-env` の default が `HOKUSAI_NOTION_API_TOKEN` でハードコード |
| `cli_main.py:339` | `notion-setup` は config を読まないパスを通る |
| `setup.py:301-307` | `persist_env_vars` が rc に書き込む env 名（`HOKUSAI_NOTION_WORKFLOWS_DB_ID` / `HOKUSAI_NOTION_PR_DB_ID`）がハードコード |
| `setup.py:317-324` | PERSIST マーカーが固定文字列で、profile 別ブロックを扱えない |

### 1.2 結果として起こる問題

- `--profile <name>` を付けて実行しても、profile config の `notion_dashboard.api_token_env` は使われない
- ユーザーは `--api-token-env <env>` を毎回明示する必要がある
- `--persist` 後に rc に書かれた env 名を profile config と一致させるため**手動 rename**が必要
- 既存 `HOKUSAI_NOTION_API_TOKEN` が別案件用に設定されている場合、誤って別ワークスペースに DB を作成してしまうリスク

---

## 2. ゴール

### 2.1 やること

- `--profile <name>` 指定時、profile config の env 名を自動採用
- `--persist` で rc に書き込む env 名も profile config に合わせる
- 複数 profile を同じ rc に並べて書ける（マーカーを profile-aware 化）
- README / 運用ガイドに手順を追記

### 2.2 やらないこと

- `--profile` 未指定時の既存挙動の変更（後方互換維持）
- profile config が存在しない単発実行ユーザーに対する強制（既定 `HOKUSAI_NOTION_API_TOKEN` フォールバック維持）
- 既存 rc ファイル内のレガシーブロック（マーカーなし）の自動 migration

---

## 3. 設計

### 3.1 env 名解決の優先順位

```
1. --api-token-env で明示指定された値
2. --profile <name> が指定されており、profile config に notion_dashboard.api_token_env がある場合 → その値
3. 既定値: "HOKUSAI_NOTION_API_TOKEN"
```

同じ優先順位を `workflows_db_id_env` / `pull_requests_db_id_env` にも適用する。

### 3.2 cli_main.py の修正方針

#### 3.2.1 argparse default の扱い

`--api-token-env` の default を `None` に変更し、`_handle_notion_setup` 内で解決ロジックを実行する。CLI ヘルプ文には「省略時は profile config もしくは `HOKUSAI_NOTION_API_TOKEN`」と記載。

#### 3.2.2 `notion-setup` のための config 読み込みパス

現状 `notion-setup` は `cli_main.py:337-339` で config を読まずに早期分岐している。この理由は「初期セットアップ時に config が無くても動かしたい」ため。

修正後:
- `--profile <name>` が明示指定されている → config を読みに行く（既存 profile registry / config loader を経由）
- profile 未指定 → 従来通り config を読まずに動かす（早期分岐維持）

#### 3.2.3 config 読み込み失敗時のエラー方針（実装版）

検討初期は「致命的にせず既定値で続行」とする案だったが、PR レビューで指摘されたとおり、`HOKUSAI_NOTION_API_TOKEN` が別案件用に設定されていると意図しない Notion ワークスペースへセットアップが走るリスクがある。最終実装は以下:

- **profile 解決自体の失敗（ProfileError 系）** — 中断（`sys.exit(1)`）。原因別に
  `ConflictingProfileAndConfigError` / `ProfileNotFoundError` /
  `ProfileRegistryNotFoundError` / `InvalidProfileNameError` を個別に catch して
  原因に応じたメッセージとヒントを出す。
- **profile 解決は成功したが config 読み込みで失敗** — 原則中断。例外として
  `--api-token-env` が明示指定されている場合のみ警告して続行（ユーザーが token
  env を明示選択しているため誤注入リスクは限定的）。

これにより「sysem を黙って既定 env で続行して別ワークスペースを誤操作する」事故を防ぐ。

### 3.3 persist_env_vars のシグネチャ拡張

```python
def persist_env_vars(
    rc_path: Path | str,
    ids: dict[str, str],
    *,
    workflows_env_name: str = "HOKUSAI_NOTION_WORKFLOWS_DB_ID",
    pull_requests_env_name: str = "HOKUSAI_NOTION_PR_DB_ID",
    profile_name: str | None = None,
    backup: bool = True,
) -> dict[str, Any]:
```

- `workflows_env_name` / `pull_requests_env_name` を引数化（既定値で後方互換）
- `profile_name` を任意で渡せるようにし、マーカーに含めて profile 別ブロックを区別可能に

### 3.4 PERSIST マーカーの profile-aware 化

#### 3.4.1 マーカー仕様（実装版）

実装した最終仕様は既存の `# === ... ===` 形式を踏襲して以下のとおり。

```
# === HOKUSAI Notion Dashboard (managed by `hokusai notion-setup`) ===
...
# === END HOKUSAI Notion Dashboard ===

# === HOKUSAI Notion Dashboard (managed by `hokusai notion-setup`, profile=hokusai) ===
...
# === END HOKUSAI Notion Dashboard (profile=hokusai) ===
```

- profile 名を含めることで、複数 profile の env を同じ rc ファイル内に並べて持てる
- 既存ユーザー向けの「profile 名なしマーカー」と profile マーカーは**別マーカー**として共存させる（legacy ブロックを `profile=default` で置換しない）

> 検討初期は `# >>> ... >>>` / `# <<< ... <<<` 形式 + 「legacy を `profile=default` で置換」を案としていたが、既存ブロックを破壊しないために legacy / profile を独立マーカーとし、書式も既存の `# === ... ===` を踏襲する実装に最終化した。

#### 3.4.2 マーカー検出ロジック（実装版）

```python
def _build_profile_markers(profile_name: str) -> tuple[str, str]:
    return (
        f"# === HOKUSAI Notion Dashboard "
        f"(managed by `hokusai notion-setup`, profile={profile_name}) ===",
        f"# === END HOKUSAI Notion Dashboard (profile={profile_name}) ===",
    )
```

`profile_name=None` の呼び出しでは従来の `PERSIST_BEGIN_MARKER` / `PERSIST_END_MARKER` 定数を使う。

#### 3.4.3 既存マーカーとの後方互換

`PERSIST_BEGIN_MARKER` / `PERSIST_END_MARKER` 定数はそのまま保持。
- profile 名指定なしの呼び出し（既存ユーザー）→ 従来マーカーを使うので既存ブロックは置換される
- profile 名指定ありの呼び出し → profile 別マーカーを使うので既存 legacy ブロックは**残ったまま共存**する

### 3.5 設定モデルの拡張不要

`NotionDashboardConfig` には既に `api_token_env` / `workflows_db_id_env` / `pull_requests_db_id_env` が存在するため、モデル変更は不要。

---

## 4. 実装ステップ

### Step 1: persist_env_vars のシグネチャ拡張

**対象ファイル**: `hokusai/integrations/notion_dashboard/setup.py`

- `persist_env_vars` に `workflows_env_name` / `pull_requests_env_name` / `profile_name` 引数を追加
- block_lines 構築時に引数の env 名を使う
- マーカーは `profile_name` から動的生成、`profile_name is None` の場合は既存定数を使う

**テスト追加**:
- `tests/test_notion_setup.py` に新規ケース
  - profile 名なしで呼ぶと既存マーカー + 既定 env 名
  - profile 名 + カスタム env 名で呼ぶと profile マーカー + カスタム env 名
  - 同じ rc に 2 つの profile の env を並べて persist できる

### Step 2: cli_main.py の env 名解決ロジック追加

**対象ファイル**: `hokusai/cli_main.py`

- `--api-token-env` の default を `None` に変更、help 文も更新
- `notion-setup` の早期分岐ロジックを修正:
  - `--profile` 指定あり → config 解決を試みる（失敗時は既定値で続行）
  - `--profile` 指定なし → 従来通り config を読まない
- `_handle_notion_setup` 内で env 名解決ロジックを追加
- `persist_env_vars` 呼び出し時に解決済み env 名と profile_name を渡す

**テスト追加**:
- `tests/test_cli_notion_setup.py`（新規 or 既存）に
  - profile 未指定 → 既定値 `HOKUSAI_NOTION_API_TOKEN` を読む
  - profile 指定 + config に api_token_env あり → config の env 名を読む
  - profile 指定 + `--api-token-env` 明示 → 明示値が勝つ
  - profile 指定だが config に api_token_env 未定義 → 既定値にフォールバック

### Step 3: README / 運用ガイド更新

**対象ファイル**:
- `README.md` / `README_JP.md`
- `docs/notion-dashboard-operation-guide.md`

**記述追加**:
- profile 別に Notion ワークスペースを使い分けるユースケース例
- `hokusai --profile <name> notion-setup --parent-page-id <ID> --persist` の動作（profile config の env 名を採用）
- 既存マーカーから profile マーカーへ移行する手順（手動 rename で OK）

### Step 4: 動作検証

ローカル実行で以下を確認:

1. 既存挙動の維持（profile 未指定実行）
2. profile 指定での setup → rc に profile マーカーで追記される
3. 別 profile での setup → 同じ rc に並んで追記される（既存ブロックは置換されない）
4. `hokusai profile doctor --deep` で各 profile の Notion 接続が成功する

---

## 5. ファイル別変更概要

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `hokusai/integrations/notion_dashboard/setup.py` | `persist_env_vars` シグネチャ拡張、マーカー動的生成 | +30 / -10 行程度 |
| `hokusai/cli_main.py` | `--api-token-env` default を `None` 化、`_handle_notion_setup` 拡張 | +40 / -5 行程度 |
| `tests/test_notion_setup.py` | profile-aware ケース追加 | +60 行程度 |
| `tests/test_cli_notion_setup.py`（新規 or 拡張） | 解決ロジックのケース追加 | +80 行程度 |
| `README.md` / `README_JP.md` | profile 運用例の追記 | +20 行 × 2 |
| `docs/notion-dashboard-operation-guide.md` | profile 別運用セクション追加 | +40 行程度 |
| `CHANGELOG.md` | v0.4.1 エントリ追加 | +10 行 |

合計: コード変更 〜100 行、テスト 〜140 行、ドキュメント 〜90 行

---

## 6. 後方互換性

| ケース | 動作 |
|---|---|
| 既存ユーザーが `hokusai notion-setup` を `--profile` なしで実行 | 既定値 `HOKUSAI_NOTION_API_TOKEN` 使用、既存マーカーで rc 書き込み |
| 既存ユーザーが `--api-token-env CUSTOM_TOKEN` を指定して実行 | 明示値が優先（変わらず） |
| 既存 rc ファイルに古いマーカー（profile 名なし）あり | profile 未指定実行時は既存マーカーを置換、profile 指定実行時は新規マーカーブロックを追記 |

破壊的変更なし。

---

## 7. リスクと対策

| リスク | 対策 |
|---|---|
| profile config 読み込み失敗で notion-setup が動かなくなる | config 解決を best-effort にし、失敗時は既定値にフォールバック + warning |
| 既存マーカーと新マーカーが共存して rc が肥大化 | 検出時に warning を出し、移行手順を docs に記載 |
| profile 名に rc syntax 的に問題のある文字（空白等）が混入 | profile 名の正規化（既存 `load_profile_registry` の検証ロジックを流用） |

---

## 8. 受け入れ基準（Issue #17 と対応）

- [ ] `hokusai --profile <name> notion-setup --parent-page-id <ID>` で、profile config の `notion_dashboard.api_token_env` が自動採用される
- [ ] `hokusai --profile <name> notion-setup --parent-page-id <ID> --persist` で rc に追記される env 名が profile config と一致する
- [ ] `--api-token-env` を明示指定した場合はそちらが優先される（既存挙動の互換）
- [ ] profile を指定しない場合は従来通り `HOKUSAI_NOTION_API_TOKEN` / `HOKUSAI_NOTION_WORKFLOWS_DB_ID` / `HOKUSAI_NOTION_PR_DB_ID` を使う
- [ ] README / docs に手順を反映

追加の受け入れ基準:

- [ ] 同じ rc に複数 profile の env ブロックを並べて持てる
- [ ] 既存ユーザーの rc ファイル（profile 名なしマーカー）が破壊されない
- [ ] テストカバレッジ: profile-aware のすべての解決パスがユニットテストで網羅される

---

## 9. ロールアウト

- マイナーリリースでなく **patch リリース（v0.4.1）** として扱う（後方互換 + 機能小規模のため）
- CHANGELOG `## [0.4.1] - 2026-MM-DD` に追加:
  - `### Changed`: `hokusai notion-setup --profile <name>` で profile config の env 名を自動採用
  - `### Added`: profile 別の rc マーカー（複数 profile を同一 rc に並列保存可能）

---

## 10. 関連ドキュメント

- Issue: https://github.com/shigenoko/hokusai/issues/17
- Profile 機能の実装計画: `docs/hokusai-profile-parallel-execution-implementation-plan.md`
- Notion dashboard の実装計画: `docs/hokusai-notion-dashboard-implementation-plan.md`
- 運用ガイド（更新対象）: `docs/notion-dashboard-operation-guide.md`
