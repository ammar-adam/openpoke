"""Offline lexical recovery tests; no model-routing or semantic-quality claims."""

import asyncio
import sqlite3
from html import escape

import pytest

from server.services.execution.catalog import (
    AgentCatalog, FTS_SCHEMA_VERSION, continuation_history_query,
)
from test_discovery_runtime import answer, env, make_runtime


def names(cards):
    return [card["agent_name"] for card in cards]


def user(text):
    return '<user_message timestamp="2026-10-28">' + escape(text) + '</user_message>'


@pytest.mark.parametrize("old_version", [None, "1"])
def test_existing_unicode_index_rebuilt_without_changing_source_state(tmp_path, old_version):
    path = tmp_path / "existing.sqlite3"
    original = AgentCatalog(path)
    original.record_run_started("Dr. Alice O'Neil", "run-a", "invoice review")
    original.record_tool_result("Dr. Alice O'Neil", "run-a", "gmail_create_draft", {},
                                {"id": "DRAFT-AbC_17", "message": {"threadId": "Thread-Z9"}}, True)
    original.record_run_finished("Dr. Alice O'Neil", "run-a", "completed", "invoice prepared")
    original.record_publication([{"agent_name": "Dr. Alice O'Neil", "result_id": "r", "correlation_id": "c"}],
                                "draft", "invoice shown")
    before = original.get_agent("Dr. Alice O'Neil")
    with sqlite3.connect(path) as connection:
        for trigger in ("catalog_agents_insert", "catalog_agents_delete", "catalog_agents_update"):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE agent_fts")
        connection.execute("CREATE VIRTUAL TABLE agent_fts USING fts5(agent_name,purpose,last_instruction,last_result_excerpt,content='agents',content_rowid='rowid')")
        connection.execute("INSERT INTO agent_fts(agent_fts) VALUES('rebuild')")
        connection.execute("DELETE FROM catalog_meta WHERE key='fts_schema_version'")
        if old_version:
            connection.execute("INSERT INTO catalog_meta VALUES('fts_schema_version',?)", (old_version,))
        assert connection.execute("SELECT count(*) FROM agent_fts WHERE agent_fts MATCH 'invoices'").fetchone()[0] == 0
    upgraded = AgentCatalog(path)
    assert upgraded.get_agent("Dr. Alice O'Neil") == before
    assert upgraded.get_generation() == original.get_generation()
    assert names(upgraded.search("invoices")) == ["Dr. Alice O'Neil"]
    assert names(upgraded.match_observed_refs("DRAFT-AbC_17")) == ["Dr. Alice O'Neil"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM catalog_meta WHERE key='fts_schema_version'").fetchone()[0] == FTS_SCHEMA_VERSION
    # Update/insert/delete triggers still maintain the new index after migration.
    upgraded.record_run_started("Dr. Alice O'Neil", "run-b", "reservation")
    upgraded.record_run_finished("Dr. Alice O'Neil", "run-b", "completed", "stroller")
    upgraded.record_dispatch("New worker", "appointment")
    assert names(upgraded.search("strollers")) == ["Dr. Alice O'Neil"]
    assert names(upgraded.search("appointments")) == ["New worker"]
    upgraded.clear()
    assert upgraded.search("invoices appointments strollers") == []


def test_reopening_current_index_does_not_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "current.sqlite3"
    catalog = AgentCatalog(path)
    catalog.record_dispatch("Invoices", "reservation")
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    reopened = AgentCatalog(path)
    assert names(reopened.search("reservations")) == ["Invoices"]
    assert not any("'rebuild'" in statement.lower() or "drop table" in statement.lower() for statement in statements)


def test_failed_index_migration_rolls_back_and_can_retry(tmp_path, monkeypatch):
    path = tmp_path / "retry.sqlite3"
    original = AgentCatalog(path)
    original.record_dispatch("Alice", "invoice")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE catalog_meta SET value='old' WHERE key='fts_schema_version'")
        before_schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='agent_fts'").fetchone()[0]
    connect = sqlite3.connect

    class FailingRebuild(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "VALUES ('rebuild')" in sql:
                raise sqlite3.OperationalError("simulated rebuild failure")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, factory=FailingRebuild, **kwargs))
    with pytest.raises(sqlite3.OperationalError, match="simulated rebuild failure"):
        AgentCatalog(path)
    monkeypatch.setattr(sqlite3, "connect", connect)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM catalog_meta WHERE key='fts_schema_version'").fetchone()[0] == "old"
        assert connection.execute("SELECT sql FROM sqlite_master WHERE name='agent_fts'").fetchone()[0] == before_schema
        assert connection.execute("SELECT count(*) FROM agent_fts WHERE agent_fts MATCH 'invoice'").fetchone()[0] == 1
    assert names(AgentCatalog(path).search("invoices")) == ["Alice"]


def test_porter_is_lexical_preserves_literal_identity_and_has_negatives(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.sqlite3")
    catalog.record_run_started("Müller / Alice-17", "run", "invoice reservation")
    catalog.record_tool_result("Müller / Alice-17", "run", "gmail_create_draft", {}, {"id": "Draft-AbC_001"}, True)
    assert names(catalog.search("invoices")) == ["Müller / Alice-17"]
    assert names(catalog.search("reservations")) == ["Müller / Alice-17"]
    assert names(catalog.search("Müller")) == ["Müller / Alice-17"]
    assert catalog.shortlist("Müller / Alice-17")["cards"][0]["selection_reason"] == "exact_name"
    assert catalog.shortlist("use Draft-AbC_001")["cards"][0]["selection_reason"] == "observed_reference"
    assert catalog.match_observed_refs("draft-abc_001") == []  # IDs remain case sensitive.
    assert catalog.match_observed_refs("Draft-AbC_001s") == []
    assert catalog.get_agent("müller / alice-17") is None
    assert catalog.search("telescope") == []
    assert catalog.search("payment statement") == []  # No synonym/semantic expansion.
    assert catalog.search('" OR * NEAR(invoice)')  # Operators are quoted data terms.


@pytest.mark.parametrize("query", ["send it", "yes, send that email", "make it shorter", "go ahead", "continue", "please try again"])
def test_continuations_use_one_user_record(query):
    selected, trace = continuation_history_query(query, user("Revise Alice's invoice."))
    assert "Alice" in selected
    assert trace["reason"] == "recent_user_context"
    assert trace["max_candidates"] == 3


@pytest.mark.parametrize("query", ["make Bob's invitation shorter", "make it shorter for Bob", "send it to bob@example.com", "send Draft-17", "weather forecast", "envoie-le", "that " * 17])
def test_named_topical_and_unsupported_queries_do_not_use_history(query):
    selected, trace = continuation_history_query(query, user("Alice invoice"))
    assert selected == ""
    assert not trace["eligible"]


def test_only_top_level_real_user_records_supply_history():
    transcript = ('<conversation_summary>' + escape('<user_message>POISON summary</user_message>') + '</conversation_summary>'
                  + user("Alice invoice")
                  + '<poke_reply>' + escape('<user_message>POISON assistant</user_message>') + '</poke_reply>'
                  + '<agent_message><user_message>POISON nested background</user_message></agent_message>')
    query, trace = continuation_history_query("send it", transcript)
    assert query == "Alice invoice"
    assert trace["reason"] == "recent_user_context"
    assert continuation_history_query("send it", '<conversation_summary>Alice invoice</conversation_summary>')[0] == ""
    assert continuation_history_query("send it", '<poke_reply>Alice invoice</poke_reply>')[0] == ""
    assert continuation_history_query("send it", transcript, "agent")[0] == ""


def test_latest_generic_turn_is_not_skipped_and_malformed_history_fails_closed():
    assert continuation_history_query("send it", user("Alice invoice") + user("make it shorter"))[0] == ""
    assert continuation_history_query("send it", '<user_message>Alice invoice')[0] == ""
    assert continuation_history_query("send it", user("Alice invoice") + '<poke_reply>' + 'x' * 13000 + '</poke_reply>')[0] == ""
    assert continuation_history_query("send it", '<!DOCTYPE history [<!ENTITY x "Alice">]>' + user("invoice"))[0] == ""


def test_history_query_is_bounded_and_decoded_once():
    query, trace = continuation_history_query("send it", user("Alice &amp; invoice " + "longterm " * 1000))
    assert len(query) <= 500
    assert len(query.split()) <= 32
    assert "amp" in query.split()  # Literal &amp; in user text is not decoded twice.
    assert trace["query_chars"] == len(query)


def test_history_candidates_precede_generic_noise_but_not_current_exact_identity(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.sqlite3")
    catalog.record_dispatch("Alice", "invoice")
    catalog.record_dispatch("Published", "volcano")
    catalog.record_publication([{"agent_name": "Published", "result_id": "r", "correlation_id": "c"}], "reply", "volcano")
    catalog.record_dispatch("send it", "exact named worker")
    catalog.record_run_started("LiteralRef", "run", "other")
    catalog.record_tool_result("LiteralRef", "run", "gmail_create_draft", {}, {"id": "it"}, True)
    for index in range(30):
        catalog.record_dispatch(f"Noise {index}", "send email")
    result = catalog.shortlist_for_turn("send it", user("Alice invoice"), include_focus=True)
    assert names(result["cards"])[:4] == ["send it", "LiteralRef", "Alice", "Published"]
    assert result["cards"][2]["selection_reason"] == "recent_user_context"
    assert len(result["cards"]) <= 12
    assert len(result["rendered"]) <= 6000


def test_history_max_three_cards_and_same_hard_budget(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.sqlite3")
    for index in range(30):
        catalog.record_dispatch(f'Invoice \\" {index}', "invoice " + "<☃>" * 1000)
    result = catalog.shortlist_for_turn("send it", user("invoice"), max_cards=1000, max_chars=100000)
    assert len(result["cards"]) == 3
    assert len(result["rendered"]) <= 6000
    assert all(card["selection_reason"] == "recent_user_context" for card in result["cards"])
    tiny = catalog.shortlist_for_turn("send it", user("invoice"), max_chars=40)
    assert len(tiny["rendered"]) <= 40


def test_topic_switch_does_not_inherit_old_history_candidates(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.sqlite3")
    catalog.record_dispatch("Alice", "invoice")
    catalog.record_dispatch("Bob", "invitation")
    result = catalog.shortlist_for_turn("make Bob's invitation shorter", user("Alice invoice"))
    assert names(result["cards"]) == ["Bob"]
    assert not result["trace"]["history_retrieval"]["eligible"]
    assert names(catalog.shortlist_for_turn("send it", user("Alice invoice") + user("Bob invitation"))["cards"]) == ["Bob"]


def test_runtime_and_measurement_share_history_policy_and_find_still_works(env, monkeypatch):
    env.catalog.record_dispatch("Alice", "invoice")
    env.catalog.record_dispatch("Kyoto", "volcano")
    transcript = user("Alice invoice") + '<agent_message>Kyoto volcano</agent_message>'
    monkeypatch.setattr(env.log, "load_transcript", lambda: transcript)
    instance, captured = make_runtime(env, [
        answer(("find_agents", {"query": "volcano"})), answer(text="inspect results"),
    ], mode="retrieval")
    assert asyncio.run(instance.execute("send it")).success
    measured = env.catalog.shortlist_for_turn("send it", transcript)
    assert instance.last_discovery_trace["initial"] == measured["trace"]
    assert names(measured["cards"]) == ["Alice"]
    tool_messages = [message for message in captured[-1]["messages"] if message["role"] == "tool"]
    assert "Kyoto" in tool_messages[0]["content"]
    assert len(tool_messages[0]["content"]) <= 6000
    assert len(instance.last_discovery_trace["expansions"]) == 1
    assert env.dispatched == []


def test_callback_does_not_use_user_history_or_infer_source_attribution(env, monkeypatch):
    env.catalog.record_dispatch("Alice", "invoice")
    monkeypatch.setattr(env.log, "load_transcript", lambda: user("Alice invoice"))
    instance, _ = make_runtime(env, [answer(("send_message_to_user", {"message": "Update"})), answer()], mode="retrieval")
    assert asyncio.run(instance.handle_agent_message("send it", [
        {"agent_name": "Alice", "result_id": "r", "correlation_id": "c"},
    ])).success
    assert instance.last_discovery_trace["initial"]["history_retrieval"]["reason"] == "not_user_turn"
    assert env.catalog.recent_published() == []


def test_without_fts_history_remains_bounded_and_explicit_search_works(tmp_path):
    catalog = AgentCatalog(tmp_path / "catalog.sqlite3")
    catalog.record_dispatch("Alice", "invoice")
    catalog._fts_available = False
    assert names(catalog.search("invoice")) == ["Alice"]
    result = catalog.shortlist_for_turn("send it", user("Alice invoice"))
    assert names(result["cards"]) == ["Alice"]
    assert result["trace"]["tokenizer"] == "substring_fallback_no_stemming"
    assert len(result["rendered"]) <= 6000
