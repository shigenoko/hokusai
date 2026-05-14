"""
Configuration Loaders

File loading and parsing functions for workflow configuration.
"""

import logging
from pathlib import Path

import yaml

from .models import (
    SLACK_NOTIFICATION_EVENTS,
    CrossReviewConfig,
    DesignRateLimitConfig,
    DesignRetryConfig,
    FigmaIntegrationConfig,
    GitHostingConfig,
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

    sync_outbox = _parse_sync_outbox(nd_raw.get("sync_outbox"))
    retry = _parse_retry(nd_raw.get("retry"))
    rate_limit = _parse_rate_limit(nd_raw.get("rate_limit"))

    return NotionDashboardConfig(
        enabled=enabled,
        api_token_env=api_token_env,
        workflows_db_id_env=workflows_db_id_env,
        pull_requests_db_id_env=pull_requests_db_id_env,
        sync_outbox=sync_outbox,
        retry=retry,
        rate_limit=rate_limit,
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

    return NotionSyncOutboxConfig(enabled=enabled, max_retry_attempts=max_retry)


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
