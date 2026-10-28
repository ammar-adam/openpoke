"""Execution agent log management with structured XML-style tags."""

from __future__ import annotations

import json
import re
import threading
from html import escape, unescape
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

from ...logging_config import logger
from ...utils.timezones import now_in_user_timezone


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_EXECUTION_LOG_DIR = _DATA_DIR / "execution_agents"


def _slugify(name: str) -> str:
    """Convert agent name to filesystem-safe slug."""
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "agent"


def _encode_payload(payload: str) -> str:
    """Encode payload for storage."""
    normalized = payload.replace("\r\n", "\n").replace("\r", "\n")
    collapsed = normalized.replace("\n", "\\n")
    return escape(collapsed, quote=False)


def _decode_payload(payload: str) -> str:
    """Decode payload from storage."""
    return unescape(payload).replace("\\n", "\n")


_ATTR_PATTERN = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"")


class ExecutionAgentLogStore:
    """Append-only journal for execution agents with XML-style tags."""

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning(f"Failed to create directory: {exc}")

    def _lock_for(self, agent_name: str) -> threading.Lock:
        """Get or create a lock for an agent."""
        slug = _slugify(agent_name)
        with self._global_lock:
            if slug not in self._locks:
                self._locks[slug] = threading.Lock()
            return self._locks[slug]

    def _log_path(self, agent_name: str) -> Path:
        """Get log file path for an agent."""
        return self._base_dir / f"{_slugify(agent_name)}.log"

    def _append(self, agent_name: str, tag: str, payload: str, *, json_payload=False):
        """Append an entry with the given tag."""
        encoded = escape(str(payload), quote=False) if json_payload else _encode_payload(str(payload))
        timestamp = now_in_user_timezone("%Y-%m-%d %H:%M:%S")
        encoding = ' encoding="json"' if json_payload else ''
        entry = f"<{tag} timestamp=\"{timestamp}\"{encoding}>{encoded}</{tag}>\n".encode("utf-8")

        with self._lock_for(agent_name):
            try:
                with self._log_path(agent_name).open("ab") as handle:
                    offset = handle.tell()
                    handle.write(entry)
                return {"offset":offset,"end_offset":offset+len(entry)}
            except Exception as exc:
                logger.error(f"Failed to append to log: {exc}")
                if json_payload:
                    raise  # Never report a new full receipt as archived when its write failed.

    def _parse_line(self, line: str) -> Optional[Tuple[str, str, str]]:
        """Parse a single log line."""
        stripped = line.strip()
        if not (stripped.startswith("<") and "</" in stripped):
            return None

        open_end = stripped.find(">")
        close_start = stripped.rfind("</")
        close_end = stripped.rfind(">")

        if open_end == -1 or close_start == -1 or close_end == -1:
            return None

        open_tag_content = stripped[1:open_end]
        if " " in open_tag_content:
            tag, attr_string = open_tag_content.split(" ", 1)
        else:
            tag, attr_string = open_tag_content, ""

        closing_tag = stripped[close_start + 2 : close_end]
        if closing_tag != tag:
            return None

        attributes: Dict[str, str] = {
            match.group(1): match.group(2) for match in _ATTR_PATTERN.finditer(attr_string)
        }
        timestamp = attributes.get("timestamp", "")
        raw_payload = stripped[open_end + 1 : close_start]
        payload = unescape(raw_payload) if attributes.get("encoding")=="json" else _decode_payload(raw_payload)
        return tag, timestamp, payload

    def record_request(self, agent_name: str, instructions: str) -> None:
        """Record an incoming request from the interaction agent."""
        self._append(agent_name, "agent_request", instructions)

    def record_action(self, agent_name: str, description: str) -> None:
        """Record an agent action (tool call)."""
        self._append(agent_name, "agent_action", description)

    def record_tool_response(self, agent_name: str, tool_name: str, response: str) -> None:
        """Record the response from a tool."""
        self._append(agent_name, "tool_response", f"{tool_name}: {response}")

    def record_agent_response(self, agent_name: str, response: str) -> None:
        """Record the agent's final response."""
        self._append(agent_name, "agent_response", response)

    def record_tool_receipt(self, agent_name, tool_name, arguments, result, success, run_id=None):
        """Preserve complete new JSON receipts, including literal escapes and newlines."""
        payload = {"tool":tool_name,"arguments":arguments,"result":result,
                   "tool_returned_normally":bool(success),"run_id":run_id}
        return self._append(agent_name,"tool_receipt",json.dumps(payload,ensure_ascii=False,default=str),json_payload=True)

    @staticmethod
    def _cursor(value, name="offset"):
        if not isinstance(value,int) or isinstance(value,bool) or value<0:
            raise ValueError(f"{name} must be a nonnegative integer")
        return value

    def recent_memory(self, agent_name, max_chars=12000):
        """A bounded raw tail, explicitly an excerpt; never scans the whole journal."""
        budget = max(512,min(12000,int(max_chars)))
        with self._lock_for(agent_name):
            try:
                with self._log_path(agent_name).open("rb") as handle:
                    handle.seek(0,2)
                    end = handle.tell()
                    start = max(0,end-budget*4)
                    handle.seek(start)
                    data = handle.read(budget*4)
            except FileNotFoundError:
                start,end,data = 0,0,b""
        while data and data[0]&0xC0==0x80:
            data,start = data[1:],start+1
        text = data.decode("utf-8")
        result = {"format":"Raw XML journal excerpt; may start mid-entry. Not a semantic summary.",
                  "offset":start,"end_offset":end,"archive_bytes":end,"omitted_earlier":start>0,"text":text}
        while len(json.dumps(result))>budget:
            removed = max(1,len(result["text"])//4)
            result["offset"] += len(result["text"][:removed].encode("utf-8"))
            result["text"] = result["text"][removed:]
            result["omitted_earlier"] = True
        return result

    def read_archive(self, agent_name, offset=0, limit=3000):
        """Read exact raw UTF-8 journal text; byte cursors survive JSON escaping."""
        offset = self._cursor(offset)
        limit = min(4000,self._cursor(limit,"limit"))
        if not limit:
            raise ValueError("limit must be positive")
        with self._lock_for(agent_name):
            try:
                with self._log_path(agent_name).open("rb") as handle:
                    handle.seek(0,2)
                    total = handle.tell()
                    if offset>total:
                        raise ValueError("offset exceeds archive length")
                    handle.seek(offset)
                    data = handle.read(limit+3)
            except FileNotFoundError:
                if offset:
                    raise ValueError("offset exceeds archive length")
                total,data = 0,b""
        # Up to three additional bytes complete a UTF-8 character at the boundary.
        stop = min(limit,len(data))
        while stop<len(data) and data[stop]&0xC0==0x80:
            stop += 1
        try:
            text = data[:stop].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("offset must be at a UTF-8 character boundary") from exc
        result = {"offset":offset,"next_offset":None,"archive_bytes":total,
                  "format":"raw_xml_journal_utf8","text":text}
        while True:
            end = offset+len(result["text"].encode("utf-8"))
            result["next_offset"] = end if end<total else None
            if len(json.dumps(result))<=5000:
                return result
            result["text"] = result["text"][:max(1,len(result["text"])//2)]

    def search_archive(self, agent_name, query, offset=0):
        """Literal, case-sensitive search of at most 64 KiB per call, with continuation."""
        offset = self._cursor(offset)
        if not isinstance(query,str) or not query or len(query)>200:
            raise ValueError("query must be 1 to 200 characters")
        needle = escape(query,quote=False).encode("utf-8")
        scan_bytes = 65536
        with self._lock_for(agent_name):
            try:
                with self._log_path(agent_name).open("rb") as handle:
                    handle.seek(0,2)
                    total = handle.tell()
                    if offset>total:
                        raise ValueError("offset exceeds archive length")
                    handle.seek(offset)
                    data = handle.read(scan_bytes+len(needle)-1)
            except FileNotFoundError:
                if offset:
                    raise ValueError("offset exceeds archive length")
                total,data = 0,b""
        matches,position = [],0
        while len(matches)<5:
            found = data.find(needle,position)
            if found<0 or found>=scan_bytes:
                break
            matches.append({"offset":offset+found,
                            "excerpt":data[max(0,found-60):found+180].decode("utf-8",errors="replace")})
            position = found+1
        consumed = position if len(matches)==5 else min(scan_bytes,max(0,total-offset))
        end = offset+consumed
        result = {"matches":matches,"next_offset":end if end<total else None,
                  "scanned_bytes":consumed,"archive_bytes":total,"search":"literal case-sensitive stored text"}
        # Excerpts alone may shrink; match offsets and scan continuation stay exact.
        while len(json.dumps(result))>5000:
            for match in matches:
                match["excerpt"] = match["excerpt"][:len(match["excerpt"])//2]
        return result

    def iter_entries(self, agent_name: str) -> Iterator[Tuple[str, str, str]]:
        """Iterate over all log entries for an agent."""
        path = self._log_path(agent_name)
        with self._lock_for(agent_name):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            except Exception as exc:
                logger.error(f"Failed to read log: {exc}")
                lines = []

        for line in lines:
            parsed = self._parse_line(line)
            if parsed is not None:
                yield parsed

    def load_transcript(self, agent_name: str) -> str:
        """Load the full transcript for inclusion in system prompt."""
        parts: List[str] = []
        for tag, timestamp, payload in self.iter_entries(agent_name):
            escaped = escape(payload, quote=False)
            if timestamp:
                parts.append(f"<{tag} timestamp=\"{timestamp}\">{escaped}</{tag}>")
            else:
                parts.append(f"<{tag}>{escaped}</{tag}>")
        return "\n".join(parts)

    def load_recent(self, agent_name: str, limit: int = 10) -> list[tuple[str, str, str]]:
        """Load recent log entries."""
        entries = list(self.iter_entries(agent_name))
        return entries[-limit:] if entries else []

    def list_agents(self) -> list[str]:
        """List all agents with logs."""
        try:
            return sorted(path.stem for path in self._base_dir.glob("*.log"))
        except Exception as exc:
            logger.error(f"Failed to list agents: {exc}")
            return []

    def clear_all(self) -> None:
        """Clear all execution agent logs."""
        try:
            for log_file in self._base_dir.glob("*.log"):
                log_file.unlink()
            logger.info("Cleared all execution agent logs")
        except Exception as exc:
            logger.error(f"Failed to clear execution logs: {exc}")


_execution_agent_logs = ExecutionAgentLogStore(_EXECUTION_LOG_DIR)


def get_execution_agent_logs() -> ExecutionAgentLogStore:
    """Get the singleton log store instance."""
    return _execution_agent_logs
