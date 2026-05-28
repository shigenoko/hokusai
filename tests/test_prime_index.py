"""Prime v2 MVP-1: `prime_index` (FTS5) + `prime_index_meta` の単体テスト

docs/design-prime-v2.md §4.2 で議論した FTS5 検索バックエンドの最小実装
(`SQLiteStore.upsert_prime_index` / `search_prime_index` /
`clear_prime_index_for_workflow`) を検証する。

範囲は MVP-1 (DDL + 3 メソッド) のみ。CLI 統合・backfill・gap analysis は
別 PR (MVP-2 以降)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hokusai.persistence.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "workflow.db")


def test_search_returns_empty_for_blank_query(store: SQLiteStore) -> None:
    """query が空文字 / 空白のみは早期 return で空リストを返す（FTS5 syntax error 回避）"""
    assert store.search_prime_index("") == []
    assert store.search_prime_index("   ") == []


def test_search_returns_empty_when_index_is_empty(store: SQLiteStore) -> None:
    """初期化直後の DB で検索しても例外を出さず空リストを返す"""
    assert store.search_prime_index("anything") == []


def test_upsert_then_search_finds_row(store: SQLiteStore) -> None:
    store.upsert_prime_index(
        workflow_id="wf-1",
        source_type="memory",
        source_id="page-abc",
        title="Notion outbox 404 観察",
        body="Workflows DB share 漏れで cleanup 時に 404 が出る",
        phase=4,
        notion_page_id="page-abc",
    )
    results = store.search_prime_index("404")
    assert len(results) == 1
    row = results[0]
    assert row["workflow_id"] == "wf-1"
    assert row["source_type"] == "memory"
    assert row["source_id"] == "page-abc"
    assert row["phase"] == 4
    assert row["notion_page_id"] == "page-abc"
    assert row["pr_url"] is None
    assert row["file_path"] is None
    # bm25 は負の数または 0 を返す（FTS5 の慣例で「小さいほど関連度高」）
    assert isinstance(row["rank"], (int, float))


def test_upsert_replaces_existing_row(store: SQLiteStore) -> None:
    """同一 (workflow_id, source_type, source_id) の 2 回目 upsert は置換動作

    検索キーは英字 (`updated`) を使う。日本語連続文字は unicode61 が
    1 token にまとめるため部分一致しない仕様 (docs/design-prime-v2.md §10
    議論問題 2 で trigram 切替を議論する対象)。
    """
    store.upsert_prime_index(
        workflow_id="wf-1",
        source_type="memory",
        source_id="page-abc",
        title="initial title",
        body="initial body",
    )
    store.upsert_prime_index(
        workflow_id="wf-1",
        source_type="memory",
        source_id="page-abc",
        title="updated title",
        body="updated body",
        pr_url="https://github.com/example/repo/pull/1",
    )
    results = store.search_prime_index("updated")
    assert len(results) == 1
    assert results[0]["title"] == "updated title"
    assert results[0]["pr_url"] == "https://github.com/example/repo/pull/1"
    # 古い行は残らない
    assert store.search_prime_index("initial") == []


def test_search_workflow_id_filter(store: SQLiteStore) -> None:
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="a",
        title="shared keyword", body="body A",
    )
    store.upsert_prime_index(
        workflow_id="wf-2", source_type="memory", source_id="b",
        title="shared keyword", body="body B",
    )
    results = store.search_prime_index("shared", workflow_id="wf-1")
    assert len(results) == 1
    assert results[0]["workflow_id"] == "wf-1"


def test_search_source_types_filter(store: SQLiteStore) -> None:
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="a",
        title="shared keyword", body="memory body",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="review_issue", source_id="b",
        title="shared keyword", body="review body",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="pr", source_id="c",
        title="shared keyword", body="pr body",
    )
    results = store.search_prime_index(
        "shared", source_types=["memory", "pr"]
    )
    types = sorted(r["source_type"] for r in results)
    assert types == ["memory", "pr"]


def test_search_limit_clamps_result_count(store: SQLiteStore) -> None:
    for i in range(5):
        store.upsert_prime_index(
            workflow_id="wf-1", source_type="memory", source_id=f"a{i}",
            title=f"title {i}", body="shared body",
        )
    results = store.search_prime_index("shared", limit=3)
    assert len(results) == 3


def test_search_invalid_limit_raises(store: SQLiteStore) -> None:
    with pytest.raises(ValueError):
        store.search_prime_index("anything", limit=0)
    with pytest.raises(ValueError):
        store.search_prime_index("anything", limit=-1)


def test_clear_prime_index_for_workflow(store: SQLiteStore) -> None:
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="a",
        title="t1", body="b1",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="b",
        title="t2", body="b2",
    )
    store.upsert_prime_index(
        workflow_id="wf-2", source_type="memory", source_id="c",
        title="t3", body="b3",
    )
    deleted = store.clear_prime_index_for_workflow("wf-1")
    assert deleted == 2
    # wf-1 行は両方消える、wf-2 行は残る
    results = store.search_prime_index("t1 OR t2 OR t3")
    assert len(results) == 1
    assert results[0]["workflow_id"] == "wf-2"


def test_clear_unknown_workflow_returns_zero(store: SQLiteStore) -> None:
    assert store.clear_prime_index_for_workflow("wf-nonexistent") == 0


def test_meta_fields_propagate_through_search(store: SQLiteStore) -> None:
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="pr", source_id="pr-130",
        title="Notion DB share procedure",
        body="docs PR for closing operational gap",
        phase=8,
        notion_page_id="np-1",
        pr_url="https://github.com/shigenoko/hokusai/pull/130",
        file_path="docs/notion-dashboard-operation-guide.md",
    )
    results = store.search_prime_index("share")
    assert len(results) == 1
    row = results[0]
    assert row["notion_page_id"] == "np-1"
    assert row["pr_url"] == "https://github.com/shigenoko/hokusai/pull/130"
    assert row["file_path"] == "docs/notion-dashboard-operation-guide.md"
    assert row["updated_at"] is not None


def test_search_does_not_match_source_type_value(store: SQLiteStore) -> None:
    """source_type は UNINDEXED なので `MATCH 'memory'` 等で source_type の
    値そのものに当たってランキングが汚染されない（PR #134 Copilot Round 1
    指摘の回帰防止）。
    """
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="a",
        title="hello world", body="lorem ipsum",
    )
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="review_issue", source_id="b",
        title="hello world", body="lorem ipsum",
    )
    # title / body に "memory" は含まれていないので 0 件であるべき
    assert store.search_prime_index("memory") == []
    # 同様に "review_issue" でも 0 件
    assert store.search_prime_index("review_issue") == []


def test_already_migrated_lowercase_ddl_is_not_rebuilt(tmp_path: Path) -> None:
    """小文字 `source_type unindexed` で書かれた新 DDL は legacy 判定されない

    SQLite は user が書いた case を sqlite_master にそのまま保持するため、
    case-sensitive な含有判定では小文字 DDL を誤って legacy として
    DROP+rebuild してしまう。`.lower()` で正規化することで防ぐ
    （PR #134 Copilot Round 2 指摘の回帰防止）。
    """
    import sqlite3

    db_path = tmp_path / "lower.db"
    # 小文字で正しい新 DDL を直接書き込む（source_type unindexed）
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE prime_index USING fts5("
            "workflow_id unindexed, source_type unindexed, "
            "source_id unindexed, phase unindexed, title, body, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        # データを入れて rebuild されると消えることを検証する
        conn.execute(
            "INSERT INTO prime_index "
            "(workflow_id, source_type, source_id, phase, title, body) "
            "VALUES ('wf-keep', 'memory', 'a', 1, 'title', 'body keep')"
        )
        conn.commit()

    # SQLiteStore() で開いても rebuild されないことを確認
    store = SQLiteStore(db_path)
    # rebuild されたなら row が消える、されていなければ残る
    results = store.search_prime_index("keep")
    assert len(results) == 1
    assert results[0]["workflow_id"] == "wf-keep"


def test_legacy_schema_migrates_to_unindexed_source_type(tmp_path: Path) -> None:
    """旧 DDL (source_type indexed) の DB を開いた時に migration が走る

    sqlite_master から取得した DDL に `source_type UNINDEXED` が含まれて
    いない場合のみ DROP + CREATE が実行される。MVP-1 段階では実 backfill
    経路がないので prime_index は空のまま rebuild される（PR #134 Copilot
    Round 1 指摘の回帰防止）。
    """
    import sqlite3

    db_path = tmp_path / "legacy.db"
    # 旧 DDL を直接書き込む（source_type が indexed の状態）
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE prime_index USING fts5("
            "workflow_id UNINDEXED, source_type, source_id UNINDEXED, "
            "phase UNINDEXED, title, body, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        conn.commit()

    # SQLiteStore() で開くと migration が走り、新 DDL に置き換わる
    store = SQLiteStore(db_path)

    with store._connect() as conn:
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='prime_index'"
        ).fetchone()
    assert sql_row is not None
    assert "source_type UNINDEXED" in (sql_row[0] or "")

    # 新 DDL でちゃんと動作する（MATCH 'memory' が source_type に当たらない）
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="a",
        title="hello", body="world",
    )
    assert store.search_prime_index("memory") == []
    assert len(store.search_prime_index("hello")) == 1


def test_japanese_tokenization_limits(store: SQLiteStore) -> None:
    """unicode61 では日本語連続文字が空白区切りの塊単位で 1 token 化される

    本テストは「現状の挙動はこうである」という固定化が目的。日本語の部分
    一致は効かないことを明示し、docs/design-prime-v2.md §10 議論問題 2 で
    trigram 切替を議論する際の事実根拠として参照する。
    """
    store.upsert_prime_index(
        workflow_id="wf-1", source_type="memory", source_id="a",
        title="ダッシュボード",
        body="Operations Console の同期再送ボタン",
    )
    # ASCII 単語は単語境界で素直に index されるのでヒット
    assert len(store.search_prime_index("Operations")) == 1
    assert len(store.search_prime_index("Console")) == 1
    # 日本語連続文字の部分一致は unicode61 では効かない
    # （trigram への切替が必要、§10 議論問題 2）
    assert store.search_prime_index("同期") == []
    assert store.search_prime_index("ボタン") == []


def test_cascade_delete_via_delete_old_completed_workflows(
    tmp_path: Path,
) -> None:
    """delete_old_completed_workflows で prime_index / meta も cascade される

    `_WORKFLOW_DEPENDENT_TABLES` に prime_index と prime_index_meta を
    追加したので、completed workflow の cleanup 時に index が孤児化しない。
    """
    from datetime import datetime, timedelta

    store = SQLiteStore(tmp_path / "workflow.db")

    # completed workflow 1 件 + 古い updated_at で workflows 行を直接書き込む
    old_ts = (datetime.now() - timedelta(days=120)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO workflows "
            "(workflow_id, task_url, task_title, branch_name, "
            "current_phase, state_json, created_at, updated_at, profile_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("wf-old", "https://example/1", "old task", "feat/old",
             10, "{}", old_ts, old_ts, None),
        )
        conn.commit()

    store.upsert_prime_index(
        workflow_id="wf-old", source_type="memory", source_id="a",
        title="orphan candidate", body="should be cascade deleted",
    )
    assert len(store.search_prime_index("orphan")) == 1

    counts = store.delete_old_completed_workflows(retention_days=90)
    assert counts["workflows"] == 1
    assert counts["prime_index_meta"] == 1
    # FTS5 virtual table の rowcount は信頼できないので件数チェックはせず、
    # search 結果が空になっていることで実削除を確認
    assert store.search_prime_index("orphan") == []
