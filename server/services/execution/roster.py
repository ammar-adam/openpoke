"""Simple agent roster management - just a list of agent names."""

import json
import time
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no fcntl; the prototype runs in one process.
    fcntl = None

from ...logging_config import logger


# Protect roster operations between threads, including separate roster instances.
# On Windows this is NOT an interprocess lock.
_ROSTER_LOCK = threading.RLock()


class AgentRoster:
    """Simple roster that stores agent names in a JSON file."""

    def __init__(self, roster_path: Path):
        self._roster_path = roster_path
        self._agents: list[str] = []
        self.load()

    def load(self) -> None:
        """Load agent names from roster.json."""
        with _ROSTER_LOCK:
            self._load()

    def _load(self) -> None:
        if self._roster_path.exists():
            try:
                with open(self._roster_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._agents = [str(name) for name in data]
            except Exception as exc:
                logger.warning(f"Failed to load roster.json: {exc}")
                self._agents = []
        else:
            self._agents = []
            self.save()

    def save(self) -> None:
        """Save with a thread lock and an additional Unix process lock."""
        with _ROSTER_LOCK:
            self._save()

    def _save(self) -> None:
        max_retries = 5
        retry_delay = 0.1

        for attempt in range(max_retries):
            try:
                self._roster_path.parent.mkdir(parents=True, exist_ok=True)

                # Do not truncate before obtaining the lock.
                with open(self._roster_path, 'a+', encoding='utf-8') as f:
                    if fcntl is not None:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    try:
                        f.seek(0)
                        f.truncate()
                        json.dump(self._agents, f, indent=2)
                        f.flush()
                        return
                    finally:
                        if fcntl is not None:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            except BlockingIOError:
                # Lock is held by another process
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.warning("Failed to acquire lock on roster.json after retries")
            except Exception as exc:
                logger.warning(f"Failed to save roster.json: {exc}")
                break

    def add_agent(self, agent_name: str) -> None:
        """Add an agent to the roster if not already present."""
        with _ROSTER_LOCK:
            if agent_name not in self._agents:
                self._agents.append(agent_name)
                self.save()

    def get_agents(self) -> list[str]:
        """Get list of all agent names."""
        with _ROSTER_LOCK:
            return list(self._agents)

    def fingerprint(self) -> tuple[str, int | None, int | None]:
        """Cheap change detection for discovery backfill; does not read JSON."""
        with _ROSTER_LOCK:
            try:
                stat = self._roster_path.stat()
                return str(self._roster_path.resolve()), stat.st_mtime_ns, stat.st_size
            except FileNotFoundError:
                return str(self._roster_path.resolve()), None, None

    def clear(self) -> None:
        """Clear the agent roster."""
        with _ROSTER_LOCK:
            self._clear()

    def _clear(self) -> None:
        self._agents = []
        try:
            if self._roster_path.exists():
                self._roster_path.unlink()
            logger.info("Cleared agent roster")
        except Exception as exc:
            logger.warning(f"Failed to clear roster.json: {exc}")


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_ROSTER_PATH = _DATA_DIR / "execution_agents" / "roster.json"

_agent_roster = AgentRoster(_ROSTER_PATH)


def get_agent_roster() -> AgentRoster:
    """Get the singleton roster instance."""
    return _agent_roster
