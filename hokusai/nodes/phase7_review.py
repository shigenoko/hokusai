"""
Phase 7: 最終レビュー

- /final-review スキルを実行
- 規約違反チェック
- レビューチェックリストによる追加検証
"""

import re
from pathlib import Path

from ..config import get_config
from ..integrations.claude_code import ClaudeCodeClient
from ..state import (
    PhaseStatus,
    RepositoryPhaseStatus,
    WorkflowState,
    add_audit_log,
    should_skip_phase,
    update_phase_status,
    update_repository_phase_status,
)
from ..utils.repo_resolver import resolve_runtime_repositories


def _load_builtin_checklist() -> str:
    """組み込みのレビューチェックリストを読み込む"""
    checklist_path = Path(__file__).parent.parent / "review_checklist.md"
    if checklist_path.exists():
        return checklist_path.read_text(encoding="utf-8")
    return ""


def _build_review_prompt(project_checklist: dict[str, dict]) -> str:
    """レビュー用のプロンプトを構築

    Args:
        project_checklist: プロジェクト固有ルール
            {"P01": {"name": "...", "description": "..."}, ...}
    """
    from ..prompts import get_prompt

    builtin_checklist = _load_builtin_checklist()

    # プロジェクト固有チェックリストを組み立て
    project_checklist_section = ""
    if project_checklist:
        parts = [
            "",
            "## プロジェクト固有のチェックリスト",
            "",
        ]
        for rule_id, rule_data in sorted(project_checklist.items()):
            name = rule_data.get("name", rule_id)
            description = rule_data.get("description", "")
            parts.append(f"### {rule_id}: {name}")
            if description and description != name:
                parts.append(f"- [ ] {description}")
            parts.append("")
        project_checklist_section = "\n".join(parts)

    return get_prompt(
        "phase7.final_review",
        builtin_checklist=builtin_checklist,
        project_checklist_section=project_checklist_section,
    )


def _extract_required_rule_ids(checklist_content: str) -> list[str]:
    """チェックリストから必須ルールIDを抽出

    ### HQ01: ... 形式のヘッダーからIDを抽出する。

    Returns:
        必須ルールIDのリスト（例: ["HQ01", "HQ02", ..., "UX02"]）
    """
    pattern = r'### ([A-Z]{2}\d{2}):'
    return re.findall(pattern, checklist_content)


def _validate_rule_completeness(
    parsed_rules: dict[str, dict],
    required_ids: list[str],
) -> list[str]:
    """必須ルールの欠落を検知

    Args:
        parsed_rules: パースされたルール結果
        required_ids: 必須ルールIDリスト

    Returns:
        欠落しているルールIDのリスト
    """
    parsed_ids = set(parsed_rules.keys())
    return [rid for rid in required_ids if rid not in parsed_ids]


def _get_scoped_project_rules(
    review_checklist: dict[str, dict],
    repo_name: str,
) -> list[str]:
    """リポジトリに該当するプロジェクト固有ルールIDを返す

    ルールの description 内の「対象:」行からリポジトリスコープを推定する。
    スコープが判定できないルールは全リポジトリに適用する。

    Args:
        review_checklist: プロジェクト固有ルール定義
        repo_name: リポジトリ名（例: "Backend", "API"）

    Returns:
        該当リポジトリ向けルールIDリスト
    """
    # リポジトリ名 → description 内のパス名マッピング
    # 対象行に含まれるパターンでリポジトリを判別
    # 注: プロジェクト固有のパターンが必要な場合は設定ファイルから読み込む形に拡張可能
    REPO_PATTERNS: dict[str, list[str]] = {}

    patterns = REPO_PATTERNS.get(repo_name, [])
    result = []

    for rule_id, rule_data in review_checklist.items():
        desc = rule_data.get("description", "")
        # 「対象:」行を探す
        target_line = ""
        for line in desc.split("\n"):
            if line.strip().startswith("対象:"):
                target_line = line
                break

        if not target_line or not patterns:
            # スコープ判定できない場合は全リポジトリに適用
            result.append(rule_id)
            continue

        # パターンマッチでスコープ判定
        if any(p in target_line for p in patterns):
            result.append(rule_id)

    return result


def _parse_rule_results(output: str) -> dict[str, dict]:
    """ルール別結果をパース

    Args:
        output: Claude Codeからの出力

    Returns:
        {
            "HQ01": {"name": "デッドコードと不要なインポート", "result": "OK", "note": ""},
            "SA01": {"name": "サーバーサイド認可", "result": "NG", "note": "権限チェック漏れ"},
            ...
        }
    """
    rules = {}

    # テーブル行を抽出
    # | HQ01 | デッドコードと不要なインポート | OK | - |
    # | P01 | ルール名 | NG | 備考 |
    pattern = r'\|\s*([A-Z]{2}\d{2}|P\d{2})\s*\|\s*(.+?)\s*\|\s*(OK|NG|SKIP)\s*\|\s*(.*?)\s*\|'

    for match in re.finditer(pattern, output):
        rule_id, name, result, note = match.groups()
        rules[rule_id] = {
            "name": name.strip(),
            "result": result,
            "note": note.strip() if note.strip() not in ["-", "−", "–", ""] else ""
        }

    return rules


def _parse_review_result(output: str) -> dict:
    """レビュー結果をパース

    判定ロジック:
    1. ルール結果ベース: NGが1つでもあれば不合格（最優先）
    2. キーワードベース: ルール結果が空の場合のみフォールバック
    3. 自由記述 issues は情報提供として記録するが、合否判定には使わない
    """
    issues = []

    # 問題点の抽出（情報提供用。合否判定には使わない）
    in_issues_section = False
    for line in output.split("\n"):
        line_stripped = line.strip()

        # 問題セクションの開始を検出
        if "検出された問題" in line or "問題点" in line or "違反" in line:
            in_issues_section = True
            continue

        # 別のセクションの開始で問題セクション終了
        if line_stripped.startswith("###") or line_stripped.startswith("## "):
            in_issues_section = False
            continue

        # 問題点の抽出
        if in_issues_section and line_stripped.startswith("-"):
            issue = line_stripped[1:].strip()
            # 無視すべき非問題マーカー
            non_issues = ["なし", "特になし", "None", "N/A", "--", "-", ""]
            if issue and issue not in non_issues and not issue.startswith("-"):
                issues.append(issue)

    # ルール別結果をパース
    rules = _parse_rule_results(output)

    # 判定ロジック: NG ルールの有無のみで判定
    if rules:
        # ルール結果がある場合: NGが1つでもあれば不合格
        passed = not any(r["result"] == "NG" for r in rules.values())
    else:
        # フォールバック: キーワードベース判定（ルール結果が取得できない場合のみ）
        passed = True
        failure_indicators = [
            "不合格",
            "違反があります",
            "問題があります",
            "問題が検出",
            "修正が必要",
        ]
        for indicator in failure_indicators:
            if indicator in output:
                passed = False
                break

    return {
        "passed": passed,
        "issues": issues,
        "rules": rules,
        "raw_output": output,
    }


def _review_single_repo(
    repo_name: str,
    repo_path: Path,
    review_prompt: str,
    timeout: int,
) -> dict:
    """単一リポジトリのレビューを実行

    Args:
        repo_name: リポジトリ名
        repo_path: リポジトリのパス
        review_prompt: レビュープロンプト
        timeout: タイムアウト秒数

    Returns:
        レビュー結果の辞書
    """
    claude = ClaudeCodeClient(working_dir=repo_path)
    output = claude.execute_prompt(review_prompt, timeout=timeout)
    result = _parse_review_result(output)
    return result


def _review_all_repositories(
    repositories: list,
    review_prompt: str,
    timeout: int,
) -> dict[str, dict]:
    """全リポジトリのレビューを実行し、リポジトリ名をキーとした結果辞書を返す"""
    review_by_repo: dict[str, dict] = {}
    for repo in repositories:
        repo_name = repo.name
        repo_path = repo.path

        if not repo_path.exists():
            print(f"   ⚠️ {repo_name}: パスが存在しません ({repo_path})")
            review_by_repo[repo_name] = {
                "passed": False,
                "issues": [f"リポジトリパスが存在しません: {repo_path}"],
                "rules": {},
                "error": "path_not_found",
            }
            continue

        print(f"   📂 {repo_name} をレビュー中...")

        try:
            result = _review_single_repo(
                repo_name=repo_name,
                repo_path=repo_path,
                review_prompt=review_prompt,
                timeout=timeout,
            )

            review_by_repo[repo_name] = {
                "passed": result["passed"],
                "issues": result["issues"],
                "rules": result["rules"],
            }

            # リポジトリ別のサマリーを表示
            repo_rules = result["rules"]
            ok_count = sum(1 for r in repo_rules.values() if r["result"] == "OK")
            total_count = len(repo_rules)
            status_emoji = "✅" if result["passed"] else "⚠️"
            print(f"      {status_emoji} {repo_name}: {ok_count}/{total_count} OK")

        except Exception as e:
            print(f"   ❌ {repo_name}: レビュー失敗 - {e}")
            review_by_repo[repo_name] = {
                "passed": False,
                "issues": [f"レビュー実行エラー: {e}"],
                "rules": {},
                "error": str(e),
            }

    return review_by_repo


def _aggregate_review_results(
    review_by_repo: dict[str, dict],
) -> dict:
    """リポジトリ別レビュー結果を集約する

    Args:
        review_by_repo: リポジトリ名をキーとしたレビュー結果

    Returns:
        集約結果:
            all_passed: 全リポジトリが合格したか
            all_issues: 全リポジトリの問題リスト（リポジトリ名付き）
            all_rules: 全リポジトリのルール結果（リポジトリ名付きキー）
    """
    all_issues: list[str] = []
    all_rules: dict[str, dict] = {}
    all_passed = True

    for repo_name, repo_result in review_by_repo.items():
        if not repo_result["passed"]:
            all_passed = False

        # 問題をリポジトリ名付きで追加
        for issue in repo_result["issues"]:
            all_issues.append(f"[{repo_name}] {issue}")

        # ルールをリポジトリ名付きで追加
        for rule_id, rule_data in repo_result.get("rules", {}).items():
            key = f"{repo_name}:{rule_id}"
            all_rules[key] = {
                "name": f"[{repo_name}] {rule_data['name']}",
                "result": rule_data["result"],
                "note": rule_data.get("note", ""),
            }

    # NG ルールを issues にも追加（Phase 5 リトライ時のプロンプトに反映するため）
    for key, rule_data in all_rules.items():
        if rule_data["result"] == "NG":
            note = rule_data.get("note", "")
            issue_text = f"{rule_data['name']}: {note}" if note else rule_data["name"]
            all_issues.append(issue_text)

    return {
        "all_passed": all_passed,
        "all_issues": all_issues,
        "all_rules": all_rules,
    }


def phase7_review_node(state: WorkflowState) -> WorkflowState:
    """Phase 7: 最終レビュー（複数リポジトリ対応）"""

    # スキップチェック
    if should_skip_phase(state, 7):
        print("⏭️  Phase 7 スキップ: レビュー済み")
        return state

    # 後続フェーズが進行中または完了済みの場合もスキップ（ワークフロー再開時の対応）
    phases = state.get("phases", {})
    phase8_status = phases.get(8, {}).get("status", "")
    if phase8_status in [PhaseStatus.COMPLETED.value, PhaseStatus.IN_PROGRESS.value]:
        print("⏭️  Phase 7 スキップ: Phase 8（PR）が進行中または完了済み")
        state = update_phase_status(state, 7, PhaseStatus.COMPLETED)
        state["current_phase"] = 8
        return state

    state = update_phase_status(state, 7, PhaseStatus.IN_PROGRESS)

    try:
        config = get_config()
        review_prompt = _build_review_prompt(config.review_checklist)
        repositories = resolve_runtime_repositories(state, config)

        print(f"📋 レビューチェックリストを使用してレビューを実行中... ({len(repositories)}リポジトリ)")

        # 全リポジトリのレビュー実行
        review_by_repo = _review_all_repositories(
            repositories, review_prompt, config.skill_timeout,
        )

        # 必須ルール完全性チェック (C2: 欠落検知)
        # リポジトリごとにスコープされたルールのみ要求する
        builtin_checklist = _load_builtin_checklist()
        required_builtin_ids = _extract_required_rule_ids(builtin_checklist)

        for repo_name, repo_result in review_by_repo.items():
            if repo_result.get("error"):
                continue  # エラーのリポジトリはスキップ

            # プロジェクト固有ルールはリポジトリスコープでフィルタ
            required_project_ids = _get_scoped_project_rules(
                config.review_checklist, repo_name
            )
            all_required_ids = required_builtin_ids + required_project_ids

            repo_rules = repo_result.get("rules", {})
            missing = _validate_rule_completeness(repo_rules, all_required_ids)
            if missing:
                missing_str = ", ".join(missing)
                # warning として記録（passed は変更しない）
                repo_result["issues"].append(f"必須ルール欠落(warning): {missing_str}")
                print(f"   ⚠️ {repo_name}: 必須ルール欠落(warning) - {missing_str}")

        # リポジトリ別のフェーズ状態を更新
        for repo_name, repo_result in review_by_repo.items():
            if repo_result.get("passed"):
                state = update_repository_phase_status(
                    state, repo_name, 7, RepositoryPhaseStatus.COMPLETED
                )
            else:
                state = update_repository_phase_status(
                    state, repo_name, 7, RepositoryPhaseStatus.FAILED
                )

        # 結果を集約
        aggregated = _aggregate_review_results(review_by_repo)
        all_passed = aggregated["all_passed"]
        all_issues = aggregated["all_issues"]
        all_rules = aggregated["all_rules"]

        # 結果を状態に保存
        state["final_review_passed"] = all_passed
        state["final_review_issues"] = all_issues
        state["final_review_rules"] = all_rules
        state["final_review_by_repo"] = review_by_repo

        # 全体のサマリーを計算
        total_rules = len(all_rules)
        ok_count = sum(1 for r in all_rules.values() if r["result"] == "OK")
        ng_count = sum(1 for r in all_rules.values() if r["result"] == "NG")

        if all_passed:
            state = update_phase_status(state, 7, PhaseStatus.COMPLETED)
            print(f"✅ Phase 7 完了: 全{len(repositories)}リポジトリのレビューに合格しました ({ok_count}/{total_rules} OK)")
        else:
            state["phases"][7]["retry_count"] += 1
            issues_count = len(all_issues)
            print(f"⚠️ Phase 7: 最終レビューで {issues_count} 件の問題が検出されました ({ng_count} ルール違反)")
            for issue in all_issues[:5]:  # 最初の5件のみ表示
                print(f"   - {issue}")
            if len(all_issues) > 5:
                print(f"   ... 他 {len(all_issues) - 5} 件")

            # fail-close: リトライ上限到達時はワークフローを停止
            if state["phases"][7]["retry_count"] >= config.max_retry_count:
                state["waiting_for_human"] = True
                state["human_input_request"] = "review_max_retry"
                state = update_phase_status(state, 7, PhaseStatus.FAILED)
                print(f"🛑 レビューリトライ上限({config.max_retry_count}回)に到達。ワークフローを停止します。")
                print("   続行するには: workflow continue <id> --action force-continue")

        state = add_audit_log(state, 7, "final_review_completed",
            "success" if all_passed else "failed", {
            "issues": all_issues,
            "checklist_used": True,
            "repositories_reviewed": list(review_by_repo.keys()),
            "rules_summary": {
                "total": total_rules,
                "ok": ok_count,
                "ng": ng_count,
            },
        })

    except Exception as e:
        state = update_phase_status(state, 7, PhaseStatus.FAILED, str(e))
        state = add_audit_log(state, 7, "phase_failed", "error", error=str(e))
        print(f"❌ Phase 7 失敗: {e}")
        raise

    return state
