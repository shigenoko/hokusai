"""LLM Gateway interceptor への workflow_id / phase 伝播テスト（F4 配線、案 A）

dogfooding-findings.md §7 F4 で記録した「3 client (claude_code / codex / gemini)
の execute_* / review_* / generate メソッドから `_invoke_llm_gateway_interceptor`
へ workflow_id / phase が伝播していない」状態を埋める PR の回帰防止テスト。

各 client の上層メソッドで `workflow_id="wf-X"` / `phase=N` を渡したとき、
内部 helper (`_invoke_llm_gateway_interceptor`) の呼び出し kwargs に
同値が含まれることを assert する（subprocess は mock してテスト本体に
影響させない）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hokusai.integrations.claude_code import ClaudeCodeClient
from hokusai.integrations.codex import CodexClient, reset_codex_client
from hokusai.integrations.gemini import GeminiClient


@pytest.fixture(autouse=True)
def _reset_codex_singleton():
    yield
    reset_codex_client()


# ---------------------------------------------------------------------------
# ClaudeCodeClient: execute_skill / execute_prompt → _run_claude_code → helper
# ---------------------------------------------------------------------------

def test_claude_code_execute_skill_propagates_workflow_id_to_interceptor(tmp_path):
    """execute_skill(workflow_id=..., phase=...) → helper に伝播する"""
    client = ClaudeCodeClient(working_dir=tmp_path)
    # claude_path / subprocess を mock して実 CLI を呼ばない
    with patch.object(client, "_find_claude_command", return_value="/bin/true"), \
         patch.object(client, "_invoke_llm_gateway_interceptor") as mock_helper, \
         patch("hokusai.integrations.claude_code.ShellRunner") as mock_runner_cls:
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="",
                                duration_ms=0, success=True)
        mock_runner_cls.return_value.run.return_value = mock_result

        client.execute_skill(
            skill="task-research",
            workflow_id="wf-claude-skill",
            phase=2,
        )

    assert mock_helper.called
    kwargs = mock_helper.call_args.kwargs
    assert kwargs["workflow_id"] == "wf-claude-skill"
    assert kwargs["phase"] == 2


def test_claude_code_execute_prompt_propagates_workflow_id_to_interceptor(tmp_path):
    """execute_prompt(workflow_id=..., phase=...) → helper に伝播する"""
    client = ClaudeCodeClient(working_dir=tmp_path)
    with patch.object(client, "_find_claude_command", return_value="/bin/true"), \
         patch.object(client, "_invoke_llm_gateway_interceptor") as mock_helper, \
         patch("hokusai.integrations.claude_code.ShellRunner") as mock_runner_cls:
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="",
                                duration_ms=0, success=True)
        mock_runner_cls.return_value.run.return_value = mock_result

        client.execute_prompt(
            prompt="hello",
            workflow_id="wf-claude-prompt",
            phase=3,
        )

    assert mock_helper.called
    kwargs = mock_helper.call_args.kwargs
    assert kwargs["workflow_id"] == "wf-claude-prompt"
    assert kwargs["phase"] == 3


def test_claude_code_default_workflow_id_is_none(tmp_path):
    """workflow_id / phase を渡さなければ helper に None が伝わる（後方互換）"""
    client = ClaudeCodeClient(working_dir=tmp_path)
    with patch.object(client, "_find_claude_command", return_value="/bin/true"), \
         patch.object(client, "_invoke_llm_gateway_interceptor") as mock_helper, \
         patch("hokusai.integrations.claude_code.ShellRunner") as mock_runner_cls:
        mock_result = MagicMock(returncode=0, stdout="ok", stderr="",
                                duration_ms=0, success=True)
        mock_runner_cls.return_value.run.return_value = mock_result

        client.execute_prompt(prompt="hello")  # workflow_id 未指定

    kwargs = mock_helper.call_args.kwargs
    assert kwargs["workflow_id"] is None
    assert kwargs["phase"] is None


# ---------------------------------------------------------------------------
# CodexClient: review_document → helper
# ---------------------------------------------------------------------------

def test_codex_review_document_propagates_workflow_id_to_interceptor():
    """review_document(workflow_id=..., phase=...) → helper に伝播する"""
    with patch.object(CodexClient, "_find_codex_command", return_value="/bin/true"):
        client = CodexClient()

    with patch.object(client, "_invoke_llm_gateway_interceptor") as mock_helper, \
         patch("hokusai.integrations.codex.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout='{"findings":[]}',
                                          stderr="")

        client.review_document(
            document="doc",
            review_prompt="please review",
            workflow_id="wf-codex",
            phase=4,
        )

    assert mock_helper.called
    kwargs = mock_helper.call_args.kwargs
    assert kwargs["workflow_id"] == "wf-codex"
    assert kwargs["phase"] == 4


# ---------------------------------------------------------------------------
# GeminiClient: review_document / generate → helper
# ---------------------------------------------------------------------------

def test_gemini_review_document_propagates_workflow_id_to_interceptor():
    """review_document(workflow_id=..., phase=...) → helper に伝播する"""
    with patch.object(GeminiClient, "_find_gemini_command", return_value="/bin/true"):
        client = GeminiClient()

    with patch.object(client, "_invoke_llm_gateway_interceptor") as mock_helper, \
         patch.object(client, "_run_with_stdin_prompt") as mock_run, \
         patch.object(client, "_parse_output", return_value={"findings": []}):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"findings":[]}',
                                          stderr="")

        client.review_document(
            document="doc",
            review_prompt="please review",
            workflow_id="wf-gemini-review",
            phase=2,
        )

    assert mock_helper.called
    kwargs = mock_helper.call_args.kwargs
    assert kwargs["workflow_id"] == "wf-gemini-review"
    assert kwargs["phase"] == 2


def test_gemini_generate_propagates_workflow_id_to_interceptor():
    """generate(workflow_id=..., phase=...) → helper に伝播する"""
    with patch.object(GeminiClient, "_find_gemini_command", return_value="/bin/true"):
        client = GeminiClient()

    with patch.object(client, "_invoke_llm_gateway_interceptor") as mock_helper, \
         patch.object(client, "_run_with_stdin_prompt") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="result", stderr="")

        client.generate(
            prompt="hello",
            workflow_id="wf-gemini-gen",
            phase=5,
        )

    assert mock_helper.called
    kwargs = mock_helper.call_args.kwargs
    assert kwargs["workflow_id"] == "wf-gemini-gen"
    assert kwargs["phase"] == 5
