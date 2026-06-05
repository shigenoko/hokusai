# HOKUSAI 実運用 Runbook（Production Readiness）

GitHub Issue + GitHub ホスティング + Notion ダッシュボード構成で HOKUSAI を
小規模実運用に乗せるための運用手順書。「[実運用前 最終実装計画書（Production
Readiness）](https://app.notion.com/p/HOKUSAI-Production-Readiness-2026-06-04-37585495565d817abdc8e39c28ce7731)」
の **T6（バージョン固定）** / **T8（起動前チェック）** を中心に、T1（バックアップ）
運用・障害対応・T9（受け入れ）を統合する。

> このドキュメントは「動く」から「運用に乗せる」への移行を支える運用面の手順を
> まとめたもの。コードの仕様は README / CHANGELOG を参照。

---

## 0. パイロット開始ゲート チェックリスト

GitHub 構成での小規模実運用を始める前に、以下を満たすこと。

- [ ] **T1** State DB バックアップ手段が用意され、cron スナップショットが回っている（§3）
- [ ] **T6** 本番のバージョンが固定され、アップグレード手順がある（§1）
- [ ] **T8** `hokusai start` 前チェックが手順化され、Slack 通知が有効（§2）
- [ ] **T9** 案件相当の issue で 1 本フル完走（Phase 1〜10）を確認済み（§5）

> T2（outbox 可視化）/ T3（cleanup --purge）/ T5（同時実行ガード）は運用ルールと
> 既存機能でカバーし、規模拡大時にコード対応する（計画書の 🟨 区分）。T4
> （enforcement ガード）/ T7（シークレット高度管理）は enforcement on・本番昇格時の
> 条件付き（🟦 区分）。

---

## 1. バージョン固定 & アップグレード手順（T6）

Alpha（`Development Status :: 3 - Alpha`）であり、v0.x はマイナー更新で破壊的変更を
含み得る。本番は必ずバージョンを固定する。

### 1.1 インストール（バージョン固定）

```bash
# uv（推奨）
uv pip install "hokusai-flow==0.11.0"

# pip
pip install "hokusai-flow==0.11.0"
```

依存を再現可能にするため、requirements / lock にも固定値を残す。

```text
# requirements.txt 例
hokusai-flow==0.11.0
```

### 1.2 バージョン確認

```bash
python -c "import hokusai; print(hokusai.__version__)"   # => 0.11.0
```

### 1.3 アップグレード手順（検証してから本番へ）

1. **検証環境**で新バージョンをインストール
2. 既存 state を使う前に **バックアップを取得**（§3.1）
3. `pytest`（リポジトリ運用なら）/ スモークの `hokusai list` / `hokusai profile doctor <name> --deep` が通ることを確認
4. CHANGELOG の **破壊的変更**・マイグレーション注記を確認
5. 問題なければ本番のピン留めバージョンを更新し、再インストール
6. 本番でも適用直後に `hokusai profile doctor <name> --deep` でヘルス確認

> ロールバックが必要になった場合は、ピン留めを旧バージョンへ戻して再インストールし、
> 必要なら §3.2 の restore で state を戻す。

---

## 2. `hokusai start` 前チェック（T8）

Notion 連携の最頻出トラブルは **DB share 漏れ / env の DB ID ずれ**（dogfooding
§1・§14）。起動前チェックを必ず実施する。

### 2.1 起動前チェック手順

```bash
# 1. profile の静的検査 + runtime 運用ヘルス検査（live API なし）
hokusai --profile <name> profile doctor <name> --deep
```

`--deep` は以下を SQLite から集約して表示する（live Notion 呼び出しはしない）:

- **Notion 同期 outbox の pending / 永続 error 件数**
- 運用ギャップ（`notion_outbox_pending` 等の検出）

`pending` / `error` が出ている場合は、本番投入前に §4 の障害対応で解消する。

```bash
# 2. Notion DB share の事前確認は start 冒頭で自動実行される
#    （M0.2 / Issue #82）。各 DB に integration が share されていない、
#    env の DB ID が古い / ゴミ箱を指す等は warning として表示される。
hokusai --profile <name> start <github_issue_url>
```

> `hokusai start` は冒頭で `check_db_share_health()` を **query probe**（read-only）
> で実行する（dogfooding §14 で retrieve probe の false positive を修正済み）。
> warning が出たら start を中断し、§2.2 を確認すること。

### 2.2 Notion DB share / env チェックリスト

- [ ] `notion-setup` で生成した **各 DB**（Workflows / Pull Requests / Review Issues 等）を
      開き、**⋯ → Add connections** で integration を share している
- [ ] env の各 DB ID（`HOKUSAI_NOTION_WORKFLOWS_DB_ID` 等）が
      **現行の live DB** を指している（ゴミ箱・旧 DB を指していない）
- [ ] `HOKUSAI_NOTION_API_TOKEN`（または profile 個別の token env）が設定済み
- [ ] 必要なら `hokusai notion-migrate-schema` でスキーマ drift を解消（Operator 等の欠落）

### 2.3 Slack 通知（無人滞留の早期検知）

無人運用での「気付けない失敗」を防ぐため Slack 通知を有効化する。

```bash
export HOKUSAI_SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T.../B.../..."
```

```yaml
notifications:
  slack:
    enabled: true
    webhook_url_env: HOKUSAI_SLACK_WEBHOOK_URL
    events:
      - waiting_for_human    # 人間レビュー待ちで停止
      - workflow_failed      # 失敗
      - pr_created           # PR 作成
      - workflow_completed   # 完了
```

> サポートイベント: `workflow_started` / `waiting_for_human` / `workflow_failed` /
> `pr_created` / `workflow_completed`。送信失敗は workflow を止めない（fail-open）。

---

## 3. State DB バックアップ運用（T1）

`~/.hokusai/`（または profile の `data_dir`）配下の `workflow.db` / `checkpoint.db`
が壊れると、進行中 workflow と監査ログ（`audit_logs`）を失う。`hokusai backup` は
SQLite online backup API を使うため **workflow 実行中でも安全**に取得できる。

### 3.1 手動バックアップ

```bash
# スナップショット作成（既定 <data_dir>/backups/<YYYYMMDD-HHMMSS>/ に出力）
hokusai --profile <name> backup --label "before-upgrade"

# 一覧（新しい順）
hokusai --profile <name> backup --list

# 作成後に新しい 14 世代だけ残して刈り込み
hokusai --profile <name> backup --keep 14
```

### 3.2 リストア

```bash
# 最新から復元（現 DB は *.pre-restore に退避される）
hokusai --profile <name> restore --from latest

# 特定スナップショットから（確認プロンプトを省略）
hokusai --profile <name> restore --from 20260604-202236 --yes
```

> restore は適用前に各スナップショット DB を `PRAGMA integrity_check` で検証し、
> 現 DB（と `-wal` / `-shm`）を `*.pre-restore` に退避してから配置する。途中失敗時は
> まとめてロールバックし、ロールバック自体が失敗した場合はエラーに明示される。

### 3.3 cron 定期スナップショット（推奨）

```bash
# 毎日 03:00 に profile-aware スナップショット、14 世代保持
0 3 * * * /usr/bin/env hokusai --profile <name> backup --keep 14 >> ~/.hokusai/logs/backup.log 2>&1
```

- 複数 profile を運用する場合は profile ごとに行を分ける
- バックアップ先（`<data_dir>/backups` または `--out`）は**別ディスク / 別ボリューム**に
  置くと、ディスク障害時にも残る

### 3.4 復旧訓練

四半期に一度など、定期的に「最新スナップショットから別環境へ restore →
`hokusai list` で workflow が見えること」を確認しておく（バックアップが
“実際に戻せる”ことの確認）。

---

## 4. 障害対応

### 4.1 Notion 同期 outbox の滞留（pending / 永続 error）

**症状**: `profile doctor --deep` で `outbox pending > 0` または `error > 0`。
Notion ダッシュボードが更新されない。

**切り分けと対応**:

1. §2.2 のチェックリストで **DB share / env / スキーマ** を確認
2. 原因（DB がゴミ箱 / env が旧 DB / integration 未接続 / スキーマ drift）を解消
3. outbox は次回の workflow イベント dispatch 時に **自動で drain（再送）** される。
   `hokusai continue <workflow-id>` 等の次イベントで再送を確認する
4. Operations Console（`hokusai dashboard`）の outbox サマリで pending / error が
   減っていることを確認

> outbox は idempotency_key で重複排除され、永続失敗は別テーブル（`notion_sync_errors`）に
> 分離される。**root cause を直さずに件数だけ消そうとしない**こと。

### 4.2 cleanup 後に同じ issue で start できない（stranded workflow）

cleanup 後に同一 issue で再 start すると worktree 欠落でエラーになる場合がある。

- 当面の運用: 同一 issue を再度回す前に、対象 workflow が完了 / cancel 済みであることを
  確認する（`hokusai list` / `hokusai status <id>`）
- `hokusai cleanup --stale --dry-run` で削除対象の worktree を事前確認できる
- どうしても残骸が消えない場合は、規模拡大フェーズで計画書 T3（`cleanup --purge`）の
  実装を行う

### 4.3 同一 task_url の同時実行（複数人運用）

同一 task_url を同一 profile で**並行実行するのは非サポート**。

- 運用ルール: 同じ issue を複数人で同時に start しない
- 人 / プロジェクト単位で **profile を分離**して衝突を避ける
- 規模拡大フェーズで計画書 T5（同時実行ガード）の実装を検討

---

## 5. 受け入れ E2E（T9）

Phase 5 以降（実装〜PR 完走）の自動 E2E は薄いため、**案件相当の issue で 1 本
フル完走**させて受け入れとする。パイロット開始前に必ず実施する。

### 5.1 受け入れチェックリスト

- [ ] GitHub issue → `hokusai start` → Phase 1〜10 を **PR 作成・マージまで完走**できる
- [ ] 途中の **human-review pause / `hokusai continue` での再開**が意図通り動く
- [ ] Phase 8 の Copilot / 人間レビューの統合ループが機能する
- [ ] 完走後、**Notion 同期 / `audit_logs` / outbox** が整合している
      （`profile doctor --deep` で outbox 残留なし）
- [ ] 見つかった新規運用穴を `docs/dogfooding-findings.md` に記録した

### 5.2 LLM Gateway enforcement（本番昇格時）

パイロットは `log_only: true`（監査のみ）で開始する。enforcement を有効化する際は:

```bash
# enforce on にする前に policy が no-op でないか診断（T4 相当の事前確認）
hokusai llm-gateway-setup
```

`allowed_providers` / `allowed_models` を設定してから `log_only: false` に切り替える。
未設定のまま enforce on にすると全 LLM 呼び出しが block される事故になり得るため、
必ず `llm-gateway-setup` で確認する。

---

## 6. 日次・定期運用サマリ

| 頻度 | 操作 | コマンド |
|------|------|----------|
| 起動前 | ヘルス確認 | `hokusai --profile <name> profile doctor <name> --deep` |
| 日次（cron） | バックアップ | `hokusai --profile <name> backup --keep 14` |
| 随時 | outbox 監視 | `hokusai --profile <name> profile doctor <name> --deep` / `hokusai dashboard` |
| 監査 | audit ログ確認 | `hokusai --profile <name> audit list --limit 20` |
| 四半期 | 復旧訓練 | 最新スナップショットを別環境へ `restore` |

---

## 関連ドキュメント

- [Notion ダッシュボード運用ガイド](notion-dashboard-operation-guide.md)
- [Profile 運用ガイド](profile-operation-guide.md)
- [dogfooding findings](dogfooding-findings.md)（§14: DB share health / §15: Phase 2 ブロッカー 等）
- README「State DB backup / restore」「Slack notifications」節
