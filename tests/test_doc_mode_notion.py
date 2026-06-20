"""doc-mode 実 Notion 出力フックのテスト（残課題: 実Notion出力）

NotionMCPClient（claude driver 経由）には依存せず、client factory を
monkeypatch して検証する。
"""

from types import SimpleNamespace

import pytest

from hokusai import doc_cli
from hokusai.config import WorkflowConfig, reset_config, set_config
from hokusai.nodes import phase0_doc

OK_TEXT = "背景 業務要件 スコープ 受入基準 制約 参照"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    reset_config()
    set_config(WorkflowConfig())
    monkeypatch.setattr(phase0_doc, "dispatch_via_gateway", lambda **k: None)
    phase0_doc.set_generation_backend(lambda p, m, prompt: OK_TEXT)
    yield
    reset_config()
    phase0_doc.set_generation_backend(None)
    doc_cli.set_output_sink(None)
    doc_cli.set_notion_client_factory(None)


class _FakeNotion:
    def __init__(self):
        self.calls = []
        self.return_value = "https://notion.so/created-page"

    def create_subpage(self, parent, title, body):
        self.calls.append((parent, title, body))
        return self.return_value


def test_notion_sink_creates_subpage_under_feature_page():
    fake = _FakeNotion()
    doc_cli.set_notion_client_factory(lambda: fake)
    state = doc_cli.run_doc_workflow(
        "requirements", "DOM指摘", feature_page_id="feat-123"
    )
    doc_cli.notion_output_sink(state)

    assert len(fake.calls) == 1
    parent, title, body = fake.calls[0]
    assert parent == "feat-123"
    assert title == "要件整理：DOM指摘"
    assert OK_TEXT in body
    assert "doc-mode 生成" in body


def test_design_title_naming():
    fake = _FakeNotion()
    doc_cli.set_notion_client_factory(lambda: fake)
    state = doc_cli.run_doc_workflow("design", "プレビュー機構", feature_page_id="f")
    doc_cli.notion_output_sink(state)
    assert fake.calls[0][1] == "【設計書】プレビュー機構"


def test_notion_sink_without_feature_page_falls_back_to_stdout(capsys):
    state = doc_cli.run_doc_workflow("requirements", "T")  # feature 未指定
    doc_cli.notion_output_sink(state)
    out = capsys.readouterr().out
    assert "スキップ" in out


def test_notion_sink_raises_on_failure():
    class Failing(_FakeNotion):
        def create_subpage(self, parent, title, body):
            return None

    doc_cli.set_notion_client_factory(lambda: Failing())
    state = doc_cli.run_doc_workflow("requirements", "T", feature_page_id="f")
    with pytest.raises(doc_cli.DocOutputError):
        doc_cli.notion_output_sink(state)


def test_handle_doc_uses_notion_sink_when_feature_page_given():
    fake = _FakeNotion()
    doc_cli.set_notion_client_factory(lambda: fake)
    args = SimpleNamespace(
        doc_subcommand="start",
        type="requirements",
        topic="DOM指摘",
        feature_page="feat-xyz",
        max_rounds=1,
        mode="auto",
    )
    rc = doc_cli.handle_doc(args)
    assert rc == 0
    assert fake.calls and fake.calls[0][0] == "feat-xyz"


def test_create_subpage_parses_url_from_agent_output():
    """NotionMCPClient.create_subpage を fake claude で直接検証。"""
    from hokusai.integrations.notion_mcp import NotionMCPClient

    client = NotionMCPClient()
    client._claude = SimpleNamespace(
        execute_prompt=lambda prompt, timeout=180, allow_mcp_tools=True: (
            "作成完了: https://notion.so/p/abc123"
        )
    )
    # 親IDは有効な 32-hex（実クライアントが検証する）
    url = client.create_subpage("2f45e03d57e28092bc05e21e932d4a0e", "T", "body")
    assert url == "https://notion.so/p/abc123"
