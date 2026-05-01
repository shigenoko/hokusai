"""
Configuration Loaders

File loading and parsing functions for workflow configuration.
"""

from pathlib import Path

import yaml

from .models import (
    SLACK_NOTIFICATION_EVENTS,
    CrossReviewConfig,
    GitHostingConfig,
    NotificationConfig,
    RepositoryConfig,
    SlackNotificationConfig,
    TaskBackendConfig,
)


def load_config_from_file(config_path: Path) -> dict:
    """設定ファイルを読み込む"""
    if not config_path.exists():
        return {}

    with open(config_path) as f:
        return yaml.safe_load(f) or {}


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

    return CrossReviewConfig(
        enabled=cr_config.get("enabled", False),
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
