"""LLM Gateway Phase 1 (#39 / v0.6.0〜) 単体テスト

- LLMGatewayConfig の loader / 既定値
- LLMGatewayContext の生成
- LLMGatewayInterceptor の log-only 動作（enabled / disabled / dry_run / audit_log_enabled の組み合わせ）
- ClaudeCodeClient._run_claude_code 経由で interceptor が呼ばれることを subprocess を mock して検証
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.config.loaders import _parse_llm_gateway_config
from hokusai.config.models import LLMGatewayConfig
from hokusai.llm_gateway import (
    LLMGatewayContext,
    LLMGatewayInterceptor,
)

# ---------------------------------------------------------------------------
# LLMGatewayConfig loader
# ---------------------------------------------------------------------------


def test_parse_llm_gateway_returns_default_when_missing():
    cfg = _parse_llm_gateway_config({})
    assert cfg == LLMGatewayConfig()


def test_parse_llm_gateway_returns_default_when_not_dict():
    cfg = _parse_llm_gateway_config({"llm_gateway": "not-a-dict"})
    assert cfg == LLMGatewayConfig()


def test_parse_llm_gateway_full_config():
    cfg = _parse_llm_gateway_config({
        "llm_gateway": {
            "enabled": True,
            "dry_run": True,
            "log_only": False,
            "audit_log_enabled": False,
        }
    })
    assert cfg.enabled is True
    assert cfg.dry_run is True
    assert cfg.log_only is False
    assert cfg.audit_log_enabled is False


def test_parse_llm_gateway_rejects_non_bool_fields_with_defaults():
    """bool 以外が来た場合、各フィールドは defaults に戻る"""
    cfg = _parse_llm_gateway_config({
        "llm_gateway": {
            "enabled": "yes",  # 不正型
            "dry_run": 1,  # 不正型（True と等価でも reject）
            "log_only": None,  # 不正型
            "audit_log_enabled": "true",  # 不正型
        }
    })
    defaults = LLMGatewayConfig()
    assert cfg.enabled is defaults.enabled
    assert cfg.dry_run is defaults.dry_run
    assert cfg.log_only is defaults.log_only
    assert cfg.audit_log_enabled is defaults.audit_log_enabled


def test_llm_gateway_default_is_disabled_log_only():
    """既定値は「Gateway 無効・log_only オン・audit_log 有効」で透過動作"""
    cfg = LLMGatewayConfig()
    assert cfg.enabled is False
    assert cfg.log_only is True
    assert cfg.audit_log_enabled is True


# ---------------------------------------------------------------------------
# LLMGatewayContext
# ---------------------------------------------------------------------------


def test_context_minimum_construction():
    ctx = LLMGatewayContext(provider="claude_code")
    assert ctx.provider == "claude_code"
    assert ctx.model == ""
    assert ctx.purpose == ""
    assert ctx.workflow_id is None
    assert ctx.phase is None
    assert ctx.metadata == {}


def test_context_with_workflow():
    ctx = LLMGatewayContext(
        provider="claude_code",
        model="claude-3-5-sonnet",
        purpose="skill_execution:dev-plan",
        workflow_id="wf-001",
        phase=4,
        metadata={"skill": "dev-plan"},
    )
    assert ctx.workflow_id == "wf-001"
    assert ctx.phase == 4
    assert ctx.metadata["skill"] == "dev-plan"


def test_context_metadata_is_read_only_after_init():
    """metadata は MappingProxyType でラップされ、後から書き換え不可になる
    （PR #40 Copilot 1 回目指摘: frozen dataclass の不変性が dict 内容に
    及ばない問題への対応）。"""
    ctx = LLMGatewayContext(provider="x", metadata={"k": "v"})
    with pytest.raises(TypeError):
        ctx.metadata["k"] = "tampered"  # type: ignore[index]
    with pytest.raises(TypeError):
        ctx.metadata["new"] = "value"  # type: ignore[index]


def test_context_metadata_isolated_from_input_dict_mutations():
    """呼び出し側が渡した dict を後から変えても context.metadata は不変"""
    original = {"k": "v"}
    ctx = LLMGatewayContext(provider="x", metadata=original)
    original["k"] = "mutated-by-caller"
    original["added"] = "after"
    assert ctx.metadata["k"] == "v"
    assert "added" not in ctx.metadata


def test_context_metadata_copies_even_when_mappingproxy_input():
    """MappingProxyType 由来の Mapping を渡しても、内部で必ず dict コピーを取る
    （PR #40 Copilot 2 回目指摘: underlying dict 経由の改変を防ぐ）"""
    from types import MappingProxyType

    underlying = {"k": "v"}
    proxy_input = MappingProxyType(underlying)
    ctx = LLMGatewayContext(provider="x", metadata=proxy_input)
    # underlying dict を後から書き換えても context は影響を受けない
    underlying["k"] = "mutated"
    underlying["added"] = "after"
    assert ctx.metadata["k"] == "v"
    assert "added" not in ctx.metadata


# ---------------------------------------------------------------------------
# LLMGatewayInterceptor
# ---------------------------------------------------------------------------


def test_interceptor_skipped_when_gateway_disabled():
    config = LLMGatewayConfig(enabled=False)
    interceptor = LLMGatewayInterceptor(config)
    decision = interceptor.intercept(
        LLMGatewayContext(provider="claude_code"), "hello"
    )
    assert decision.decision == "skipped"
    assert decision.reason == "gateway_disabled"
    assert decision.audit_emitted is False


def test_interceptor_log_when_enabled(caplog):
    config = LLMGatewayConfig(enabled=True, audit_log_enabled=True)
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="claude_code", purpose="execute_prompt"),
            "test prompt body",
        )
    assert decision.decision == "log"
    assert decision.reason == "phase1_log_only"
    assert decision.audit_emitted is True
    # logger に audit entry が JSON 形式で流れる
    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    assert audit_records, "audit log entry should be emitted"
    # JSON 部分を解析できる
    json_part = audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    payload = json.loads(json_part)
    assert payload["event"] == "llm_gateway_decision"
    assert payload["decision"] == "log"
    assert payload["context"]["provider"] == "claude_code"
    assert payload["context"]["purpose"] == "execute_prompt"
    # prompt 本文は保存されず length / hash のみ
    assert "prompt_body" not in payload
    assert payload["prompt_length"] == len("test prompt body")
    assert len(payload["prompt_hash"]) == 16


def test_interceptor_dry_run_uses_distinct_reason(caplog):
    """dry_run=True でも Phase 1 では block しないが、reason だけ区別する"""
    config = LLMGatewayConfig(enabled=True, dry_run=True, audit_log_enabled=True)
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="claude_code"), "x"
        )
    assert decision.decision == "log"
    assert decision.reason == "dry_run_log_only"


def test_interceptor_audit_includes_config_snapshot(caplog):
    """audit JSON に config_snapshot が含まれ、Phase 1 動作中の設定が記録される
    （PR #40 Copilot 2 回目指摘: log_only / dry_run が decision に影響しなくても
    監査上どの設定で動いていたかは残す）"""
    config = LLMGatewayConfig(enabled=True, dry_run=True, audit_log_enabled=True)
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        interceptor.intercept(LLMGatewayContext(provider="x"), "p")
    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    payload = json.loads(audit_records[0].message.split("llm_gateway_audit ", 1)[1])
    assert payload["config_snapshot"]["dry_run"] is True
    assert payload["config_snapshot"]["log_only"] is True  # Phase 1 は常に True


def test_interceptor_audit_handles_non_json_serializable_metadata(caplog):
    """metadata に Path 等の非 JSON-serializable な値が入っても audit が落ちない
    （PR #40 Copilot 1 回目指摘）"""
    config = LLMGatewayConfig(enabled=True, audit_log_enabled=True)
    interceptor = LLMGatewayInterceptor(config)
    # Path リテラルは「型が JSON-serializable でないこと」だけを表現したい。
    # SonarCloud S5443 を踏まないよう /tmp 直下や /var/tmp は避け、書き込み
    # しない読み取り専用相当の架空パスを使う（実 I/O は発生しない）。
    sample_path = Path("/example/llm-gateway/audit-fixture")
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="x", metadata={"path": sample_path}),
            "hello",
        )
    assert decision.decision == "log"
    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    assert len(audit_records) == 1
    payload = json.loads(audit_records[0].message.split("llm_gateway_audit ", 1)[1])
    # Path は default=str で文字列化される
    assert payload["context"]["metadata"]["path"] == str(sample_path)


def test_interceptor_skips_audit_when_audit_log_disabled(caplog):
    config = LLMGatewayConfig(enabled=True, audit_log_enabled=False)
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="claude_code"), "x"
        )
    assert decision.decision == "log"
    assert decision.audit_emitted is False
    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    assert audit_records == []


def test_interceptor_hash_is_deterministic_for_same_prompt(caplog):
    """同じ prompt は同じ prompt_hash を返す（dedupe / 再現性のため）。

    PR #40 Copilot 1 回目指摘: spy が常に真の sanity check に劣化していたので、
    実際の audit JSON を取得して `prompt_hash` 一致を検証する形に直す。
    """
    import hashlib

    config = LLMGatewayConfig(enabled=True, audit_log_enabled=True)
    interceptor = LLMGatewayInterceptor(config)

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        interceptor.intercept(LLMGatewayContext(provider="x"), "same")
        interceptor.intercept(LLMGatewayContext(provider="x"), "same")
        interceptor.intercept(LLMGatewayContext(provider="x"), "different")

    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    assert len(audit_records) == 3

    def _extract_hash(record_message: str) -> str:
        json_part = record_message.split("llm_gateway_audit ", 1)[1]
        return json.loads(json_part)["prompt_hash"]

    hashes = [_extract_hash(r.message) for r in audit_records]
    # 同じ prompt "same" を 2 回送ると hash が一致する
    assert hashes[0] == hashes[1]
    # 別 prompt は別 hash
    assert hashes[0] != hashes[2]
    # 期待値（sha256 16 桁 hex）と一致
    expected_same_hash = hashlib.sha256(b"same").hexdigest()[:16]
    assert hashes[0] == expected_same_hash


# ---------------------------------------------------------------------------
# ClaudeCodeClient → interceptor 配線
# ---------------------------------------------------------------------------


class _FakeShellResult:
    def __init__(self, stdout: str = "ok"):
        self.success = True
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""
        self.duration_ms = 10


def test_claude_code_client_invokes_interceptor_on_run(monkeypatch, caplog, tmp_path):
    """ClaudeCodeClient._run_claude_code が呼ばれると interceptor も呼ばれる"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.integrations.claude_code import ClaudeCodeClient

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    client = ClaudeCodeClient(working_dir=tmp_path)
    # claude コマンド検出をスキップ
    monkeypatch.setattr(
        ClaudeCodeClient, "claude_path", "/usr/bin/false"
    )
    # subprocess を mock して実 subprocess を起動しない
    monkeypatch.setattr(
        "hokusai.integrations.claude_code.ShellRunner",
        lambda cwd=None: type("S", (), {"run": lambda self, cmd, timeout: _FakeShellResult()})(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client._run_claude_code(
            "prompt body", timeout=10, gateway_purpose="test_purpose"
        )
    assert result == "ok"
    # interceptor が log を出している
    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    assert len(audit_records) == 1
    json_part = audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    payload = json.loads(json_part)
    assert payload["context"]["provider"] == "claude_code"
    assert payload["context"]["purpose"] == "test_purpose"
    assert payload["decision"] == "log"


def test_claude_code_client_interceptor_swallows_exceptions(monkeypatch, tmp_path):
    """interceptor が例外を投げても _run_claude_code は影響を受けない"""
    from hokusai.integrations.claude_code import ClaudeCodeClient

    client = ClaudeCodeClient(working_dir=tmp_path)
    monkeypatch.setattr(
        ClaudeCodeClient, "claude_path", "/usr/bin/false"
    )
    monkeypatch.setattr(
        "hokusai.integrations.claude_code.ShellRunner",
        lambda cwd=None: type("S", (), {"run": lambda self, cmd, timeout: _FakeShellResult("ok")})(),
    )

    # interceptor 内で例外を起こすために get_config を壊す
    def _raising_get_config():
        raise RuntimeError("config broken")

    monkeypatch.setattr("hokusai.config.get_config", _raising_get_config)

    # 例外が漏れないこと（returns ok）
    result = client._run_claude_code("prompt", timeout=10)
    assert result == "ok"


def test_claude_code_client_includes_append_system_prompt_hash_in_metadata(
    monkeypatch, caplog, tmp_path
):
    """`append_system_prompt` の hash / length が audit metadata に載る
    （PR #40 Copilot 1 回目指摘: CLI に追加される内容と audit hash/length が
    不一致になる問題への対応）"""
    import hashlib

    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.integrations.claude_code import ClaudeCodeClient

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    client = ClaudeCodeClient(working_dir=tmp_path)
    monkeypatch.setattr(ClaudeCodeClient, "claude_path", "/usr/bin/false")
    monkeypatch.setattr(
        "hokusai.integrations.claude_code.ShellRunner",
        lambda cwd=None: type("S", (), {"run": lambda self, cmd, timeout: _FakeShellResult()})(),
    )

    system_prompt = "You are restricted to read-only operations."
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        client._run_claude_code(
            "main prompt",
            timeout=10,
            append_system_prompt=system_prompt,
        )

    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    assert len(audit_records) == 1
    payload = json.loads(audit_records[0].message.split("llm_gateway_audit ", 1)[1])
    metadata = payload["context"]["metadata"]
    assert metadata["append_system_prompt_length"] == len(system_prompt)
    expected_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:16]
    assert metadata["append_system_prompt_hash"] == expected_hash


def test_claude_code_client_skips_interceptor_when_llm_gateway_missing(
    monkeypatch, caplog, tmp_path
):
    """llm_gateway 属性が config にない場合は interceptor 呼び出しを silent skip"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.integrations.claude_code import ClaudeCodeClient

    # WorkflowConfig を構築して llm_gateway を None にする（古い config 想定）
    cfg = WorkflowConfig()
    cfg.llm_gateway = None  # type: ignore[assignment]
    set_config(cfg)

    client = ClaudeCodeClient(working_dir=tmp_path)
    monkeypatch.setattr(ClaudeCodeClient, "claude_path", "/usr/bin/false")
    monkeypatch.setattr(
        "hokusai.integrations.claude_code.ShellRunner",
        lambda cwd=None: type("S", (), {"run": lambda self, cmd, timeout: _FakeShellResult()})(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client._run_claude_code("prompt", timeout=10)
    assert result == "ok"
    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    assert audit_records == []
