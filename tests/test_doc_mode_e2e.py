"""doc-mode 実プロバイダ E2E（opt-in）

実 LLM CLI（claude_code / codex / gemini）を実際に起動して doc-mode を1周
走らせる E2E。コスト・外部依存があるため、既定ではスキップする。

実行方法:
    HOKUSAI_DOC_E2E=1 uv run pytest tests/test_doc_mode_e2e.py -q

provider は doc_orchestration.roles で指定する（既定: 全 role claude_code、
HOKUSAI_DOC_E2E_PROVIDER で上書き可: codex / gemini / claude_code）。
必要な CLI が未導入の場合は skip する。
"""

import os
import shutil

import pytest

from hokusai import doc_cli
from hokusai.config import WorkflowConfig, reset_config, set_config
from hokusai.config.models import DocOrchestrationConfig

_E2E = os.environ.get("HOKUSAI_DOC_E2E") == "1"
_PROVIDER = os.environ.get("HOKUSAI_DOC_E2E_PROVIDER", "claude_code")

# provider -> 必要な CLI コマンド名
_PROVIDER_CLI = {"claude_code": "claude", "codex": "codex", "gemini": "gemini"}


pytestmark = pytest.mark.skipif(
    not _E2E,
    reason="実プロバイダ E2E は HOKUSAI_DOC_E2E=1 のときのみ実行（既定スキップ）",
)


@pytest.fixture()
def _config():
    reset_config()
    roles = {role: {"provider": _PROVIDER} for role in ("ideator", "drafter", "reviewer", "finalizer")}
    set_config(
        WorkflowConfig(
            doc_orchestration=DocOrchestrationConfig(enabled=True, roles=roles)
        )
    )
    yield
    reset_config()


def test_e2e_real_provider_auto_run(_config):
    # 未知 provider は設定ミスとして明示 fail（黙って skip しない）
    if _PROVIDER not in _PROVIDER_CLI:
        pytest.fail(
            f"未知の HOKUSAI_DOC_E2E_PROVIDER='{_PROVIDER}'"
            f"（claude_code / codex / gemini のいずれか）"
        )
    cli = _PROVIDER_CLI[_PROVIDER]
    # CLI 未導入のときのみ skip
    if shutil.which(cli) is None:
        pytest.skip(f"provider CLI '{cli}' が未導入のためスキップ")

    # backend 未注入 → default_generation_backend（実 provider client）が走る
    state = doc_cli.run_doc_workflow(
        doc_type="requirements",
        topic="doc-mode E2E スモーク：簡単な要件を1つ書く",
        run_mode="auto",
    )
    assert isinstance(state.get("final_doc"), str)
    assert state["final_doc"].strip() != ""
    # 型準拠チェックは provider 出力依存のため厳密 assert はしない（実走確認が目的）
