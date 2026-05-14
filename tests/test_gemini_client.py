"""GeminiClient のテスト（Issue #31 / v0.4.6）

CodexClient と同パターンの単体テスト + 汎用 generate() の動作確認。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from hokusai.integrations.gemini import GeminiClient, reset_gemini_client


@pytest.fixture(autouse=True)
def _reset_gemini():
    yield
    reset_gemini_client()


# ---------------------------------------------------------------------------
# コマンド検出
# ---------------------------------------------------------------------------


def test_gemini_client_uses_env_path(monkeypatch):
    """環境変数 GEMINI_PATH を最優先で使う。"""
    monkeypatch.setenv("GEMINI_PATH", "/custom/path/gemini")
    client = GeminiClient()
    assert client.gemini_path == "/custom/path/gemini"


def test_gemini_client_uses_which_when_no_env(monkeypatch):
    """GEMINI_PATH 未設定なら shutil.which の結果を使う。"""
    monkeypatch.delenv("GEMINI_PATH", raising=False)
    with patch("hokusai.integrations.gemini.shutil.which", return_value="/usr/local/bin/gemini"):
        client = GeminiClient()
    assert client.gemini_path == "/usr/local/bin/gemini"


def test_gemini_client_raises_when_command_missing(monkeypatch):
    """gemini コマンドが見つからない場合 FileNotFoundError。"""
    monkeypatch.delenv("GEMINI_PATH", raising=False)
    with (
        patch("hokusai.integrations.gemini.shutil.which", return_value=None),
        patch("hokusai.integrations.gemini.Path.exists", return_value=False),
    ):
        with pytest.raises(FileNotFoundError, match="gemini コマンドが見つかりません"):
            GeminiClient()


@pytest.mark.parametrize(
    "bad_model",
    [
        "model with space",
        "model;rm -rf /",
        "-r maliciousflag",
        "model$(injection)",
        "model\nnewline",
        "",
    ],
)
def test_gemini_client_rejects_unsafe_model_name(monkeypatch, bad_model):
    """フラグ注入や shell metacharacter を含むモデル名は ValueError で拒否する。

    SonarCloud pythonsecurity:S6350 対応: subprocess.run に渡す引数が
    whitelist 検証されることを保証する。
    """
    monkeypatch.setenv("GEMINI_PATH", "/fake/gemini")
    with pytest.raises(ValueError, match="不正な文字"):
        GeminiClient(model=bad_model)


@pytest.mark.parametrize(
    "good_model",
    [
        "gemini-2.5-pro",
        "gemini-1.5-flash",
        "models/gemini-pro",
        "preview:gemini-2.0",
    ],
)
def test_gemini_client_accepts_typical_model_names(monkeypatch, good_model):
    """一般的な Gemini モデル名（英数字 / `-` / `.` / `_` / `:` / `/`）は許容する。"""
    monkeypatch.setenv("GEMINI_PATH", "/fake/gemini")
    client = GeminiClient(model=good_model)
    assert client.model == good_model


# ---------------------------------------------------------------------------
# review_document（CodexClient と同インターフェース）
# ---------------------------------------------------------------------------


@pytest.fixture
def gemini_client(monkeypatch):
    monkeypatch.setenv("GEMINI_PATH", "/fake/gemini")
    return GeminiClient(model="gemini-2.5-pro", timeout=60)


def test_review_document_returns_dict_on_success(gemini_client):
    """正常系: JSON 出力を dict として返す。"""
    sample = {
        "findings": [],
        "overall_assessment": "approve",
        "summary": "OK",
        "confidence_score": 0.9,
    }
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(sample), stderr=""
    )
    with patch("hokusai.integrations.gemini.subprocess.run", return_value=mock_result):
        result = gemini_client.review_document(
            document="設計ドキュメント", review_prompt="レビューしてください"
        )
    assert result["overall_assessment"] == "approve"
    assert result["summary"] == "OK"


def test_review_document_timeout_raises_timeout_error(gemini_client):
    """タイムアウト時に TimeoutError を送出する。"""
    with patch(
        "hokusai.integrations.gemini.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["gemini"], timeout=60),
    ):
        with pytest.raises(TimeoutError, match="タイムアウト"):
            gemini_client.review_document(document="d", review_prompt="p")


def test_review_document_nonzero_exit_raises_runtime_error(gemini_client):
    """exit code 非ゼロ時に RuntimeError を送出する。"""
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="invalid api key"
    )
    with patch("hokusai.integrations.gemini.subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError, match="exit code 1"):
            gemini_client.review_document(document="d", review_prompt="p")


def test_review_document_parses_markdown_json_block(gemini_client):
    """markdown ```json ... ``` 形式の出力をパースする。"""
    sample = {
        "findings": [],
        "overall_assessment": "approve",
        "summary": "OK",
    }
    wrapped_output = f"以下がレビュー結果です:\n\n```json\n{json.dumps(sample)}\n```\n"
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=wrapped_output, stderr=""
    )
    with patch("hokusai.integrations.gemini.subprocess.run", return_value=mock_result):
        result = gemini_client.review_document(document="d", review_prompt="p")
    assert result["overall_assessment"] == "approve"


def test_review_document_falls_back_to_text_when_unparseable(gemini_client):
    """JSON としてパースできない出力は parse_error=True で text を summary に格納。"""
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="LLM がエラーで返事できませんでした", stderr=""
    )
    with patch("hokusai.integrations.gemini.subprocess.run", return_value=mock_result):
        result = gemini_client.review_document(document="d", review_prompt="p")
    assert result.get("parse_error") is True
    assert "エラー" in result["summary"]
    assert result["findings"] == []


def test_review_document_handles_prose_prefix_with_nested_json(gemini_client):
    """前置き prose + ネストした JSON object を含む出力を正しくパースする。

    Copilot レビュー 1 回目 #6 対応: 旧実装は rfind('{') を使っていたため、
    ネストした `findings[].suggestion` の中の `{` を起点にしてしまい partial
    fragment を json.loads に渡して失敗していた。新実装は最初の top-level
    `{` から対応する `}` までを brace balance で抽出する。
    """
    sample = {
        "findings": [
            {
                "category": "completeness",
                "severity": "major",
                "title": "テスト不足",
                "description": "test case が足りません",
                "suggestion": "{ 例として add_test() を追加 } のような実装",
            }
        ],
        "overall_assessment": "request_changes",
        "summary": "概ね良好",
    }
    prose_then_json = (
        "レビュー結果は以下のとおりです。注意点が見つかりました。\n\n"
        f"{json.dumps(sample, ensure_ascii=False)}\n\n"
        "以上です。"
    )
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=prose_then_json, stderr=""
    )
    with patch("hokusai.integrations.gemini.subprocess.run", return_value=mock_result):
        result = gemini_client.review_document(document="d", review_prompt="p")
    # parse_error にならず、正しい dict が返る
    assert result.get("parse_error") is not True
    assert result["overall_assessment"] == "request_changes"
    assert len(result["findings"]) == 1
    assert result["findings"][0]["title"] == "テスト不足"


# ---------------------------------------------------------------------------
# 汎用 generate（B 案で再利用される汎用 API）
# ---------------------------------------------------------------------------


def test_generate_returns_plain_text(gemini_client):
    """generate() はプロンプトに対する生のテキスト出力を返す。"""
    mock_result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="これは Gemini からの応答です。", stderr=""
    )
    with patch("hokusai.integrations.gemini.subprocess.run", return_value=mock_result):
        text = gemini_client.generate("Python とは何か説明してください")
    assert text == "これは Gemini からの応答です。"


def test_generate_with_files_includes_file_content_in_prompt(gemini_client, tmp_path):
    """generate() に files を渡すと、各ファイルの内容がプロンプトに連結される。

    v0.4.6 以降: プロンプトは argv の `-p` ではなく stdin で渡される
    （SonarCloud pythonsecurity:S6350 対策）。
    """
    file_a = tmp_path / "a.txt"
    file_a.write_text("ファイル A の中身", encoding="utf-8")
    file_b = tmp_path / "b.py"
    file_b.write_text("def hello(): pass", encoding="utf-8")

    captured_kwargs: list[dict] = []

    def fake_run(cmd, **kwargs):
        captured_kwargs.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

    with patch("hokusai.integrations.gemini.subprocess.run", side_effect=fake_run):
        gemini_client.generate("要約して", files=[file_a, file_b])

    assert len(captured_kwargs) == 1
    # argv にはモデル名のみ、user-controlled なプロンプトは載らない
    cmd = captured_kwargs[0]["cmd"]
    assert "-p" not in cmd
    assert "-m" in cmd
    # プロンプトは stdin (input=) で渡される
    stdin_prompt = captured_kwargs[0]["input"]
    assert "ファイル A の中身" in stdin_prompt
    assert "def hello(): pass" in stdin_prompt


def test_review_document_passes_prompt_via_stdin(gemini_client):
    """review_document() でもプロンプトは argv ではなく stdin で渡される
    （SonarCloud pythonsecurity:S6350 対策の回帰防止）。
    """
    captured_kwargs: list[dict] = []

    def fake_run(cmd, **kwargs):
        captured_kwargs.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout='{"summary": "ok"}', stderr=""
        )

    with patch("hokusai.integrations.gemini.subprocess.run", side_effect=fake_run):
        gemini_client.review_document(document="doc 内容", review_prompt="rev prompt")

    assert len(captured_kwargs) == 1
    cmd = captured_kwargs[0]["cmd"]
    # argv には gemini path + "-m" + model 名のみ。"-p" は含まれない
    assert "-p" not in cmd
    assert cmd[-2] == "-m"
    # プロンプト + ドキュメントが stdin で渡される
    stdin_prompt = captured_kwargs[0]["input"]
    assert "rev prompt" in stdin_prompt
    assert "doc 内容" in stdin_prompt


def test_generate_timeout_raises(gemini_client):
    """generate() でも timeout 時に TimeoutError。"""
    with patch(
        "hokusai.integrations.gemini.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["gemini"], timeout=60),
    ):
        with pytest.raises(TimeoutError):
            gemini_client.generate("プロンプト")


# ---------------------------------------------------------------------------
# シングルトンファクトリ
# ---------------------------------------------------------------------------


def test_get_gemini_client_returns_singleton(monkeypatch):
    monkeypatch.setenv("GEMINI_PATH", "/fake/gemini")
    from hokusai.integrations.gemini import get_gemini_client

    c1 = get_gemini_client()
    c2 = get_gemini_client()
    assert c1 is c2


def test_reset_gemini_client_allows_recreation(monkeypatch):
    monkeypatch.setenv("GEMINI_PATH", "/fake/gemini")
    from hokusai.integrations.gemini import get_gemini_client, reset_gemini_client

    c1 = get_gemini_client()
    reset_gemini_client()
    c2 = get_gemini_client()
    assert c1 is not c2
