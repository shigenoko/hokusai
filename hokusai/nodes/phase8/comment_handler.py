"""
Phase 8: コメント返信・解決処理

レビューコメントへの返信と、スレッドの解決を行う。
"""

from ...integrations.factory import get_git_hosting_client
from ...state import (
    WorkflowState,
    get_current_pr,
)

# issue comment の fix_summary に含まれる場合、返信を投稿せずスキップするキーワード
_SKIP_KEYWORDS = [
    "対応不要",
    "スキップ",
    "対応なし",
    "変更不要",
    "情報のみ",
    "ポジティブフィードバック",
]


def _is_no_action_needed(comment: dict) -> bool:
    """fix_summary の内容から対応不要と判断できるか"""
    fix_summary = comment.get("fix_summary", "") or ""
    summary_lower = fix_summary.lower()
    return any(kw in summary_lower for kw in _SKIP_KEYWORDS)


def _generate_reply_message(comment: dict) -> str:
    """コメント内容に応じた返信メッセージを生成（review comment 用）"""
    # fix_summaryが設定されている場合はそれを使用
    fix_summary = comment.get("fix_summary")
    if fix_summary:
        return f"修正しました。\n\n{fix_summary}"

    # コメント本文のキーワードに基づいて返信を生成
    body = comment.get("body", "").lower()

    # 簡単な修正パターン
    simple_fixes = [
        ("redundant", "冗長なフィールドを削除しました。"),
        ("unused", "未使用の変数/引数を削除しました。"),
        ("missing", "不足していた項目を追加しました。"),
        ("typo", "タイポを修正しました。"),
        ("sort", "ソート機能を追加しました。"),
        ("accessibility", "アクセシビリティ属性（aria-sort）を追加しました。"),
        ("key", "一意のキーを使用するよう修正しました。"),
        ("error", "エラーハンドリングを改善しました。"),
        ("catch", "catchブロックでエラーをキャプチャするよう修正しました。"),
        ("timezone", "タイムゾーン処理を改善しました。"),
        (
            "pagination",
            "コメントで注意事項を追加しました。将来的にページネーション実装を検討します。",
        ),
        (
            "auth.currentuser",
            "useAccessContext()から取得したfirebaseUserを使用するよう修正しました。",
        ),
        ("inconsisten", "一貫性を保つよう修正しました。"),
    ]

    for keyword, reply in simple_fixes:
        if keyword in body:
            return f"修正しました。{reply}"

    # デフォルトの返信
    return "修正しました。ご確認ください。"


def _generate_issue_comment_reply(comment: dict) -> str:
    """issue comment（PR全体へのコメント）用の返信メッセージを生成

    review comment と異なりスレッド構造がないため、
    どのコメントへの返信かを引用で明示する。
    """
    fix_summary = comment.get("fix_summary")
    author = comment.get("author", "")
    body = comment.get("body", "")

    # 元コメントの先頭部分を引用（長い場合は切り詰め）
    quote_lines = body.strip().split("\n")
    quote_preview = "\n".join(f"> {line}" for line in quote_lines[:3])
    if len(quote_lines) > 3:
        quote_preview += "\n> ..."

    # ヘッダー: どのコメントへの対応かを明示
    header = f"@{author} さんの以下のコメントについて対応しました。\n\n{quote_preview}\n\n---\n\n"

    if fix_summary:
        return f"{header}{fix_summary}"

    return f"{header}確認し、対応いたしました。ご確認ください。"


def _reply_to_all_comments(
    state: WorkflowState,
    comments: list,
    pr_number: int = None,
    git_hosting=None,
    default_message: str = None,
    resolve_after_reply: bool = True,
) -> list:
    """全コメントに返信し、返信済みフラグを更新。オプションでスレッドも解決"""
    if not comments:
        return comments

    try:
        if git_hosting is None:
            git_hosting = get_git_hosting_client()
        if pr_number is None:
            current_pr = get_current_pr(state)
            pr_number = current_pr.get("number") if current_pr else None

        updated_comments = []
        for comment in comments:
            if comment.get("replied"):
                updated_comments.append(comment)
                continue

            comment_id = comment.get("id")
            comment_type = comment.get("comment_type", "review")
            # コメント種別に応じた返信を生成
            if default_message:
                reply_message = default_message
            elif comment_type == "issue":
                reply_message = _generate_issue_comment_reply(comment)
            else:
                reply_message = _generate_reply_message(comment)

            if comment_type == "issue":
                # 対応不要と判定されたコメントは返信せずスキップ
                if _is_no_action_needed(comment):
                    comment["replied"] = True
                    print(f"   - issue comment {comment_id} は対応不要のためスキップ")
                else:
                    # issue comment: 新規 issue comment として返信（返信マーカー埋め込み）
                    reply_body = f"<!-- hokusai-reply-to: {comment_id} -->\n{reply_message}"
                    if comment_id and git_hosting.reply_to_issue_comment(
                        pr_number, reply_body
                    ):
                        comment["replied"] = True
                        print(f"   ✓ issue comment {comment_id} に返信しました")
                    # issue comment にはスレッド解決がないためスキップ
            else:
                # review comment: 既存のスレッド返信
                if comment_id and git_hosting.reply_to_comment(
                    pr_number, comment_id, reply_message
                ):
                    comment["replied"] = True
                    print(f"   ✓ コメント {comment_id} に返信しました")

                    # スレッドを解決
                    if resolve_after_reply:
                        thread_id = comment.get("thread_id")
                        if not thread_id:
                            # スレッドIDがない場合は取得
                            thread_id = git_hosting.get_thread_id_for_comment(
                                pr_number, comment_id
                            )
                            comment["thread_id"] = thread_id

                        if thread_id and git_hosting.resolve_thread(thread_id):
                            comment["resolved"] = True
                            print("   ✓ スレッド解決しました")

            updated_comments.append(comment)

        return updated_comments

    except Exception as e:
        print(f"⚠️ コメント返信処理エラー: {e}")
        return comments
