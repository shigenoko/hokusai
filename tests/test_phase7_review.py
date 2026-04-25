"""
Phase 7: レビュー機構のテスト

C2: 必須ルール欠落検知
C3: 合格/不合格判定の堅牢化
"""

import pytest

from hokusai.nodes.phase7_review import (
    _aggregate_review_results,
    _extract_required_rule_ids,
    _validate_rule_completeness,
    _parse_rule_results,
    _parse_review_result,
)


# --- C2: 必須ルール欠落検知 ---


class TestExtractRequiredRuleIds:
    """チェックリストからルールID抽出のテスト"""

    def test_extract_standard_ids(self):
        content = """
### HQ01: デッドコード
- [ ] check1

### HQ02: 重複コード
- [ ] check2

### SA01: サーバーサイド認可
- [ ] check3
"""
        ids = _extract_required_rule_ids(content)
        assert ids == ["HQ01", "HQ02", "SA01"]

    def test_empty_content(self):
        assert _extract_required_rule_ids("") == []

    def test_no_matching_headers(self):
        content = "## Some Section\n- item\n"
        assert _extract_required_rule_ids(content) == []

    def test_project_rule_ids_not_extracted(self):
        """P01形式はこの関数では抽出しない（プロジェクト固有ルール用）"""
        content = "### P01: Custom Rule\n"
        assert _extract_required_rule_ids(content) == []

    def test_td04_not_in_builtin_checklist(self):
        """TD04はbuilt-inチェックリストに含まれない（プロジェクト固有ルール）"""
        from hokusai.nodes.phase7_review import _load_builtin_checklist
        content = _load_builtin_checklist()
        ids = _extract_required_rule_ids(content)
        assert "TD04" not in ids
        # TD01-TD03 は存在する
        assert "TD03" in ids


class TestValidateRuleCompleteness:
    """必須ルール完全性チェックのテスト"""

    def test_all_present(self):
        parsed = {"HQ01": {}, "HQ02": {}, "SA01": {}}
        required = ["HQ01", "HQ02", "SA01"]
        assert _validate_rule_completeness(parsed, required) == []

    def test_missing_rules(self):
        parsed = {"HQ01": {}}
        required = ["HQ01", "HQ02", "SA01"]
        missing = _validate_rule_completeness(parsed, required)
        assert missing == ["HQ02", "SA01"]

    def test_extra_rules_ok(self):
        """パース結果に余分なルールがあっても問題ない"""
        parsed = {"HQ01": {}, "HQ02": {}, "SA01": {}, "XX99": {}}
        required = ["HQ01", "HQ02"]
        assert _validate_rule_completeness(parsed, required) == []

    def test_empty_parsed(self):
        parsed = {}
        required = ["HQ01", "SA01"]
        assert _validate_rule_completeness(parsed, required) == ["HQ01", "SA01"]

    def test_empty_required(self):
        parsed = {"HQ01": {}}
        assert _validate_rule_completeness(parsed, []) == []

    def test_project_rule_missing_detected(self):
        """プロジェクト固有ルール（P21等）が欠落した場合に検知される"""
        parsed = {"HQ01": {}, "P01": {}}
        required = ["HQ01", "P01", "P21"]
        missing = _validate_rule_completeness(parsed, required)
        assert "P21" in missing


# --- C3: 合格/不合格判定の堅牢化 ---


class TestParseRuleResults:
    """ルール結果テーブルのパーステスト"""

    def test_standard_table(self):
        output = """
| ルールID | ルール名 | 結果 | 備考 |
|----------|----------|------|------|
| HQ01 | デッドコード | OK | - |
| SA01 | 認可 | NG | 権限チェック漏れ |
| UX01 | アクセシビリティ | SKIP | 該当なし |
"""
        rules = _parse_rule_results(output)
        assert len(rules) == 3
        assert rules["HQ01"]["result"] == "OK"
        assert rules["SA01"]["result"] == "NG"
        assert rules["SA01"]["note"] == "権限チェック漏れ"
        assert rules["UX01"]["result"] == "SKIP"

    def test_project_rules(self):
        output = "| P01 | カスタムルール | OK | - |"
        rules = _parse_rule_results(output)
        assert "P01" in rules
        assert rules["P01"]["result"] == "OK"

    def test_no_table(self):
        output = "レビュー完了。問題なし。"
        assert _parse_rule_results(output) == {}


class TestParseReviewResult:
    """レビュー結果パースの堅牢性テスト"""

    def test_rule_based_pass(self):
        """ルール結果が全てOKなら合格"""
        output = """
| HQ01 | デッドコード | OK | - |
| SA01 | 認可 | OK | - |

### 最終判定
- 合格
"""
        result = _parse_review_result(output)
        assert result["passed"] is True

    def test_rule_based_fail(self):
        """ルール結果にNGがあれば不合格"""
        output = """
| HQ01 | デッドコード | OK | - |
| SA01 | 認可 | NG | 権限チェック漏れ |

### 最終判定
- 不合格
"""
        result = _parse_review_result(output)
        assert result["passed"] is False

    def test_rule_ng_overrides_pass_keyword(self):
        """C3修正: ルールNGが「合格」キーワードより優先される"""
        output = """
| HQ01 | デッドコード | NG | console.log残存 |

修正すれば合格になります。
"""
        result = _parse_review_result(output)
        assert result["passed"] is False

    def test_contradictory_keywords_with_rules(self):
        """C3修正: 矛盾するキーワードがあってもルール結果が優先"""
        output = """
| HQ01 | デッドコード | OK | - |
| SA01 | 認可 | OK | - |

以前は不合格でしたが、修正により合格です。
"""
        result = _parse_review_result(output)
        # ルール結果が全てOKなので合格
        assert result["passed"] is True

    def test_keyword_fallback_when_no_rules(self):
        """ルール結果なしの場合、キーワードでフォールバック"""
        output = "レビュー結果: 不合格。修正が必要です。"
        result = _parse_review_result(output)
        assert result["passed"] is False

    def test_keyword_fallback_pass(self):
        """ルール結果なし + 失敗キーワードなし → 合格"""
        output = "コードは問題ありませんでした。"
        result = _parse_review_result(output)
        assert result["passed"] is True

    def test_issues_are_informational_not_fail(self):
        """自由記述 issues は情報提供のみ。NG ルールがなければ合格"""
        output = """
| HQ01 | デッドコード | OK | - |

### 検出された問題
- console.logが残っています
"""
        result = _parse_review_result(output)
        # NG ルールがないので合格（issues は記録されるが合否に影響しない）
        assert result["passed"] is True
        assert len(result["issues"]) == 1

    def test_non_issue_markers_ignored(self):
        """「なし」等のマーカーは問題として抽出しない"""
        output = """
### 検出された問題
- なし
- 特になし
- None
"""
        result = _parse_review_result(output)
        assert result["issues"] == []

    def test_project_rule_ng_causes_fail(self):
        """プロジェクト固有ルール（P21）がNGの場合、レビュー不合格になる"""
        output = """
| HQ01 | デッドコード | OK | - |
| P21 | 外部スキーマ参照の main 収束 | NG | shared-schema が topic branch を参照 |
"""
        result = _parse_review_result(output)
        assert result["passed"] is False
        assert result["rules"]["P21"]["result"] == "NG"

    def test_project_rule_in_review_prompt(self):
        """プロジェクト固有ルールがレビュープロンプトに含まれる"""
        from hokusai.nodes.phase7_review import _build_review_prompt
        project_checklist = {
            "P21": {
                "name": "外部スキーマ参照の main 収束",
                "description": "shared-schema の gitlink が api/main に含まれること",
            }
        }
        prompt = _build_review_prompt(project_checklist)
        assert "P21" in prompt
        assert "外部スキーマ参照" in prompt


# --- NG ルールの issues 反映 ---


class TestAggregateReviewResultsNgToIssues:
    """NG ルールが all_issues に自動追加されることのテスト"""

    def test_ng_rules_added_to_issues(self):
        """NG ルールが all_issues に含まれる"""
        review_by_repo = {
            "Backend": {
                "passed": False,
                "issues": [],
                "rules": {
                    "HQ01": {"name": "デッドコード", "result": "OK", "note": ""},
                    "HQ02": {
                        "name": "既存ユーティリティの再利用",
                        "result": "NG",
                        "note": "useToast を再実装している",
                    },
                },
            }
        }
        result = _aggregate_review_results(review_by_repo)
        assert not result["all_passed"]
        assert any("既存ユーティリティの再利用" in i for i in result["all_issues"])
        assert any("useToast を再実装している" in i for i in result["all_issues"])

    def test_ok_rules_not_added_to_issues(self):
        """OK ルールは all_issues に追加されない"""
        review_by_repo = {
            "Backend": {
                "passed": True,
                "issues": [],
                "rules": {
                    "HQ01": {"name": "デッドコード", "result": "OK", "note": ""},
                },
            }
        }
        result = _aggregate_review_results(review_by_repo)
        assert result["all_issues"] == []

    def test_multiple_ng_rules_across_repos(self):
        """複数リポジトリの NG ルールがすべて issues に追加される"""
        review_by_repo = {
            "Backend": {
                "passed": False,
                "issues": ["手動で追加した問題"],
                "rules": {
                    "HQ04": {
                        "name": "既存パターンとの一貫性",
                        "result": "NG",
                        "note": "Service 経由でない",
                    },
                },
            },
            "API": {
                "passed": False,
                "issues": [],
                "rules": {
                    "TD01": {
                        "name": "スキーマ形式の一貫性",
                        "result": "NG",
                        "note": "既存と異なる形式",
                    },
                },
            },
        }
        result = _aggregate_review_results(review_by_repo)
        assert not result["all_passed"]
        # 手動 issues + NG ルール 2 件 = 3 件
        assert len(result["all_issues"]) == 3
        assert any("手動で追加した問題" in i for i in result["all_issues"])
        assert any("既存パターンとの一貫性" in i for i in result["all_issues"])
        assert any("スキーマ形式の一貫性" in i for i in result["all_issues"])

    def test_ng_rule_without_note(self):
        """備考なしの NG ルールはルール名のみが issues に追加される"""
        review_by_repo = {
            "Backend": {
                "passed": False,
                "issues": [],
                "rules": {
                    "P05": {
                        "name": "ロジック重複禁止",
                        "result": "NG",
                        "note": "",
                    },
                },
            }
        }
        result = _aggregate_review_results(review_by_repo)
        assert any("ロジック重複禁止" in i for i in result["all_issues"])
        # 備考なしなので ": " が付かない
        ng_issues = [i for i in result["all_issues"] if "ロジック重複禁止" in i]
        assert not any(i.endswith(": ") for i in ng_issues)
