# Issue #19 実装計画: Notion 接続状態パネルに「どの Notion か」識別情報を表示

**Issue**: [#19 Operations Console の Notion 接続状態パネルに「どの Notion か」識別できる情報を表示](https://github.com/shigenoko/hokusai/issues/19)
**作成日**: 2026-05-14
**対象バージョン**: v0.4.2（patch リリース想定）

---

## 1. 背景

v0.4.1（#17 / #18）で `hokusai notion-setup` の profile-aware 化を実装し、複数の Notion ワークスペースを profile 単位で使い分けられるようになった。これに伴い、Operations Console の Notion 接続状態パネルに「どの Notion か」を識別する情報がないことが問題として顕在化した。

### 1.1 v0.4.1 時点の動作（`scripts/dashboard.py:129-187`）

`render_notion_dashboard_panel()` は以下のみを表示:
- 接続状態（🟢 接続準備済み / 🟡 設定済み（環境変数未設定））
- 保留件数 / 永続失敗件数
- 「同期再送」ボタン

### 1.2 結果として起こる問題

- どの Notion ワークスペースに接続しているか不明
- profile 切り替え時の動作確認ができない
- 「dashboard では OK 表示なのに、別の workspace に書き込んでいた」事故の温床

## 2. ゴール

### 2.1 やること

- Notion 接続状態パネルに以下の identification 情報を表示:
  - 現在 active な profile 名
  - 使用中の env 変数名（`api_token_env`）
  - Workflows DB / Pull Requests DB の ID（マスク表示）
  - DB URL（クリック可能なリンク）
  - Bot ユーザー名 / integration 名（Notion API `GET /users/me` から取得）

### 2.2 やらないこと

- Notion API workspace name の取得（API 仕様上、直接取得不可）
- 接続状態パネル以外の UI 変更
- Notion 連携以外（GitHub / Slack 等）の同様の改善（別 Issue で扱う）
- `--deep` profile doctor の実 API 接続確認（別の改善余地）

## 3. 設計

### 3.1 表示する identification 情報

| 項目 | 取得元 | 表示形式 |
|---|---|---|
| Profile | runtime（`get_config().profile_name`） | `hokusai` |
| API token env | profile config の `notion_dashboard.api_token_env` | `HOKUSAI_NOTION_API_TOKEN_4HOKUSAI` |
| Workflows DB | `workflows_db_id_env` 解決値 | `35f85495...82ff`（リンク） |
| Pull Requests DB | `pull_requests_db_id_env` 解決値 | `35f85495...c0dc`（リンク） |
| Bot user | Notion API `GET /users/me` の `name` / `bot.owner` | `HOKUSAI Integration (bot)` |

### 3.2 DB ID マスク

`<先頭 8 桁>...<末尾 4 桁>` 形式でマスク表示する。

- 完全な ID は HTML 属性（`title` / `data-id`）に持たせて、tooltip / クリップボードコピーで取得可能にする
- 完全 ID はサーバー側ログには出力しない

```python
def _mask_db_id(db_id: str) -> str:
    if not db_id or len(db_id) < 12:
        return "(unknown)"
    return f"{db_id[:8]}...{db_id[-4:]}"
```

### 3.3 DB URL の生成

Notion DB URL は `https://www.notion.so/<id_without_dashes>` の形式。

```python
def _notion_db_url(db_id: str) -> str:
    return f"https://www.notion.so/{db_id.replace('-', '')}"
```

### 3.4 Bot 情報の取得

#### 3.4.1 NotionAPIClient に `get_bot_info()` メソッドを追加

`hokusai/integrations/notion_dashboard/client.py`:

```python
def get_bot_info(self) -> dict:
    """Notion API GET /users/me を呼んで bot 情報を取得する。
    
    Returns:
        {"id": ..., "name": ..., "type": "bot", "bot": {...}}
    """
    return self._request("GET", "/users/me")
```

#### 3.4.2 キャッシュ戦略

dashboard パネル描画のたびに Notion API を叩くのは非効率。以下のキャッシュを採用:

- **process memory cache（TTL 300 秒）** — `(api_token_env_value, ...)` をキーに、bot info を memory に保持
- キャッシュ失敗時は `(unknown)` 表示で degrade（パネル全体を落とさない）

実装上は `functools.lru_cache` ではなく、`time.time()` を見る簡易キャッシュとする（TTL 制御のため）。

### 3.5 panel 描画の変更

`scripts/dashboard.py` の `render_notion_dashboard_panel()` を拡張。

現状の表示の下に、以下の identification セクションを追加:

```html
<div class="card-section">
  <h4>接続先 Notion</h4>
  <table class="kv-table">
    <tr><th>Profile</th><td>hokusai</td></tr>
    <tr><th>API token env</th><td><code>HOKUSAI_NOTION_API_TOKEN_4HOKUSAI</code></td></tr>
    <tr><th>Workflows DB</th><td><a href="..." target="_blank">35f85495...82ff</a></td></tr>
    <tr><th>Pull Requests DB</th><td><a href="..." target="_blank">35f85495...c0dc</a></td></tr>
    <tr><th>Bot user</th><td>HOKUSAI Integration</td></tr>
  </table>
</div>
```

### 3.6 エラー時の挙動

- Notion API への bot info 取得が失敗（401 / network error 等）→ `(unable to fetch)` と表示し、他の項目は通常通り
- profile config が読めない（極端な状況）→ identification セクション自体を出さない

## 4. 実装ステップ

### Step 1: NotionAPIClient に `get_bot_info()` 追加

**対象ファイル**: `hokusai/integrations/notion_dashboard/client.py`

- 既存の `_request("GET", path)` を使った薄いラッパ
- 単体テストで mock を使って動作確認

### Step 2: identification helper を `notion_dashboard/setup.py` に追加

**対象ファイル**: `hokusai/integrations/notion_dashboard/__init__.py` / `setup.py`（または新規ファイル `identification.py`）

- `_mask_db_id`、`_notion_db_url`、`_get_bot_info_cached` のような関数を public または internal で提供
- TTL キャッシュは process memory（dashboard はプロセス常駐想定）

### Step 3: dashboard パネルへの組み込み

**対象ファイル**: `scripts/dashboard.py`

- `render_notion_dashboard_panel()` を改修
- profile name / env 名 / DB ID / DB URL / Bot user を取得して HTML にレンダ
- 取得失敗時は graceful degrade

### Step 4: テスト追加

**対象ファイル**: `tests/test_notion_dashboard_panel.py`（新規 or 既存拡張）

ケース:
1. profile 設定済み + bot info 取得成功 → 全項目が表示される
2. bot info 取得失敗 → `(unable to fetch)` 表示で他項目は表示される
3. notion_dashboard が disabled → パネル全体が空 string
4. DB ID マスクの形式
5. キャッシュの TTL 動作（時刻 mock）

### Step 5: README / 運用ガイド更新

**対象ファイル**:
- `docs/notion-dashboard-operation-guide.md`（接続先確認手順を追加）
- `CHANGELOG.md`（v0.4.2 エントリ）

## 5. ファイル別変更概要

| ファイル | 変更内容 | 規模 |
|---|---|---|
| `hokusai/integrations/notion_dashboard/client.py` | `get_bot_info()` 追加 | +10 行 |
| `hokusai/integrations/notion_dashboard/identification.py`（新規） | mask / URL / cached bot info ヘルパ | +60 行 |
| `hokusai/integrations/notion_dashboard/__init__.py` | export 追加 | +5 行 |
| `scripts/dashboard.py` | panel 描画の拡張 | +50 行 / -5 行 |
| `tests/test_notion_dashboard_panel.py` | identification ケース追加 | +80 行 |
| `docs/notion-dashboard-operation-guide.md` | 接続先確認手順 | +20 行 |
| `CHANGELOG.md` | v0.4.2 エントリ | +15 行 |

合計: コード 〜120 行 / テスト 〜80 行 / docs 〜35 行

## 6. 後方互換性

| ケース | 動作 |
|---|---|
| 既存ユーザーが dashboard を開く | 接続状態 / 件数表示は従来通り、identification セクションが追加で表示される |
| profile 未指定で dashboard を起動 | profile 表示は `(default)` または非表示、他項目は env 名 / DB ID から推測 |
| Notion 未設定（disabled） | パネル全体が空（従来通り） |
| Notion API が一時的に到達不能 | identification 項目は表示され、Bot user のみ `(unable to fetch)` |

破壊的変更なし。

## 7. リスクと対策

| リスク | 対策 |
|---|---|
| Bot info 取得の API call で rate limit 消費 | 5 分 TTL の process memory cache |
| profile が複数あり dashboard が単一 process | 既存 v0.3.0 設計通り、dashboard は 1 profile 単位で起動。複数 profile を同時表示はしない |
| DB ID が画面録画 / スクリーンショットで流出 | マスク表示（先頭 8 桁 + 末尾 4 桁）。完全 ID は HTML 属性経由でのみ取得可能 |
| Notion API token 自体の表示要望 | 表示しない。env 名のみ表示する |

## 8. 受け入れ基準（Issue #19 と対応）

- [ ] `hokusai dashboard` の Notion 接続状態パネルに「どの Notion か」を識別できる情報が表示される
- [ ] 使用中の env 変数名（`api_token_env`）が表示される
- [ ] Workflows DB / Pull Requests DB の ID がマスク表示される
- [ ] DB URL がクリック可能なリンクとして表示される
- [ ] Bot ユーザー名（または integration 名）が表示される
- [ ] active な profile 名が表示される
- [ ] profile を切り替えて dashboard を起動すると、表示が変わる
- [ ] DB ID 等の完全な値はサーバー側ログには記録されない
- [ ] Bot info 取得失敗時に panel 全体が落ちず、graceful degrade する

## 9. ロールアウト

- patch リリース（v0.4.2）として扱う（後方互換 + 機能小規模）
- CHANGELOG `## [0.4.2] - 2026-MM-DD`:
  - `### Added`: Operations Console の Notion 接続状態パネルに識別情報（profile / env 名 / DB ID / DB URL / Bot user）を追加

## 10. 関連ドキュメント

- Issue: https://github.com/shigenoko/hokusai/issues/19
- v0.4.1（前提となる profile-aware notion-setup）: PR #18 / `docs/hokusai-issue-17-notion-setup-profile-aware-implementation-plan.md`
- Operations Console（更新対象）: `docs/notion-dashboard-operation-guide.md`
