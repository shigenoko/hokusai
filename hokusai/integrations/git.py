"""
Git Client

Gitリポジトリ操作を行うクライアント。
"""

import re
from pathlib import Path

from ..config import get_config
from ..constants import BRANCH_NAME_LIMIT
from ..logging_config import get_logger
from ..utils.shell import ShellError, ShellRunner

logger = get_logger("git")


class BranchReuseDenied(RuntimeError):
    """既存ブランチの base 整合チェックに失敗した場合の例外

    Attributes:
        branch_name: 対象ブランチ名
        base_branch: ベースブランチ名
        issues: 検出された問題のリスト
    """

    def __init__(self, branch_name: str, base_branch: str, issues: list[str]):
        self.branch_name = branch_name
        self.base_branch = base_branch
        self.issues = issues
        issues_text = "\n".join(f"  - {issue}" for issue in issues)
        super().__init__(
            f"既存ブランチ {branch_name} は origin/{base_branch} と構造差分があります。\n"
            f"{issues_text}\n\n"
            f"まず base を取り込んでください:\n"
            f"  git fetch origin {base_branch}\n"
            f"  git rebase origin/{base_branch}   または   "
            f"git merge origin/{base_branch}"
        )


class GitClient:
    """Gitリポジトリを操作するクライアント"""

    def __init__(self, repo_path: str | None = None):
        """
        初期化

        Args:
            repo_path: リポジトリのパス（省略時は設定のproject_root）
        """
        if repo_path:
            self.repo_path = Path(repo_path)
        else:
            config = get_config()
            self.repo_path = config.project_root

    def generate_branch_name(self, task_title: str) -> str:
        """
        タスクタイトルからブランチ名を生成

        日本語タイトルの場合はClaude Codeを使って英語のブランチ名を生成する。

        Args:
            task_title: タスクタイトル

        Returns:
            生成されたブランチ名（feature/xxx形式）
        """
        # 日本語が含まれているかチェック
        has_japanese = any("\u3040" <= c <= "\u9fff" for c in task_title)

        if has_japanese:
            # Claude Codeを使って英語ブランチ名を生成
            branch_suffix = self._generate_branch_name_with_ai(task_title)
            if branch_suffix:
                return f"feature/{branch_suffix}"

        # フォールバック: ASCIIベースの生成
        return self._generate_branch_name_ascii(task_title)

    def _generate_branch_name_with_ai(self, task_title: str) -> str | None:
        """
        Claude Codeを使ってタスクタイトルから英語ブランチ名を生成

        Args:
            task_title: タスクタイトル

        Returns:
            ブランチサフィックス（feature/を除く部分）、失敗時はNone
        """
        try:
            from .claude_code import ClaudeCodeClient

            claude = ClaudeCodeClient()
            prompt = f"""以下のタスクタイトルから、Gitブランチ名のサフィックスを生成してください。

タスクタイトル: {task_title}

要件:
- 英語の小文字とハイフンのみ使用
- 30文字以内
- タスクの本質的な機能を表す名前
- feature/ プレフィックスは不要（サフィックスのみ出力）

出力形式:
ブランチ名のサフィックスのみを1行で出力。説明は不要。

例:
- 「ユーザー認証機能を追加」→ add-user-auth
- 「商品一覧のページネーション対応」→ product-list-pagination
- 「ライブタグ編集機能を追加する」→ add-live-tag-edit
"""
            result = claude.execute_prompt(prompt, timeout=60)

            # 結果をクリーンアップ
            branch_suffix = result.strip().lower()
            # 複数行の場合は最初の行を使用
            branch_suffix = branch_suffix.split("\n")[0].strip()
            # 不正な文字を除去
            branch_suffix = re.sub(r"[^a-z0-9\-]", "-", branch_suffix)
            branch_suffix = re.sub(r"-+", "-", branch_suffix).strip("-")

            if branch_suffix and len(branch_suffix) <= BRANCH_NAME_LIMIT:
                logger.info(f"AI生成ブランチ名: feature/{branch_suffix}")
                return branch_suffix
            else:
                logger.warning(f"AI生成ブランチ名が不正: {branch_suffix}")
                return None

        except Exception as e:
            logger.warning(f"AI ブランチ名生成に失敗、フォールバック使用: {e}")
            return None

    def _generate_branch_name_ascii(self, task_title: str) -> str:
        """
        ASCIIベースでブランチ名を生成（フォールバック）

        Args:
            task_title: タスクタイトル

        Returns:
            生成されたブランチ名（feature/xxx形式）
        """
        import unicodedata

        # タイトルをASCIIに正規化（日本語は除去）
        normalized = unicodedata.normalize("NFKD", task_title)
        ascii_title = normalized.encode("ascii", "ignore").decode("ascii")

        # 空白をハイフンに、特殊文字を除去
        branch_suffix = re.sub(r"[^a-zA-Z0-9\-]", "-", ascii_title.lower())
        branch_suffix = re.sub(r"-+", "-", branch_suffix).strip("-")

        # 空の場合はタイムスタンプを使用
        if not branch_suffix:
            from datetime import datetime
            branch_suffix = datetime.now().strftime("%Y%m%d-%H%M%S")

        # 長すぎる場合は切り詰め
        if len(branch_suffix) > BRANCH_NAME_LIMIT:
            branch_suffix = branch_suffix[:BRANCH_NAME_LIMIT].rstrip("-")

        return f"feature/{branch_suffix}"

    def create_feature_branch(self, branch_name: str, base_branch: str | None = None) -> None:
        """
        フィーチャーブランチを作成

        Args:
            branch_name: 作成するブランチ名
            base_branch: ベースとなるブランチ（デフォルト: 設定のbase_branch）
        """
        if base_branch is None:
            config = get_config()
            base_branch = config.base_branch

        # 現在のブランチを確認
        current = self.get_current_branch()

        # 既に同じブランチにいる場合はスキップ
        if current == branch_name:
            print(f"🌿 既にブランチ {branch_name} にいます")
            return

        # ブランチが存在するか確認
        try:
            self._run_git("rev-parse", "--verify", branch_name)
            # 存在する場合はチェックアウト
            self._run_git("checkout", branch_name)
            print(f"🌿 既存のブランチ {branch_name} にチェックアウトしました")
        except ShellError:
            # 存在しない場合は新規作成
            self.create_branch(branch_name, base_branch)

    def create_branch(self, branch_name: str, base_branch: str | None = None) -> None:
        """
        新しいブランチを作成

        Args:
            branch_name: 作成するブランチ名
            base_branch: ベースとなるブランチ（デフォルト: 設定のbase_branch）
        """
        if base_branch is None:
            config = get_config()
            base_branch = config.base_branch

        # 最新の状態をフェッチ
        self._run_git("fetch", "origin", base_branch)

        # ブランチを作成してチェックアウト
        self._run_git("checkout", "-b", branch_name, f"origin/{base_branch}")

        print(f"🌿 ブランチを作成しました: {branch_name}")

    def checkout_branch(self, branch_name: str) -> None:
        """
        ブランチをチェックアウト

        Args:
            branch_name: チェックアウトするブランチ名
        """
        self._run_git("checkout", branch_name)

    def get_current_branch(self) -> str:
        """
        現在のブランチ名を取得

        Returns:
            現在のブランチ名
        """
        result = self._run_git("branch", "--show-current")
        return result.strip()

    def checkout_existing_branch(self, branch_name: str) -> None:
        """
        既存のブランチにチェックアウト

        Args:
            branch_name: チェックアウトするブランチ名

        Raises:
            ShellError: ブランチが存在しない場合またはチェックアウト失敗
        """
        current = self.get_current_branch()

        if current == branch_name:
            print(f"🌿 既にブランチ {branch_name} にいます")
            return

        # ブランチが存在するか確認
        self._run_git("rev-parse", "--verify", branch_name)
        self._run_git("checkout", branch_name)
        print(f"🌿 ブランチ {branch_name} にチェックアウトしました")

    def has_uncommitted_changes(self) -> bool:
        """
        コミットされていない変更があるかチェック

        Returns:
            未コミットの変更がある場合True
        """
        result = self._run_git("status", "--porcelain", "--ignore-submodules")
        return bool(result.strip())

    def get_diff_stats(self) -> dict[str, int]:
        """
        変更の統計情報を取得

        Returns:
            {files_changed, insertions, deletions} の辞書
        """
        try:
            result = self._run_git("diff", "--stat", "HEAD")
            # 最終行をパース: " 3 files changed, 10 insertions(+), 5 deletions(-)"
            lines = result.strip().split("\n")
            if not lines:
                return {"files_changed": 0, "insertions": 0, "deletions": 0}

            last_line = lines[-1]
            stats = {
                "files_changed": 0,
                "insertions": 0,
                "deletions": 0,
            }

            match = re.search(r"(\d+) files? changed", last_line)
            if match:
                stats["files_changed"] = int(match.group(1))

            match = re.search(r"(\d+) insertions?\(\+\)", last_line)
            if match:
                stats["insertions"] = int(match.group(1))

            match = re.search(r"(\d+) deletions?\(-\)", last_line)
            if match:
                stats["deletions"] = int(match.group(1))

            return stats
        except Exception:
            return {"files_changed": 0, "insertions": 0, "deletions": 0}

    def sync_submodule(self, submodule_path: str | None = None) -> None:
        """
        サブモジュールを同期

        Args:
            submodule_path: サブモジュールのパス（デフォルト: 設定から取得）

        Notes:
            以下の手順で同期:
            1. ベースブランチをマージして本体を最新にする
            2. サブモジュールを更新
            3. 必要に応じてビルド
        """
        config = get_config()

        if not config.submodule_enabled:
            print("ℹ️ サブモジュールは無効です")
            return

        if submodule_path is None:
            submodule_path = config.submodule_path

        if not submodule_path:
            print("⚠️ サブモジュールパスが設定されていません")
            return

        base_branch = config.base_branch

        # 1. ベースブランチをマージ
        self._run_git("fetch", "origin", base_branch)
        self._run_git("merge", f"origin/{base_branch}")

        # 2. サブモジュールを更新
        submodule_full_path = self.repo_path / submodule_path
        shell = ShellRunner(cwd=submodule_full_path)
        shell.run_git("checkout", "main", check=True)
        shell.run_git("pull", "origin", "main", check=True)

        print(f"📦 サブモジュールを同期しました: {submodule_path}")

    def check_submodule_changes(self, submodule_path: str | None = None) -> bool:
        """
        サブモジュールに変更があるかチェック

        Args:
            submodule_path: サブモジュールのパス

        Returns:
            変更がある場合True
        """
        config = get_config()

        if submodule_path is None:
            submodule_path = config.submodule_path

        if not submodule_path:
            return False

        result = self._run_git("status", "--porcelain", submodule_path)
        return bool(result.strip())

    def run_git_command(self, args: list[str], cwd: str | None = None) -> tuple[bool, str]:
        """
        汎用Gitコマンドランナー

        Args:
            args: Gitコマンドの引数リスト（例: ["diff", "--name-only"]）
            cwd: 作業ディレクトリ（省略時はrepo_path）

        Returns:
            (成功したか, 標準出力または標準エラー出力) のタプル
        """
        work_dir = cwd or str(self.repo_path)
        shell = ShellRunner(cwd=work_dir)
        result = shell.run_git(*args)
        if result.success:
            return True, result.stdout
        else:
            return False, result.stderr

    def get_diff_files(self, base_branch: str, head_branch: str) -> list[str]:
        """
        2つのブランチ間の差分ファイルリストを取得

        Args:
            base_branch: ベースブランチ（例: "origin/main"）
            head_branch: ヘッドブランチ（例: "HEAD"）

        Returns:
            差分のあるファイルパスのリスト
        """
        success, output = self.run_git_command(
            ["diff", "--name-only", f"{base_branch}...{head_branch}"]
        )
        if not success:
            return []
        return [f for f in output.strip().split("\n") if f]

    def get_file_diff(
        self,
        base_branch: str,
        head_branch: str,
        path: str,
        max_lines: int = 200,
    ) -> str:
        """
        ファイル単位の diff を取得

        Args:
            base_branch: ベースブランチ（例: "origin/main"）
            head_branch: ヘッドブランチ（例: "HEAD"）
            path: ファイルパス
            max_lines: 最大行数（超過時は切り詰め）

        Returns:
            diff テキスト（超過時は末尾に truncation メッセージ付き）
        """
        success, output = self.run_git_command(
            ["diff", f"{base_branch}...{head_branch}", "--", path]
        )
        if not success:
            return ""
        lines = output.split("\n")
        if len(lines) > max_lines:
            truncated = "\n".join(lines[:max_lines])
            return f"{truncated}\n... (truncated: {len(lines)} lines total)"
        return output

    def get_diff_stat(self, base_branch: str, head_branch: str) -> str:
        """
        ブランチ間の diff stat を取得

        Args:
            base_branch: ベースブランチ
            head_branch: ヘッドブランチ

        Returns:
            git diff --stat の出力
        """
        success, output = self.run_git_command(
            ["diff", "--stat", f"{base_branch}...{head_branch}"]
        )
        if not success:
            return ""
        return output.strip()

    def get_log_oneline(self, count: int = 20, branch: str | None = None) -> str:
        """
        コミットログを1行形式で取得

        Args:
            count: 取得するコミット数（デフォルト20）
            branch: ブランチ範囲指定（例: "origin/main..HEAD"）

        Returns:
            git log --onelineの出力文字列
        """
        cmd = ["log", "--oneline", f"-{count}"]
        if branch:
            cmd = ["log", "--oneline", branch]
        success, output = self.run_git_command(cmd)
        if not success:
            return ""
        return output.strip()

    def cherry_pick(self, commit_hash: str) -> tuple[bool, str]:
        """
        コミットをチェリーピック

        Args:
            commit_hash: チェリーピックするコミットハッシュ

        Returns:
            (成功したか, 出力メッセージ) のタプル
        """
        return self.run_git_command(["cherry-pick", commit_hash])

    def branch_exists_locally(self, branch_name: str) -> bool:
        """
        ブランチがローカルに存在するか確認

        Args:
            branch_name: 確認するブランチ名

        Returns:
            ローカルに存在する場合True
        """
        success, output = self.run_git_command(["branch", "--list", branch_name])
        return success and bool(output.strip())

    # === Base ブランチ同期 ===

    # --- Branch reuse 許容条件 ---
    # ahead（feature branch 独自コミット）: 常に許可（通常の開発状態）
    # behind（base の未取り込みコミット）: 警告のみ（自動 merge/rebase はしない）
    # .gitmodules 差分: 常に NG（submodule 構成変更は構造破壊リスク大）
    # submodule 構造差分: 常に NG（submodule ↔ 通常ディレクトリ変換は Git の型変換）
    # 上記の NG 条件に該当する場合、worktree 作成前に停止しユーザーに手動対応を促す。

    # behind 件数がこの閾値を超えると警告メッセージに含める
    _BASE_BEHIND_WARNING_THRESHOLD = 50

    def fetch_base_branch(self, base_branch: str) -> bool:
        """origin/<base_branch> を fetch する

        Args:
            base_branch: ベースブランチ名（"origin/" 接頭辞なし）

        Returns:
            fetch 成功なら True、失敗なら False
        """
        branch = base_branch.replace("origin/", "")
        try:
            self._run_git("fetch", "origin", branch)
            return True
        except ShellError:
            logger.warning(f"fetch に失敗（ベース: {branch}）、続行します")
            return False

    def sync_local_base_branch(self, base_branch: str) -> None:
        """ローカルの base_branch を origin/<base_branch> に同期する

        Args:
            base_branch: ベースブランチ名（"origin/" 接頭辞なし）
        """
        branch = base_branch.replace("origin/", "")
        try:
            self._run_git("branch", "-f", branch, f"origin/{branch}")
            logger.info(f"ローカル {branch} を origin/{branch} に同期")
        except ShellError:
            logger.warning(f"ローカル {branch} の同期に失敗、続行します")

    def get_branch_ahead_behind(
        self, branch_name: str, base_branch: str
    ) -> tuple[int, int]:
        """branch と origin/<base_branch> の ahead/behind 件数を返す

        Args:
            branch_name: 対象ブランチ名
            base_branch: ベースブランチ名（"origin/" 接頭辞なし）

        Returns:
            (ahead, behind) のタプル。ahead = branch 独自コミット数、
            behind = base 側の未取り込みコミット数
        """
        base = base_branch.replace("origin/", "")
        try:
            output = self._run_git(
                "rev-list", "--left-right", "--count",
                f"origin/{base}...{branch_name}",
            )
            parts = output.strip().split()
            if len(parts) == 2:
                return int(parts[1]), int(parts[0])
        except (ShellError, ValueError):
            pass
        return 0, 0

    def detect_base_structure_conflicts(
        self, branch_name: str, base_branch: str
    ) -> list[str]:
        """branch と origin/<base_branch> 間の危険な構造差分を検出する

        検出対象:
        - .gitmodules の差分
        - submodule path の差分（submodule ↔ 通常ディレクトリ変換を含む）

        Args:
            branch_name: 対象ブランチ名
            base_branch: ベースブランチ名（"origin/" 接頭辞なし）

        Returns:
            検出された危険差分のメッセージリスト（問題なしなら空リスト）
        """
        base = base_branch.replace("origin/", "")
        issues: list[str] = []

        # .gitmodules の差分チェック（2ドット比較: origin/base と branch の直接差分）
        try:
            diff_output = self._run_git(
                "diff", "--name-status", f"origin/{base}", branch_name,
                "--", ".gitmodules",
            )
            if diff_output.strip():
                issues.append(
                    ".gitmodules が origin/{} と不一致（"
                    "submodule 構成の追加・削除・変更が検出されました）".format(base)
                )
        except ShellError:
            pass

        # submodule path の差分チェック
        # base 側と branch 側で submodule エントリ（mode 160000）を比較
        try:
            base_submodules = self._list_submodule_paths(f"origin/{base}")
            branch_submodules = self._list_submodule_paths(branch_name)

            added = branch_submodules - base_submodules
            removed = base_submodules - branch_submodules

            if added:
                issues.append(
                    f"branch に base にない submodule path があります: "
                    f"{', '.join(sorted(added))}"
                )
            if removed:
                issues.append(
                    f"base で削除された submodule path が branch に残っています: "
                    f"{', '.join(sorted(removed))}"
                )
        except ShellError:
            pass

        return issues

    def _list_submodule_paths(self, ref: str) -> set[str]:
        """指定 ref の submodule パス一覧を返す（mode 160000 のエントリ）"""
        try:
            output = self._run_git("ls-tree", "-r", ref)
        except ShellError:
            return set()

        paths: set[str] = set()
        for line in output.strip().split("\n"):
            if not line:
                continue
            # format: "<mode> <type> <hash>\t<path>"
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].startswith("160000"):
                paths.add(parts[1])
        return paths

    def validate_branch_reuse_against_base(
        self, branch_name: str, base_branch: str
    ) -> list[str]:
        """既存 branch の再利用可否を base ブランチと比較して検証する

        base fetch → ahead/behind 判定 → 構造差分検出の順に実行し、
        問題があればユーザー向けメッセージのリストを返す。
        問題がなければ空リストを返す。

        Args:
            branch_name: 再利用する既存ブランチ名
            base_branch: ベースブランチ名（"origin/" 接頭辞なし）

        Returns:
            エラーメッセージのリスト（問題なしなら空リスト）
        """
        base = base_branch.replace("origin/", "")
        messages: list[str] = []

        # 1. base fetch（失敗時は stale な origin を信用せず即停止）
        if not self.fetch_base_branch(base):
            messages.append(
                f"origin/{base} の fetch に失敗しました。"
                f"ネットワーク接続またはリモート設定を確認してください"
            )
            return messages

        # 2. ahead/behind 判定
        ahead, behind = self.get_branch_ahead_behind(branch_name, base)
        if behind > self._BASE_BEHIND_WARNING_THRESHOLD:
            messages.append(
                f"origin/{base} から {behind} コミット遅れています"
            )

        # 3. 構造差分検出（.gitmodules, submodule）
        structure_issues = self.detect_base_structure_conflicts(branch_name, base)
        messages.extend(structure_issues)

        return messages

    # === Worktree 操作 ===

    def create_worktree(
        self,
        worktree_path: str | Path,
        branch_name: str,
        base_ref: str,
    ) -> None:
        """
        git worktree を作成

        branch が未作成の場合は新規ブランチと worktree を同時に作成する。
        branch が既存の場合は既存ブランチで worktree を作成する。
        既存 branch 再利用時は base ブランチとの整合性を検証し、
        危険な構造差分がある場合は BranchReuseDenied 例外で停止する。

        Args:
            worktree_path: worktree の配置先パス
            branch_name: ブランチ名
            base_ref: ベース参照（例: "origin/main"）

        Raises:
            ShellError: worktree 作成に失敗した場合
            BranchReuseDenied: 既存 branch の base 整合チェックに失敗した場合
        """
        wt_path = Path(worktree_path)

        # 既存 worktree が残っている場合は削除してから再作成
        if wt_path.exists():
            logger.warning(f"既存 worktree を削除して再作成: {wt_path}")
            self.remove_worktree(wt_path, force=True)

        # 親ディレクトリを作成
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # 最新をフェッチし、ローカルブランチも同期
        local_branch = base_ref.replace("origin/", "")
        self.fetch_base_branch(local_branch)
        self.sync_local_base_branch(local_branch)

        # ブランチが存在するか確認
        try:
            self._run_git("rev-parse", "--verify", branch_name)
            branch_exists = True
        except ShellError:
            branch_exists = False

        if branch_exists:
            # 既存 branch 再利用: base との整合性を検証
            issues = self.validate_branch_reuse_against_base(
                branch_name, local_branch
            )
            if issues:
                raise BranchReuseDenied(branch_name, local_branch, issues)

            self._run_git("worktree", "add", str(wt_path), branch_name)
            logger.info(f"既存ブランチ {branch_name} で worktree 作成: {wt_path}")
        else:
            self._run_git(
                "worktree", "add", "-b", branch_name, str(wt_path), base_ref,
            )
            logger.info(f"新規ブランチ {branch_name} で worktree 作成: {wt_path}")

    def remove_worktree(self, worktree_path: str | Path, force: bool = False) -> None:
        """
        git worktree を削除

        Args:
            worktree_path: 削除する worktree のパス
            force: 強制削除するか
        """
        wt_path = Path(worktree_path)
        args = ["worktree", "remove", str(wt_path)]
        if force:
            args.insert(2, "--force")
        try:
            self._run_git(*args)
            logger.info(f"worktree 削除: {wt_path}")
        except ShellError as e:
            logger.warning(f"worktree 削除に失敗: {wt_path}: {e}")
            # force でも失敗した場合、git worktree prune を試行
            if force:
                try:
                    self._run_git("worktree", "prune")
                except ShellError:
                    pass

    def is_worktree(self, path: str | Path) -> bool:
        """
        指定パスが git worktree として登録されているか確認

        Args:
            path: 確認するパス

        Returns:
            worktree として存在する場合 True
        """
        check_path = str(Path(path).resolve())
        try:
            output = self._run_git("worktree", "list", "--porcelain")
            for line in output.splitlines():
                if line.startswith("worktree "):
                    wt = line[len("worktree "):]
                    if str(Path(wt).resolve()) == check_path:
                        return True
        except ShellError:
            pass
        return False

    def init_submodules(self, worktree_path: str | Path) -> None:
        """
        worktree 上でサブモジュールを初期化

        Args:
            worktree_path: サブモジュールを初期化する worktree のパス
        """
        shell = ShellRunner(cwd=worktree_path)
        result = shell.run_git(
            "-C", str(worktree_path),
            "submodule", "update", "--init", "--recursive",
        )
        if result.success:
            logger.info(f"submodule 初期化完了: {worktree_path}")
        else:
            logger.warning(f"submodule 初期化に失敗: {result.stderr}")

    def _run_git(self, *args: str) -> str:
        """
        Gitコマンドを実行

        Args:
            *args: Gitコマンドの引数

        Returns:
            コマンドの標準出力

        Raises:
            ShellError: コマンドが失敗した場合
        """
        shell = ShellRunner(cwd=self.repo_path)
        result = shell.run_git(*args, check=True)
        return result.stdout
