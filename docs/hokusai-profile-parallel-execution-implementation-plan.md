# HOKUSAI Profile 並列実行対応 実装計画書

**作成日**: 2026-05-12

**Target Version**: v0.3.0（v0.2.0 直後の次マイナーリリース）

**対象読者**: HOKUSAI 運用設計者、実装担当エンジニア、複数案件を管理するテックリード

**位置付け**: 本ドキュメントは、A社 / B社 / C社のように、案件ごとに Notion / Figma / Miro / GitHub / GitLab / Slack などのアカウントやトークンが異なる前提で、HOKUSAI を安全に並列運用するための profile 機能の実装計画である。

**改訂履歴**:

| 日付 | 改訂内容 |
|---|---|
| 2026-05-12 | 初版 |
| 2026-05-12 | レビュー指摘 12 件を反映: Target v0.3.0 明示、§4.4「1 process = 1 profile」契約、§5.1 ファイル権限と公開リスク、§8.2 workflow_id 衝突戦略明確化、§9.2 dashboard.py 責務分離方針、§10.2 env 管理代替策（direnv / secret manager / container 等）、Phase D 詳細化、Phase E 既存 DB マイグレーション、§13 テスト追加（壊れた YAML / SQLite 競合 / legacy DB）、§14 Step 6 既存 DB 取扱、§15 リスク追加、§16 Open Q5/Q6 追加、§17 v0.3.0 マイルストーンと任意目標 |
| 2026-05-12 | 追加レビュー反映: `profile_name` 保存先を `workflows.profile_name` カラム中心に明確化、誤操作防止の実装フェーズ表記を Phase E に統一、循環参照テストを実パス衝突検出へ変更、複数 profile 時の profile 省略を警告ではなくエラーへ変更 |

---

## 1. 背景

HOKUSAI は現状、`-c/--config` で設定ファイルを指定できるため、案件ごとに YAML を分けることは可能である。一方で、本格運用では以下のような状況が想定される。

- A社、B社、C社の開発案件が同時に動いている
- それぞれ異なる Notion workspace / DB / integration token を使う
- それぞれ異なる Figma / Miro workspace token を使う
- GitHub / GitLab organization や repository も案件ごとに異なる
- ワークフロー状態、checkpoint、worktree、cache、outbox を案件間で混ぜたくない
- Operations Console も案件ごとに別々に起動・監視したい

この状況で「現在の profile を切り替える」方式にすると、A社のつもりでB社の token / DB / worktree を使う事故が起きやすい。特に cron、dashboard、複数ターミナル、複数エンジニア運用では危険である。

そのため、profile はグローバル状態ではなく、**各コマンド実行時に明示する独立した実行スコープ**として設計する。

## 2. 目的

HOKUSAI に profile 概念を導入し、案件ごとの実行環境を明確に分離しながら、複数 profile を同時に並列実行できるようにする。

### 2.1. ゴール

- `hokusai --profile a-company start ...` のように、実行時に profile を明示できる
- profile ごとに config / data_dir / database_path / checkpoint_db_path / worktree_root を分離できる
- profile ごとに Notion / Figma / Miro / Slack などの環境変数名を分離できる
- profile ごとに Operations Console を別ポートで並列起動できる
- 既存の `-c/--config` 運用を壊さない
- profile 名をログ、workflow state、dashboard 表示に含め、誤操作を減らす
- `profile use` のようなグローバル切替に依存しない

### 2.2. 非ゴール

- シークレット値を profile 定義ファイルへ保存する
- 複数案件の状態を 1 つの SQLite DB に集約する
- Notion / Figma / Miro の account switching を HOKUSAI が自動で行う
- OS ユーザーや container の分離を HOKUSAI が完全に代替する
- 既存 `-c/--config` を廃止する

## 3. 結論サマリ

profile は「切り替えるもの」ではなく、**並列に存在する実行環境を選択する引数**として扱う。

```bash
hokusai --profile a-company start <A社のタスクURL>
hokusai --profile b-company start <B社のタスクURL>
hokusai --profile c-company list
```

Operations Console も profile ごとに別プロセス・別ポートで起動する。

```bash
hokusai dashboard --profile a-company --port 8765
hokusai dashboard --profile b-company --port 8766
hokusai dashboard --profile c-company --port 8767
```

profile 解決後は、内部的には現在の `WorkflowConfig` を生成する。既存コードの大部分は `WorkflowConfig` を引き続き参照し、profile は config 解決前段の概念として実装する。

## 4. 基本方針

### 4.1. 1 profile = 1 tenant

1 profile は 1 tenant / 1案件 / 1顧客環境を表す。

profile ごとに分離するもの:

| 項目 | 分離理由 |
|---|---|
| config file | Notion / Figma / Miro / repo / command が案件ごとに異なるため |
| data_dir | logs / cache / runtime artifacts を混ぜないため |
| workflow.db | workflow_id、outbox、design cache を混ぜないため |
| checkpoint.db | LangGraph checkpoint を混ぜないため |
| worktree_root | cleanup や branch 作業対象を混ぜないため |
| token env var names | A社 token と B社 token の誤使用を防ぐため |
| Operations Console port | 複数案件を同時監視できるようにするため |

### 4.2. `--profile` は明示的なスコープ

以下を正式な使い方にする。

```bash
hokusai --profile a-company start ...
hokusai --profile a-company continue <workflow-id>
hokusai --profile a-company status
```

以下のような状態fulな切替は原則採用しない。

```bash
hokusai profile use a-company
hokusai start ...
```

`profile use` は便利に見えるが、ターミナル、cron、dashboard、別プロセスが絡むと事故要因になる。導入する場合でも shell 補助に留め、HOKUSAI 本体の正規 UX にはしない。

### 4.3. `-c/--config` 互換を維持

既存の直接 config 指定は維持する。

```bash
hokusai -c configs/a-company.yaml start ...
```

優先順位は以下とする。

1. `--config` があれば最優先
2. `--profile` があれば profile registry から config を解決
3. どちらもなければ既存の探索順序を使う

`--profile` と `--config` を同時指定した場合はエラーにする。暗黙の上書きは事故要因になる。

### 4.4. 1 process = 1 profile 契約

HOKUSAI は現在 `hokusai/config/manager.py` の `set_config()` / `get_config()` でモジュールレベルのシングルトンを保持している。同一プロセスで複数 profile を扱おうとするとこのシングルトンが衝突するため、以下を契約として明文化する。

- **1 OS プロセス = 1 profile** を契約とする
- 複数 profile を並行運用したい場合は **必ず別プロセス**として起動する（CLI 起動・dashboard 起動とも）
- `set_config()` シングルトンはこの前提で維持する（context-local 化はしない）
- テストで複数 profile を検証する場合は subprocess 起動か、`set_config()` の明示リセットを使う

この制約により、profile 切替に伴う in-process state pollution を避け、デバッグ時のメンタルモデルも単純化される。

## 5. Profile Registry

### 5.1. 配置場所

profile registry はユーザー単位の設定として、以下を標準候補にする。

```text
~/.hokusai/profiles.yaml
```

将来的にチーム共有テンプレートを持つ場合は、リポジトリ内にも置けるようにする。

```text
./hokusai-profiles.yaml
```

ただし、シークレット値は絶対に保存しない。保存するのは config path と運用メタ情報のみ。

**ファイル権限と公開リスク**:

- `~/.hokusai/profiles.yaml` は `chmod 600` を推奨（multi-user OS で他ユーザーから読まれないため）
- `./hokusai-profiles.yaml` を repo 内に置く場合は、**顧客識別情報（`label` / `description` の社名など）が含まれるなら `.gitignore` に追加**。public repo へのコミットは顧客情報漏洩につながる
- repo 共有する場合は `hokusai-profiles.example.yaml` のような匿名サンプルだけにする運用を推奨

### 5.2. 形式

```yaml
default_profile: a-company

profiles:
  a-company:
    label: "A社 EC Platform"
    config: "~/work/hokusai-configs/a-company.yaml"
    data_dir: "~/.hokusai/profiles/a-company"
    dashboard:
      port: 8765
    description: "A社向け開発案件"

  b-company:
    label: "B社 Admin System"
    config: "~/work/hokusai-configs/b-company.yaml"
    data_dir: "~/.hokusai/profiles/b-company"
    dashboard:
      port: 8766
```

`data_dir` は profile registry 側でも指定できるが、最終的には各 profile config の `data_dir` / `database_path` / `checkpoint_db_path` / `worktree_root` に展開して `WorkflowConfig` に渡す。

### 5.3. Profile config 例

```yaml
# ~/work/hokusai-configs/a-company.yaml
project_root: ~/repo/a-company/backend
base_branch: main

data_dir: ~/.hokusai/profiles/a-company
database_path: ~/.hokusai/profiles/a-company/workflow.db
checkpoint_db_path: ~/.hokusai/profiles/a-company/checkpoint.db
worktree_root: ~/.hokusai/profiles/a-company/worktrees

task_backend:
  type: notion

git_hosting:
  type: github
  repo: a-company/backend

notion_dashboard:
  enabled: true
  api_token_env: A_HOKUSAI_NOTION_API_TOKEN
  workflows_db_id_env: A_HOKUSAI_NOTION_WORKFLOWS_DB_ID
  pull_requests_db_id_env: A_HOKUSAI_NOTION_PR_DB_ID

figma:
  enabled: true
  api_token_env: A_HOKUSAI_FIGMA_API_TOKEN

miro:
  enabled: true
  api_token_env: A_HOKUSAI_MIRO_API_TOKEN

notifications:
  slack:
    enabled: true
    webhook_url_env: A_HOKUSAI_SLACK_WEBHOOK_URL
```

## 6. CLI UX

### 6.1. グローバルオプション

既存の parser に `--profile` を追加する。

```bash
hokusai --profile a-company start <task-url>
hokusai --profile a-company continue <workflow-id>
hokusai --profile a-company status
hokusai --profile a-company list
hokusai --profile a-company cleanup --stale
```

`-c/--config` は維持する。

```bash
hokusai -c configs/a-company.yaml start <task-url>
```

### 6.2. Profile 管理コマンド

最小実装で追加する。

```bash
hokusai profile list
hokusai profile show a-company
hokusai profile doctor a-company
```

`profile list` は registry にある profile を表示する。

```text
PROFILE      CONFIG                                  DATA DIR
a-company    ~/work/hokusai-configs/a-company.yaml   ~/.hokusai/profiles/a-company
b-company    ~/work/hokusai-configs/b-company.yaml   ~/.hokusai/profiles/b-company
```

`profile show` は解決後の config path、data_dir、database_path、worktree_root、参照する env var 名だけを表示する。シークレット値は表示しない。

`profile doctor` は以下を確認する。

- config file が存在する
- data_dir / worktree_root が作成可能
- Notion / Figma / Miro / Slack の env var 名が設定されている
- `database_path` と `checkpoint_db_path` が他 profile と衝突していない
- Operations Console port が他 profile と衝突していない

### 6.3. Dashboard 起動

既存の `scripts/dashboard.py` 直接起動に加え、正式 CLI として以下を追加する。

```bash
hokusai dashboard --profile a-company --port 8765
hokusai dashboard --profile b-company --port 8766
```

`--port` 省略時は profile registry の `dashboard.port` を使う。どちらも無い場合は既存の既定値を使う。ただし複数 profile 起動時は port 衝突を検出してエラーにする。

## 7. 設定解決フロー

### 7.1. 新規モジュール

以下を追加する。

```text
hokusai/config/profiles.py
```

責務:

- profile registry の読み込み
- profile 名の validation
- profile から config path / data_dir / dashboard port を解決
- `--profile` と `--config` の排他チェック
- profile 解決結果を `WorkflowConfig` 生成に渡す

### 7.2. データモデル

```python
@dataclass
class ProfileConfig:
    name: str
    label: str | None
    config_path: Path
    data_dir: Path | None
    dashboard_port: int | None
    description: str | None = None

@dataclass
class ProfileRegistry:
    default_profile: str | None
    profiles: dict[str, ProfileConfig]
```

### 7.3. `create_config_from_env_and_file()` の拡張

既存関数を壊さず、profile 解決済み config path を渡せるようにする。

案:

```python
def create_config_from_env_and_file(
    config_file: str | Path | None = None,
    *,
    profile_name: str | None = None,
) -> WorkflowConfig:
    ...
```

ただし `config_file` と `profile_name` の同時指定はエラーにする。

profile registry に `data_dir` がある場合、config file に明示がなければ以下を補完する。

```text
data_dir: <profile.data_dir>
database_path: <profile.data_dir>/workflow.db
checkpoint_db_path: <profile.data_dir>/checkpoint.db
worktree_root: <profile.data_dir>/worktrees
```

config file 側に明示がある場合は config file を優先する。

## 8. 永続化・排他制御

### 8.1. profile ごとの DB 分離

profile ごとに必ず別 DB を使う。

```text
~/.hokusai/profiles/a-company/workflow.db
~/.hokusai/profiles/b-company/workflow.db
~/.hokusai/profiles/c-company/workflow.db
```

checkpoint も同様に分離する。

```text
~/.hokusai/profiles/a-company/checkpoint.db
```

### 8.2. workflow_id の扱い

profile ごとに DB が分かれるため、workflow_id は **profile 内で一意**であればよい。生成ロジック自体は現状（`wf_YYYYMMDD_NNN` 形式）を変更しない。

**識別単位**: 内部処理では `(profile_name, workflow_id)` の **組** が一意識別子。単体の `workflow_id` は profile 名と合わせて初めて意味を持つ。

**表示時**: UI / Slack / Notion / logs では profile 名を併記する。

```text
[a-company] wf_20260512_001
```

**設計判断**:

- workflow_id 自体に profile prefix を含める案（例: `a_wf_...`）も検討したが却下
  - 既存 workflow_id 生成ロジックとの後方互換が損なわれる
  - 表示が長くなり、Slack / Notion での視認性が落ちる
- `(profile, workflow_id)` 組による横断検索の実装コストは限定的（§8.3 の候補探索ロジックでカバー）

### 8.3. 誤操作防止

`continue` / `cleanup` / `pr-status` など workflow_id を受け取るコマンドでは、指定 profile の DB に workflow_id が存在しない場合、他 profile を探索して候補を出す。

```text
workflow_id wf_123 was not found in profile a-company.
It exists in:
  - b-company

Run:
  hokusai --profile b-company continue wf_123
```

これは便利機能だが、Phase E 以降でもよい。Phase A/B の最小実装では、指定 profile の DB に存在しない場合に単純な not found エラーを返すだけでもよい。

## 9. Operations Console

### 9.1. 1 profile = 1 dashboard process

Operations Console は profile ごとに独立プロセスとして起動する。

```bash
hokusai dashboard --profile a-company --port 8765
hokusai dashboard --profile b-company --port 8766
```

理由:

- dashboard 内部の `_store` や config singleton を profile 間で共有しない（§4.4「1 process = 1 profile」契約を dashboard にも適用）
- 接続状態はローカル環境 + profile config の組み合わせで評価される
- Notion retry / cleanup / cache refresh の対象を明確にする

### 9.2. `scripts/dashboard.py` の責務分離

現状 `scripts/dashboard.py` には以下が集約されている:

- HTTP server 起動（`PORT = 8765` ハードコード）
- `_store: SQLiteStore | None` のシングルトン
- `_get_notion_dispatcher()` のキャッシュ
- HTML rendering / API ハンドラ
- tests からの直接 import（`isolated_dashboard.DB_PATH` 等）

Phase D で以下のように責務を分離する:

1. **`hokusai/dashboard/server.py` を新設**し、HTTP server / rendering / API ハンドラを移す（profile config を引数で受け取る形に変更）
2. **`scripts/dashboard.py` は薄いラッパ**として残す（後方互換、既存の `python scripts/dashboard.py` 起動を壊さない）
3. **CLI `hokusai dashboard --profile <name>`** は `hokusai/dashboard/server.py` を import して起動
4. **tests の import 経路** も `hokusai/dashboard/server.py` を指すよう同 Phase 内で移行

これにより `_store` / dispatcher キャッシュは server module スコープに閉じ、profile ごとに別プロセスで完全分離される。

### 9.3. 画面表示

全ページのヘッダーに profile を表示する。

```text
HOKUSAI Operations Console / Profile: a-company
```

設定ページにも以下を表示する。

- profile name
- config path
- data_dir
- database_path
- checkpoint_db_path
- worktree_root
- Notion / Figma / Miro / Slack の env var 名

値そのものは表示しない。

## 10. 環境変数とシークレット管理

### 10.1. 案件別 prefix（基本方針）

案件ごとに env var 名を分ける。

```bash
export A_HOKUSAI_NOTION_API_TOKEN="secret_..."
export A_HOKUSAI_NOTION_WORKFLOWS_DB_ID="..."
export A_HOKUSAI_NOTION_PR_DB_ID="..."
export A_HOKUSAI_FIGMA_API_TOKEN="..."
export A_HOKUSAI_MIRO_API_TOKEN="..."

export B_HOKUSAI_NOTION_API_TOKEN="secret_..."
export B_HOKUSAI_NOTION_WORKFLOWS_DB_ID="..."
export B_HOKUSAI_NOTION_PR_DB_ID="..."
export B_HOKUSAI_FIGMA_API_TOKEN="..."
export B_HOKUSAI_MIRO_API_TOKEN="..."
```

ただし「3 案件 × 10 env var = 30 個常時 export」状態は OS 環境変数 namespace を汚染する。下記の代替策と組み合わせて運用するのが推奨。

### 10.2. 代替策（軽量〜厳密まで）

| 軽さ | 方式 | 概要 | 適する場面 |
|---|---|---|---|
| 軽 | **direnv** | 案件 repo ディレクトリごとに `.envrc` を置き、`cd` で自動 source | 個人開発、案件 repo が分かれている場合 |
| 中 | **secret manager**（1Password CLI / sops / Doppler / Vault） | 起動時に CLI が token を fetch して env 注入 | チーム共有、token rotation が頻繁な場合 |
| 中 | **`hokusai run --profile <name> -- <cmd>`**（将来案） | profile 情報から env を組み立てて子プロセスに注入 | 将来的に HOKUSAI 側で wrapper 実装するなら |
| 厳 | **container / devcontainer** | profile ごとに別コンテナ、env も完全分離 | 顧客分離要件が厳格な場合 |
| 厳 | **OS user 分離** | profile ごとに別 OS ユーザー | 監査対応・コンプライアンス要件 |

実運用では「**direnv + 案件別 prefix**」「**secret manager + 案件別 prefix**」の組み合わせから始め、要件が厳しくなれば container / OS user 分離に移行するのが現実的。

### 10.3. `profile doctor` の確認

`profile doctor` は env var の存在だけを確認し、値は表示しない。

```text
Notion token env: A_HOKUSAI_NOTION_API_TOKEN ... set
Figma token env: A_HOKUSAI_FIGMA_API_TOKEN ... missing
```

### 10.4. CLI 認証依存の注意

`gh` / `glab` / Notion MCP など、外部 CLI や MCP が OS ユーザーのログイン状態に依存するものは、profile だけでは完全分離できない場合がある。

厳密な顧客分離が必要な場合は以下を推奨する。

- profile ごとに container / devcontainer を分ける
- profile ごとに OS ユーザーを分ける
- `GH_TOKEN` / `GITLAB_TOKEN` などを process env で注入する
- MCP 設定ファイルも profile ごとに分ける

## 11. Notion / Figma / Miro 運用

### 11.1. Notion

profile ごとに以下を分ける。

- HOKUSAI Workflows DB
- HOKUSAI Pull Requests DB
- HOKUSAI Notion integration token

Service Status は Notion に同期しない。接続状態は profile ごとの Operations Console で確認する。

### 11.2. Figma / Miro

profile ごとに token env var 名を分ける。

```yaml
figma:
  enabled: true
  api_token_env: A_HOKUSAI_FIGMA_API_TOKEN

miro:
  enabled: true
  api_token_env: A_HOKUSAI_MIRO_API_TOKEN
```

design cache は profile ごとの `workflow.db` に保存されるため、A社の Figma cache が B社に見えることはない。

## 12. 実装フェーズ

### Phase A: Profile Registry 基盤

**対象ファイル**

- `hokusai/config/profiles.py`
- `hokusai/config/__init__.py`
- `tests/test_profiles.py`

**実装内容**

- `ProfileConfig` / `ProfileRegistry` dataclass 追加
- `~/.hokusai/profiles.yaml` 読み込み
- profile 名 validation
- config path 解決
- data_dir 補完ルール実装
- registry が無い場合のエラー文整備

**DoD**

- profile registry を読める
- unknown profile で明確なエラーが出る
- `--profile` と `--config` の同時指定を拒否できる

### Phase B: CLI 統合

**対象ファイル**

- `hokusai/cli_main.py`
- `hokusai/config/manager.py`
- `tests/test_cli_profiles.py`

**実装内容**

- global option `--profile` 追加
- config 解決に profile を渡す
- `hokusai profile list`
- `hokusai profile show <name>`
- `hokusai profile doctor <name>`
- エラー出力の整備

**DoD**

- `hokusai --profile a-company list` が該当 DB だけを見る
- `hokusai --profile a-company start ...` が該当 config で動く
- `-c/--config` 既存動作が壊れない

### Phase C: Data Dir 自動補完

**対象ファイル**

- `hokusai/config/manager.py`
- `hokusai/config/models.py`
- `tests/test_profiles.py`

**実装内容**

- profile registry の `data_dir` から `database_path` / `checkpoint_db_path` / `worktree_root` を補完
- config file 明示値を優先
- path の `~` 展開
- profile ごとのディレクトリ作成

**DoD**

- data_dir だけ指定した profile でも安全に DB / checkpoint / worktree が分離される
- 既存 config file の明示 path は上書きされない

### Phase D: Dashboard 正式 CLI 化

**v0.3.0 での方針（最小侵襲）**

§9.2 で議論した「`hokusai/dashboard/server.py` への責務移行」は v0.3.0 では
**実施しない**。`scripts/dashboard.py` は既存のテスト・運用に深く統合されており、
責務移行を伴うリファクタは破壊的変更のリスクが高いため、v0.3.0 では以下の
最小侵襲アプローチで CLI 統合を実現する:

- `scripts/dashboard.py` の HTTP server / rendering / API 実装は据え置き
- `scripts/dashboard.py` を環境変数（`HOKUSAI_DASHBOARD_PORT` /
  `HOKUSAI_DASHBOARD_DB_PATH` / `HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH` /
  `HOKUSAI_DASHBOARD_PROFILE`）で外部制御可能化
- `hokusai/dashboard/__init__.py` を新設し、CLI が env を組み立てて
  `scripts.dashboard.main()` を呼ぶブリッジ層として機能させる
- 完全な責務移行（`hokusai/dashboard/server.py` 新設）はフォローアップ
  リリース（v0.4 以降）で対応

**対象ファイル**

- `hokusai/cli_main.py`
- `hokusai/dashboard/__init__.py`（新規、CLI ⇄ scripts.dashboard のブリッジ層）
- `scripts/dashboard.py`（env 経由制御化、責務移行は未実施）
- `pyproject.toml`（wheel に `scripts` パッケージを含める設定追加）
- `tests/test_dashboard_profiles.py`

**実装内容**

- `scripts/dashboard.py` を環境変数経由で外部制御可能化
  - `HOKUSAI_DASHBOARD_PORT` / `HOKUSAI_DASHBOARD_DB_PATH` /
    `HOKUSAI_DASHBOARD_CHECKPOINT_DB_PATH` / `HOKUSAI_DASHBOARD_PROFILE`
  - module 内 state（`_store` シングルトン等）の env 反映用 refresh フック
- `hokusai/dashboard/__init__.py` で CLI ⇄ scripts.dashboard をブリッジ
  - profile config から DB path / port を解決して env に注入
  - `scripts.dashboard.main()` を起動
  - 起動前ポート衝突検出（`_port_in_use`、`EADDRINUSE` のみ True 判定）
  - 起動時 race による `OSError(EADDRINUSE)` を `DashboardPortInUseError` に変換
- `hokusai dashboard [--profile <name>] [--port <port>]` サブコマンド追加
- dashboard header に profile name を表示
- profile registry の `dashboard.port` を default として利用
- `pyproject.toml` の `[tool.hatch.build.targets.wheel]` で `packages = ["hokusai", "scripts"]` に拡張（配布版で `scripts.dashboard` を import できるようにする）

**DoD**

- A社/B社 dashboard を別ポートで同時起動できる
- それぞれ別 DB / config を参照する
- 接続状態ページが profile config の env var 名を参照する
- 既存の `python scripts/dashboard.py` 起動も壊れない（後方互換）
- 既存の dashboard 関連テスト（`tests/test_dashboard_notion_panel.py` 等）が pass する
- pip インストール環境でも `hokusai dashboard` が起動する

### Phase E: 誤操作防止と診断

**対象ファイル**

- `hokusai/cli_main.py`
- `hokusai/persistence/sqlite_store.py`
- `tests/test_profile_safety.py`

**実装内容**

- workflow not found 時に他 profile の候補を探索（`find_workflow_in_other_profiles()`）
  - sqlite3 URI `mode=ro` で副作用なしの read-only 探索
  - profile config の `database_path` 明示上書きを尊重（false negative 防止）
- `profile doctor` での衝突検出（v0.3.0 範囲）
  - **検出対象**: dashboard port 重複 / `data_dir` 重複 / config file 存在チェック
  - **`--deep` フラグ**: 受け付けるが実装は注意書き表示のみ（API 疎通確認は v0.4 以降）
  - **v0.3.0 では未実装**: `database_path` / `checkpoint_db_path` / `worktree_root`
    個別の衝突検出、env var 存在確認（`data_dir` 統一運用が主、明示上書きは
    レアケースのため後続対応）
- command 出力に profile 名を表示
- `workflows` テーブルに `profile_name` カラムを追加し、workflow の所属 profile を保存する
  - 保存先の正本は `workflows.profile_name`
  - `state_json` には既存処理との互換のため任意で `profile_name` を含めてもよいが、検索・一覧表示・監査用途では DB カラムを参照する
  - `save_workflow()` / `load_workflow()` / `list_active_workflows()` / workflow detail 系のクエリを `profile_name` 対応に更新する
  - 既存 workflow（v0.2.x 以前に作成されたもの）の `profile_name` カラムは NULL のまま
  - 表示時は profile 名未設定なら `(legacy)` または default profile 名でフォールバック
  - SQLite ALTER TABLE による在地マイグレーション（既存の SQLite WAL モードと互換）
  - PRAGMA table_info で事前判定し、duplicate column の race も catch して並行起動に耐える

**DoD**

- 間違った profile で `continue` した時に分かりやすい案内が出る
- workflow detail / list で profile が見える
- 既存 v0.2.x DB を v0.3.0 で開いても壊れない（legacy 表示で読み出し可能）
- `profile doctor` で port / data_dir 衝突を検出できる（実 API 疎通確認は v0.4 以降）

### Phase F: ドキュメント・運用ガイド

**対象ファイル**

- `docs/profile-operation-guide.md`
- `docs/notion-dashboard-operation-guide.md`
- `README.md`
- `README_JP.md`
- `configs/example-profiles.yaml`
- `configs/example-profile-a-company.yaml`

**実装内容**

- multi-profile 運用手順
- 案件別 env var prefix の例
- dashboard 並列起動例
- container / OS user 分離が必要なケースの注意

**DoD**

- A社/B社/C社の例でセットアップから起動まで説明されている
- シークレットを profile registry に保存しないことが明記されている

## 13. テスト計画

### 13.1. Unit

- profile registry parser
- unknown profile
- invalid profile name
- `--profile` / `--config` 排他
- data_dir 補完
- env var 名の表示と secret 値非表示
- **壊れた YAML / 不正フィールドのエラーメッセージ**（registry / profile config 双方）
- **profile 間の実パス衝突検出**
  - 同一 `config` path の重複
  - `database_path` / `checkpoint_db_path` / `worktree_root` の重複
  - symlink 解決後に同一実体を指す path の衝突
- **`(profile, workflow_id)` 組による横断検索ロジック**（§8.2 / §8.3）

### 13.2. Integration

- `hokusai --profile a list` が A の DB のみ読む
- `hokusai --profile b list` が B の DB のみ読む
- A/B で同じ workflow_id が存在しても混ざらない
- dashboard A/B を別 port で起動できる
- Notion/Figma/Miro の token env var 名が profile ごとに変わる
- **同一 profile の dashboard + CLI 並行書き込み**（SQLite WAL competitive write、profile 並列導入で顕在化しうるため）
- **既存 v0.2.x DB（`profile_name` カラム NULL）を v0.3.0 で読み出し**できる（legacy 表示でフォールバック）

### 13.3. Regression

- `hokusai -c configs/example-github-issue.yaml start ...` が従来通り動く
- profile registry が無い環境でも `-c` 指定なら動く
- default config 探索順序が壊れない
- 既存の `python scripts/dashboard.py` 直接起動が壊れない（§9.2 の薄いラッパが機能）

## 14. 移行計画

### Step 1: 現行 config を案件別に分割

```text
configs/a-company.yaml
configs/b-company.yaml
configs/c-company.yaml
```

### Step 2: data_dir を明示

各 config に以下を入れる。

```yaml
data_dir: ~/.hokusai/profiles/a-company
database_path: ~/.hokusai/profiles/a-company/workflow.db
checkpoint_db_path: ~/.hokusai/profiles/a-company/checkpoint.db
worktree_root: ~/.hokusai/profiles/a-company/worktrees
```

### Step 3: token env var 名を案件別 prefix に変更

```yaml
notion_dashboard:
  api_token_env: A_HOKUSAI_NOTION_API_TOKEN

figma:
  api_token_env: A_HOKUSAI_FIGMA_API_TOKEN
```

### Step 4: profile registry を追加

```yaml
profiles:
  a-company:
    config: "~/work/hokusai-configs/a-company.yaml"
    dashboard:
      port: 8765
  b-company:
    config: "~/work/hokusai-configs/b-company.yaml"
    dashboard:
      port: 8766
```

### Step 5: 運用コマンドを `--profile` に移行

```bash
hokusai --profile a-company start ...
hokusai --profile b-company start ...
```

### Step 6: 既存 DB の取り扱い

v0.2.x で作成した既存 `~/.hokusai/workflow.db` がある場合:

- そのまま `data_dir: ~/.hokusai` を `default` profile として扱う設定にすれば、追加マイグレーション不要で継続利用可能
- ただし他案件と混在を避けたい場合は、既存 DB を案件別 `data_dir` に移し替え（`mv ~/.hokusai/workflow.db ~/.hokusai/profiles/<name>/workflow.db`）してから profile 設定を完了させる
- ALTER TABLE による `profile_name` カラム追加は HOKUSAI 側で自動実行される（既存行は NULL → 表示時 `(legacy)` 扱い）

## 15. リスクと対策

| リスク | 対策 |
|---|---|
| profile 指定忘れ | default_profile は任意。registry に profile が 1 つだけなら省略可、2 つ以上ある場合は profile 省略をエラーにする |
| token env var 名の取り違え | `profile doctor` で env var 名と設定先を表示。値は非表示 |
| DB path 衝突 | `profile doctor` と起動時 validation で検出 |
| worktree_root 衝突 | `profile doctor` で検出 |
| dashboard port 衝突 | 起動時に listen 済み port を検出 |
| gh/glab CLI の account 共有 | process env / container / OS user 分離を推奨 |
| Notion MCP の account 共有 | profile ごとの MCP 設定分離を将来課題にする |
| **同一 profile での dashboard + CLI 並行書き込み（SQLite ロック競合）** | SQLite WAL モードで概ね対応済み。Phase D 完了後に integration test で検証（§13.2） |
| **profile registry の権限不備による情報漏洩** | `~/.hokusai/profiles.yaml` は `chmod 600`、repo コミット時は顧客情報を `description`/`label` に含めない（§5.1） |
| **OS / shell 依存コマンドの差異**（port 衝突検出の `lsof` / `ss` 等） | macOS / Linux 両対応を最低限とし、それ以外は best-effort |
| **既存 v0.2.x DB 互換性** | `profile_name` カラム ALTER TABLE 追加、NULL 行は `(legacy)` 表示（§Phase E） |

## 16. Open Questions

1. `default_profile` を許可するか
   - 開発者個人の単一案件では便利
   - 複数案件運用では指定忘れ事故につながる
   - 案: registry に profile が 1 つだけなら省略可、2 つ以上なら明示必須

2. profile registry をリポジトリに置くか、ユーザー home に置くか
   - home は個人向け
   - repo はチーム共有しやすい
   - 案: `~/.hokusai/profiles.yaml` を正本、repo 側はテンプレート扱い

3. `profile use` を完全に禁止するか
   - 案: 本体コマンドとしては実装しない
   - shell alias / direnv での補助はドキュメントに留める

4. dashboard を `scripts/dashboard.py` から CLI サブコマンドへ完全移行するか
   - 案: 当面は両対応。正式 UX は `hokusai dashboard --profile ...`
   - §9.2 で `scripts/dashboard.py` を薄いラッパ化する方針を採用

5. `profile doctor --deep` で実 connectivity チェックを実装するか
   - env var 存在だけでなく、Notion API ラウンドトリップ / DB ID 実在確認まで踏み込むか
   - 案: Phase E に optional flag として実装。`--deep` 指定時のみ実 API を叩く
   - リスク: rate limit を消費する、CI 環境で実 token がないと test が回らない
   - これらを許容するなら追加価値が大きいため実装推奨

6. `~/.hokusai/profiles.yaml` の所有・配布フォーマット
   - 個人 home に置くと CI / chef / ansible 等での配布が難しい
   - 案: 環境変数 `HOKUSAI_PROFILES_FILE` で path override 可能にし、CI では `/etc/hokusai/profiles.yaml` 等を指せるようにする

## 17. 完了条件

### 17.1. 機能要件

- A社/B社/C社の 3 profile を同時に定義できる
- 3 profile の workflow DB / checkpoint DB / worktree root が分離される
- A社/B社 dashboard を同時起動できる
- `hokusai --profile a-company list` と `hokusai --profile b-company list` の結果が混ざらない
- Notion / Figma / Miro / Slack の env var 名が profile ごとに分離される
- 既存 `-c/--config` 利用者に破壊的変更がない
- 既存 v0.2.x DB を v0.3.0 で開いても壊れない（legacy 表示で読み出し可能）

### 17.2. リリース要件

- マイルストーン: **v0.3.0**
- CHANGELOG.md に Added / Changed / Migration Guide を記載
- 移行ガイド（§14）を `docs/profile-operation-guide.md` に詳細化
- `--profile` と `-c/--config` の共存パターンを README に明示
- `profile doctor` のサンプル出力を運用ガイドに掲載

### 17.3. 任意目標（Phase E 以降の発展系）

- `profile doctor --deep` による Notion / Figma / Miro 実 connectivity チェック（Open Q5）
- `HOKUSAI_PROFILES_FILE` 環境変数による registry path override（Open Q6）
- `hokusai run --profile <name> -- <cmd>` 形式の env 注入 wrapper（§10.2）
