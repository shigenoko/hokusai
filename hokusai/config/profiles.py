"""Profile Registry

複数案件（A社 / B社 / C社など）を並列運用するための profile 機能の基盤。

設計判断（docs/hokusai-profile-parallel-execution-implementation-plan.md §4 参照）:
- profile は「切り替え対象」ではなく **明示的な実行スコープ**として扱う
- `1 OS プロセス = 1 profile` の契約（set_config シングルトンと整合）
- registry にはシークレット値を保存しない（config path と運用メタ情報のみ）
- registry が存在しなくても `-c/--config` 経由の従来運用は維持される
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# profile 名の制約: 英数小文字 + ハイフン / アンダースコア。先頭は英字。
# 案件名を URL / ファイル名 / SQL identifier として安全に扱えるようにする。
_PROFILE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

# registry の探索パス（環境変数 > home の標準位置）
# Note: 実装計画書 §5.1 では「repo 内 template（./hokusai-profiles.yaml）」も
# 候補として議論されていたが、v0.3.0 では実装していない。チーム共有テンプレが
# 必要になった場合は HOKUSAI_PROFILES_FILE 環境変数で repo 内 path を指す
# 運用で代替可能。
_DEFAULT_REGISTRY_ENV = "HOKUSAI_PROFILES_FILE"
_DEFAULT_REGISTRY_HOME = Path.home() / ".hokusai" / "profiles.yaml"


class ProfileError(Exception):
    """profile 関連のエラー基底"""


class ProfileRegistryNotFoundError(ProfileError):
    """profile registry ファイルが見つからない"""


class ProfileNotFoundError(ProfileError):
    """指定された profile が registry に存在しない"""


class InvalidProfileNameError(ProfileError):
    """profile 名の形式が不正"""


class ConflictingProfileAndConfigError(ProfileError):
    """--profile と --config が同時指定された"""


@dataclass
class ProfileConfig:
    """単一 profile の設定

    registry から解決された profile 情報を保持する。シークレット値は含まない。
    """

    name: str
    config_path: Path
    label: str | None = None
    data_dir: Path | None = None
    dashboard_port: int | None = None
    description: str | None = None


@dataclass
class ProfileRegistry:
    """profile registry 全体

    `~/.hokusai/profiles.yaml` をパースした結果を保持。
    """

    default_profile: str | None = None
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)
    source_path: Path | None = None

    def get(self, name: str) -> ProfileConfig:
        """指定 profile を取得。存在しない場合は ProfileNotFoundError"""
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles.keys())) or "(none)"
            raise ProfileNotFoundError(
                f"profile '{name}' は registry に存在しません。"
                f"利用可能な profile: {available}"
            )
        return self.profiles[name]

    def names(self) -> list[str]:
        """登録 profile 名一覧（ソート済み）"""
        return sorted(self.profiles.keys())


def validate_profile_name(name: str) -> None:
    """profile 名のバリデーション

    Raises:
        InvalidProfileNameError: 名前が不正な場合
    """
    if not name:
        raise InvalidProfileNameError("profile 名が空です")
    if not _PROFILE_NAME_PATTERN.match(name):
        raise InvalidProfileNameError(
            f"profile 名 '{name}' は不正です。"
            "英小文字で始まり、英数字 / ハイフン / アンダースコアのみ使用可能。"
        )


def resolve_registry_path(explicit: Path | str | None = None) -> Path:
    """registry ファイルパスを解決

    優先順:
    1. 引数で明示
    2. 環境変数 HOKUSAI_PROFILES_FILE
    3. ~/.hokusai/profiles.yaml

    Returns:
        探索順で最初に確定したパス（実在チェックはしない）
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get(_DEFAULT_REGISTRY_ENV)
    if env:
        return Path(env).expanduser()
    return _DEFAULT_REGISTRY_HOME


def load_profile_registry(registry_path: Path | str | None = None) -> ProfileRegistry:
    """profile registry ファイルをパース

    Args:
        registry_path: 明示パス。省略時は resolve_registry_path() で解決

    Returns:
        ProfileRegistry（profile が 0 件でも例外にせず空 registry を返す）

    Raises:
        ProfileRegistryNotFoundError: ファイルが存在しない場合
        InvalidProfileNameError: profile 名が不正
        ProfileError: YAML パース失敗、必須フィールド欠落
    """
    path = resolve_registry_path(registry_path)
    if not path.exists():
        raise ProfileRegistryNotFoundError(
            f"profile registry が見つかりません: {path}。"
            "registry を作成するか、-c/--config で直接設定ファイルを指定してください。"
        )

    try:
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ProfileError(
            f"profile registry の YAML パースに失敗: {path}: {e}"
        ) from e

    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile registry のトップレベルは dict である必要があります: {path}"
        )

    default_profile = raw.get("default_profile")
    if default_profile is not None and not isinstance(default_profile, str):
        raise ProfileError(
            f"default_profile は文字列である必要があります: got {type(default_profile).__name__}"
        )

    profiles_raw = raw.get("profiles", {})
    # `profiles:` のみ書いて値を省略すると YAML は null として読まれる。
    # これは「空 registry」として有効な定義なので、空 dict にフォールバック。
    if profiles_raw is None:
        profiles_raw = {}
    if not isinstance(profiles_raw, dict):
        raise ProfileError(
            f"profiles は dict である必要があります: got {type(profiles_raw).__name__}"
        )

    profiles: dict[str, ProfileConfig] = {}
    for name, entry in profiles_raw.items():
        if not isinstance(name, str):
            raise ProfileError(f"profile 名は文字列である必要があります: got {name!r}")
        validate_profile_name(name)

        if not isinstance(entry, dict):
            raise ProfileError(
                f"profile '{name}' の値は dict である必要があります: "
                f"got {type(entry).__name__}"
            )

        config_path_raw = entry.get("config")
        if not config_path_raw or not isinstance(config_path_raw, str):
            raise ProfileError(
                f"profile '{name}' に config (str) が指定されていません"
            )
        config_path = Path(config_path_raw).expanduser()

        data_dir_raw = entry.get("data_dir")
        data_dir: Path | None = None
        if data_dir_raw is not None:
            if not isinstance(data_dir_raw, str):
                raise ProfileError(
                    f"profile '{name}' の data_dir は文字列である必要があります: "
                    f"got {type(data_dir_raw).__name__}"
                )
            data_dir = Path(data_dir_raw).expanduser()

        dashboard_raw = entry.get("dashboard")
        dashboard_port: int | None = None
        if dashboard_raw is not None:
            if not isinstance(dashboard_raw, dict):
                raise ProfileError(
                    f"profile '{name}' の dashboard は dict である必要があります: "
                    f"got {type(dashboard_raw).__name__}"
                )
            port = dashboard_raw.get("port")
            if port is not None:
                # bool は Python では int のサブクラスなので明示的に除外
                if isinstance(port, bool) or not isinstance(port, int):
                    raise ProfileError(
                        f"profile '{name}' の dashboard.port は int である必要があります: "
                        f"got {type(port).__name__}"
                    )
                if not (1 <= port <= 65535):
                    raise ProfileError(
                        f"profile '{name}' の dashboard.port は 1〜65535 の範囲である必要があります: "
                        f"got {port}"
                    )
                dashboard_port = port

        label = entry.get("label")
        if label is not None and not isinstance(label, str):
            raise ProfileError(f"profile '{name}' の label は文字列である必要があります")

        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            raise ProfileError(
                f"profile '{name}' の description は文字列である必要があります"
            )

        profiles[name] = ProfileConfig(
            name=name,
            config_path=config_path,
            label=label,
            data_dir=data_dir,
            dashboard_port=dashboard_port,
            description=description,
        )

    if default_profile is not None and default_profile not in profiles:
        raise ProfileError(
            f"default_profile '{default_profile}' が profiles に存在しません"
        )

    return ProfileRegistry(
        default_profile=default_profile,
        profiles=profiles,
        source_path=path,
    )


def assert_profile_config_exclusive(
    profile_name: str | None,
    config_file: str | Path | None,
) -> None:
    """--profile と --config の同時指定をエラーにする

    Raises:
        ConflictingProfileAndConfigError: 両方指定された場合
    """
    if profile_name and config_file:
        raise ConflictingProfileAndConfigError(
            "--profile と --config / -c は同時に指定できません。"
            "暗黙の上書きは事故要因のため、どちらか一方のみ指定してください。"
        )


def find_workflow_in_other_profiles(
    workflow_id: str,
    current_profile: str | None,
    registry: ProfileRegistry,
) -> list[str]:
    """Phase E: workflow_id が他 profile の DB に存在するか探索。

    `hokusai --profile a-co continue wf-xxx` で対象 workflow が見つからない時、
    他 profile に存在するかを案内するために使う。

    Args:
        workflow_id: 検索対象の workflow_id
        current_profile: 既に探索済みの profile（除外する）
        registry: 探索対象の registry

    Returns:
        workflow が存在する profile 名のリスト（存在しない場合は空 list）。
    """
    from ..persistence.sqlite_store import SQLiteStore

    found_in: list[str] = []
    for name, profile in registry.profiles.items():
        if name == current_profile:
            continue
        db_path = _resolve_profile_database_path(profile)
        if db_path is None or not db_path.exists():
            continue
        try:
            store = SQLiteStore(db_path)
            if store.workflow_exists(workflow_id):
                found_in.append(name)
        except Exception:
            # 他 profile の DB に問題があってもここでは無視
            # （current profile の操作を妨げない）
            continue
    return found_in


def _resolve_profile_database_path(profile: "ProfileConfig") -> Path | None:
    """他 profile の DB ファイルパスを解決する（横断探索用ヘルパ）。

    優先順位:
    1. profile.config_path の YAML に `database_path` が明示されていればそれ
       （Phase C の上書き許可と整合）
    2. profile.data_dir が指定されていれば `data_dir / "workflow.db"`
    3. どちらも無ければ None（探索スキップ）

    config file の読み込みに失敗した場合（破損 YAML 等）は data_dir フォールバック。
    """
    # 1. config file の database_path 明示値を優先（false negative 防止）
    try:
        if profile.config_path.exists():
            with profile.config_path.open(encoding="utf-8") as f:
                cfg_raw = yaml.safe_load(f) or {}
            if isinstance(cfg_raw, dict):
                db_raw = cfg_raw.get("database_path")
                if isinstance(db_raw, str) and db_raw:
                    return Path(db_raw).expanduser()
    except Exception:
        # 破損 / 権限不足等はデフォルト経路にフォールバック
        pass

    # 2. data_dir / workflow.db のデフォルト位置
    if profile.data_dir is not None:
        return profile.data_dir / "workflow.db"

    # 3. 探索不能
    return None


def resolve_profile_to_config_path(
    profile_name: str,
    registry_path: Path | str | None = None,
) -> tuple[ProfileConfig, Path]:
    """profile 名から WorkflowConfig 用の設定ファイルパスを解決

    Args:
        profile_name: profile 名
        registry_path: registry ファイルパス（省略時は resolve_registry_path）

    Returns:
        (ProfileConfig, 実 config file path)

    Raises:
        InvalidProfileNameError / ProfileRegistryNotFoundError /
        ProfileNotFoundError / ProfileError
    """
    validate_profile_name(profile_name)
    registry = load_profile_registry(registry_path)
    profile = registry.get(profile_name)
    if not profile.config_path.exists():
        raise ProfileError(
            f"profile '{profile_name}' の config file が存在しません: "
            f"{profile.config_path}"
        )
    return profile, profile.config_path
