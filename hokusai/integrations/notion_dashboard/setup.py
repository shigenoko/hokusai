"""HOKUSAI Notion 初期セットアップ

Notion 上に HOKUSAI 用の DB / ページを一括作成する。

作成されるリソース:
- HOKUSAI Workflows DB
- HOKUSAI Pull Requests DB（Workflow → Workflows DB の relation 付き）
- HOKUSAI Service Status ページ

前提:
- 親ページ（parent_page_id）が事前に Notion 上に存在し、HOKUSAI integration が
  接続されていること
- API token が integration から発行済みで、対象ワークスペースに権限があること

設計判断:
- 冪等性は明示的に持たせない: 再実行すると新しい DB / ページが作られる。失敗時は
  Notion 側で archived/削除してから再実行することを想定。
- スキーマ定義はこのファイルにハードコード: 設定で外部化はしない。スキーマ変更は
  実装側のリリースに合わせて行うのが安全。
- relation は single_property: dual_property を使うと synced backref 名が固定で
  きないため。バックリンクが必要なら手動で設定する。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient

logger = get_logger("integrations.notion_dashboard.setup")


# ----- リソース名（運用ガイドの命名と一致させる） -----------------------
WORKFLOWS_DB_TITLE = "HOKUSAI Workflows DB"
PULL_REQUESTS_DB_TITLE = "HOKUSAI Pull Requests DB"
SERVICE_STATUS_PAGE_TITLE = "HOKUSAI Service Status"


# ----- DB 説明（手動編集を抑止する警告文） ------------------------------
# Notion 上で DB を開いた際の上部に常時表示される。スキーマ変更やレコード
# 改変を防ぐため、許可される編集箇所を明示する。
_WORKFLOWS_DB_DESCRIPTION = (
    "⚠️ HOKUSAI が自動管理する DB です。スキーマ（プロパティ）の追加・削除は"
    "行わないでください。HOKUSAI が書き込むプロパティ（Name / Workflow ID / "
    "Status / Current Phase / Current Phase Name / Waiting Reason / Next Action / "
    "GitLab MR / Research Page / Design Page / Plan Page / Started At / Completed At / "
    "Last Updated / Last Sync / Sync Errors / Error Summary）への手動編集は"
    "避けてください。人間が入力するプロパティ: Business Owner / Tech Lead / "
    "Priority / Assignee / GitLab Epic / GitLab Issue。詳細は HOKUSAI 運用ガイド"
    "（docs/notion-dashboard-operation-guide.md）を参照。"
)

_PULL_REQUESTS_DB_DESCRIPTION = (
    "⚠️ HOKUSAI が自動管理する DB です。レコードは HOKUSAI が PR 作成時（Phase 8a）"
    "に自動生成します。手動でのレコード作成、プロパティの追加・削除、HOKUSAI が"
    "書き込むプロパティ（PR Number / URL / Repository / Status / Workflow / "
    "Created At / Last Updated）への編集は行わないでください。Reviewer プロパティ"
    "のみ運用上の入力可能枠として用意しています。詳細は HOKUSAI 運用ガイド"
    "（docs/notion-dashboard-operation-guide.md）を参照。"
)


# ----- Workflows DB プロパティ定義 ----------------------------------------
# 実装計画書 §6.2 / 運用ガイド §2.2 と完全一致させる
_WORKFLOWS_DB_PROPERTIES: dict[str, dict[str, Any]] = {
    "Name": {"title": {}},
    "Workflow ID": {"rich_text": {}},
    "Status": {
        "select": {
            "options": [
                {"name": "Ready", "color": "default"},
                {"name": "Running", "color": "blue"},
                {"name": "Waiting for Human", "color": "yellow"},
                {"name": "Failed", "color": "red"},
                {"name": "Done", "color": "green"},
                {"name": "Canceled", "color": "gray"},
            ]
        }
    },
    "Current Phase": {"number": {"format": "number"}},
    "Current Phase Name": {"rich_text": {}},
    "Waiting Reason": {
        "select": {
            "options": [
                {"name": "branch_hygiene"},
                {"name": "cross_review_blocked"},
                {"name": "review_wait"},
                {"name": "copilot_review_wait"},
                {"name": "human_review_wait"},
                {"name": "review_fix"},
                {"name": "review_status"},
                {"name": "complete_review"},
            ]
        }
    },
    "Next Action": {"rich_text": {}},
    "Assignee": {"people": {}},
    "Business Owner": {"people": {}},
    "Tech Lead": {"people": {}},
    "Priority": {
        "select": {
            "options": [
                {"name": "High", "color": "red"},
                {"name": "Medium", "color": "yellow"},
                {"name": "Low", "color": "default"},
            ]
        }
    },
    "GitLab Epic": {"url": {}},
    "GitLab Issue": {"url": {}},
    "GitLab MR": {"url": {}},
    "Research Page": {"url": {}},
    "Design Page": {"url": {}},
    "Plan Page": {"url": {}},
    "Started At": {"date": {}},
    "Completed At": {"date": {}},
    "Last Updated": {"date": {}},
    "Last Sync": {"date": {}},
    "Sync Errors": {"rich_text": {}},
    "Error Summary": {"rich_text": {}},
}


# ----- Pull Requests DB プロパティ定義（Workflow relation を含む） ------
def _pr_db_properties(workflows_db_id: str) -> dict[str, dict[str, Any]]:
    return {
        "PR Number": {"title": {}},
        "URL": {"url": {}},
        "Repository": {
            "select": {
                "options": [
                    {"name": "Backend", "color": "blue"},
                    {"name": "Frontend", "color": "green"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "Draft", "color": "gray"},
                    {"name": "Open", "color": "blue"},
                    {"name": "Approved", "color": "green"},
                    {"name": "Merged", "color": "purple"},
                    {"name": "Closed", "color": "red"},
                ]
            }
        },
        "Workflow": {
            "relation": {
                "database_id": workflows_db_id,
                "single_property": {},
            }
        },
        "Reviewer": {"multi_select": {"options": []}},
        "Created At": {"date": {}},
        "Last Updated": {"date": {}},
    }


class NotionSetupError(Exception):
    """Notion セットアップ中の致命的エラー（呼び出し側へ伝搬する）"""


def setup_notion_workspace(
    api_token: str,
    parent_page_id: str,
    *,
    api_client: NotionAPIClient | None = None,
) -> dict[str, str]:
    """Notion ワークスペースに HOKUSAI 用 DB / ページを一括作成する。

    Args:
        api_token: HOKUSAI 専用 Notion Integration の Internal Integration Token
        parent_page_id: 親ページの ID（事前に integration を接続しておくこと）
        api_client: テスト用に NotionAPIClient を差し替える場合に指定

    Returns:
        {
            "workflows_db_id": "...",
            "pull_requests_db_id": "...",
            "service_status_page_id": "...",
        }

    Raises:
        NotionSetupError: いずれかのリソース作成に失敗した場合
    """
    if not api_token:
        raise NotionSetupError("api_token が空です")
    if not parent_page_id:
        raise NotionSetupError("parent_page_id が空です")

    api = api_client or NotionAPIClient(api_token=api_token)

    # 1. Workflows DB を先に作る（PR DB の relation で参照するため）
    logger.info("Workflows DB を作成中...")
    try:
        wf_db = api.create_database({
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [
                {"type": "text", "text": {"content": WORKFLOWS_DB_TITLE}}
            ],
            "description": [
                {"type": "text", "text": {"content": _WORKFLOWS_DB_DESCRIPTION}}
            ],
            "properties": _WORKFLOWS_DB_PROPERTIES,
        })
    except Exception as e:
        raise NotionSetupError(f"Workflows DB の作成に失敗: {type(e).__name__}: {e}") from e

    workflows_db_id = wf_db.get("id")
    if not workflows_db_id:
        raise NotionSetupError(
            "Workflows DB の作成レスポンスに id が含まれません"
        )

    # 2. Pull Requests DB を作る（Workflow → Workflows DB の relation を含める）
    logger.info("Pull Requests DB を作成中...")
    try:
        pr_db = api.create_database({
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [
                {"type": "text", "text": {"content": PULL_REQUESTS_DB_TITLE}}
            ],
            "description": [
                {"type": "text", "text": {"content": _PULL_REQUESTS_DB_DESCRIPTION}}
            ],
            "properties": _pr_db_properties(workflows_db_id),
        })
    except Exception as e:
        raise NotionSetupError(
            f"Pull Requests DB の作成に失敗: {type(e).__name__}: {e}"
        ) from e

    pull_requests_db_id = pr_db.get("id")
    if not pull_requests_db_id:
        raise NotionSetupError(
            "Pull Requests DB の作成レスポンスに id が含まれません"
        )

    # 3. Service Status ページを作る（サブページ。HOKUSAI が定期書き換え）
    logger.info("Service Status ページを作成中...")
    try:
        page = api.create_page({
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [
                        {"type": "text", "text": {"content": SERVICE_STATUS_PAGE_TITLE}}
                    ]
                }
            },
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": (
                                        "HOKUSAI が定期的にサービス接続状態を"
                                        "書き換えます。手動編集は反映されません。"
                                    )
                                },
                            }
                        ]
                    },
                }
            ],
        })
    except Exception as e:
        raise NotionSetupError(
            f"Service Status ページの作成に失敗: {type(e).__name__}: {e}"
        ) from e

    service_status_page_id = page.get("id")
    if not service_status_page_id:
        raise NotionSetupError(
            "Service Status ページの作成レスポンスに id が含まれません"
        )

    return {
        "workflows_db_id": workflows_db_id,
        "pull_requests_db_id": pull_requests_db_id,
        "service_status_page_id": service_status_page_id,
    }


# ---------------------------------------------------------------------------
# シェル rc ファイルへの環境変数書き込み（--persist 用）
# ---------------------------------------------------------------------------

# マーカー: ブロックを冪等に書き換えるために前後を囲む
PERSIST_BEGIN_MARKER = (
    "# === HOKUSAI Notion Dashboard (managed by `hokusai notion-setup`) ==="
)
PERSIST_END_MARKER = "# === END HOKUSAI Notion Dashboard ==="


def detect_shell_rc() -> Path:
    """SHELL 環境変数から rc ファイルパスを推測する。

    対応:
    - zsh → ~/.zshrc
    - bash → ~/.bashrc
    - その他 → ~/.profile
    """
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    return home / ".profile"


def persist_env_vars(
    rc_path: Path | str,
    ids: dict[str, str],
    *,
    backup: bool = True,
) -> dict[str, Any]:
    """HOKUSAI Notion ダッシュボード用の env vars を rc ファイルに書き込む。

    冪等性: マーカーで囲んだブロックを既存の rc ファイル内から検出し、
    あれば置き換え、なければ末尾に追記する。

    Args:
        rc_path: 書き込み先（~/.zshrc 等）
        ids: setup_notion_workspace の戻り値（workflows_db_id 等）
        backup: True なら書き込み前に <rc_path>.hokusai.bak を作成

    Returns:
        {
            "rc_path": str,
            "backup_path": str | None,
            "action": "appended" | "replaced",
            "block_text": str,
        }
    """
    rc_path = Path(rc_path).expanduser()

    block_lines = [
        PERSIST_BEGIN_MARKER,
        f"# Last updated: {datetime.now().isoformat()}",
        f'export HOKUSAI_NOTION_WORKFLOWS_DB_ID="{ids["workflows_db_id"]}"',
        f'export HOKUSAI_NOTION_PR_DB_ID="{ids["pull_requests_db_id"]}"',
        f'export HOKUSAI_NOTION_SERVICE_STATUS_PAGE_ID="{ids["service_status_page_id"]}"',
        PERSIST_END_MARKER,
    ]
    new_block = "\n".join(block_lines) + "\n"

    existing = rc_path.read_text() if rc_path.exists() else ""

    backup_path: Path | None = None
    if backup and rc_path.exists():
        backup_path = rc_path.with_suffix(rc_path.suffix + ".hokusai.bak")
        backup_path.write_text(existing)

    if PERSIST_BEGIN_MARKER in existing and PERSIST_END_MARKER in existing:
        # 既存ブロックを置き換え
        start_idx = existing.index(PERSIST_BEGIN_MARKER)
        end_idx = existing.index(PERSIST_END_MARKER) + len(PERSIST_END_MARKER)
        # 末尾の改行も含めて差し替え（次のコンテンツとの空行管理）
        if end_idx < len(existing) and existing[end_idx] == "\n":
            end_idx += 1
        new_content = existing[:start_idx] + new_block + existing[end_idx:]
        action = "replaced"
    else:
        # 末尾に追記
        if existing and not existing.endswith("\n"):
            existing += "\n"
        # 既存内容と空行をはさんで追加
        prefix = "\n" if existing else ""
        new_content = existing + prefix + new_block
        action = "appended"

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(new_content)

    return {
        "rc_path": str(rc_path),
        "backup_path": str(backup_path) if backup_path else None,
        "action": action,
        "block_text": new_block,
    }
