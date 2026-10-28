"""Read-only archive tools bound to the current worker, with no path/name argument."""

from ....services.execution import get_execution_agent_logs


def get_schemas():
    def schema(name,description,properties,required):
        return {"type":"function","function":{"name":name,"description":description,
                "parameters":{"type":"object","properties":properties,"required":required,"additionalProperties":False}}}
    cursor = {"type":"integer","minimum":0,"description":"Byte offset from a prior archive result, default 0."}
    return [
        schema("read_worker_archive","Read exact raw XML journal text for this worker only. Follow next_offset; concatenate chunks before decoding XML/JSON. Output is bounded; a UTF-8 boundary may add up to 3 bytes to the requested window.",
               {"offset":cursor,"limit":{"type":"integer","minimum":1,"maximum":4000,"description":"Byte window, default 3000."}},[]),
        schema("search_worker_archive","Literal case-sensitive search of this worker's stored journal. Scans at most 64 KiB plus match overlap per call. Continue next_offset; an empty page is not proof of no match. Returns up to five offsets/excerpts.",
               {"query":{"type":"string","minLength":1,"maxLength":200},"offset":cursor},["query"]),
    ]


def build_registry(agent_name):
    store = get_execution_agent_logs()
    return {
        "read_worker_archive":lambda offset=0,limit=3000: store.read_archive(agent_name,offset,limit),
        "search_worker_archive":lambda query,offset=0: store.search_archive(agent_name,query,offset),
    }
