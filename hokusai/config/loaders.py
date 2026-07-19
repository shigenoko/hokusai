"""
Configuration Loaders

File loading and parsing functions for workflow configuration.
"""

import logging
import os
from pathlib import Path

import yaml

from .models import (
    SLACK_NOTIFICATION_EVENTS,
    CrossReviewConfig,
    DesignRateLimitConfig,
    DesignRetryConfig,
    DocOrchestrationConfig,
    FigmaIntegrationConfig,
    GitHostingConfig,
    LLMGatewayConfig,
    MiroIntegrationConfig,
    NotificationConfig,
    NotionDashboardConfig,
    NotionSyncOutboxConfig,
    NotionSyncRateLimitConfig,
    NotionSyncRetryConfig,
    RepositoryConfig,
    SlackNotificationConfig,
    TaskBackendConfig,
    WebDashboardAuthConfig,
    WebDashboardConfig,
)

_logger = logging.getLogger("hokusai.config.loaders")


def load_config_from_file(config_path: Path) -> dict:
    """設定ファイルを読み込む"""
    if not config_path.exists():
        return {}

    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def _str_or_default(value: object, default: str) -> str:
    """value が「中身のある文字列」ならそれを返し、そうでなければ default を返す。

    各パーサ関数で繰り返し使われるバリデーション。空文字や非 str を弾いて
    プロパティの env 名・realm 名などのデフォルトに戻す目的で使用する。
    """
    return value if isinstance(value, str) and value.strip() else default


def _parse_task_backend_config(config_dict: dict) -> TaskBackendConfig:
    """task_backend 設定をパース"""
    tb_config = config_dict.get("task_backend", {})

    if isinstance(tb_config, str):
        # 簡易形式: task_backend: notion
        return TaskBackendConfig(type=tb_config)

    if not isinstance(tb_config, dict):
        return TaskBackendConfig()

    return TaskBackendConfig(
        type=tb_config.get("type", "notion"),
        repo=tb_config.get("repo"),
        base_url=tb_config.get("base_url"),
        project_key=tb_config.get("project_key"),
        extra={k: v for k, v in tb_config.items() if k not in ["type", "repo", "base_url", "project_key"]},
    )


def _parse_git_hosting_config(config_dict: dict) -> GitHostingConfig:
    """git_hosting 設定をパース"""
    gh_config = config_dict.get("git_hosting", {})

    if isinstance(gh_config, str):
        # 簡易形式: git_hosting: github
        return GitHostingConfig(type=gh_config)

    if not isinstance(gh_config, dict):
        return GitHostingConfig()

    return GitHostingConfig(
        type=gh_config.get("type", "github"),
        base_url=gh_config.get("base_url", "https://gitlab.com"),
        project_path=gh_config.get("project_path"),
        workspace=gh_config.get("workspace"),
        repo_slug=gh_config.get("repo_slug"),
        extra={k: v for k, v in gh_config.items() if k not in ["type", "base_url", "project_path", "workspace", "repo_slug"]},
    )


def _parse_review_checklist(config_dict: dict) -> dict[str, dict]:
    """review_checklistをパース（新旧形式対応）

    新形式（推奨）:
        review_checklist:
          P01:
            name: "Firestore index確認"
            description: "collection group queryには必ずindexを確認"

    旧形式（後方互換）:
        review_checklist:
          - "Firestoreのcollection group queryには必ずindexを確認"

    Returns:
        {
            "P01": {"name": "Firestore index確認", "description": "..."},
            ...
        }
    """
    checklist = config_dict.get("review_checklist", [])

    if isinstance(checklist, list):
        # 旧形式: リスト → P01, P02... に変換
        return {
            f"P{i+1:02d}": {"name": item, "description": item}
            for i, item in enumerate(checklist)
        }
    elif isinstance(checklist, dict):
        # 新形式: そのまま使用（descriptionがない場合はnameで補完）
        result = {}
        for rule_id, rule_data in checklist.items():
            if isinstance(rule_data, dict):
                result[rule_id] = {
                    "name": rule_data.get("name", rule_id),
                    "description": rule_data.get("description", rule_data.get("name", "")),
                }
            elif isinstance(rule_data, str):
                # 簡易形式: P01: "ルール名"
                result[rule_id] = {"name": rule_data, "description": rule_data}
        return result
    return {}


def _parse_cross_review_config(config_dict: dict) -> CrossReviewConfig:
    """cross_review 設定をパース"""
    cr_config = config_dict.get("cross_review", {})

    if not isinstance(cr_config, dict):
        return CrossReviewConfig()

    phases = cr_config.get("phases", [2, 4])
    if not isinstance(phases, list):
        phases = [2, 4]
    parsed_phases: list[int] = []
    for phase in phases:
        if isinstance(phase, int) and 1 <= phase <= 10:
            parsed_phases.append(phase)
    if not parsed_phases:
        parsed_phases = [2, 4]

    on_failure = cr_config.get("on_failure", "warn")
    if on_failure not in {"warn", "block", "skip"}:
        on_failure = "warn"

    max_correction_rounds = cr_config.get("max_correction_rounds", 2)
    if not isinstance(max_correction_rounds, int) or max_correction_rounds < 1:
        max_correction_rounds = 2

    # provider バリデーション（v0.4.6〜）: 未知値は warning ログ出力 + 既定 codex に fallback
    raw_provider = cr_config.get("provider", "codex")
    if raw_provider in {"codex", "gemini"}:
        provider = raw_provider
    else:
        _logger.warning(
            "cross_review.provider=%r は未対応です（'codex' か 'gemini' を指定）。"
            "既定 'codex' にフォールバックします。タイポの可能性を確認してください。",
            raw_provider,
        )
        provider = "codex"

    return CrossReviewConfig(
        enabled=cr_config.get("enabled", False),
        provider=provider,
        model=cr_config.get("model", "codex-mini-latest"),
        phases=parsed_phases,
        timeout=cr_config.get("timeout", 300),
        on_failure=on_failure,
        max_correction_rounds=max_correction_rounds,
    )


def _parse_doc_orchestration_config(config_dict: dict) -> DocOrchestrationConfig:
    """doc_orchestration（Phase 0 doc-mode）設定をパースする。

    未指定 / 不正値は既定にフォールバックする。roles は role ごとに
    provider を検証し、未知 provider のエントリは既定にフォールバックする。
    """
    raw = config_dict.get("doc_orchestration", {})
    if not isinstance(raw, dict):
        return DocOrchestrationConfig()

    enabled = bool(raw.get("enabled", False))

    rounds = raw.get("rounds", 1)
    if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1:
        rounds = 1

    valid_providers = {"claude_code", "codex", "gemini"}
    default_roles = DocOrchestrationConfig().roles
    raw_roles = raw.get("roles")
    if not isinstance(raw_roles, dict):
        raw_roles = {}

    parsed_roles: dict = {}
    for role, default in default_roles.items():
        entry = raw_roles.get(role, default)
        if isinstance(entry, dict) and entry.get("provider") in valid_providers:
            parsed_roles[role] = {"provider": entry["provider"]}
        else:
            if isinstance(entry, dict) and entry.get("provider") is not None:
                _logger.warning(
                    "doc_orchestration.roles.%s.provider=%r は未対応です"
                    "（claude_code / codex / gemini）。既定にフォールバックします。",
                    role,
                    entry.get("provider"),
                )
            parsed_roles[role] = dict(default)

    model = raw.get("model", "")
    if not isinstance(model, str):
        model = ""

    return DocOrchestrationConfig(
        enabled=enabled,
        rounds=rounds,
        roles=parsed_roles,
        model=model,
    )


def _parse_notifications_config(config_dict: dict) -> NotificationConfig:
    """notifications 設定をパース

    設定例:
        notifications:
          slack:
            enabled: true
            webhook_url_env: HOKUSAI_SLACK_WEBHOOK_URL
            events:
              - waiting_for_human
              - workflow_failed
              - pr_created
              - workflow_completed
            timeout: 5.0

    バリデーション:
    - notifications が dict でなければデフォルト
    - slack.enabled は bool のみ採用
    - webhook_url_env が空文字ならデフォルト
    - events は既知イベントのみ採用、不正値のみなら events のデフォルトに戻す
    - timeout は 1.0 以上 30.0 以下にクランプ
    """
    notifications_raw = config_dict.get("notifications")
    if not isinstance(notifications_raw, dict):
        return NotificationConfig()

    slack_raw = notifications_raw.get("slack")
    if not isinstance(slack_raw, dict):
        return NotificationConfig()

    defaults = SlackNotificationConfig()

    enabled = slack_raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        enabled = defaults.enabled

    webhook_url_env = slack_raw.get("webhook_url_env", defaults.webhook_url_env)
    if not isinstance(webhook_url_env, str) or not webhook_url_env.strip():
        webhook_url_env = defaults.webhook_url_env

    events_raw = slack_raw.get("events", defaults.events)
    if isinstance(events_raw, list):
        valid_events = [
            evt for evt in events_raw
            if isinstance(evt, str) and evt in SLACK_NOTIFICATION_EVENTS
        ]
        events = valid_events if valid_events else list(defaults.events)
    else:
        events = list(defaults.events)

    timeout_raw = slack_raw.get("timeout", defaults.timeout)
    if isinstance(timeout_raw, (int, float)) and not isinstance(timeout_raw, bool):
        timeout = float(timeout_raw)
        if timeout < 1.0:
            timeout = 1.0
        elif timeout > 30.0:
            timeout = 30.0
    else:
        timeout = defaults.timeout

    return NotificationConfig(
        slack=SlackNotificationConfig(
            enabled=enabled,
            webhook_url_env=webhook_url_env,
            events=events,
            timeout=timeout,
        )
    )


def _parse_notion_dashboard_config(config_dict: dict) -> NotionDashboardConfig:
    """notion_dashboard 設定をパース

    設定例:
        notion_dashboard:
          enabled: true
          api_token_env: HOKUSAI_NOTION_API_TOKEN
          workflows_db_id_env: HOKUSAI_NOTION_WORKFLOWS_DB_ID
          pull_requests_db_id_env: HOKUSAI_NOTION_PR_DB_ID
          review_issues_db_id_env: HOKUSAI_NOTION_REVIEW_ISSUES_DB_ID
          work_items_db_id_env: HOKUSAI_NOTION_WORK_ITEMS_DB_ID
          sync_outbox:
            enabled: true
            max_retry_attempts: 10
          retry:
            max_attempts: 3
            backoff_seconds: 5
          rate_limit:
            requests_per_second: 2
            debounce_ms: 5000

    バリデーション方針:
    - notion_dashboard が dict でなければデフォルト
    - enabled は bool のみ採用
    - 各 _env キーは空文字以外の str のみ採用、それ以外はデフォルトに戻す
    - max_retry_attempts は 1 以上の int、それ以外はデフォルト
    - max_attempts は 1〜10 にクランプ
    - backoff_seconds は 0.5〜60 にクランプ
    - requests_per_second は 0.1〜10 にクランプ
    - debounce_ms は 0〜30000 にクランプ
    """
    nd_raw = config_dict.get("notion_dashboard")
    if not isinstance(nd_raw, dict):
        return NotionDashboardConfig()

    defaults = NotionDashboardConfig()

    enabled = nd_raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        enabled = defaults.enabled

    api_token_env = _str_or_default(nd_raw.get("api_token_env"), defaults.api_token_env)
    workflows_db_id_env = _str_or_default(
        nd_raw.get("workflows_db_id_env"), defaults.workflows_db_id_env
    )
    pull_requests_db_id_env = _str_or_default(
        nd_raw.get("pull_requests_db_id_env"), defaults.pull_requests_db_id_env
    )
    review_issues_db_id_env = _str_or_default(
        nd_raw.get("review_issues_db_id_env"), defaults.review_issues_db_id_env
    )
    work_items_db_id_env = _str_or_default(
        nd_raw.get("work_items_db_id_env"), defaults.work_items_db_id_env
    )
    workflow_gates_db_id_env = _str_or_default(
        nd_raw.get("workflow_gates_db_id_env"),
        defaults.workflow_gates_db_id_env,
    )
    project_memory_db_id_env = _str_or_default(
        nd_raw.get("project_memory_db_id_env"),
        defaults.project_memory_db_id_env,
    )

    sync_outbox = _parse_sync_outbox(nd_raw.get("sync_outbox"))
    retry = _parse_retry(nd_raw.get("retry"))
    rate_limit = _parse_rate_limit(nd_raw.get("rate_limit"))

    return NotionDashboardConfig(
        enabled=enabled,
        api_token_env=api_token_env,
        workflows_db_id_env=workflows_db_id_env,
        pull_requests_db_id_env=pull_requests_db_id_env,
        review_issues_db_id_env=review_issues_db_id_env,
        work_items_db_id_env=work_items_db_id_env,
        workflow_gates_db_id_env=workflow_gates_db_id_env,
        project_memory_db_id_env=project_memory_db_id_env,
        sync_outbox=sync_outbox,
        retry=retry,
        rate_limit=rate_limit,
    )


def _llm_gateway_enabled_env_override() -> bool | None:
    """`HOKUSAI_LLM_GATEWAY_ENABLED` env を boolean に解釈する。

    dogfooding-findings.md §7 F1 で記録した「yaml 編集なしの一時 enable 経路が
    無い」運用穴を埋めるための env override（PR #122）。truthy/falsy/未設定 を
    `True / False / None` に正規化し、None なら yaml 値（or default）を尊重する。

    truthy: "1" / "true" / "yes" / "on"（case-insensitive）
    falsy:  "0" / "false" / "no" / "off"（case-insensitive）
    その他（空文字 / 認識外の文字列）: `None`（override しない、yaml/default 維持）
    """
    raw = os.environ.get("HOKUSAI_LLM_GATEWAY_ENABLED")
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_llm_gateway_config(config_dict: dict) -> LLMGatewayConfig:
    """llm_gateway 設定をパース（#39 / v0.6.0〜、Issue #58 でフル schema 拡張）。

    設定例（要件 §4.1）:
        llm_gateway:
          enabled: true
          dry_run: false
          log_only: true
          audit_log_enabled: true
          allowed_providers: [openai, anthropic, google]
          allowed_models:
            default: [gpt-5.4, claude-sonnet-4.5]
            high_cost_requires_gate: [gpt-5.5, claude-opus-4.5]
          spend_cap:
            monthly_jpy: 50000
            daily_jpy: 5000
            per_workflow_jpy: 500
            per_phase_jpy: 200
            fail_mode: block
          pii_redaction:
            enabled: true
            rules: [email, jp_phone_number, credit_card]
            default_action: redact
            fail_mode: block
          approvals:
            high_cost_model: required
            pii_send_without_redaction: required
            policy_override: required
          audit:
            store_prompt_hash: true
            store_redacted_preview: true
            store_full_prompt: false

    バリデーション方針:
    - llm_gateway が dict でなければデフォルト
    - 各値は型 / enum 検証し、不正値は既定値にフォールバック（warning なし
      は将来的に追加検討、現状は静かに既定値）
    - allowed_providers / detector rules / model リストは「str 要素のみ」を
      抽出（dict / int 混入時に安全に str のみ採用）

    env override（PR #122 / F1）:
    - `HOKUSAI_LLM_GATEWAY_ENABLED` が truthy/falsy で指定されていれば
      `enabled` 値を yaml/default に対して上書きする。dogfooding 観察時に
      yaml 編集なしで一時 enable / disable できる経路（既存 yaml は維持）。
    - env が未設定 or 認識外文字列なら yaml/default を維持。
    """
    raw = config_dict.get("llm_gateway")
    defaults = LLMGatewayConfig()
    enabled_env = _llm_gateway_enabled_env_override()

    if not isinstance(raw, dict):
        # yaml セクションなし: env override があれば enabled だけ上書きする
        # （他フィールドは default のまま）。
        if enabled_env is not None:
            return LLMGatewayConfig(enabled=enabled_env)
        return LLMGatewayConfig()

    def _bool_or_default(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        return value if isinstance(value, bool) else default

    enabled = _bool_or_default("enabled", defaults.enabled)
    if enabled_env is not None:
        # env が yaml を上書き（dogfooding 一時 enable 用途）
        enabled = enabled_env

    return LLMGatewayConfig(
        enabled=enabled,
        dry_run=_bool_or_default("dry_run", defaults.dry_run),
        log_only=_bool_or_default("log_only", defaults.log_only),
        audit_log_enabled=_bool_or_default(
            "audit_log_enabled", defaults.audit_log_enabled
        ),
        allowed_providers=_parse_str_list_or_none(
            raw.get("allowed_providers"), key_present="allowed_providers" in raw
        ),
        allowed_models=_parse_llm_gateway_allowed_models(
            raw.get("allowed_models")
        ),
        spend_cap=_parse_llm_gateway_spend_cap(raw.get("spend_cap")),
        pii_redaction=_parse_llm_gateway_pii_redaction(
            raw.get("pii_redaction")
        ),
        approvals=_parse_llm_gateway_approvals(raw.get("approvals")),
        audit=_parse_llm_gateway_audit(raw.get("audit")),
    )


def _parse_str_list(raw: object, default: list[str]) -> list[str]:
    """YAML から list[str] を安全に抽出する共通 helper。

    None / 非 list は既定値を返し、list の場合は str 要素のみを採用する
    （dict や数値の混入を防ぐ）。
    """
    if not isinstance(raw, list):
        return list(default)
    return [v for v in raw if isinstance(v, str)]


def _parse_str_list_or_none(
    raw: object, *, key_present: bool
) -> list[str] | None:
    """YAML から「未指定 (None)」と「明示的に空配列 ([])」を区別する list[str] 抽出。

    要件 §4.2 の `allowed_providers` / `allowed_models.default` は必須項目
    だが、後方互換のため未指定（YAML にキーなし）も許容する。Issue #58 で
    `None` = 未指定 / `[]` = 明示空 allowlist の意味付けを採用（後続
    enforcement PR で deny-all 解釈を確定する）。

    Args:
        raw: YAML から取り出した生値（dict.get の戻り）
        key_present: YAML 上でキーが明示指定されていたか（`"key" in raw`）

    Returns:
        - キー未指定 → `None`
        - キーありかつ非 list → `None`（型不正なので未指定と同等扱い）
        - キーありかつ明示 `[]` → `[]`（ユーザーが明示した空 allowlist）
        - キーありかつ list（str 要素を含む）→ str 要素のみ抽出
        - キーありかつ非空 list だが str 要素が 1 つもない（例: `[42]`）→ `None`
          （filter 後 [] と explicit [] の semantic flip を防ぐため、型不正と
          同等扱いで未指定に倒す）
    """
    if not key_present:
        return None
    if not isinstance(raw, list):
        return None
    if not raw:
        # 明示的に空配列 → そのまま保持
        return []
    filtered = [v for v in raw if isinstance(v, str)]
    if not filtered:
        # 元 list は非空だが str が 1 つもない → 不正値として None に倒す
        # （explicit [] と区別する: filter 後 [] になると semantic が flip する）
        return None
    return filtered


def _parse_llm_gateway_allowed_models(raw: object):
    from .models import LLMGatewayAllowedModelsConfig

    defaults = LLMGatewayAllowedModelsConfig()
    if not isinstance(raw, dict):
        return defaults
    # default は None / [] / [...] を区別する（allowed_providers と同じ方針）
    default_value = _parse_str_list_or_none(
        raw.get("default"), key_present="default" in raw
    )
    return LLMGatewayAllowedModelsConfig(
        default=default_value,
        high_cost_requires_gate=_parse_str_list(
            raw.get("high_cost_requires_gate"),
            defaults.high_cost_requires_gate,
        ),
    )


def _parse_llm_gateway_spend_cap(raw: object):
    from ..llm_gateway.decisions import ALL_FAIL_MODES
    from .models import LLMGatewaySpendCapConfig

    defaults = LLMGatewaySpendCapConfig()
    if not isinstance(raw, dict):
        return defaults

    def _int_or_none(key: str) -> int | None:
        # 負値は上限金額として無意味（後続 enforcement で「常に超過」扱いに
        # なってしまう）ため、bool 除外に加え value >= 0 も検証する。
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    fail_mode_raw = raw.get("fail_mode", defaults.fail_mode)
    fail_mode = (
        fail_mode_raw
        if isinstance(fail_mode_raw, str) and fail_mode_raw in ALL_FAIL_MODES
        else defaults.fail_mode
    )
    return LLMGatewaySpendCapConfig(
        monthly_jpy=_int_or_none("monthly_jpy"),
        daily_jpy=_int_or_none("daily_jpy"),
        per_workflow_jpy=_int_or_none("per_workflow_jpy"),
        per_phase_jpy=_int_or_none("per_phase_jpy"),
        fail_mode=fail_mode,
    )


def _parse_llm_gateway_pii_redaction(raw: object):
    from ..llm_gateway.decisions import ALL_FAIL_MODES, ALL_REDACTION_ACTIONS
    from ..llm_gateway.policy import ALL_DETECTOR_RULES
    from .models import LLMGatewayPiiRedactionConfig

    defaults = LLMGatewayPiiRedactionConfig()
    if not isinstance(raw, dict):
        return defaults

    enabled_raw = raw.get("enabled", defaults.enabled)
    enabled = (
        enabled_raw if isinstance(enabled_raw, bool) else defaults.enabled
    )

    # rules は str のうち DetectorRule 列挙に含まれる値だけを採用
    rules_raw = raw.get("rules")
    if isinstance(rules_raw, list):
        rules = [v for v in rules_raw if isinstance(v, str) and v in ALL_DETECTOR_RULES]
    else:
        rules = list(defaults.rules)

    default_action_raw = raw.get("default_action", defaults.default_action)
    default_action = (
        default_action_raw
        if isinstance(default_action_raw, str)
        and default_action_raw in ALL_REDACTION_ACTIONS
        else defaults.default_action
    )

    fail_mode_raw = raw.get("fail_mode", defaults.fail_mode)
    fail_mode = (
        fail_mode_raw
        if isinstance(fail_mode_raw, str) and fail_mode_raw in ALL_FAIL_MODES
        else defaults.fail_mode
    )
    return LLMGatewayPiiRedactionConfig(
        enabled=enabled,
        rules=rules,
        default_action=default_action,
        fail_mode=fail_mode,
    )


# approvals.* の許容値（required / optional / disabled）
_ALL_APPROVAL_LEVELS = frozenset({"required", "optional", "disabled"})


def _parse_llm_gateway_approvals(raw: object):
    from .models import LLMGatewayApprovalsConfig

    defaults = LLMGatewayApprovalsConfig()
    if not isinstance(raw, dict):
        return defaults

    def _level_or_default(key: str, default: str) -> str:
        value = raw.get(key, default)
        return (
            value
            if isinstance(value, str) and value in _ALL_APPROVAL_LEVELS
            else default
        )

    return LLMGatewayApprovalsConfig(
        high_cost_model=_level_or_default(
            "high_cost_model", defaults.high_cost_model
        ),
        pii_send_without_redaction=_level_or_default(
            "pii_send_without_redaction", defaults.pii_send_without_redaction
        ),
        policy_override=_level_or_default(
            "policy_override", defaults.policy_override
        ),
    )


def _parse_llm_gateway_audit(raw: object):
    from .models import LLMGatewayAuditConfig

    defaults = LLMGatewayAuditConfig()
    if not isinstance(raw, dict):
        return defaults

    def _bool_or_default(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        return value if isinstance(value, bool) else default

    return LLMGatewayAuditConfig(
        store_prompt_hash=_bool_or_default(
            "store_prompt_hash", defaults.store_prompt_hash
        ),
        store_redacted_preview=_bool_or_default(
            "store_redacted_preview", defaults.store_redacted_preview
        ),
        store_full_prompt=_bool_or_default(
            "store_full_prompt", defaults.store_full_prompt
        ),
    )


def _parse_sync_outbox(raw: object) -> NotionSyncOutboxConfig:
    defaults = NotionSyncOutboxConfig()
    if not isinstance(raw, dict):
        return defaults

    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        enabled = defaults.enabled

    max_retry = raw.get("max_retry_attempts", defaults.max_retry_attempts)
    if not isinstance(max_retry, int) or isinstance(max_retry, bool) or max_retry < 1:
        max_retry = defaults.max_retry_attempts
    elif max_retry > 100:
        max_retry = 100

    # Issue #109 / fail-fast モードの YAML 読み取り（Copilot Round 3 指摘）。
    # bool 以外（int, str 等）は defaults の False に倒す。
    fail_fast = raw.get(
        "fail_fast_on_workflow_started_error",
        defaults.fail_fast_on_workflow_started_error,
    )
    if not isinstance(fail_fast, bool):
        fail_fast = defaults.fail_fast_on_workflow_started_error

    return NotionSyncOutboxConfig(
        enabled=enabled,
        max_retry_attempts=max_retry,
        fail_fast_on_workflow_started_error=fail_fast,
    )


def _parse_retry(raw: object) -> NotionSyncRetryConfig:
    defaults = NotionSyncRetryConfig()
    if not isinstance(raw, dict):
        return defaults

    max_attempts = raw.get("max_attempts", defaults.max_attempts)
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        max_attempts = defaults.max_attempts
    elif max_attempts > 10:
        max_attempts = 10

    backoff = raw.get("backoff_seconds", defaults.backoff_seconds)
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)):
        backoff = defaults.backoff_seconds
    else:
        backoff = float(backoff)
        if backoff < 0.5:
            backoff = 0.5
        elif backoff > 60:
            backoff = 60.0

    return NotionSyncRetryConfig(max_attempts=max_attempts, backoff_seconds=backoff)


def _parse_rate_limit(raw: object) -> NotionSyncRateLimitConfig:
    defaults = NotionSyncRateLimitConfig()
    if not isinstance(raw, dict):
        return defaults

    rps = raw.get("requests_per_second", defaults.requests_per_second)
    if isinstance(rps, bool) or not isinstance(rps, (int, float)):
        rps = defaults.requests_per_second
    else:
        rps = float(rps)
        if rps < 0.1:
            rps = 0.1
        elif rps > 10:
            rps = 10.0

    debounce = raw.get("debounce_ms", defaults.debounce_ms)
    if not isinstance(debounce, int) or isinstance(debounce, bool) or debounce < 0:
        debounce = defaults.debounce_ms
    elif debounce > 30000:
        debounce = 30000

    return NotionSyncRateLimitConfig(requests_per_second=rps, debounce_ms=debounce)


def _parse_web_dashboard_config(config_dict: dict) -> WebDashboardConfig:
    """web_dashboard 設定をパース

    設定例:
        web_dashboard:
          auth:
            enabled: true
            username_env: HOKUSAI_OPS_USERNAME
            password_env: HOKUSAI_OPS_PASSWORD
            realm: "HOKUSAI Operations Console"
    """
    raw = config_dict.get("web_dashboard")
    if not isinstance(raw, dict):
        return WebDashboardConfig()

    auth_raw = raw.get("auth")
    if not isinstance(auth_raw, dict):
        return WebDashboardConfig()

    defaults = WebDashboardAuthConfig()

    enabled = auth_raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        enabled = defaults.enabled

    return WebDashboardConfig(
        auth=WebDashboardAuthConfig(
            enabled=enabled,
            username_env=_str_or_default(
                auth_raw.get("username_env"), defaults.username_env
            ),
            password_env=_str_or_default(
                auth_raw.get("password_env"), defaults.password_env
            ),
            realm=_str_or_default(auth_raw.get("realm"), defaults.realm),
        )
    )


def _parse_design_retry(raw: object) -> DesignRetryConfig:
    defaults = DesignRetryConfig()
    if not isinstance(raw, dict):
        return defaults

    max_attempts = raw.get("max_attempts", defaults.max_attempts)
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        max_attempts = defaults.max_attempts
    elif max_attempts > 10:
        max_attempts = 10

    backoff = raw.get("backoff_seconds", defaults.backoff_seconds)
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)):
        backoff = defaults.backoff_seconds
    else:
        backoff = float(backoff)
        if backoff < 0.5:
            backoff = 0.5
        elif backoff > 60:
            backoff = 60.0

    return DesignRetryConfig(max_attempts=max_attempts, backoff_seconds=backoff)


def _parse_design_rate_limit(raw: object) -> DesignRateLimitConfig:
    defaults = DesignRateLimitConfig()
    if not isinstance(raw, dict):
        return defaults

    rps = raw.get("requests_per_second", defaults.requests_per_second)
    if isinstance(rps, bool) or not isinstance(rps, (int, float)):
        rps = defaults.requests_per_second
    else:
        rps = float(rps)
        if rps < 0.1:
            rps = 0.1
        elif rps > 10:
            rps = 10.0

    return DesignRateLimitConfig(requests_per_second=rps)


def _parse_writeback_config(raw: object):
    """Phase E (v0.4.0): figma.writeback / miro.writeback サブ設定をパース。

    設定例:
        writeback:
          enabled: true
          on_failure: warn

    無効値は既定（disabled, warn）にフォールバック。
    """
    from .models import WritebackConfig
    if not isinstance(raw, dict):
        return WritebackConfig()
    defaults = WritebackConfig()
    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        enabled = defaults.enabled
    on_failure = raw.get("on_failure", defaults.on_failure)
    if on_failure not in {"warn", "block", "skip"}:
        on_failure = defaults.on_failure
    return WritebackConfig(enabled=enabled, on_failure=on_failure)


def _parse_figma_config(config_dict: dict) -> FigmaIntegrationConfig:
    """figma 設定をパース

    設定例:
        figma:
          enabled: true
          api_token_env: HOKUSAI_FIGMA_API_TOKEN
          fetch_comments: true
          export_images: true
          cache_ttl_seconds: 1800
          timeout: 10.0
          on_failure: warn
          retry:
            max_attempts: 3
            backoff_seconds: 5
          rate_limit:
            requests_per_second: 1.5
          writeback:                # Phase E (v0.4.0)
            enabled: true
            on_failure: warn        # warn | block | skip
    """
    raw = config_dict.get("figma")
    if not isinstance(raw, dict):
        return FigmaIntegrationConfig()

    defaults = FigmaIntegrationConfig()

    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        enabled = defaults.enabled

    api_token_env = _str_or_default(raw.get("api_token_env"), defaults.api_token_env)

    fetch_comments = raw.get("fetch_comments", defaults.fetch_comments)
    if not isinstance(fetch_comments, bool):
        fetch_comments = defaults.fetch_comments

    export_images = raw.get("export_images", defaults.export_images)
    if not isinstance(export_images, bool):
        export_images = defaults.export_images

    cache_ttl = raw.get("cache_ttl_seconds", defaults.cache_ttl_seconds)
    if not isinstance(cache_ttl, int) or isinstance(cache_ttl, bool) or cache_ttl < 0:
        cache_ttl = defaults.cache_ttl_seconds
    elif cache_ttl > 86400:
        cache_ttl = 86400

    timeout_raw = raw.get("timeout", defaults.timeout)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
        timeout = defaults.timeout
    else:
        timeout = float(timeout_raw)
        if timeout < 1.0:
            timeout = 1.0
        elif timeout > 60.0:
            timeout = 60.0

    on_failure = raw.get("on_failure", defaults.on_failure)
    if on_failure not in {"warn", "block", "skip"}:
        on_failure = defaults.on_failure

    return FigmaIntegrationConfig(
        enabled=enabled,
        api_token_env=api_token_env,
        fetch_comments=fetch_comments,
        export_images=export_images,
        cache_ttl_seconds=cache_ttl,
        timeout=timeout,
        on_failure=on_failure,
        retry=_parse_design_retry(raw.get("retry")),
        rate_limit=_parse_design_rate_limit(raw.get("rate_limit")),
        writeback=_parse_writeback_config(raw.get("writeback")),
    )


def _parse_miro_config(config_dict: dict) -> MiroIntegrationConfig:
    """miro 設定をパース

    設定例:
        miro:
          enabled: true
          api_token_env: HOKUSAI_MIRO_API_TOKEN
          default_team_id_env: HOKUSAI_MIRO_TEAM_ID
          use_mcp: false
          cache_ttl_seconds: 1800
          timeout: 10.0
          on_failure: warn
          retry:
            max_attempts: 3
            backoff_seconds: 5
          rate_limit:
            requests_per_second: 1.5
    """
    raw = config_dict.get("miro")
    if not isinstance(raw, dict):
        return MiroIntegrationConfig()

    defaults = MiroIntegrationConfig()

    enabled = raw.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        enabled = defaults.enabled

    api_token_env = _str_or_default(raw.get("api_token_env"), defaults.api_token_env)
    default_team_id_env = _str_or_default(
        raw.get("default_team_id_env"), defaults.default_team_id_env
    )

    use_mcp = raw.get("use_mcp", defaults.use_mcp)
    if not isinstance(use_mcp, bool):
        use_mcp = defaults.use_mcp

    cache_ttl = raw.get("cache_ttl_seconds", defaults.cache_ttl_seconds)
    if not isinstance(cache_ttl, int) or isinstance(cache_ttl, bool) or cache_ttl < 0:
        cache_ttl = defaults.cache_ttl_seconds
    elif cache_ttl > 86400:
        cache_ttl = 86400

    timeout_raw = raw.get("timeout", defaults.timeout)
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
        timeout = defaults.timeout
    else:
        timeout = float(timeout_raw)
        if timeout < 1.0:
            timeout = 1.0
        elif timeout > 60.0:
            timeout = 60.0

    on_failure = raw.get("on_failure", defaults.on_failure)
    if on_failure not in {"warn", "block", "skip"}:
        on_failure = defaults.on_failure

    return MiroIntegrationConfig(
        enabled=enabled,
        api_token_env=api_token_env,
        default_team_id_env=default_team_id_env,
        use_mcp=use_mcp,
        cache_ttl_seconds=cache_ttl,
        timeout=timeout,
        on_failure=on_failure,
        retry=_parse_design_retry(raw.get("retry")),
        rate_limit=_parse_design_rate_limit(raw.get("rate_limit")),
        writeback=_parse_writeback_config(raw.get("writeback")),
    )


def _parse_repositories(config_dict: dict, default_base_branch: str = "main") -> list:
    """repositories設定をパース

    設定例:
        repositories:
          - name: Backend
            path: ~/repos/my-backend
            base_branch: develop
            default_target: true   # デフォルトでブランチ作成・実装の対象
          - name: API
            path: ~/repos/my-api
            base_branch: main
            default_target: false  # デフォルトでは対象外（必要時に手動指定）

    Returns:
        List[RepositoryConfig]
    """
    repos_config = config_dict.get("repositories", [])
    if not isinstance(repos_config, list):
        return []

    repositories = []
    for repo_data in repos_config:
        if not isinstance(repo_data, dict):
            continue

        name = repo_data.get("name", "")
        path_str = repo_data.get("path", "")
        if not name or not path_str:
            continue

        path = Path(path_str).expanduser()
        base_branch = repo_data.get("base_branch", default_base_branch)
        # default_target: 省略時はTrue（後方互換性のため）
        default_target = repo_data.get("default_target", True)

        repositories.append(RepositoryConfig(
            name=name,
            path=path,
            base_branch=base_branch,
            description=repo_data.get("description"),
            build_command=repo_data.get("build_command"),
            test_command=repo_data.get("test_command"),
            lint_command=repo_data.get("lint_command"),
            coding_rules=repo_data.get("coding_rules"),
            setup_command=repo_data.get("setup_command"),
            default_target=default_target,
        ))

    return repositories
