"""hokusai/utils/pr_body_sections.py のテスト"""

from hokusai.utils.pr_body_sections import upsert_section


class TestUpsertSection:
    """upsert_section のテスト"""

    def test_adds_section_to_empty_body(self):
        """空の本文にセクションを追加"""
        result = upsert_section("", "変更サマリー", "### Backend\n- file.py")
        assert "## 変更サマリー" in result
        assert "### Backend" in result

    def test_adds_section_to_existing_body(self):
        """既存本文の末尾にセクションを追加"""
        body = "## 概要\n\nこのPRは...\n"
        result = upsert_section(body, "変更サマリー", "### Backend\n- file.py")
        assert "## 概要" in result
        assert "このPRは..." in result
        assert "## 変更サマリー" in result
        assert "### Backend" in result

    def test_replaces_existing_section(self):
        """既存セクションを置換"""
        body = "## 概要\n\n概要文\n\n## 変更サマリー\n\n古いサマリー\n\n## その他\n\n補足\n"
        result = upsert_section(body, "変更サマリー", "新しいサマリー")
        assert "## 概要" in result
        assert "概要文" in result
        assert "古いサマリー" not in result
        assert "新しいサマリー" in result
        assert "## その他" in result
        assert "補足" in result

    def test_replaces_last_section(self):
        """最後のセクションを置換"""
        body = "## 概要\n\n説明\n\n## 変更サマリー\n\n古い\n"
        result = upsert_section(body, "変更サマリー", "新しい")
        assert "古い" not in result
        assert "新しい" in result
        assert "## 概要" in result

    def test_preserves_other_sections(self):
        """他セクションを壊さない"""
        body = "## 概要\n\n説明\n\n## Cherry-pick\n\ninfo\n"
        result = upsert_section(body, "変更サマリー", "サマリー内容")
        assert "## 概要" in result
        assert "説明" in result
        assert "## Cherry-pick" in result
        assert "info" in result
        assert "サマリー内容" in result
