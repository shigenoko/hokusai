#!/usr/bin/env python3
"""
HOKUS AI Workflow Dashboard

ワークフローの進捗状況をブラウザで表示するシンプルなダッシュボード。
使い方: python scripts/dashboard.py
"""

import html as html_mod
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import yaml

# Ensure the project root is importable when running as a standalone script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hokusai.constants import PHASE_SHORT_NAMES
from hokusai.persistence.sqlite_store import SQLiteStore
from hokusai.utils.phase_page_templates import (
    PHASE_PAGE_DECISION_DEFAULT,
    get_phase_page_context,
    initialize_phase_page_state,
)

DB_PATH = Path.home() / ".hokusai" / "workflow.db"
CHECKPOINT_DB_PATH = Path.home() / ".hokusai" / "checkpoint.db"
_store: SQLiteStore | None = None


def _get_store() -> SQLiteStore:
    """SQLiteStoreのシングルトンインスタンスを取得"""
    global _store
    if _store is None:
        _store = SQLiteStore(DB_PATH)
    return _store


CHECKLIST_PATH = Path(__file__).parent.parent / "hokusai" / "review_checklist.md"
CONFIGS_DIR = Path(__file__).parent.parent / "configs"
PORT = 8765
HOKUSAI_COMMAND_TIMEOUT = 3600

_BG_LOG_DIR = Path(tempfile.gettempdir()) / "hokusai-dashboard-logs"
_BG_META_DIR = _BG_LOG_DIR / "meta"
_bg_processes: dict[str, subprocess.Popen] = {}
_bg_lock = threading.Lock()
_BG_MAX_AGE_SECONDS = 12 * 3600  # 12時間以上前のメタは stale と見なす


def _save_running_meta(identifier: str, pid: int, log_file: str, cmdline: list[str] | None = None) -> None:
    """実行中メタデータを永続化する。"""
    _BG_META_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = identifier.replace("/", "_").replace(":", "_")[:80]
    meta_path = _BG_META_DIR / f"{safe_name}.json"
    try:
        meta_path.write_text(json.dumps({
            "identifier": identifier,
            "pid": pid,
            "log_file": log_file,
            "started_at": datetime.now().isoformat(),
            "cmdline": cmdline or [],
        }))
    except OSError:
        pass


def _remove_running_meta(identifier: str) -> None:
    """実行中メタデータを削除する。"""
    safe_name = identifier.replace("/", "_").replace(":", "_")[:80]
    meta_path = _BG_META_DIR / f"{safe_name}.json"
    meta_path.unlink(missing_ok=True)


def _pid_is_alive(pid: int) -> bool:
    """指定PIDのプロセスが生存しているか確認する。"""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _verify_pid_is_hokusai(pid: int) -> bool:
    """pid が hokusai プロセスか cmdline で照合する（pid再利用対策）。

    ps コマンドが利用できない環境や照合失敗時は安全側（True）を返す。
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False  # プロセスが存在しない
        return "hokusai" in result.stdout
    except Exception:
        return True  # 確認失敗時は安全側（実行中と見なす）


def _get_running_identifiers() -> set[str]:
    """実行中のバックグラウンドプロセスの識別子セットを返す。

    メモリ上の _bg_processes と永続メタファイルを統合し、
    pid 生存確認 + cmdline 照合 + started_at 最大経過時間チェックを行う。
    非生存 pid や stale なメタはクリーンアップする。
    """
    running = set()

    # メモリ上のプロセス
    with _bg_lock:
        for k, proc in list(_bg_processes.items()):
            if proc.poll() is None:
                running.add(k)

    # 永続メタファイルから復元
    if _BG_META_DIR.is_dir():
        for meta_file in _BG_META_DIR.glob("*.json"):
            try:
                meta = json.loads(meta_file.read_text())
                pid = meta.get("pid", 0)
                identifier = meta.get("identifier", "")
                if not (pid and identifier):
                    meta_file.unlink(missing_ok=True)
                    continue

                # started_at による最大経過時間ガード（pid再利用対策）
                started_at = meta.get("started_at", "")
                if started_at:
                    try:
                        age = (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
                        if age > _BG_MAX_AGE_SECONDS:
                            meta_file.unlink(missing_ok=True)
                            continue
                    except ValueError:
                        pass

                if _pid_is_alive(pid) and _verify_pid_is_hokusai(pid):
                    running.add(identifier)
                else:
                    meta_file.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError):
                meta_file.unlink(missing_ok=True)

    return running


def is_workflow_running(workflow_id: str) -> bool:
    """指定ワークフローのバックグラウンドプロセスが実行中か判定する。

    workflow_id ベースの identifier (start:{wf_id}, continue:{wf_id}) のみチェック。
    """
    running = _get_running_identifiers()
    return f"start:{workflow_id}" in running or f"continue:{workflow_id}" in running


def _get_substep_progress(workflow_id: str) -> str | None:
    """実行中ワークフローのログ末尾からサブステップ進捗行を取得"""
    for ident_prefix in ("continue_", "start_"):
        safe_id = f"{ident_prefix}{workflow_id}"[:80]
        meta_path = _BG_META_DIR / f"{safe_id}.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            log_file = meta.get("log_file")
            if log_file and Path(log_file).exists():
                return _tail_substep(log_file)
    return None


def _tail_substep(log_path: str) -> str | None:
    """ログファイル末尾から最新の📋行を抽出（末尾4KBのみ読み取り）"""
    p = Path(log_path)
    try:
        size = p.stat().st_size
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            if size > 4096:
                f.seek(size - 4096)
            tail = f.read()
    except OSError:
        return None
    matches = re.findall(r"📋 Phase \d+ \[\d+/\d+\].+", tail)
    return matches[-1] if matches else None


def get_workflows():
    """ワークフロー一覧を取得

    SQLiteStoreのマイグレーション処理を適用してpull_requestsを正しく読み取る。
    """
    store = _get_store()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("""
            SELECT workflow_id, task_title, current_phase, updated_at, task_url, state_json
            FROM workflows
            ORDER BY updated_at DESC
        """)
        workflows = []
        for row in cursor.fetchall():
            # state_jsonからリポジトリ情報と待機状態を抽出
            repos = set()
            waiting_status = None
            state = {}
            try:
                state = json.loads(row[5])
                # SQLiteStoreと同じマイグレーション処理を適用
                state = store._migrate_legacy_pr_fields(state)
                for pr in state.get("pull_requests", []):
                    owner = pr.get("owner", "")
                    repo = pr.get("repo", "")
                    if owner and repo:
                        repos.add(f"{owner}/{repo}")
            except (json.JSONDecodeError, TypeError):
                pass

            # Human-in-the-loop待機状態を判定
            waiting_status = get_waiting_status(state)

            workflows.append({
                "workflow_id": row[0],
                "task_title": row[1],
                "current_phase": row[2],
                "updated_at": row[3],
                "task_url": row[4],
                "repos": list(repos),
                "waiting_status": waiting_status,
                "run_mode": state.get("run_mode", "step"),
                "current_phase_status": (
                    (state.get("phases", {}).get(str(row[2])) or state.get("phases", {}).get(row[2]) or {}).get("status", "pending")
                ),
            })
        return workflows


def get_workflow_detail(workflow_id: str):
    """ワークフロー詳細を取得

    SQLiteStoreを使用してマイグレーション処理を適用する。
    これにより、旧フィールド(backend_pr_url等)がpull_requestsリストに変換される。
    """
    store = _get_store()
    return store.load_workflow(workflow_id)


def retry_phase(workflow_id: str, from_phase: int) -> dict:
    """指定フェーズ以降をリセットしてリトライ可能にする。

    ワークフローDB のフェーズステータスと LangGraph チェックポイントの
    両方をクリアし、一貫した状態でリトライできるようにする。

    Args:
        workflow_id: ワークフローID
        from_phase: リセット開始フェーズ（このフェーズ以降を pending に戻す）

    Returns:
        {"success": True/False, ...}
    """
    store = _get_store()
    state = store.load_workflow(workflow_id)
    if state is None:
        return {"success": False, "errors": [f"ワークフローが見つかりません: {workflow_id}"]}

    if from_phase < 1 or from_phase > 10:
        return {"success": False, "errors": [f"無効なフェーズ番号: {from_phase}"]}

    # 1. ワークフローDB: from_phase 以降のフェーズを pending にリセット
    phases = state.get("phases", {})
    reset_phases = []
    for i in range(from_phase, 11):
        # SQLiteStore は int キーで返す場合がある
        key = i if i in phases else str(i)
        if key in phases:
            phases[key]["status"] = "pending"
            phases[key]["started_at"] = None
            phases[key]["completed_at"] = None
            phases[key]["error_message"] = None
            phases[key]["retry_count"] = 0
            reset_phases.append(i)

    state["phases"] = phases
    state["current_phase"] = from_phase

    # フェーズ出力もクリア（from_phase に応じて）
    if from_phase <= 2:
        state["research_result"] = None
        state["schema_change_required"] = False
    if from_phase <= 3:
        state["design_result"] = None
    if from_phase <= 4:
        state["work_plan"] = None
        state["expected_changed_files"] = []
    if from_phase <= 5:
        state["implementation_result"] = None

    # クロスレビュー結果をクリア（対象フェーズ以降）
    cross_review = state.get("cross_review_results", {})
    if cross_review:
        for i in range(from_phase, 11):
            cross_review.pop(i, None)
            cross_review.pop(str(i), None)
        state["cross_review_results"] = cross_review

    # Phase 6 検証結果をクリア
    if from_phase <= 6:
        state["verification"] = {}
        state["verification_errors"] = []
        # repositories の phase_status から Phase 6 以降をクリア
        for repo in state.get("repositories", []):
            ps = repo.get("phase_status", {})
            for i in range(from_phase, 11):
                ps.pop(i, None)
                ps.pop(str(i), None)

    # waiting 系フラグをクリア
    state["waiting_for_human"] = False
    state["human_input_request"] = None

    store.save_workflow(workflow_id, state)

    # 2. チェックポイントDB: 該当ワークフローのチェックポイントを削除
    try:
        with sqlite3.connect(str(CHECKPOINT_DB_PATH)) as cp_conn:
            cp_conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (workflow_id,))
            cp_conn.execute("DELETE FROM writes WHERE thread_id = ?", (workflow_id,))
    except Exception:
        pass  # チェックポイントDB が存在しない場合も許容

    return {
        "success": True,
        "message": f"Phase {from_phase} 以降をリセットしました（{reset_phases}）",
        "reset_phases": reset_phases,
    }


def retry_notion_actions(workflow_id: str) -> dict:
    """スキップされた Notion アクションをリトライする。

    PR callout など、Notion 未接続でスキップされたアクションを再実行する。
    Claude Code 経由で Notion MCP を使うため、同期的に実行する。

    Args:
        workflow_id: ワークフローID

    Returns:
        {"success": True/False, ...}
    """
    from hokusai.state import add_audit_log
    from hokusai.utils.notion_helpers import record_pr_callout_to_notion

    store = _get_store()
    state = store.load_workflow(workflow_id)
    if state is None:
        return {"success": False, "errors": [f"ワークフローが見つかりません: {workflow_id}"]}

    if is_workflow_running(workflow_id):
        return {"success": False, "errors": ["ワークフローが実行中です"]}

    retried = []
    errors = []

    # PR callout のリトライ
    pull_requests = state.get("pull_requests", [])
    if pull_requests:
        # recorded_count をリセットして再送信を強制
        state["notion_recorded_pr_count"] = 0
        try:
            state = record_pr_callout_to_notion(state, phase=8)
            retried.append("notion_prepend_pr_callout")
        except Exception as e:
            errors.append(f"PR callout 失敗: {e}")

    if not retried and not errors:
        return {"success": True, "message": "リトライ対象の Notion アクションはありません"}

    # 成功した場合、notion_connected を更新
    if retried and not errors:
        state["notion_connected"] = True
        state = add_audit_log(state, 0, "notion_retry", "success", {
            "retried_actions": retried,
        })

    store.save_workflow(workflow_id, state)

    if errors:
        return {"success": False, "errors": errors, "retried": retried}

    return {
        "success": True,
        "message": f"Notion アクションをリトライしました: {', '.join(retried)}",
        "retried": retried,
    }


def format_datetime(iso_str: str | None) -> str:
    """日時をフォーマット"""
    if not iso_str:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y/%m/%d %H:%M")
    except Exception:
        return iso_str


def _find_cross_review_blocked_phase(state: dict) -> int | None:
    """現在 cross_review_blocked で停止中のフェーズを特定する。

    フェーズの現在ステータスが failed かつ error_message が
    "cross_review_blocked" であるフェーズのみを候補とする。
    completed に復元済みのフェーズは、audit_log や cross_review_results に
    履歴が残っていても除外する。
    """
    phases = state.get("phases", {})
    for entry in reversed(state.get("audit_log", [])):
        action = entry.get("action")
        phase = entry.get("phase")
        if action == "cross_review_blocked" and phase in (2, 3, 4):
            # フェーズが現在 failed かつ cross_review_blocked であることを確認
            phase_data = phases.get(phase) or phases.get(str(phase)) or {}
            if (phase_data.get("status") != "failed"
                    or phase_data.get("error_message") != "cross_review_blocked"):
                continue
            # critical findings が残っているか確認
            cr = state.get("cross_review_results", {})
            result = cr.get(phase) or cr.get(str(phase))
            if result:
                findings = result.get("findings", [])
                if any(f.get("severity") == "critical" for f in findings):
                    return phase
    return None


def get_waiting_status(state: dict) -> str | None:
    """Human-in-the-loop待機状態を判定

    Returns:
        待機状態の文字列（None=待機中ではない）
        - "copilot_review": Copilotレビュー待ち
        - "copilot_fix": Copilot指摘修正待ち
        - "human_review": 人間レビュー待ち
        - "human_fix": 人間レビュー指摘修正待ち
        - "implementation": 実装待ち（Phase 5）
        - "branch_push": ブランチプッシュ待ち
    """
    if not state.get("waiting_for_human", False):
        return None

    # プッシュ未検出（修正フローで新コミットなし）
    if state.get("push_verification_failed"):
        return "push_not_detected"

    # cross-review blocked 判定（他の条件より先にチェック）
    if _find_cross_review_blocked_phase(state) is not None:
        return "cross_review_blocked"

    # Phase 8のサブ状態
    if state.get("waiting_for_copilot_review"):
        return "copilot_review"
    if state.get("copilot_fix_requested"):
        return "copilot_fix"
    if state.get("waiting_for_human_review"):
        return "human_review"
    if state.get("human_fix_requested"):
        return "human_fix"

    # その他の待機状態
    human_input = state.get("human_input_request", "")
    if human_input == "copilot_review":
        return "copilot_review"
    if human_input == "human_review":
        return "human_review"
    if human_input == "implementation":
        return "implementation"
    if human_input == "branch_push":
        return "branch_push"
    if human_input == "branch_hygiene":
        return "branch_hygiene"
    if human_input == "review_status":
        return "review_status"

    return "waiting"  # 汎用的な待機状態


def waiting_status_label(status: str | None) -> str:
    """待機状態のラベルを返す"""
    labels = {
        "push_not_detected": "プッシュ未検出",
        "copilot_review": "Copilotレビュー待ち",
        "copilot_fix": "Copilot修正待ち",
        "human_review": "人間レビュー待ち",
        "human_fix": "人間修正待ち",
        "implementation": "実装待ち",
        "branch_push": "プッシュ待ち",
        "branch_hygiene": "ブランチ整理待ち",
        "cross_review_blocked": "クロスレビュー修正待ち",
        "review_status": "レビュー対応中",
        "waiting": "操作待ち",
    }
    return labels.get(status, "") if status else ""


def phase_name(phase: int) -> str:
    """フェーズ番号を名前に変換"""
    return PHASE_SHORT_NAMES.get(phase, f"Phase {phase}")


def status_badge(status: str, waiting_status: str | None = None) -> str:
    """ステータスをバッジHTMLに変換

    Args:
        status: フェーズステータス (completed, in_progress, pending, etc.)
        waiting_status: Human-in-the-loop待機状態（オプション）
    """
    styles = {
        "completed": "background:#dcfce7;color:#166534;",
        "in_progress": "background:#dbeafe;color:#1e40af;",
        "pending": "background:#f3f4f6;color:#4b5563;",
        "skipped": "background:#fef3c7;color:#92400e;",
        "failed": "background:#fee2e2;color:#991b1b;",
        "waiting": "background:#fef3c7;color:#92400e;",
    }

    # 待機中の場合は特別な表示
    if status == "in_progress" and waiting_status:
        label = waiting_status_label(waiting_status)
        return f'<span class="badge waiting-badge">{label}</span>'

    style = styles.get(status, "background:#f3f4f6;color:#4b5563;")
    return f'<span class="badge" style="{style}">{status}</span>'


def _phase9_button_label(state: dict) -> str:
    """Phase 9 のボタンラベルを状態に応じて返す"""
    if state.get("copilot_fix_requested") or state.get("human_fix_requested"):
        return "修正を実行"
    if state.get("waiting_for_copilot_review"):
        return "Copilotレビュー確認"
    if state.get("waiting_for_human_review"):
        return "人間レビュー確認"

    human_input = state.get("human_input_request", "")
    if human_input == "human_review":
        # 全PRレビュー完了済みか確認
        prs = state.get("pull_requests", [])
        pending = [
            pr for pr in prs
            if pr.get("status") not in ("approved", "merged")
        ]
        if not pending:
            return "レビュー完了処理"
        current_pr = None
        idx = state.get("current_pr_index", 0)
        if 0 <= idx < len(prs):
            current_pr = prs[idx]
        if current_pr and current_pr.get("status") in ("approved", "merged"):
            return "次のPRへ移動"
        return "レビュー確認"
    return "このフェーズを実行"


def _classify_author(author: str) -> str:
    """レビューコメントの author をカテゴリに分類する。"""
    a = author.lower()
    if "copilot" in a:
        return "copilot"
    if "devin" in a:
        return "devin"
    if a:
        return "human"
    return "unknown"


def render_pr_progress(state: dict, workflow_id: str | None = None, bg_running: bool = False) -> str:
    """Phase 9 (レビュー対応) のPR進捗をサマリー + モーダルで返す

    Phase 9 行にはサマリー（1行概要 + 詳細ボタン）を表示し、
    詳細テーブルはモーダル dialog 内に配置する。

    Args:
        state: ワークフロー状態
        workflow_id: ワークフローID
        bg_running: バックグラウンドプロセスが実行中か
    """
    pull_requests = state.get("pull_requests", [])
    if not pull_requests:
        return ""

    current_phase = state.get("current_phase", 0)
    phase9_status = (state.get("phases", {}).get(9) or state.get("phases", {}).get("9") or {}).get("status")
    # Phase 9 が in_progress ならper-PRボタンを常に表示
    is_review_status = current_phase >= 9 and phase9_status == "in_progress"

    # モーダル用ユニークID
    modal_id = f"phase9-modal-{workflow_id or 'none'}"

    summary_items = []
    table_rows = []
    all_confirmed = True
    for i, pr in enumerate(pull_requests):
        repo_name = pr.get("repo_name", "?")
        pr_number = pr.get("number", "?")
        pr_url = pr.get("url", "")
        confirmed = pr.get("human_review_confirmed", False)

        if not confirmed:
            all_confirmed = False

        # コメント数（human_comments を Devin / 人間に分離、issue_comments も含めて集計）
        copilot_comments = pr.get("copilot_comments", [])
        human_comments = pr.get("human_comments", [])
        issue_comments = pr.get("issue_comments", [])
        all_comments = copilot_comments + human_comments + issue_comments
        total_count = len(all_comments)
        replied_count = sum(1 for c in all_comments if c.get("replied"))

        # author 別の (対応済み, 合計) を集計
        author_stats: dict[str, list[int]] = {}  # {category: [replied, total]}
        for c in copilot_comments + human_comments:
            cat = _classify_author(c.get("author", ""))
            if cat not in author_stats:
                author_stats[cat] = [0, 0]
            author_stats[cat][1] += 1
            if c.get("replied"):
                author_stats[cat][0] += 1
        # issue comment は「PR全体」カテゴリで集計
        if issue_comments:
            issue_replied = sum(1 for c in issue_comments if c.get("replied"))
            author_stats["issue"] = [issue_replied, len(issue_comments)]

        if confirmed:
            icon = "✅"
        else:
            icon = "▶"

        # --- サマリー行（Phase 9 行内に表示） ---
        summary_items.append(
            f'<span class="phase9-summary-item">'
            f'{icon} <code>#{pr_number}</code> {html_mod.escape(repo_name)} {replied_count}/{total_count}'
            f'</span>'
        )

        # --- テーブル行（モーダル内に表示） ---
        # 進捗テキスト
        progress_main = f"{icon} {replied_count}/{total_count}"
        comment_detail = ""
        if total_count > 0:
            parts = []
            # Copilot/Devin/人間は常に表示（0件でも）、不明・PR全体は存在時のみ
            for key, display in [("copilot", "Copilot"), ("devin", "Devin"), ("human", "人間")]:
                stats = author_stats.get(key, [0, 0])
                parts.append(f"{display}: {stats[0]}/{stats[1]}件")
            unknown_stats = author_stats.get("unknown")
            if unknown_stats and unknown_stats[1] > 0:
                parts.append(f"不明: {unknown_stats[0]}/{unknown_stats[1]}件")
            issue_stats = author_stats.get("issue")
            if issue_stats and issue_stats[1] > 0:
                parts.append(f"PR全体: {issue_stats[0]}/{issue_stats[1]}件")
            comment_detail = f'<br><span style="color:#6b7280;font-size:11px;">{" / ".join(parts)}</span>'

        # PR番号リンク
        pr_link = f'<a href="{html_mod.escape(pr_url)}" target="_blank" style="text-decoration:none;"><code>#{pr_number}</code></a>' if pr_url else f'<code>#{pr_number}</code>'

        # per-PR アクションボタン
        action_cell = ""
        if is_review_status and workflow_id:
            if bg_running:
                # 実行中はボタンを無効化し、実行中インジケーターを表示
                action_cell = (
                    '<div class="phase9-review-actions">'
                    '<span class="badge" style="background:#dbeafe;color:#1e40af;font-size:11px;">実行中...</span>'
                    '</div>'
                )
            else:
                recheck_btn = (
                    f'<button type="button" class="phase-run-btn" style="font-size:11px;padding:2px 8px;" '
                    f'onclick="prReviewAction(\'{workflow_id}\', {i}, \'recheck\')">再確認</button>'
                )
                if confirmed:
                    toggle_btn = (
                        f'<button type="button" class="phase-retry-btn" style="font-size:11px;padding:2px 8px;" '
                        f'onclick="prReviewAction(\'{workflow_id}\', {i}, \'unmark_complete\')">対応完了を取消</button>'
                    )
                else:
                    toggle_btn = (
                        f'<button type="button" class="phase-run-btn" style="font-size:11px;padding:2px 8px;background:transparent;color:#059669;border:1px solid #059669;" '
                        f'onclick="prReviewAction(\'{workflow_id}\', {i}, \'mark_complete\')">対応完了</button>'
                    )
                auto_fix_btn = (
                    f'<button type="button" class="phase-run-btn" style="font-size:11px;padding:2px 8px;background:#7c3aed;" '
                    f'onclick="prReviewAction(\'{workflow_id}\', {i}, \'retry_auto_fix\')">自動修正</button>'
                )
                action_cell = f'<div class="phase9-review-actions">{recheck_btn}{auto_fix_btn}{toggle_btn}</div>'

        table_rows.append(
            f'<tr>'
            f'<td style="white-space:nowrap;">{pr_link}</td>'
            f'<td style="white-space:nowrap;">{html_mod.escape(repo_name)}</td>'
            f'<td>{progress_main}{comment_detail}</td>'
            f'<td>{action_cell}</td>'
            f'</tr>'
        )

    # プッシュ未検出警告
    push_warning = ""
    if state.get("push_verification_failed"):
        push_warning = (
            '<div style="color:#e74c3c;font-weight:bold;margin:8px 0;">'
            '⚠️ コード修正がプッシュされていません。修正をコミット＆プッシュしてください。'
            '</div>'
        )

    # 全PR完了ボタン（Phase 9 行のアクション列に表示）
    finish_btn = ""
    if is_review_status and workflow_id:
        if bg_running:
            finish_btn = (
                '<button type="button" class="phase-run-btn" disabled>'
                '実行中...</button>'
            )
        else:
            disabled = "" if all_confirmed else "disabled"
            finish_btn = (
                f'<button type="button" class="phase-run-btn" {disabled} '
                f'onclick="prReviewAction(\'{workflow_id}\', null, \'finish_review\')">'
                f'全PR完了 → 次へ</button>'
            )

    # テーブル（モーダル内）
    table_html = (
        '<table class="phase9-review-table">'
        '<thead><tr>'
        '<th>PR</th>'
        '<th>コンポーネント</th>'
        '<th>進捗</th>'
        '<th>アクション</th>'
        '</tr></thead><tbody>'
        + "".join(table_rows)
        + '</tbody></table>'
    )

    # サマリー（Phase 9 行内に表示）
    detail_btn = ""
    if is_review_status and workflow_id:
        if bg_running:
            detail_btn = (
                '<button type="button" class="phase9-detail-btn" disabled '
                'style="opacity:0.5;cursor:not-allowed;">実行中...</button>'
            )
        else:
            detail_btn = (
                f'<button type="button" class="phase9-detail-btn" '
                f'onclick="document.getElementById(\'{modal_id}\').showModal()">レビュー詳細を開く</button>'
            )

    summary_html = (
        f'<div class="phase9-summary">'
        f'<div>{"".join(summary_items)}</div>'
        f'{detail_btn}'
        f'</div>'
    )

    # モーダル（dialog 要素）
    modal_html = (
        f'<dialog id="{modal_id}" class="phase9-modal">'
        f'<div class="phase9-modal-header">'
        f'<h3>レビュー対応 詳細</h3>'
        f'<button type="button" class="phase9-modal-close" '
        f'onclick="document.getElementById(\'{modal_id}\').close()">&times;</button>'
        f'</div>'
        f'<div class="phase9-modal-body">'
        f'{push_warning}'
        f'{table_html}'
        f'</div>'
        f'</dialog>'
    )

    return f'{summary_html}{modal_html}', finish_btn


def result_icon(result: str) -> str:
    """結果をアイコンに変換"""
    icons = {
        "success": "✅",
        "failed": "❌",
        "warning": "⚠️",
    }
    return icons.get(result, "•")


def render_workflow_list(workflows: list) -> str:
    """ワークフロー一覧をHTMLにレンダリング"""
    running_ids = _get_running_identifiers()
    rows = ""
    for wf in workflows:
        # current_phase は「次に実行するフェーズ番号」なので、完了数は -1
        completed_phases = max(0, min(wf["current_phase"] - 1, 10))
        phase = min(wf["current_phase"], 10)  # can_continue 判定用
        progress = int((completed_phases / 10) * 100)
        repos = wf.get("repos", [])
        repos_html = "<br>".join(f"<code>{r}</code>" for r in repos) if repos else "-"
        waiting_status = wf.get("waiting_status")
        phase_status = wf.get("current_phase_status", "pending")
        run_mode = wf.get("run_mode", "step")
        run_mode_label = "AUTO mode" if run_mode == "auto" else "STEP mode"

        # バックグラウンド実行中かチェック
        wf_id = wf["workflow_id"]
        bg_running = (
            f"start:{wf_id}" in running_ids
            or f"continue:{wf_id}" in running_ids
        )

        if bg_running:
            disabled_attr = "disabled"
        else:
            can_continue = (
                phase < 10 and (
                    waiting_status is not None or
                    phase_status in ("in_progress", "pending", "failed")
                )
            )
            disabled_attr = "" if can_continue else "disabled"
        updated_at = format_datetime(wf["updated_at"])
        updated_html = updated_at.replace(" ", "<br>", 1) if " " in updated_at else updated_at
        actions_html = (
            f'<div class="row-actions">'
            f'<span class="row-run-mode-badge">{run_mode_label}</span>'
            f'<button type="button" class="row-run-btn" {disabled_attr} '
            f"onclick=\"continueWorkflowByMode('{wf_id}', '{run_mode}', event)\">"
            f'{"実行中..." if bg_running else "継続"}</button>'
            f'</div>'
        )

        # 待機状態バッジ
        waiting_html = ""
        if bg_running:
            waiting_html = '<span class="badge" style="background:#dbeafe;color:#1e40af;">実行中</span>'
        elif waiting_status:
            label = waiting_status_label(waiting_status)
            waiting_html = f'<span class="badge waiting-badge">{label}</span>'

        rows += f"""
        <tr onclick="location.href='/?id={wf['workflow_id']}'" style="cursor:pointer;">
            <td><code>{wf['workflow_id'][:12]}</code></td>
            <td>{repos_html}</td>
            <td>{wf['task_title'] or '-'}</td>
            <td class="col-progress">
                <div class="progress-container">
                    <div class="progress-bar">
                        <div class="progress-fill" style="width:{progress}%;"></div>
                    </div>
                    <span class="progress-text">{completed_phases}/10</span>
                    {waiting_html}
                </div>
            </td>
            <td class="text-muted col-updated">{updated_html}</td>
            <td class="col-actions">{actions_html}</td>
        </tr>
        """
    return rows


def cross_review_badge(phase: int, cross_review_results: dict) -> str:
    """クロスLLMレビュー結果をバッジHTMLに変換"""
    result = cross_review_results.get(phase) or cross_review_results.get(str(phase))
    if not result:
        return '<span class="text-muted">-</span>'

    assessment = result.get("overall_assessment", "unknown")
    findings = result.get("findings", [])
    findings_count = len(findings)
    critical_count = len([f for f in findings if f.get("severity") == "critical"])

    badge_styles = {
        "approve": "background:#dcfce7;color:#166534;",
        "request_changes": "background:#fef3c7;color:#92400e;",
        "needs_discussion": "background:#dbeafe;color:#1e40af;",
    }
    badge_labels = {
        "approve": "approve",
        "request_changes": "changes",
        "needs_discussion": "discuss",
    }
    style = badge_styles.get(assessment, "background:#f3f4f6;color:#4b5563;")
    label = badge_labels.get(assessment, assessment)

    detail = ""
    if findings_count > 0:
        detail = f' <span class="text-muted text-sm">({findings_count}件'
        if critical_count > 0:
            detail += f", critical {critical_count}"
        detail += ")</span>"

    return f'<span class="badge" style="{style}">{label}</span>{detail}'


def verification_badge(state: dict) -> str:
    """Phase 6 の検証結果をバッジHTMLに変換"""
    verification = state.get("verification", {}) if state else {}
    if not verification or all(v == "not_run" for v in verification.values()):
        return '<span class="text-muted">-</span>'

    badges = []
    for key in ("build", "test", "lint"):
        val = verification.get(key)
        if not val or val == "not_run":
            continue
        icon = "✓" if val == "pass" else "✗"
        status_class = "result-ok" if val == "pass" else "result-ng"
        badges.append(
            f'<span class="verification-item {status_class}">'
            f'{icon} {key}</span>'
        )
    return " ".join(badges) if badges else '<span class="text-muted">-</span>'


# ---------------------------------------------------------------------------
# Phase 6 失敗詳細 — 原因分類 & パネル表示
# ---------------------------------------------------------------------------

_CONFIG_ERROR_PATTERNS = [
    "unexpected EOF",
    "syntax error",
    "bash:",
    "sh:",
    "command not found",
    "lint_command",
    "repositories[].",
]

_ENVIRONMENT_ERROR_PATTERNS = [
    "EADDRINUSE",
    "Port ",
    "Docker daemon",
    "ETIMEDOUT",
    "ECONNREFUSED",
    "ENOMEM",
    "Permission denied",
]

_CODE_ERROR_PATTERNS = [
    "eslint",
    "TypeScript",
    "Failed",
    "AssertionError",
    "Expected",
    "Module not found",
]
_CODE_ERROR_REGEX = re.compile(r"TS\d{4}|\[P\d{2}\]")


def classify_verification_error(err: dict) -> dict:
    """verification_errors の 1 エントリを分類して返す。

    Returns:
        {
            "repository": str,
            "command": str,
            "category": "config_error" | "environment_error" | "code_error" | "unknown",
            "summary": str,
            "hints": list[str],
        }
    """
    repo = err.get("repository", "")
    cmd = err.get("command", "")
    output = err.get("error_output", "") or ""

    # 分類判定
    category = "unknown"
    summary = "原因を特定できません"
    hints: list[str] = []

    if any(p in output for p in _CONFIG_ERROR_PATTERNS):
        category = "config_error"
        summary = f"カスタム {cmd}_command の誤判定または設定不備"
        hints = [
            f"settings で repositories[].{cmd}_command を確認してください",
            "bash -c の引用符と regex を確認してください",
        ]
    elif any(p in output for p in _ENVIRONMENT_ERROR_PATTERNS):
        category = "environment_error"
        summary = "実行環境の問題（ポート競合・権限・接続エラー等）"
        hints = [
            "他のプロセスがポートを使用していないか確認してください",
            "環境を確認してから再試行してください",
        ]
    elif any(p in output for p in _CODE_ERROR_PATTERNS) or _CODE_ERROR_REGEX.search(output):
        category = "code_error"
        summary = f"コードの {cmd} エラー（TypeScript / テスト / Lint 等）"
        hints = [
            f"Phase 5 で生成されたコードに {cmd} エラーがあります",
            "Phase 5 からやり直すか、手動でコードを修正してください",
        ]

    return {
        "repository": repo,
        "command": cmd,
        "category": category,
        "summary": summary,
        "hints": hints,
    }


def phase6_failure_summary(state: dict) -> list[dict]:
    """Phase 6 の全 verification_errors を分類して返す。"""
    errors = state.get("verification_errors", [])
    return [
        classify_verification_error(e) for e in errors if e.get("success", True) is not True
    ]


def _phase6_settings_link(
    config_name: str, repo_name: str, command: str, workflow_id: str = "",
) -> str:
    """設定画面への deep link URL を生成する。"""
    if not config_name:
        return "/settings"
    from urllib.parse import quote
    field = f"{command}_command"
    url = f"/settings?config={quote(config_name)}&repo={quote(repo_name)}&field={quote(field)}"
    if workflow_id:
        url += f"&workflow_id={quote(workflow_id)}"
    return url


def render_phase6_failure_panel(state: dict, workflow_id: str) -> str:
    """Phase 6 失敗詳細パネルの HTML を生成する。

    verification に fail が含まれる場合にのみ内容を返す。
    """
    verification = state.get("verification", {})
    if not verification or not any(v == "fail" for v in verification.values()):
        return ""

    failures = phase6_failure_summary(state)
    if not failures:
        return ""

    config_name = state.get("config_name", "")

    cards_html = ""
    for f in failures:
        repo = f["repository"]
        cmd = f["command"]
        category = f["category"]
        summary = f["summary"]
        hints = f["hints"]

        # カテゴリ別ラベル
        cat_labels = {
            "config_error": ("設定問題", "#f59e0b", "#78350f"),
            "environment_error": ("環境問題", "#8b5cf6", "#4c1d95"),
            "code_error": ("コード問題", "#ef4444", "#7f1d1d"),
            "unknown": ("不明", "#6b7280", "#1f2937"),
        }
        cat_label, cat_bg, cat_color = cat_labels.get(category, cat_labels["unknown"])

        # エラー出力のプレビュー（先頭20行）
        raw_errors = state.get("verification_errors", [])
        error_output = ""
        full_output = ""
        for e in raw_errors:
            if e.get("repository") == repo and e.get("command") == cmd and not e.get("success"):
                full_output = e.get("error_output", "")
                lines = full_output.split("\n")
                error_output = "\n".join(lines[:20])
                if len(lines) > 20:
                    error_output += f"\n... (残り {len(lines) - 20} 行)"
                break

        # ヒント
        hints_html = ""
        if hints:
            hints_html = "<ul>" + "".join(f"<li>{html_mod.escape(h)}</li>" for h in hints) + "</ul>"

        # アクションボタン
        actions_html = ""
        if category == "config_error":
            link = _phase6_settings_link(config_name, repo, cmd, workflow_id)
            actions_html = f"""
            <a href="{link}" class="phase6-action-btn phase6-action-primary">設定を修正</a>
            <button type="button" class="phase6-action-btn"
                onclick="retryFromPhase('{workflow_id}', 6)">Phase 6 をリセット</button>
            """
        elif category == "code_error":
            actions_html = f"""
            <button type="button" class="phase6-action-btn phase6-action-primary"
                onclick="retryFromPhase('{workflow_id}', 5)">Phase 5 からやり直す</button>
            <button type="button" class="phase6-action-btn"
                onclick="retryFromPhase('{workflow_id}', 6)">Phase 6 をリセット</button>
            """
        elif category == "environment_error":
            actions_html = f"""
            <button type="button" class="phase6-action-btn phase6-action-primary"
                onclick="retryFromPhase('{workflow_id}', 6)">再試行</button>
            """
        else:
            actions_html = f"""
            <button type="button" class="phase6-action-btn"
                onclick="retryFromPhase('{workflow_id}', 6)">Phase 6 をリセット</button>
            """

        # 詳細ログ展開
        detail_toggle = ""
        if full_output and len(full_output.split("\n")) > 20:
            escaped_full = html_mod.escape(full_output)
            detail_toggle = f"""
            <details class="phase6-log-details">
                <summary>全ログを表示</summary>
                <pre class="phase6-log-full">{escaped_full}</pre>
            </details>
            """

        cards_html += f"""
        <div class="phase6-error-card">
            <div class="phase6-error-header">
                <span class="phase6-repo-badge">[{html_mod.escape(repo)}] {html_mod.escape(cmd)} 失敗</span>
                <span class="phase6-category-badge" style="background:{cat_bg};color:{cat_color};">
                    推定原因: {cat_label}
                </span>
            </div>
            <p class="phase6-summary">{html_mod.escape(summary)}</p>
            {hints_html}
            <pre class="phase6-log-preview">{html_mod.escape(error_output)}</pre>
            {detail_toggle}
            <div class="phase6-actions">{actions_html}</div>
        </div>
        """

    return f"""
    <div class="card phase6-failure-panel">
        <h3>Phase 6 失敗詳細</h3>
        {cards_html}
    </div>
    """


def render_phase_table(
    phases: dict,
    state: dict | None = None,
    workflow_id: str | None = None,
    bg_running: bool = False,
) -> str:
    """フェーズテーブルをHTMLにレンダリング

    Args:
        phases: フェーズ情報の辞書
        state: ワークフロー状態（待機状態判定用、オプション）
        workflow_id: ワークフローID（ボタン表示用、オプション）
        bg_running: バックグラウンドプロセスが実行中か
    """
    # 待機状態を取得
    waiting_status = get_waiting_status(state) if state else None
    current_phase = state.get("current_phase", 0) if state else 0
    cross_review_results = state.get("cross_review_results", {}) if state else {}

    # クロスレビュー対象フェーズ
    CROSS_REVIEW_PHASES = {2, 3, 4}

    rows = ""
    for i in range(1, 11):
        phase = phases.get(str(i)) or phases.get(i) or {}
        status = phase.get("status", "pending")
        started = format_datetime(phase.get("started_at"))
        completed = format_datetime(phase.get("completed_at"))
        retry = phase.get("retry_count", 0)

        # 現在のフェーズで待機中の場合、待機状態を表示
        phase_waiting = waiting_status if (i == current_phase and status == "in_progress") else None

        # バックグラウンド実行中の場合、現在フェーズを「実行中」として表示
        if bg_running and i == current_phase:
            substep = _get_substep_progress(workflow_id) if workflow_id else None
            substep_text = ""
            if substep:
                substep_text = re.sub(r"📋 Phase \d+ ", "", substep)
            status_html = (
                '<span class="badge" style="background:#dbeafe;color:#1e40af;">実行中</span>'
                f'<span class="substep-text" id="substep-progress">{html_mod.escape(substep_text)}</span>'
            )
        else:
            status_html = status_badge(status, phase_waiting)

        # Phase 9 (レビュー対応) のPR進捗を表示
        # PR進捗がある場合は「実行中」バッジを省略（行右端に表示済みのため）
        phase9_finish_btn = ""
        if i == 9 and status in ("in_progress", "completed") and state:
            result = render_pr_progress(state, workflow_id, bg_running=bg_running)
            if isinstance(result, tuple):
                pr_progress_html, phase9_finish_btn = result
            else:
                pr_progress_html = result
            if pr_progress_html:
                status_html = pr_progress_html

        # レビュー / 検証列
        if i in CROSS_REVIEW_PHASES:
            review_html = cross_review_badge(i, cross_review_results)
        elif i == 6:
            review_html = verification_badge(state)
        else:
            review_html = ""

        action_button = ""
        retry_button = ""
        if workflow_id:
            # Phase 9 は current_phase=9 になるまで非表示
            if i == 10 and current_phase < 10:
                action_button = ""
            elif bg_running:
                # バックグラウンド実行中は全ボタン無効
                action_button = (
                    f'<button type="button" class="phase-run-btn" disabled '
                    f'onclick="continueWorkflowStep(\'{workflow_id}\')">'
                    f'{"実行中..." if i == current_phase else "このフェーズを実行"}</button>'
                )
            else:
                # 現在のフェーズが completed の場合、次のフェーズも有効にする
                current_phase_status = (phases.get(str(current_phase)) or phases.get(current_phase) or {}).get("status")
                next_phase = current_phase + 1 if current_phase_status == "completed" else current_phase
                is_actionable_phase = i == current_phase or i == next_phase
                # failed フェーズもリトライ可能にする
                # ただし cross_review_blocked 中は バナーのボタンを使うため無効化
                is_runnable_status = status in ("in_progress", "pending", "failed")
                is_cr_blocked = (
                    state.get("waiting_for_human", False)
                    and status == "failed"
                    and i == current_phase
                    and _find_cross_review_blocked_phase(state) == i
                )
                enabled = is_actionable_phase and is_runnable_status and not is_cr_blocked
                disabled_attr = "" if enabled else "disabled"

                # Phase 9 は per-PR ボタンで操作するため、アクション列に完了ボタンを表示
                if i == 9 and i == current_phase and status == "in_progress":
                    action_button = phase9_finish_btn
                else:
                    # Phase 9 のボタンラベルを状態に応じて変更
                    btn_label = "このフェーズを実行"
                    if i == 9 and i == current_phase and status == "in_progress" and state:
                        btn_label = _phase9_button_label(state)

                    action_button = (
                        f'<button type="button" class="phase-run-btn" {disabled_attr} '
                        f'onclick="continueWorkflowStep(\'{workflow_id}\')">{btn_label}</button>'
                    )

            # 完了済み/失敗済みフェーズにリセットボタンを表示（実行中は除く）
            if not bg_running and status in ("completed", "failed"):
                retry_button = (
                    f'<button type="button" class="phase-retry-btn" '
                    f'title="選択したフェーズ以降のステータスを未実行に戻します" '
                    f'onclick="retryFromPhase(\'{workflow_id}\', {i})">リセット</button>'
                )

        rows += f"""
        <tr>
            <td>{i}</td>
            <td>{phase_name(i)}</td>
            <td>{status_html}</td>
            <td>{started}</td>
            <td>{completed}</td>
            <td>{retry if retry > 0 else '-'}</td>
            <td>{review_html}</td>
            <td>{action_button} {retry_button}</td>
        </tr>
        """
    return rows


def render_audit_log(audit_log: list) -> str:
    """監査ログをHTMLにレンダリング"""
    rows = ""
    for entry in reversed(audit_log[-30:]):  # 最新30件を新しい順に表示
        ts = format_datetime(entry.get("timestamp"))
        phase = entry.get("phase", "-")
        action = entry.get("action", "-")
        result = entry.get("result", "-")
        details = entry.get("details")
        details_str = ""
        if details:
            if isinstance(details, dict):
                details_str = ", ".join(f"{k}={v}" for k, v in list(details.items())[:3])
            else:
                details_str = str(details)[:50]
        rows += f"""
        <tr>
            <td class="text-nowrap text-muted">{ts}</td>
            <td class="text-center">{phase}</td>
            <td><code>{action}</code></td>
            <td class="text-nowrap">{result_icon(result)} {result}</td>
            <td class="text-muted text-sm">{details_str}</td>
        </tr>
        """
    return rows


def github_status_badge(status: str) -> str:
    """GitHubステータスをバッジHTMLに変換"""
    styles = {
        "open": "background:#dcfce7;color:#166534;",
        "closed": "background:#f3f4f6;color:#4b5563;",
        "merged": "background:#f3e8ff;color:#6b21a8;",
        "draft": "background:#fef3c7;color:#92400e;",
    }
    style = styles.get(status.lower(), "background:#f3f4f6;color:#4b5563;") if status else "background:#f3f4f6;color:#4b5563;"
    display = status or "-"
    return f'<span class="badge" style="{style}">{display}</span>'


def render_prs(pull_requests: list) -> str:
    """PR一覧をHTMLにレンダリング"""
    if not pull_requests:
        return "<p>PRはありません</p>"
    rows = ""
    for pr in pull_requests:
        url = pr.get("url", "#")
        title = pr.get("title", "-")
        number = pr.get("number", "-")
        owner = pr.get("owner", "")
        repo = pr.get("repo", pr.get("repo_name", "-"))
        repo_name = f"{owner}/{repo}" if owner else repo
        hokusai_status = pr.get("status", "-")
        github_status = pr.get("github_status", "-")
        copilot = "✅" if pr.get("copilot_review_passed") else "-"
        rows += f"""
        <tr>
            <td><a href="{url}" target="_blank"><strong>#{number}</strong></a></td>
            <td><code>{repo_name}</code></td>
            <td>{title}</td>
            <td>{status_badge(hokusai_status)}</td>
            <td>{github_status_badge(github_status)}</td>
            <td>{copilot}</td>
        </tr>
        """
    return f"""
    <table>
        <thead><tr><th>PR</th><th>リポジトリ</th><th>タイトル</th><th>内部</th><th>GitHub</th><th>Copilot</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
    """


def render_review_rules(rules: dict) -> str:
    """レビュールール結果をHTMLにレンダリング

    Args:
        rules: {"CQ01": {"name": "Dead Code", "result": "OK", "note": ""}, ...}
    """
    if not rules:
        return "<p class='text-muted'>ルール別結果はありません（Phase 7完了後に表示されます）</p>"

    # サマリー計算
    total = len(rules)
    ok = sum(1 for r in rules.values() if r.get("result") == "OK")
    ng = sum(1 for r in rules.values() if r.get("result") == "NG")
    skip = sum(1 for r in rules.values() if r.get("result") == "SKIP")

    rows = ""
    for rule_id, rule in sorted(rules.items()):
        result = rule.get("result", "-")
        if result == "OK":
            icon = "✓"
            result_class = "result-ok"
        elif result == "NG":
            icon = "✗"
            result_class = "result-ng"
        elif result == "SKIP":
            icon = "—"
            result_class = "result-skip"
        else:
            icon = "•"
            result_class = "text-muted"

        note = rule.get("note", "")
        rows += f"""
        <tr>
            <td><code>{rule_id}</code></td>
            <td>{rule.get('name', '-')}</td>
            <td class="{result_class}">{icon} {result}</td>
            <td class="text-muted text-sm">{note if note else '-'}</td>
        </tr>
        """

    return f"""
    <div class="summary-stats">
        <span class="stat stat-ok">✓ {ok} OK</span>
        <span class="stat stat-ng">✗ {ng} NG</span>
        <span class="stat stat-skip">— {skip} SKIP</span>
        <span class="stat stat-total">{total} 件</span>
    </div>
    <table>
        <thead><tr><th>ルールID</th><th>ルール名</th><th>結果</th><th>備考</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
    """


def render_review_by_repo(review_by_repo: dict) -> str:
    """リポジトリ別レビュー結果をHTMLにレンダリング

    Args:
        review_by_repo: {
            "Backend": {"passed": True, "rules": {...}, "issues": [...]},
            "API": {"passed": True, "rules": {...}, "issues": [...]},
        }
    """
    if not review_by_repo:
        return "<p class='text-muted'>リポジトリ別結果はありません</p>"

    sections = ""
    for repo_name, repo_result in sorted(review_by_repo.items()):
        passed = repo_result.get("passed", False)
        rules = repo_result.get("rules", {})
        issues = repo_result.get("issues", [])

        # サマリー計算
        total = len(rules)
        ok = sum(1 for r in rules.values() if r.get("result") == "OK")
        ng = sum(1 for r in rules.values() if r.get("result") == "NG")
        skip = sum(1 for r in rules.values() if r.get("result") == "SKIP")

        # ステータス
        status_icon = "✓" if passed else "✗"
        status_class = "repo-passed" if passed else "repo-failed"

        # ルール行を生成
        rows = ""
        for rule_id, rule in sorted(rules.items()):
            result = rule.get("result", "-")
            if result == "OK":
                icon = "✓"
                result_class = "result-ok"
            elif result == "NG":
                icon = "✗"
                result_class = "result-ng"
            elif result == "SKIP":
                icon = "—"
                result_class = "result-skip"
            else:
                icon = "•"
                result_class = "text-muted"

            note = rule.get("note", "")
            rows += f"""
            <tr>
                <td><code>{rule_id}</code></td>
                <td>{rule.get('name', '-')}</td>
                <td class="{result_class}">{icon} {result}</td>
                <td class="text-muted text-sm">{note if note else '-'}</td>
            </tr>
            """

        # 問題リスト
        issues_html = ""
        if issues:
            issues_items = "".join(f"<li>{issue}</li>" for issue in issues[:5])
            if len(issues) > 5:
                issues_items += f"<li class='text-muted'>...他 {len(issues) - 5} 件</li>"
            issues_html = f"<ul class='issues-list'>{issues_items}</ul>"

        sections += f"""
        <div class="repo-card {status_class}">
            <h4 class="repo-header">
                <span class="repo-status">{status_icon}</span>
                {repo_name}
                <span class="repo-score">{ok}/{total}</span>
            </h4>
            <div class="summary-stats">
                <span class="stat stat-ok">✓ {ok} OK</span>
                <span class="stat stat-ng">✗ {ng} NG</span>
                <span class="stat stat-skip">— {skip} SKIP</span>
            </div>
            {issues_html}
            <details class="rule-details">
                <summary>ルール詳細を表示</summary>
                <table>
                    <thead><tr><th>ルールID</th><th>ルール名</th><th>結果</th><th>備考</th></tr></thead>
                    <tbody>{rows}</tbody>
                </table>
            </details>
        </div>
        """

    return sections


def _notion_status_badge(state: dict) -> str:
    """Notion接続状態のバッジHTMLを生成"""
    notion_connected = state.get("notion_connected")
    if notion_connected is True:
        return '<span class="badge badge-ok">Connected</span>'
    elif notion_connected is False:
        return '<span class="badge badge-warn">Disconnected (Skipped)</span>'
    else:
        # None: 旧データや未確認
        return '<span class="badge badge-muted">-</span>'


def apply_cross_review_fixes(workflow_id: str, phase: int) -> dict:
    """cross-review findings を元に LLM でドキュメントを修正"""
    from hokusai.state import add_audit_log
    from hokusai.utils.notion_helpers import sync_phase_page_from_state

    store = _get_store()
    state = store.load_workflow(workflow_id)
    if not state:
        return {"success": False, "errors": ["ワークフローが見つかりません"]}

    if is_workflow_running(workflow_id):
        return {"success": False, "errors": ["ワークフローが実行中です"]}

    # 1. 現在の文書を state から取得
    PHASE_CONTENT_KEYS = {2: "research_result", 3: "design_result", 4: "work_plan"}
    content_key = PHASE_CONTENT_KEYS.get(phase)
    if not content_key:
        return {"success": False, "errors": [f"Phase {phase} は修正対象外です"]}
    current_doc = state.get(content_key)
    if not current_doc:
        return {"success": False, "errors": [f"Phase {phase} の文書が state にありません"]}

    # 2. findings 取得
    cr_results = state.get("cross_review_results", {})
    result = cr_results.get(phase) or cr_results.get(str(phase))
    if not result:
        return {"success": False, "errors": [f"Phase {phase} のレビュー結果がありません"]}

    findings = result.get("findings", [])
    findings_text = "\n".join(
        f"- [{f.get('severity')}] {f.get('title')}: {f.get('description')}"
        + (f"\n  提案: {f.get('suggestion')}" if f.get("suggestion") else "")
        for f in findings
    )

    # 3. 修正プロンプト構築
    prompt = f"""以下の文書をクロスレビューの指摘に従って修正してください。

## 指摘事項
{findings_text}

## 現在の文書
{current_doc}

## 指示
- 指摘を反映して全文を再出力せよ
- 文書の構造を維持すること
- 前置き文禁止
- 要約禁止
- Markdown本文のみ出力"""

    # 4. LLM 実行
    from hokusai.config import create_config_from_env_and_file, set_config
    config_name = state.get("config_name")
    if config_name:
        config_path = _resolve_config_path(config_name)
        if config_path:
            set_config(create_config_from_env_and_file(str(config_path)))

    from hokusai.config import get_config
    from hokusai.integrations.claude_code import ClaudeCodeClient
    llm_timeout = get_config().skill_timeout
    claude = ClaudeCodeClient()
    try:
        fixed_doc = claude.execute_prompt(prompt=prompt, timeout=llm_timeout)
    except Exception as e:
        return {"success": False, "errors": [f"LLM実行エラー: {e}"]}

    # 5. state 更新
    state[content_key] = fixed_doc
    notion_updated = sync_phase_page_from_state(state, phase)
    subpage_url = state.get("phase_subpages", {}).get(phase)
    state = add_audit_log(state, phase, "cross_review_fix_applied", "success",
        details={"findings_count": len(findings),
                 "critical_count": sum(1 for f in findings if f.get("severity") == "critical"),
                 "notion_updated": notion_updated})
    store.save_workflow(workflow_id, state)

    msg = f"Phase {phase} の文書を修正しました"
    if subpage_url and not notion_updated:
        msg += "（※ Notion子ページの更新に失敗しました。再レビュー後に再同期されます）"
    return {"success": True, "message": msg}


def rerun_cross_review_for_phase(workflow_id: str, phase: int) -> dict:
    """修正後の文書に対して cross-review を再実行"""
    from hokusai.state import add_audit_log
    from hokusai.utils.notion_helpers import sync_phase_page_from_state

    store = _get_store()
    state = store.load_workflow(workflow_id)
    if not state:
        return {"success": False, "errors": ["ワークフローが見つかりません"]}

    if is_workflow_running(workflow_id):
        return {"success": False, "errors": ["ワークフローが実行中です"]}

    PHASE_CONTENT_KEYS = {2: "research_result", 3: "design_result", 4: "work_plan"}
    content_key = PHASE_CONTENT_KEYS.get(phase)
    if not content_key:
        return {"success": False, "errors": [f"Phase {phase} は対象外です"]}
    document = state.get(content_key)
    if not document:
        return {"success": False, "errors": [f"Phase {phase} の文書がありません"]}

    # Configをロード（cross_review.enabled=True にするために必須）
    from hokusai.config import create_config_from_env_and_file, reset_config, set_config
    config_name = state.get("config_name")
    if not config_name:
        return {"success": False, "errors": ["ワークフローに config_name が設定されていません"]}
    config_path = _resolve_config_path(config_name)
    if not config_path:
        return {"success": False, "errors": [f"設定ファイルが見つかりません: {config_name}"]}
    reset_config()
    loaded_config = create_config_from_env_and_file(str(config_path))
    set_config(loaded_config)
    if not loaded_config.cross_review.enabled:
        return {"success": False, "errors": [f"設定 '{config_name}' で cross_review が無効です"]}
    if phase not in loaded_config.cross_review.phases:
        return {"success": False, "errors": [f"Phase {phase} は cross_review 対象外です（対象: {loaded_config.cross_review.phases}）"]}

    # cross-review 再実行
    from hokusai.utils.cross_review import execute_cross_review
    state = add_audit_log(state, phase, "cross_review_rerun_started", "info")
    state = execute_cross_review(state, document, phase)

    # critical 解消チェック
    cr_result = state.get("cross_review_results", {}).get(phase, {})
    findings = cr_result.get("findings", [])
    critical_count = sum(1 for f in findings if f.get("severity") == "critical")

    if critical_count == 0:
        state["waiting_for_human"] = False
        state["human_input_request"] = ""
        # フェーズステータスを completed に復元（completed_at / current_phase も更新）
        from hokusai.state import PhaseStatus, update_phase_status
        phases = state.get("phases", {})
        phase_data = phases.get(phase) or phases.get(str(phase))
        if phase_data and phase_data.get("status") == "failed":
            state = update_phase_status(state, phase, PhaseStatus.COMPLETED)
            state["phases"][phase]["error_message"] = None
    else:
        # fix_applied 検出をリセットするため blocked エントリを再記録
        state = add_audit_log(state, phase, "cross_review_blocked", "warning",
            details={"reason": f"再レビュー後も critical 指摘が {critical_count} 件残存"})

    state = add_audit_log(state, phase, "cross_review_rerun_completed", "success",
        details={"findings_count": len(findings), "critical_count": critical_count,
                 "assessment": cr_result.get("overall_assessment"),
                 "resolved": critical_count == 0})
    store.save_workflow(workflow_id, state)
    sync_phase_page_from_state(state, phase)

    msg = f"Phase {phase} の再レビュー完了。"
    if critical_count == 0:
        msg += " Critical 指摘が解消されました。ワークフローを続行できます。"
    else:
        msg += f" まだ {critical_count} 件の critical 指摘があります。"
    return {"success": True, "message": msg, "critical_count": critical_count}


def continue_ignoring_cross_review(workflow_id: str, phase: int) -> dict:
    """cross-review 指摘を無視してワークフローを続行"""
    from hokusai.state import add_audit_log
    from hokusai.utils.notion_helpers import sync_phase_page_from_state

    store = _get_store()
    state = store.load_workflow(workflow_id)
    if not state:
        return {"success": False, "errors": ["ワークフローが見つかりません"]}

    cr_results = state.get("cross_review_results", {})
    result = cr_results.get(phase) or cr_results.get(str(phase))
    findings = result.get("findings", []) if result else []
    critical_count = sum(1 for f in findings if f.get("severity") == "critical")

    state["waiting_for_human"] = False
    state["human_input_request"] = ""
    # フェーズステータスを completed に復元（completed_at / current_phase も更新）
    from hokusai.state import PhaseStatus, update_phase_status
    phases = state.get("phases", {})
    phase_data = phases.get(phase) or phases.get(str(phase))
    if phase_data and phase_data.get("status") == "failed":
        state = update_phase_status(state, phase, PhaseStatus.COMPLETED)
        state["phases"][phase]["error_message"] = None
    state = add_audit_log(state, phase, "cross_review_ignored", "warning",
        details={"findings_count": len(findings), "critical_count": critical_count})
    store.save_workflow(workflow_id, state)
    sync_phase_page_from_state(state, phase)

    return {"success": True, "message": f"Phase {phase} のレビュー指摘を無視して続行可能にしました"}


def _notion_warning_banner(state: dict, workflow_id: str = "") -> str:
    """Notion未接続時の警告バナーHTMLを生成"""
    if state.get("notion_connected") is not False:
        return ""

    # スキップされた Notion アクションがあるか確認
    skipped_actions = []
    for entry in state.get("audit_log", []):
        if (entry.get("result") == "skipped"
            and "notion" in entry.get("action", "").lower()
        ):
            skipped_actions.append(entry.get("action", ""))

    retry_btn = ""
    if skipped_actions and workflow_id:
        retry_btn = (
            f' <button type="button" class="phase-retry-btn" '
            f'onclick="retryNotion(\'{workflow_id}\')">'
            f'Notion リトライ</button>'
        )

    return (
        '<div class="notion-warning">'
        f'⚠️ この実行では Notion に反映されていません。{retry_btn}'
        '</div>'
    )


def _hygiene_action_banner(state: dict, workflow_id: str) -> str:
    """ブランチ衛生チェック待機時のアクション選択バナーHTMLを生成"""
    waiting_status = get_waiting_status(state)
    if waiting_status != "branch_hygiene":
        return ""

    issues = state.get("branch_hygiene_issues", [])
    issues_html = ""
    for issue in issues:
        severity_icon = {"error": "&#10060;", "warning": "&#9888;&#65039;", "info": "&#8505;&#65039;"}.get(
            issue.get("severity", "info"), ""
        )
        issues_html += f"<li>{severity_icon} {issue.get('message', '')}"
        if issue.get("recommendation"):
            issues_html += f"<br><small>{issue['recommendation']}</small>"
        issues_html += "</li>"

    return f"""
    <div class="hygiene-action-banner">
        <h4>&#9888;&#65039; ブランチ衛生問題を検出</h4>
        <ul>{issues_html}</ul>
        <div class="hygiene-actions">
            <button type="button" class="hygiene-btn hygiene-btn-primary"
                onclick="executeHygieneAction('{workflow_id}', 'rebase')">
                Rebase
            </button>
            <button type="button" class="hygiene-btn"
                onclick="executeHygieneAction('{workflow_id}', 'cherry-pick')">
                Cherry-pick
            </button>
            <button type="button" class="hygiene-btn"
                onclick="executeHygieneAction('{workflow_id}', 'merge-base')">
                Merge
            </button>
            <button type="button" class="hygiene-btn hygiene-btn-muted"
                onclick="executeHygieneAction('{workflow_id}', 'ignore')">
                無視して続行
            </button>
        </div>
    </div>
    """


def _cross_review_blocked_banner(state: dict, workflow_id: str) -> str:
    """クロスレビュー blocked 時の findings 表示 + アクションボタン"""
    waiting_status = get_waiting_status(state)
    if waiting_status != "cross_review_blocked":
        return ""

    # blocked phase を特定（audit_log ベース）
    blocked_phase = _find_cross_review_blocked_phase(state)
    if not blocked_phase:
        return ""
    cross_review_results = state.get("cross_review_results", {})
    result = cross_review_results.get(blocked_phase) or cross_review_results.get(str(blocked_phase))
    if not result:
        return ""

    # 修正適用済みか判定
    # cross_review_blocked のタイムスタンプ以降に fix_applied があるかで判定
    # （リセット後の再実行で過去の fix_applied が誤判定されるのを防ぐ）
    # fix_applied 判定: 最新の blocked/rerun_completed 以降に fix_applied があるか
    reset_ts = ""
    for entry in reversed(state.get("audit_log", [])):
        if entry.get("phase") == blocked_phase and entry.get("action") in (
            "cross_review_blocked", "cross_review_rerun_completed",
        ):
            reset_ts = entry.get("timestamp", "")
            break
    fix_applied = any(
        entry.get("action") == "cross_review_fix_applied"
        and entry.get("phase") == blocked_phase
        and entry.get("timestamp", "") > reset_ts
        for entry in state.get("audit_log", [])
    )

    # findings HTML 構築
    assessment = result.get("overall_assessment", "unknown")
    summary = result.get("summary", "")
    confidence = result.get("confidence_score")
    findings = result.get("findings", [])

    findings_html = ""
    for f in findings:
        severity = f.get("severity", "info")
        icon = {"critical": "&#128308;", "major": "&#128992;", "minor": "&#128993;", "info": "&#128309;"}.get(severity, "")
        title = html_mod.escape(f.get("title", ""))
        desc = html_mod.escape(f.get("description", ""))
        suggestion = html_mod.escape(f.get("suggestion", ""))
        findings_html += f"""
        <div class="finding-item finding-{severity}">
            <div class="finding-header">{icon} [{severity}] {title}</div>
            <div class="finding-desc">{desc}</div>
            {f'<div class="finding-suggestion">&#128161; {suggestion}</div>' if suggestion else ''}
        </div>"""

    confidence_str = f"{confidence:.0%}" if confidence is not None else "-"

    return f"""
    <div class="cross-review-banner">
        <h4>&#128269; Cross-Review: Phase {blocked_phase} に critical 指摘があります</h4>
        <div class="cr-summary">
            <span><strong>Assessment:</strong> {html_mod.escape(assessment)}</span>
            <span><strong>Confidence:</strong> {confidence_str}</span>
        </div>
        <p>{html_mod.escape(summary)}</p>
        <div class="cr-findings">{findings_html}</div>
        <div class="cr-actions">
            <button type="button" class="hygiene-btn hygiene-btn-primary"
                onclick="applyCrossReviewFixes('{workflow_id}', {blocked_phase})"
                {'disabled style="opacity:0.5"' if fix_applied else ''}>
                {'&#10003; 修正適用済み' if fix_applied else '指摘を反映して修正'}
            </button>
            <button type="button" class="hygiene-btn{' hygiene-btn-primary' if fix_applied else ''}"
                onclick="rerunCrossReview('{workflow_id}', {blocked_phase})">
                再レビュー{'（推奨）' if fix_applied else ''}
            </button>
            <button type="button" class="hygiene-btn hygiene-btn-muted"
                onclick="ignoreCrossReview('{workflow_id}', {blocked_phase})">
                &#9888;&#65039; 無視して続行
            </button>
        </div>
    </div>"""


def render_phase_page_links(state: dict, workflow_id: str, bg_running: bool = False) -> str:
    """Phase 2-4 のフェーズページリンクと判断情報を表示する。"""
    rows = []
    current_phase = state.get("current_phase", 0)
    waiting_status = get_waiting_status(state)
    for phase in (2, 3, 4):
        context = get_phase_page_context(state, phase)
        subpage_url = context["phase_subpage_url"]
        link_html = (
            f'<a href="{subpage_url}" target="_blank">Open ↗</a>'
            if subpage_url else "<span class='text-muted'>未作成</span>"
        )
        decision = context["current_decision"]
        decision_html = f"<code>{decision}</code>" if decision != PHASE_PAGE_DECISION_DEFAULT else "<span class='text-muted'>-</span>"
        action_html = "<span class='text-muted'>-</span>"
        if phase == current_phase and waiting_status == "cross_review_blocked":
            disabled = "disabled" if bg_running else ""
            action_html = (
                f'<div class="phase-decision-actions">'
                f'<button type="button" class="phase-decision-btn" {disabled} '
                f'onclick="applyPhaseDecision(\'{workflow_id}\', {phase}, \'request_changes\')">request_changes</button>'
                f'<button type="button" class="phase-decision-btn phase-decision-btn-primary" {disabled} '
                f'onclick="applyPhaseDecision(\'{workflow_id}\', {phase}, \'approve_and_move_next\')">approve_and_move_next</button>'
                f"</div>"
            )
        rows.append(
            f"<tr>"
            f"<td>Phase {phase}</td>"
            f"<td>{link_html}</td>"
            f"<td><code>{context['display_status']}</code></td>"
            f"<td>{decision_html}</td>"
            f"<td><code>{context['recommended_action']}</code></td>"
            f"<td>{action_html}</td>"
            f"</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>Phase</th><th>Page</th><th>Display Status</th><th>Decision</th><th>Recommended</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def submit_phase_decision(workflow_id: str, phase: int, decision: str) -> dict:
    """人間判断を state と Notion フェーズページへ反映する。"""
    from hokusai.state import PhaseStatus, add_audit_log, update_phase_status
    from hokusai.utils.notion_helpers import sync_phase_page_from_state

    if phase not in (2, 3, 4):
        return {"success": False, "errors": [f"Phase {phase} は対象外です"]}
    if decision not in {"request_changes", "approve_and_move_next"}:
        return {"success": False, "errors": [f"未知の decision です: {decision}"]}

    store = _get_store()
    state = store.load_workflow(workflow_id)
    if not state:
        return {"success": False, "errors": ["ワークフローが見つかりません"]}
    if is_workflow_running(workflow_id):
        return {"success": False, "errors": ["ワークフローが実行中です"]}

    initialize_phase_page_state(state, phase)
    now = datetime.now().isoformat()
    state["phase_page_decision"][phase] = decision
    state["phase_page_last_human_note_at"][phase] = now
    state["phase_page_recommended_action"][phase] = decision

    if decision == "approve_and_move_next":
        state["waiting_for_human"] = False
        state["human_input_request"] = None
        phase_state = state.get("phases", {}).get(phase) or state.get("phases", {}).get(str(phase))
        if phase_state and phase_state.get("status") == PhaseStatus.FAILED.value:
            state = update_phase_status(state, phase, PhaseStatus.COMPLETED)
            state["phases"][phase]["error_message"] = None

    state = add_audit_log(
        state,
        phase,
        "phase_page_decision_recorded",
        "success",
        details={"decision": decision},
    )
    store.save_workflow(workflow_id, state)

    synced = sync_phase_page_from_state(state, phase)
    message = f"Phase {phase} の判断を `{decision}` として記録しました"
    if decision == "request_changes":
        message += "。必要に応じて修正または再レビューを続けてください。"
    elif decision == "approve_and_move_next":
        message += "。ワークフローを継続できます。"
    if state.get("phase_subpages", {}).get(phase) and not synced:
        message += "（※ Notionフェーズページの同期には失敗しました）"
    return {"success": True, "message": message}


def render_detail_page(state: dict, bg_running: bool = False) -> str:
    """詳細ページをHTMLにレンダリング"""
    workflow_id = state.get("workflow_id", "-")
    task_title = state.get("task_title", "-")
    task_url = state.get("task_url", "#")
    branch = state.get("branch_name", "-")
    base_branch = state.get("base_branch", "-")
    current_phase = state.get("current_phase", 0)
    phases = state.get("phases", {})
    audit_log = state.get("audit_log", [])
    pull_requests = state.get("pull_requests", [])
    review_rules = state.get("final_review_rules", {})
    review_by_repo = state.get("final_review_by_repo", {})

    # リポジトリ情報を抽出
    repos = set()
    for pr in pull_requests:
        owner = pr.get("owner", "")
        repo = pr.get("repo", "")
        if owner and repo:
            repos.add(f"{owner}/{repo}")
    repos_html = ", ".join(f"<code>{r}</code>" for r in sorted(repos)) if repos else "-"

    # バックグラウンド実行中のポーリングスクリプト（外側 f-string とネストすると Python 3.11 で構文エラーになるため変数化）
    bg_polling_script = ""
    if bg_running:
        bg_polling_script = f'''
    <div data-bg-running="1" style="display:none;"></div>
    <script>
    setInterval(async () => {{
        try {{
            const resp = await fetch('/api/workflow/progress?id={workflow_id}');
            const data = await resp.json();
            if (!data.running) {{ sessionStorage.removeItem('_hokusai_banner'); location.reload(); return; }}
            const el = document.getElementById('substep-progress');
            if (el && data.substep) {{
                el.textContent = data.substep.replace(new RegExp('📋 Phase ' + '\\\\d+ '), '');
            }}
        }} catch(e) {{}}
    }}, 3000);
    </script>
    '''

    return f"""
    <a href="/" class="back-link">&larr; 一覧に戻る</a>
    <h1 class="page-title">{task_title}</h1>

    <div class="card">
        <h3>基本情報</h3>
        <table class="info-table">
            <tr><td>ID</td><td><code class="copyable" onclick="copyToClipboard('{workflow_id}')" title="クリックでコピー">{workflow_id}</code></td></tr>
            <tr><td>リポジトリ</td><td>{repos_html}</td></tr>
            <tr><td>タスク</td><td><a href="{task_url}" target="_blank">Notion で開く ↗</a></td></tr>
            <tr><td>ブランチ</td><td><code>{branch}</code></td></tr>
            <tr><td>ベース</td><td><code>{base_branch}</code></td></tr>
            <tr><td>フェーズ</td><td><strong>{current_phase}/10</strong> ({phase_name(current_phase)})</td></tr>
            <tr><td>Notion</td><td>{_notion_status_badge(state)}</td></tr>
        </table>
    </div>

    {_notion_warning_banner(state, workflow_id)}
    {_hygiene_action_banner(state, workflow_id)}
    {_cross_review_blocked_banner(state, workflow_id)}

    <div class="card">
        <h3>フェーズ進捗</h3>
        <table>
            <thead><tr><th>#</th><th>フェーズ</th><th>ステータス</th><th>開始</th><th>完了</th><th>リトライ</th><th>レビュー / 検証</th><th>実行</th></tr></thead>
            <tbody>{render_phase_table(phases, state, workflow_id, bg_running)}</tbody>
        </table>
    </div>

    {render_phase6_failure_panel(state, workflow_id)}

    <div class="card">
        <h3>Phase Pages</h3>
        {render_phase_page_links(state, workflow_id, bg_running)}
    </div>

    <div class="card">
        <h3>Pull Requests</h3>
        {render_prs(pull_requests)}
    </div>

    <div class="card">
        <h3>レビュー結果（Phase 7）</h3>
        {render_review_by_repo(review_by_repo) if review_by_repo else render_review_rules(review_rules)}
    </div>

    <div class="card">
        <h3>監査ログ（最新30件）</h3>
        <table>
            <thead><tr><th>時刻</th><th>Phase</th><th>アクション</th><th>結果</th><th>詳細</th></tr></thead>
            <tbody>{render_audit_log(audit_log)}</tbody>
        </table>
    </div>

    <div class="card">
        <h3>操作</h3>
        <button type="button" class="delete-btn" {"disabled" if bg_running else ""} onclick="deleteWorkflow('{workflow_id}')">このワークフローを削除</button>
    </div>

    {bg_polling_script}
    """


def parse_builtin_rules() -> list[dict]:
    """組み込みルールをパース

    Returns:
        [
            {"id": "CQ01", "category": "コード品質", "name": "Dead Code", "items": [...]},
            ...
        ]
    """
    if not CHECKLIST_PATH.exists():
        return []

    content = CHECKLIST_PATH.read_text(encoding="utf-8")
    rules = []
    current_category = ""
    current_rule = None

    for line in content.split("\n"):
        line = line.strip()

        # カテゴリヘッダー: ## コード品質 (CQ)
        if line.startswith("## ") and "(" in line and ")" in line:
            # カテゴリ名を抽出
            current_category = line[3:].split("(")[0].strip()

        # ルールヘッダー: ### CQ01: Dead Code
        elif line.startswith("### "):
            if current_rule:
                rules.append(current_rule)
            # ルールIDと名前を抽出
            rule_header = line[4:]
            if ":" in rule_header:
                rule_id, rule_name = rule_header.split(":", 1)
                current_rule = {
                    "id": rule_id.strip(),
                    "category": current_category,
                    "name": rule_name.strip(),
                    "items": [],
                }
            else:
                current_rule = None

        # チェック項目: - [ ] 説明
        elif line.startswith("- [ ]") and current_rule:
            item = line[5:].strip()
            current_rule["items"].append(item)

    # 最後のルールを追加
    if current_rule:
        rules.append(current_rule)

    return rules


def list_config_files() -> list[str]:
    """設定ファイル一覧を取得

    Returns:
        ["my-project", "another-project", ...]  (拡張子なし)
    """
    if not CONFIGS_DIR.exists():
        return []

    configs = []
    for f in CONFIGS_DIR.glob("*.yaml"):
        configs.append(f.stem)
    for f in CONFIGS_DIR.glob("*.yml"):
        if f.stem not in configs:
            configs.append(f.stem)

    return sorted(configs)


def _resolve_config_path(config_name: str | None) -> Path | None:
    """config名から設定ファイルパスを解決する。"""
    if not config_name:
        return None

    yaml_path = CONFIGS_DIR / f"{config_name}.yaml"
    if yaml_path.exists():
        return yaml_path

    yml_path = CONFIGS_DIR / f"{config_name}.yml"
    if yml_path.exists():
        return yml_path

    return None


def _resolve_hokusai_args(args: list[str]) -> list[str]:
    """hokusai コマンドの引数を解決する。PATHにない場合は uv run へフォールバック。"""
    if args and args[0] == "hokusai":
        if shutil.which("hokusai") is None and shutil.which("uv") is not None:
            return ["uv", "run", "hokusai", *args[1:]]
    return list(args)


def _make_hokusai_env() -> dict[str, str]:
    """hokusai 実行用の環境変数を構築する。"""
    run_env = os.environ.copy()
    run_env.setdefault("UV_CACHE_DIR", "/tmp/uv-cache")
    # ダッシュボード経由は非対話実行。Notion接続失敗時に入力待ちしない。
    run_env.setdefault("HOKUSAI_NONINTERACTIVE_CONTINUE", "1")
    # Claude Code セッション内から起動してもネスト検出に引っかからないようにする
    run_env.pop("CLAUDECODE", None)
    return run_env


def _run_hokusai_command(args: list[str]) -> dict:
    """hokusaiコマンドを同期実行し、結果を辞書で返す。"""
    resolved_args = _resolve_hokusai_args(args)
    run_env = _make_hokusai_env()

    try:
        result = subprocess.run(
            resolved_args,
            capture_output=True,
            text=True,
            timeout=HOKUSAI_COMMAND_TIMEOUT,
            check=False,
            env=run_env,
            stdin=subprocess.DEVNULL,
        )
        return {
            "success": result.returncode == 0,
            "return_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "success": False,
            "return_code": -1,
            "stdout": (e.stdout or "").strip() if hasattr(e, "stdout") else "",
            "stderr": (e.stderr or "").strip() if hasattr(e, "stderr") else "",
            "error": f"タイムアウトしました（{HOKUSAI_COMMAND_TIMEOUT}秒）",
        }
    except FileNotFoundError:
        if args and args[0] == "hokusai":
            fallback_hint = "（または uv run hokusai が利用可能か確認してください）"
        else:
            fallback_hint = ""
        return {
            "success": False,
            "return_code": -1,
            "stdout": "",
            "stderr": "",
            "error": f"hokusai コマンドが見つかりません{fallback_hint}",
        }
    except Exception as e:
        return {
            "success": False,
            "return_code": -1,
            "stdout": "",
            "stderr": "",
            "error": f"実行エラー: {e}",
        }


def _launch_hokusai_background(args: list[str], identifier: str, env_extra: dict[str, str] | None = None) -> dict:
    """hokusaiコマンドをバックグラウンドで起動し、即座に制御を返す。

    Args:
        args: 実行するコマンド引数リスト
        identifier: 二重起動防止用の識別子（start:{wf_id} / continue:{wf_id}）
        env_extra: 追加の環境変数（HOKUSAI_WORKFLOW_ID など）

    Returns:
        {"launched": True, "pid": ..., "log_file": ...} or
        {"launched": False, "error": ...}
    """
    # 二重起動チェック（メモリ + 永続メタ）
    if identifier in _get_running_identifiers():
        return {
            "launched": False,
            "error": "このワークフローは既に実行中です",
        }

    resolved_args = _resolve_hokusai_args(args)
    run_env = _make_hokusai_env()
    if env_extra:
        run_env.update(env_extra)

    _BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = identifier.replace("/", "_").replace(":", "_")[:50]
    log_path = _BG_LOG_DIR / f"{safe_id}_{timestamp}.log"

    try:
        log_fh = open(log_path, "w")
        proc = subprocess.Popen(
            resolved_args,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=run_env,
            start_new_session=True,
        )
    except FileNotFoundError:
        log_fh.close()
        if args and args[0] == "hokusai":
            fallback_hint = "（または uv run hokusai が利用可能か確認してください）"
        else:
            fallback_hint = ""
        return {
            "launched": False,
            "error": f"hokusai コマンドが見つかりません{fallback_hint}",
        }

    with _bg_lock:
        _bg_processes[identifier] = proc

    _save_running_meta(identifier, proc.pid, str(log_path), cmdline=resolved_args)

    def _reaper():
        proc.wait()
        log_fh.close()
        with _bg_lock:
            if _bg_processes.get(identifier) is proc:
                del _bg_processes[identifier]
        _remove_running_meta(identifier)

    t = threading.Thread(target=_reaper, daemon=True)
    t.start()

    return {
        "launched": True,
        "pid": proc.pid,
        "log_file": str(log_path),
    }


def start_workflow_step_mode(task_url: str, config_name: str | None = None) -> dict:
    """`hokusai --step start` をバックグラウンドで起動する。

    workflow_id をダッシュボード側で事前生成し、環境変数経由で CLI に渡す。
    これにより identifier が start:{workflow_id} となり、実行中判定が統一される。
    """
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    cmd = ["hokusai", "--step"]
    if config_name:
        config_path = _resolve_config_path(config_name)
        if not config_path:
            return {
                "success": False,
                "errors": [f"設定ファイルが見つかりません: {config_name}"],
            }
        cmd.extend(["-c", str(config_path)])
    cmd.extend(["start", task_url])

    env_extra = {"HOKUSAI_WORKFLOW_ID": wf_id}
    if config_name:
        env_extra["HOKUSAI_CONFIG_NAME"] = config_name
    bg_result = _launch_hokusai_background(
        cmd, f"start:{wf_id}", env_extra=env_extra,
    )
    if not bg_result["launched"]:
        return {"success": False, "errors": [bg_result["error"]]}

    return {
        "success": True,
        "message": "ワークフローを開始しました（stepモード）",
        "workflow_id": wf_id,
        **bg_result,
    }


def start_workflow_auto_mode(task_url: str, config_name: str | None = None) -> dict:
    """`hokusai start` をバックグラウンドで起動する。

    workflow_id をダッシュボード側で事前生成し、環境変数経由で CLI に渡す。
    """
    wf_id = f"wf-{uuid.uuid4().hex[:8]}"
    cmd = ["hokusai"]
    if config_name:
        config_path = _resolve_config_path(config_name)
        if not config_path:
            return {
                "success": False,
                "errors": [f"設定ファイルが見つかりません: {config_name}"],
            }
        cmd.extend(["-c", str(config_path)])
    cmd.extend(["start", task_url])

    env_extra = {"HOKUSAI_WORKFLOW_ID": wf_id}
    if config_name:
        env_extra["HOKUSAI_CONFIG_NAME"] = config_name
    bg_result = _launch_hokusai_background(
        cmd, f"start:{wf_id}", env_extra=env_extra,
    )
    if not bg_result["launched"]:
        return {"success": False, "errors": [bg_result["error"]]}

    return {
        "success": True,
        "message": "ワークフローを開始しました（自動モード）",
        "workflow_id": wf_id,
        **bg_result,
    }


def continue_workflow_step_mode(workflow_id: str, action: str | None = None) -> dict:
    """`hokusai --step continue` をバックグラウンドで起動する。"""
    # config_name を state から取得
    store = _get_store()
    state = store.load_workflow(workflow_id)
    if not state:
        return {"success": False, "errors": ["ワークフローが見つかりません"]}
    config_name = state.get("config_name")

    # failed フェーズの自動修復: cross_review_blocked で failed になったが
    # waiting_for_human=False（解消済み）の場合、completed に復元して次フェーズへ進める
    # NOTE: Phase 自体のエラー（出力検証失敗等）は修復対象外
    cp = state.get("current_phase", 1)
    phases = state.get("phases", {})
    phase_data = phases.get(cp) or phases.get(str(cp))
    if (phase_data
        and phase_data.get("status") == "failed"
        and phase_data.get("error_message") == "cross_review_blocked"
        and not state.get("waiting_for_human")
    ):
        phase_data["status"] = "completed"
        phase_data["error_message"] = None
        state["current_phase"] = cp + 1
        store.save_workflow(workflow_id, state)

    cmd = ["hokusai", "--step"]
    if config_name:
        config_path = _resolve_config_path(config_name)
        if config_path:
            cmd.extend(["-c", str(config_path)])
    cmd.extend(["continue", workflow_id])
    if action:
        cmd.extend(["--action", action])

    env_extra: dict[str, str] = {}
    if config_name:
        env_extra["HOKUSAI_CONFIG_NAME"] = config_name
    bg_result = _launch_hokusai_background(
        cmd, f"continue:{workflow_id}",
        env_extra=env_extra or None,
    )
    if not bg_result["launched"]:
        return {"success": False, "errors": [bg_result["error"]]}

    return {
        "success": True,
        "message": "次フェーズを実行しました（stepモード）",
        "workflow_id": workflow_id,
        **bg_result,
    }


def _clear_checkpoint(workflow_id: str) -> None:
    """チェックポイントDBから該当ワークフローのチェックポイントを削除する。"""
    try:
        with sqlite3.connect(str(CHECKPOINT_DB_PATH)) as cp_conn:
            cp_conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (workflow_id,))
            cp_conn.execute("DELETE FROM writes WHERE thread_id = ?", (workflow_id,))
    except Exception:
        pass



def handle_pr_review_action(workflow_id: str, pr_index: int | None, action: str) -> dict:
    """PR個別レビューアクションを処理する。

    Args:
        workflow_id: ワークフローID
        pr_index: PR のインデックス（finish_review の場合は None）
        action: "recheck", "mark_complete", "unmark_complete", "finish_review"
    """
    store = _get_store()
    state = store.load_workflow(workflow_id)
    if not state:
        return {"success": False, "errors": ["ワークフローが見つかりません"]}

    if action == "recheck":
        # pr_index のPRをレビュー再確認 → ワークフロー実行
        if pr_index is not None:
            state["current_pr_index"] = pr_index
        state["waiting_for_human"] = False
        state["human_input_request"] = "review_status"
        # auto_fix_attempts をリセット（wait ノードで自動返信されるのを防止）
        state["auto_fix_attempts"] = 0
        store.save_workflow(workflow_id, state)
        # チェックポイントをクリアして _determine_resume_node で正しいノードから再開
        _clear_checkpoint(workflow_id)
        return continue_workflow_step_mode(workflow_id)

    elif action == "mark_complete":
        # pr_index のPRを対応完了マーク（DB直接更新）
        prs = state.get("pull_requests", [])
        if pr_index is None or pr_index < 0 or pr_index >= len(prs):
            return {"success": False, "errors": ["無効なPRインデックスです"]}
        prs[pr_index]["human_review_confirmed"] = True
        prs[pr_index]["status"] = "approved"
        state["pull_requests"] = prs
        store.save_workflow(workflow_id, state)
        return {"success": True, "message": "PRを対応完了にしました"}

    elif action == "unmark_complete":
        # 対応完了を取消（DB直接更新）
        prs = state.get("pull_requests", [])
        if pr_index is None or pr_index < 0 or pr_index >= len(prs):
            return {"success": False, "errors": ["無効なPRインデックスです"]}
        prs[pr_index]["human_review_confirmed"] = False
        prs[pr_index]["status"] = "reviewing"
        state["pull_requests"] = prs
        store.save_workflow(workflow_id, state)
        return {"success": True, "message": "対応完了を取消しました"}

    elif action == "retry_auto_fix":
        # レビュー指摘のrepliedフラグをリセットして自動修正を再試行
        if pr_index is not None:
            state["current_pr_index"] = pr_index
        # 対象PRのコメントのreplied/resolvedフラグをリセット
        prs = state.get("pull_requests", [])
        target_idx = pr_index if pr_index is not None else state.get("current_pr_index", 0)
        if 0 <= target_idx < len(prs):
            pr = prs[target_idx]
            for comment in pr.get("copilot_comments", []):
                comment["replied"] = False
                comment["resolved"] = False
            for comment in pr.get("human_comments", []):
                comment["replied"] = False
                comment["resolved"] = False
            state["pull_requests"] = prs
        # stateのコメントもリセット
        for comment in state.get("copilot_review_comments", []):
            comment["replied"] = False
            comment["resolved"] = False
        for comment in state.get("human_review_comments", []):
            comment["replied"] = False
            comment["resolved"] = False
        # auto_fix_attemptsをリセット
        state["auto_fix_attempts"] = 0
        state["waiting_for_human"] = False
        state["human_input_request"] = "review_status"
        store.save_workflow(workflow_id, state)
        _clear_checkpoint(workflow_id)
        return continue_workflow_step_mode(workflow_id)

    elif action == "finish_review":
        # 全PR完了 → Phase 9 終了処理
        state["human_input_request"] = "complete_review"
        state["waiting_for_human"] = False
        store.save_workflow(workflow_id, state)
        _clear_checkpoint(workflow_id)
        return continue_workflow_step_mode(workflow_id)

    return {"success": False, "errors": [f"不明なアクション: {action}"]}


def continue_workflow_auto_mode(workflow_id: str, action: str | None = None) -> dict:
    """`hokusai continue` をバックグラウンドで起動する。"""
    # config_name を state から取得
    store = _get_store()
    state = store.load_workflow(workflow_id)
    if not state:
        return {"success": False, "errors": ["ワークフローが見つかりません"]}
    config_name = state.get("config_name")

    # failed フェーズの自動修復（step_mode と同様）
    # NOTE: cross_review_blocked 由来の失敗のみ修復対象
    cp = state.get("current_phase", 1)
    phases = state.get("phases", {})
    phase_data = phases.get(cp) or phases.get(str(cp))
    if (phase_data
        and phase_data.get("status") == "failed"
        and phase_data.get("error_message") == "cross_review_blocked"
        and not state.get("waiting_for_human")
    ):
        phase_data["status"] = "completed"
        phase_data["error_message"] = None
        state["current_phase"] = cp + 1
        store.save_workflow(workflow_id, state)

    cmd = ["hokusai"]
    if config_name:
        config_path = _resolve_config_path(config_name)
        if config_path:
            cmd.extend(["-c", str(config_path)])
    cmd.extend(["continue", workflow_id])
    if action:
        cmd.extend(["--action", action])

    env_extra: dict[str, str] = {}
    if config_name:
        env_extra["HOKUSAI_CONFIG_NAME"] = config_name
    bg_result = _launch_hokusai_background(
        cmd, f"continue:{workflow_id}",
        env_extra=env_extra or None,
    )
    if not bg_result["launched"]:
        return {"success": False, "errors": [bg_result["error"]]}

    return {
        "success": True,
        "message": "ワークフローを継続しました（自動モード）",
        "workflow_id": workflow_id,
        **bg_result,
    }


def load_config_yaml(config_name: str) -> dict:
    """指定されたconfig名のYAMLファイルを読み込む

    Args:
        config_name: 設定ファイル名（拡張子なし）

    Returns:
        設定内容の辞書

    Raises:
        FileNotFoundError: ファイルが存在しない場合
        yaml.YAMLError: YAMLパースエラーの場合
    """
    config_path = CONFIGS_DIR / f"{config_name}.yaml"
    if not config_path.exists():
        config_path = CONFIGS_DIR / f"{config_name}.yml"
    if not config_path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {config_name}")

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
        return data if data is not None else {}


def save_config_yaml(config_name: str, data: dict) -> bool:
    """YAMLファイルを保存する

    原子的保存: 一時ファイルに書き込み→rename
    .bakバックアップを自動作成

    Args:
        config_name: 設定ファイル名（拡張子なし）
        data: 保存するデータ

    Returns:
        成功した場合True

    Raises:
        Exception: 保存に失敗した場合
    """
    config_path = CONFIGS_DIR / f"{config_name}.yaml"

    # .ymlファイルが存在する場合はそちらを使用
    if not config_path.exists():
        yml_path = CONFIGS_DIR / f"{config_name}.yml"
        if yml_path.exists():
            config_path = yml_path

    # バックアップ作成
    if config_path.exists():
        backup_path = config_path.with_suffix(config_path.suffix + ".bak")
        shutil.copy2(config_path, backup_path)

    # 原子的保存: 一時ファイルに書き込み→rename
    fd, temp_path = tempfile.mkstemp(
        suffix=".yaml",
        prefix=f"{config_name}_",
        dir=CONFIGS_DIR
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(
                data,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        # 一時ファイルを本来のパスに移動
        shutil.move(temp_path, config_path)
        return True
    except Exception:
        # エラー時は一時ファイルを削除
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def validate_config(data: dict) -> tuple[bool, list[str], list[str]]:
    """設定値を検証する

    検証項目:
    - 必須フィールド: project_root, base_branch
    - 型チェック: max_retry_count, skill_timeout等は数値
    - パス存在チェック: project_root, repositories[].path

    Args:
        data: 検証する設定データ

    Returns:
        (is_valid, errors): 検証結果とエラーメッセージのリスト
    """
    errors = []

    # 必須フィールドチェック
    required_fields = ["project_root", "base_branch"]
    for field in required_fields:
        if field not in data or data[field] is None or data[field] == "":
            errors.append(f"必須フィールドが未設定です: {field}")

    # 数値型チェック
    numeric_fields = [
        "max_retry_count",
        "retry_delay_seconds",
        "skill_timeout",
        "command_timeout",
        "max_concurrent_tasks",
    ]
    for field in numeric_fields:
        if field in data and data[field] is not None:
            value = data[field]
            if not isinstance(value, (int, float)):
                errors.append(f"{field} は数値である必要があります")
            elif isinstance(value, (int, float)) and value < 0:
                errors.append(f"{field} は0以上である必要があります")

    # project_root パス存在チェック
    if "project_root" in data and data["project_root"]:
        project_root = Path(data["project_root"]).expanduser()
        if not project_root.exists():
            errors.append(f"project_root のパスが存在しません: {data['project_root']}")
        elif not project_root.is_dir():
            errors.append(f"project_root はディレクトリである必要があります: {data['project_root']}")

    # repositories[].path 存在チェック
    repositories = data.get("repositories", [])
    if isinstance(repositories, list):
        for i, repo in enumerate(repositories):
            if isinstance(repo, dict):
                repo_path = repo.get("path")
                if repo_path:
                    path = Path(repo_path).expanduser()
                    if not path.exists():
                        errors.append(f"repositories[{i}].path のパスが存在しません: {repo_path}")
                    elif not path.is_dir():
                        errors.append(f"repositories[{i}].path はディレクトリである必要があります: {repo_path}")

    # cross_review 検証
    cross_review = data.get("cross_review")
    if isinstance(cross_review, dict):
        cr_model = cross_review.get("model")
        valid_models = ("codex-mini-latest", "claude-code", "gemini-cli", "gpt-5.4")
        if cr_model is not None and cr_model not in valid_models:
            errors.append(f"cross_review.model は {' / '.join(valid_models)} のいずれかです")

        cr_timeout = cross_review.get("timeout")
        if cr_timeout is not None:
            if not isinstance(cr_timeout, (int, float)) or cr_timeout < 0:
                errors.append("cross_review.timeout は0以上の数値である必要があります")

        cr_on_failure = cross_review.get("on_failure")
        if cr_on_failure is not None and cr_on_failure not in ("warn", "block", "skip"):
            errors.append("cross_review.on_failure は warn / block / skip のいずれかです")

        cr_max_rounds = cross_review.get("max_correction_rounds")
        if cr_max_rounds is not None:
            if not isinstance(cr_max_rounds, int) or cr_max_rounds < 1:
                errors.append("cross_review.max_correction_rounds は1以上の整数である必要があります")

        cr_phases = cross_review.get("phases")
        if cr_phases is not None:
            if not isinstance(cr_phases, list):
                errors.append("cross_review.phases はリストである必要があります")
            elif any(not isinstance(p, int) or p < 1 or p > 9 for p in cr_phases):
                errors.append("cross_review.phases は1〜9の整数リストである必要があります")

    # コマンドフィールドの静的検証
    cmd_result = validate_command_fields(data)
    errors.extend(cmd_result.get("errors", []))
    cmd_warnings = cmd_result.get("warnings", [])

    is_valid = len(errors) == 0
    return is_valid, errors, cmd_warnings


def validate_command_fields(data: dict) -> dict:
    """build/test/lint_command フィールドの静的検証。

    Returns:
        {"errors": [...], "warnings": [...]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # トップレベルコマンド
    for field in ("build_command", "test_command", "lint_command"):
        cmd = data.get(field)
        if cmd and isinstance(cmd, str):
            _check_command_string(cmd, field, errors, warnings)

    # リポジトリ単位コマンド
    for i, repo in enumerate(data.get("repositories", []) or []):
        if not isinstance(repo, dict):
            continue
        repo_name = repo.get("name", f"[{i}]")
        for field in ("build_command", "test_command", "lint_command"):
            cmd = repo.get(field)
            if cmd and isinstance(cmd, str):
                _check_command_string(cmd, f"repositories[{repo_name}].{field}", errors, warnings)

    return {"errors": errors, "warnings": warnings}


def _check_command_string(cmd: str, label: str, errors: list, warnings: list) -> None:
    """個別コマンド文字列の静的検査。"""
    # Level 1: 構文エラー検出（hard error）
    single_quotes = cmd.count("'") - cmd.count("\\'")
    if single_quotes % 2 != 0:
        errors.append(f"{label}: シングルクオートの数が不整合です（閉じ忘れの可能性）")

    if "bash -c" in cmd:
        # bash -c '...' の閉じ確認
        after_bash_c = cmd.split("bash -c", 1)[1].strip()
        if after_bash_c.startswith("'") and not after_bash_c.rstrip().endswith("'"):
            errors.append(f"{label}: bash -c のクオートが閉じられていません")

    # if/fi の対応
    if_count = len(re.findall(r"\bif\b", cmd))
    fi_count = len(re.findall(r"\bfi\b", cmd))
    if if_count > 0 and if_count != fi_count:
        errors.append(f"{label}: if ({if_count}個) と fi ({fi_count}個) の数が一致しません")

    # for/done の対応
    for_count = len(re.findall(r"\bfor\b", cmd))
    while_count = len(re.findall(r"\bwhile\b", cmd))
    done_count = len(re.findall(r"\bdone\b", cmd))
    loop_count = for_count + while_count
    if loop_count > 0 and loop_count != done_count:
        errors.append(f"{label}: for/while ({loop_count}個) と done ({done_count}個) の数が一致しません")

    # Level 3: 危険 regex 警告
    if r"[\s\S]*" in cmd:
        warnings.append(f"{label}: [\\s\\S]* は貪欲すぎる可能性があります（境界パターンの確認を推奨）")


def parse_project_rules(config_name: str) -> list[dict]:
    """プロジェクト固有ルールをパース

    Args:
        config_name: 設定ファイル名（拡張子なし）

    Returns:
        [
            {"id": "P01", "name": "ルール名", "description": "説明"},
            ...
        ]
    """
    import yaml

    config_path = CONFIGS_DIR / f"{config_name}.yaml"
    if not config_path.exists():
        config_path = CONFIGS_DIR / f"{config_name}.yml"
    if not config_path.exists():
        return []

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    checklist = config.get("review_checklist", [])
    rules = []

    if isinstance(checklist, list):
        # 旧形式: リスト
        for i, item in enumerate(checklist):
            rules.append({
                "id": f"P{i+1:02d}",
                "name": item[:50] + ("..." if len(item) > 50 else ""),
                "description": item,
            })
    elif isinstance(checklist, dict):
        # 新形式: 辞書
        for rule_id, rule_data in checklist.items():
            if isinstance(rule_data, dict):
                rules.append({
                    "id": rule_id,
                    "name": rule_data.get("name", rule_id),
                    "description": rule_data.get("description", rule_data.get("name", "")),
                })
            elif isinstance(rule_data, str):
                rules.append({
                    "id": rule_id,
                    "name": rule_data[:50] + ("..." if len(rule_data) > 50 else ""),
                    "description": rule_data,
                })

    return rules


def render_rulebook_page(selected_config: str | None = None) -> str:
    """ルールブックページをHTMLにレンダリング

    Args:
        selected_config: 選択された設定ファイル名（拡張子なし）
    """
    builtin_rules = parse_builtin_rules()
    config_files = list_config_files()
    project_rules = parse_project_rules(selected_config) if selected_config else []

    # カテゴリごとにグループ化
    categories = {}
    for rule in builtin_rules:
        cat = rule["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(rule)

    # 組み込みルールのHTML
    builtin_html = ""
    for category, rules in categories.items():
        rules_html = ""
        for rule in rules:
            items_html = "".join(f"<li>{item}</li>" for item in rule["items"])
            rules_html += f"""
            <div class="rule-item">
                <h4 class="rule-title">
                    <code>{rule['id']}</code> {rule['name']}
                </h4>
                <ul class="rule-checklist">
                    {items_html}
                </ul>
            </div>
            """

        builtin_html += f"""
        <div class="card">
            <h3>{category}</h3>
            {rules_html}
        </div>
        """

    # サマリー
    total_builtin = len(builtin_rules)
    total_builtin_items = sum(len(r["items"]) for r in builtin_rules)
    total_project = len(project_rules)

    # コンボボックスのオプション
    options_html = '<option value="">-- 選択してください --</option>'
    for cfg in config_files:
        selected = 'selected' if cfg == selected_config else ''
        options_html += f'<option value="{cfg}" {selected}>{cfg}</option>'

    # プロジェクト固有ルールのHTML
    project_html = ""
    if project_rules:
        project_rules_html = ""
        for rule in project_rules:
            project_rules_html += f"""
            <div class="rule-item project-rule">
                <h4 class="rule-title">
                    <code>{rule['id']}</code> {rule['name']}
                </h4>
                <p class="rule-description">{rule['description']}</p>
            </div>
            """
        project_html = f"""
        <div class="card project-rules-card">
            <h3>プロジェクト固有ルール（{selected_config}）- {total_project}件</h3>
            {project_rules_html}
        </div>
        """
    elif selected_config:
        project_html = f"""
        <div class="card">
            <p class="text-muted">{selected_config} にはプロジェクト固有ルールが定義されていません。</p>
        </div>
        """

    return f"""
    <a href="/" class="back-link">&larr; ダッシュボードに戻る</a>

    <h1 class="page-title">レビュールールブック</h1>

    <div class="card">
        <h3>概要</h3>
        <p>Phase 7（最終レビュー）で適用されるルール一覧です。</p>
        <table class="info-table">
            <tr><td>組み込みルール</td><td>{total_builtin} ルール（{total_builtin_items} 項目）</td></tr>
            <tr><td>プロジェクト固有ルール</td><td>{total_project} ルール</td></tr>
            <tr><td>合計</td><td><strong>{total_builtin + total_project} ルール</strong></td></tr>
        </table>
    </div>

    <div class="card">
        <h3>プロジェクト設定を選択</h3>
        <select id="configSelect" onchange="location.href='/rulebook?config=' + this.value" class="select-input">
            {options_html}
        </select>
    </div>

    {project_html}

    <h2 class="section-title">組み込みルール（{total_builtin}件）</h2>
    {builtin_html}

    <div class="card">
        <h3>カスタムルールの追加方法</h3>
        <p>設定ファイル（configs/*.yaml）に以下を追加：</p>
        <pre class="code-block"><code>review_checklist:
  - "プロジェクト固有のルール1"
  - "プロジェクト固有のルール2"

# または詳細形式
review_checklist:
  P01:
    name: "ルール名"
    description: "詳細な説明"</code></pre>
    </div>
    """


def render_prompts_page() -> str:
    """LLM指示文管理ページをHTMLにレンダリング"""
    return """\
    <a href="/" class="back-link">&larr; ダッシュボードに戻る</a>
    <h1 class="page-title">LLM指示文</h1>
    <div style="display:flex;gap:20px;min-height:600px;">
        <div id="promptList" style="width:280px;flex-shrink:0;">
            <div class="card" style="padding:0;">
                <div style="padding:12px 16px;border-bottom:1px solid var(--border-color);">
                    <h3 style="margin:0;font-size:13px;text-transform:uppercase;letter-spacing:1px;color:var(--text-muted);">プロンプト一覧</h3>
                </div>
                <div id="promptListItems" style="max-height:550px;overflow-y:auto;"></div>
            </div>
        </div>
        <div id="promptEditor" style="flex:1;min-width:0;">
            <div class="card">
                <div id="promptMeta" style="margin-bottom:16px;">
                    <p style="color:var(--text-muted);font-size:14px;">左の一覧からプロンプトを選択してください。</p>
                </div>
                <div id="promptEditArea" style="display:none;">
                    <textarea id="promptContent" class="text-input" style="width:100%;height:400px;font-family:monospace;font-size:13px;line-height:1.6;resize:vertical;box-sizing:border-box;"></textarea>
                    <div style="margin-top:12px;display:flex;gap:8px;align-items:center;">
                        <button class="save-btn" onclick="savePrompt()">保存</button>
                        <button class="reset-btn" onclick="resetPrompt()">リセット</button>
                        <span id="promptSaveResult" style="font-size:13px;"></span>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script>
    let _prompts = [];
    let _selectedId = null;
    let _originalContent = '';

    async function loadPromptList() {
        const res = await fetch('/api/prompts');
        const data = await res.json();
        if (!data.success) return;
        _prompts = data.data;
        const container = document.getElementById('promptListItems');
        let html = '';
        let currentPhase = '';
        for (const p of _prompts) {
            const phase = p.id.split('.')[0];
            if (phase !== currentPhase) {
                currentPhase = phase;
                const label = phase.replace('_', ' ').replace(/\\b\\w/g, c => c.toUpperCase());
                html += '<div style="padding:8px 16px 4px;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--text-muted);background:var(--bg-secondary);">' + label + '</div>';
            }
            html += '<div class="prompt-item" data-id="' + p.id + '" onclick="selectPrompt(\\'' + p.id + '\\')" style="padding:8px 16px;cursor:pointer;border-bottom:1px solid var(--border-color);font-size:13px;">';
            html += '<div style="font-weight:500;">' + p.title + '</div>';
            html += '<div style="font-size:11px;color:var(--text-muted);margin-top:2px;">' + p.id + '</div>';
            html += '</div>';
        }
        container.innerHTML = html;
    }

    async function selectPrompt(id) {
        _selectedId = id;
        document.querySelectorAll('.prompt-item').forEach(el => {
            el.style.background = el.dataset.id === id ? 'var(--bg-secondary)' : '';
        });
        const entry = _prompts.find(p => p.id === id);
        if (!entry) return;
        const res = await fetch('/api/prompts/' + encodeURIComponent(id));
        const data = await res.json();
        if (!data.success) return;
        _originalContent = data.data.content;
        const mtime = entry.mtime ? new Date(entry.mtime * 1000).toLocaleString('ja-JP') : '-';
        const vars = (entry.variables || []).length > 0 ? entry.variables.map(v => '{' + v + '}').join(', ') : 'なし';
        document.getElementById('promptMeta').innerHTML =
            '<div style="margin-bottom:12px;">' +
            '<h3 style="margin:0 0 8px;">' + entry.title + '</h3>' +
            '<div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--text-muted);">' +
            '<span>ID: <code>' + entry.id + '</code></span>' +
            '<span>Kind: <code>' + (entry.kind || '-') + '</code></span>' +
            '<span>Variables: <code>' + vars + '</code></span>' +
            '<span>更新: ' + mtime + '</span>' +
            '</div></div>';
        document.getElementById('promptContent').value = data.data.content;
        document.getElementById('promptEditArea').style.display = '';
        document.getElementById('promptSaveResult').textContent = '';
    }

    async function savePrompt() {
        if (!_selectedId) return;
        const content = document.getElementById('promptContent').value;
        const res = await fetch('/api/prompts/' + encodeURIComponent(_selectedId), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content: content})
        });
        const data = await res.json();
        const el = document.getElementById('promptSaveResult');
        if (data.success) {
            el.style.color = 'var(--status-completed)';
            el.textContent = '\\u2705 保存しました';
            _originalContent = content;
            loadPromptList();
        } else {
            el.style.color = 'var(--status-failed)';
            el.textContent = '\\u274c ' + (data.errors || []).join(', ');
        }
    }

    function resetPrompt() {
        if (_originalContent !== undefined) {
            document.getElementById('promptContent').value = _originalContent;
            document.getElementById('promptSaveResult').textContent = '';
        }
    }

    loadPromptList();
    </script>
    """


def render_settings_page(config_files: list) -> str:
    """設定ページをHTMLにレンダリング

    Args:
        config_files: 設定ファイル一覧
    """
    # コンボボックスのオプション
    options_html = '<option value="">-- 選択してください --</option>'
    for cfg in config_files:
        options_html += f'<option value="{cfg}">{cfg}</option>'

    return f"""
    <a href="/" class="back-link">&larr; ダッシュボードに戻る</a>

    <h1 class="page-title">設定</h1>

    <!-- ダッシュボード設定 (Phase 3C) -->
    <div class="card">
        <h3>ダッシュボード設定</h3>
        <p>ブラウザのLocalStorageに保存されます。</p>

        <div class="settings-form">
            <div class="setting-item">
                <label for="theme">テーマ</label>
                <select id="theme" class="select-input" onchange="saveDashboardSettings()">
                    <option value="light" selected>ライト</option>
                    <option value="dark">ダーク</option>
                    <option value="system">システム連動</option>
                </select>
            </div>

            <div class="setting-item">
                <label for="listLimit">一覧表示件数</label>
                <select id="listLimit" class="select-input" onchange="saveDashboardSettings()">
                    <option value="0" selected>全件</option>
                    <option value="10">10件</option>
                    <option value="25">25件</option>
                    <option value="50">50件</option>
                    <option value="100">100件</option>
                </select>
            </div>
        </div>

        <div class="settings-actions">
            <button class="reset-btn" onclick="resetDashboardSettings()">設定をリセット</button>
        </div>
    </div>

    <!-- プロジェクト設定 (Phase 3A + 3B) -->
    <div class="card">
        <h3>プロジェクト設定</h3>
        <p>設定ファイル（configs/*.yaml）を編集できます。変更は「新規開始ワークフロー」に適用されます。</p>

        <div class="setting-item">
            <label for="projectConfigSelect">設定ファイルを選択</label>
            <select id="projectConfigSelect" class="select-input" onchange="loadProjectConfig(this.value)">
                {options_html}
            </select>
        </div>

        <!-- モード切り替えタブ -->
        <div id="configEditorTabs" class="editor-tabs" style="display: none;">
            <button type="button" class="tab-btn active" onclick="switchEditorMode('form')">フォームモード</button>
            <button type="button" class="tab-btn" onclick="switchEditorMode('yaml')">YAMLモード</button>
        </div>

        <!-- フォームモード (Phase 3A) -->
        <div id="formEditor" class="editor-panel" style="display: none;">
            <form id="projectConfigForm" onsubmit="saveProjectConfig(event)">
                <!-- 基本設定 -->
                <fieldset class="config-fieldset">
                    <legend>基本設定</legend>
                    <div class="setting-item">
                        <label for="cfg_project_root">プロジェクトルート</label>
                        <input type="text" id="cfg_project_root" name="project_root" class="text-input" placeholder="/path/to/project">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_base_branch">ベースブランチ</label>
                        <input type="text" id="cfg_base_branch" name="base_branch" class="text-input" placeholder="main">
                    </div>
                </fieldset>

                <!-- コマンド設定 -->
                <fieldset class="config-fieldset">
                    <legend>検証コマンド</legend>
                    <div class="setting-item">
                        <label for="cfg_build_command">ビルドコマンド</label>
                        <input type="text" id="cfg_build_command" name="build_command" class="text-input" placeholder="npm run build">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_test_command">テストコマンド</label>
                        <input type="text" id="cfg_test_command" name="test_command" class="text-input" placeholder="npm test">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_lint_command">Lintコマンド</label>
                        <input type="text" id="cfg_lint_command" name="lint_command" class="text-input" placeholder="npm run lint">
                    </div>
                </fieldset>

                <!-- タイムアウト・リトライ設定 -->
                <fieldset class="config-fieldset">
                    <legend>タイムアウト・リトライ</legend>
                    <div class="setting-item">
                        <label for="cfg_max_retry_count">最大リトライ回数</label>
                        <input type="number" id="cfg_max_retry_count" name="max_retry_count" class="text-input" min="0" max="10" placeholder="3">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_retry_delay_seconds">リトライ遅延 (秒)</label>
                        <input type="number" id="cfg_retry_delay_seconds" name="retry_delay_seconds" class="text-input" min="0" placeholder="5">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_skill_timeout">スキルタイムアウト (秒)</label>
                        <input type="number" id="cfg_skill_timeout" name="skill_timeout" class="text-input" min="0" placeholder="600">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_command_timeout">コマンドタイムアウト (秒)</label>
                        <input type="number" id="cfg_command_timeout" name="command_timeout" class="text-input" min="0" placeholder="300">
                    </div>
                </fieldset>

                <!-- タスクバックエンド設定 -->
                <fieldset class="config-fieldset">
                    <legend>タスクバックエンド</legend>
                    <div class="setting-item">
                        <label for="cfg_task_backend_type">タイプ</label>
                        <select id="cfg_task_backend_type" name="task_backend.type" class="select-input">
                            <option value="">-- 選択 --</option>
                            <option value="github_issue">GitHub Issue</option>
                            <option value="jira">Jira</option>
                            <option value="linear">Linear</option>
                            <option value="notion">Notion</option>
                        </select>
                    </div>
                    <div class="setting-item">
                        <label for="cfg_task_backend_base_url">ベースURL</label>
                        <input type="text" id="cfg_task_backend_base_url" name="task_backend.base_url" class="text-input" placeholder="https://...">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_task_backend_project_key">プロジェクトキー</label>
                        <input type="text" id="cfg_task_backend_project_key" name="task_backend.project_key" class="text-input" placeholder="PROJECT">
                    </div>
                </fieldset>

                <!-- Gitホスティング設定 -->
                <fieldset class="config-fieldset">
                    <legend>Gitホスティング</legend>
                    <div class="setting-item">
                        <label for="cfg_git_hosting_type">タイプ</label>
                        <select id="cfg_git_hosting_type" name="git_hosting.type" class="select-input">
                            <option value="">-- 選択 --</option>
                            <option value="github">GitHub</option>
                            <option value="gitlab">GitLab</option>
                            <option value="bitbucket">Bitbucket</option>
                        </select>
                    </div>
                    <div class="setting-item">
                        <label for="cfg_git_hosting_base_url">ベースURL</label>
                        <input type="text" id="cfg_git_hosting_base_url" name="git_hosting.base_url" class="text-input" placeholder="https://github.com">
                    </div>
                </fieldset>

                <!-- クロスLLMレビュー設定 -->
                <fieldset class="config-fieldset">
                    <legend>クロスLLMレビュー</legend>
                    <p class="text-muted">Phase 2/3/4 の出力を別のLLMで自動レビューします。</p>
                    <div class="setting-item">
                        <label class="checkbox-label">
                            <input type="checkbox" id="cfg_cross_review_enabled">
                            レビューを有効にする
                        </label>
                    </div>
                    <div class="setting-item">
                        <label for="cfg_cross_review_model">レビュアーモデル</label>
                        <select id="cfg_cross_review_model" name="cross_review.model" class="select-input">
                            <option value="codex-mini-latest">OpenAI Codex</option>
                            <option value="gpt-5.4">OpenAI GPT-5.4</option>
                            <option value="claude-code">Claude Code</option>
                            <option value="gemini-cli">Gemini CLI</option>
                        </select>
                    </div>
                    <div class="setting-item">
                        <label>対象 Phase</label>
                        <div class="checkbox-group">
                            <label class="checkbox-label"><input type="checkbox" class="cfg_cross_review_phase" value="2" checked> Phase 2（事前調査）</label>
                            <label class="checkbox-label"><input type="checkbox" class="cfg_cross_review_phase" value="3"> Phase 3（設計）</label>
                            <label class="checkbox-label"><input type="checkbox" class="cfg_cross_review_phase" value="4" checked> Phase 4（作業計画）</label>
                        </div>
                    </div>
                    <div class="setting-item">
                        <label for="cfg_cross_review_timeout">タイムアウト (秒)</label>
                        <input type="number" id="cfg_cross_review_timeout" name="cross_review.timeout" class="text-input" min="30" placeholder="300">
                    </div>
                    <div class="setting-item">
                        <label for="cfg_cross_review_on_failure">失敗時の動作</label>
                        <select id="cfg_cross_review_on_failure" name="cross_review.on_failure" class="select-input">
                            <option value="warn">warn — 警告のみで続行</option>
                            <option value="block">block — 停止して人間に確認を求める</option>
                            <option value="skip">skip — スキップして続行</option>
                        </select>
                    </div>
                    <div class="setting-item">
                        <label for="cfg_cross_review_max_rounds">最大修正ラウンド数</label>
                        <input type="number" id="cfg_cross_review_max_rounds" name="cross_review.max_correction_rounds" class="text-input" min="1" max="5" placeholder="2">
                    </div>
                </fieldset>

                <!-- リポジトリ設定 -->
                <fieldset class="config-fieldset">
                    <legend>リポジトリ配列</legend>
                    <p class="text-muted">name/path/base_branch/default_target とコマンド上書きを編集できます。</p>
                    <div id="repositoriesContainer" class="repos-container"></div>
                    <div class="form-actions">
                        <button type="button" class="reset-btn" onclick="addRepositoryRow()">リポジトリを追加</button>
                    </div>
                </fieldset>

                <div class="form-actions">
                    <button type="submit" class="save-btn">保存</button>
                    <button type="button" class="reset-btn" onclick="loadProjectConfig(document.getElementById('projectConfigSelect').value)">リセット</button>
                    <button type="button" class="reset-btn" onclick="validateConfigOnly()">検証のみ</button>
                    <button type="button" class="save-btn" onclick="saveAndRetryPhase6()" id="btnSaveAndRetryPhase6" style="display:none;">保存して Phase 6 を再実行</button>
                </div>
            </form>
        </div>

        <!-- YAMLモード (Phase 3B) -->
        <div id="yamlEditor" class="editor-panel" style="display: none;">
            <div id="yamlError" class="yaml-error" style="display: none;"></div>
            <textarea id="yamlTextarea" class="yaml-textarea" rows="30" placeholder="YAML形式で編集..."></textarea>
            <div class="form-actions">
                <button type="button" class="save-btn" onclick="saveYamlConfig()">保存</button>
                <button type="button" class="reset-btn" onclick="loadProjectConfig(document.getElementById('projectConfigSelect').value)">リセット</button>
                <button type="button" class="reset-btn" onclick="validateYaml()">構文チェック</button>
                <button type="button" class="reset-btn" onclick="validateConfigOnly()">検証のみ</button>
                <button type="button" class="save-btn" onclick="saveAndRetryPhase6()" id="btnSaveAndRetryPhase6Yaml" style="display:none;">保存して Phase 6 を再実行</button>
            </div>
        </div>

        <!-- ステータスメッセージ -->
        <div id="configStatus" class="config-status" style="display: none;"></div>

        <p style="margin-top: 16px;">
            <a href="/rulebook">レビュールールブックを表示 &rarr;</a>
        </p>
    </div>

    <script>
        // プロジェクト設定関連のJavaScript
        let currentConfigName = '';
        let currentConfigData = {{}};
        let currentEditorMode = 'form';

        // プロジェクト設定を読み込み
        async function loadProjectConfig(configName) {{
            if (!configName) {{
                document.getElementById('configEditorTabs').style.display = 'none';
                document.getElementById('formEditor').style.display = 'none';
                document.getElementById('yamlEditor').style.display = 'none';
                return;
            }}

            try {{
                const response = await fetch(`/api/config?name=${{encodeURIComponent(configName)}}`);
                const result = await response.json();

                if (result.success) {{
                    currentConfigName = configName;
                    currentConfigData = result.data;

                    // エディタを表示
                    document.getElementById('configEditorTabs').style.display = 'flex';
                    switchEditorMode(currentEditorMode);

                    // フォームに値をセット
                    populateForm(result.data);

                    // YAMLテキストエリアに値をセット
                    document.getElementById('yamlTextarea').value = jsyaml.dump(result.data);

                    showConfigStatus('設定を読み込みました', 'success');
                }} else {{
                    showConfigStatus('読み込みに失敗しました: ' + result.errors.join(', '), 'error');
                }}
            }} catch (e) {{
                showConfigStatus('読み込みエラー: ' + e.message, 'error');
            }}
        }}

        // フォームに値をセット
        function populateForm(data) {{
            // 基本設定
            setValue('cfg_project_root', data.project_root || '');
            setValue('cfg_base_branch', data.base_branch || '');

            // コマンド設定
            setValue('cfg_build_command', data.build_command || '');
            setValue('cfg_test_command', data.test_command || '');
            setValue('cfg_lint_command', data.lint_command || '');

            // タイムアウト・リトライ
            setValue('cfg_max_retry_count', data.max_retry_count || '');
            setValue('cfg_retry_delay_seconds', data.retry_delay_seconds || '');
            setValue('cfg_skill_timeout', data.skill_timeout || '');
            setValue('cfg_command_timeout', data.command_timeout || '');

            // タスクバックエンド
            const taskBackend = data.task_backend || {{}};
            setValue('cfg_task_backend_type', taskBackend.type || '');
            setValue('cfg_task_backend_base_url', taskBackend.base_url || '');
            setValue('cfg_task_backend_project_key', taskBackend.project_key || '');

            // Gitホスティング
            const gitHosting = data.git_hosting || {{}};
            setValue('cfg_git_hosting_type', gitHosting.type || '');
            setValue('cfg_git_hosting_base_url', gitHosting.base_url || '');

            // クロスLLMレビュー
            const crossReview = data.cross_review || {{}};
            document.getElementById('cfg_cross_review_enabled').checked = !!crossReview.enabled;
            setValue('cfg_cross_review_model', crossReview.model || 'codex-mini-latest');
            setValue('cfg_cross_review_timeout', crossReview.timeout || 300);
            setValue('cfg_cross_review_on_failure', crossReview.on_failure || 'warn');
            setValue('cfg_cross_review_max_rounds', crossReview.max_correction_rounds || 2);
            // Phase チェックボックス
            const crPhases = Array.isArray(crossReview.phases) ? crossReview.phases : [2, 4];
            document.querySelectorAll('.cfg_cross_review_phase').forEach(cb => {{
                cb.checked = crPhases.includes(parseInt(cb.value));
            }});

            // リポジトリ配列
            renderRepositoriesEditor(Array.isArray(data.repositories) ? data.repositories : []);
        }}

        function setValue(id, value) {{
            const el = document.getElementById(id);
            if (el) el.value = value;
        }}

        // フォームからデータを取得
        function getFormData() {{
            const data = {{ ...currentConfigData }};

            // 基本設定
            data.project_root = document.getElementById('cfg_project_root').value || undefined;
            data.base_branch = document.getElementById('cfg_base_branch').value || undefined;

            // コマンド設定
            const buildCmd = document.getElementById('cfg_build_command').value;
            const testCmd = document.getElementById('cfg_test_command').value;
            const lintCmd = document.getElementById('cfg_lint_command').value;
            if (buildCmd) data.build_command = buildCmd;
            if (testCmd) data.test_command = testCmd;
            if (lintCmd) data.lint_command = lintCmd;

            // タイムアウト・リトライ
            const maxRetry = document.getElementById('cfg_max_retry_count').value;
            const retryDelay = document.getElementById('cfg_retry_delay_seconds').value;
            const skillTimeout = document.getElementById('cfg_skill_timeout').value;
            const commandTimeout = document.getElementById('cfg_command_timeout').value;
            if (maxRetry) data.max_retry_count = parseInt(maxRetry);
            if (retryDelay) data.retry_delay_seconds = parseInt(retryDelay);
            if (skillTimeout) data.skill_timeout = parseInt(skillTimeout);
            if (commandTimeout) data.command_timeout = parseInt(commandTimeout);

            // タスクバックエンド
            const tbType = document.getElementById('cfg_task_backend_type').value;
            const tbBaseUrl = document.getElementById('cfg_task_backend_base_url').value;
            const tbProjectKey = document.getElementById('cfg_task_backend_project_key').value;
            if (tbType || tbBaseUrl || tbProjectKey) {{
                data.task_backend = data.task_backend || {{}};
                if (tbType) data.task_backend.type = tbType;
                if (tbBaseUrl) data.task_backend.base_url = tbBaseUrl;
                if (tbProjectKey) data.task_backend.project_key = tbProjectKey;
            }}

            // Gitホスティング
            const ghType = document.getElementById('cfg_git_hosting_type').value;
            const ghBaseUrl = document.getElementById('cfg_git_hosting_base_url').value;
            if (ghType || ghBaseUrl) {{
                data.git_hosting = data.git_hosting || {{}};
                if (ghType) data.git_hosting.type = ghType;
                if (ghBaseUrl) data.git_hosting.base_url = ghBaseUrl;
            }}

            // クロスLLMレビュー
            const crEnabled = document.getElementById('cfg_cross_review_enabled').checked;
            const crModel = document.getElementById('cfg_cross_review_model').value;
            const crTimeout = document.getElementById('cfg_cross_review_timeout').value;
            const crOnFailure = document.getElementById('cfg_cross_review_on_failure').value;
            const crMaxRounds = document.getElementById('cfg_cross_review_max_rounds').value;
            const crPhases = [];
            document.querySelectorAll('.cfg_cross_review_phase:checked').forEach(cb => {{
                crPhases.push(parseInt(cb.value));
            }});
            data.cross_review = {{
                enabled: crEnabled,
                model: crModel || 'codex-mini-latest',
                phases: crPhases.length > 0 ? crPhases : [2, 4],
                timeout: crTimeout ? parseInt(crTimeout) : 300,
                on_failure: crOnFailure || 'warn',
                max_correction_rounds: crMaxRounds ? parseInt(crMaxRounds) : 2,
            }};

            // リポジトリ配列
            data.repositories = collectRepositoriesData();

            return data;
        }}

        function renderRepositoriesEditor(repositories) {{
            const container = document.getElementById('repositoriesContainer');
            if (!container) return;
            container.innerHTML = '';

            if (!repositories || repositories.length === 0) {{
                addRepositoryRow();
                return;
            }}

            repositories.forEach(repo => addRepositoryRow(repo));
        }}

        function addRepositoryRow(repo = {{}}) {{
            const container = document.getElementById('repositoriesContainer');
            if (!container) return;

            const row = document.createElement('div');
            row.className = 'repo-row';
            row.innerHTML = `
                <div class="repo-row-grid">
                    <input type="text" class="text-input repo-name" placeholder="name (Backend)" value="${{repo.name || ''}}">
                    <input type="text" class="text-input repo-path" placeholder="path (/path/to/repo)" value="${{repo.path || ''}}">
                    <input type="text" class="text-input repo-base-branch" placeholder="base_branch (main)" value="${{repo.base_branch || ''}}">
                    <label class="repo-checkbox">
                        <input type="checkbox" class="repo-default-target" ${{repo.default_target ? 'checked' : ''}}>
                        default_target
                    </label>
                    <input type="text" class="text-input repo-build-command" placeholder="build_command (optional)" value="${{repo.build_command || ''}}">
                    <input type="text" class="text-input repo-test-command" placeholder="test_command (optional)" value="${{repo.test_command || ''}}">
                    <input type="text" class="text-input repo-lint-command" placeholder="lint_command (optional)" value="${{repo.lint_command || ''}}">
                </div>
                <div class="form-actions">
                    <button type="button" class="reset-btn repo-remove-btn">削除</button>
                </div>
            `;

            row.querySelector('.repo-remove-btn').addEventListener('click', () => {{
                row.remove();
                const rows = container.querySelectorAll('.repo-row');
                if (rows.length === 0) {{
                    addRepositoryRow();
                }}
            }});

            container.appendChild(row);
        }}

        function collectRepositoriesData() {{
            const rows = document.querySelectorAll('#repositoriesContainer .repo-row');
            const repositories = [];

            rows.forEach(row => {{
                const name = row.querySelector('.repo-name')?.value.trim() || '';
                const path = row.querySelector('.repo-path')?.value.trim() || '';
                const baseBranch = row.querySelector('.repo-base-branch')?.value.trim() || '';
                const defaultTarget = !!row.querySelector('.repo-default-target')?.checked;
                const buildCommand = row.querySelector('.repo-build-command')?.value.trim() || '';
                const testCommand = row.querySelector('.repo-test-command')?.value.trim() || '';
                const lintCommand = row.querySelector('.repo-lint-command')?.value.trim() || '';

                // すべて空なら無視
                if (!name && !path && !baseBranch && !buildCommand && !testCommand && !lintCommand) {{
                    return;
                }}

                const repo = {{
                    name,
                    path,
                    base_branch: baseBranch,
                    default_target: defaultTarget,
                }};
                if (buildCommand) repo.build_command = buildCommand;
                if (testCommand) repo.test_command = testCommand;
                if (lintCommand) repo.lint_command = lintCommand;
                repositories.push(repo);
            }});

            return repositories;
        }}

        // プロジェクト設定を保存 (フォームモード)
        async function saveProjectConfig(event) {{
            event.preventDefault();

            if (!currentConfigName) {{
                showConfigStatus('設定ファイルが選択されていません', 'error');
                return;
            }}

            const data = getFormData();
            await saveConfig(currentConfigName, data);
        }}

        // YAML設定を保存
        async function saveYamlConfig() {{
            if (!currentConfigName) {{
                showConfigStatus('設定ファイルが選択されていません', 'error');
                return;
            }}

            const yamlText = document.getElementById('yamlTextarea').value;

            try {{
                const data = jsyaml.load(yamlText);
                document.getElementById('yamlError').style.display = 'none';
                await saveConfig(currentConfigName, data);
            }} catch (e) {{
                document.getElementById('yamlError').textContent = 'YAML構文エラー: ' + e.message;
                document.getElementById('yamlError').style.display = 'block';
            }}
        }}

        // 設定を保存
        async function saveConfig(configName, data) {{
            try {{
                const response = await fetch('/settings', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ config_name: configName, data: data }})
                }});
                const result = await response.json();

                if (result.success) {{
                    currentConfigData = data;
                    if (result.warnings && result.warnings.length > 0) {{
                        showConfigStatus('保存しました（警告: ' + result.warnings.join('; ') + '）', 'warning');
                    }} else {{
                        showConfigStatus('保存しました', 'success');
                    }}
                }} else {{
                    const msg = result.errors ? result.errors.join(', ') : '不明なエラー';
                    const warn = (result.warnings && result.warnings.length > 0)
                        ? '\\n警告: ' + result.warnings.join('; ') : '';
                    showConfigStatus('保存に失敗しました: ' + msg + warn, 'error');
                }}
            }} catch (e) {{
                showConfigStatus('保存エラー: ' + e.message, 'error');
            }}
        }}

        // 事前検証のみ（保存しない）
        async function validateConfigOnly() {{
            if (!currentConfigName) {{
                showConfigStatus('設定ファイルが選択されていません', 'error');
                return;
            }}
            const data = currentEditorMode === 'form' ? getFormData()
                : jsyaml.load(document.getElementById('yamlTextarea').value);
            try {{
                const resp = await fetch('/api/config/validate', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ data }})
                }});
                const result = await resp.json();
                if (result.success) {{
                    const warn = (result.warnings && result.warnings.length > 0)
                        ? '（警告: ' + result.warnings.join('; ') + '）' : '';
                    showConfigStatus('検証OK' + warn, result.warnings?.length ? 'warning' : 'success');
                }} else {{
                    showConfigStatus('検証NG: ' + result.errors.join(', '), 'error');
                }}
            }} catch (e) {{
                showConfigStatus('検証エラー: ' + e.message, 'error');
            }}
        }}

        // 保存して Phase 6 を再実行
        async function saveAndRetryPhase6() {{
            const params = new URLSearchParams(window.location.search);
            const workflowId = params.get('workflow_id');
            if (!workflowId) {{
                showConfigStatus('workflow_id が URL にありません。ワークフロー画面から遷移してください。', 'error');
                return;
            }}
            if (!currentConfigName) {{
                showConfigStatus('設定ファイルが選択されていません', 'error');
                return;
            }}
            if (!confirm('設定を保存して Phase 6 をリセット＆再実行します。よろしいですか？')) return;

            // 1. 保存
            const data = currentEditorMode === 'form' ? getFormData()
                : jsyaml.load(document.getElementById('yamlTextarea').value);
            try {{
                const saveResp = await fetch('/settings', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ config_name: currentConfigName, data }})
                }});
                const saveResult = await saveResp.json();
                if (!saveResult.success) {{
                    showConfigStatus('保存に失敗: ' + saveResult.errors.join(', '), 'error');
                    return;
                }}
            }} catch (e) {{
                showConfigStatus('保存エラー: ' + e.message, 'error');
                return;
            }}

            // 2. Phase 6 リセット
            try {{
                const retryResp = await fetch('/api/workflow/retry-phase', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, from_phase: 6 }})
                }});
                const retryResult = await retryResp.json();
                if (!retryResult.success) {{
                    showConfigStatus('リセット失敗: ' + (retryResult.errors || []).join(', '), 'error');
                    return;
                }}
            }} catch (e) {{
                showConfigStatus('リセットエラー: ' + e.message, 'error');
                return;
            }}

            // 3. Phase 6 再実行
            try {{
                const runResp = await fetch('/api/workflow/continue-step', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId }})
                }});
                const runResult = await runResp.json();
                if (runResult.success) {{
                    showConfigStatus('Phase 6 を再実行しました。ワークフロー画面に戻ります...', 'success');
                    setTimeout(() => {{ window.location.href = '/?id=' + workflowId; }}, 1500);
                }} else {{
                    showConfigStatus('再実行失敗: ' + (runResult.errors || []).join(', '), 'error');
                }}
            }} catch (e) {{
                showConfigStatus('再実行エラー: ' + e.message, 'error');
            }}
        }}

        // YAML構文チェック
        function validateYaml() {{
            const yamlText = document.getElementById('yamlTextarea').value;

            try {{
                jsyaml.load(yamlText);
                document.getElementById('yamlError').textContent = '✓ 構文は正しいです';
                document.getElementById('yamlError').style.display = 'block';
                document.getElementById('yamlError').className = 'yaml-error yaml-success';
            }} catch (e) {{
                document.getElementById('yamlError').textContent = 'YAML構文エラー: ' + e.message;
                document.getElementById('yamlError').style.display = 'block';
                document.getElementById('yamlError').className = 'yaml-error';
            }}
        }}

        // エディタモード切り替え
        function switchEditorMode(mode) {{
            currentEditorMode = mode;

            const formEditor = document.getElementById('formEditor');
            const yamlEditor = document.getElementById('yamlEditor');
            const tabs = document.querySelectorAll('.tab-btn');

            if (mode === 'form') {{
                formEditor.style.display = 'block';
                yamlEditor.style.display = 'none';
                tabs[0].classList.add('active');
                tabs[1].classList.remove('active');

                // YAMLからフォームに同期
                try {{
                    const yamlText = document.getElementById('yamlTextarea').value;
                    if (yamlText) {{
                        const data = jsyaml.load(yamlText);
                        populateForm(data);
                    }}
                }} catch (e) {{ /* ignore */ }}
            }} else {{
                formEditor.style.display = 'none';
                yamlEditor.style.display = 'block';
                tabs[0].classList.remove('active');
                tabs[1].classList.add('active');

                // フォームからYAMLに同期
                const data = getFormData();
                document.getElementById('yamlTextarea').value = jsyaml.dump(data);
            }}
        }}

        // ステータスメッセージ表示
        function showConfigStatus(message, type) {{
            const status = document.getElementById('configStatus');
            status.textContent = message;
            status.className = 'config-status config-status-' + type;
            status.style.display = 'block';
            setTimeout(() => {{ status.style.display = 'none'; }}, 3000);
        }}

        // query param で config 自動ロード & フィールドハイライト
        (function() {{
            const params = new URLSearchParams(window.location.search);
            const configParam = params.get('config');
            const repoParam = params.get('repo');
            const fieldParam = params.get('field');
            // workflow_id がある場合は「保存して Phase 6 を再実行」ボタンを表示
            if (params.get('workflow_id')) {{
                const btn1 = document.getElementById('btnSaveAndRetryPhase6');
                const btn2 = document.getElementById('btnSaveAndRetryPhase6Yaml');
                if (btn1) btn1.style.display = '';
                if (btn2) btn2.style.display = '';
            }}
            if (configParam) {{
                const sel = document.getElementById('projectConfigSelect');
                if (sel) {{
                    sel.value = configParam;
                    loadProjectConfig(configParam).then(() => {{
                        if (repoParam && fieldParam) {{
                            // リポジトリ行を検索してハイライト
                            setTimeout(() => {{
                                const rows = document.querySelectorAll('#repositoriesContainer .repo-row');
                                rows.forEach(row => {{
                                    const nameInput = row.querySelector('.repo-name');
                                    if (nameInput && nameInput.value === repoParam) {{
                                        row.style.outline = '2px solid #2563eb';
                                        row.style.borderRadius = '8px';
                                        const target = row.querySelector('.repo-' + fieldParam.replace('_command', '-command'));
                                        if (target) {{
                                            target.style.background = '#dbeafe';
                                            target.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                                            target.focus();
                                        }} else {{
                                            row.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                                        }}
                                    }}
                                }});
                            }}, 300);
                        }}
                    }});
                }}
            }}
        }})();
    </script>

    <!-- js-yaml ライブラリ (CDN) -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/js-yaml/4.1.0/js-yaml.min.js"></script>
    """


def render_step_controls(config_files: list) -> str:
    """実行コントロール（step/auto）をHTMLにレンダリング。"""
    options_html = '<option value="">（デフォルト設定）</option>'
    for cfg in config_files:
        options_html += f'<option value="{cfg}">{cfg}</option>'

    return f"""
    <div class="card">
        <h3>実行コントロール</h3>
        <p>実行モードを選択して開始/継続を実行します。</p>

        <div class="settings-form">
            <div class="setting-item">
                <label for="executionMode">実行モード</label>
                <select id="executionMode" class="select-input">
                    <option value="step" selected>ステップ実行（hokusai --step）</option>
                    <option value="auto">通常自動実行（hokusai）</option>
                </select>
            </div>
            <div class="setting-item">
                <label for="startTaskUrl">NotionタスクURL</label>
                <input type="text" id="startTaskUrl" class="text-input" placeholder="https://www.notion.so/...">
            </div>
            <div class="setting-item">
                <label for="startConfigSelect">設定ファイル（任意）</label>
                <select id="startConfigSelect" class="select-input">{options_html}</select>
            </div>
            <div class="form-actions">
                <button type="button" class="save-btn" onclick="startWorkflowFromControl()">開始</button>
            </div>
        </div>
    </div>
    """


def render_html(content: str, title: str = "HOKUS AI Dashboard") -> str:
    """HTMLテンプレート"""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #f8fafc;
            --bg-secondary: #fff;
            --bg-tertiary: #f1f5f9;
            --bg-hover: #f8fafc;
            --text-primary: #1e293b;
            --text-secondary: #475569;
            --text-muted: #64748b;
            --text-muted-2: #94a3b8;
            --border-color: #e2e8f0;
            --border-light: #f1f5f9;
            --accent-color: #3b82f6;
            --accent-hover: #2563eb;
            --code-bg: #f1f5f9;
            --code-text: #475569;
            --card-shadow: 0 1px 3px rgba(0,0,0,0.04);
            --logo-text: #0f172a;
        }}
        /* Dark mode */
        html.dark {{
            --bg-primary: #0f172a;
            --bg-secondary: #1e293b;
            --bg-tertiary: #334155;
            --bg-hover: #334155;
            --text-primary: #f1f5f9;
            --text-secondary: #cbd5e1;
            --text-muted: #94a3b8;
            --text-muted-2: #64748b;
            --border-color: #334155;
            --border-light: #475569;
            --accent-color: #60a5fa;
            --accent-hover: #93c5fd;
            --code-bg: #334155;
            --code-text: #e2e8f0;
            --card-shadow: 0 1px 3px rgba(0,0,0,0.3);
            --logo-text: #f1f5f9;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 24px;
        }}
        /* Header */
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 32px;
            padding-bottom: 20px;
            border-bottom: 1px solid var(--border-color);
        }}
        .header-left {{
            display: flex;
            align-items: center;
            gap: 24px;
        }}
        .logo {{
            font-size: 20px;
            font-weight: 700;
            color: var(--logo-text);
            letter-spacing: -0.5px;
        }}
        .logo span {{
            color: var(--accent-color);
        }}
        .header-nav {{
            display: flex;
            gap: 16px;
        }}
        .header-nav a {{
            font-size: 14px;
            color: var(--text-muted);
            text-decoration: none;
        }}
        .header-nav a:hover {{
            color: var(--accent-color);
        }}
        .refresh-btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            background: var(--bg-secondary);
            color: var(--text-secondary);
            border: 1px solid var(--border-color);
            padding: 8px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-family: inherit;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.15s ease;
        }}
        .refresh-btn:hover {{
            background: var(--bg-tertiary);
            border-color: var(--border-light);
        }}
        /* Typography */
        h1, .page-title {{
            font-size: 24px;
            font-weight: 700;
            color: var(--logo-text);
            margin-bottom: 24px;
        }}
        h2, .section-title {{
            font-size: 18px;
            font-weight: 600;
            color: var(--text-secondary);
            margin: 32px 0 16px;
        }}
        h3 {{
            font-size: 14px;
            font-weight: 600;
            color: var(--text-secondary);
            margin: 0 0 16px 0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        p {{
            color: var(--text-muted);
            margin-bottom: 12px;
        }}
        /* Cards */
        .card {{
            background: var(--bg-secondary);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            border: 1px solid var(--border-color);
            box-shadow: var(--card-shadow);
        }}
        /* Tables */
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 12px 16px;
            text-align: left;
            border-bottom: 1px solid var(--border-light);
        }}
        th {{
            background: var(--bg-tertiary);
            font-weight: 600;
            font-size: 12px;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        tbody tr {{
            transition: background 0.15s ease;
        }}
        tbody tr:hover {{
            background: var(--bg-hover);
        }}
        tbody tr:last-child td {{
            border-bottom: none;
        }}
        .info-table td:first-child {{
            color: var(--text-muted);
            width: 140px;
        }}
        /* Links */
        a {{
            color: var(--accent-color);
            text-decoration: none;
            transition: color 0.15s ease;
        }}
        a:hover {{
            color: var(--accent-hover);
        }}
        .back-link {{
            display: inline-flex;
            align-items: center;
            font-size: 14px;
            color: var(--text-muted);
            margin-bottom: 16px;
        }}
        .back-link:hover {{
            color: var(--accent-color);
        }}
        /* Code */
        code {{
            font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
            background: var(--code-bg);
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 13px;
            color: var(--code-text);
        }}
        code.copyable {{
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        code.copyable:hover {{
            background: var(--border-color);
        }}
        /* Badges */
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
        }}
        .badge-ok {{
            background: #d1fae5;
            color: #065f46;
            border: 1px solid #6ee7b7;
        }}
        .badge-warn {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fbbf24;
        }}
        .badge-muted {{
            background: var(--bg-secondary);
            color: var(--text-muted);
        }}
        .waiting-badge {{
            background: linear-gradient(135deg, #fef3c7, #fde68a);
            color: #92400e;
            border: 1px solid #fbbf24;
            animation: pulse-waiting 2s ease-in-out infinite;
        }}
        @keyframes pulse-waiting {{
            0%, 100% {{ opacity: 1; }}
            50% {{ opacity: 0.7; }}
        }}
        .notion-warning {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fbbf24;
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 16px;
            font-weight: 500;
        }}
        .hygiene-action-banner {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fbbf24;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 16px;
        }}
        .hygiene-action-banner h4 {{
            margin: 0 0 8px 0;
        }}
        .hygiene-action-banner ul {{
            margin: 0 0 12px 0;
            padding-left: 20px;
        }}
        .hygiene-action-banner li {{
            margin-bottom: 4px;
        }}
        .hygiene-actions {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .hygiene-btn {{
            padding: 6px 16px;
            border-radius: 6px;
            border: 1px solid #d97706;
            background: #fff;
            color: #92400e;
            cursor: pointer;
            font-size: 0.85rem;
            font-weight: 500;
        }}
        .hygiene-btn:hover {{
            background: #fef3c7;
        }}
        .hygiene-btn-primary {{
            background: #d97706;
            color: #fff;
            border-color: #b45309;
        }}
        .hygiene-btn-primary:hover {{
            background: #b45309;
        }}
        .hygiene-btn-muted {{
            border-color: #ccc;
            color: #666;
        }}
        .hygiene-btn-muted:hover {{
            background: #f3f4f6;
        }}
        /* Cross-review banner */
        .cross-review-banner {{
            background: #fef3c7;
            border: 1px solid #f59e0b;
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 16px;
        }}
        .cr-summary {{
            display: flex;
            gap: 16px;
            margin: 8px 0;
            font-size: 0.9em;
        }}
        .cr-findings {{
            margin: 12px 0;
        }}
        .finding-item {{
            padding: 8px 12px;
            margin: 4px 0;
            border-radius: 4px;
            border-left: 3px solid #ccc;
        }}
        .finding-critical {{
            border-left-color: #ef4444;
            background: #fef2f2;
        }}
        .finding-major {{
            border-left-color: #f97316;
            background: #fff7ed;
        }}
        .finding-minor {{
            border-left-color: #eab308;
            background: #fefce8;
        }}
        .finding-header {{
            font-weight: 600;
        }}
        .finding-desc {{
            margin: 4px 0;
            font-size: 0.9em;
        }}
        .finding-suggestion {{
            margin-top: 4px;
            font-size: 0.85em;
            color: #4b5563;
        }}
        .cr-actions {{
            display: flex;
            gap: 8px;
            margin-top: 12px;
        }}
        /* Progress */
        .progress-container {{
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .progress-bar {{
            width: 100px;
            height: 6px;
            background: #e2e8f0;
            border-radius: 3px;
            overflow: hidden;
        }}
        .progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, #3b82f6, #60a5fa);
            border-radius: 3px;
            transition: width 0.3s ease;
        }}
        .progress-text {{
            font-size: 13px;
            color: #64748b;
            font-weight: 500;
        }}
        /* Verification */
        .verification-item {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
        }}
        .verification-item.result-ok {{
            background: #dcfce7;
            color: #166534;
        }}
        .verification-item.result-ng {{
            background: #fee2e2;
            color: #991b1b;
        }}
        /* Phase 6 failure panel */
        .phase6-failure-panel {{ border-left: 4px solid #ef4444; }}
        .phase6-error-card {{
            background: #fef2f2; border-radius: 8px; padding: 16px; margin-bottom: 12px;
        }}
        .phase6-error-header {{
            display: flex; align-items: center; gap: 12px; margin-bottom: 8px; flex-wrap: wrap;
        }}
        .phase6-repo-badge {{
            font-weight: 600; font-size: 15px; color: #991b1b;
        }}
        .phase6-category-badge {{
            display: inline-block; padding: 2px 10px; border-radius: 12px;
            font-size: 12px; font-weight: 500;
        }}
        .phase6-summary {{ color: #374151; margin: 4px 0 8px; }}
        .phase6-error-card ul {{
            margin: 0 0 8px; padding-left: 20px; color: #6b7280; font-size: 13px;
        }}
        .phase6-log-preview {{
            background: #1f2937; color: #e5e7eb; padding: 12px; border-radius: 6px;
            font-size: 12px; line-height: 1.5; overflow-x: auto; max-height: 320px; overflow-y: auto;
            white-space: pre-wrap; word-break: break-all;
        }}
        .phase6-log-details {{ margin-top: 8px; }}
        .phase6-log-details summary {{
            cursor: pointer; color: #2563eb; font-size: 13px;
        }}
        .phase6-log-full {{
            background: #1f2937; color: #e5e7eb; padding: 12px; border-radius: 6px;
            font-size: 11px; line-height: 1.4; overflow-x: auto; max-height: 600px; overflow-y: auto;
            white-space: pre-wrap; word-break: break-all; margin-top: 8px;
        }}
        .phase6-actions {{
            display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap;
        }}
        .phase6-action-btn {{
            display: inline-block; padding: 6px 16px; border-radius: 6px;
            font-size: 13px; font-weight: 500; cursor: pointer; text-decoration: none;
            border: 1px solid #d1d5db; background: #fff; color: #374151;
        }}
        .phase6-action-btn:hover {{ background: #f3f4f6; }}
        .phase6-action-primary {{
            background: #2563eb; color: #fff; border-color: #2563eb;
        }}
        .phase6-action-primary:hover {{ background: #1d4ed8; }}
        /* Results */
        .result-ok {{ color: #16a34a; font-weight: 500; }}
        .result-ng {{ color: #dc2626; font-weight: 600; }}
        .result-skip {{ color: #ca8a04; }}
        /* Summary stats */
        .summary-stats {{
            display: flex;
            gap: 16px;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }}
        .stat {{
            display: inline-flex;
            align-items: center;
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
        }}
        .stat-ok {{ background: #dcfce7; color: #166534; }}
        .stat-ng {{ background: #fee2e2; color: #991b1b; }}
        .stat-skip {{ background: #fef3c7; color: #92400e; }}
        .stat-total {{ background: #f1f5f9; color: #475569; }}
        /* Repo cards */
        .repo-card {{
            margin-bottom: 16px;
            padding: 20px;
            border-radius: 10px;
            border: 1px solid #e2e8f0;
            background: #fafafa;
        }}
        .repo-card.repo-passed {{
            border-left: 4px solid #22c55e;
        }}
        .repo-card.repo-failed {{
            border-left: 4px solid #ef4444;
        }}
        .repo-header {{
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 0 0 12px 0;
            font-size: 16px;
            color: #1e293b;
        }}
        .repo-status {{
            font-size: 18px;
        }}
        .repo-passed .repo-status {{ color: #22c55e; }}
        .repo-failed .repo-status {{ color: #ef4444; }}
        .repo-score {{
            margin-left: auto;
            font-size: 14px;
            color: #64748b;
            font-weight: 400;
        }}
        .issues-list {{
            margin: 12px 0;
            padding-left: 20px;
            color: #dc2626;
            font-size: 14px;
        }}
        .issues-list li {{
            margin-bottom: 4px;
        }}
        /* Rule details */
        .rule-details {{
            margin-top: 16px;
        }}
        .rule-details summary {{
            cursor: pointer;
            color: #3b82f6;
            font-size: 14px;
            font-weight: 500;
            padding: 8px 0;
        }}
        .rule-details summary:hover {{
            color: #2563eb;
        }}
        .rule-details table {{
            margin-top: 12px;
        }}
        /* Rule items for rulebook */
        .rule-item {{
            margin-bottom: 16px;
            padding: 16px;
            background: #f8fafc;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
        }}
        .rule-item.project-rule {{
            background: #faf5ff;
            border-color: #e9d5ff;
        }}
        .rule-title {{
            margin: 0 0 10px 0;
            font-size: 15px;
            color: #1e293b;
        }}
        .rule-checklist {{
            margin: 0;
            padding-left: 20px;
            color: #64748b;
            font-size: 13px;
        }}
        .rule-checklist li {{
            margin-bottom: 4px;
        }}
        .rule-description {{
            margin: 0;
            color: #64748b;
            font-size: 13px;
        }}
        .project-rules-card {{
            border-color: #c084fc;
        }}
        .project-rules-card h3 {{
            color: #7c3aed;
        }}
        /* Select input */
        .select-input {{
            background: var(--bg-secondary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            padding: 10px 16px;
            border-radius: 8px;
            font-family: inherit;
            font-size: 14px;
            cursor: pointer;
            min-width: 200px;
        }}
        .select-input:focus {{
            outline: none;
            border-color: var(--accent-color);
            box-shadow: 0 0 0 3px rgba(59,130,246,0.1);
        }}
        /* Settings form */
        .settings-form {{
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}
        .setting-item {{
            display: flex;
            flex-direction: column;
            gap: 8px;
        }}
        .setting-item label {{
            font-size: 14px;
            font-weight: 500;
            color: var(--text-primary);
        }}
        .settings-actions {{
            margin-top: 20px;
            padding-top: 20px;
            border-top: 1px solid var(--border-color);
        }}
        .reset-btn {{
            background: transparent;
            color: var(--text-muted);
            border: 1px solid var(--border-color);
            padding: 8px 16px;
            border-radius: 8px;
            cursor: pointer;
            font-family: inherit;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.15s ease;
        }}
        .reset-btn:hover {{
            background: var(--bg-tertiary);
            color: var(--text-primary);
        }}
        /* Save button */
        .save-btn {{
            background: var(--accent-color);
            color: #fff;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-family: inherit;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.15s ease;
        }}
        .save-btn:hover {{
            background: var(--accent-hover);
        }}
        .delete-btn {{
            background: #ef4444;
            color: #fff;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 500;
        }}
        .delete-btn:hover:not(:disabled) {{
            background: #dc2626;
        }}
        .delete-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .phase-run-btn {{
            background: var(--accent-color);
            color: #fff;
            border: none;
            padding: 6px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-family: inherit;
            font-size: 12px;
            font-weight: 500;
            transition: all 0.15s ease;
        }}
        .phase-run-btn:hover:not(:disabled) {{
            background: var(--accent-hover);
        }}
        .phase-run-btn:disabled {{
            background: #cbd5e1;
            color: #64748b;
            cursor: not-allowed;
        }}
        .phase-retry-btn {{
            background: transparent;
            color: #dc2626;
            border: 1px solid #fca5a5;
            padding: 4px 8px;
            border-radius: 6px;
            cursor: pointer;
            font-family: inherit;
            font-size: 11px;
            font-weight: 500;
            margin-left: 4px;
            transition: all 0.15s ease;
        }}
        .phase-decision-actions {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .phase-decision-btn {{
            background: #fff7ed;
            color: #9a3412;
            border: 1px solid #fdba74;
            padding: 6px 10px;
            border-radius: 6px;
            cursor: pointer;
            font-family: inherit;
            font-size: 12px;
            font-weight: 600;
        }}
        .phase-decision-btn:hover:not(:disabled) {{
            background: #ffedd5;
        }}
        .phase-decision-btn-primary {{
            background: #1d4ed8;
            border-color: #1d4ed8;
            color: #fff;
        }}
        .phase-decision-btn-primary:hover:not(:disabled) {{
            background: #1e40af;
        }}
        .phase-decision-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        .phase-retry-btn:hover:not(:disabled) {{
            background: #fef2f2;
            border-color: #dc2626;
        }}
        .phase-retry-btn:disabled {{
            opacity: 0.5;
            cursor: not-allowed;
        }}
        /* Phase 9 レビュー対応テーブル */
        .phase9-review-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            margin-top: 4px;
        }}
        .phase9-review-table th {{
            text-align: left;
            padding: 4px 8px;
            color: #6b7280;
            font-weight: normal;
            border-bottom: 2px solid #e5e7eb;
            white-space: nowrap;
        }}
        .phase9-review-table td {{
            padding: 4px 8px;
            border-bottom: 1px solid #f3f4f6;
            vertical-align: top;
        }}
        .phase9-review-actions {{
            display: flex;
            gap: 4px;
            flex-wrap: wrap;
        }}
        .phase9-review-footer {{
            margin-top: 8px;
            padding-top: 8px;
            border-top: 1px solid #e5e7eb;
        }}
        .phase9-summary {{
            font-size: 12px;
            margin-top: 4px;
        }}
        .phase9-summary-item {{
            display: inline-block;
            margin-right: 12px;
            white-space: nowrap;
        }}
        .phase9-detail-btn {{
            background: var(--accent-color);
            color: #fff;
            border: none;
            padding: 4px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-family: inherit;
            font-size: 11px;
            font-weight: 500;
            margin-top: 4px;
            transition: all 0.15s ease;
        }}
        .phase9-detail-btn:hover {{
            background: var(--accent-hover);
        }}
        /* Phase 9 モーダル */
        .phase9-modal {{
            border: none;
            border-radius: 12px;
            padding: 0;
            max-width: 720px;
            width: 90%;
            box-shadow: 0 8px 32px rgba(0,0,0,0.2);
            background: #ffffff;
            color: var(--text-primary);
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            margin: 0;
        }}
        .phase9-modal::backdrop {{
            background: rgba(0,0,0,0.4);
        }}
        .phase9-modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 20px;
            border-bottom: 1px solid #e5e7eb;
        }}
        .phase9-modal-header h3 {{
            margin: 0;
            font-size: 15px;
            font-weight: 600;
        }}
        .phase9-modal-close {{
            background: transparent;
            border: none;
            font-size: 18px;
            cursor: pointer;
            color: #6b7280;
            padding: 4px 8px;
            border-radius: 4px;
        }}
        .phase9-modal-close:hover {{
            background: #f3f4f6;
            color: #111827;
        }}
        .phase9-modal-body {{
            padding: 16px 20px;
            max-height: 60vh;
            overflow-y: auto;
        }}
        .row-actions {{
            display: flex;
            flex-direction: column;
            gap: 6px;
            align-items: flex-start;
            width: max-content;
        }}
        .row-run-btn {{
            background: var(--accent-color);
            color: #fff;
            border: none;
            padding: 5px 9px;
            border-radius: 6px;
            cursor: pointer;
            font-family: inherit;
            font-size: 12px;
            font-weight: 500;
            transition: all 0.15s ease;
        }}
        .row-run-btn:hover:not(:disabled) {{
            background: var(--accent-hover);
        }}
        .row-run-btn:disabled {{
            background: #cbd5e1;
            color: #64748b;
            cursor: not-allowed;
        }}
        .row-run-mode-badge {{
            font-size: 11px;
            color: #2f6f4f;
            font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
            background: #ecfdf3;
            border: 1px solid #bbf7d0;
            border-radius: 4px;
            padding: 2px 8px;
            line-height: 1.2;
            white-space: nowrap;
        }}
        .workflow-list-table th:last-child,
        .workflow-list-table td:last-child {{
            width: 128px;
            min-width: 128px;
        }}
        .workflow-list-table th.col-progress,
        .workflow-list-table td.col-progress,
        .workflow-list-table th.col-actions,
        .workflow-list-table td.col-actions {{
            text-align: center;
        }}
        .workflow-list-table td.col-updated {{
            text-align: center;
            white-space: nowrap;
            line-height: 1.3;
        }}
        .workflow-list-table td.col-progress .progress-container,
        .workflow-list-table td.col-actions .row-actions {{
            justify-content: center;
            align-items: center;
            margin: 0 auto;
        }}
        /* Editor tabs */
        .editor-tabs {{
            display: flex;
            gap: 4px;
            margin: 20px 0 16px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 0;
        }}
        .tab-btn {{
            background: transparent;
            color: var(--text-muted);
            border: none;
            padding: 10px 20px;
            border-radius: 8px 8px 0 0;
            cursor: pointer;
            font-family: inherit;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.15s ease;
            border-bottom: 2px solid transparent;
            margin-bottom: -1px;
        }}
        .tab-btn:hover {{
            color: var(--text-primary);
            background: var(--bg-tertiary);
        }}
        .tab-btn.active {{
            color: var(--accent-color);
            border-bottom: 2px solid var(--accent-color);
            background: transparent;
        }}
        /* Editor panel */
        .editor-panel {{
            margin-top: 16px;
        }}
        /* Config fieldset */
        .config-fieldset {{
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px 20px;
            margin-bottom: 16px;
            background: var(--bg-primary);
        }}
        .config-fieldset legend {{
            font-weight: 600;
            font-size: 14px;
            color: var(--text-secondary);
            padding: 0 8px;
        }}
        /* Text input */
        .text-input {{
            width: 100%;
            max-width: 100%;
            background: var(--bg-secondary);
            color: var(--text-primary);
            border: 1px solid var(--border-color);
            padding: 10px 14px;
            border-radius: 8px;
            font-family: inherit;
            font-size: 14px;
            transition: all 0.15s ease;
        }}
        .text-input:focus {{
            outline: none;
            border-color: var(--accent-color);
            box-shadow: 0 0 0 3px rgba(59,130,246,0.1);
        }}
        .text-input::placeholder {{
            color: var(--text-muted-2);
        }}
        /* Form actions */
        .form-actions {{
            display: flex;
            gap: 12px;
            margin-top: 20px;
            padding-top: 16px;
            border-top: 1px solid var(--border-color);
        }}
        /* Repositories editor */
        .repos-container {{
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-top: 12px;
        }}
        .repo-row {{
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 12px;
            background: var(--bg-secondary);
        }}
        .repo-row-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px 12px;
            align-items: center;
        }}
        .checkbox-label {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 14px;
            cursor: pointer;
        }}
        .checkbox-group {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px 24px;
        }}
        .repo-checkbox {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            color: var(--text-secondary);
        }}
        .repo-remove-btn {{
            margin-top: 8px;
        }}
        .command-result {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            z-index: 9999;
            margin: 0;
            padding: 12px 20px;
            border-radius: 0;
            border: none;
            border-bottom: 2px solid var(--border-color);
            background: var(--bg-primary);
            font-size: 13px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
        }}
        .command-result pre {{
            margin-top: 8px;
            background: var(--code-bg);
            color: var(--code-text);
            border: 1px solid var(--border-color);
            border-radius: 6px;
            padding: 10px;
            overflow-x: auto;
            white-space: pre-wrap;
            font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
            font-size: 12px;
        }}
        .command-result-success {{
            border-bottom-color: #22c55e;
            background: #f0fdf4;
            color: #166534;
        }}
        .command-result-error {{
            border-bottom-color: #ef4444;
            background: #fef2f2;
            color: #991b1b;
        }}
        /* YAML textarea */
        .yaml-textarea {{
            width: 100%;
            min-height: 400px;
            background: #1e293b;
            color: #e2e8f0;
            border: 1px solid var(--border-color);
            padding: 16px;
            border-radius: 8px;
            font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace;
            font-size: 13px;
            line-height: 1.5;
            resize: vertical;
            tab-size: 2;
        }}
        .yaml-textarea:focus {{
            outline: none;
            border-color: var(--accent-color);
        }}
        /* YAML error */
        .yaml-error {{
            background: #fee2e2;
            color: #991b1b;
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 13px;
            margin-bottom: 12px;
            border: 1px solid #fecaca;
        }}
        .yaml-success {{
            background: #dcfce7;
            color: #166534;
            border-color: #bbf7d0;
        }}
        @media (max-width: 768px) {{
            .settings-form {{
                gap: 14px;
            }}
            .text-input, .select-input {{
                max-width: 100%;
                min-width: 0;
                width: 100%;
            }}
            .form-actions {{
                flex-direction: column;
                align-items: stretch;
            }}
            .repo-row-grid {{
                grid-template-columns: 1fr;
            }}
        }}
        /* Config status */
        .config-status {{
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 14px;
            margin-top: 16px;
        }}
        .config-status-success {{
            background: #dcfce7;
            color: #166534;
            border: 1px solid #bbf7d0;
        }}
        .config-status-error {{
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #fecaca;
        }}
        .config-status-warning {{
            background: #fef3c7;
            color: #92400e;
            border: 1px solid #fde68a;
        }}
        /* Code block */
        .code-block {{
            background: #1e293b;
            padding: 16px;
            border-radius: 8px;
            overflow-x: auto;
            margin: 12px 0;
        }}
        .code-block code {{
            background: none;
            color: #e2e8f0;
            padding: 0;
            font-size: 13px;
        }}
        /* Utility classes */
        .text-muted {{ color: var(--text-muted-2); }}
        .text-sm {{ font-size: 13px; }}
        .text-center {{ text-align: center; }}
        .text-nowrap {{ white-space: nowrap; }}
        /* Toast */
        .copy-toast {{
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%) translateY(20px);
            background: #1e293b;
            color: #fff;
            padding: 12px 24px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 500;
            z-index: 2000;
            opacity: 0;
            transition: all 0.2s ease;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }}
        .copy-toast.show {{
            opacity: 1;
            transform: translateX(-50%) translateY(0);
        }}
        .substep-text {{
            margin-left: 8px;
            font-size: 0.85em;
            color: var(--text-muted);
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-left">
                <div class="logo">HOKUS<span>AI</span> Dashboard</div>
                <nav class="header-nav">
                    <a href="/">一覧</a>
                    <a href="/settings">設定</a>
                    <a href="/rulebook">ルールブック</a>
                    <a href="/prompts">LLM指示文</a>
                </nav>
            </div>
            <button class="refresh-btn" onclick="location.reload()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 12a9 9 0 11-2.2-5.9M21 3v6h-6"/>
                </svg>
                更新
            </button>
        </div>
        <div id="workflowActionResult" class="command-result" style="display:none;"></div>
        {content}
    </div>
    <div id="copyToast" class="copy-toast">コピーしました</div>
    <script>
        const _actionButtonSelector = 'button.row-run-btn, button.phase-run-btn, button.phase-retry-btn, button.save-btn, button.delete-btn, button.phase-decision-btn, .cr-actions button, .hygiene-actions button';
        let _originalDisabledState = new Map();

        function _disableAllActionButtons() {{
            _originalDisabledState = new Map();
            document.querySelectorAll(_actionButtonSelector).forEach(btn => {{
                _originalDisabledState.set(btn, btn.disabled);
                btn.disabled = true;
                btn.style.opacity = '0.5';
            }});
        }}

        function _restoreActionButtons() {{
            document.querySelectorAll(_actionButtonSelector).forEach(btn => {{
                const wasDisabled = _originalDisabledState.get(btn) ?? false;
                btn.disabled = wasDisabled;
                btn.style.opacity = wasDisabled ? '0.5' : '';
            }});
            _originalDisabledState = new Map();
        }}

        function showWorkflowActionResult(result, isError = false) {{
            const el = document.getElementById('workflowActionResult');
            if (!el) return;
            const stdout = result.stdout ? `<pre><strong>stdout</strong>\n${{escapeHtml(result.stdout)}}</pre>` : '';
            const stderr = result.stderr ? `<pre><strong>stderr</strong>\n${{escapeHtml(result.stderr)}}</pre>` : '';
            const errors = result.errors ? `<pre><strong>errors</strong>\n${{escapeHtml(result.errors.join('\\n'))}}</pre>` : '';
            const workflow = result.workflow_id ? `<p><strong>workflow_id:</strong> <code>${{result.workflow_id}}</code></p>` : '';
            const mode = result.mode ? `<p><strong>mode:</strong> <code>${{escapeHtml(result.mode)}}</code></p>` : '';
            el.className = 'command-result ' + (isError ? 'command-result-error' : 'command-result-success');
            el.innerHTML = `
                <p><strong>${{isError ? '実行失敗' : '実行開始'}}</strong>${{result.message ? ': ' + escapeHtml(result.message) : ''}}</p>
                ${{mode}}
                ${{workflow}}
                ${{errors}}
                ${{stdout}}
                ${{stderr}}
            `;
            el.style.display = 'block';
            if (isError) {{
                _restoreActionButtons();
                sessionStorage.removeItem('_hokusai_banner');
            }} else {{
                _disableAllActionButtons();
                // リロード後もバナーを復元するために sessionStorage に保存
                sessionStorage.setItem('_hokusai_banner', JSON.stringify({{
                    html: el.innerHTML,
                    className: el.className,
                }}));
            }}
        }}

        // ページロード時に sessionStorage からバナーを復元
        (function _restoreBanner() {{
            const saved = sessionStorage.getItem('_hokusai_banner');
            if (!saved) return;
            const el = document.getElementById('workflowActionResult');
            if (!el) return;
            try {{
                const data = JSON.parse(saved);
                el.innerHTML = data.html;
                el.className = data.className;
                el.style.display = 'block';
            }} catch(e) {{}}
            // bg_running が終了している（= 自動リロードで running=false）場合はバナーをクリア
            if (!document.querySelector('[data-bg-running]')) {{
                sessionStorage.removeItem('_hokusai_banner');
            }}
        }}());

        function escapeHtml(text) {{
            if (!text) return '';
            return text
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;');
        }}

        function getSelectedExecutionMode() {{
            const modeSelect = document.getElementById('executionMode');
            const mode = (modeSelect?.value || 'step').trim();
            return mode === 'auto' ? 'auto' : 'step';
        }}

        async function startWorkflowFromControl() {{
            if (window.__workflowActionPending) {{
                showWorkflowActionResult({{ errors: ['実行中です。完了までお待ちください。'] }}, true);
                return;
            }}
            const taskUrlInput = document.getElementById('startTaskUrl');
            const configSelect = document.getElementById('startConfigSelect');
            const taskUrl = taskUrlInput?.value?.trim();
            const configName = configSelect?.value || '';
            const mode = getSelectedExecutionMode();
            if (!taskUrl) {{
                showWorkflowActionResult({{ errors: ['NotionタスクURLを入力してください'] }}, true);
                return;
            }}

            try {{
                window.__workflowActionPending = true;
                showWorkflowActionResult({{ message: 'バックグラウンドで実行を開始しています...', mode }}, false);
                const endpoint = mode === 'auto' ? '/api/workflow/start-auto' : '/api/workflow/start-step';
                const response = await fetch(endpoint, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ task_url: taskUrl, config_name: configName || null }}),
                }});
                const result = await response.json();
                result.mode = mode;
                if (result.success) {{
                    window.alert(`バックグラウンドで実行を開始しました。\\nmode: ${{mode}}\\n\\nページリロードで進捗を確認できます。`);
                    location.href = '/';
                }} else {{
                    showWorkflowActionResult(result, true);
                }}
            }} catch (e) {{
                showWorkflowActionResult({{ errors: [e.message] }}, true);
            }} finally {{
                window.__workflowActionPending = false;
            }}
        }}

        async function continueWorkflowByMode(workflowId, mode, event = null) {{
            if (window.__workflowActionPending) {{
                showWorkflowActionResult({{ errors: ['実行中です。完了までお待ちください。'] }}, true);
                return;
            }}
            if (event) {{
                event.stopPropagation();
                event.preventDefault();
            }}
            const normalizedMode = (mode === 'auto') ? 'auto' : 'step';
            if (!workflowId) {{
                showWorkflowActionResult({{ errors: ['workflow_id が取得できません'] }}, true);
                return;
            }}

            try {{
                window.__workflowActionPending = true;
                showWorkflowActionResult({{ message: 'バックグラウンドで実行を開始しています...', mode: normalizedMode, workflow_id: workflowId }}, false);
                const endpoint = normalizedMode === 'auto'
                    ? '/api/workflow/continue-auto'
                    : '/api/workflow/continue-step';
                const response = await fetch(endpoint, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId }}),
                }});
                const result = await response.json();
                result.mode = normalizedMode;
                if (result.success) {{
                    location.reload();
                }} else {{
                    showWorkflowActionResult(result, true);
                }}
            }} catch (e) {{
                showWorkflowActionResult({{ errors: [e.message] }}, true);
            }} finally {{
                window.__workflowActionPending = false;
            }}
        }}

        async function executeHygieneAction(workflowId, action) {{
            const labels = {{
                'rebase': 'Rebase',
                'cherry-pick': 'Cherry-pick',
                'merge': 'Merge',
                'ignore': '無視して続行',
            }};
            const label = labels[action] || action;
            if (!confirm(`ブランチ衛生対応: ${{label}} を実行しますか？`)) return;
            try {{
                showWorkflowActionResult({{ message: `${{label}} を実行中...` }}, false);
                const response = await fetch('/api/workflow/continue-step', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, action: action }}),
                }});
                const result = await response.json();
                if (result.success) {{
                    location.reload();
                }} else {{
                    showWorkflowActionResult(result, true);
                }}
            }} catch (e) {{
                showWorkflowActionResult({{ errors: [e.message] }}, true);
            }}
        }}

        async function deleteWorkflow(workflowId) {{
            if (!workflowId) return;
            if (!confirm('このワークフローを削除しますか？\\n\\nID: ' + workflowId + '\\n\\nこの操作は取り消せません。')) return;
            try {{
                const response = await fetch('/api/workflow/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId }}),
                }});
                const result = await response.json();
                if (result.success) {{
                    alert(result.message || '削除しました');
                    location.href = '/';
                }} else {{
                    alert('削除に失敗しました: ' + (result.errors || []).join(', '));
                }}
            }} catch (e) {{
                alert('エラー: ' + e.message);
            }}
        }}

        async function retryFromPhase(workflowId, fromPhase) {{
            if (!workflowId || !fromPhase) return;
            if (!confirm(`選択したフェーズ以降のステータスを未実行に戻します。よろしいですか？\\n\\nID: ${{workflowId}}\\nPhase ${{fromPhase}}〜9 が未実行に戻ります。`)) return;
            try {{
                const response = await fetch('/api/workflow/retry-phase', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, from_phase: fromPhase }}),
                }});
                const result = await response.json();
                if (result.success) {{
                    alert(result.message || 'リセットしました');
                    location.reload();
                }} else {{
                    alert('リセットに失敗しました: ' + (result.errors || []).join(', '));
                }}
            }} catch (e) {{
                alert('エラー: ' + e.message);
            }}
        }}

        async function retryNotion(workflowId) {{
            if (!workflowId) return;
            if (!confirm('Notion へのデータ反映をリトライします。よろしいですか？')) return;
            try {{
                const response = await fetch('/api/workflow/retry-notion', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId }}),
                }});
                const result = await response.json();
                if (result.success) {{
                    alert(result.message || 'Notion リトライ完了');
                    location.reload();
                }} else {{
                    alert('Notion リトライ失敗: ' + (result.errors || []).join(', '));
                }}
            }} catch (e) {{
                alert('エラー: ' + e.message);
            }}
        }}

        function _setCrButtonsDisabled(disabled) {{
            const el = document.querySelector('.cr-actions');
            if (!el) return;
            if (disabled) {{
                el.setAttribute('data-original-html', el.innerHTML);
                el.innerHTML = '<span style="color:#92400e;font-style:italic;padding:6px 0;">&#9203; 処理中です。しばらくお待ちください...</span>';
            }} else {{
                const orig = el.getAttribute('data-original-html');
                if (orig) {{ el.innerHTML = orig; el.removeAttribute('data-original-html'); }}
            }}
        }}

        async function applyCrossReviewFixes(workflowId, phase) {{
            if (!confirm('クロスレビュー指摘を反映して文書を修正します。よろしいですか？')) return;
            _setCrButtonsDisabled(true);
            showWorkflowActionResult({{ message: 'LLMによる文書修正を実行中...（数分かかる場合があります）' }}, false);
            try {{
                const resp = await fetch('/api/workflow/apply-cross-review-fixes', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, phase: phase }}),
                }});
                const result = await resp.json();
                if (result.success) {{ alert(result.message); sessionStorage.removeItem('_hokusai_banner'); location.reload(); }}
                else {{ _setCrButtonsDisabled(false); showWorkflowActionResult(result, true); }}
            }} catch (e) {{ _setCrButtonsDisabled(false); showWorkflowActionResult({{ errors: [e.message] }}, true); }}
        }}

        async function rerunCrossReview(workflowId, phase) {{
            if (!confirm('修正後の文書に対してクロスレビューを再実行します。よろしいですか？')) return;
            _setCrButtonsDisabled(true);
            showWorkflowActionResult({{ message: 'クロスレビューを再実行中...' }}, false);
            try {{
                const resp = await fetch('/api/workflow/rerun-cross-review', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, phase: phase }}),
                }});
                const result = await resp.json();
                if (result.success) {{ alert(result.message); sessionStorage.removeItem('_hokusai_banner'); location.reload(); }}
                else {{ _setCrButtonsDisabled(false); showWorkflowActionResult(result, true); }}
            }} catch (e) {{ _setCrButtonsDisabled(false); showWorkflowActionResult({{ errors: [e.message] }}, true); }}
        }}

        async function ignoreCrossReview(workflowId, phase) {{
            if (!confirm('\\u26a0\\ufe0f クロスレビューの指摘を無視して続行します。\\n監査ログに記録されます。\\n\\n本当によろしいですか？')) return;
            _setCrButtonsDisabled(true);
            showWorkflowActionResult({{ message: 'クロスレビュー指摘を無視して続行中...' }}, false);
            try {{
                const resp = await fetch('/api/workflow/continue-ignore-cross-review', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, phase: phase }}),
                }});
                const result = await resp.json();
                if (result.success) {{ alert(result.message); sessionStorage.removeItem('_hokusai_banner'); location.reload(); }}
                else {{ _setCrButtonsDisabled(false); showWorkflowActionResult(result, true); }}
            }} catch (e) {{ _setCrButtonsDisabled(false); showWorkflowActionResult({{ errors: [e.message] }}, true); }}
        }}

        async function applyPhaseDecision(workflowId, phase, decision) {{
            const label = decision === 'approve_and_move_next' ? 'approve_and_move_next' : 'request_changes';
            if (!confirm(`Phase ${{phase}} に対して ${{label}} を記録します。よろしいですか？`)) return;
            try {{
                showWorkflowActionResult({{ message: `Phase ${{phase}} の判断を記録中...` }}, false);
                const resp = await fetch('/api/workflow/phase-decision', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, phase: phase, decision: decision }}),
                }});
                const result = await resp.json();
                if (result.success) {{
                    alert(result.message);
                    location.reload();
                }} else {{
                    showWorkflowActionResult(result, true);
                }}
            }} catch (e) {{
                showWorkflowActionResult({{ errors: [e.message] }}, true);
            }}
        }}

        async function prReviewAction(workflowId, prIndex, action) {{
            if (!workflowId || !action) return;
            if (window.__prReviewActionPending) return;
            window.__prReviewActionPending = true;
            // クリックしたボタンの親要素内の全ボタンを無効化
            const actionsEl = event && event.target ? event.target.closest('.phase9-review-actions') : null;
            if (actionsEl) {{
                actionsEl.querySelectorAll('button').forEach(btn => {{
                    btn.disabled = true;
                    btn.style.opacity = '0.5';
                }});
                event.target.textContent = '実行中...';
            }}
            // 「レビュー詳細を開く」ボタンも無効化
            document.querySelectorAll('.phase9-detail-btn').forEach(btn => {{
                btn.disabled = true;
                btn.style.opacity = '0.5';
                btn.style.cursor = 'not-allowed';
            }});
            try {{
                const response = await fetch('/api/workflow/pr-review-action', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId, pr_index: prIndex, action: action }}),
                }});
                const result = await response.json();
                if (result.success) {{
                    location.reload();
                }} else {{
                    window.__prReviewActionPending = false;
                    alert('エラー: ' + (result.errors || []).join(', '));
                    location.reload();
                }}
            }} catch (e) {{
                window.__prReviewActionPending = false;
                alert('エラー: ' + e.message);
                location.reload();
            }}
        }}

        async function continueWorkflowStep(workflowIdArg = null) {{
            if (window.__workflowActionPending) {{
                showWorkflowActionResult({{ errors: ['実行中です。完了までお待ちください。'] }}, true);
                return;
            }}
            const workflowId = (workflowIdArg || '').trim();
            if (!workflowId) {{
                showWorkflowActionResult({{ errors: ['workflow_id が取得できません'] }}, true);
                return;
            }}

            try {{
                window.__workflowActionPending = true;
                showWorkflowActionResult({{ message: 'バックグラウンドで実行を開始しています...', mode: 'step', workflow_id: workflowId }}, false);
                const response = await fetch('/api/workflow/continue-step', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ workflow_id: workflowId }}),
                }});
                const result = await response.json();
                result.mode = 'step';
                if (result.success) {{
                    location.reload();
                }} else {{
                    showWorkflowActionResult(result, true);
                }}
            }} catch (e) {{
                showWorkflowActionResult({{ errors: [e.message] }}, true);
            }} finally {{
                window.__workflowActionPending = false;
            }}
        }}

        // ダッシュボード設定のLocalStorageキー
        const SETTINGS_KEY = 'hokusai_dashboard_settings';

        // デフォルト設定
        const DEFAULT_SETTINGS = {{
            theme: 'light',
            listLimit: 0
        }};

        // 設定を読み込み
        function loadDashboardSettings() {{
            try {{
                const saved = localStorage.getItem(SETTINGS_KEY);
                if (saved) {{
                    return {{ ...DEFAULT_SETTINGS, ...JSON.parse(saved) }};
                }}
            }} catch (e) {{
                console.warn('設定の読み込みに失敗しました:', e);
            }}
            return {{ ...DEFAULT_SETTINGS }};
        }}

        // 設定を保存
        function saveDashboardSettings() {{
            const settings = {{
                theme: document.getElementById('theme')?.value || DEFAULT_SETTINGS.theme,
                listLimit: parseInt(document.getElementById('listLimit')?.value || DEFAULT_SETTINGS.listLimit)
            }};

            try {{
                localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
                applySettings(settings);
                showToast('設定を保存しました');
            }} catch (e) {{
                console.error('設定の保存に失敗しました:', e);
            }}
        }}

        // 設定をリセット
        function resetDashboardSettings() {{
            try {{
                localStorage.removeItem(SETTINGS_KEY);
                applySettings(DEFAULT_SETTINGS);
                restoreFormValues(DEFAULT_SETTINGS);
                showToast('設定をリセットしました');
            }} catch (e) {{
                console.error('設定のリセットに失敗しました:', e);
            }}
        }}

        // 設定を適用
        function applySettings(settings) {{
            // テーマ適用
            applyTheme(settings.theme);

            // 一覧表示件数を適用（テーブル行の表示/非表示）
            applyListLimit(settings.listLimit);
        }}

        // テーマを適用
        function applyTheme(theme) {{
            const html = document.documentElement;
            html.classList.remove('dark');

            if (theme === 'dark') {{
                html.classList.add('dark');
            }} else if (theme === 'system') {{
                if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {{
                    html.classList.add('dark');
                }}
            }}
        }}

        // 一覧表示件数を適用
        function applyListLimit(limit) {{
            if (limit <= 0) return; // 全件表示

            const tables = document.querySelectorAll('table tbody');
            tables.forEach(tbody => {{
                const rows = tbody.querySelectorAll('tr');
                rows.forEach((row, index) => {{
                    row.style.display = index < limit ? '' : 'none';
                }});
            }});
        }}

        // フォームの値を復元
        function restoreFormValues(settings) {{
            const themeSelect = document.getElementById('theme');
            const listLimitSelect = document.getElementById('listLimit');

            if (themeSelect) {{
                themeSelect.value = settings.theme;
            }}
            if (listLimitSelect) {{
                listLimitSelect.value = settings.listLimit;
            }}
        }}

        // トースト表示
        function showToast(message) {{
            const toast = document.getElementById('copyToast');
            toast.textContent = message;
            toast.classList.add('show');
            setTimeout(() => toast.classList.remove('show'), 2000);
        }}

        // クリップボードにコピー
        function copyToClipboard(text) {{
            navigator.clipboard.writeText(text).then(() => {{
                showToast('コピーしました');
            }});
        }}

        // システムテーマ変更を監視
        if (window.matchMedia) {{
            window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {{
                const settings = loadDashboardSettings();
                if (settings.theme === 'system') {{
                    applyTheme('system');
                }}
            }});
        }}

        // ページ読み込み時に設定を適用
        (function() {{
            const settings = loadDashboardSettings();
            applySettings(settings);
            restoreFormValues(settings);
        }})();
    </script>
</body>
</html>"""


class DashboardHandler(SimpleHTTPRequestHandler):
    """ダッシュボード用HTTPハンドラ"""

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/api/config":
            # 設定読み込みAPI
            config_name = query.get("name", [None])[0]
            self._handle_config_get(config_name)
            return
        elif parsed.path == "/api/workflow/progress":
            wf_id = query.get("id", [None])[0]
            if not wf_id:
                self._send_json_response({"running": False})
                return
            running = is_workflow_running(wf_id)
            substep = _get_substep_progress(wf_id) if running else None
            self._send_json_response({
                "running": running,
                "substep": substep,
            })
            return
        elif parsed.path == "/api/workflow/running-count":
            count = len(_get_running_identifiers())
            self._send_json_response({"count": count})
            return
        elif parsed.path == "/api/configs":
            # 設定ファイル一覧API
            self._handle_configs_list()
            return
        elif parsed.path == "/settings":
            # 設定ページ
            config_files = list_config_files()
            content = render_settings_page(config_files)
            title = "設定"
        elif parsed.path == "/rulebook":
            # ルールブックページ
            selected_config = query.get("config", [None])[0]
            content = render_rulebook_page(selected_config)
            title = "Rulebook"
        elif parsed.path == "/prompts":
            # LLM指示文ページ
            content = render_prompts_page()
            title = "LLM指示文"
        elif parsed.path == "/api/prompts":
            # プロンプト一覧API
            self._handle_prompts_list()
            return
        elif parsed.path.startswith("/api/prompts/"):
            # プロンプト詳細取得API
            prompt_id = parsed.path[len("/api/prompts/"):]
            from urllib.parse import unquote
            prompt_id = unquote(prompt_id)
            self._handle_prompt_get(prompt_id)
            return
        elif "id" in query:
            # 詳細ページ
            workflow_id = query["id"][0]
            state = get_workflow_detail(workflow_id)
            if state:
                wf_running = is_workflow_running(workflow_id)
                content = render_detail_page(state, bg_running=wf_running)
                title = state.get("task_title", "詳細")
            else:
                content = "<p>ワークフローが見つかりません</p>"
                title = "Not Found"
        else:
            # 一覧ページ
            workflows = get_workflows()
            config_files = list_config_files()
            # バックグラウンド実行中のワークフローがあれば自動リロード
            any_running = bool(_get_running_identifiers())
            auto_reload_script = ""
            if any_running:
                auto_reload_script = """
                <script>
                setInterval(async () => {
                    try {
                        const resp = await fetch('/api/workflow/running-count');
                        const data = await resp.json();
                        if (data.count === 0) { location.reload(); }
                    } catch(e) {}
                }, 5000);
                </script>
                """
            content = f"""
            {render_step_controls(config_files)}
            <div class="card">
                <a href="/rulebook">レビュールールブックを表示 →</a>
            </div>
            <div class="card">
                <h3>ワークフロー一覧（{len(workflows)}件）</h3>
                <table class="workflow-list-table">
                    <thead><tr><th>ID</th><th>リポジトリ</th><th>タスク</th><th class="col-progress">進捗</th><th>最終更新</th><th class="col-actions">操作</th></tr></thead>
                    <tbody>{render_workflow_list(workflows)}</tbody>
                </table>
            </div>
            {auto_reload_script}
            """
            title = "HOKUS AI Dashboard"

        html = render_html(content, title)

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _handle_config_get(self, config_name: str | None):
        """設定読み込みGETリクエストを処理する

        レスポンス形式:
            成功時: {"success": true, "data": {...}}
            失敗時: {"success": false, "errors": ["エラー1"]}
        """
        if not config_name:
            self._send_json_response(
                {"success": False, "errors": ["config name が指定されていません"]}
            )
            return

        try:
            data = load_config_yaml(config_name)
            self._send_json_response({"success": True, "data": data})
        except FileNotFoundError as e:
            self._send_json_response(
                {"success": False, "errors": [str(e)]}, status_code=404
            )
        except yaml.YAMLError as e:
            self._send_json_response(
                {"success": False, "errors": [f"YAMLパースエラー: {e}"]}
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"読み込みに失敗しました: {e}"]}
            )

    def _handle_configs_list(self):
        """設定ファイル一覧を返す

        レスポンス形式:
            {"success": true, "configs": ["my-project", "another-project", ...]}
        """
        configs = list_config_files()
        self._send_json_response({"success": True, "configs": configs})

    def do_POST(self):
        """POSTリクエストを処理する"""
        parsed = urlparse(self.path)

        if parsed.path == "/api/workflow/start-step":
            self._handle_start_step_post()
        elif parsed.path == "/api/workflow/start-auto":
            self._handle_start_auto_post()
        elif parsed.path == "/api/workflow/continue-step":
            self._handle_continue_step_post()
        elif parsed.path == "/api/workflow/continue-auto":
            self._handle_continue_auto_post()
        elif parsed.path == "/api/workflow/delete":
            self._handle_delete_workflow_post()
        elif parsed.path == "/api/workflow/retry-phase":
            self._handle_retry_phase_post()
        elif parsed.path == "/api/workflow/apply-cross-review-fixes":
            self._handle_apply_cross_review_fixes_post()
        elif parsed.path == "/api/workflow/rerun-cross-review":
            self._handle_rerun_cross_review_post()
        elif parsed.path == "/api/workflow/continue-ignore-cross-review":
            self._handle_continue_ignore_cross_review_post()
        elif parsed.path == "/api/workflow/phase-decision":
            self._handle_phase_decision_post()
        elif parsed.path == "/settings":
            self._handle_settings_post()
        elif parsed.path == "/api/config/validate":
            self._handle_config_validate_post()
        elif parsed.path == "/api/workflow/pr-review-action":
            self._handle_pr_review_action_post()
        elif parsed.path == "/api/workflow/retry-notion":
            self._handle_retry_notion_post()
        elif parsed.path.startswith("/api/prompts/"):
            from urllib.parse import unquote
            prompt_id = unquote(parsed.path[len("/api/prompts/"):])
            self._handle_prompt_save(prompt_id)
        else:
            self.send_error(404, "Not Found")

    def _read_json_body(self) -> dict:
        """JSONリクエストボディを読み込む。"""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        return json.loads(body.decode("utf-8"))

    def _handle_start_step_post(self):
        """stepモードでワークフローを開始する。"""
        try:
            request_data = self._read_json_body()
            task_url = request_data.get("task_url", "").strip()
            config_name = request_data.get("config_name")

            if not task_url:
                self._send_json_response(
                    {"success": False, "errors": ["task_url が指定されていません"]},
                    status_code=400,
                )
                return

            result = start_workflow_step_mode(task_url, config_name)
            self._send_json_response(result, status_code=(202 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_start_auto_post(self):
        """自動モードでワークフローを開始する。"""
        try:
            request_data = self._read_json_body()
            task_url = request_data.get("task_url", "").strip()
            config_name = request_data.get("config_name")

            if not task_url:
                self._send_json_response(
                    {"success": False, "errors": ["task_url が指定されていません"]},
                    status_code=400,
                )
                return

            result = start_workflow_auto_mode(task_url, config_name)
            self._send_json_response(result, status_code=(202 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_continue_step_post(self):
        """stepモードでワークフローを1フェーズ進める。"""
        try:
            request_data = self._read_json_body()
            workflow_id = request_data.get("workflow_id", "").strip()
            action = request_data.get("action")
            if not workflow_id:
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id が指定されていません"]},
                    status_code=400,
                )
                return

            result = continue_workflow_step_mode(workflow_id, action=action)
            self._send_json_response(result, status_code=(202 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_continue_auto_post(self):
        """自動モードでワークフローを継続する。"""
        try:
            request_data = self._read_json_body()
            workflow_id = request_data.get("workflow_id", "").strip()
            action = request_data.get("action")
            if not workflow_id:
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id が指定されていません"]},
                    status_code=400,
                )
                return

            result = continue_workflow_auto_mode(workflow_id, action=action)
            self._send_json_response(result, status_code=(202 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_retry_phase_post(self):
        """指定フェーズ以降をリセットしてリトライ可能にする。"""
        try:
            request_data = self._read_json_body()
            workflow_id = request_data.get("workflow_id", "").strip()
            from_phase = request_data.get("from_phase")

            if not workflow_id:
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id が指定されていません"]},
                    status_code=400,
                )
                return
            if not isinstance(from_phase, int):
                self._send_json_response(
                    {"success": False, "errors": ["from_phase（整数）が指定されていません"]},
                    status_code=400,
                )
                return

            result = retry_phase(workflow_id, from_phase)
            self._send_json_response(
                result, status_code=(200 if result.get("success") else 400),
            )
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_retry_notion_post(self):
        """スキップされた Notion アクションをリトライする。"""
        try:
            request_data = self._read_json_body()
            workflow_id = request_data.get("workflow_id", "").strip()
            if not workflow_id:
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id が指定されていません"]},
                    status_code=400,
                )
                return

            result = retry_notion_actions(workflow_id)
            self._send_json_response(
                result, status_code=(200 if result.get("success") else 400),
            )
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_apply_cross_review_fixes_post(self):
        """クロスレビュー指摘を LLM で修正する。"""
        try:
            data = self._read_json_body()
            workflow_id = data.get("workflow_id", "").strip()
            phase = data.get("phase")
            if not workflow_id or not isinstance(phase, int):
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id と phase（整数）が必要です"]}, status_code=400)
                return
            result = apply_cross_review_fixes(workflow_id, phase)
            self._send_json_response(result, status_code=(200 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response({"success": False, "errors": ["JSONパースエラー"]}, status_code=400)
        except Exception as e:
            self._send_json_response({"success": False, "errors": [f"予期しないエラー: {e}"]}, status_code=500)

    def _handle_rerun_cross_review_post(self):
        """クロスレビューを再実行する。"""
        try:
            data = self._read_json_body()
            workflow_id = data.get("workflow_id", "").strip()
            phase = data.get("phase")
            if not workflow_id or not isinstance(phase, int):
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id と phase（整数）が必要です"]}, status_code=400)
                return
            result = rerun_cross_review_for_phase(workflow_id, phase)
            self._send_json_response(result, status_code=(200 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response({"success": False, "errors": ["JSONパースエラー"]}, status_code=400)
        except Exception as e:
            self._send_json_response({"success": False, "errors": [f"予期しないエラー: {e}"]}, status_code=500)

    def _handle_continue_ignore_cross_review_post(self):
        """クロスレビュー指摘を無視して続行する。"""
        try:
            data = self._read_json_body()
            workflow_id = data.get("workflow_id", "").strip()
            phase = data.get("phase")
            if not workflow_id or not isinstance(phase, int):
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id と phase（整数）が必要です"]}, status_code=400)
                return
            result = continue_ignoring_cross_review(workflow_id, phase)
            self._send_json_response(result, status_code=(200 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response({"success": False, "errors": ["JSONパースエラー"]}, status_code=400)
        except Exception as e:
            self._send_json_response({"success": False, "errors": [f"予期しないエラー: {e}"]}, status_code=500)

    def _handle_phase_decision_post(self):
        """Phase 2-4 の人間判断を記録する。"""
        try:
            data = self._read_json_body()
            workflow_id = data.get("workflow_id", "").strip()
            phase = data.get("phase")
            decision = (data.get("decision") or "").strip()
            if not workflow_id or not isinstance(phase, int) or not decision:
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id / phase（整数） / decision が必要です"]},
                    status_code=400,
                )
                return
            result = submit_phase_decision(workflow_id, phase, decision)
            self._send_json_response(result, status_code=(200 if result.get("success") else 400))
        except json.JSONDecodeError:
            self._send_json_response({"success": False, "errors": ["JSONパースエラー"]}, status_code=400)
        except Exception as e:
            self._send_json_response({"success": False, "errors": [f"予期しないエラー: {e}"]}, status_code=500)

    def _handle_delete_workflow_post(self):
        """ワークフローを削除する。"""
        try:
            request_data = self._read_json_body()
            workflow_id = request_data.get("workflow_id", "").strip()
            if not workflow_id:
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id が指定されていません"]},
                    status_code=400,
                )
                return

            store = _get_store()
            state = store.load_workflow(workflow_id)
            if state is None:
                self._send_json_response(
                    {"success": False, "errors": [f"ワークフローが見つかりません: {workflow_id}"]},
                    status_code=404,
                )
                return

            _clear_checkpoint(workflow_id)
            store.delete_workflow(workflow_id)
            self._send_json_response({
                "success": True,
                "message": f"ワークフロー {workflow_id} を削除しました",
            })
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_settings_post(self):
        """設定保存POSTリクエストを処理する

        リクエスト形式:
            {"config_name": "xxx", "data": {...}}

        レスポンス形式:
            成功時: {"success": true, "message": "保存しました"}
            失敗時: {"success": false, "errors": ["エラー1", "エラー2"]}
        """
        try:
            request_data = self._read_json_body()

            config_name = request_data.get("config_name")
            data = request_data.get("data")

            # 基本的なバリデーション
            if not config_name:
                self._send_json_response(
                    {"success": False, "errors": ["config_name が指定されていません"]}
                )
                return

            if data is None or not isinstance(data, dict):
                self._send_json_response(
                    {"success": False, "errors": ["data が不正です"]}
                )
                return

            # 設定値のバリデーション
            is_valid, errors, warnings = validate_config(data)
            if not is_valid:
                self._send_json_response({"success": False, "errors": errors, "warnings": warnings})
                return

            # 保存
            try:
                save_config_yaml(config_name, data)
                resp: dict = {"success": True, "message": "保存しました"}
                if warnings:
                    resp["warnings"] = warnings
                self._send_json_response(resp)
            except FileNotFoundError as e:
                self._send_json_response(
                    {"success": False, "errors": [str(e)]}
                )
            except yaml.YAMLError as e:
                self._send_json_response(
                    {"success": False, "errors": [f"YAML形式エラー: {e}"]}
                )
            except Exception as e:
                self._send_json_response(
                    {"success": False, "errors": [f"保存に失敗しました: {e}"]}
                )

        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]}
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]}
            )

    def _handle_config_validate_post(self):
        """設定の事前検証のみ実行する（保存しない）。"""
        try:
            request_data = self._read_json_body()
            data = request_data.get("data")
            if not isinstance(data, dict):
                self._send_json_response(
                    {"success": False, "errors": ["data が不正です"]}
                )
                return

            is_valid, errors, warnings = validate_config(data)
            self._send_json_response({
                "success": is_valid,
                "errors": errors,
                "warnings": warnings,
            })
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]}
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"検証エラー: {e}"]}
            )

    def _handle_pr_review_action_post(self):
        """PR個別レビューアクションを処理する。"""
        try:
            request_data = self._read_json_body()
            workflow_id = request_data.get("workflow_id", "").strip()
            pr_index = request_data.get("pr_index")
            action = request_data.get("action", "").strip()

            if not workflow_id:
                self._send_json_response(
                    {"success": False, "errors": ["workflow_id が指定されていません"]},
                    status_code=400,
                )
                return
            if not action:
                self._send_json_response(
                    {"success": False, "errors": ["action が指定されていません"]},
                    status_code=400,
                )
                return

            result = handle_pr_review_action(workflow_id, pr_index, action)
            status_code = 202 if result.get("success") else 400
            self._send_json_response(result, status_code=status_code)
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]},
                status_code=400,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [f"予期しないエラー: {e}"]},
                status_code=500,
            )

    def _handle_prompts_list(self):
        """プロンプト一覧APIハンドラ"""
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from hokusai.prompts import list_prompts
            prompts = list_prompts()
            self._send_json_response({"success": True, "data": prompts})
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [str(e)]}, status_code=500
            )

    def _handle_prompt_get(self, prompt_id: str):
        """プロンプト詳細取得APIハンドラ"""
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from hokusai.prompts import read_prompt_file
            content = read_prompt_file(prompt_id)
            self._send_json_response({"success": True, "data": {"id": prompt_id, "content": content}})
        except KeyError:
            self._send_json_response(
                {"success": False, "errors": [f"Unknown prompt ID: {prompt_id}"]},
                status_code=404,
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [str(e)]}, status_code=500
            )

    def _handle_prompt_save(self, prompt_id: str):
        """プロンプト保存APIハンドラ"""
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from hokusai.prompts import write_prompt_file
            request_data = self._read_json_body()
            content = request_data.get("content", "")
            write_prompt_file(prompt_id, content)
            self._send_json_response({"success": True, "message": "保存しました"})
        except KeyError:
            self._send_json_response(
                {"success": False, "errors": [f"Unknown prompt ID: {prompt_id}"]},
                status_code=404,
            )
        except ValueError as e:
            self._send_json_response(
                {"success": False, "errors": [str(e)]}, status_code=400
            )
        except json.JSONDecodeError:
            self._send_json_response(
                {"success": False, "errors": ["JSONパースエラー"]}, status_code=400
            )
        except Exception as e:
            self._send_json_response(
                {"success": False, "errors": [str(e)]}, status_code=500
            )

    def _send_json_response(self, data: dict, status_code: int = 200):
        """JSONレスポンスを送信する"""
        response_body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format, *args):
        # ログを抑制
        pass


def init_db():
    """データベースが存在しない場合、空のDBを作成"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id TEXT PRIMARY KEY,
                task_url TEXT NOT NULL,
                task_title TEXT,
                branch_name TEXT,
                current_phase INTEGER DEFAULT 1,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
    print(f"データベースを作成しました: {DB_PATH}")


def main():
    if not DB_PATH.exists():
        init_db()

    url = f"http://localhost:{PORT}"
    print("HOKUS AI Dashboard を起動中...")
    print(f"URL: {url}")
    print("終了するには Ctrl+C を押してください")

    # ブラウザを開く
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"⚠️ ブラウザ自動起動に失敗: {e}")

    # マルチスレッドサーバー（長時間 subprocess が他リクエストをブロックしないようにする）
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingHTTPServer(("localhost", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nシャットダウン中...")
        server.shutdown()


if __name__ == "__main__":
    main()
