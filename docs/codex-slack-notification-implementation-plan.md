# Slack 通知機能 実装計画

## 概要

HOKUSAI のワークフロー進行状況を Slack に通知できるようにする。

対象は、ワークフロー開始、Human-in-the-loop 待機、失敗、PR 作成、完了など、利用者が次のアクションを判断するために必要なイベントに絞る。Phase ごとの全通知はノイズが多くなりやすいため、初期実装では扱わない。

## 調査結果

現時点で Slack 通知の実装は存在しない。

ただし、通知を差し込むための構造は揃っている。

- ワークフロー実行は `hokusai/workflow.py` の `WorkflowRunner._run_stream_loop()` に集約されている
- `_run_stream_loop()` は `start()` と `continue_workflow()` の共通実行ループになっている
- イベントごとに LangGraph state を取得し、SQLite に保存している
- Human-in-the-loop 待機、最大イベント数到達、ループ検出、例外失敗などの停止理由が `interrupt_reason` として整理されている
- ワークフロー state には通知に必要な情報がある
  - `workflow_id`
  - `task_url`
  - `task_title`
  - `current_phase`
  - `phases`
  - `pull_requests`
  - `waiting_for_human`
  - `human_input_request`
  - `verification_errors`
- ダッシュボード実行も最終的には `hokusai start` / `hokusai continue` をサブプロセスで起動するため、CLI 側の通知実装でダッシュボード経由にも効く

## 実装方針

Slack Incoming Webhook を使う。

理由:

- Bot token や Slack SDK を導入しなくても通知できる
- HOKUSAI の用途では双方向操作より一方向通知が主目的
- 標準ライブラリ `urllib.request` で実装でき、依存追加が不要
- ワークフロー本体から独立した薄い通知クライアントとして扱いやすい

Webhook URL は YAML に直書きせず、環境変数参照を基本にする。

```yaml
notifications:
  slack:
    enabled: true
    webhook_url_env: HOKUSAI_SLACK_WEBHOOK_URL
    events:
      - workflow_started
      - waiting_for_human
      - workflow_failed
      - pr_created
      - workflow_completed
```

将来的に複数チャンネルへ出し分ける必要が出た場合は、`channels` または `targets` の配列形式へ拡張する。

## 通知対象イベント

初期実装で扱うイベントは以下に限定する。

| イベント | 通知タイミング | 目的 |
|---|---|---|
| `workflow_started` | 新規ワークフロー開始時 | 作業開始の共有 |
| `waiting_for_human` | Human-in-the-loop 待機で停止した時 | 人間の判断が必要なことを知らせる |
| `workflow_failed` | 例外、ループ検出、最大イベント数到達、失敗状態で停止した時 | 失敗対応を促す |
| `pr_created` | PR が作成または検出された時 | レビュー対象PRを知らせる |
| `workflow_completed` | Phase 10 完了時 | 作業完了の共有 |

`workflow_started` は `WorkflowRunner.start()` で初期 state 保存後に送る。

`waiting_for_human` / `workflow_failed` / `workflow_completed` は `_run_stream_loop()` の戻り値確定直前または直後に、最終 state と `interrupt_reason` を見て送る。

`pr_created` は `pull_requests` の件数が増えたタイミングで送る。初期実装では Phase 8a の完了後に `pull_requests` を見て通知するのが最も明確。

## 追加・変更するファイル

### `hokusai/config/models.py`

通知設定用 dataclass を追加する。

案:

```python
@dataclass
class SlackNotificationConfig:
    enabled: bool = False
    webhook_url_env: str = "HOKUSAI_SLACK_WEBHOOK_URL"
    events: list[str] = field(default_factory=lambda: [
        "waiting_for_human",
        "workflow_failed",
        "pr_created",
        "workflow_completed",
    ])
    timeout: float = 5.0


@dataclass
class NotificationConfig:
    slack: SlackNotificationConfig = field(default_factory=SlackNotificationConfig)
```

`WorkflowConfig` に `notifications: NotificationConfig` を追加する。

### `hokusai/config/loaders.py`

`_parse_notifications_config()` を追加する。

バリデーション方針:

- `notifications` が dict でなければデフォルト
- `slack.enabled` は bool のみ採用
- `webhook_url_env` は空文字ならデフォルト
- `events` は既知イベントのみ採用
- 不正なイベントだけが指定された場合はデフォルトに戻す
- `timeout` は 1 以上 30 以下程度に丸める

### `hokusai/config/manager.py`

`_parse_notifications_config()` を呼び、`WorkflowConfig` に渡す。

また、`config_dict.pop(...)` の除外対象に `notifications` を追加する。

### `hokusai/integrations/notifications/slack.py`

Slack Incoming Webhook 送信用モジュールを追加する。

責務:

- 設定と環境変数から Webhook URL を解決する
- Slack message payload を作る
- `urllib.request` で POST する
- 送信失敗を例外として外へ漏らさず、logger warning にする
- Webhook URL をログに出さない

実装イメージ:

```python
def notify_slack(event: str, state: dict, *, reason: str | None = None) -> None:
    config = get_config().notifications.slack
    if not config.enabled or event not in config.events:
        return
    webhook_url = os.environ.get(config.webhook_url_env)
    if not webhook_url:
        logger.warning("Slack通知は有効ですが webhook URL 環境変数が未設定です")
        return
    payload = build_payload(event, state, reason=reason)
    post_webhook(webhook_url, payload, timeout=config.timeout)
```

### `hokusai/workflow.py`

`WorkflowRunner` から通知を呼ぶ。

候補:

- `start()` で初期 state 保存後に `workflow_started`
- `_run_stream_loop()` のイベント処理後に、前回 state と現在 state を比較して `pr_created`
- `_run_stream_loop()` の終了時に `interrupt_reason` と最終 state を見て `waiting_for_human` / `workflow_failed` / `workflow_completed`
- 例外ハンドリング内で `workflow_failed`

通知失敗でワークフローを失敗させないことを徹底する。

## メッセージ設計

Slack には短く、次のアクションが分かる情報だけを出す。

初期実装はプレーンテキストの mrkdwn フォーマットで送る。Block Kit 化は Phase 2 として後追いで行う（[Block Kit 対応（Phase 2）](#block-kit-対応phase-2) 参照）。

例: Human-in-the-loop 待機

```text
HOKUSAI: 人間の判断待ち

Workflow: wf-1234abcd
Task: <task_url>
Phase: 7
Reason: branch_hygiene

Next: hokusai continue wf-1234abcd
```

例: PR 作成

```text
HOKUSAI: PR を作成しました

Workflow: wf-1234abcd
Task: <task_url>
PRs:
- Backend: <https://github.com/example/backend/pull/123|PR #123>
- Frontend: <https://github.com/example/frontend/pull/45|PR #45>
```

例: 失敗

```text
HOKUSAI: ワークフローが停止しました

Workflow: wf-1234abcd
Task: <task_url>
Phase: 6
Reason: loop_detected
Error: ...
```

### Block Kit 対応（Phase 2）

最小実装が安定して動くようになった後、Block Kit 形式によるリッチ表示を追加する。
位置付けはあくまで「視認性の改善」であり、最小実装の完了条件には含めない。

切り替え方針:

- payload builder を `build_text_payload()` と `build_block_kit_payload()` の 2 系統にする
- `SlackNotificationConfig.format` に `"text" | "block_kit"` を追加し、デフォルトは後方互換のため `"text"`
- 受信側 Slack の表示崩れを避けるため、テキスト fallback は必ず併送する

例: PR 作成イベントの Block Kit ペイロード

```json
{
  "blocks": [
    {
      "type": "header",
      "text": { "type": "plain_text", "text": "🚀 HOKUSAI: PR を作成しました" }
    },
    {
      "type": "section",
      "fields": [
        { "type": "mrkdwn", "text": "*Workflow:*\nwf-1234abcd" },
        { "type": "mrkdwn", "text": "*Task:*\n<TASK_URL|タスク>" },
        { "type": "mrkdwn", "text": "*Backend PR:*\n<PR_URL|#123>" },
        { "type": "mrkdwn", "text": "*Frontend PR:*\n<PR_URL|#45>" }
      ]
    }
  ],
  "text": "HOKUSAI: PR を作成しました (wf-1234abcd)"
}
```

イベント別のヘッダ絵文字対応:

| イベント | 絵文字 | ヘッダ文言 |
|---|---|---|
| `workflow_started` | 🛫 | HOKUSAI: ワークフロー開始 |
| `waiting_for_human` | 👀 | HOKUSAI: 人間の判断待ち |
| `pr_created` | 🚀 | HOKUSAI: PR を作成しました |
| `workflow_failed` | ❌ | HOKUSAI: ワークフローが停止しました |
| `workflow_completed` | ✅ | HOKUSAI: ワークフロー完了 |

## セキュリティ方針

- Webhook URL は YAML に保存しない
- `HOKUSAI_SLACK_WEBHOOK_URL` などの環境変数で渡す
- Webhook URL をログ、例外メッセージ、Slack本文に出さない
- 設定ページで `webhook_url` のようなキーが直書きされた場合は、既存のトークン混入警告の対象にする
- 通知送信は best effort とし、失敗してもワークフロー本体を止めない

### `_detect_token_like_values` への Slack Webhook URL パターン追加

既存の `scripts/dashboard.py` の `_detect_token_like_values` は、設定値中のトークン直書きを検出する仕組みになっている。
ここに Slack Incoming Webhook URL のパターンを追加し、YAML に webhook URL が直書きされた場合に警告する。

追加するパターン:

```python
(
    r"^https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+$",
    "Slack Incoming Webhook URL",
)
```

検出された場合の挙動:

- ダッシュボードの設定検証で警告として返す
- 送信自体は止めない（ユーザの自己責任での運用は許容）

テスト:

- `tests/integrations/test_validate_config_warnings.py` に Slack Webhook URL 検出ケースを追加

## ダッシュボード対応

初期実装ではダッシュボード専用 UI は不要。

理由:

- ダッシュボードは `hokusai start` / `hokusai continue` をサブプロセスで起動している
- CLI 側に通知を入れればダッシュボード実行にも反映される
- Webhook URL 入力 UI を作るとシークレット管理責務が増える

追加する場合でも、最初は「Slack通知が有効か」「環境変数が設定されているか」の表示に留める。

## テスト方針

### 単体テスト

追加候補:

- `tests/test_slack_notification.py`
  - `enabled=false` なら送信しない
  - `events` に含まれないイベントは送信しない
  - webhook URL 環境変数がない場合は送信しない
  - payload に `workflow_id` / `task_url` / PR URL が含まれる
  - 送信例外が外に漏れない
- `tests/test_codex.py` または設定系テスト
  - `WorkflowConfig` に `notifications` がある
  - `_parse_notifications_config()` が不正値をデフォルトに戻す
- `tests/test_workflow.py`
  - `_run_stream_loop()` 終了時に適切な通知イベントが呼ばれる
  - 例外時に `workflow_failed` が呼ばれる

### 実行確認

依存追加なしで済む想定なので、既存の pytest で確認する。

```bash
uv run pytest tests/test_workflow.py tests/test_slack_notification.py
```

## 実装ステップ

1. 設定モデルを追加する
   - `SlackNotificationConfig`
   - `NotificationConfig`
   - `WorkflowConfig.notifications`

2. 設定パーサを追加する
   - `_parse_notifications_config()`
   - `create_config_from_env_and_file()` への組み込み

3. Slack 通知クライアントを追加する
   - payload builder
   - webhook sender
   - best effort error handling

4. `WorkflowRunner` に通知フックを追加する
   - start
   - PR 作成検出
   - waiting / failed / completed

5. テストを追加する
   - 設定パース
   - Slack payload
   - 送信スキップ条件
   - workflow hook
   - Slack Webhook URL の直書き検出

6. README / example config を更新する
   - `configs/example-github-issue.yaml`
   - `README.md`
   - `README_JP.md`

## Definition of Done（最小実装）

- `notifications.slack.enabled: true` と `HOKUSAI_SLACK_WEBHOOK_URL` で通知が送れる
- 通知対象イベントを YAML で制御できる
- Slack 通知失敗でワークフロー本体が失敗しない
- ダッシュボード経由の start / continue でも同じ通知が送られる
- Webhook URL がログやDBに保存されない
- `_detect_token_like_values` が Slack Webhook URL の YAML 直書きを警告する
- README / README_JP に Slack 通知設定の使い方が追記されている
- example config に `notifications.slack` の設定例が追記されている
- 主要テストが通る

`connection_status` への Slack 表示追加と `hokusai connect slack` は、最小実装の完了条件には含めない。
これらは設定確認・導入補助の改善として後続対応に分離する。

## 着手前に合意すべき意思決定項目（Open Questions）

実装着手前に、以下については明示的に方針を確定させておく。
あいまいなまま進めると後段で手戻りが発生するため、最小実装の前段で合意を取る。

1. **デフォルト通知イベントの選定**
   - `workflow_started` / `workflow_completed` をデフォルト ON にするか OFF にするか
   - ノイズ過多になる懸念がある一方、進行が見えないと利用者が不安になる
   - 暫定案: `waiting_for_human` / `workflow_failed` / `pr_created` / `workflow_completed` を ON、`workflow_started` は OFF

2. **複数チャンネル運用の対応スコープ**
   - イベント別に異なる Webhook URL に出し分けるか
   - 本 PR では単一 Webhook に絞り、複数チャンネルは将来拡張へ送る
   - 設計上は将来 `channels` / `targets` の配列形式へ拡張可能な構造にしておく

3. **メンション対象の表現方法**
   - `@channel` / `@here` / 個別ユーザーをイベント別に指定したいか
   - Slack の user_id 指定が必要なケース（`<@U12345>`）を最小実装で扱うか
   - 暫定案: 最小実装ではメンション無し、将来 `mention` フィールドで追加

4. **Mattermost 互換性の扱い**
   - 同じ Webhook 形式で動くが、本 PR の動作保証範囲に含めるか
   - 暫定案: 動作保証はしないが、互換性が壊れる変更は避ける

5. **MCP 統合との関係**
   - Slack MCP（`mcp__slack__*` ツール）と Webhook 直接送信のすみ分け
   - 暫定案: 本 PR は Webhook 直接送信のみ。MCP 連携はダッシュボード側のシークレット管理整備とセットで別 PR

6. **`hokusai connect slack` の扱い**
   - 既存 `hokusai connect` パターンに乗せて、状態確認 / テスト送信 CLI を提供するか
   - 暫定案: 最小実装では入れず、後続対応で `connection_status` と合わせて設計する。OAuth 系の認証フローは持たない

7. **Block Kit 化のタイミング**
   - 最小実装のテキスト送信と同時に出すか、Phase 2 として分離するか
   - 暫定案: Phase 2 として分離。最小実装の完了条件には含めない

各項目の暫定案で進めて差し支えなければ、レビュアからの no-objection をもって着手する。

## 将来拡張

- 複数 Slack Webhook への通知
- リポジトリ別チャンネル出し分け
- メンション設定
- 通知テンプレートのカスタマイズ
- Slack Block Kit 形式のリッチ表示（Phase 2 として本計画に組み込み済み）
- `connection_status` への Slack 追加
- `hokusai connect slack` による設定状況表示とテスト送信
- ダッシュボードの接続状態パネルへの Slack 追加
