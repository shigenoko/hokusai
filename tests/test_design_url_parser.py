"""Figma / Miro URL パーサのユニットテスト。"""

from __future__ import annotations

import pytest

from hokusai.integrations.design.url_parser import (
    parse_figma_url,
    parse_miro_url,
)


class TestFigmaUrlParser:
    def test_file_url_with_node_id(self):
        u = "https://www.figma.com/file/AbCdEf123456/My-Design?node-id=12-34"
        r = parse_figma_url(u)
        assert r.file_key == "AbCdEf123456"
        # Figma URL は hyphen 区切り（"12-34"）だが、REST API 用に
        # colon 区切り（"12:34"）に正規化される。
        assert r.node_id == "12:34"
        assert r.title == "My-Design"

    def test_node_id_normalized_to_colon_format(self):
        """URL の hyphen 形式 node-id が colon 形式に正規化される。

        Figma REST API（/files/{key}/nodes, /images/{key}）は colon 区切り
        を要求するので、parser 段階で統一しておく。
        """
        cases = [
            ("https://www.figma.com/design/AbCdEf123456/X?node-id=0-1", "0:1"),
            ("https://www.figma.com/design/AbCdEf123456/X?node-id=12-34", "12:34"),
            ("https://www.figma.com/design/AbCdEf123456/X?node-id=123-456", "123:456"),
            # 既に colon 形式の場合はそのまま（hyphen 置換しても結果同じ）
            ("https://www.figma.com/design/AbCdEf123456/X?node-id=12:34", "12:34"),
        ]
        for url, expected in cases:
            r = parse_figma_url(url)
            assert r.node_id == expected, f"{url} → expected {expected}, got {r.node_id}"

    def test_design_url_without_node_id(self):
        u = "https://www.figma.com/design/Xyz789AbCDef/Title"
        r = parse_figma_url(u)
        assert r.file_key == "Xyz789AbCDef"
        assert r.node_id is None

    def test_proto_url(self):
        u = "https://www.figma.com/proto/AbCdEf123456/Title"
        r = parse_figma_url(u)
        assert r.file_key == "AbCdEf123456"

    def test_board_url_figjam(self):
        u = "https://www.figma.com/board/AbCdEf123456/Title"
        r = parse_figma_url(u)
        assert r.file_key == "AbCdEf123456"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "https://example.com/file/abc/def",
            "https://www.figma.com/notapath/abc/def",
            "https://www.figma.com/file/abc",  # too short file_key
            None,
        ],
    )
    def test_invalid(self, url):
        with pytest.raises(ValueError):
            parse_figma_url(url)


class TestMiroUrlParser:
    def test_app_board(self):
        u = "https://miro.com/app/board/uXjVABcDeFg=/"
        r = parse_miro_url(u)
        assert r.board_id == "uXjVABcDeFg="

    def test_app_board_with_query(self):
        u = "https://miro.com/app/board/uXjVABcDeFg=/?moveToWidget=1"
        r = parse_miro_url(u)
        assert r.board_id == "uXjVABcDeFg="

    def test_short_board(self):
        u = "https://miro.com/board/AbC123/Title"
        r = parse_miro_url(u)
        assert r.board_id == "AbC123"
        assert r.title == "Title"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not a url",
            "https://example.com/app/board/abc/x",
            "https://miro.com/notapath/abc",
        ],
    )
    def test_invalid(self, url):
        with pytest.raises(ValueError):
            parse_miro_url(url)
