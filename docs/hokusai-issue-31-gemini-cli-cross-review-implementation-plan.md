# Issue #31 実装計画書: Gemini CLI をクロスレビュー LLM として追加（v0.4.6）

## 1. 背景と目的

HOKUSAI のクロス LLM レビュー（Phase 2/3/4）は現状 OpenAI Codex のみ対応。Gemini CLI（`gemini` コマンド）もクロスレビュー用 LLM として選べるようにする。

A 案: 既存 Codex の隣に Gemini を並べる構造。`cross_review.provider` 設定で切替可能にする。

## 2. 後続 B 案を見据えた設計指針

A 案では cross-review 専用 dispatch のみを実装し、主エージェント側の抽象化（`CodingAgentClient` Protocol 等）は導入しない（YAGNI）。

ただし `GeminiClient` の API は B 案で再利用できる汎用的なものにする:

- 専用 API（避ける）: `review_phase(phase_num, ...)` のような cross-review 限定
- 汎用 API（採用）:
  - `review_document(document, prompt, schema_path) -> dict`: CodexClient と同インターフェース
  - `generate(prompt: str, files: list[Path] = None) -> str`: B 案で Phase 2/3/4 から呼ぶ汎用テキスト生成

## 3. 変更ファイル

### 新規

- **`hokusai/integrations/gemini.py`**
  - `GeminiClient` クラス（`gemini` CLI を subprocess 経由で呼ぶ）
  - `_find_gemini_command()`: PATH / GEMINI_PATH 環境変数 / 一般的なインストール場所から検出
  - `review_document(document, review_prompt, schema_path=None, timeout=None) -> dict`: CodexClient と同インターフェース
  - `generate(prompt: str, files: list[Path] | None = None, timeout: int | None = None) -> str`: 汎用テキスト生成
  - `_parse_output(output) -> dict`: JSON 出力パース
  - `get_gemini_client()` / `reset_gemini_client()` ファクトリ
- **`tests/test_gemini_client.py`**: subprocess モック + 出力パース + エラーハンドリング

### 変更

- **`hokusai/config/models.py`**: `CrossReviewConfig` に `provider: str = "codex"` フィールド追加
- **`hokusai/config/loaders.py`** (該当箇所があれば): provider バリデーション
- **`hokusai/utils/cross_review.py`**:
  - `provider` で `CodexClient` / `GeminiClient` を dispatch する factory
  - dispatch ロジック以外の処理（プロンプト構築、エラーハンドリング、state 更新）は client 非依存に維持
- **`hokusai/integrations/connection_status.py`**: Gemini 接続状態チェック追加
- **`hokusai/cli_main.py`**: `hokusai connect gemini` サブコマンド追加（既存 connect github/gitlab と同パターン）
- **`hokusai/integrations/__init__.py`**: GeminiClient export

### docs

- `README.md` / `README_JP.md`: Cross-LLM レビューセクションを更新（provider 選択を明記）
- `CHANGELOG.md`: v0.4.6 エントリ
- `docs/notion-dashboard-operation-guide.md`: 関連箇所があれば

### version

- `pyproject.toml`: 0.4.5 → 0.4.6
- `hokusai/__init__.py`: 0.4.5 → 0.4.6
- `uv.lock` 同期

## 4. cross_review.py の dispatch 設計

```python
# 現状
from ..integrations.codex import CodexClient
client = CodexClient(model=..., timeout=...)

# 新
def _create_review_client(config) -> ReviewClient:
    provider = config.cross_review.provider
    if provider == "codex":
        from ..integrations.codex import CodexClient
        return CodexClient(model=config.cross_review.model, timeout=...)
    elif provider == "gemini":
        from ..integrations.gemini import GeminiClient
        return GeminiClient(model=config.cross_review.model, timeout=...)
    else:
        raise ValueError(f"Unknown cross_review.provider: {provider}")
```

各 client は同インターフェース（`review_document(document, prompt, schema_path) -> dict`）を実装することで、`cross_review.py` の dispatch 以降は client 非依存。

## 5. config 設定例

```yaml
# Codex を使う場合（既定）
cross_review:
  enabled: true
  provider: codex
  model: codex-mini-latest
  phases: [2, 4]

# Gemini を使う場合
cross_review:
  enabled: true
  provider: gemini
  model: gemini-2.5-pro
  phases: [2, 4]
```

## 6. テスト方針

### tests/test_gemini_client.py（新規）

- `test_gemini_client_requires_command`: gemini CLI が見つからない場合 FileNotFoundError
- `test_gemini_client_review_document_success`: subprocess モックで JSON 出力 → dict 返却
- `test_gemini_client_review_document_timeout`: timeout 時に TimeoutError
- `test_gemini_client_review_document_nonzero_exit`: exit code 非ゼロで RuntimeError
- `test_gemini_client_parse_markdown_json_block`: ```json ... ``` 形式の出力をパース
- `test_gemini_client_generate_returns_text`: `generate()` がプレーンテキストを返す
- `test_gemini_client_singleton_factory`: `get_gemini_client()` が同インスタンスを返す

### tests/test_cross_review.py（拡張）

- `test_cross_review_dispatches_codex_by_default`: `provider` 未指定で CodexClient が使われる
- `test_cross_review_dispatches_gemini_when_configured`: `provider: gemini` で GeminiClient が使われる
- `test_cross_review_unknown_provider_raises`: 未知の provider で ValueError

### tests/test_connection_status.py（拡張）

- `test_connection_status_detects_gemini`: gemini が PATH にあれば status=ok

## 7. リスクと対応

| リスク | 対応 |
|---|---|
| Gemini CLI の出力形式が Codex と異なる | `_parse_output` を独立実装し、Gemini 固有の出力形式（markdown wrapping 等）に対応 |
| gemini コマンドのインストール場所が不定 | PATH 検出 + 環境変数 GEMINI_PATH + 一般的なインストール場所のフォールバック |
| API token 認証方式の違い | `gemini` CLI 側に委譲（HOKUSAI 自身は token を保持しない、`gemini auth login` で認証） |
| Codex / Gemini で同じ schema が使えるか | `schemas/review_schema.json` は client 非依存の JSON schema なのでそのまま流用可能 |

## 8. 受入条件

- [ ] `GeminiClient` を作成（CodexClient と同等のインターフェース + 汎用 generate メソッド）
- [ ] `CrossReviewConfig.provider` で `codex` / `gemini` 切替可能
- [ ] `hokusai connect gemini` で接続状態確認
- [ ] テスト全件 pass、lint クリーン
- [ ] README / CHANGELOG 更新
- [ ] version 0.4.5 → 0.4.6

## 9. 関連

- 既存 cross-review: `hokusai/utils/cross_review.py`、`hokusai/integrations/codex.py`
- 後続: 主コーディングエージェント抽象化（B 案、v0.5.x で別 Issue）
