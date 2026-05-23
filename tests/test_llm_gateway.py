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

from hokusai.config import reset_config
from hokusai.config.loaders import _parse_llm_gateway_config
from hokusai.config.models import LLMGatewayConfig
from hokusai.llm_gateway import (
    LLMGatewayContext,
    LLMGatewayInterceptor,
)


@pytest.fixture(autouse=True)
def _reset_global_config():
    """各テストの前後で process-global config を必ず reset する。

    本ファイルの一部テストは `set_config(cfg)` で
    `hokusai.config.manager._config` を上書きする。後始末がないと後続テストに
    config が leak して order-dependent flakiness を生む。さらに**他テスト
    モジュール（例: test_dashboard_auth.py）が reset せずに set_config する
    場合、本ファイルの先頭テストが他ファイルから leak した config で開始
    してしまう**ため、yield の前後どちらでも reset する（PR #63 Copilot Round
    1-2 指摘）。
    """
    reset_config()
    try:
        yield
    finally:
        reset_config()


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
    """audit JSON の config_snapshot は LLMGatewayConfig の実値を反映する。

    PR #40 Copilot 2 回目指摘で config_snapshot を audit に載せたが、3 回目で
    「ハードコード/reason 推定ではなくユーザーが渡した実値を記録すべき」と
    指摘されたため、log_only=True / False / dry_run=True / False の組み合わせを
    変えて snapshot が一致することを検証する。
    """
    config_dry_run_log_only = LLMGatewayConfig(
        enabled=True, dry_run=True, log_only=True, audit_log_enabled=True
    )
    interceptor = LLMGatewayInterceptor(config_dry_run_log_only)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        interceptor.intercept(LLMGatewayContext(provider="x"), "p")
    audit_records = [r for r in caplog.records if "llm_gateway_audit" in r.message]
    payload = json.loads(audit_records[0].message.split("llm_gateway_audit ", 1)[1])
    assert payload["config_snapshot"] == {
        "enabled": True,
        "log_only": True,
        "dry_run": True,
        "audit_log_enabled": True,
    }

    caplog.clear()
    # log_only=False / dry_run=False の組み合わせでも実値が記録されること
    # （Phase 1 では挙動は変わらないが、Phase 5+ への移行検証用に audit には
    # 渡された値がそのまま残るべき）
    config_explicit_false = LLMGatewayConfig(
        enabled=True, dry_run=False, log_only=False, audit_log_enabled=True
    )
    interceptor2 = LLMGatewayInterceptor(config_explicit_false)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        interceptor2.intercept(LLMGatewayContext(provider="x"), "p")
    audit_records2 = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    payload2 = json.loads(
        audit_records2[0].message.split("llm_gateway_audit ", 1)[1]
    )
    assert payload2["config_snapshot"] == {
        "enabled": True,
        "log_only": False,
        "dry_run": False,
        "audit_log_enabled": True,
    }


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
# Phase 1 §8a: policy_hits 評価（Issue #60）
# allowed_providers / allowed_models.default / allowed_models.high_cost_requires_gate
# を log-only で評価し audit entry に積む。decision は "log" 据え置き。
# ---------------------------------------------------------------------------


def _make_config(**llm_kwargs):
    """LLMGatewayAllowedModelsConfig 等を差し込んだ config を組み立てる helper"""
    from hokusai.config.models import LLMGatewayAllowedModelsConfig

    allowed_models = llm_kwargs.pop(
        "allowed_models", LLMGatewayAllowedModelsConfig()
    )
    return LLMGatewayConfig(
        enabled=True, audit_log_enabled=True, allowed_models=allowed_models,
        **llm_kwargs,
    )


def _audit_payload(caplog):
    """caplog に流れた最後の audit log entry を JSON dict として返す"""
    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert audit_records, "audit log entry should be emitted"
    return json.loads(
        audit_records[-1].message.split("llm_gateway_audit ", 1)[1]
    )


def test_interceptor_policy_hits_empty_when_no_allowlist_configured(caplog):
    """allowed_providers=None / allowed_models.default=None / high_cost=[]
    のデフォルト状態では policy_hits は空（後方互換）"""
    config = _make_config()
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="claude_code", model="claude-3-opus"),
            "p",
        )
    assert decision.policy_hits == ()
    assert _audit_payload(caplog)["policy_hits"] == []


def test_interceptor_policy_hits_unknown_provider(caplog):
    """allowed_providers に含まれない provider → "unknown_provider" を hit"""
    config = _make_config(allowed_providers=["openai"])
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="anthropic"), "p"
        )
    assert "unknown_provider" in decision.policy_hits
    assert "unknown_provider" in _audit_payload(caplog)["policy_hits"]


def test_interceptor_policy_hits_skip_when_allowed_providers_none(caplog):
    """allowed_providers=None (未指定) は evaluation を skip（明示 [] と区別）"""
    config = _make_config(allowed_providers=None)
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="anything"), "p"
        )
    assert "unknown_provider" not in decision.policy_hits


def test_interceptor_policy_hits_unknown_provider_when_explicit_empty(caplog):
    """allowed_providers=[] (明示空) は「全 provider 拒否」の意図なので
    どの provider でも "unknown_provider" を hit する"""
    config = _make_config(allowed_providers=[])
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="anything"), "p"
        )
    assert "unknown_provider" in decision.policy_hits


def test_interceptor_policy_hits_unknown_model(caplog):
    """allowed_models.default に含まれない model → "unknown_model" を hit"""
    from hokusai.config.models import LLMGatewayAllowedModelsConfig

    config = _make_config(
        allowed_models=LLMGatewayAllowedModelsConfig(
            default=["claude-3-5-sonnet"],
        )
    )
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="claude_code", model="claude-3-opus"),
            "p",
        )
    assert "unknown_model" in decision.policy_hits


def test_interceptor_policy_hits_high_cost_model(caplog):
    """high_cost_requires_gate に含まれる model → "high_cost_model" を hit"""
    from hokusai.config.models import LLMGatewayAllowedModelsConfig

    config = _make_config(
        allowed_models=LLMGatewayAllowedModelsConfig(
            high_cost_requires_gate=["claude-3-opus"],
        )
    )
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="claude_code", model="claude-3-opus"),
            "p",
        )
    assert "high_cost_model" in decision.policy_hits


def test_interceptor_policy_hits_skip_when_model_empty(caplog):
    """context.model が空文字 (呼び出し側で取得不可) のときは allowed_models
    系の evaluation を skip（Copilot Round 1 指摘）。空を「allowlist にない」と
    判定すると常時 unknown_model hit で audit が誤検知だらけになるため。"""
    from hokusai.config.models import LLMGatewayAllowedModelsConfig

    config = _make_config(
        allowed_models=LLMGatewayAllowedModelsConfig(
            default=["claude-3-5-sonnet"],
            high_cost_requires_gate=["claude-3-opus"],
        )
    )
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            # model="" (default) — ClaudeCodeClient 等が model を埋めない実態に相当
            LLMGatewayContext(provider="claude_code"),
            "p",
        )
    assert "unknown_model" not in decision.policy_hits
    assert "high_cost_model" not in decision.policy_hits


def test_interceptor_policy_hits_multiple_hits(caplog):
    """複数 hit が同時に発生する: unknown_provider + unknown_model + high_cost"""
    from hokusai.config.models import LLMGatewayAllowedModelsConfig

    config = _make_config(
        allowed_providers=["openai"],
        allowed_models=LLMGatewayAllowedModelsConfig(
            default=["claude-3-5-sonnet"],
            high_cost_requires_gate=["claude-3-opus"],
        ),
    )
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(
                provider="anthropic", model="claude-3-opus"
            ),
            "p",
        )
    hits = set(decision.policy_hits)
    assert {"unknown_provider", "unknown_model", "high_cost_model"} <= hits


def test_interceptor_policy_hits_evaluated_in_dry_run(caplog):
    """dry_run でも policy_hits 評価は走る（log-only なので block しないだけ）"""
    from hokusai.config.models import LLMGatewayAllowedModelsConfig

    config = LLMGatewayConfig(
        enabled=True,
        dry_run=True,
        audit_log_enabled=True,
        allowed_models=LLMGatewayAllowedModelsConfig(
            high_cost_requires_gate=["claude-3-opus"],
        ),
    )
    interceptor = LLMGatewayInterceptor(config)
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        decision = interceptor.intercept(
            LLMGatewayContext(provider="claude_code", model="claude-3-opus"),
            "p",
        )
    assert decision.reason == "dry_run_log_only"
    assert "high_cost_model" in decision.policy_hits


def test_interceptor_policy_hits_omitted_when_gateway_disabled():
    """gateway disabled 時は evaluation を skip し policy_hits は空"""
    from hokusai.config.models import LLMGatewayAllowedModelsConfig

    config = LLMGatewayConfig(
        enabled=False,
        allowed_providers=["openai"],
        allowed_models=LLMGatewayAllowedModelsConfig(
            high_cost_requires_gate=["claude-3-opus"],
        ),
    )
    interceptor = LLMGatewayInterceptor(config)
    decision = interceptor.intercept(
        LLMGatewayContext(provider="anthropic", model="claude-3-opus"), "p"
    )
    assert decision.decision == "skipped"
    assert decision.policy_hits == ()


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


def test_claude_code_client_swallows_append_system_prompt_encode_error(
    monkeypatch, tmp_path
):
    """append_system_prompt の hash 計算が UnicodeEncodeError 等で失敗しても
    workflow を落とさず metadata を省略して継続する（PR #67 Copilot Round 1
    指摘）。encode は helper の try/except 範囲外で実行されるため、metadata
    構築自体を try/except でラップしている必要がある。"""
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
        lambda cwd=None: type(
            "S", (), {"run": lambda self, cmd, timeout: _FakeShellResult()}
        )(),
    )

    # 不正サロゲートを含む文字列 → utf-8 encode で UnicodeEncodeError
    bad_prompt = "valid\ud800tail"
    result = client._run_claude_code(
        "main", timeout=10, append_system_prompt=bad_prompt
    )
    # 例外が漏れず通常結果を返すこと
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


# ---------------------------------------------------------------------------
# CodexClient → interceptor 配線（Issue #62）
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    """subprocess.run の戻り値モック（CodexClient.review_document が使う形）"""

    def __init__(
        self,
        stdout: str = '{"summary": "ok", "issues": []}',
        stderr: str = "",
        returncode: int = 0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_codex_client(monkeypatch, model: str = "codex-mini-latest"):
    """CodexClient を codex コマンド検出スキップ付きで生成"""
    from hokusai.integrations.codex import CodexClient

    monkeypatch.setattr(
        CodexClient, "_find_codex_command", lambda self: "/usr/bin/false"
    )
    return CodexClient(model=model)


def test_codex_client_invokes_interceptor_on_review(monkeypatch, caplog):
    """CodexClient.review_document が呼ばれると interceptor も呼ばれ、
    provider="codex" / model=self.model が audit に記録される。"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    client = _make_codex_client(monkeypatch, model="codex-mini-latest")
    # 実 subprocess を起動しない
    monkeypatch.setattr(
        "hokusai.integrations.codex.subprocess.run",
        lambda *a, **kw: _FakeCompletedProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client.review_document(
            document="hello world", review_prompt="review please"
        )
    assert result == {"summary": "ok", "issues": []}

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert len(audit_records) == 1
    payload = json.loads(
        audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    )
    assert payload["context"]["provider"] == "codex"
    assert payload["context"]["model"] == "codex-mini-latest"
    assert payload["context"]["purpose"] == "cross_review"
    # schema_path 省略時は has_schema=False
    assert payload["context"]["metadata"]["has_schema"] is False
    assert payload["decision"] == "log"


def test_codex_client_interceptor_treats_empty_schema_path_as_missing(
    monkeypatch, caplog
):
    """schema_path="" は CLI に --output-schema を渡さない (`if schema_path:` で
    falsy) ため、audit の `has_schema` も False で揃える（PR #63 Copilot Round 3
    指摘: audit metadata と実 invocation の意味を一致させる）"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    client = _make_codex_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.codex.subprocess.run",
        lambda *a, **kw: _FakeCompletedProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        client.review_document(
            document="d", review_prompt="r", schema_path=""
        )

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert len(audit_records) == 1
    payload = json.loads(
        audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    )
    assert payload["context"]["metadata"]["has_schema"] is False


def test_codex_client_interceptor_records_has_schema_flag(monkeypatch, caplog):
    """schema_path を渡すと metadata.has_schema が True になる"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    client = _make_codex_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.codex.subprocess.run",
        lambda *a, **kw: _FakeCompletedProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        client.review_document(
            document="doc",
            review_prompt="prompt",
            schema_path="/fake/schema.json",
        )

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    # interceptor が必ず audit を 1 件出していることを先に確認
    # （IndexError マスクで本来の失敗原因が分かりにくくなるのを防ぐ、
    # PR #63 Copilot Round 1 指摘）
    assert len(audit_records) == 1
    payload = json.loads(
        audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    )
    assert payload["context"]["metadata"]["has_schema"] is True


def test_codex_client_interceptor_swallows_exceptions(monkeypatch):
    """interceptor が例外を投げても review_document は副作用なく続行する"""
    client = _make_codex_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.codex.subprocess.run",
        lambda *a, **kw: _FakeCompletedProcess(),
    )
    # interceptor 内で get_config を壊して例外を発生させる
    def _raising_get_config():
        raise RuntimeError("config broken")

    monkeypatch.setattr("hokusai.config.get_config", _raising_get_config)

    # 例外が漏れず通常結果を返すこと
    result = client.review_document(document="d", review_prompt="r")
    assert result == {"summary": "ok", "issues": []}


def test_codex_client_skips_interceptor_when_llm_gateway_missing(
    monkeypatch, caplog
):
    """llm_gateway 属性が config にない場合は audit を出さず透過する"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig()
    cfg.llm_gateway = None  # type: ignore[assignment]
    set_config(cfg)

    client = _make_codex_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.codex.subprocess.run",
        lambda *a, **kw: _FakeCompletedProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client.review_document(document="d", review_prompt="r")
    assert result == {"summary": "ok", "issues": []}
    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert audit_records == []


def test_codex_client_proceeds_when_gateway_disabled(monkeypatch, caplog):
    """gateway disabled でも subprocess 呼び出しは続行する（block しない）"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig(llm_gateway=LLMGatewayConfig(enabled=False))
    set_config(cfg)

    client = _make_codex_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.codex.subprocess.run",
        lambda *a, **kw: _FakeCompletedProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client.review_document(document="d", review_prompt="r")
    assert result == {"summary": "ok", "issues": []}
    # disabled なので audit log は出ない
    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert audit_records == []


# ---------------------------------------------------------------------------
# GeminiClient → interceptor 配線（Issue #64）
# review_document() / generate() の 2 callsite それぞれで interceptor が呼ばれ、
# context に provider="gemini" / model=self.model が記録されることを検証。
# ---------------------------------------------------------------------------


class _FakeGeminiProcess:
    """gemini CLI 経由 subprocess.run の戻り値モック"""

    def __init__(
        self,
        stdout: str = '{"findings": [], "overall_assessment": "ok", "summary": "g"}',
        stderr: str = "",
        returncode: int = 0,
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _make_gemini_client(monkeypatch, model: str = "gemini-2.5-pro"):
    """GeminiClient を gemini コマンド検出スキップ付きで生成"""
    from hokusai.integrations.gemini import GeminiClient

    monkeypatch.setattr(
        GeminiClient, "_find_gemini_command", staticmethod(lambda: "/usr/bin/false")
    )
    return GeminiClient(model=model)


def test_gemini_client_invokes_interceptor_on_review(monkeypatch, caplog):
    """GeminiClient.review_document が interceptor を呼び、provider=gemini /
    model=self.model / purpose=cross_review が audit に記録される"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    client = _make_gemini_client(monkeypatch, model="gemini-2.5-pro")
    monkeypatch.setattr(
        "hokusai.integrations.gemini.subprocess.run",
        lambda *a, **kw: _FakeGeminiProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client.review_document(
            document="doc", review_prompt="review"
        )
    assert result["overall_assessment"] == "ok"

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert len(audit_records) == 1
    payload = json.loads(
        audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    )
    assert payload["context"]["provider"] == "gemini"
    assert payload["context"]["model"] == "gemini-2.5-pro"
    assert payload["context"]["purpose"] == "cross_review"
    assert payload["context"]["metadata"]["has_schema"] is False


def test_gemini_client_invokes_interceptor_on_generate(monkeypatch, caplog):
    """GeminiClient.generate も interceptor を呼び purpose=generate /
    metadata.file_count が記録される"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    client = _make_gemini_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.gemini.subprocess.run",
        lambda *a, **kw: _FakeGeminiProcess(stdout="hello"),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client.generate(prompt="say hi")
    assert result == "hello"

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert len(audit_records) == 1
    payload = json.loads(
        audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    )
    assert payload["context"]["provider"] == "gemini"
    assert payload["context"]["purpose"] == "generate"
    # files 省略 → file_count == 0
    assert payload["context"]["metadata"]["file_count"] == 0


def test_gemini_client_interceptor_swallows_exceptions(monkeypatch):
    """interceptor が例外を投げても review_document は副作用なく続行する"""
    client = _make_gemini_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.gemini.subprocess.run",
        lambda *a, **kw: _FakeGeminiProcess(),
    )
    def _raising_get_config():
        raise RuntimeError("config broken")

    monkeypatch.setattr("hokusai.config.get_config", _raising_get_config)

    result = client.review_document(document="d", review_prompt="r")
    assert result["overall_assessment"] == "ok"


def test_gemini_client_skips_interceptor_when_llm_gateway_missing(
    monkeypatch, caplog
):
    """llm_gateway 属性が config にない場合は audit を出さず透過する"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig()
    cfg.llm_gateway = None  # type: ignore[assignment]
    set_config(cfg)

    client = _make_gemini_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.gemini.subprocess.run",
        lambda *a, **kw: _FakeGeminiProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client.review_document(document="d", review_prompt="r")
    assert result["overall_assessment"] == "ok"
    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert audit_records == []


def test_gemini_client_proceeds_when_gateway_disabled(monkeypatch, caplog):
    """gateway disabled でも subprocess 呼び出しは続行する"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig

    cfg = WorkflowConfig(llm_gateway=LLMGatewayConfig(enabled=False))
    set_config(cfg)

    client = _make_gemini_client(monkeypatch)
    monkeypatch.setattr(
        "hokusai.integrations.gemini.subprocess.run",
        lambda *a, **kw: _FakeGeminiProcess(),
    )

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        result = client.generate(prompt="p")
    assert result == '{"findings": [], "overall_assessment": "ok", "summary": "g"}'
    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert audit_records == []


# ---------------------------------------------------------------------------
# dispatch_via_gateway 単体テスト（Issue #66）
# 3 client から DRY 化した共通 helper の動作検証。各 client の既存テストは
# refactor 後も pass しているため、本セクションでは helper 自体の境界条件を
# 直接検証する。
# ---------------------------------------------------------------------------


def test_dispatch_helper_emits_audit_when_enabled(caplog):
    """gateway enabled なら provider/model/purpose/metadata が audit に載る"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.llm_gateway import dispatch_via_gateway

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        dispatch_via_gateway(
            provider="codex",
            model="codex-mini-latest",
            purpose="cross_review",
            prompt="hello",
            metadata={"flag": True},
        )

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert len(audit_records) == 1
    payload = json.loads(
        audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    )
    assert payload["context"]["provider"] == "codex"
    assert payload["context"]["model"] == "codex-mini-latest"
    assert payload["context"]["purpose"] == "cross_review"
    assert payload["context"]["metadata"]["flag"] is True


def test_dispatch_helper_skips_when_llm_gateway_missing(caplog):
    """config に llm_gateway 属性がない（古い config）→ silent skip"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.llm_gateway import dispatch_via_gateway

    cfg = WorkflowConfig()
    cfg.llm_gateway = None  # type: ignore[assignment]
    set_config(cfg)

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        dispatch_via_gateway(
            provider="claude_code",
            model="",
            purpose="any",
            prompt="p",
        )

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert audit_records == []


def test_dispatch_helper_skips_when_gateway_disabled(caplog):
    """gateway enabled=False → interceptor 内部で no-op、audit 出ない"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.llm_gateway import dispatch_via_gateway

    cfg = WorkflowConfig(llm_gateway=LLMGatewayConfig(enabled=False))
    set_config(cfg)

    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        dispatch_via_gateway(
            provider="gemini",
            model="gemini-2.5-pro",
            purpose="generate",
            prompt="p",
        )

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    assert audit_records == []


def test_dispatch_helper_swallows_exceptions(monkeypatch):
    """get_config が例外を投げても helper は静かに戻る（呼び出し側に伝播しない）"""
    from hokusai.llm_gateway import dispatch_via_gateway

    def _raising_get_config():
        raise RuntimeError("config broken")

    monkeypatch.setattr("hokusai.config.get_config", _raising_get_config)

    # 例外が漏れず None を返すこと（assert ではなく単に call して通ること）
    dispatch_via_gateway(
        provider="codex",
        model="m",
        purpose="cross_review",
        prompt="p",
    )


def test_dispatch_helper_sanitizes_exception_message_from_log(
    monkeypatch, caplog
):
    """例外メッセージに含まれる secret/PII が debug log にこぼれないことを検証
    （PR #67 Copilot Round 1 指摘）。`exc_info=True` を使うと exc.args の
    文字列が traceback に含まれてしまうため、`log_suppressed_exception` で
    type + frame だけ記録する設計にしている。"""
    from hokusai.llm_gateway import dispatch_via_gateway

    secret_text = "SECRET-12345-DO-NOT-LEAK"

    def _raising_get_config():
        raise RuntimeError(secret_text)

    monkeypatch.setattr("hokusai.config.get_config", _raising_get_config)

    with caplog.at_level(logging.DEBUG, logger="hokusai.llm_gateway"):
        dispatch_via_gateway(
            provider="codex", model="m", purpose="p", prompt="x"
        )

    # debug log は出ているが、secret 文字列は含まれない
    log_messages = " ".join(r.getMessage() for r in caplog.records)
    assert "LLM Gateway interceptor 内例外を抑制" in log_messages
    assert "RuntimeError" in log_messages  # 型名は OK
    assert secret_text not in log_messages  # メッセージ本文は NG


def test_log_suppressed_exception_does_not_leak_args(caplog):
    """`log_suppressed_exception` 単体で `str(exc)` が log に流れないことを検証"""
    from hokusai.llm_gateway.dispatch import log_suppressed_exception

    sensitive = "TOKEN=abc123_secret"
    try:
        raise ValueError(sensitive)
    except ValueError as exc:
        with caplog.at_level(logging.DEBUG, logger="hokusai.llm_gateway"):
            log_suppressed_exception("test prefix", exc)

    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert "test prefix" in log_text
    assert "ValueError" in log_text  # type 名は記録される
    assert sensitive not in log_text  # 例外メッセージは記録されない


def test_log_suppressed_exception_handles_null_traceback(caplog):
    """exc.__traceback__ が None の例外（未 raise 等）を渡しても本関数自体は
    例外を投げず空 frame として log を出す（PR #67 Copilot Round 2 指摘）"""
    from hokusai.llm_gateway.dispatch import log_suppressed_exception

    # 未 raise の例外は __traceback__ が None
    exc = RuntimeError("never raised")
    assert exc.__traceback__ is None

    with caplog.at_level(logging.DEBUG, logger="hokusai.llm_gateway"):
        # 関数自体が例外を投げないこと
        log_suppressed_exception("null tb test", exc)

    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert "null tb test" in log_text
    assert "RuntimeError" in log_text
    # 空 frame として記録される
    assert "frames=[]" in log_text


def test_dispatch_helper_swallows_import_error_at_callsite(monkeypatch, tmp_path):
    """3 client は dispatch module の import が失敗してもワークフローを
    落とさない（PR #67 Copilot Round 2 指摘）。CodexClient を例として検証。"""
    import sys

    from hokusai.integrations.codex import CodexClient

    # codex の `_find_codex_command` をスキップ
    monkeypatch.setattr(
        CodexClient, "_find_codex_command", lambda self: "/usr/bin/false"
    )
    client = CodexClient(model="codex-mini-latest")

    # subprocess 自体は mock しないと test 環境で失敗するのでパス。
    # 重要なのは _invoke_llm_gateway_interceptor が例外を漏らさないこと。
    # dispatch module import を壊して呼ぶ
    monkeypatch.setitem(sys.modules, "hokusai.llm_gateway.dispatch", None)

    # 直接 _invoke_llm_gateway_interceptor を呼ぶ
    # （review_document を経由すると subprocess も走るため）
    client._invoke_llm_gateway_interceptor("test prompt", has_schema=False)
    # 例外が漏れず戻ってくれば OK


def test_dispatch_helper_copies_metadata(caplog):
    """metadata は helper 内で dict コピーされ、呼び出し後に元 dict を書き換えても
    audit には影響しない（LLMGatewayContext が MappingProxyType でラップ済だが
    helper レイヤでも防御的にコピーする方針を維持）"""
    from hokusai.config import set_config
    from hokusai.config.models import WorkflowConfig
    from hokusai.llm_gateway import dispatch_via_gateway

    cfg = WorkflowConfig(
        llm_gateway=LLMGatewayConfig(enabled=True, audit_log_enabled=True),
    )
    set_config(cfg)

    src_metadata = {"key": "original"}
    with caplog.at_level(logging.INFO, logger="hokusai.llm_gateway"):
        dispatch_via_gateway(
            provider="codex",
            model="m",
            purpose="p",
            prompt="x",
            metadata=src_metadata,
        )
    # 呼び出し後に src_metadata を書き換える
    src_metadata["key"] = "mutated"

    audit_records = [
        r for r in caplog.records if "llm_gateway_audit" in r.message
    ]
    payload = json.loads(
        audit_records[0].message.split("llm_gateway_audit ", 1)[1]
    )
    # audit には呼び出し時の値が残る
    assert payload["context"]["metadata"]["key"] == "original"
