# HOKUSAI Profile 運用ガイド

**対象読者**: 複数案件（A 社・B 社・C 社）の開発を 1 PC で並列運用するエンジニア / テックリード / 運用担当

**前提**: HOKUSAI v0.3.0 以降

---

## 1. profile とは

profile は **1 案件 = 1 tenant** を表す実行スコープ。以下を案件ごとに完全分離する:

| 分離対象 | 理由 |
|---|---|
| `project_root` | 案件ごとに開発リポジトリが異なる |
| `data_dir` / `database_path` | workflow 状態 / outbox / cache を混ぜない |
| `checkpoint_db_path` | LangGraph checkpoint を混ぜない |
| `worktree_root` | cleanup / branch 作業対象を混ぜない |
| Notion / Figma / Miro / GitHub / Slack の token env var 名 | 誤った案件 token の使用を防止 |
| Operations Console port | 複数案件の dashboard を同時に開く |

**重要原則**: profile は「切り替え対象」ではなく **明示的な実行スコープ**。
`hokusai --profile a-company start ...` のように、コマンド実行のたびに明示する。

---

## 2. 初期セットアップ

### 2.1. profile registry の作成

`~/.hokusai/profiles.yaml` を作成する。

```yaml
default_profile: a-company

profiles:
  a-company:
    label: "A社 EC Platform"
    config: "~/work/hokusai-configs/a-company.yaml"
    data_dir: "~/.hokusai/profiles/a-company"
    dashboard:
      port: 8765
    description: "A社向け EC 開発案件"

  b-company:
    label: "B社 Admin System"
    config: "~/work/hokusai-configs/b-company.yaml"
    data_dir: "~/.hokusai/profiles/b-company"
    dashboard:
      port: 8766
```

**ファイル権限**: `chmod 600 ~/.hokusai/profiles.yaml`（multi-user OS で他ユーザーから読まれない）

**機密扱い**: `label` / `description` に顧客識別情報を書くなら、registry を **public repo にコミットしない**。コミットする場合は `hokusai-profiles.example.yaml` のような匿名サンプルにする。

### 2.2. 各 profile の config YAML

```yaml
# ~/work/hokusai-configs/a-company.yaml
project_root: ~/repo/a-company/backend
base_branch: main

# data_dir / database_path / checkpoint_db_path / worktree_root は
# registry の data_dir から自動補完される（明示してもよい）

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

### 2.3. 環境変数の設定

案件ごとに env var 名を **案件 prefix で分離** する:

```bash
# ~/.zshrc または direnv の .envrc に追記
export A_HOKUSAI_NOTION_API_TOKEN="secret_xxx"
export A_HOKUSAI_NOTION_WORKFLOWS_DB_ID="db-id"
export A_HOKUSAI_NOTION_PR_DB_ID="pr-db-id"
export A_HOKUSAI_FIGMA_API_TOKEN="figd_xxx"
export A_HOKUSAI_MIRO_API_TOKEN="miro_xxx"
export A_HOKUSAI_SLACK_WEBHOOK_URL="https://hooks.slack.com/..."

export B_HOKUSAI_NOTION_API_TOKEN="..."
# ...
```

**代替策（軽量〜厳密まで）**:

| 軽さ | 方式 | 概要 |
|---|---|---|
| 軽 | **direnv** | 案件 repo ディレクトリごとに `.envrc` を置き、`cd` で自動 source |
| 中 | **secret manager**（1Password CLI / sops / Doppler / Vault） | 起動時に CLI が token を fetch して env 注入 |
| 厳 | **container / devcontainer** | profile ごとに別コンテナで完全分離 |
| 厳 | **OS user 分離** | profile ごとに別 OS ユーザー（監査要件対応） |

### 2.4. 設定の検証

```bash
hokusai profile list                # 登録された profile 一覧
hokusai profile show a-company      # 解決結果（シークレットは表示されない）
hokusai profile doctor a-company    # 静的検査
hokusai profile doctor a-company --deep   # v0.3.0 時点では注意書きのみ表示
```

`doctor` で検出される問題:
- config file が存在しない
- `data_dir` が作成できない（権限不足など）
- dashboard port が他 profile と衝突
- data_dir が他 profile と衝突

> **`--deep` の現状**: v0.3.0 では `--deep` フラグは受け付けるものの、
> 実 API 接続テスト（Notion / Figma / Miro / GitHub / GitLab への疎通確認）は
> 未実装で「Phase E で実装予定」の注意書きを表示するのみです。
> 実装は次バージョン以降で対応します。

---

## 3. 日常運用

### 3.1. ワークフロー起動 / 進行

```bash
# A 社案件
hokusai --profile a-company start "https://notion.so/.../A社タスク"
hokusai --profile a-company status
hokusai --profile a-company continue <workflow-id>
hokusai --profile a-company list

# B 社案件（別ターミナル / 同時進行可）
hokusai --profile b-company start "https://notion.so/.../B社タスク"
hokusai --profile b-company list
```

実行時には冒頭に `Profile: a-company` が表示され、誤操作の検知が容易。

### 3.2. Dashboard の並列起動

```bash
# A 社 dashboard（port 8765、自動でブラウザ起動）
hokusai dashboard --profile a-company

# B 社 dashboard（port 8766、別ターミナル）
hokusai dashboard --profile b-company

# C 社 dashboard（--port で override）
hokusai dashboard --profile c-company --port 8767
```

dashboard のヘッダロゴ横に `Profile: a-company` バッジが表示され、開いている dashboard がどの案件かを誤認しない。

### 3.3. workflow_id の取り違え対応

profile を間違えた場合、HOKUSAI が他 profile に同じ workflow_id があるかを案内する仕組みが用意されている（CLI 統合は Phase F 以降）。

現状の API:

```python
from hokusai.config import find_workflow_in_other_profiles, load_profile_registry

registry = load_profile_registry()
found_in = find_workflow_in_other_profiles(
    "wf_20260512_001",
    current_profile="a-company",
    registry=registry,
)
# found_in == ["b-company"] のように、他 profile に存在する場合は案内
```

---

## 4. 移行手順（v0.2.x からのアップグレード）

### 4.1. 後方互換

- v0.2.x で作成された `~/.hokusai/workflow.db` は **そのまま動く**
  （ALTER TABLE で `profile_name` カラムが自動追加され、既存行は `(legacy)` 扱い）
- 既存の `hokusai -c configs/your.yaml start ...` 経路は **変更なし**
- `python scripts/dashboard.py` 直接起動も **動く**

### 4.2. 既存 1 案件運用 → profile 化への移行

```
Step 1. 現行 config を案件名で分割
  cp claude-workflow.yaml ~/work/hokusai-configs/a-company.yaml

Step 2. data_dir / database_path / checkpoint_db_path / worktree_root を
        案件別に明示
  # ~/work/hokusai-configs/a-company.yaml に追記
  data_dir: ~/.hokusai/profiles/a-company
  database_path: ~/.hokusai/profiles/a-company/workflow.db
  checkpoint_db_path: ~/.hokusai/profiles/a-company/checkpoint.db
  worktree_root: ~/.hokusai/profiles/a-company/worktrees

Step 3. 環境変数を案件 prefix に変更
  # 既存の HOKUSAI_NOTION_API_TOKEN を A_HOKUSAI_NOTION_API_TOKEN に
  # rename、config の api_token_env も合わせる

Step 4. ~/.hokusai/profiles.yaml を作成

Step 5. 既存 DB を案件 data_dir に移動（任意）
  mv ~/.hokusai/workflow.db ~/.hokusai/profiles/a-company/workflow.db
  mv ~/.hokusai/checkpoint.db ~/.hokusai/profiles/a-company/checkpoint.db

Step 6. profile doctor で検証
  hokusai profile doctor a-company

Step 7. 運用コマンドを --profile に切り替え
  hokusai --profile a-company list
  hokusai --profile a-company status
```

### 4.3. 既存 DB をそのまま使う場合

既存 `~/.hokusai/workflow.db` を `default` profile 配下として扱う設定にすれば、Step 5 のファイル移動は不要。

```yaml
# profiles.yaml
profiles:
  default:
    label: "既存運用"
    config: "~/.claude-workflow.yaml"
    data_dir: "~/.hokusai"
```

---

## 5. トラブルシューティング

### 5.1. `profile registry が見つかりません`

```
hokusai --profile a-company list
→ エラー: profile registry が見つかりません: ~/.hokusai/profiles.yaml
```

→ `~/.hokusai/profiles.yaml` を作成するか、`HOKUSAI_PROFILES_FILE` 環境変数で他のパスを指定する。または `-c/--config` で直接設定ファイルを指定する。

### 5.2. `--profile と --config / -c は同時に指定できません`

→ 明示的に排他にしている。どちらか一方のみ使う。

### 5.3. Dashboard port が衝突する

```
hokusai dashboard --profile a-company
→ エラー: port 8765 は既に使用中です。
```

→ `lsof -i :8765` で何が掴んでいるか確認。別案件の dashboard なら、それを参照する。新規に立てたい場合は `--port 8770` のように override。

### 5.4. profile doctor で port 衝突警告

```
hokusai profile doctor a-company
→ ✗ dashboard port 8765 が他 profile と衝突: b-company
```

→ `~/.hokusai/profiles.yaml` の `dashboard.port` を case ごとに別の値にする。

---

## 6. 1 process = 1 profile 契約

HOKUSAI は **1 OS プロセス = 1 profile** を契約として動作する。
理由は `set_config()` シングルトンが存在し、プロセス内で profile を切り替えると state が混ざる可能性があるため。

**実運用での結果**:
- 複数 profile を同時に動かすには **必ず別プロセス**として起動する（CLI / dashboard 共通）
- 別ターミナル / cron / launchd / systemd 等で並列実行は問題なし
- 1 プロセス内で `--profile a` 実行後に `--profile b` を実行することは **想定外**

---

## 7. 関連ドキュメント

| ドキュメント | 内容 |
|---|---|
| `docs/hokusai-profile-parallel-execution-implementation-plan.md` | 実装計画書（Phase A〜F、リスク、Open Questions） |
| `configs/example-profiles.yaml` | profiles.yaml の雛形 |
| `configs/example-profile-company.yaml` | profile config の雛形 |
| `docs/notion-dashboard-operation-guide.md` | Notion 同期の運用ガイド |
| `CHANGELOG.md` | v0.3.0 のリリースノート |
