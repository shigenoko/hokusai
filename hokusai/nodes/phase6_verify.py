"""
Phase 6: プログラム評価・検証

- build
- test
- lint

テスト失敗時にエラー内容を分析し、環境問題の場合は
ユーザーに選択肢を提示してHuman-in-the-loop状態に移行する。
"""

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..config import get_config
from ..logging_config import get_logger
from ..state import (
    PhaseStatus,
    RepositoryPhaseStatus,
    VerificationErrorEntry,
    VerificationResult,
    WorkflowState,
    add_audit_log,
    get_repository_state,
    init_repository_state,
    should_skip_phase,
    update_phase_status,
    update_repository_phase_status,
)
from ..utils.repo_resolver import resolve_runtime_repositories
from ..utils.shell import ShellRunner

logger = get_logger("phase6")


class FailureType(Enum):
    """テスト失敗の種類"""
    CODE_ERROR = "code_error"  # コードの問題
    ENVIRONMENT_ERROR = "environment_error"  # 環境の問題
    UNKNOWN = "unknown"  # 不明


@dataclass
class CommandResult:
    """コマンド実行結果"""
    success: bool
    stdout: str
    stderr: str
    return_code: int
    timed_out: bool = False


@dataclass
class FailureAnalysis:
    """失敗分析結果"""
    failure_type: FailureType
    summary: str
    details: str
    suggested_fix: Optional[str] = None
    can_auto_fix: bool = False
    port_to_kill: Optional[int] = None
    process_to_kill: Optional[int] = None


# 環境問題を検出するパターン
ENVIRONMENT_ERROR_PATTERNS = [
    # ポート競合
    {
        "pattern": r"Port (\d+) is not open|port (\d+).*taken|EADDRINUSE.*:(\d+)",
        "type": FailureType.ENVIRONMENT_ERROR,
        "summary": "ポート競合",
        "details_template": "ポート {port} が既に使用中です",
        "suggested_fix": "該当ポートを使用しているプロセスを終了してください",
        "extract_port": True,
    },
    # エミュレータ関連
    {
        "pattern": r"Could not start.*[Ee]mulator|[Ee]mulator.*failed to start|firebase.*emulator.*error",
        "type": FailureType.ENVIRONMENT_ERROR,
        "summary": "エミュレータ起動失敗",
        "details_template": "Firebase Emulatorの起動に失敗しました",
        "suggested_fix": "既存のエミュレータプロセスを終了してください",
    },
    # Docker関連
    {
        "pattern": r"Cannot connect to the Docker daemon|docker.*not running|Is the docker daemon running",
        "type": FailureType.ENVIRONMENT_ERROR,
        "summary": "Docker未起動",
        "details_template": "Dockerデーモンが起動していません",
        "suggested_fix": "Docker Desktopを起動してください",
    },
    # ネットワーク関連
    {
        "pattern": r"ECONNREFUSED|ETIMEDOUT|getaddrinfo.*ENOTFOUND",
        "type": FailureType.ENVIRONMENT_ERROR,
        "summary": "ネットワーク接続エラー",
        "details_template": "ネットワーク接続に問題があります",
        "suggested_fix": "ネットワーク接続を確認してください",
    },
    # メモリ不足
    {
        "pattern": r"ENOMEM|JavaScript heap out of memory|Killed.*out of memory",
        "type": FailureType.ENVIRONMENT_ERROR,
        "summary": "メモリ不足",
        "details_template": "メモリが不足しています",
        "suggested_fix": "不要なアプリケーションを終了してメモリを解放してください",
    },
    # パーミッション関連
    {
        "pattern": r"EACCES|Permission denied|access denied",
        "type": FailureType.ENVIRONMENT_ERROR,
        "summary": "パーミッションエラー",
        "details_template": "ファイルまたはリソースへのアクセス権限がありません",
        "suggested_fix": "ファイルのパーミッションを確認してください",
    },
]


def phase6_verify_node(state: WorkflowState) -> WorkflowState:
    """Phase 6: プログラム評価・検証"""

    # スキップチェック
    if should_skip_phase(state, 6):
        print("⏭️  Phase 6 スキップ: 検証済み")
        return state

    # 後続フェーズが完了済みの場合もスキップ（ワークフロー再開時の対応）
    phases = state.get("phases", {})
    phase7_status = phases.get(7, {}).get("status", "")
    phase8_status = phases.get(8, {}).get("status", "")
    if phase7_status == PhaseStatus.COMPLETED.value or phase8_status in [PhaseStatus.COMPLETED.value, PhaseStatus.IN_PROGRESS.value]:
        print("⏭️  Phase 6 スキップ: 後続フェーズ（レビュー・PR）が完了済み")
        state = update_phase_status(state, 6, PhaseStatus.COMPLETED)
        state["current_phase"] = 7
        return state

    state = update_phase_status(state, 6, PhaseStatus.IN_PROGRESS)

    try:
        config = get_config()

        # 対象リポジトリを取得（worktree path を含むランタイム情報）
        target_repositories = resolve_runtime_repositories(state, config)
        if not target_repositories:
            raise ValueError("有効な対象リポジトリがありません")

        total_repos = len(target_repositories)
        failed_results = []  # 失敗した結果を蓄積
        verification_errors: list[VerificationErrorEntry] = []  # エラー詳細

        # 検証ステータスの初期化 (全ての項目がパスすれば成功)
        verification_status = {
            "build": VerificationResult.PASS.value,
            "test": VerificationResult.PASS.value,
            "lint": VerificationResult.PASS.value,
        }

        print(f"🔍 検証を開始します ({total_repos}リポジトリ)")

        # リポジトリ状態を初期化（存在しない場合）
        for repo in target_repositories:
            if not get_repository_state(state, repo.name):
                repo_state = init_repository_state(
                    name=repo.name,
                    path=str(repo.path),
                    branch=state.get("branch_name", ""),
                    base_branch=repo.base_branch,
                    source_path=str(repo.source_path),
                    worktree_created=repo.worktree_created,
                )
                if "repositories" not in state:
                    state["repositories"] = []
                state["repositories"].append(repo_state)

        for idx, repo in enumerate(target_repositories, 1):
            # 既に完了済みのリポジトリはスキップ（リトライ時）
            repo_state = get_repository_state(state, repo.name)
            if repo_state:
                phase6_status = repo_state.get("phase_status", {}).get(6)
                if phase6_status == RepositoryPhaseStatus.COMPLETED.value:
                    print(f"\n📦 [{idx}/{total_repos}] {repo.name} はスキップ（検証済み）")
                    continue

            print(f"\n📦 [{idx}/{total_repos}] {repo.name} の検証中...")
            cwd = str(repo.path)
            repo_has_failure = False

            # コマンドの決定 (リポジトリ固有設定 > 共通設定)
            cmds = {
                "build": repo.build_command or config.build_command,
                "test": repo.test_command or config.test_command,
                "lint": repo.lint_command or config.lint_command,
            }

            for cmd_type, cmd in cmds.items():
                if not cmd:
                    # 設定ミスを見逃さないため warning を出す
                    logger.warning(f"{repo.name}:{cmd_type} のコマンドが未設定のためスキップ")
                    continue

                emoji = {"build": "🔨", "test": "🧪", "lint": "🔍"}.get(cmd_type, "▶️")
                print(f"   {emoji} {cmd_type}: {cmd}")
                result = _run_command_with_output(cmd, cwd, config.command_timeout)

                # エラー詳細を記録
                error_output = None
                if not result.success:
                    # エラー出力を最大500行に制限
                    combined_output = (result.stdout + "\n" + result.stderr).strip()
                    lines = combined_output.split("\n")
                    if len(lines) > 500:
                        error_output = "\n".join(lines[:500]) + f"\n... ({len(lines) - 500} lines truncated)"
                    else:
                        error_output = combined_output

                verification_errors.append(VerificationErrorEntry(
                    repository=repo.name,
                    command=cmd_type,
                    success=result.success,
                    error_output=error_output,
                ))

                if not result.success:
                    verification_status[cmd_type] = VerificationResult.FAIL.value
                    repo_has_failure = True
                    # 失敗結果を保存 (リポジトリ名をキーに含める)
                    failed_results.append((f"{repo.name}:{cmd_type}", result))
                    print("      ❌ 失敗")
                else:
                    print("      ✅ 成功")

            # リポジトリ別ステータスを記録
            if repo_has_failure:
                state = update_repository_phase_status(
                    state, repo.name, 6, RepositoryPhaseStatus.FAILED
                )
            else:
                state = update_repository_phase_status(
                    state, repo.name, 6, RepositoryPhaseStatus.COMPLETED
                )

        # 結果の保存
        state["verification"] = verification_status
        state["verification_errors"] = verification_errors

        # 結果判定 (どれか一つでも失敗していれば FAIL)
        all_passed = all(v == VerificationResult.PASS.value for v in verification_status.values())

        if all_passed:
            state = update_phase_status(state, 6, PhaseStatus.COMPLETED)
            print(f"\n✅ Phase 6 完了: 全ての検証に成功しました ({total_repos}リポジトリ)")
        else:
            # エラー分析 (failed_results にリポジトリ名が含まれているため、ログでも区別可能)
            analysis = _analyze_failures(failed_results)

            if analysis and analysis.failure_type == FailureType.ENVIRONMENT_ERROR:
                # 環境問題の場合、ユーザーに選択肢を提示
                state = _handle_environment_error(state, analysis, failed_results)
            else:
                # コードの問題の場合、通常のリトライフロー
                state["phases"][6]["retry_count"] += 1
                state["total_retry_count"] += 1
                failed = [k for k, v in state["verification"].items()
                         if v == VerificationResult.FAIL.value]
                failed_details = [name for name, _ in failed_results]
                print(f"\n⚠️ Phase 6: 検証失敗 - {', '.join(failed)}")
                print(f"   詳細: {', '.join(failed_details)}")

                # fail-close: リトライ上限到達時はワークフローを停止
                if state["phases"][6]["retry_count"] >= config.max_retry_count:
                    state["waiting_for_human"] = True
                    state["human_input_request"] = "verification_max_retry"
                    state = update_phase_status(state, 6, PhaseStatus.FAILED)
                    print(f"🛑 検証リトライ上限({config.max_retry_count}回)に到達。ワークフローを停止します。")
                    print("   続行するには: workflow continue <id> --action force-continue")

        state = add_audit_log(state, 6, "verification_completed",
            "success" if all_passed else "failed", {
            "build": state["verification"]["build"],
            "test": state["verification"]["test"],
            "lint": state["verification"]["lint"],
            "repository_count": total_repos,
            "failed_details": [name for name, _ in failed_results] if failed_results else [],
        })

    except Exception as e:
        state = update_phase_status(state, 6, PhaseStatus.FAILED, str(e))
        state = add_audit_log(state, 6, "phase_failed", "error", error=str(e))
        print(f"❌ Phase 6 失敗: {e}")
        raise

    return state


def _run_command_with_output(command: str, cwd: str, timeout: int) -> CommandResult:
    """コマンドを実行して結果を返す（出力含む）"""
    try:
        # shell=Trueの場合、shで実行
        shell = ShellRunner(cwd=cwd)
        result = shell.run(["sh", "-c", command], timeout=timeout)
        return CommandResult(
            success=result.success,
            stdout=result.stdout,
            stderr=result.stderr,
            return_code=result.returncode,
        )
    except subprocess.TimeoutExpired as e:
        print(f"⚠️ タイムアウト: {command}")
        return CommandResult(
            success=False,
            stdout=e.stdout or "" if hasattr(e, 'stdout') else "",
            stderr=e.stderr or "" if hasattr(e, 'stderr') else "",
            return_code=-1,
            timed_out=True,
        )
    except Exception as e:
        print(f"⚠️ エラー: {e}")
        return CommandResult(
            success=False,
            stdout="",
            stderr=str(e),
            return_code=-1,
        )


def _analyze_failures(failed_results: list[tuple[str, CommandResult]]) -> Optional[FailureAnalysis]:
    """
    失敗したコマンドの出力を分析してエラー原因を特定

    Args:
        failed_results: (コマンド名, 結果) のタプルリスト

    Returns:
        分析結果（環境問題が検出された場合）
    """
    for cmd_name, result in failed_results:
        combined_output = result.stdout + "\n" + result.stderr

        for pattern_info in ENVIRONMENT_ERROR_PATTERNS:
            match = re.search(pattern_info["pattern"], combined_output, re.IGNORECASE)
            if match:
                details = pattern_info["details_template"]
                port_to_kill = None

                # ポート番号を抽出
                if pattern_info.get("extract_port"):
                    for group in match.groups():
                        if group and group.isdigit():
                            port_to_kill = int(group)
                            details = details.format(port=port_to_kill)
                            break

                logger.info(f"環境問題を検出: {pattern_info['summary']} ({cmd_name})")

                return FailureAnalysis(
                    failure_type=pattern_info["type"],
                    summary=pattern_info["summary"],
                    details=details,
                    suggested_fix=pattern_info.get("suggested_fix"),
                    port_to_kill=port_to_kill,
                )

    return None


def _handle_environment_error(
    state: WorkflowState,
    analysis: FailureAnalysis,
    failed_results: list[tuple[str, CommandResult]],
) -> WorkflowState:
    """
    環境問題が検出された場合の処理

    ユーザーに選択肢を提示してHuman-in-the-loop状態に移行
    """
    failed_cmds = [name for name, _ in failed_results]

    # ポート競合の場合、プロセス情報を取得
    # Note: この lsof 呼び出しはGit操作ではなくポート使用状況の確認であり、
    # 検証ドメインに属するためここに残す（GitClientへの移行対象外）
    port_info = ""
    if analysis.port_to_kill:
        try:
            shell = ShellRunner()
            result = shell.run(["lsof", "-i", f":{analysis.port_to_kill}"])
            if result.stdout:
                port_info = f"\n\nポート {analysis.port_to_kill} を使用中のプロセス:\n{result.stdout}"
        except Exception:
            pass

    # 選択肢メッセージを構築
    message = f"""
╔══════════════════════════════════════════════════════════════════╗
║  ⚠️ 環境問題が検出されました                                      ║
╚══════════════════════════════════════════════════════════════════╝

【検出された問題】
  種類: {analysis.summary}
  詳細: {analysis.details}
  失敗したコマンド: {', '.join(failed_cmds)}
{port_info}

【推奨される対処】
  {analysis.suggested_fix}

【選択肢】
  1. 問題を手動で解決してワークフローを再開
     → 問題を解決後: workflow continue <workflow_id>

  2. テスト失敗を無視して続行（非推奨）
     → HOKUSAI_SKIP_TEST=1 workflow continue <workflow_id>

  3. ワークフローを中止
     → 何もしない（後で再開可能）

ワークフローID: {state['workflow_id']}
"""

    print(message)
    logger.info(f"環境問題検出: {analysis.summary} - Human-in-the-loop移行")

    # Human-in-the-loop状態に設定
    state["waiting_for_human"] = True
    state["human_input_request"] = "environment_error"

    # エラー情報をstateに保存（再開時に参照可能）
    state["last_environment_error"] = {
        "type": analysis.failure_type.value,
        "summary": analysis.summary,
        "details": analysis.details,
        "suggested_fix": analysis.suggested_fix,
        "port": analysis.port_to_kill,
        "failed_commands": failed_cmds,
    }

    # リトライカウントは増やさない（環境問題なので）
    state = add_audit_log(state, 6, "environment_error_detected", "warning", {
        "error_type": analysis.summary,
        "details": analysis.details,
        "failed_commands": failed_cmds,
    })

    return state


# 後方互換性のため、旧関数も残す
def _run_command(command: str, cwd: str, timeout: int) -> bool:
    """コマンドを実行して成功/失敗を返す（後方互換）"""
    result = _run_command_with_output(command, cwd, timeout)
    return result.success
