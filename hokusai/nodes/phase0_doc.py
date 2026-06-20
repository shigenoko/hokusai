"""Phase 0 doc-mode ノード（M1: 最小パス draft → crosscheck → finalize）

Issue #176 / 設計書「Phase 0 doc-mode ワークフロー」§5・§6 に対応する。

M1 スコープ:
- ``phase0b_draft`` / ``phase0c_crosscheck`` / ``phase0d_finalize`` の3ノード（線形）
- 各 LLM 呼び出しは ``dispatch_via_gateway``（purpose=draft/review/finalize、
  phase=0）を必ず経由し、audit_logs に記録される
- 実生成バックエンドは差し替え可能（``set_generation_backend``）。M2 以降で
  role→provider を実 client（ClaudeCodeClient / CodexClient / GeminiClient）へ
  束ねる。M1 では「配線」を完成させ、生成本体は注入する。

ideation（S0）/ 型NGループ / HITL ゲートは後続マイルストーン（M3/M4/M5）。
"""

from __future__ import annotations

from typing import Callable, Optional

from ..config import get_config
from ..llm_gateway import dispatch_via_gateway
from ..logging_config import get_logger
from ..state import DocWorkflowState, add_audit_log

logger = get_logger("phase0_doc")

# doc-mode は phase 0 として audit に記録する
PHASE = 0

# 型の必須セクション（finalize の型準拠チェック用）。M1 は簡易な包含チェック。
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "requirements": ["背景", "業務要件", "スコープ", "受入基準", "制約", "参照"],
    "design": [
        "概要",
        "アーキ",
        "データモデル",
        "インターフェース",
        "フェーズ",
        "リスク",
    ],
}


# 実生成バックエンド: (provider, model, prompt) -> 生成テキスト
GenerationBackend = Callable[[str, str, str], str]
_generation_backend: Optional[GenerationBackend] = None


def set_generation_backend(fn: Optional[GenerationBackend]) -> None:
    """doc-mode の実生成バックエンドを束縛/解除する。

    M1 ではここに provider client を束ねる配線が未実装のため、利用側
    （テスト含む）で明示的に注入する。``None`` を渡すと解除する。
    """
    global _generation_backend
    _generation_backend = fn


def _role_provider(role: str) -> tuple[str, str]:
    """設定から role に対応する (provider, model) を解決する。

    ``doc_orchestration.roles`` 未設定時は安全側に ``claude_code`` を返す。
    """
    cfg = getattr(get_config(), "doc_orchestration", None)
    roles = getattr(cfg, "roles", {}) if cfg is not None else {}
    provider = (roles.get(role) or {}).get("provider", "claude_code")
    model = getattr(cfg, "model", "") if cfg is not None else ""
    return provider, model


def invoke_llm(
    provider: str,
    model: str,
    prompt: str,
    *,
    purpose: str,
    workflow_id: Optional[str] = None,
) -> str:
    """LLM Gateway を必ず経由してから実生成を行う。

    ``dispatch_via_gateway`` は送信前 interceptor（既定 log-only）で、
    provider/model/purpose/phase を audit_logs に記録する。実生成は
    ``set_generation_backend`` で束ねたバックエンドに委譲する。
    """
    dispatch_via_gateway(
        provider=provider,
        model=model,
        purpose=purpose,
        prompt=prompt,
        workflow_id=workflow_id,
        phase=PHASE,
    )
    backend = _generation_backend or default_generation_backend
    return backend(provider, model, prompt)


class DocModeProviderError(RuntimeError):
    """provider client の実行に失敗したことを表す（CLI 未導入等）。"""


def _coerce_text(value: object) -> str:
    """provider client の戻り値（str / dict）をテキストに正規化する。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("final_doc", "document", "summary", "review", "content", "text"):
            if value.get(key):
                return str(value[key])
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)


def default_generation_backend(provider: str, model: str, prompt: str) -> str:
    """M2: role→provider を実 client に束ねた既定の生成バックエンド。

    provider ごとに能力が異なるため、最も自然な生成系メソッドへ振り分ける:
    - ``claude_code`` → ``ClaudeCodeClient.execute_prompt``
    - ``gemini``      → ``GeminiClient.generate``
    - ``codex``       → ``CodexClient.review_document``（レビュー特化のため流用）

    provider CLI 未導入等で実行できない場合は ``DocModeProviderError`` を送出し、
    どの provider で失敗したかを明示する（graceful degrade: 例外を握り潰さず
    呼び出し側で扱えるようにする）。
    """
    try:
        if provider == "claude_code":
            from ..integrations.claude_code import ClaudeCodeClient

            return ClaudeCodeClient().execute_prompt(prompt)
        if provider == "gemini":
            from ..integrations.gemini import GeminiClient

            client = GeminiClient(model=model) if model else GeminiClient()
            return client.generate(prompt)
        if provider == "codex":
            from ..integrations.codex import CodexClient

            client = CodexClient(model=model) if model else CodexClient()
            result = client.review_document(
                document=prompt,
                review_prompt="次の文書をレビューし、修正案を日本語で挙げてください。",
            )
            return _coerce_text(result)
    except (FileNotFoundError, RuntimeError, TimeoutError) as exc:
        raise DocModeProviderError(
            f"provider '{provider}' の実行に失敗しました（CLI 未導入の可能性）: {exc}"
        ) from exc

    raise ValueError(f"未知の provider: {provider}")


def check_template(doc_type: str, text: str) -> dict:
    """型の必須セクションが含まれるかを簡易チェックする。"""
    required = REQUIRED_SECTIONS.get(doc_type, [])
    missing = [section for section in required if section not in (text or "")]
    return {"ok": not missing, "missing": missing}


# 壁打ちルールブック（M3）。出力ゲートではなく「方法のプロンプト」として注入する。
IDEATION_RULEBOOK = (
    "以下の技法を用いて発散的に検討せよ（これは出力ゲートではなく方法である）:\n"
    "- 代替案を3つ以上挙げる\n"
    "- 各案に steelman（最強の反論）を付す\n"
    "- 暗黙の前提を可視化する\n"
    "- red-team: リスク・破綻シナリオを先回りする\n"
    "- SCAMPER で発想を広げる\n"
)


def _ideation_prompt(state: DocWorkflowState) -> str:
    return (
        f"トピック: {state['topic']}\n\n"
        f"{IDEATION_RULEBOOK}\n"
        f"上記に沿って、{state['doc_type']} の検討材料を発散的に出力せよ。"
    )


def phase0a_ideation_node(state: DocWorkflowState) -> DocWorkflowState:
    """ideator が壁打ちルールブックに沿って発散検討を行う（軽い統制）。"""
    provider, model = _role_provider("ideator")
    state["ideation_result"] = invoke_llm(
        provider,
        model,
        _ideation_prompt(state),
        purpose="ideation",
        workflow_id=state["workflow_id"],
    )
    state["current_step"] = "ideation"
    add_audit_log(
        state, PHASE, "phase0a_ideation", "completed", {"provider": provider}
    )
    return state


def _draft_prompt(state: DocWorkflowState) -> str:
    return (
        f"次のトピックについて、{state['doc_type']} を型に沿って作成してください。\n"
        f"トピック: {state['topic']}\n"
        f"アイデア出し結果:\n{state.get('ideation_result', '')}\n"
    )


def _crosscheck_prompt(state: DocWorkflowState) -> str:
    return (
        "次のドラフトをレビューし、抜け漏れ・矛盾・受入基準の検証可能性の観点で"
        "修正案を箇条書きで挙げてください。\n\n"
        f"--- draft ---\n{state['draft']}\n"
    )


def _finalize_prompt(state: DocWorkflowState) -> str:
    notes = "\n".join(state.get("review_notes", []))
    return (
        "次のドラフトにレビュー指摘を反映し、型に沿った確定稿を出力してください。\n\n"
        f"--- draft ---\n{state['draft']}\n\n"
        f"--- review notes ---\n{notes}\n"
    )


def phase0b_draft_node(state: DocWorkflowState) -> DocWorkflowState:
    """drafter が型に沿って初稿を生成する。"""
    provider, model = _role_provider("drafter")
    state["draft"] = invoke_llm(
        provider,
        model,
        _draft_prompt(state),
        purpose="draft",
        workflow_id=state["workflow_id"],
    )
    state["current_step"] = "draft"
    add_audit_log(state, PHASE, "phase0b_draft", "completed", {"provider": provider})
    return state


def phase0c_crosscheck_node(state: DocWorkflowState) -> DocWorkflowState:
    """reviewer が別 provider で指摘・修正案を出す。"""
    provider, model = _role_provider("reviewer")
    notes = invoke_llm(
        provider,
        model,
        _crosscheck_prompt(state),
        purpose="review",
        workflow_id=state["workflow_id"],
    )
    state["review_notes"].append(notes)
    state["round"] = state.get("round", 0) + 1
    state["current_step"] = "crosscheck"
    add_audit_log(
        state,
        PHASE,
        "phase0c_crosscheck",
        "completed",
        {"provider": provider, "round": state["round"]},
    )
    return state


def phase0d_finalize_node(state: DocWorkflowState) -> DocWorkflowState:
    """finalizer が指摘を反映し、型準拠チェック付きで確定稿を出す。"""
    provider, model = _role_provider("finalizer")
    final = invoke_llm(
        provider,
        model,
        _finalize_prompt(state),
        purpose="finalize",
        workflow_id=state["workflow_id"],
    )
    state["final_doc"] = final
    state["template_check"] = check_template(state["doc_type"], final)
    state["finalize_attempts"] = state.get("finalize_attempts", 0) + 1
    state["current_step"] = "finalize"
    add_audit_log(
        state,
        PHASE,
        "phase0d_finalize",
        "completed",
        {
            "provider": provider,
            "template_ok": state["template_check"]["ok"],
            "finalize_attempts": state["finalize_attempts"],
        },
    )
    return state


# === ルーティング（M4: rounds ループ / 型NG→draft 戻し、上限つき）===

# 条件分岐の戻り値。グラフ側のマッピングキーと一致させる。
ROUTE_CROSSCHECK = "phase0c_crosscheck"
ROUTE_FINALIZE = "phase0d_finalize"
ROUTE_DRAFT = "phase0b_draft"
ROUTE_END = "END"


def should_continue_crosscheck(state: DocWorkflowState) -> str:
    """crosscheck を ``max_rounds`` 回まで繰り返し、それ以外は finalize へ。"""
    if state.get("round", 0) < state.get("max_rounds", 1):
        return ROUTE_CROSSCHECK
    return ROUTE_FINALIZE


def should_fix_template(state: DocWorkflowState) -> str:
    """型NG かつ上限未満なら draft に戻して再生成、そうでなければ終了。"""
    check = state.get("template_check") or {}
    attempts = state.get("finalize_attempts", 0)
    if not check.get("ok", True) and attempts < state.get("max_finalize_rounds", 2):
        return ROUTE_DRAFT
    return ROUTE_END
