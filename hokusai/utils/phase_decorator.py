"""
Phase Node Decorator

フェーズノードの共通ボイラープレートを提供するデコレータモジュール。
"""

from functools import wraps

from ..state import (
    PhaseStatus,
    WorkflowState,
    add_audit_log,
    should_skip_phase,
    update_phase_status,
)


def phase_node(phase: int, action: str, *, skip_check: bool = True):
    """フェーズノードの共通ボイラープレートを提供するデコレータ。

    スキップ判定、ステータス更新、監査ログ記録を自動化する。
    デコレートされた関数は実処理のみを記述すればよい。

    Args:
        phase: フェーズ番号 (1-10)
        action: 監査ログに記録するアクション名
        skip_check: should_skip_phase()によるスキップ判定を行うか (デフォルト: True)

    Usage::

        @phase_node(phase=3, action="design_completed")
        def phase3_design_node(state: WorkflowState) -> WorkflowState:
            # 実処理のみを記述
            return state
    """

    def decorator(func):
        @wraps(func)
        def wrapper(state: WorkflowState) -> WorkflowState:
            # スキップチェック
            if skip_check and should_skip_phase(state, phase):
                print(f"\u23ed\ufe0f  Phase {phase} \u30b9\u30ad\u30c3\u30d7")
                return state

            state = update_phase_status(state, phase, PhaseStatus.IN_PROGRESS)

            try:
                state = func(state)

                state = update_phase_status(state, phase, PhaseStatus.COMPLETED)
                state = add_audit_log(state, phase, action, "success")

                print(f"\u2705 Phase {phase} \u5b8c\u4e86")

            except Exception as e:
                state = update_phase_status(
                    state, phase, PhaseStatus.FAILED, str(e)
                )
                state = add_audit_log(
                    state, phase, "phase_failed", "error", error=str(e)
                )
                print(f"\u274c Phase {phase} \u5931\u6557: {e}")
                raise

            return state

        return wrapper

    return decorator
