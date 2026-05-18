"""HOKUSAI Notion 初期セットアップ

Notion 上に HOKUSAI 用の DB / ページを一括作成する。

作成されるリソース:
- Workflows DB
- Pull Requests DB（Workflow → Workflows DB の relation 付き）
- Review Issues DB（Workflow → Workflows DB の relation 付き、v0.5.0〜 / #36）

前提:
- 親ページ（parent_page_id）が事前に Notion 上に存在し、HOKUSAI integration が
  接続されていること
- API token が integration から発行済みで、対象ワークスペースに権限があること

設計判断:
- 冪等性は **DB 作成と scaffold ページで分ける**:
    - DB 作成（Workflows / Pull Requests / Review Issues）: 冪等ではない。
      再実行すると新しい DB が作られる。失敗時は Notion 側で archived/削除
      してから再実行することを想定。
    - `--scaffold` で作るドキュメントツリー: ハブ `Documentation`（icon 📚）と配下
      3 サブページ `議論`（💬）/ `運用ガイド`（📖）/ `要件定義`（📋）。idempotent で
      配置先パスごとに既存検出（pagination 全走査）。v0.4.3（絵文字 prefix 付き）
      / v0.4.4（HOKUSAI prefix + 英語名）の旧タイトルも 2 世代分 legacy alias として
      検出する（canonical 優先）。Issue #25 / v0.4.3 / v0.4.4 / v0.4.5。
- スキーマ定義はこのファイルにハードコード: 設定で外部化はしない。スキーマ変更は
  実装側のリリースに合わせて行うのが安全。
- relation は single_property: dual_property を使うと synced backref 名が固定で
  きないため。バックリンクが必要なら手動で設定する。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ...logging_config import get_logger
from .client import NotionAPIClient

logger = get_logger("integrations.notion_dashboard.setup")


# ----- リソース名（運用ガイドの命名と一致させる） -----------------------
# v0.4.5（Issue #29）以降: 親ページが HOKUSAI 文脈で配置される想定のため、
# 配下リソースから冗長な HOKUSAI prefix を削除。識別性は DB description の
# 警告文と親ページ名で確保する。
WORKFLOWS_DB_TITLE = "Workflows DB"
PULL_REQUESTS_DB_TITLE = "Pull Requests DB"
REVIEW_ISSUES_DB_TITLE = "Review Issues DB"


# 各 DB スキーマで共通利用するプロパティ名定数（重複文字列を一元化）
_PROP_LAST_UPDATED = "Last Updated"


# ----- DB 説明（手動編集を抑止する警告文） ------------------------------
# Notion 上で DB を開いた際の上部に常時表示される。スキーマ変更やレコード
# 改変を防ぐため、許可される編集箇所を明示する。
_WORKFLOWS_DB_DESCRIPTION = (
    "⚠️ HOKUSAI が自動管理する DB です。スキーマ（プロパティ）の追加・削除は"
    "行わないでください。HOKUSAI が書き込むプロパティ（Name / Workflow ID / "
    "Status / Current Phase / Current Phase Name / Waiting Reason / Next Action / "
    "GitLab MR / Research Page / Design Page / Plan Page / Started At / Completed At / "
    "Last Updated / Last Sync / Sync Errors / Error Summary / Operator）への手動編集は"
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

_REVIEW_ISSUES_DB_DESCRIPTION = (
    "⚠️ HOKUSAI が自動管理する DB です。レコードは HOKUSAI が Phase 6 verification "
    "failure / Phase 7 final review 等の指摘発生時に自動生成・更新します。dedupe_key "
    "は source + repository + rule + file + message の sha256 hash で重複を抑止します。"
    "Phase 6 verification failure に限り、Message プロパティは error_output の先頭行のみ"
    "ですが、dedupe_key の hash 入力には error_output 全文を使います（test runner が"
    "共通バナーを先頭行に出すケースで別失敗を区別するため、Message が同じでも別レコードに"
    "なり得ます）。Source / Severity / Repository / Workflow / Dedupe Key / Operator / "
    "Rule ID / File Path / Message / Last Updated を HOKUSAI が書き込みます。Status は"
    "新規作成時のみ HOKUSAI が初期値 open を書き込み、その後の Status 編集"
    "（waived / resolved）は人手の運用判断として HOKUSAI からの上書きを行いません。"
    "Created At も新規作成時のみ書き込み、Notion 側で初回作成時刻を温存します。詳細は "
    "HOKUSAI 運用ガイド（docs/notion-dashboard-operation-guide.md の Review Issues DB "
    "セクション）を参照。"
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
    _PROP_LAST_UPDATED: {"date": {}},
    "Last Sync": {"date": {}},
    "Sync Errors": {"rich_text": {}},
    "Error Summary": {"rich_text": {}},
    # Issue #21 / v0.4.8: 複数エンジニア共有 profile 運用で「誰が hokusai start を
    # 叩いたか」を可視化する。workflow_started 時に env HOKUSAI_OPERATOR_NAME →
    # whoami → "(unknown)" の順で解決して書き込む。以降の event では上書きしない
    # （Notion 側の既存値を温存）。
    "Operator": {"rich_text": {}},
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
        _PROP_LAST_UPDATED: {"date": {}},
    }


# ----- Review Issues DB プロパティ定義 -----------------------------------
# Source enum: 前 4 つが MVP で発行する種別、後 3 つは後続機能（Policy Governance /
# LLM Gateway / Dependency Governance）用の先行確保枠。schema を後から拡張する
# migration コストを避けるため最初から含めて作成する。review_issues_db.py の
# SOURCE_* 定数と完全一致させること。
def _review_issues_db_properties(workflows_db_id: str) -> dict[str, dict[str, Any]]:
    return {
        "Title": {"title": {}},
        "Source": {
            "select": {
                "options": [
                    {"name": "final_review", "color": "orange"},
                    {"name": "verification_failure", "color": "red"},
                    {"name": "copilot_review", "color": "blue"},
                    {"name": "ci_failure", "color": "pink"},
                    {"name": "policy_violation", "color": "purple"},
                    {"name": "llm_gateway_block", "color": "yellow"},
                    {"name": "dependency_vuln", "color": "brown"},
                ]
            }
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "open", "color": "red"},
                    {"name": "resolved", "color": "green"},
                    {"name": "waived", "color": "gray"},
                    {"name": "duplicate", "color": "default"},
                ]
            }
        },
        "Severity": {
            "select": {
                "options": [
                    {"name": "critical", "color": "red"},
                    {"name": "high", "color": "orange"},
                    {"name": "medium", "color": "yellow"},
                    {"name": "low", "color": "blue"},
                    {"name": "info", "color": "default"},
                ]
            }
        },
        "Repository": {
            "select": {
                "options": [
                    {"name": "Backend", "color": "blue"},
                    {"name": "Frontend", "color": "green"},
                ]
            }
        },
        "Workflow": {
            "relation": {
                "database_id": workflows_db_id,
                "single_property": {},
            }
        },
        "Dedupe Key": {"rich_text": {}},
        "Operator": {"rich_text": {}},
        "Rule ID": {"rich_text": {}},
        "File Path": {"rich_text": {}},
        "Message": {"rich_text": {}},
        "Created At": {"date": {}},
        _PROP_LAST_UPDATED: {"date": {}},
    }


class NotionSetupError(Exception):
    """Notion セットアップ中の致命的エラー（呼び出し側へ伝搬する）"""


def setup_notion_workspace(
    api_token: str,
    parent_page_id: str,
    *,
    scaffold: bool = False,
    api_client: NotionAPIClient | None = None,
) -> dict[str, Any]:
    """Notion ワークスペースに HOKUSAI 用 DB / ページを一括作成する。

    Args:
        api_token: HOKUSAI 専用 Notion Integration の Internal Integration Token
        parent_page_id: 親ページの ID（事前に integration を接続しておくこと）
        scaffold: True のとき、DB 作成に加えて標準ドキュメントツリーも作成する。
            ツリーはハブ `Documentation`（icon 📚）配下に `議論`（icon 💬）/
            `運用ガイド`（icon 📖）/ `要件定義`（icon 📋）の 3 サブページ。
            配置先パスごとに既存検出（idempotent）、v0.4.3（絵文字 prefix 付き）
            / v0.4.4（HOKUSAI prefix + 英語名）の旧タイトルも legacy alias と
            して検出し重複作成を回避（canonical 優先）。
        api_client: テスト用に NotionAPIClient を差し替える場合に指定

    Returns:
        {
            "workflows_db_id": "...",
            "pull_requests_db_id": "...",
            "review_issues_db_id": "...",
            "scaffold": {                # scaffold=True のときのみ
                "created": [{"title": str, "id": str}, ...],
                "skipped": [{"title": str, "id": str}, ...],
                "failed":  [{"title": str, "error": str}, ...],
                # 致命的失敗（ハブ作成失敗など）の場合のみ:
                "error": "ExceptionType: message",
            },
        }

    Raises:
        NotionSetupError: いずれかの DB リソース作成に失敗した場合。
            scaffold 失敗は致命的扱いせず、結果 dict にエラーを含めて返す。
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

    # 3. Review Issues DB を作る（Workflow → Workflows DB の relation を含める）
    logger.info("Review Issues DB を作成中...")
    try:
        ri_db = api.create_database({
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [
                {"type": "text", "text": {"content": REVIEW_ISSUES_DB_TITLE}}
            ],
            "description": [
                {"type": "text", "text": {"content": _REVIEW_ISSUES_DB_DESCRIPTION}}
            ],
            "properties": _review_issues_db_properties(workflows_db_id),
        })
    except Exception as e:
        raise NotionSetupError(
            f"Review Issues DB の作成に失敗: {type(e).__name__}: {e}"
        ) from e

    review_issues_db_id = ri_db.get("id")
    if not review_issues_db_id:
        raise NotionSetupError(
            "Review Issues DB の作成レスポンスに id が含まれません"
        )

    result: dict[str, Any] = {
        "workflows_db_id": workflows_db_id,
        "pull_requests_db_id": pull_requests_db_id,
        "review_issues_db_id": review_issues_db_id,
    }

    # 4. scaffold（オプトイン）: 標準ドキュメントツリーを作成
    # DB 作成と異なり、scaffold 失敗は致命扱いしない（DB は既に作成済みのため）。
    # scaffold_notion_workspace は入力検証以外は raise せず、partial state を
    # 返り値に含めるため、ここでは error 用の fallback dict 構築は不要。
    if scaffold:
        try:
            result["scaffold"] = scaffold_notion_workspace(
                api_token, parent_page_id, api_client=api
            )
        except Exception as e:
            # 想定外の例外（入力検証以外）に対する最終 fallback。
            # 部分状態を保てないので空でも error を残す。
            logger.warning(
                "scaffold が想定外の例外で失敗: %s: %s",
                type(e).__name__, str(e),
            )
            result["scaffold"] = {
                "created": [],
                "skipped": [],
                "failed": [],
                "error": f"{type(e).__name__}: {e}",
            }

    return result


# ---------------------------------------------------------------------------
# Issue #25: 標準ドキュメントツリーの scaffold（オプトイン）
# ---------------------------------------------------------------------------

# 標準ツリー定義: top-level page ごとに icon と placeholder を持つ。
# 順序を保ちたいので list of tuple で定義。
# v0.4.4（Issue #27）: title 文字列は素のテキスト、絵文字は icon 側のみ。
# v0.4.5（Issue #29）: ハブから HOKUSAI prefix を削除（親ページ名で文脈確保）。
#                      サブページタイトルを日本語化（日本語運用フローに合わせる）。
_DOCUMENTATION_HUB_TITLE = "Documentation"
_DOCUMENTATION_HUB_ICON = "📚"
_DOCUMENTATION_HUB_PLACEHOLDER = (
    "HOKUSAI の Notion governance layer 上で人間が管理するドキュメントのハブ。"
    "HOKUSAI が自動同期する DB（Workflows / Pull Requests）とは別領域で、"
    "議論・運用・要件などをツリーで整理する。"
)
# 旧タイトル（idempotent 検出時に後方互換で skip 対象）。新→旧の順で 2 世代分:
# - v0.4.4: "HOKUSAI Documentation"
# - v0.4.3: "📚 HOKUSAI Documentation"
_DOCUMENTATION_HUB_LEGACY_TITLES: tuple[str, ...] = (
    "HOKUSAI Documentation",
    "📚 HOKUSAI Documentation",
)

# サブページの定義: (title, icon, placeholder, legacy_aliases)
# legacy_aliases は v0.4.4 / v0.4.3 で使われていたタイトル（英語 / 絵文字 prefix）。
# 後方互換のため既存検出対象に含める（canonical 優先）。
_DOCUMENTATION_CHILDREN: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "議論",
        "💬",
        "コード変更を伴う前段の議論・設計判断を残す場所。"
        "決定後は関連 GitHub Issue を本文に追加して双方向リンクを張る。"
        "「Decided」ステータスのドキュメントは Project Memory の候補にもなる。",
        ("Discussions", "💬 Discussions"),
    ),
    (
        "運用ガイド",
        "📖",
        "日常運用の手順書（profile 切り替え、token 更新、復旧手順、"
        "Operations Console の使い方など）。"
        "リポジトリ内 docs/*-operation-guide.md と整合させる。",
        ("Operation Guides", "📖 Operation Guides"),
    ),
    (
        "要件定義",
        "📋",
        "要件定義書の Notion 版または GitHub へのリンク集。"
        "コード変更を伴わない設計レベルの要件をここに集約する。"
        "リポジトリ内 docs/hokusai-*-requirements.md と対応する。",
        ("Requirements", "📋 Requirements"),
    ),
]


def _find_existing_child_page(
    api_client: NotionAPIClient,
    parent_page_id: str,
    title: str,
    *,
    legacy_aliases: tuple[str, ...] = (),
) -> str | None:
    """親ページの子ブロック一覧から、同名 / 旧タイトルの child_page の id を探す。

    見つからなければ None を返す。canonical な `title` と完全一致するページを
    最優先で返し、見つからなければ `legacy_aliases` の最初の一致を返す。
    `legacy_aliases` は過去バージョンで使われていたタイトル（絵文字 prefix 付き等）
    を渡すことで、後方互換で重複ページ作成を回避するが、新旧両方のページが
    親に存在する場合は canonical 側を優先する（さもないと legacy hub 配下に
    サブを作ってしまう）。

    Notion API は 1 レスポンス最大 100 件のため、`has_more` を見て全ページを
    走査する。途中で API エラーが発生した場合は idempotent チェックを完了
    できないため `NotionSetupError` を送出する（fail-open で重複ページを作って
    しまうのを避ける）。
    """
    legacy_set = set(legacy_aliases)
    legacy_match_id: str | None = None  # canonical が見つからなかった場合の fallback
    cursor: str | None = None
    while True:
        try:
            blocks = api_client.list_block_children(
                parent_page_id, start_cursor=cursor
            )
        except Exception as e:
            raise NotionSetupError(
                "親ページの子要素取得に失敗（idempotent チェック不能）: "
                f"{type(e).__name__}: {e}"
            ) from e
        for block in blocks.get("results", []):
            if block.get("type") != "child_page":
                continue
            block_title = block.get("child_page", {}).get("title")
            # canonical title 完全一致は最優先で即返し
            if block_title == title:
                return block.get("id")
            # legacy alias は最初の一致を覚えておくが、走査を続けて
            # canonical が見つかればそちらを優先する
            if legacy_match_id is None and block_title in legacy_set:
                legacy_match_id = block.get("id")
        if not blocks.get("has_more"):
            return legacy_match_id
        cursor = blocks.get("next_cursor")
        if not cursor:
            return legacy_match_id


def _build_documentation_page_payload(
    parent_id: str, title: str, icon_emoji: str, placeholder: str
) -> dict[str, Any]:
    """child_page の作成 payload を組み立てる。

    icon に絵文字を、children に placeholder paragraph を含める。

    Notion Create Page API の仕様（page_id parent）:
    - properties は "title" キーのみ許容され、値は rich-text array を **直接**
      渡す（DB 行用の {"title": {"title": [...]}} 形式は使えない）。
    - DB 行用形式を渡すと Notion 側で 400 エラーになる。
    """
    return {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": icon_emoji},
        "properties": {
            "title": [{"type": "text", "text": {"content": title}}]
        },
        "children": [
            {
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": placeholder}}
                    ]
                },
            }
        ],
    }


def _resolve_hub_page(
    api: NotionAPIClient,
    parent_page_id: str,
    created: list[dict[str, str]],
    skipped: list[dict[str, str]],
) -> str:
    """ハブページ（Documentation、icon 📚）を取得 or 作成し id を返す。

    既存なら `skipped` に append、新規作成なら `created` に append する。
    ハブ作成失敗は scaffold 全体の致命扱いとして NotionSetupError を投げる。
    旧バージョン（v0.4.3: 絵文字 prefix 付き / v0.4.4: HOKUSAI prefix 付き）
    で作成されたページも 2 世代分の legacy alias で後方互換 skip 検出する。
    """
    existing_hub_id = _find_existing_child_page(
        api, parent_page_id, _DOCUMENTATION_HUB_TITLE,
        legacy_aliases=_DOCUMENTATION_HUB_LEGACY_TITLES,
    )
    if existing_hub_id:
        skipped.append({"title": _DOCUMENTATION_HUB_TITLE, "id": existing_hub_id})
        logger.info(
            "ハブページは既に存在: %s (id=%s)",
            _DOCUMENTATION_HUB_TITLE, existing_hub_id,
        )
        return existing_hub_id
    try:
        hub_response = api.create_page(
            _build_documentation_page_payload(
                parent_page_id,
                _DOCUMENTATION_HUB_TITLE,
                _DOCUMENTATION_HUB_ICON,
                _DOCUMENTATION_HUB_PLACEHOLDER,
            )
        )
    except Exception as e:
        raise NotionSetupError(
            f"ハブページの作成に失敗: {type(e).__name__}: {e}"
        ) from e
    hub_id = hub_response.get("id", "")
    if not hub_id:
        raise NotionSetupError("ハブページ作成レスポンスに id が含まれません")
    created.append({"title": _DOCUMENTATION_HUB_TITLE, "id": hub_id})
    logger.info("ハブページを作成: %s (id=%s)", _DOCUMENTATION_HUB_TITLE, hub_id)
    return hub_id


def _create_or_skip_subpage(
    api: NotionAPIClient,
    hub_id: str,
    sub_title: str,
    sub_icon: str,
    sub_placeholder: str,
    created: list[dict[str, str]],
    skipped: list[dict[str, str]],
    failed: list[dict[str, str]],
    *,
    legacy_aliases: tuple[str, ...] = (),
) -> None:
    """単一サブページを idempotent に作成し、結果を created/skipped/failed に振り分ける。

    legacy_aliases は v0.4.3 以前で作成された絵文字 prefix 付きタイトルを
    後方互換で既存検出するために渡す。
    """
    existing_sub_id = _find_existing_child_page(
        api, hub_id, sub_title, legacy_aliases=legacy_aliases,
    )
    if existing_sub_id:
        skipped.append({"title": sub_title, "id": existing_sub_id})
        logger.info("サブページは既に存在: %s (id=%s)", sub_title, existing_sub_id)
        return
    try:
        sub_response = api.create_page(
            _build_documentation_page_payload(
                hub_id, sub_title, sub_icon, sub_placeholder
            )
        )
    except Exception as e:
        err_detail = f"{type(e).__name__}: {e}"
        logger.warning("サブページの作成に失敗（skip）: %s: %s", sub_title, err_detail)
        failed.append({"title": sub_title, "error": err_detail})
        return
    sub_id = sub_response.get("id", "")
    if not sub_id:
        err_detail = "create_page レスポンスに id が含まれません"
        logger.warning("%s: %s", err_detail, sub_title)
        failed.append({"title": sub_title, "error": err_detail})
        return
    created.append({"title": sub_title, "id": sub_id})
    logger.info("サブページを作成: %s (id=%s)", sub_title, sub_id)


def scaffold_notion_workspace(
    api_token: str,
    parent_page_id: str,
    *,
    api_client: NotionAPIClient | None = None,
) -> dict[str, Any]:
    """親ページ配下に標準ドキュメントツリーを作成する（idempotent）。

    ツリー構造（v0.4.5〜: title はハブ英語 / サブ日本語、絵文字は icon）:
        <parent>
        └── Documentation              ← icon 📚
            ├── 議論                    ← icon 💬
            ├── 運用ガイド              ← icon 📖
            └── 要件定義                ← icon 📋

    既存に同名ページがある場合は skip（破壊しない）。v0.4.3（絵文字 prefix 付き）
    と v0.4.4（HOKUSAI prefix + 英語名）の旧タイトルも 2 世代分の legacy alias
    として検出される。新旧両方のページが共存する場合は canonical 側を優先する。

    本関数は入力検証エラー（NotionSetupError）以外は raise しない。実行時の
    API エラー（ハブ作成失敗 / 子要素取得失敗 / サブページ作成失敗）はすべて
    返り値 dict に partial state として記録する。途中で失敗しても、すでに
    作成済み / skip 済みのページ情報は失われない（呼び出し側が復旧手順を
    判断できるようにするため）。

    Args:
        api_token: Notion Integration Token
        parent_page_id: 親ページの ID
        api_client: テスト差し替え用

    Returns:
        {
            "created": [{"title": str, "id": str}, ...],
            "skipped": [{"title": str, "id": str}, ...],
            "failed":  [{"title": str, "error": str}, ...],  # 個別サブページの失敗
            # ハブ作成失敗 / 子要素取得失敗（idempotent チェック不能）等の
            # 致命的失敗時のみ追加される:
            "error": "ExceptionType: message",
        }

    Raises:
        NotionSetupError: 入力（api_token / parent_page_id）が空のときのみ。
    """
    if not api_token:
        raise NotionSetupError("api_token が空です")
    if not parent_page_id:
        raise NotionSetupError("parent_page_id が空です")

    api = api_client or NotionAPIClient(api_token=api_token)

    created: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    try:
        hub_id = _resolve_hub_page(api, parent_page_id, created, skipped)
    except Exception as e:
        err_detail = f"{type(e).__name__}: {e}"
        logger.warning("ハブページの解決に失敗: %s", err_detail)
        return {
            "created": created, "skipped": skipped, "failed": failed,
            "error": err_detail,
        }

    for sub_title, sub_icon, sub_placeholder, sub_legacy in _DOCUMENTATION_CHILDREN:
        try:
            _create_or_skip_subpage(
                api, hub_id, sub_title, sub_icon, sub_placeholder,
                created, skipped, failed,
                legacy_aliases=sub_legacy,
            )
        except Exception as e:
            # サブ毎の lookup / create エラーで全体を止めない。
            err_detail = f"{type(e).__name__}: {e}"
            logger.warning(
                "サブページの処理に失敗（continue）: %s: %s", sub_title, err_detail,
            )
            failed.append({"title": sub_title, "error": err_detail})

    return {"created": created, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# シェル rc ファイルへの環境変数書き込み（--persist 用）
# ---------------------------------------------------------------------------

# マーカー: ブロックを冪等に書き換えるために前後を囲む
# 既存ユーザー（v0.4.0 以前）向けの "profile 名なし" マーカー。
# `profile_name=None` で `persist_env_vars` を呼ぶと従来通りこのマーカーが使われる。
PERSIST_BEGIN_MARKER = (
    "# === HOKUSAI Notion Dashboard (managed by `hokusai notion-setup`) ==="
)
PERSIST_END_MARKER = "# === END HOKUSAI Notion Dashboard ==="


# v0.4.1 以降: profile 別マーカー
# 同じ rc ファイルに複数 profile の env ブロックを並列で持てるようにする。
# `profile_name=None` の場合は上記の従来マーカーを使い、後方互換を維持する。
def _build_profile_markers(profile_name: str) -> tuple[str, str]:
    """profile 名つきマーカーを生成する。

    Args:
        profile_name: profile 名（rc ファイル内で識別子として使う）

    Returns:
        (begin_marker, end_marker)
    """
    return (
        f"# === HOKUSAI Notion Dashboard "
        f"(managed by `hokusai notion-setup`, profile={profile_name}) ===",
        f"# === END HOKUSAI Notion Dashboard (profile={profile_name}) ===",
    )


# シェル変数名として妥当な形式（POSIX shell の identifier）
# 想定外の文字（空白、改行、`;` 等）を含む値を rc に書くと注入リスクがあるため、
# `persist_env_vars` / `_handle_notion_setup` の入口でチェックする。
_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# profile 名として許容する形式。hokusai/config/profiles.py の _PROFILE_NAME_PATTERN
# と一致させる。マーカー行（コメント形式）に直接埋め込むため、改行 / 制御文字 /
# 空白などが入ると rc の構造が壊れるためここでも独立に検証する。
_PROFILE_NAME_FOR_MARKER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def is_valid_env_var_name(name: Any) -> bool:
    """env 変数名がシェル identifier として妥当な形式かを返す（読みやすい述語）。

    `[A-Za-z_][A-Za-z0-9_]*` に一致する非空文字列のみ True。
    """
    return isinstance(name, str) and bool(_ENV_VAR_NAME_PATTERN.match(name))


def _validate_env_var_name(name: str, *, role: str) -> None:
    """env 変数名がシェル identifier として妥当か検証する。

    Args:
        name: 検証対象の env 変数名
        role: エラーメッセージに含める用途名（"workflows_db_id_env" 等）

    Raises:
        ValueError: name が空、または `[A-Za-z_][A-Za-z0-9_]*` の形式に合わない
    """
    if not is_valid_env_var_name(name):
        raise ValueError(
            f"invalid env variable name for {role}: {name!r} "
            f"(must match [A-Za-z_][A-Za-z0-9_]*)"
        )


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
    workflows_env_name: str = "HOKUSAI_NOTION_WORKFLOWS_DB_ID",
    pull_requests_env_name: str = "HOKUSAI_NOTION_PR_DB_ID",
    review_issues_env_name: str = "HOKUSAI_NOTION_REVIEW_ISSUES_DB_ID",
    profile_name: str | None = None,
    backup: bool = True,
) -> dict[str, Any]:
    """HOKUSAI Notion ダッシュボード用の env vars を rc ファイルに書き込む。

    冪等性: マーカーで囲んだブロックを既存の rc ファイル内から検出し、
    あれば置き換え、なければ末尾に追記する。

    Args:
        rc_path: 書き込み先（~/.zshrc 等）
        ids: setup_notion_workspace の戻り値（workflows_db_id 等）
        workflows_env_name: workflows DB ID を保持する env 変数名
        pull_requests_env_name: PR DB ID を保持する env 変数名
        review_issues_env_name: Review Issues DB ID を保持する env 変数名。
            ids に review_issues_db_id が含まれない旧呼び出しでも安全に動くよう、
            その場合はこの行を省略する（後方互換）。
        profile_name: profile 名（指定時は profile 別マーカーを使う）。
            `None` の場合は v0.4.0 以前の従来マーカーを使い、後方互換を維持する。
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

    # シェル変数名の最終ガード（コマンド注入 / rc 破損の防止）
    _validate_env_var_name(workflows_env_name, role="workflows_env_name")
    _validate_env_var_name(pull_requests_env_name, role="pull_requests_env_name")
    _validate_env_var_name(review_issues_env_name, role="review_issues_env_name")

    # profile 名指定時は profile 別マーカー、未指定時は従来マーカー
    if profile_name is not None:
        # profile 名はコメント行に直接埋め込むため、改行・空白・制御文字等が
        # 入ると rc の構造が壊れる。hokusai/config/profiles.py の規則と一致させる。
        if not isinstance(profile_name, str) or not _PROFILE_NAME_FOR_MARKER_PATTERN.match(
            profile_name
        ):
            raise ValueError(
                f"invalid profile_name for rc marker: {profile_name!r} "
                f"(must match [a-z][a-z0-9_-]*)"
            )
        begin_marker, end_marker = _build_profile_markers(profile_name)
    else:
        begin_marker, end_marker = PERSIST_BEGIN_MARKER, PERSIST_END_MARKER

    block_lines = [
        begin_marker,
        f"# Last updated: {datetime.now().isoformat()}",
        f'export {workflows_env_name}="{ids["workflows_db_id"]}"',
        f'export {pull_requests_env_name}="{ids["pull_requests_db_id"]}"',
    ]
    # Review Issues DB ID は v0.5.x で追加。古い呼び出し（ids に未含）でも
    # KeyError を出さず、その行だけスキップする（後方互換）。
    review_issues_db_id = ids.get("review_issues_db_id")
    if review_issues_db_id:
        block_lines.append(
            f'export {review_issues_env_name}="{review_issues_db_id}"'
        )
    block_lines.append(end_marker)
    new_block = "\n".join(block_lines) + "\n"

    existing = rc_path.read_text() if rc_path.exists() else ""

    backup_path: Path | None = None
    if backup and rc_path.exists():
        backup_path = rc_path.with_suffix(rc_path.suffix + ".hokusai.bak")
        backup_path.write_text(existing)

    if begin_marker in existing and end_marker in existing:
        # 既存ブロックを置き換え（profile が同じなら同じマーカーで上書き）
        start_idx = existing.index(begin_marker)
        end_idx = existing.index(end_marker) + len(end_marker)
        # 末尾の改行も含めて差し替え（次のコンテンツとの空行管理）
        if end_idx < len(existing) and existing[end_idx] == "\n":
            end_idx += 1
        new_content = existing[:start_idx] + new_block + existing[end_idx:]
        action = "replaced"
    else:
        # 末尾に追記（別 profile の既存ブロックがあっても並列で保存）
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
