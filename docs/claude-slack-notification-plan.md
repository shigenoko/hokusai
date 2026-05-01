# Slack 通知機能 実装計画書

作成日: 2026-05-01
作成者: Claude Opus 4.7（HOKUSAI 上での調査結果に基づく）

## 1. 背景と目的

HOKUSAI のワークフローは現在、進捗や完了を **標準出力 (`print_workflow_completed`) と `logger`** にしか出していない。長時間動く 10 フェーズの自動化ワークフローを別タスクと並行で回す運用では、人間レビュー待ち（Phase 8 統合レビューループ）や失敗発生にユーザーが気付けず、PR が放置されるリスクがある。

外部通知の最初の宛先として **Slack** を追加し、以下を達成する:

- PR draft 作成・人間レビュー待ち・ワークフロー失敗を即座にチームへ通知
- 既存の `connection_status` / `hokusai connect` パターンに乗せて、認証状態・テスト送信もダッシュボードと CLI の両方から扱えるようにする
- セキュリティ系の既存仕組み（トークン直書き警告 / `_safe_config_path` 等）と整合する形で導入する

## 2. ゴールと非ゴール

### ゴール

- Slack Incoming Webhook を使った通知を `hokusai/integrations/slack.py` として追加
- ワークフロー進行中の主要イベント（PR draft / human review 待ち / 失敗 / 完了）で通知発火
- `connection_status.py` のレジストリに `slack` を追加し、ダッシュボードと CLI で状態確認可能にする
- `hokusai connect slack` で webhook URL の検証＋テスト送信が行える
- 設定 (`SlackConfig`) は環境変数経由で webhook URL を受け取り、YAML への直書きは検出して警告する

### 非ゴール

- Slack Bot Token を使ったインタラクティブ機能（DM・スレッド更新・ボタン操作）
- Mattermost / Discord / Teams など他チャットツールへの汎用通知抽象化
- 通知履歴の永続化と再送（idempotency）
- ワークフロー外イベント（CI 結果・依存スキャナー等）の通知

これらは将来別 PR で扱う。

## 3. 現状調査の要約

| 項目 | 状況 |
|---|---|
| 既存通知系 | 標準出力と `logger` のみ。外部通知ゼロ |
| 既存依存 | `langgraph` / `langchain-core` / `pydantic` / `pyyaml`。HTTP クライアントは未導入 |
| 既存連携パターン | `hokusai/integrations/connection_status.py` のサービスレジストリ（`gh` / `glab` / `notion_mcp` / `codex` / `claude` / `jira` / `linear`） |
| ワークフロー構造 | LangGraph 上の 10 フェーズ。`hokusai/nodes/phase*.py` と `workflow.py:run_step` |
| 設定 | `WorkflowConfig` 配下にカテゴリ別 dataclass（`TaskBackendConfig` / `GitHostingConfig` / `CrossReviewConfig` 等） |
| セキュリティ | `_detect_token_like_values` がトークン直書きを警告、`_safe_config_path` がパストラバーサル防御済み |

## 4. 方式比較と推奨

| 方式 | 認証 | 機能 | 工数 | 推奨度 |
|---|---|---|---|---|
| **Incoming Webhook**（推奨） | URL ベース | テキスト + Block Kit、リッチ表示可 | 小 | ⭐⭐⭐ |
| Bot Token + Web API | OAuth Bot Token | DM / スレッド更新 / インタラクティブ | 中〜大 | 必要になった時 |
| MCP 経由（Notion MCP と同じ） | `claude mcp add slack` | Claude Code 経由 | 中 | 既存 MCP 統合と整合させたい場合 |

**推奨: Incoming Webhook**

選定理由:

- 外部 SDK 不要（stdlib `urllib.request` または既存 `httpx` 追加）
- 設定は webhook URL 1 つだけ
- Block Kit でリッチ表示は十分可能
- 漏洩時のリスクはトークン rotation で対処可（OAuth Bot Token と異なり既存資産の権限影響なし）
- 将来 Bot Token に移行する場合も、本体側のコード変更は限定的

## 5. アーキテクチャと構成

### ファイル構成

```text
hokusai/integrations/slack.py            ← 新規: Webhook クライアント + 状態判定
hokusai/config/models.py                 ← 拡張: SlackConfig dataclass を追加
hokusai/integrations/connection_status.py  ← 拡張: slack をレジストリに追加
hokusai/cli/commands/connect.py          ← 拡張: hokusai connect slack を追加
hokusai/nodes/phase8/*.py                ← フック挿入（PR draft / human review）
hokusai/nodes/phase10_record.py          ← フック挿入（workflow_completed）
hokusai/workflow.py                      ← フック挿入（workflow_started / phase_failed）
configs/example-*.yaml                   ← Slack 設定例の追記
tests/integrations/test_slack.py         ← 新規テスト
docs/claude-slack-notification-plan.md   ← 本文書
```

### 設定 (SlackConfig dataclass)

```python
@dataclass
class SlackConfig:
    enabled: bool = False
    # webhook URL は YAML 直書きを禁止する。env 名のみ指定し、実体は環境変数から取得。
    webhook_url_env: str | None = None
    default_channel: str | None = None  # webhook 側のデフォルトを使う場合は None
    # 通知トリガーのイベント名のリスト
    notify_on: list[str] = field(default_factory=lambda: [
        "pr_drafted",
        "human_review_needed",
        "phase_failed",
    ])
    # メンション対象（イベント別）
    mention: dict[str, str] = field(default_factory=dict)
    # 送信タイムアウト秒
    timeout: int = 10
```

### YAML 設定例

```yaml
slack:
  enabled: true
  webhook_url_env: HOKUSAI_SLACK_WEBHOOK
  default_channel: "#hokusai"
  notify_on:
    - workflow_started
    - pr_drafted
    - human_review_needed
    - phase_failed
    - workflow_completed
  mention:
    phase_failed: "@channel"
    human_review_needed: "@reviewer-team"
  timeout: 10
```

## 6. 通知イベント設計

| イベント名 | フック箇所 | 重要度 | デフォルト有効 | メッセージ概要 |
|---|---|---|---|---|
| `workflow_started` | `workflow.py:run_step` の入口 | 低 | ❌ | task_url + phase 開始 |
| `pr_drafted` | Phase 8 末尾（draft PR 作成完了直後） | ⭐⭐⭐ | ✅ | PR URL + 概要 |
| `human_review_needed` | Phase 8 統合レビューループ → human レビュー待ちに遷移 | ⭐⭐⭐ | ✅ | PR URL + 「review お願いします」 |
| `phase_failed` | `last_environment_error` セット時 / 各 phase の error path | ⭐⭐ | ✅ | エラー要約 + workflow_id |
| `pr_approved` | Phase 9 で human approval が完了 | ⭐ | ❌ | PR URL + approval 報告 |
| `workflow_completed` | Phase 10 record の完了直後 | ⭐ | ❌ | task_url + 累計時間 |

実装上はイベント名を文字列定数として `hokusai/integrations/slack.py` 内に置き、各 node から `slack_notifier.send(event=..., payload=...)` を呼ぶ。`enabled=False` なら no-op、`event not in notify_on` なら no-op。

## 7. メッセージフォーマット（Block Kit）

例: `pr_drafted` イベント

```json
{
  "blocks": [
    {
      "type": "header",
      "text": { "type": "plain_text", "text": "🚀 PR draft 作成完了" }
    },
    {
      "type": "section",
      "fields": [
        { "type": "mrkdwn", "text": "*Task:*\n<TASK_URL|タスク>" },
        { "type": "mrkdwn", "text": "*PR:*\n<PR_URL|#1234>" },
        { "type": "mrkdwn", "text": "*Workflow:*\nwf-xxxxx" },
        { "type": "mrkdwn", "text": "*Branch:*\nfeature/foo" }
      ]
    }
  ]
}
```

イベントごとに header の絵文字とタイトルを変える（例: 失敗 = `❌` / human review = `👀` / 完了 = `✅`）。

## 8. セキュリティ設計

| 観点 | 対策 |
|---|---|
| Webhook URL の漏洩 | YAML 直書きを禁止し、`webhook_url_env` で環境変数名のみを保存。実体は `os.environ` から実行時に取得 |
| YAML 直書き検出 | 既存 `_detect_token_like_values` のトークンパターンに `https://hooks.slack.com/services/...` を追加し、誤って YAML に書いた場合は警告 |
| 接続状態の取得 | `connection_status.py` の `_check_slack` は **テスト送信を行わず**、`webhook_url_env` の指定有無と環境変数の設定有無のみを確認（network 不要・冪等） |
| `hokusai connect slack` のテスト送信 | `--test` オプション指定時のみ実際の webhook へ POST。デフォルトはドライラン |
| 失敗時のフォールバック | webhook 送信失敗（タイムアウト・401・5xx）はログに記録するが、ワークフロー本体は止めない（通知の失敗で本処理を巻き添えにしない） |
| Rate limit | Slack の rate limit (1 webhook あたり 1msg/sec 程度) に当たった場合は warn ログだけ出して握り潰す |

## 9. `connection_status` への追加

新サービス `slack` をレジストリに追加。`SERVICE_METADATA` を拡張:

```python
SERVICE_METADATA["slack"] = {
    "label": "Slack",
    "category": "notification",   # 新カテゴリ
    "required_for": ["notification"],
}
```

`_check_slack` の判定ルール:

| 条件 | status |
|---|---|
| `slack.enabled=False` | `disabled` |
| `webhook_url_env` 未指定 | `not_installed` |
| 環境変数が未設定 | `not_authenticated` |
| 環境変数あり | `connected` |

ダッシュボードと `hokusai connect --status` の両方に自動で出る。

## 10. CLI: `hokusai connect slack`

`hokusai/cli/commands/connect.py` の `SUPPORTED_SERVICES` に追加:

```python
"slack": {
    "label": "Slack",
    "cli": None,                            # 専用 CLI なし
    "status_command": None,                  # status はネイティブにチェック
    "auth_command": None,                    # 認証コマンド不要
    "install_url": "https://api.slack.com/messaging/webhooks",
    "test_command_hint": "hokusai connect slack --test",
}
```

挙動:

- `hokusai connect slack` → 設定状況を表示し、未設定なら案内（YAML への `webhook_url_env` 追加 + 環境変数の export 手順）
- `hokusai connect slack --test` → 環境変数から webhook URL を取得して、テストメッセージを送信

## 11. 段階的実装計画（phase-wise）

| フェーズ | 作業 | 工数 | 完了基準 |
|---|---|---|---|
| **P1** | Webhook クライアント (`slack.py`) + `SlackConfig` 追加 + `_detect_token_like_values` の Slack URL 追加 | 2〜3h | 単体テストで webhook 送信のモック成功 / 設定 YAML が読める |
| **P2** | `connection_status.py` に slack 追加。ダッシュボードに状態表示が出ることを確認 | 1h | 手元検証でカード表示・各状態 (`disabled`/`not_installed`/`not_authenticated`/`connected`) が正しい |
| **P3** | `hokusai connect slack` / `slack --test` の CLI コマンド | 1〜2h | `--test` で実 Slack に届く |
| **P4** | Phase 8（PR draft / human review）への通知フック | 2〜3h | フック発火の単体テスト + 実 Slack で確認 |
| **P5** | `phase_failed` への通知フック + ワークフロー完了通知 | 1〜2h | エラー注入で通知が出る単体テスト |
| **P6** | テスト網羅 + README 更新 + 提案ドキュメントへの反映 | 1〜2h | `pytest tests/` グリーン / README に Slack 連携が追記される |

**合計: 8〜13h（実働 1〜1.5 日）**

優先度を下げる場合は P1 + P2 + P4 だけでも実用価値あり。

## 12. テスト戦略

| レイヤ | 対象 | テストで担保すること |
|---|---|---|
| ユニット | `SlackWebhookClient.send` | URL 形式チェック / タイムアウト / 5xx 失敗時に raise しない / Block Kit JSON 構造 |
| ユニット | `_check_slack`（connection_status） | 4 種の status 分岐すべて |
| ユニット | `_detect_token_like_values` | `https://hooks.slack.com/services/...` を検出 |
| ユニット | 各 node のフック呼び出し | `monkeypatch` で `slack_notifier.send` を捕捉し、想定イベント・ペイロードで呼ばれている |
| 結合 | `hokusai connect slack --test` | 環境変数経由 + dry-run |

## 13. 既存ドキュメントとの関係

- `docs/dashboard-connection-settings-proposal.md` の「次に検討する候補」に Slack 通知が追加される想定。本ドキュメント実装後に当該箇所も更新する。
- README の「Standard 機能一覧」と「Connection Status」セクションに Slack を追記する。

## 14. 受け入れ基準（Definition of Done）

- [ ] `hokusai connect --status` の出力に Slack が表示される
- [ ] ダッシュボード `/settings` の「サービス接続状態」に Slack カードが表示される
- [ ] `hokusai connect slack --test` で実 Slack に届くテストメッセージが送れる
- [ ] Phase 8 の PR draft 完了時、設定済み Slack に PR URL を含む通知が届く
- [ ] human review 待ちに遷移したとき通知が届く
- [ ] フェーズ失敗時、エラー要約が通知され、ワークフロー自体は失敗しない
- [ ] `pytest tests/` 全件パス（追加分含む）
- [ ] `_detect_token_like_values` が webhook URL の YAML 直書きを警告する
- [ ] README / `docs/dashboard-connection-settings-proposal.md` に追記済み

## 15. オープンクエスチョン

1. **デフォルト通知イベントの選定**: `workflow_started` / `workflow_completed` をデフォルト ON にするか OFF にするか。ノイズになる懸念あり
2. **複数チャンネル運用**: イベント別に異なるチャンネル URL を切り替える機能は本 PR で入れるか、後続 PR にするか
3. **メンション対象の表現**: `@channel` / `@here` / specific user の指定方法（Slack の user_id 指定が必要なケースあり）
4. **Mattermost 互換**: 同じ webhook 形式で動くが、本 PR の動作保証範囲に含めるか
5. **MCP 統合との関係**: Slack MCP（`mcp__slack__*` ツール）との関係。連携可能だが、本 PR では Webhook 直接送信のみとし、MCP は別 PR で扱うのが Phase D（Web シークレット管理）と整合する

## 16. 次の一手

レビュー観点で問題なければ、**P1（Webhook クライアント + SlackConfig + トークン警告パターン追加）から着手**する。先に方式（Webhook）と通知イベントセットだけ合意しておけば、後段のフック挿入は機械的に進められる。

実装着手の合意があれば feature ブランチを切ってから順次 PR 化する。
