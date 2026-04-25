"""
Workflow constants and shared definitions.

Phase names, status icons, and numeric thresholds used across
the hokusai workflow modules.
"""

# ---------------------------------------------------------------------------
# Phase names (formal) -- used in progress records, workflow status display
# ---------------------------------------------------------------------------
PHASE_NAMES: dict[int, str] = {
    1: "タスク受領・準備",
    2: "事前調査",
    3: "設計",
    4: "作業計画",
    5: "実装",
    6: "検証",
    7: "最終レビュー",
    8: "PR作成",
    9: "レビュー対応",
    10: "進捗記録",
}

# ---------------------------------------------------------------------------
# Phase names (short) -- used in the dashboard UI
# ---------------------------------------------------------------------------
PHASE_SHORT_NAMES: dict[int, str] = {
    1: "準備",
    2: "調査",
    3: "設計",
    4: "計画",
    5: "実装",
    6: "検証",
    7: "レビュー",
    8: "PR作成",
    9: "レビュー対応",
    10: "記録",
}

# ---------------------------------------------------------------------------
# Status icons -- keyed by PhaseStatus *value* strings
# ---------------------------------------------------------------------------
PHASE_STATUS_ICONS: dict[str, str] = {
    "completed": "✅",
    "in_progress": "🟡",
    "failed": "❌",
    "skipped": "⏭️",
    "pending": "🔲",
}

# ---------------------------------------------------------------------------
# Magic-number constants
# ---------------------------------------------------------------------------

# Maximum length for generated branch names (integrations/git.py, phase7_5)
BRANCH_NAME_LIMIT: int = 50

# Commit count above which a squash is recommended (phase7_5)
COMMIT_THRESHOLD: int = 15

# Merge-commit count above which a rebase is recommended (phase7_5)
MERGE_COMMIT_THRESHOLD: int = 3

# Safety cap on workflow events per run (workflow.py)
MAX_WORKFLOW_EVENTS: int = 100

# Display limits for file lists in hygiene reports (phase7_5)
MAX_DISPLAY_FILES: int = 10
MAX_DISPLAY_FILES_SHORT: int = 5

# ---------------------------------------------------------------------------
# Notion Callout definitions
# ---------------------------------------------------------------------------
CALLOUT_WORKFLOW_START: dict[str, str] = {
    "icon": "🚀",
    "color": "blue_bg",
    "title": "Workflow",
}
CALLOUT_PULL_REQUESTS: dict[str, str] = {
    "icon": "📋",
    "color": "green_bg",
    "title": "Pull Requests",
}
CALLOUT_CROSS_REVIEW: dict[str, str] = {
    "icon": "🔍",
    "color": "yellow_bg",
    "title": "Cross-LLM Review",
}

# ---------------------------------------------------------------------------
# Cross-LLM Review prompts — 外部テンプレートへ移行済み
# 後方互換のため関数でアクセスするラッパーを提供
# ---------------------------------------------------------------------------
def _get_cross_review_prompt(phase: int) -> str:
    from .prompts import get_prompt

    prompt_id = f"cross_review.phase{phase}"
    try:
        return get_prompt(prompt_id)
    except KeyError:
        return ""


# 後方互換: dict ライクにアクセスできるラッパー
class _CrossReviewPromptsProxy:
    """dict[int, str] 互換のプロキシ。テンプレートファイルから読み込む。"""

    def get(self, phase: int, default: str = "") -> str:
        result = _get_cross_review_prompt(phase)
        return result if result else default

    def __getitem__(self, phase: int) -> str:
        result = _get_cross_review_prompt(phase)
        if not result:
            raise KeyError(phase)
        return result

    def __contains__(self, phase: int) -> bool:
        return bool(_get_cross_review_prompt(phase))


CROSS_REVIEW_PROMPTS: _CrossReviewPromptsProxy = _CrossReviewPromptsProxy()
