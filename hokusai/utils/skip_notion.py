"""Profile-aware な Notion skip 判定 helper（Issue #111 / C. SKIP_NOTION profile 化）.

dogfooding-findings §1.3: ``HOKUSAI_SKIP_NOTION=1`` がプロセス全体に効くため、
profile を切り替えた後も別 profile の設定と整合せずに片方の経路だけ動く状況に
なっていた。本モジュールは ``HOKUSAI_SKIP_NOTION_<SLUG>`` (profile 単位) を
最優先で評価し、未指定なら legacy global ``HOKUSAI_SKIP_NOTION`` にフォールバック
することで、後方互換を維持しつつ profile-aware な skip を提供する。

評価順序:

1. 明示引数 ``profile_name`` があれば ``HOKUSAI_SKIP_NOTION_<SLUG>``
2. プロセス全体の ``HOKUSAI_ACTIVE_PROFILE`` env があれば同じ key
3. legacy global ``HOKUSAI_SKIP_NOTION``

いずれも値 ``"1"`` のみを「skip」と解釈する（他の truthy 表現は受け付けない）。

SLUG: ``profile_name`` を upper-case にし ``[^A-Z0-9]+`` を ``_`` に変換した文字列。
例: ``"4hokusai"`` → ``"4HOKUSAI"``、``"my-project"`` → ``"MY_PROJECT"``。
"""

from __future__ import annotations

import os
import re

ACTIVE_PROFILE_ENV = "HOKUSAI_ACTIVE_PROFILE"
LEGACY_GLOBAL_ENV = "HOKUSAI_SKIP_NOTION"


def profile_skip_env_name(profile_name: str) -> str:
    """profile 単位の skip env 名を返す。

    Args:
        profile_name: ``"4hokusai"`` / ``"my-project"`` 等

    Returns:
        ``"HOKUSAI_SKIP_NOTION_4HOKUSAI"`` / ``"HOKUSAI_SKIP_NOTION_MY_PROJECT"`` 等
    """
    slug = re.sub(r"[^A-Z0-9]+", "_", profile_name.upper()).strip("_")
    return f"HOKUSAI_SKIP_NOTION_{slug}"


def active_skip_env_name() -> str | None:
    """skip を起こしている env 変数名を返す（PR #112 Copilot Round 1 指摘）.

    warning / skip-reason 文言に正確な env 名（``HOKUSAI_SKIP_NOTION_<SLUG>`` か
    legacy global ``HOKUSAI_SKIP_NOTION`` か）を出すための補助 helper。
    skip 状態でないなら None を返す。``is_skip_notion`` と同じ評価順を踏襲。
    """
    active = os.environ.get(ACTIVE_PROFILE_ENV, "").strip()
    if active:
        suffix_name = profile_skip_env_name(active)
        if os.environ.get(suffix_name) == "1":
            return suffix_name
    if os.environ.get(LEGACY_GLOBAL_ENV) == "1":
        return LEGACY_GLOBAL_ENV
    return None


def is_skip_notion(profile_name: str | None = None) -> bool:
    """Notion 同期を skip するか判定する。

    Args:
        profile_name: 明示的に profile を指定する場合のみ渡す。未指定なら
            ``HOKUSAI_ACTIVE_PROFILE`` env から自動解決する。

    Returns:
        skip すべきなら True、Notion 同期を有効化すべきなら False。
    """
    # (1) 明示引数優先
    if profile_name:
        if os.environ.get(profile_skip_env_name(profile_name)) == "1":
            return True
    # (2) context env から profile 自動解決
    else:
        active = os.environ.get(ACTIVE_PROFILE_ENV, "").strip()
        if active and os.environ.get(profile_skip_env_name(active)) == "1":
            return True
    # (3) legacy global fallback（後方互換）
    return os.environ.get(LEGACY_GLOBAL_ENV) == "1"


def set_active_profile(profile_name: str | None) -> None:
    """``HOKUSAI_ACTIVE_PROFILE`` を setenv する（``main()`` から呼ぶ想定）.

    ``profile_name`` が None / 空文字なら何もしない（既存 env を消したり上書き
    したりしない）。
    """
    if profile_name and profile_name.strip():
        os.environ[ACTIVE_PROFILE_ENV] = profile_name.strip()
