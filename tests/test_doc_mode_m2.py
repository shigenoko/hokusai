"""Phase 0 doc-mode M2 テスト（役割→provider を実 client に束ねる）

検証内容:
- default_generation_backend が provider ごとに正しい client メソッドへ振り分ける
- codex の dict 戻り値がテキストへ正規化される
- 未知 provider は ValueError
- provider CLI 未導入（client が RuntimeError）は DocModeProviderError に変換される

実 CLI（claude/codex/gemini）には依存せず、client クラスを monkeypatch する。
"""

import pytest

from hokusai.nodes import phase0_doc


def test_dispatch_claude_code(monkeypatch):
    captured = {}

    class FakeClaude:
        def execute_prompt(self, prompt):
            captured["prompt"] = prompt
            return "claude-draft"

    monkeypatch.setattr(
        "hokusai.integrations.claude_code.ClaudeCodeClient", lambda *a, **k: FakeClaude()
    )
    out = phase0_doc.default_generation_backend("claude_code", "", "P")
    assert out == "claude-draft"
    assert captured["prompt"] == "P"


def test_dispatch_gemini(monkeypatch):
    class FakeGemini:
        def __init__(self, *a, **k):
            pass  # テスト用スタブ: 初期化不要

        def generate(self, prompt):
            return "gemini-text"

    monkeypatch.setattr("hokusai.integrations.gemini.GeminiClient", FakeGemini)
    assert phase0_doc.default_generation_backend("gemini", "", "P") == "gemini-text"


def test_dispatch_codex_dict_is_coerced(monkeypatch):
    class FakeCodex:
        def __init__(self, *a, **k):
            pass  # テスト用スタブ: 初期化不要

        def review_document(self, document, review_prompt):
            return {"summary": "codex-review"}

    monkeypatch.setattr("hokusai.integrations.codex.CodexClient", FakeCodex)
    assert phase0_doc.default_generation_backend("codex", "", "P") == "codex-review"


def test_unknown_provider_raises_value_error():
    with pytest.raises(ValueError):
        phase0_doc.default_generation_backend("unknown", "", "P")


def test_missing_cli_raises_provider_error(monkeypatch):
    class Boom:
        def execute_prompt(self, prompt):
            raise RuntimeError("claude command not found")

    monkeypatch.setattr(
        "hokusai.integrations.claude_code.ClaudeCodeClient", lambda *a, **k: Boom()
    )
    with pytest.raises(phase0_doc.DocModeProviderError):
        phase0_doc.default_generation_backend("claude_code", "", "P")


def test_coerce_text_fallback_to_json():
    out = phase0_doc._coerce_text({"unexpected": "値"})
    assert "unexpected" in out and "値" in out
