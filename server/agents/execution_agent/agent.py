"""Execution Agent implementation."""

import json
from pathlib import Path
from typing import List, Optional, Dict, Any

from ...services.execution import get_execution_agent_logs
from ...logging_config import logger


# Load system prompt template from file
_prompt_path = Path(__file__).parent / "system_prompt.md"
if _prompt_path.exists():
    SYSTEM_PROMPT_TEMPLATE = _prompt_path.read_text(encoding="utf-8").strip()
else:
    # Placeholder template - you'll replace this with actual instructions
    SYSTEM_PROMPT_TEMPLATE = """You are an execution agent responsible for completing specific tasks using available tools.

Agent Name: {agent_name}
Purpose: {agent_purpose}

Instructions:
[TO BE FILLED IN BY USER]

You have access to Gmail tools to help complete your tasks. When given instructions:
1. Analyze what needs to be done
2. Use the appropriate tools to complete the task
3. Provide clear status updates on your actions

Be thorough, accurate, and efficient in your execution."""


class ExecutionAgent:
    """Manages state and history for an execution agent."""

    # Initialize execution agent with name, conversation limits, and log store access
    def __init__(
        self,
        name: str,
        conversation_limit: Optional[int] = None
    ):
        """
        Initialize an execution agent.

        Args:
            name: Human-readable agent name (e.g., 'conversation with keith')
            conversation_limit: Retained for compatibility; history is always character-bounded.
        """
        self.name = name
        self.conversation_limit = conversation_limit
        self._log_store = get_execution_agent_logs()

    # Generate system prompt template with agent name and purpose derived from name
    def build_system_prompt(self) -> str:
        """Build the system prompt for this agent."""
        agent_purpose = f"Handle tasks related to: {self.name}"

        return SYSTEM_PROMPT_TEMPLATE.format(
            agent_name=self.name,
            agent_purpose=agent_purpose
        )

    # Combine base system prompt with conversation history, applying conversation limits
    def build_system_prompt_with_history(self, current_state=None) -> str:
        """
        Build system prompt including agent history.

        Returns:
            System prompt with embedded history transcript
        """
        base_prompt = self.build_system_prompt()

        memory = json.dumps(self._log_store.recent_memory(self.name,max_chars=12000))
        state = {}
        if isinstance(current_state,dict):
            for key in ("version","last_run_status","latest_run_id","last_result_run_id"):
                state[key] = current_state.get(key)
            state["last_instruction"] = str(current_state.get("last_instruction", ""))[:512]
            state["last_result_excerpt_unverified"] = str(current_state.get("last_result_excerpt", ""))[:600]
            state["observed_refs"] = list(current_state.get("observed_refs") or [])[:8]
            state["omitted_refs"] = max(0,len(current_state.get("observed_refs") or [])-8)
            while len(json.dumps(state))>4000 and state["observed_refs"]:
                state["observed_refs"].pop()
                state["omitted_refs"] += 1
            if len(json.dumps(state))>4000:
                state = {"note":"Current state exceeded its display bound; inspect the archive."}
        instructions = (
            "History is an excerpt, not a semantic summary or complete list of constraints. "
            "Use search_worker_archive for older facts; continue its next_offset until null before concluding no match. "
            "Use read_worker_archive with a returned byte offset to retrieve exact stored text, following next_offset. "
            "Chunks are raw XML journal text: concatenate them before decoding XML entities or JSON receipt escapes. "
            "Observed object IDs and run status are hints, not proof of current provider state, ownership, or user approval. "
            "Legacy logs may already have truncated details; the archive cannot restore those."
        )
        return f"{base_prompt}\n\n# Compact worker state\n{json.dumps(state)}\n\n# Recent archive excerpt\n{memory}\n\n{instructions}"

    # Format current instruction as user message for LLM consumption
    def build_messages_for_llm(self, current_instruction: str) -> List[Dict[str, str]]:
        """
        Build message array for LLM call.

        Args:
            current_instruction: Current instruction from interaction agent

        Returns:
            List of messages in OpenRouter format
        """
        return [
            {"role": "user", "content": current_instruction}
        ]

    # Log the agent's final response to the execution log store
    def record_response(self, response: str) -> None:
        """Record agent's response to the log."""
        self._log_store.record_agent_response(self.name, response)

    def record_tool_execution(self, tool_name, arguments, result, success=True, run_id=None):
        """Persist full new receipts; clipping happens only in the model-facing view."""
        return self._log_store.record_tool_receipt(self.name,tool_name,arguments,result,success,run_id)
