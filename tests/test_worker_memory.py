"""Bounded worker context with exact retrieval from the existing journal."""

import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.agents.execution_agent import agent as agent_module, runtime as runtime_module
from server.agents.execution_agent.tools import memory
from server.services.execution.log_store import ExecutionAgentLogStore


@pytest.fixture
def store(tmp_path,monkeypatch):
    logs = ExecutionAgentLogStore(tmp_path/"logs")
    monkeypatch.setattr(agent_module,"get_execution_agent_logs",lambda:logs)
    monkeypatch.setattr(memory,"get_execution_agent_logs",lambda:logs)
    monkeypatch.setattr(runtime_module,"get_settings",lambda:SimpleNamespace(openrouter_api_key="offline",execution_agent_model="offline"))
    return logs


def reconstruct(store,name,offset=0):
    pieces = []
    while True:
        page = store.read_archive(name,offset=offset,limit=4000)
        assert len(json.dumps(page))<=5000
        pieces.append(page["text"])
        end = offset+len(page["text"].encode("utf-8"))
        if page["next_offset"] is None:
            assert end==page["archive_bytes"]
            return "".join(pieces)
        assert page["next_offset"]==end>offset
        offset = page["next_offset"]


def test_prompt_uses_bounded_tail_without_loading_full_history(store,monkeypatch):
    store.record_agent_response("Alice","OLD_CONSTRAINT "+"x"*150000)
    store.record_agent_response("Alice","LATEST_REQUEST "+"🚀<>&"*10000+" END_MARKER")
    before = store._log_path("Alice").read_bytes()
    monkeypatch.setattr(store,"load_transcript",lambda *a:pytest.fail("Full transcript loaded"))
    monkeypatch.setattr(store,"iter_entries",lambda *a:pytest.fail("Full history iterated"))
    monkeypatch.setattr(Path,"read_text",lambda *a,**k:pytest.fail("Full file read"))
    excerpt = store.recent_memory("Alice")
    assert len(json.dumps(excerpt))<=12000
    assert excerpt["omitted_earlier"] and excerpt["offset"]>0
    assert excerpt["text"].encode("utf-8")==before[excerpt["offset"]:excerpt["end_offset"]]
    prompt = agent_module.ExecutionAgent("Alice").build_system_prompt_with_history()
    assert "END_MARKER" in prompt and "OLD_CONSTRAINT" not in prompt
    assert "not a semantic summary" in prompt
    assert "read_worker_archive" in prompt and "search_worker_archive" in prompt
    assert len(prompt)<=len(agent_module.ExecutionAgent("Alice").build_system_prompt())+17000


def test_full_new_receipts_roundtrip_literal_escapes_and_buried_identifiers(store):
    args = {"body":"literal \\n and actual\nline\r\nnext","quoted":"<>&\"","unicode":"🚀"}
    result = {"body":"x"*16000,"id":"BURIED-ID-9281","tail":"尾"*3000}
    reference = agent_module.ExecutionAgent("Alice").record_tool_execution("gmail_create_draft",args,result,True,"run-a")
    archived = reconstruct(store,"Alice",reference["offset"])
    assert archived.encode("utf-8")==store._log_path("Alice").read_bytes()
    tag,timestamp,payload = store._parse_line(archived)
    receipt = json.loads(payload)
    assert tag=="tool_receipt" and timestamp
    assert receipt["arguments"]==args and receipt["result"]==result
    assert receipt["run_id"]=="run-a"
    assert reference["end_offset"]==len(archived.encode("utf-8"))
    hits = store.search_archive("Alice","BURIED-ID-9281")
    assert hits["matches"]
    exact = store.read_archive("Alice",hits["matches"][0]["offset"],100)
    assert exact["text"].startswith("BURIED-ID-9281")


def test_archive_chunks_preserve_unicode_boundaries_and_reject_invalid_offsets(store):
    path = store._log_path("Alice")
    path.write_bytes("🚀尾abc".encode("utf-8"))
    first = store.read_archive("Alice",limit=1)
    assert first["text"]=="🚀" and first["next_offset"]==4
    assert reconstruct(store,"Alice")=="🚀尾abc"
    with pytest.raises(ValueError,match="UTF-8"):
        store.read_archive("Alice",offset=1)
    with pytest.raises(ValueError,match="exceeds"):
        store.read_archive("Alice",offset=100)
    end = store.read_archive("Alice",offset=10)
    assert end["text"]=="" and end["next_offset"] is None


def test_search_pages_through_archive_and_catches_window_boundary_match(store):
    raw = "x"*(65536-2)+"NEEDLE "+"x"*70000+"NEEDLE"
    store._log_path("Alice").write_bytes(raw.encode("utf-8"))
    offsets,offset = [],0
    while True:
        result = store.search_archive("Alice","NEEDLE",offset)
        assert result["scanned_bytes"]<=65536
        assert len(json.dumps(result))<=5000
        offsets.extend(item["offset"] for item in result["matches"])
        if result["next_offset"] is None:
            break
        assert result["next_offset"]>offset
        offset = result["next_offset"]
    assert offsets==[raw.index("NEEDLE"),raw.rindex("NEEDLE")]


def test_search_match_limit_keeps_all_matches_reachable(store):
    store.record_agent_response("Alice","needle "*19)
    offset,count = 0,0
    while True:
        result = store.search_archive("Alice","needle",offset)
        assert len(result["matches"])<=5
        count += len(result["matches"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert count==19


def test_archive_tools_are_bound_to_worker_and_do_not_accept_paths(store):
    store.record_agent_response("Alice","ALICE_ONLY")
    store.record_agent_response("Bob","BOB_ONLY")
    tools = memory.build_registry("Alice")
    assert "ALICE_ONLY" in tools["read_worker_archive"]()["text"]
    assert tools["search_worker_archive"]("BOB_ONLY")["matches"]==[]
    with pytest.raises(TypeError):
        tools["read_worker_archive"](agent_name="Bob")
    with pytest.raises(TypeError):
        tools["read_worker_archive"](path="../bob.log")


@pytest.mark.parametrize("arguments",[{"offset":-1},{"offset":True},{"limit":0},{"limit":False}])
def test_invalid_archive_cursors_fail(store,arguments):
    with pytest.raises(ValueError):
        store.read_archive("Alice",**arguments)


def answer(tool=None,arguments=None,text=""):
    message = {"content":text}
    if tool:
        message["tool_calls"] = [{"id":"call-1","type":"function","function":{"name":tool,"arguments":json.dumps(arguments)}}]
    return {"choices":[{"message":message}]}


def test_runtime_archives_full_receipt_before_clipping_tool_output(store,monkeypatch):
    args = {"body":"new terms "+"x"*12000}
    result = {"data":{"draft_id":"DRAFT-EXACT","body":"y"*15000}}
    replies = iter([answer("gmail_create_draft",args),answer(text="Draft ready")])
    model_inputs = []
    async def model(**kwargs):
        model_inputs.append(copy.deepcopy(kwargs))
        return next(replies)
    monkeypatch.setattr(runtime_module,"request_chat_completion",model)
    monkeypatch.setattr(runtime_module,"get_tool_registry",lambda agent_name:{"gmail_create_draft":lambda **arguments:result})
    runtime = runtime_module.ExecutionAgentRuntime("Alice")
    completed = asyncio.run(runtime.execute("Draft the reply"))
    assert completed.success
    tool_output = next(message["content"] for message in model_inputs[1]["messages"] if message["role"]=="tool")
    assert len(tool_output)<=6000
    view = json.loads(tool_output)
    assert view["truncated"] is True and view["archive"]["offset"]==0
    receipt = json.loads(next(payload for tag,_,payload in store.iter_entries("Alice") if tag=="tool_receipt"))
    assert receipt["arguments"]==args and receipt["result"]==result


def test_archive_reads_do_not_recursively_copy_history_into_log(store,monkeypatch):
    store.record_agent_response("Alice","Original evidence")
    original = store._log_path("Alice").read_bytes()
    replies = iter([answer("read_worker_archive",{}),answer(text="Read complete")])
    async def model(**kwargs): return next(replies)
    monkeypatch.setattr(runtime_module,"request_chat_completion",model)
    runtime = runtime_module.ExecutionAgentRuntime("Alice")
    assert asyncio.run(runtime.execute("Read prior evidence")).success
    entries = list(store.iter_entries("Alice"))
    assert len(entries)==2 and all(tag!="tool_receipt" for tag,_,_ in entries)
    assert store._log_path("Alice").read_bytes().startswith(original)


def test_runtime_uses_compact_catalog_state_and_records_tool_evidence(store,monkeypatch):
    from server.services.execution.catalog import AgentCatalog
    catalog = AgentCatalog(store._base_dir.parent/"catalog.sqlite3")
    generation = catalog.get_generation()
    catalog.record_run_started("Alice","run-a","Revise the current draft",expected_generation=generation)
    replies = iter([answer("gmail_create_draft",{"body":"New body"}),answer(text="Draft ready")])
    inputs = []
    async def model(**kwargs):
        inputs.append(kwargs["system"])
        return next(replies)
    monkeypatch.setattr(runtime_module,"request_chat_completion",model)
    monkeypatch.setattr(runtime_module,"get_tool_registry",lambda agent_name:{"gmail_create_draft":lambda **arguments:{"data":{"draft_id":"D42"}}})
    runtime = runtime_module.ExecutionAgentRuntime("Alice",catalog=catalog,run_id="run-a",catalog_generation=generation)
    assert asyncio.run(runtime.execute("Revise")).success
    assert "Revise the current draft" in inputs[0] and "last_result_excerpt_unverified" in inputs[0]
    assert any(reference["value"]=="D42" for reference in catalog.get_agent("Alice")["observed_refs"])


def test_clear_during_model_wait_does_not_recreate_logs_or_execute_tools(store,monkeypatch):
    from server.services.execution.catalog import AgentCatalog
    catalog = AgentCatalog(store._base_dir.parent/"catalog.sqlite3")
    generation = catalog.get_generation()
    catalog.record_run_started("Alice","run-a",expected_generation=generation)
    store.record_request("Alice","Old request")
    async def model(**kwargs):
        catalog.clear()
        store.clear_all()
        return answer("gmail_create_draft",{})
    monkeypatch.setattr(runtime_module,"request_chat_completion",model)
    monkeypatch.setattr(runtime_module,"get_tool_registry",lambda agent_name:{"gmail_create_draft":lambda **arguments:pytest.fail("Stale run executed a tool")})
    runtime = runtime_module.ExecutionAgentRuntime("Alice",catalog=catalog,run_id="run-a",catalog_generation=generation)
    assert not asyncio.run(runtime.execute("Old request")).success
    assert not store._log_path("Alice").exists()


@pytest.mark.parametrize("result",[
    {"error":"Denied"},{"success":False},{"data":{"successful":False,"detail":"Draft rejected"}},
    {"result":{"status":"failed","error":"No connection"}},
])
def test_known_provider_error_envelopes_are_not_reported_as_success(store,monkeypatch,result):
    monkeypatch.setattr(runtime_module,"get_tool_registry",lambda agent_name:{"provider":lambda:result})
    runtime = runtime_module.ExecutionAgentRuntime("Alice")
    success,returned = asyncio.run(runtime._execute_tool("provider",{}))
    assert success is False and returned is result
    formatted = json.loads(runtime._format_tool_result("provider",success,returned,{}))
    assert formatted["status"]=="error" and formatted["result"]==result


def test_blocking_provider_leaves_event_loop_responsive_and_is_not_retried(store,monkeypatch):
    entered,release,finished = threading.Event(),threading.Event(),threading.Event()
    calls = []
    def blocking_provider():
        calls.append("called")
        entered.set()
        release.wait(timeout=2)
        finished.set()
        return {"success":True}
    monkeypatch.setattr(runtime_module,"get_tool_registry",lambda agent_name:{"provider":blocking_provider})
    runtime = runtime_module.ExecutionAgentRuntime("Alice")
    async def scenario():
        call = asyncio.create_task(runtime._execute_tool("provider",{}))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert entered.is_set()
            # This check runs while the synchronous SDK call is still blocked.
            assert not finished.is_set() and not call.done()
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(call,timeout=0.02)
            assert not finished.is_set()  # A timeout did not cancel the provider thread.
            assert calls==["called"]
        finally:
            release.set()
            await asyncio.to_thread(finished.wait,2)
            if not call.done():
                await call
    asyncio.run(scenario())


def test_async_provider_and_sync_awaitable_factory_are_supported(store,monkeypatch):
    async def async_provider(value): return {"value":value}
    monkeypatch.setattr(runtime_module,"get_tool_registry",lambda agent_name:{
        "async_provider":async_provider,"factory":lambda value:async_provider(value)})
    runtime = runtime_module.ExecutionAgentRuntime("Alice")
    async def scenario():
        assert await runtime._execute_tool("async_provider",{"value":1})==(True,{"value":1})
        assert await runtime._execute_tool("factory",{"value":2})==(True,{"value":2})
    asyncio.run(scenario())
