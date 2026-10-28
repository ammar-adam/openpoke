"""Tool definitions for interaction agent."""

import asyncio
import copy
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ...logging_config import logger
from ...services.conversation import get_conversation_log
from ...services.execution import get_agent_roster, get_execution_agent_logs
from ..execution_agent.batch_manager import ExecutionBatchManager


@dataclass
class ToolResult:
    """Standardized payload returned by interaction-agent tools."""

    success: bool
    payload: Any = None
    user_message: Optional[str] = None
    recorded_reply: bool = False


@dataclass
class ToolContext:
    """Trusted runtime context; never populated from model tool arguments."""

    discovery_mode: str
    correlation_id: str
    catalog: Any = None
    generation: int = 0
    source_results: Dict[str, Dict[str, str]] = field(default_factory=dict)
    offered_versions: Dict[str, int] = field(default_factory=dict)
    state_refreshes: int = 0


_TOOL_CONTEXT: ContextVar[Optional[ToolContext]] = ContextVar("interaction_tool_context", default=None)


@contextmanager
def tool_context(context: ToolContext):
    token = _TOOL_CONTEXT.set(context)
    try:
        yield
    finally:
        _TOOL_CONTEXT.reset(token)


def _publication_sources(source_result_ids):
    if source_result_ids is None:
        return []
    context = _TOOL_CONTEXT.get()
    if not isinstance(source_result_ids, list) or any(not isinstance(item, str) for item in source_result_ids):
        raise ValueError("source_result_ids must be a list of result IDs")
    if not source_result_ids:
        return []
    if context is None or context.discovery_mode == "legacy":
        raise ValueError("No source results are available for this turn")
    if any(item not in context.source_results for item in source_result_ids):
        raise ValueError("Unknown source_result_id for this callback; nothing was published")
    return [context.source_results[item] for item in dict.fromkeys(source_result_ids)]


def _record_publication(sources, kind: str, message: str) -> None:
    context = _TOOL_CONTEXT.get()
    if context is not None and context.catalog is not None and sources:
        context.catalog.record_publication(
            sources, kind, message, expected_generation=context.generation
        )

# Tool schemas for OpenRouter
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": "Deliver instructions to a specific execution agent. Creates a new agent if the name doesn't exist in the roster, or reuses an existing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Human-readable agent name describing its purpose (e.g., 'Vercel Job Offer', 'Email to Sharanjeet'). This name will be used to identify and potentially reuse the agent."
                    },
                    "instructions": {"type": "string", "description": "Instructions for the agent to execute."},
                },
                "required": ["agent_name", "instructions"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "Deliver a natural-language response directly to the user. Use this for updates, confirmations, or any assistant response the user should see immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Plain-text message that will be shown to the user and recorded in the conversation log.",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_draft",
            "description": "Record an email draft so the user can review the exact text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email for the draft.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject for the draft.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body content (plain text).",
                    },
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait silently when a message is already in conversation history to avoid duplicating responses. Adds a <wait> log entry that is not visible to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why waiting (e.g., 'Message already sent', 'Draft already created').",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

_EXECUTION_BATCH_MANAGER = ExecutionBatchManager()


def _refresh_agent_state(context, card) -> ToolResult:
    """Show fresh evidence once; this path never queues work."""
    if card is None:
        return ToolResult(success=False, payload={"error": "Agent is no longer available. Search again; nothing was dispatched."})
    if context.state_refreshes >= 1:
        return ToolResult(success=False, payload={"error": "Agent state changed again. No work was dispatched; stop and explain that the work is still changing."})
    from ...services.execution.catalog import render_cards
    budget = 2500
    while True:
        rendered = render_cards([card], max_cards=1, max_chars=budget)
        if '"agent_name"' not in rendered:
            return ToolResult(success=False, payload={"error": "Agent identity exceeds the context budget; no work was dispatched."})
        payload = {
            "code": "agent_state_refresh", "work_card": rendered,
            "error": "No work was dispatched. This agent's state was missing or changed since you read it. Reconsider the request using this fresh card before retrying.",
        }
        # Match the actual IA tool envelope, including a second layer of JSON
        # escaping. Non-ASCII text can expand considerably under ensure_ascii.
        envelope = {"tool": "send_message_to_agent", "status": "error", "arguments": {}, "error": payload}
        if len(json.dumps(envelope)) <= 6000:
            break
        budget = budget // 2
    context.state_refreshes += 1
    context.offered_versions[card["agent_name"]] = card["version"]
    return ToolResult(success=False, payload=payload)


# Create or reuse execution agent and dispatch instructions asynchronously
def send_message_to_agent(agent_name: str, instructions: str, create_if_missing: bool = False) -> ToolResult:
    """Send instructions to an execution agent."""
    context = _TOOL_CONTEXT.get()
    bounded = context is not None and context.discovery_mode != "legacy"
    loop = None
    if bounded:
        if not isinstance(agent_name, str) or not agent_name.strip():
            return ToolResult(success=False, payload={"error": "agent_name must be a nonempty string"})
        if not isinstance(instructions, str):
            return ToolResult(success=False, payload={"error": "instructions must be a string"})
        if not isinstance(create_if_missing, bool):
            return ToolResult(success=False, payload={"error": "create_if_missing must be a boolean"})
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return ToolResult(success=False, payload={"error": "No event loop available; no work was recorded"})
    roster = get_agent_roster()
    if bounded:
        card = context.catalog.get_agent(agent_name)
        is_new = card is None
        if is_new and not create_if_missing:
            return ToolResult(success=False, payload={"error": "Agent not found. Search for existing work, or explicitly set create_if_missing for a genuinely new task."})
        expected_version = context.offered_versions.get(agent_name)
        if not is_new and expected_version is None:
            return _refresh_agent_state(context, card)
        from ...services.execution.catalog import StaleAgentState
        try:
            current = context.catalog.record_dispatch(
                agent_name, instructions, expected_version=expected_version,
                expected_generation=context.generation,
            )
        except StaleAgentState as exc:
            return _refresh_agent_state(context, exc.current_card)
        context.offered_versions[agent_name] = current["version"]
    else:
        roster.load()
        existing_agents = set(roster.get_agents())
        is_new = agent_name not in existing_agents

    if is_new:
        if bounded:
            roster.load()
        roster.add_agent(agent_name)

    get_execution_agent_logs().record_request(agent_name, instructions)

    action = "Created" if is_new else "Reused"
    logger.info(f"{action} agent: {agent_name}")

    async def _execute_async() -> None:
        try:
            if bounded:
                result = await _EXECUTION_BATCH_MANAGER.execute_agent(
                    agent_name, instructions, request_id=str(uuid.uuid4()),
                    correlation_id=context.correlation_id,
                    discovery_mode=context.discovery_mode, catalog=context.catalog,
                    catalog_generation=context.generation,
                )
            else:
                result = await _EXECUTION_BATCH_MANAGER.execute_agent(agent_name, instructions, discovery_mode="legacy")
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"Agent '{agent_name}' completed: {status}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"Agent '{agent_name}' failed: {str(exc)}")

    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("No running event loop available for async execution")
            return ToolResult(success=False, payload={"error": "No event loop available"})

    loop.create_task(_execute_async())

    return ToolResult(
        success=True,
        payload={
            "status": "submitted",
            "agent_name": agent_name,
            "new_agent_created": is_new,
        },
    )


# Send immediate message to user and record in conversation history
def send_message_to_user(message: str, source_result_ids: Optional[list[str]] = None) -> ToolResult:
    """Record a user-visible reply in the conversation log."""
    sources = _publication_sources(source_result_ids)
    log = get_conversation_log()
    log.record_reply(message)
    _record_publication(sources, "message", message)

    return ToolResult(
        success=True,
        payload={"status": "delivered"},
        user_message=message,
        recorded_reply=True,
    )


# Format and record email draft for user review
def send_draft(
    to: str,
    subject: str,
    body: str,
    source_result_ids: Optional[list[str]] = None,
) -> ToolResult:
    """Record a draft update in the conversation log for the interaction agent."""
    sources = _publication_sources(source_result_ids)
    log = get_conversation_log()

    message = f"To: {to}\nSubject: {subject}\n\n{body}"

    log.record_reply(message)
    _record_publication(sources, "draft", message)
    logger.info(f"Draft recorded for: {to}")

    return ToolResult(
        success=True,
        payload={
            "status": "draft_recorded",
            "to": to,
            "subject": subject,
        },
        recorded_reply=True,
    )


# Record silent wait state to avoid duplicate responses
def wait(reason: str) -> ToolResult:
    """Wait silently and add a wait log entry that is not visible to the user."""
    log = get_conversation_log()
    
    # Record a dedicated wait entry so the UI knows to ignore it
    log.record_wait(reason)
    

    return ToolResult(
        success=True,
        payload={
            "status": "waiting",
            "reason": reason,
        },
        recorded_reply=True,
    )


# Return predefined tool schemas for LLM function calling
def get_tool_schemas(discovery_mode: str = "legacy"):
    """Return OpenAI-compatible tool schemas."""
    if discovery_mode == "legacy":
        return TOOL_SCHEMAS
    schemas = copy.deepcopy(TOOL_SCHEMAS)
    for schema in schemas:
        function = schema["function"]
        if function["name"] == "send_message_to_agent":
            function["description"] = "Deliver instructions to an existing execution agent by exact name. Creating an agent requires create_if_missing=true."
            function["parameters"]["properties"]["create_if_missing"] = {
                "type": "boolean", "description": "Set true only for a genuinely new task after checking for relevant existing work. Defaults to false."
            }
        if function["name"] in {"send_message_to_user", "send_draft"}:
            function["parameters"]["properties"]["source_result_ids"] = {
                "type": "array", "items": {"type": "string"},
                "description": "Relevant execution result IDs from this callback that this message actually presents. Omit for acknowledgements or unrelated messages."
            }
    schemas.append({
        "type": "function", "function": {
            "name": "find_agents",
            "description": "Read-only search of the saved agent catalog. Available once per turn when the shortlist lacks relevant work. This does not dispatch or create agents.",
            "parameters": {
                "type": "object", "properties": {
                    "query": {"type": "string", "description": "Concrete counterpart or topic words from the conversation."},
                    "exact_name": {"type": "string", "description": "Optional known exact agent name for direct lookup."},
                }, "required": ["query"], "additionalProperties": False,
            },
        },
    })
    return schemas


# Route tool calls to appropriate handlers with argument validation and error handling
def handle_tool_call(name: str, arguments: Any) -> ToolResult:
    """Handle tool calls from interaction agent."""
    try:
        context = _TOOL_CONTEXT.get()
        if context is not None and context.catalog is not None and context.generation != context.catalog.get_generation():
            return ToolResult(success=False, payload={"error": "Conversation was cleared; this old turn cannot publish or dispatch work"})
        if isinstance(arguments, str):
            args = json.loads(arguments) if arguments.strip() else {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            return ToolResult(success=False, payload={"error": "Invalid arguments format"})

        if name == "send_message_to_agent":
            return send_message_to_agent(**args)
        if name == "send_message_to_user":
            return send_message_to_user(**args)
        if name == "send_draft":
            return send_draft(**args)
        if name == "wait":
            return wait(**args)

        logger.warning("unexpected tool", extra={"tool": name})
        return ToolResult(success=False, payload={"error": f"Unknown tool: {name}"})
    except json.JSONDecodeError:
        return ToolResult(success=False, payload={"error": "Invalid JSON"})
    except ValueError as exc:
        return ToolResult(success=False, payload={"error": str(exc)})
    except TypeError as exc:
        return ToolResult(success=False, payload={"error": f"Missing required arguments: {exc}"})
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("tool call failed", extra={"tool": name, "error": str(exc)})
        return ToolResult(success=False, payload={"error": "Failed to execute"})
