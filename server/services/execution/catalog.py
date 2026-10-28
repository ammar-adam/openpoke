"""Bounded, single-user discovery metadata for existing execution-agent names.

The catalog is an index, not a replacement for execution logs. Names remain the
worker identity. A publication is evidence of what the app displayed, not proof
that a person read it. Limits below are character limits, not token estimates.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

MAX_PURPOSE_CHARS = 512
MAX_INSTRUCTION_CHARS = 1024
MAX_RESULT_CHARS = 768
MAX_OBSERVED_REFS = 8
MAX_QUERY_TERMS = 64
MAX_SEARCH_RESULTS = 100
DEFAULT_MAX_CARDS = 12
DEFAULT_MAX_CHARS = 6000
_TRUNCATED = "… [truncated]"
_OPEN = "<agent_catalog>\n"
_CLOSE = "\n</agent_catalog>"
_FIELDS = "agent_name, purpose, last_instruction, last_used_at, version, latest_run_id, last_run_status, last_result_excerpt, last_result_run_id, observed_refs_json"


class StaleAgentState(ValueError):
    """The offered worker state changed; no dispatch metadata was written."""

    def __init__(self, current_card=None):
        super().__init__("Worker state changed; inspect its refreshed card before dispatching")
        self.current_card = current_card


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= len(_TRUNCATED):
        return text[:limit]
    return text[: limit - len(_TRUNCATED)] + _TRUNCATED


def _limit(value: int, maximum: int = MAX_SEARCH_RESULTS) -> int:
    return max(0, min(int(value), maximum))


def _terms(query: str) -> list[str]:
    # Quote independently extracted words; never pass user FTS operators through.
    return list(dict.fromkeys(re.findall(r"[^\W_]+", str(query), re.UNICODE)))[:MAX_QUERY_TERMS]


def _card(row: Any) -> dict[str, Any]:
    card = {field: row[field] for field in _FIELDS.split(", ")}
    card["observed_refs"] = json.loads(card.pop("observed_refs_json"))
    return card


def _serialize_card(card: dict[str, Any]) -> str:
    # Preserve identity after JSON parsing without allowing values to close the
    # surrounding prompt tags. This is formatting, not an injection guarantee.
    return (json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e"))


def _fit_cards(
    cards: Iterable[dict[str, Any]], max_cards: int, max_chars: int
) -> tuple[list[dict[str, Any]], str]:
    """Fit whole identities and optionally shorter metadata into a hard budget."""
    max_cards = _limit(max_cards, DEFAULT_MAX_CARDS)
    max_chars = max(0, int(max_chars))
    if not max_cards or max_chars < len(_OPEN) + len(_CLOSE):
        return [], ""
    selected: list[dict[str, Any]] = []
    lines: list[str] = []
    seen: set[str] = set()
    used = len(_OPEN) + len(_CLOSE)
    for original in cards:
        name = original.get("agent_name")
        if not isinstance(name, str) or not name.strip() or name in seen:
            continue
        seen.add(name)
        if len(selected) >= max_cards:
            break
        item = {
            "agent_name": name,
            "version": original.get("version", 0),
            "purpose": _clip(original.get("purpose", ""), MAX_PURPOSE_CHARS),
            "last_instruction": _clip(original.get("last_instruction", ""), MAX_INSTRUCTION_CHARS),
            "last_used_at": original.get("last_used_at", 0),
            "last_run_status": original.get("last_run_status", "unknown"),
            "latest_run_id": original.get("latest_run_id"),
            "last_result_excerpt": _clip(original.get("last_result_excerpt", ""), MAX_RESULT_CHARS),
            "last_result_run_id": original.get("last_result_run_id"),
            "observed_refs": list(original.get("observed_refs", []))[:MAX_OBSERVED_REFS],
        }
        for field in ("selection_reason", "matched_ref"):
            if field in original:
                item[field] = original[field]
        if isinstance(original.get("last_publication"), dict):
            item["last_publication"] = dict(original["last_publication"])
            item["last_publication"]["excerpt"] = _clip(item["last_publication"].get("excerpt", ""), 200)
        room = max_chars - used - (1 if lines else 0)
        line = _serialize_card(item)
        if "last_publication" in item:
            while len(line) > room and item["last_publication"]["excerpt"]:
                item["last_publication"]["excerpt"] = _clip(item["last_publication"]["excerpt"], max(0, len(item["last_publication"]["excerpt"])-max(1, len(line)-room)))
                line = _serialize_card(item)
        # Metadata may shrink, but an identity must never be silently truncated.
        for field in ("last_result_excerpt", "last_instruction", "purpose"):
            while len(line) > room and item[field]:
                shrink = max(1, len(line) - room)
                new_length = max(0, len(item[field]) - shrink)
                item[field] = _clip(item[field], new_length)
                line = _serialize_card(item)
        while len(line) > room and item["observed_refs"]:
            item["observed_refs"].pop()
            line = _serialize_card(item)
        if len(line) > room:
            continue
        selected.append(item)
        lines.append(line)
        used += len(line) + (1 if len(lines) > 1 else 0)
    rendered = _OPEN + "\n".join(lines) + _CLOSE if lines else _OPEN + "</agent_catalog>"
    return selected, rendered


def render_cards(
    cards: Iterable[dict[str, Any]],
    max_cards: int = DEFAULT_MAX_CARDS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Render bounded JSON cards; the bound includes the wrapper and newlines."""
    return _fit_cards(cards, max_cards, max_chars)[1]


class AgentCatalog:
    """SQLite-backed metadata, lexical discovery, and published-worker focus."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path is not None else _DEFAULT_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._roster_fingerprints: dict[int, str] = {}
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO catalog_meta VALUES ('generation', '0');
                CREATE TABLE IF NOT EXISTS agents (
                    agent_name TEXT PRIMARY KEY COLLATE BINARY,
                    purpose TEXT NOT NULL DEFAULT '',
                    last_instruction TEXT NOT NULL DEFAULT '',
                    last_used_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS publications (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    result_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message_excerpt TEXT NOT NULL,
                    published_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS published_focus (
                    agent_name TEXT PRIMARY KEY,
                    publication_sequence INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS published_focus_recent
                    ON published_focus(publication_sequence DESC);
                CREATE TABLE IF NOT EXISTS observed_refs (
                    agent_name TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
                    source_run_id TEXT NOT NULL, observed_at REAL NOT NULL,
                    PRIMARY KEY(agent_name,kind,value)
                );
                CREATE INDEX IF NOT EXISTS observed_refs_value ON observed_refs(value);
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(agents)")}
            for name, declaration in (
                ("version", "INTEGER NOT NULL DEFAULT 0"),
                ("latest_run_id", "TEXT"),
                ("last_run_status", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("last_result_excerpt", "TEXT NOT NULL DEFAULT ''"),
                ("last_result_run_id", "TEXT"),
                ("observed_refs_json", "TEXT NOT NULL DEFAULT '[]'"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE agents ADD COLUMN {name} {declaration}")
            try:
                fts_columns = {row["name"] for row in connection.execute("PRAGMA table_info(agent_fts)")}
                if fts_columns and "last_result_excerpt" not in fts_columns:
                    connection.executescript("""
                        DROP TRIGGER IF EXISTS catalog_agents_insert;
                        DROP TRIGGER IF EXISTS catalog_agents_delete;
                        DROP TRIGGER IF EXISTS catalog_agents_update;
                        DROP TABLE agent_fts;
                        DELETE FROM catalog_meta WHERE key='fts_initialized';
                    """)
                connection.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS agent_fts USING fts5(
                        agent_name, purpose, last_instruction, last_result_excerpt,
                        content='agents', content_rowid='rowid'
                    );
                    CREATE TRIGGER IF NOT EXISTS catalog_agents_insert AFTER INSERT ON agents BEGIN
                        INSERT INTO agent_fts(rowid, agent_name, purpose, last_instruction, last_result_excerpt)
                        VALUES (new.rowid, new.agent_name, new.purpose, new.last_instruction, new.last_result_excerpt);
                    END;
                    CREATE TRIGGER IF NOT EXISTS catalog_agents_delete AFTER DELETE ON agents BEGIN
                        INSERT INTO agent_fts(agent_fts, rowid, agent_name, purpose, last_instruction, last_result_excerpt)
                        VALUES ('delete', old.rowid, old.agent_name, old.purpose, old.last_instruction, old.last_result_excerpt);
                    END;
                    CREATE TRIGGER IF NOT EXISTS catalog_agents_update AFTER UPDATE OF purpose,last_instruction,last_result_excerpt ON agents BEGIN
                        INSERT INTO agent_fts(agent_fts, rowid, agent_name, purpose, last_instruction, last_result_excerpt)
                        VALUES ('delete', old.rowid, old.agent_name, old.purpose, old.last_instruction, old.last_result_excerpt);
                        INSERT INTO agent_fts(rowid, agent_name, purpose, last_instruction, last_result_excerpt)
                        VALUES (new.rowid, new.agent_name, new.purpose, new.last_instruction, new.last_result_excerpt);
                    END;
                """)
                indexed = connection.execute(
                    "SELECT value FROM catalog_meta WHERE key='fts_initialized'"
                ).fetchone()
                if indexed is None:
                    connection.execute("INSERT INTO agent_fts(agent_fts) VALUES ('rebuild')")
                    connection.execute("INSERT INTO catalog_meta VALUES ('fts_initialized', '1')")
                self._fts_available = True
            except sqlite3.OperationalError as exc:
                if "no such module" not in str(exc).lower():
                    raise
                self._fts_available = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @property
    def search_backend(self) -> str:
        return "fts5_bm25" if self._fts_available else "sql_lexical_fallback"

    def ensure_legacy_backfill(self, roster: Any) -> None:
        """Import names only after a roster fingerprint changes, never read logs.

        AgentRoster.fingerprint uses file metadata, so an unchanged turn does not
        parse the JSON roster or copy all its names. Foreign roster implementations
        without fingerprint() get an in-process one-time import.
        """
        fingerprint_function = getattr(roster, "fingerprint", None)
        fingerprint = (
            json.dumps(fingerprint_function(), default=str)
            if callable(fingerprint_function)
            else f"object:{id(roster)}"
        )
        with self._lock:
            if self._roster_fingerprints.get(id(roster)) == fingerprint:
                return
            with self._connect() as connection:
                stored = connection.execute(
                    "SELECT value FROM catalog_meta WHERE key='roster_fingerprint'"
                ).fetchone()
                if stored is not None and stored["value"] == fingerprint:
                    self._roster_fingerprints[id(roster)] = fingerprint
                    return
                # Loading is deliberately after the cheap fingerprint checks.
                if callable(fingerprint_function) and callable(getattr(roster, "load", None)):
                    roster.load()
                names = roster.get_agents()
                connection.executemany(
                    "INSERT OR IGNORE INTO agents(agent_name) VALUES (?)",
                    ((name,) for name in names if isinstance(name, str) and name.strip()),
                )
                fingerprint = (
                    json.dumps(fingerprint_function(), default=str)
                    if callable(fingerprint_function) else fingerprint
                )
                connection.execute(
                    "INSERT INTO catalog_meta(key,value) VALUES ('roster_fingerprint',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (fingerprint,),
                )
            self._roster_fingerprints[id(roster)] = fingerprint

    @staticmethod
    def _generation(connection) -> int:
        return int(connection.execute("SELECT value FROM catalog_meta WHERE key='generation'").fetchone()[0])

    @staticmethod
    def _with_publication(connection, card):
        if card is None:
            return None
        row = connection.execute("SELECT p.result_id,p.kind,p.message_excerpt,p.published_at FROM published_focus f "
                                 "JOIN publications p ON p.sequence=f.publication_sequence WHERE f.agent_name=?",
                                 (card["agent_name"],)).fetchone()
        if row:
            card["last_publication"] = {"result_id": row["result_id"], "kind": row["kind"],
                                        "excerpt": _clip(row["message_excerpt"], 200), "published_at": row["published_at"]}
        return card

    @staticmethod
    def _dispatch(connection, agent_name, instructions):
        if not isinstance(agent_name, str) or not agent_name.strip() or not isinstance(instructions, str):
            raise ValueError("A nonempty worker name and text instructions are required")
        connection.execute(
            "INSERT INTO agents(agent_name,purpose,last_instruction,last_used_at,version) VALUES (?,?,?,?,1) "
            "ON CONFLICT(agent_name) DO UPDATE SET "
            "purpose=CASE WHEN agents.purpose='' THEN excluded.purpose ELSE agents.purpose END, "
            "last_instruction=excluded.last_instruction,last_used_at=excluded.last_used_at,version=agents.version+1",
            (agent_name, _clip(instructions, MAX_PURPOSE_CHARS), _clip(instructions, MAX_INSTRUCTION_CHARS), time.time()),
        )

    def record_dispatch(self, agent_name: str, instructions: str, expected_version=None,
                        expected_generation=None) -> dict[str, Any]:
        if expected_version is not None and (isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0):
            raise ValueError("expected_version must be a nonnegative integer")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT {_FIELDS} FROM agents WHERE agent_name=?", (agent_name,)).fetchone()
            if expected_generation is not None and expected_generation != self._generation(connection):
                raise StaleAgentState()
            if expected_version is not None and (row is None or row["version"] != expected_version):
                raise StaleAgentState(self._with_publication(connection, _card(row) if row else None))
            self._dispatch(connection, agent_name, instructions)
            return self._with_publication(connection, _card(connection.execute(f"SELECT {_FIELDS} FROM agents WHERE agent_name=?", (agent_name,)).fetchone()))

    def record_dispatch_many(self, records: Iterable[tuple[str, str]]) -> None:
        """Apply dispatch metadata in one transaction (also useful for eval seeds)."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for agent_name, instructions in records:
                self._dispatch(connection, agent_name, instructions)

    def record_run_started(self, agent_name, run_id, instructions="", expected_generation=None) -> bool:
        if not isinstance(agent_name, str) or not agent_name.strip() or not isinstance(instructions, str):
            raise ValueError("Invalid worker name or instructions")
        if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
            raise ValueError("Invalid run ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_generation is not None and expected_generation != self._generation(connection):
                return False
            existing = connection.execute("SELECT latest_run_id FROM agents WHERE agent_name=?", (agent_name,)).fetchone()
            if existing and existing[0] == run_id:
                return True
            connection.execute(
                "INSERT INTO agents(agent_name,purpose,last_instruction,last_used_at,version,latest_run_id,last_run_status) "
                "VALUES (?,?,?,?,1,?,'running') ON CONFLICT(agent_name) DO UPDATE SET "
                "purpose=CASE WHEN agents.purpose='' THEN excluded.purpose ELSE agents.purpose END, "
                "last_instruction=CASE WHEN excluded.last_instruction='' THEN agents.last_instruction ELSE excluded.last_instruction END, "
                "last_used_at=excluded.last_used_at,version=agents.version+1,latest_run_id=excluded.latest_run_id,last_run_status='running'",
                (agent_name, _clip(instructions, MAX_PURPOSE_CHARS), _clip(instructions, MAX_INSTRUCTION_CHARS), time.time(), run_id),
            )
            return True

    def record_run_finished(self, agent_name, run_id, status, response="", expected_generation=None) -> bool:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("Status describes a run: completed, failed, or interrupted")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_generation is not None and expected_generation != self._generation(connection):
                return False
            cursor = connection.execute(
                "UPDATE agents SET last_run_status=?,last_result_excerpt=?,last_result_run_id=?,last_used_at=?,version=version+1 "
                "WHERE agent_name=? AND latest_run_id=? AND last_run_status='running'",
                (status, _clip(response, MAX_RESULT_CHARS), run_id, time.time(), agent_name, run_id),
            )
            return bool(cursor.rowcount)

    def record_tool_result(self, agent_name, run_id, tool_name, arguments, result, success,
                           expected_generation=None) -> bool:
        """Index IDs observed in known successful receipt shapes, not ownership."""
        if success is not True or not isinstance(result, dict):
            return False
        nested = [result[key] for key in ("data", "result") if isinstance(result.get(key), dict)]
        if any(item.get("error") or item.get("success") is False or item.get("successful") is False
               or item.get("status") in ("error", "failed")
               for item in [result] + nested):
            return False
        if tool_name not in {"gmail_create_draft", "gmail_execute_draft", "gmail_forward_email", "gmail_reply_to_thread", "gmail_list_drafts"}:
            return False
        refs = []
        def add(kind, value):
            if isinstance(value, str) and value.strip() and len(value) <= 256 and (kind, value) not in refs:
                refs.append((kind, value))
        for item in nested or [result]:
            if tool_name == "gmail_create_draft":
                add("draft", item.get("draft_id") or item.get("id"))
                if isinstance(item.get("draft"), dict):
                    add("draft", item["draft"].get("id"))
            elif tool_name in {"gmail_execute_draft", "gmail_forward_email", "gmail_reply_to_thread"}:
                add("message", item.get("id") or item.get("message_id"))
            elif isinstance(item.get("drafts"), list):
                for draft in item["drafts"][:MAX_OBSERVED_REFS]:
                    if isinstance(draft, dict):
                        add("draft", draft.get("id"))
            add("thread", item.get("threadId") or item.get("thread_id"))
            if isinstance(item.get("message"), dict):
                add("message", item["message"].get("id"))
                add("thread", item["message"].get("threadId") or item["message"].get("thread_id"))
        if not refs:
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_generation is not None and expected_generation != self._generation(connection):
                return False
            current = connection.execute("SELECT 1 FROM agents WHERE agent_name=? AND latest_run_id=? AND last_run_status='running'", (agent_name, run_id)).fetchone()
            if current is None:
                return False
            connection.executemany(
                "INSERT INTO observed_refs(agent_name,kind,value,source_run_id,observed_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(agent_name,kind,value) DO UPDATE SET source_run_id=excluded.source_run_id,observed_at=excluded.observed_at",
                [(agent_name, kind, value, run_id, time.time()) for kind, value in refs[:MAX_OBSERVED_REFS]],
            )
            connection.execute("DELETE FROM observed_refs WHERE agent_name=? AND rowid NOT IN "
                               "(SELECT rowid FROM observed_refs WHERE agent_name=? ORDER BY observed_at DESC,rowid DESC LIMIT ?)",
                               (agent_name, agent_name, MAX_OBSERVED_REFS))
            saved = [dict(row) for row in connection.execute("SELECT kind,value FROM observed_refs WHERE agent_name=? ORDER BY observed_at DESC,rowid DESC", (agent_name,))]
            connection.execute("UPDATE agents SET observed_refs_json=?,version=version+1 WHERE agent_name=?",
                               (json.dumps(saved, ensure_ascii=False), agent_name))
            return True

    def get_agent(self, agent_name: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_FIELDS} FROM agents WHERE agent_name=?", (agent_name,)
            ).fetchone()
            return self._with_publication(connection, _card(row) if row is not None else None)

    def search(self, query: str, limit: int = DEFAULT_MAX_CARDS) -> list[dict[str, Any]]:
        limit = _limit(limit)
        terms = _terms(query)
        if not limit or not terms:
            return []
        with self._connect() as connection:
            if self._fts_available:
                match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                fields = ", ".join("a." + field for field in _FIELDS.split(", "))
                rows = connection.execute(
                    f"SELECT {fields} FROM agent_fts JOIN agents a ON a.rowid=agent_fts.rowid "
                    "WHERE agent_fts MATCH ? ORDER BY bm25(agent_fts), a.last_used_at DESC, "
                    "a.agent_name LIMIT ?", (match, limit),
                ).fetchall()
            else:
                # This fallback returns bounded rows but may scan metadata in SQL.
                # It is not claimed to have the indexed scaling of FTS5.
                expression = "lower(agent_name || ' ' || purpose || ' ' || last_instruction || ' ' || last_result_excerpt)"
                clauses = [f"{expression} LIKE ?" for _ in terms]
                score = " + ".join(f"CASE WHEN {clause} THEN 1 ELSE 0 END" for clause in clauses)
                patterns = ["%" + term.lower() + "%" for term in terms]
                rows = connection.execute(
                    f"SELECT {_FIELDS}, ({score}) AS score FROM agents WHERE "
                    + " OR ".join(clauses)
                    + " ORDER BY score DESC, last_used_at DESC, agent_name LIMIT ?",
                    patterns + patterns + [limit],
                ).fetchall()
        return [_card(row) for row in rows]

    def match_observed_refs(self, query: str, limit: int = DEFAULT_MAX_CARDS) -> list[dict[str, Any]]:
        # Match bounded literal tokens by an index; never scan every stored ID or
        # infer IDs from natural-language descriptions. Sentence punctuation is data.
        values = list(dict.fromkeys([str(query).strip()] +
            [token.rstrip(".,!?;:") for token in re.findall(r"[^\s<>\"'(),;]+", str(query))] + _terms(query)))[:MAX_QUERY_TERMS]
        values = [value for value in values if value and len(value) <= 256]
        if not values or not _limit(limit):
            return []
        fields = ", ".join("a." + field for field in _FIELDS.split(", "))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {fields},r.kind AS ref_kind,r.value AS ref_value FROM observed_refs r "
                "JOIN agents a ON a.agent_name=r.agent_name WHERE r.value IN (" + ",".join("?" for _ in values) + ") "
                "ORDER BY r.observed_at DESC,a.agent_name LIMIT ?", values + [_limit(limit)],
            ).fetchall()
        return [dict(_card(row), matched_ref={"kind": row["ref_kind"], "value": row["ref_value"]}) for row in rows]

    def shortlist(
        self, query: str, include_focus: bool = True,
        max_cards: int = DEFAULT_MAX_CARDS, max_chars: int = DEFAULT_MAX_CHARS,
    ) -> dict[str, Any]:
        max_cards = _limit(max_cards, DEFAULT_MAX_CARDS)
        candidates: list[dict[str, Any]] = []
        reasons: dict[str, str] = {}

        def add(card: Optional[dict[str, Any]], reason: str) -> None:
            if card is not None and card["agent_name"] not in reasons:
                reasons[card["agent_name"]] = reason
                candidates.append(dict(card, selection_reason=reason))

        add(self.get_agent(str(query).strip()), "exact_name")
        for card in self.match_observed_refs(query, limit=max_cards):
            add(card, "observed_reference")
        if include_focus:
            for card in self.recent_published(limit=3):
                add(card, "published")
        for card in self.search(query, limit=max(max_cards * 3, 12)):
            add(card, self.search_backend)
        selected, rendered = _fit_cards(candidates, max_cards, max_chars)
        trace = {
            "backend": self.search_backend,
            "include_focus": bool(include_focus),
            "max_cards": max_cards,
            "max_chars": max(0, int(max_chars)),
            "rendered_chars": len(rendered),
            "candidates_observed": len(candidates),
            "omitted_observed": len(candidates) - len(selected),
            "selected": [
                {"agent_name": card["agent_name"], "reason": reasons[card["agent_name"]]}
                for card in selected
            ],
        }
        return {"cards": selected, "rendered": rendered, "trace": trace}

    def get_generation(self) -> int:
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT value FROM catalog_meta WHERE key='generation'"
            ).fetchone()["value"])

    def record_publication(
        self, source_results: list[dict[str, Any]], kind: str, message: str,
        expected_generation: Optional[int] = None,
    ) -> None:
        """Persist runtime-validated references, never infer ownership from text."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = int(connection.execute(
                "SELECT value FROM catalog_meta WHERE key='generation'"
            ).fetchone()["value"])
            if expected_generation is not None and generation != expected_generation:
                return
            seen: set[str] = set()
            for result in source_results:
                name = result.get("agent_name")
                result_id = result.get("result_id")
                correlation_id = result.get("correlation_id")
                if (not isinstance(name, str) or name in seen
                        or not isinstance(result_id, str) or not result_id
                        or not isinstance(correlation_id, str) or not correlation_id):
                    continue
                if connection.execute("SELECT 1 FROM agents WHERE agent_name=?", (name,)).fetchone() is None:
                    continue
                seen.add(name)
                cursor = connection.execute(
                    "INSERT INTO publications(agent_name,result_id,correlation_id,kind,message_excerpt,published_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (name, result_id, correlation_id, _clip(kind, 64), _clip(message, 1000), time.time()),
                )
                connection.execute(
                    "INSERT INTO published_focus(agent_name,publication_sequence) VALUES (?,?) "
                    "ON CONFLICT(agent_name) DO UPDATE SET publication_sequence=excluded.publication_sequence",
                    (name, cursor.lastrowid),
                )
                connection.execute("UPDATE agents SET version=version+1 WHERE agent_name=?", (name,))

    def recent_published(self, limit: int = 3) -> list[dict[str, Any]]:
        fields = ", ".join("a." + field for field in _FIELDS.split(", "))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {fields},p.result_id,p.kind,p.message_excerpt,p.published_at "
                "FROM published_focus f JOIN agents a ON a.agent_name=f.agent_name "
                "JOIN publications p ON p.sequence=f.publication_sequence "
                "ORDER BY f.publication_sequence DESC LIMIT ?", (_limit(limit),),
            ).fetchall()
        return [dict(_card(row), last_publication={"result_id": row["result_id"], "kind": row["kind"],
                     "excerpt": _clip(row["message_excerpt"], 200), "published_at": row["published_at"]}) for row in rows]

    def clear(self) -> None:
        """Clear discovery state and invalidate callbacks from the prior chat."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM published_focus")
            connection.execute("DELETE FROM publications")
            connection.execute("DELETE FROM observed_refs")
            connection.execute("DELETE FROM agents")
            connection.execute("DELETE FROM catalog_meta WHERE key='roster_fingerprint'")
            connection.execute(
                "UPDATE catalog_meta SET value=CAST(value AS INTEGER)+1 WHERE key='generation'"
            )
            self._roster_fingerprints.clear()


_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "execution_agents" / "catalog.sqlite3"
_catalog: Optional[AgentCatalog] = None
_catalog_lock = threading.Lock()


def get_agent_catalog() -> AgentCatalog:
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = AgentCatalog()
        return _catalog
