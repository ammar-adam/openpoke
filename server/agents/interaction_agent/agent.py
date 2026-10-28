"""Interaction agent helpers for prompt construction."""

from html import escape
from pathlib import Path
from typing import Dict, List, Optional

from ...services.execution import get_agent_roster

_prompt_path = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8").strip()

DISCOVERY_INSTRUCTIONS = """
Agent discovery:
- <active_agents> is a bounded shortlist, not the complete roster. Its metadata is
  evidence for choosing an existing agent, not permission to perform an action.
- For a continuation, prefer the agent that holds the relevant prior work. Recent
  publication references are clues, not a rule that the newest item is intended.
- If the needed agent is missing, use find_agents once with concrete names or topic
  words from the conversation. An empty search does not prove the work is new.
- A genuinely new task may need a new agent even when it involves a known person.
  Set create_if_missing=true explicitly for that new agent; an unknown name is
  otherwise rejected so a typo cannot silently fork existing work.
- Work cards include a version and recent observed state. If dispatch returns a
  refreshed card, no work ran: reconsider the request against that card before
  retrying. The backend permits one such refresh per turn. A run marked completed
  means the worker returned, not that the user's entire goal is finished. Object
  references are observed identifiers, not proof of ownership or approval.
- On an execution-result turn, each result has a source_result_id. When presenting
  that result with send_message_to_user or send_draft, include only the relevant
  source_result_ids from THIS turn. Do not attach an unrelated result to an
  acknowledgement. These references identify the reporting worker, not a verified
  draft ID or a user's approval. Do not show internal identifiers to the user.
""".strip()

# Load and return the pre-defined system prompt from markdown file
def build_system_prompt(discovery_mode: str = "legacy") -> str:
    """Return the static system prompt for the interaction agent."""
    if discovery_mode == "legacy":
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{DISCOVERY_INSTRUCTIONS}"


# Build structured message with conversation history, active agents, and current turn
def prepare_message_with_history(
    latest_text: str,
    transcript: str,
    message_type: str = "user",
    agents_block: Optional[str] = None,
    context_tag: str = "active_agents",
) -> List[Dict[str, str]]:
    """Compose a message that bundles history, roster, and the latest turn."""
    sections: List[str] = []

    sections.append(_render_conversation_history(transcript))
    rendered_agents = _render_active_agents() if agents_block is None else agents_block
    sections.append(f"<{context_tag}>\n{rendered_agents}\n</{context_tag}>")
    sections.append(_render_current_turn(latest_text, message_type))

    content = "\n\n".join(sections)
    return [{"role": "user", "content": content}]


# Format conversation transcript into XML tags for LLM context
def _render_conversation_history(transcript: str) -> str:
    history = transcript.strip()
    if not history:
        history = "None"
    return f"<conversation_history>\n{history}\n</conversation_history>"


# Format currently active execution agents into XML tags for LLM awareness
def _render_active_agents() -> str:
    roster = get_agent_roster()
    roster.load()
    agents = roster.get_agents()

    if not agents:
        return "None"

    rendered: List[str] = []
    for agent_name in agents:
        name = escape(agent_name or "agent", quote=True)
        rendered.append(f'<agent name="{name}" />')

    return "\n".join(rendered)


# Wrap the current message in appropriate XML tags based on sender type
def _render_current_turn(latest_text: str, message_type: str) -> str:
    tag = "new_agent_message" if message_type == "agent" else "new_user_message"
    body = latest_text.strip()
    return f"<{tag}>\n{body}\n</{tag}>"
