"""DesignContextResolver の挙動テスト（HTTP は全てモック）。"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

import pytest

from hokusai.config.models import (
    FigmaIntegrationConfig,
    MiroIntegrationConfig,
    WorkflowConfig,
)
from hokusai.integrations.design import (
    DesignCache,
    DesignContextResolver,
    FigmaClient,
    MiroClient,
    extract_figma_urls,
    extract_miro_urls,
)
from hokusai.persistence.sqlite_store import SQLiteStore


@pytest.fixture
def cache():
    with tempfile.TemporaryDirectory() as td:
        yield DesignCache(SQLiteStore(os.path.join(td, "d.db")))


@pytest.fixture
def figma_mock():
    fc = MagicMock(spec=FigmaClient)
    fc.get_file.return_value = {
        "name": "D",
        "lastModified": "2026-01-01T00:00:00Z",
        "document": {
            "id": "0:0",
            "children": [
                {
                    "id": "1:1",
                    "type": "FRAME",
                    "name": "Login",
                    "children": [
                        {"id": "1:2", "type": "TEXT", "characters": "ようこそ"},
                        {"id": "1:3", "type": "INSTANCE", "name": "Button/Primary"},
                    ],
                }
            ],
        },
    }
    fc.get_comments.return_value = []
    fc.get_image_urls.return_value = {}
    return fc


@pytest.fixture
def miro_mock():
    mc = MagicMock(spec=MiroClient)
    mc.get_board.return_value = {"name": "Flow", "modifiedAt": "2026-01-02"}
    mc.list_items.return_value = [
        {"id": "f1", "type": "frame", "data": {"title": "Step 1"}},
        {"id": "s1", "type": "sticky_note", "data": {"content": "<p>登録</p>"}},
    ]
    return mc


def _config(**kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        figma=FigmaIntegrationConfig(enabled=True, api_token_env="_F_TEST", **kwargs.get("figma", {})),
        miro=MiroIntegrationConfig(enabled=True, api_token_env="_M_TEST", **kwargs.get("miro", {})),
    )


class TestUrlExtraction:
    def test_extract_both(self):
        text = (
            "spec: https://www.figma.com/file/Abc12345DEF/Design?node-id=1-2\n"
            "flow: https://miro.com/app/board/uXjV12345=/x"
        )
        assert len(extract_figma_urls(text)) == 1
        assert len(extract_miro_urls(text)) == 1

    def test_no_url(self):
        assert extract_figma_urls("plain text") == []
        assert extract_miro_urls("plain text") == []


class TestResolver:
    def test_ok(self, cache, figma_mock, miro_mock, monkeypatch):
        monkeypatch.setenv("_F_TEST", "figd_x")
        monkeypatch.setenv("_M_TEST", "tok")
        r = DesignContextResolver(
            config=_config(), cache=cache,
            figma_client=figma_mock, miro_client=miro_mock,
        )
        res = r.resolve(
            figma_url="https://www.figma.com/file/Abc12345DEF/D",
            miro_url="https://miro.com/app/board/uXjV12345=/x",
        )
        assert res.figma.status in ("ok", "partial")
        assert res.miro.status in ("ok", "partial")
        assert res.block is False

    def test_cache_hit_skips_api(self, cache, figma_mock, miro_mock, monkeypatch):
        monkeypatch.setenv("_F_TEST", "figd_x")
        monkeypatch.setenv("_M_TEST", "tok")
        r = DesignContextResolver(
            config=_config(), cache=cache,
            figma_client=figma_mock, miro_client=miro_mock,
        )
        f_url = "https://www.figma.com/file/Abc12345DEF/D"
        m_url = "https://miro.com/app/board/uXjV12345=/x"
        r.resolve(figma_url=f_url, miro_url=m_url)
        figma_mock.get_file.reset_mock()
        miro_mock.get_board.reset_mock()
        r.resolve(figma_url=f_url, miro_url=m_url)
        assert figma_mock.get_file.call_count == 0
        assert miro_mock.get_board.call_count == 0

    def test_cache_hit_preserves_partial_status(self, cache, monkeypatch):
        """warnings 入りの context をキャッシュから復元すると status=partial になる。

        初回 partial が二回目以降 ok に格上げされて、Notion Dashboard の
        Design Status や design_review_required の判定が狂う問題を防ぐ。
        """
        monkeypatch.setenv("_F_TEST", "figd_x")
        monkeypatch.setenv("_M_TEST", "tok")

        # warnings 入り context を直接キャッシュへ書く
        cache.put_figma(
            "Abc12345DEF",
            None,
            {
                "source": "figma",
                "url": "https://www.figma.com/file/Abc12345DEF/D",
                "title": "D",
                "summary": "...",
                "screens": [],
                "comments": [],
                "warnings": ["Figma のスクリーン情報を抽出できませんでした"],
            },
            ttl_seconds=3600,
        )
        cache.put_miro(
            "uXjV12345=",
            {
                "source": "miro",
                "url": "https://miro.com/app/board/uXjV12345=/x",
                "title": "B",
                "summary": "...",
                "screens": [],
                "warnings": ["Miro item を取得できませんでした"],
            },
            ttl_seconds=3600,
        )

        # API クライアントは呼ばれないはず
        from unittest.mock import MagicMock

        from hokusai.integrations.design import FigmaClient, MiroClient

        fc = MagicMock(spec=FigmaClient)
        mc = MagicMock(spec=MiroClient)

        r = DesignContextResolver(
            config=_config(), cache=cache, figma_client=fc, miro_client=mc,
        )
        res = r.resolve(
            figma_url="https://www.figma.com/file/Abc12345DEF/D",
            miro_url="https://miro.com/app/board/uXjV12345=/x",
        )
        assert res.figma.status == "partial"
        assert res.miro.status == "partial"

    def test_cache_hit_preserves_ok_status(self, cache, monkeypatch):
        """warnings なしの cached context は status=ok のまま復元される。"""
        monkeypatch.setenv("_F_TEST", "figd_x")

        cache.put_figma(
            "Abc12345DEF",
            None,
            {
                "source": "figma",
                "title": "D",
                "summary": "...",
                "screens": [{"name": "Login"}],
                "comments": [],
                "warnings": [],
            },
            ttl_seconds=3600,
        )
        from unittest.mock import MagicMock

        from hokusai.integrations.design import FigmaClient, MiroClient

        fc = MagicMock(spec=FigmaClient)
        mc = MagicMock(spec=MiroClient)
        cfg = WorkflowConfig(
            figma=FigmaIntegrationConfig(enabled=True, api_token_env="_F_TEST"),
            miro=MiroIntegrationConfig(enabled=False),
        )
        r = DesignContextResolver(config=cfg, cache=cache, figma_client=fc, miro_client=mc)
        res = r.resolve(figma_url="https://www.figma.com/file/Abc12345DEF/D")
        assert res.figma.status == "ok"

    def test_failure_block(self, cache, figma_mock, miro_mock, monkeypatch):
        # token 無し → failed
        monkeypatch.delenv("_F_TEST", raising=False)
        monkeypatch.delenv("_M_TEST", raising=False)
        cfg = WorkflowConfig(
            figma=FigmaIntegrationConfig(enabled=True, api_token_env="_F_TEST", on_failure="warn"),
            miro=MiroIntegrationConfig(enabled=True, api_token_env="_M_TEST", on_failure="block"),
        )
        r = DesignContextResolver(
            config=cfg, cache=cache,
            figma_client=figma_mock, miro_client=miro_mock,
        )
        res = r.resolve(
            figma_url="https://www.figma.com/file/Abc12345DEF/D",
            miro_url="https://miro.com/app/board/uXjV12345=/x",
        )
        assert res.figma.status == "failed"
        assert res.miro.status == "failed"
        assert res.block is True
        assert len(res.sync_errors) == 2

    def test_disabled(self, cache, monkeypatch):
        cfg = WorkflowConfig(
            figma=FigmaIntegrationConfig(enabled=False),
            miro=MiroIntegrationConfig(enabled=False),
        )
        r = DesignContextResolver(config=cfg, cache=cache)
        res = r.resolve(
            figma_url="https://www.figma.com/file/Abc12345DEF/D",
            miro_url="https://miro.com/app/board/uXjV12345=/x",
        )
        assert res.figma.status == "not_configured"
        assert res.miro.status == "not_configured"

    def test_render_markdown(self, cache, figma_mock, miro_mock, monkeypatch):
        monkeypatch.setenv("_F_TEST", "figd_x")
        monkeypatch.setenv("_M_TEST", "tok")
        r = DesignContextResolver(
            config=_config(), cache=cache,
            figma_client=figma_mock, miro_client=miro_mock,
        )
        res = r.resolve(
            figma_url="https://www.figma.com/file/Abc12345DEF/D",
            miro_url="https://miro.com/app/board/uXjV12345=/x",
        )
        md = DesignContextResolver.render_markdown(res)
        assert "外部デザイン" in md
        assert "Figma" in md
        assert "Miro" in md

    def test_skip_on_failure(self, cache, figma_mock, miro_mock, monkeypatch):
        monkeypatch.delenv("_F_TEST", raising=False)
        cfg = WorkflowConfig(
            figma=FigmaIntegrationConfig(enabled=True, api_token_env="_F_TEST", on_failure="skip"),
            miro=MiroIntegrationConfig(enabled=False),
        )
        r = DesignContextResolver(
            config=cfg, cache=cache,
            figma_client=figma_mock, miro_client=miro_mock,
        )
        res = r.resolve(figma_url="https://www.figma.com/file/Abc12345DEF/D")
        assert res.figma.status == "skipped"
        assert res.block is False
