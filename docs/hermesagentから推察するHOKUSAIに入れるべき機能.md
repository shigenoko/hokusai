# Hermes Agentから推察するHOKUSAIに入れるべき機能

**作成日**: 2026-05-12

**改訂履歴**:

| 日付 | 改訂内容 |
|---|---|
| 2026-05-12 | 初版 |
| 2026-05-12 | Hermes Agent 実機能調査結果を反映: Toolset 3 階層、Hooks 3 層、Delegation、Cron 2 モード + context_from チェーン、Context Files first-match-wins、MCP tool 単位 allowlist、Trajectory ShareGPT 互換フォーマット、メッセージング 20+ プラットフォーム規模の正確化 |
| 2026-05-12 | 追加レビュー反映: Hooks 配置を profile スコープへ統一、`profile doctor` と `profile doctor --deep` の責務分離、JSONL 監査ログの保護要件追記、Scheduled Maintenance 小見出し番号修正、改訂メモの名残を削除 |
| 2026-05-12 | §5 doctor 検査項目に `<data_dir>/sessions/` ディレクトリ / JSONL ファイル権限チェックを追加し、§9.5 監査ログ保護要件と双方向整合 |

## 目的

Hermes Agentの機能群を参考にしつつ、HOKUSAIの基本コンセプトである「開発案件をフェーズ管理し、Notion/Git/PR/レビュー/Operations Consoleと連動して進めるワークフローエンジン」を崩さない範囲で、導入すべき機能を整理する。

HOKUSAIは汎用パーソナルエージェントではなく、複数案件の開発運用を再現性高く回すための開発支援基盤として進化させるべきである。

## Hermes Agentの特徴

Hermes Agent（MIT、v0.13.0）は、常駐型の汎用エージェント基盤として極めて広い機能領域を持つ。HOKUSAIに取り込み判断をするうえで把握しておくべき主要点は以下の通り。

- **20以上のメッセージングプラットフォームを1 gatewayプロセスで統合**（Slack / Discord / Telegram / WhatsApp / Signal / Email / SMS / Matrix / Mattermost / Teams / Feishu / DingTalk / WeCom / WeChat / Yuanbao / QQbot / LINE / Google Chat / Webhooks / Home Assistant / Open-WebUI ほか）
- **MCP統合**（stdio + HTTP、OAuth manager、tool単位のinclude/exclude allowlist、動的更新通知 `notifications/tools/list_changed` 対応）
- **3階層のContext Files読み込み**（`HERMES.md` / `AGENTS.md` / `CLAUDE.md` / `.cursorrules` のfirst-match-wins + `SOUL.md`のinstance-global）
- **Toolset 3階層構造**（core / composite / platform）と permission / allowlist
- **3層のHooks システム**（Gateway / Plugin / Shell、`pre_tool_call`ブロック、`pre_llm_call`コンテキスト注入を含む）
- **Cron 2モード**（agent-based / no-agent script-only）と `context_from` によるジョブ間チェーン
- **Delegation（並列サブエージェント）** とfresh conversation isolation
- **長期記憶**（`MEMORY.md` 環境・規約、`USER.md` ユーザーモデル、Honchoによるdialectic user modeling）
- **ブラウザ操作 / 音声 / TTS / Computer use** などの汎用インターフェース
- **Trajectory永続化**（SQLite + FTS5 + JSONL二重保存、ShareGPT互換 `<tool_call>` / `<tool_response>` XML フォーマット）
- **Skills（自己改善ループ）** と Curator による自動キュレーション

これらは強力だが、HOKUSAIにそのまま取り込むと「開発ワークフロー支援」から「何でもできる常駐エージェント」へ寄ってしまう。HOKUSAIでは、機能の採否を「複数案件の開発運用が安全に、並列に、追跡可能に回るか」で判断する。

## HOKUSAIに入れるべき機能

### 1. Profile / Workspace分離

最優先で導入するべき機能。

複数案件を本格運用する場合、A社、B社、C社でNotion、Figma、Miro、GitHub/GitLab、環境変数、作業ディレクトリ、DB、チェックポイント、ダッシュボードポートが分かれる。これらを単一のグローバル設定で扱うと、誤同期や誤操作のリスクが高い。

HOKUSAIでは、profileを「切り替え対象」ではなく「並列実行できる独立した実行スコープ」として扱うべきである。

各profileは少なくとも次を持つ。

- `profile_id`
- `project_root`
- `worktree_root`
- `data_dir`
- `database_path`
- `checkpoint_db_path`
- `notion`
- `figma`
- `miro`
- `git_hosting`
- `dashboard_port`
- profile専用の環境変数読み込み設定

CLIでは次のような利用形にする。

```bash
hokusai --profile company-a start
hokusai --profile company-b status
hokusai dashboard --profile company-c --port 8767
hokusai profile list
hokusai profile doctor company-a
```

### 2. Toolset / Capability制御（3階層構造）

Hermes Agentのtoolset的な考え方は、HOKUSAIにも必要である。ただしフラットな on/off ではなく、Hermesと同様に **3階層のtoolset構造** を採用する。

#### 2.1. Toolset の3階層

| 階層 | 役割 | HOKUSAIでの例 |
|---|---|---|
| **Core** | 単一機能群の最小単位 | `notion-read` / `notion-write` / `figma-read` / `github-pr` / `github-mutation` / `slack-notify` |
| **Composite** | 複数 core を統合した役割単位 | `developer-review` = `notion-read` + `github-pr` + `slack-notify`<br>`design-handoff` = `notion-write` + `figma-read` + `miro-read`<br>`release-manager` = `github-mutation` + `slack-notify` |
| **Platform** | 特定運用コンテキスト向け完全構成 | `hokusai-cli` / `hokusai-dashboard` / `hokusai-cron` |

#### 2.2. Profile設定例

case A: 機能群を直接 enable

```yaml
profiles:
  company-a:
    capabilities:
      notion: true
      figma: true
      miro: false
      github: true
      gitlab: false
      slack: false
```

case B: composite で宣言（推奨）

```yaml
profiles:
  company-a:
    toolsets:
      - developer-review
      - design-handoff
  company-b:
    toolsets:
      - developer-review
      - release-manager
```

composite を使うと、profile 設定が宣言的になり、新しい case ができたときに「個別 core の組み合わせ」ではなく「役割名」で記述できる。

#### 2.3. 個別 tool レベルの override

Hermesと同様、composite 内の特定 tool だけ無効化できる粒度も持つ。

```yaml
profiles:
  company-a:
    toolsets:
      - developer-review
    disabled_tools:
      - github.delete_branch   # composite には含まれるが、この案件では禁止
      - notion.archive_page
```

これにより、誤った外部サービスへの接続や、危険な destructive 操作の意図しない実行を避けられる。

### 3. MCP / 外部ツールallowlist（server単位 + tool単位）

HOKUSAIがMCPや外部ツール連携を拡張する場合、profileごとのallowlistが必要である。

汎用的にMCPサーバーをすべて見せるのではなく、案件単位で利用可能なツールを固定する。これにより、A社案件のNotionやFigmaを操作しているつもりで、B社のMCP設定を参照してしまうような事故を防ぐ。

Hermes は server 単位だけでなく **tool 単位の include / exclude** までサポートしており、HOKUSAI でも同程度の粒度が運用上必要になる（例: 「Notion MCP は使うが `delete_page` だけ禁止」）。

設定例（server + tool 単位）:

```yaml
profiles:
  company-a:
    mcp:
      allowed_servers:
        notion-company-a:
          tools:
            include:        # 明示許可リスト（省略時は全許可）
              - retrieve-a-page
              - patch-page
              - post-search
            exclude:        # 明示禁止リスト
              - delete-a-block
              - move-page
            resources: true  # MCP resource API を許可するか
            prompts: false   # MCP prompt API を許可するか
        figma-company-a:
          tools:
            exclude:
              - post-comment  # read-only モードに固定
```

server 単位だけで十分な場合はシンプルな形式も許容:

```yaml
profiles:
  company-b:
    mcp:
      allowed_servers:
        - notion-company-b
        - github-company-b
```

allowlist に書かれていない server / tool は **deny by default**。

### 4. Project Context Files

Hermes Agentのコンテキストファイルの考え方は、HOKUSAIにも有効である。

HOKUSAIでは、案件ごとの開発規約、PR方針、レビュー観点、運用ルールを明示的に読み込めるようにする。

#### 4.1. 候補ファイルと優先順位（first-match-wins）

以下の順で **最初に見つかった 1 ファイルだけを読み込む**（全部マージしない）。これは Hermes の方式に倣う。複数マージすると profile / project / global の混乱が起きやすいため。

1. `.hokusai/context.md`（HOKUSAI 固有・最優先）
2. `HOKUSAI.md`
3. `AGENTS.md`
4. `CLAUDE.md`（既存 Claude Code エコシステムとの互換性のため）

#### 4.2. スコープ: project-local のみ

Context files は **project-local（profile の `project_root` + 祖先ディレクトリ walk）** のみを読む。`~/.hokusai/` から global context を読む設計は採用しない。

理由:

- HOKUSAI には profile という強い global 設定層が既にある
- さらに global context を重ねると「どこから来た指示か」が追跡困難になる
- 開発規約はリポジトリにコミットして version 管理するべき情報

#### 4.3. profile からの無効化

profile 設定から context file 読み込みを無効化、または特定ファイル名のみ許可できる。

```yaml
profiles:
  company-a:
    context_files:
      enabled: true
      allowed:           # 省略時は §4.1 全部
        - .hokusai/context.md
        - HOKUSAI.md
```

#### 4.4. 用途

- 案件固有のブランチ運用
- PRレビューの観点
- テスト実行方針
- Notion更新方針
- Figma/Miroの扱い
- 禁止操作

### 5. Doctor / Setup Wizard

複数profileを安全に使うには、設定の正しさを事前に検査できる必要がある。

`hokusai profile doctor <profile>` を追加し、次を検査する。

- 設定ファイルが存在するか
- `project_root` が存在するか
- `worktree_root` が存在するか
- DBパスがprofileごとに分離されているか
- 必要な環境変数が揃っているか
- Notion/Figma/Miro/GitHub/GitLab の env var 名が設定され、必要な値が存在するか
- dashboard portが衝突していないか
- capabilityと認証情報の整合性が取れているか
- `<data_dir>/sessions/` のディレクトリ権限が `700`、JSONL ファイル権限が `600` 以下か（§9.5 と連動）

通常の `profile doctor` は静的検査と env var の存在確認に留める。実際に Notion / Figma / Miro / GitHub / GitLab API へ接続する確認は、rate limit や CI 実行への影響を避けるため、明示的に `profile doctor --deep <profile>` を指定した場合だけ行う。

これは本格運用時の初期トラブルを大きく減らす。

### 6. Hooks システム（3層構造）

Hermes Agent の Hooks システムは、HOKUSAI の「destructive 操作の事前承認」「Phase 開始前のコンテキスト動的注入」というユースケースに直結する、最も転用価値の高い設計要素である。

#### 6.1. 3層構造

| 層 | 登録方法 | 実行コンテキスト | HOKUSAI での用途 |
|---|---|---|---|
| **Gateway hooks** | `<profile.data_dir>/hooks/<name>/HOOK.yaml` + `handler.py` | Operations Console / cron のみ | アラート、外部 webhook 通知、監査ログ |
| **Plugin hooks** | 拡張プラグインの `ctx.register_hook()` | CLI + dashboard 両方 | tool 介入、guardrails、metrics 収集 |
| **Shell hooks** | profile config の `hooks:` ブロック | CLI + dashboard 両方 | drop-in スクリプト、blocking、コンテキスト注入 |

#### 6.2. 主な介入点

| Hook | 戻り値 | HOKUSAI での例 |
|---|---|---|
| **`pre_tool_call`** | `{"action": "block", "message": ...}` で **tool 実行を事前ブロック** | 「`github.create_pr` を blocked、対応 Notion タスクが Active でない」「`notion.archive_page` は手動承認待ち」 |
| **`pre_llm_call`** | `{"context": str}` で **LLM 呼び出し前にコンテキスト注入**（prompt cache を壊さない） | Phase 5 開始前に Notion ページ本文を context に注入、PR レビュー前に対応 spec を注入 |
| **`post_tool_call`** | fire-and-forget observer | tool 呼び出しのテレメトリ送信 |
| **`transform_tool_result`** | 結果加工 | 機密情報の自動マスキング |
| **`on_session_start` / `on_session_end`** | セッションライフサイクル | profile 名と workflow_id をログ先頭に明示、終了時に outbox flush |
| **`pre_approval_request` / `post_approval_response`** | 承認フロー介入 | Slack 通知連携、承認履歴 DB 保存 |

#### 6.3. 設定例

```yaml
# ~/work/hokusai-configs/company-a.yaml
hooks:
  pre_tool_call:
    - name: notion-task-must-be-active
      handler: ./hooks/check_notion_task_active.py
      tools:                       # 対象 tool を絞り込み
        - github.create_pr
        - github.push
    - name: destructive-needs-approval
      handler: ./hooks/require_approval.py
      tools:
        - github.delete_branch
        - notion.archive_page
        - figma.post_comment

  pre_llm_call:
    - name: inject-notion-spec
      handler: ./hooks/inject_notion_spec.py
      phases: [5]                  # Phase 5 のみ発火

  on_session_start:
    - name: log-profile-and-workflow
      handler: ./hooks/log_session_start.py
```

#### 6.4. 承認の永続化

Hermes は初回 shell hook 利用時に承認を要求し、`~/.hermes/shell-hooks-allowlist.json` に保存する。HOKUSAI でも同じパターンを採用する。

```text
~/.hokusai/profiles/company-a/hooks-allowlist.json
```

これにより、案件ごとに「一度承認した hook の組み合わせ」が記録され、二重承認による疲労を避けられる。

#### 6.5. 設計上の注意

- **Hook の戻り値で workflow を破壊できない**: `pre_tool_call` で block しても、HOKUSAI 本体の state machine が壊れない設計にする（block 後は Phase 状態が `blocked_by_hook` に遷移、`hokusai continue` で再開可能）
- **profile スコープ**: hook 定義は profile config に紐づく。global hook は採用しない（4.2 と同じ理由）
- **承認に Slack を絡める場合**: Slack 通知 → ボタンクリック → HOKUSAI への callback の経路は **§9 Slack 限定通知に統合**（追加プラットフォームを増やさない）

### 7. 限定的なScheduled Maintenance（2モード + context_fromチェーン）

Hermes Agentのcron的な機能のうち、「自然言語で任意 prompt を定期実行できる仕組み」は HOKUSAI に入れない。責務が広がりすぎる。

ただし Hermes が採用している **2 モード構造** と **`context_from` チェーン** は、HOKUSAI の保守用途と相性が良く、取り入れる価値がある。

#### 7.1. 2モード

| モード | 内容 | HOKUSAI での用途 |
|---|---|---|
| **No-Agent (Script-only)** | 定義済みスクリプトを実行し stdout を直配信 | stale worktree 検出、outbox 再送、接続状態更新など、決まりきった保守ジョブ |
| **Agent-Based** | profile config で許可された toolset を持つ fresh session で prompt を実行 | PR レビュー状況の要約、Notion ダッシュボードの整合性確認など、判断が必要な保守ジョブ |

Hermes の「任意 prompt 登録」と異なり、HOKUSAI では **Agent モードでも prompt は事前定義された保守用テンプレートに限定**する。

#### 7.2. context_from チェーン

ジョブ間の出力チェーンをサポートする。前ジョブの output を次ジョブの context として渡せる。

```yaml
cron_jobs:
  pr-status-fetch:
    schedule: "0 8 * * *"          # 毎朝 8 時
    mode: no-agent
    script: hokusai cron pr-status-collect

  pr-status-summarize:
    schedule: "0 9 * * *"          # 毎朝 9 時
    mode: agent
    template: pr_status_summary    # 事前定義テンプレート
    context_from: pr-status-fetch  # 1 時間前の出力を context に
    deliver_to: slack
```

#### 7.3. 標準保守ジョブ（HOKUSAI 同梱）

- stale worktree検出
- 古いcheckpointの整理
- outboxの再送
- PR状態の再取得
- Notion同期の再試行
- Operations Console用の接続状態更新

これらは No-Agent モードで提供し、profile ごとに on/off できる。

#### 7.4. 再帰防止

Hermes と同様、**cron 実行内で新たな cron ジョブを登録することは禁止**する（再帰防止）。

### 8. Phase Artifact Compression

長い開発案件では、フェーズごとの議論、レビュー、PR、修正履歴が増える。Hermes Agentの文脈圧縮に近い仕組みを、HOKUSAIではフェーズ成果物の圧縮として導入する。

目的は、会話の長期記憶ではなく、開発判断の追跡性を維持すること。

保存対象:

- 要件整理の要約
- 実装方針
- 変更ファイル
- テスト結果
- PRレビュー指摘
- 未解決リスク
- 次フェーズへの申し送り

保存先はprofileごとのDBまたは`data_dir`配下とし、案件間で混ざらないようにする。

### 9. Session / Run Replay

Hermes Agentの実行履歴や軌跡データの考え方は、HOKUSAIでは「ワークフロー実行の再現性」として取り入れる。

HOKUSAIで重要なのは、エージェントの内面状態を保存することではなく、あるPRやフェーズで何が実行され、何が判断され、どの外部サービスに同期されたかを追跡できることである。

#### 9.1. 二重保存（SQLite + JSONL）

Hermes は session を SQLite（FTS5 全文検索付き）と JSONL の両方に保存している。HOKUSAI でも同じ二重保存を採用する。

- **SQLite (`workflow.db`)**: メタデータ + 検索インデックス。Operations Console / `hokusai list` / `hokusai status` で使う
- **JSONL (`<data_dir>/sessions/<workflow_id>.jsonl`)**: 1 イベント 1 行の生ログ。tool 呼び出し、外部サービス同期、エラー、人間判断を時系列で残す

両方を持つ理由:

- SQLite だけだと「LLM への full re-feed」「監査用 raw 確認」が辛い
- JSONL だけだと検索や集計が遅い
- 二重保存のコストは小さく、用途分離の恩恵が大きい

#### 9.2. 記録対象

- 実行profile
- コマンド
- 対象repository
- phase
- 外部サービス同期結果
- テスト結果
- PR番号
- レビューコメント
- エラーとリトライ

#### 9.3. フォーマット（ShareGPT互換）

JSONL のスキーマは **ShareGPT互換 role**（`system` / `human` / `gpt` / `tool`）とし、tool 呼び出しは Hermes と同じ XML 風タグで正規化する。

```jsonl
{"role": "tool", "content": "<tool_call>{\"name\":\"github.create_pr\",\"args\":{...}}</tool_call>", "timestamp": "2026-05-12T09:15:23+09:00"}
{"role": "tool", "content": "<tool_response>{\"pr_number\":42,\"url\":\"...\"}</tool_response>", "timestamp": "2026-05-12T09:15:24+09:00"}
```

理由: **保存形式だけを互換にしておく**ことで、将来 fine-tuning / 監査 replay / 学習データ生成が必要になったときの選択肢を残せる。生成パイプライン自体（trajectory_compressor / batch_runner）は HOKUSAI のスコープ外（§ HOKUSAIに入れない方がよい機能を参照）。

#### 9.4. profile ごとの保存

JSONL は **profile ごとに `<data_dir>/sessions/` 配下に保存** し、案件間で混ざらないようにする。これは Profile 並列実行対応 実装計画書（v0.3.0 マイルストーン）と整合する。

これにより、案件運用時の監査性とデバッグ性が上がる。

#### 9.5. 監査ログの保護要件

JSONL 生ログには、tool args / tool response として顧客情報、Notion ページ本文、PR コメント、外部サービスのレスポンス、場合によっては秘匿値に近い情報が混ざる可能性がある。そのため、Session / Run Replay は保存形式だけでなく、保護要件も同時に設計する。

最低限の要件:

- `<data_dir>/sessions/` は profile ごとに分離し、ディレクトリ権限は `700`、JSONL ファイル権限は `600` を推奨する
- token / authorization header / webhook URL / cookie / secret らしき値は保存前に redaction する
- export 機能を付ける場合は、redacted export を既定にし、raw export は明示オプションにする
- 保存期間を profile ごとに設定できるようにする（例: `retention_days: 90`）
- Operations Console で全文表示する場合は、raw JSONL ではなく redacted view を既定にする
- `profile doctor` で sessions ディレクトリの権限が緩すぎる場合に警告する

### 10. Delegation（並列サブエージェント）

Hermes の `delegate_task` は、子 AIAgent を spawn して **完全に独立した fresh conversation** で並列実行させる仕組み。HOKUSAI の以下のユースケースに直接転用できる。

#### 10.1. HOKUSAI での対象シナリオ

- **複数リポジトリの並列レビュー**: monorepo / multi-repo 構成で、各 PR を並列にレビューエージェントへ投げる
- **複数 Figma ファイル並列調査**: Phase 3（design）で複数のデザイン file を同時に読み込み、要約だけを親エージェントに戻す
- **複数 PR の並列状態取得**: `pr-status-fetch` cron で並列に GitHub API を叩く
- **Phase 5 内の並列実装**: 独立した複数モジュールを同時に実装させる（ただしリスク高、要慎重設計）

#### 10.2. 設計原則（Hermes と同じ）

- **Fresh conversation**: 子は親の会話履歴 / tool call 履歴を一切見ない。`goal` と `context` だけ明示的に渡す
- **結果は要約のみ親へ**: 子の生 conversation は親 context に流入しない（context 爆発を防ぐ）
- **並列度はデフォルト 3**: thread pool で実行、結果は task index 順にソート
- **Leaf level での再帰禁止**: 孫 delegate / clarify / memory write は不可（無限ネスト防止）

#### 10.3. 設定例

```yaml
profiles:
  company-a:
    delegation:
      enabled: true
      max_parallel: 3
      child_toolsets:
        - notion-read
        - github-pr      # PR 取得のみ、mutation は不可
        - figma-read
```

子に渡せる toolset は親より **必ず狭く制限** する（mutation 系を子から外す）。

#### 10.4. profile スコープ

delegation も profile 単位で許可 / 禁止する。child の toolset も親 profile の capability 範囲内に限定される。

#### 10.5. 採用判断

P2 候補。Profile / Hooks の実装が落ち着いた後、Phase 5 や cron の並列化要件が出てきた時点で導入を検討する。

## HOKUSAIに入れない方がよい機能

### 汎用メッセージングゲートウェイ

SlackやDiscordから何でも命令できるようにする機能は、HOKUSAIの責務を広げすぎる。通知やレビュー依頼程度なら有効だが、常駐チャットエージェント化は避ける。

### 音声 / TTS / 汎用UIインターフェース

HOKUSAIの価値は開発ワークフローの制御と追跡にある。音声やTTSは本質的な改善につながりにくく、優先度は低い。

### 自己成長型の人格・長期ユーザーモデル

案件運用で必要なのは、ユーザーの人格記憶ではなく、profile、プロジェクト規約、実行履歴、フェーズ成果物である。個人化を強めるより、案件ごとの再現性を重視する。

### 任意の自然言語cron

任意タスクを自然言語で定期実行できる機能は強力だが危険である。HOKUSAIでは、許可された保守ジョブだけを定義的に実行する方がよい。

### 非開発系インテグレーション

Home Assistant、Spotify、一般的な個人アプリ連携のような機能は、HOKUSAIの対象外とする。

### RL用Trajectory生成

研究用途としては有用だが、HOKUSAIのプロダクト価値に直結しない。必要になった場合でも、監査ログやrun replayから後で派生させる方がよい。

## 優先順位

### P0（v0.3.0 マイルストーン）

Profile 並列実行対応 実装計画書（`docs/hokusai-profile-parallel-execution-implementation-plan.md`）と同期。

- Profile / Workspace 分離
- profile ごとの DB、data_dir、worktree、外部サービス設定分離
- `--profile` 対応
- `profile list / show / doctor`

### P1（Profile 安定後の次マイルストーン）

- Hooks システム（3 層構造、`pre_tool_call` ブロック + `pre_llm_call` コンテキスト注入）
- Toolset / Capability 制御（3 階層 core / composite / platform）
- MCP / 外部ツール allowlist（server 単位 + tool 単位）
- Project Context Files（first-match-wins、project-local）

### P2

- 限定的な Scheduled Maintenance（2 モード + context_from チェーン）
- Phase Artifact Compression
- Session / Run Replay（SQLite + JSONL 二重保存、ShareGPT 互換フォーマット）
- Delegation（並列サブエージェント。Phase 5 や cron の並列化要件が出た時点で導入）

### P3

- Slack などへの限定通知
- Operations Console 上の profile 横断ビュー

## 推奨する設計方針

HOKUSAIがHermes Agentから取り入れるべきなのは、汎用エージェント化の方向ではなく、次の設計要素である。

- profile 単位の隔離（1 process = 1 profile 契約）
- ツール権限の明示（3 階層 toolset + tool 単位 allowlist）
- プロジェクト文脈の明示的読み込み（first-match-wins、project-local）
- **Hook 介入点による事前ブロック / 動的コンテキスト注入**
- 実行履歴の追跡性（SQLite + JSONL 二重保存、ShareGPT 互換）
- 保守ジョブの定義的実行（2 モード + context_from チェーン）
- 並列実行（Delegation、ただし fresh conversation + 親子 isolation）

つまり、HOKUSAIは「常駐して何でもやるAI」ではなく、「複数の開発案件を安全に並列運用するための、profile分離された開発ワークフロー基盤」として設計するべきである。

## 参考

### Hermes Agent 公式

- GitHub: https://github.com/NousResearch/hermes-agent
- Documentation: https://hermes-agent.nousresearch.com/docs

### 主要 docs ページ（参照元）

- Toolsets reference: `website/docs/reference/toolsets-reference.md`
- Tools feature: `website/docs/user-guide/features/tools.md`
- MCP feature: `website/docs/user-guide/features/mcp.md`
- MCP config reference: `website/docs/reference/mcp-config-reference.md`
- Context files: `website/docs/user-guide/features/context-files.md`
- Personality / SOUL.md: `website/docs/user-guide/features/personality.md`
- Hooks: `website/docs/user-guide/features/hooks.md`
- Delegation: `website/docs/user-guide/features/delegation.md`
- Cron: `website/docs/user-guide/features/cron.md`
- Memory: `website/docs/user-guide/features/memory.md`
- Profiles: `website/docs/user-guide/profiles.md`
- Trajectory format: `website/docs/developer-guide/trajectory-format.md`
- Session storage: `website/docs/developer-guide/session-storage.md`
- Agent loop: `website/docs/developer-guide/agent-loop.md`
- Gateway internals: `website/docs/developer-guide/gateway-internals.md`

### HOKUSAI 内関連ドキュメント

- `docs/hokusai-profile-parallel-execution-implementation-plan.md`（Profile 機能 v0.3.0 実装計画）
