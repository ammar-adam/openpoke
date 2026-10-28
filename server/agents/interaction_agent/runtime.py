"""Interaction Agent Runtime - handles LLM calls for user and agent turns."""

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .agent import build_system_prompt, prepare_message_with_history
from .tools import ToolContext, ToolResult, get_tool_schemas, handle_tool_call, tool_context
from ...config import get_settings
from ...services.conversation import get_conversation_log, get_working_memory_log
from ...services.execution import get_agent_roster
from ...services.execution.catalog import render_cards
from ...openrouter_client import request_chat_completion
from ...logging_config import logger


@dataclass
class InteractionResult:
    """Result from the interaction agent."""

    success: bool
    response: str
    error: Optional[str] = None
    execution_agents_used: int = 0


@dataclass
class _ToolCall:
    """Parsed tool invocation from an LLM response."""

    identifier: Optional[str]
    name: str
    arguments: Dict[str, Any]


@dataclass
class _LoopSummary:
    """Aggregate information produced by the interaction loop."""

    last_assistant_text: str = ""
    user_messages: List[str] = field(default_factory=list)
    tool_names: List[str] = field(default_factory=list)
    execution_agents: Set[str] = field(default_factory=set)
    waiting: bool = False


class InteractionAgentRuntime:
    """Manages the interaction agent's request processing."""

    MAX_TOOL_ITERATIONS = 8

    # Initialize interaction agent runtime with settings and service dependencies
    def __init__(self, discovery_mode: Optional[str] = None, catalog=None, model_client=None) -> None:
        settings = get_settings()
        self.api_key = settings.openrouter_api_key
        self.model = settings.interaction_agent_model
        self.settings = settings
        self.conversation_log = get_conversation_log()
        self.working_memory_log = get_working_memory_log()
        self.discovery_mode = discovery_mode or os.getenv("OPENPOKE_AGENT_DISCOVERY", "legacy")
        if self.discovery_mode not in {"legacy", "retrieval", "focus"}:
            raise ValueError("OPENPOKE_AGENT_DISCOVERY must be legacy, retrieval, or focus")
        self.catalog = catalog
        if self.discovery_mode in {"retrieval", "focus"} and self.catalog is None:
            from ...services.execution.catalog import get_agent_catalog
            self.catalog = get_agent_catalog()
        self.tool_schemas = get_tool_schemas(self.discovery_mode)
        self.model_client = model_client
        self.last_discovery_trace: Dict[str, Any] = {}
        self._search_expansions = 0
        self._active_context: Optional[ToolContext] = None
        self._offered_versions: Dict[str, int] = {}

        if not self.api_key and model_client is None:
            raise ValueError(
                "OpenRouter API key not configured. Set OPENROUTER_API_KEY environment variable."
            )

    # Main entry point for processing user messages through the LLM interaction loop
    async def execute(self, user_message: str) -> InteractionResult:
        """Handle a user-authored message."""
        try:
            context = self._new_context()
            self._ensure_context_current(context)
            transcript_before = self._load_conversation_transcript()
            self.conversation_log.record_user_message(user_message)

            system_prompt = build_system_prompt(self.discovery_mode)
            messages = self._prepare_messages(user_message, transcript_before, "user")

            logger.info("Processing user message through interaction agent")
            summary = await self._run_interaction_loop(system_prompt, messages, context)
            self._ensure_context_current(context)

            final_response = self._finalize_response(summary)

            if final_response and not summary.user_messages:
                self.conversation_log.record_reply(final_response)

            return InteractionResult(
                success=True,
                response=final_response,
                execution_agents_used=len(summary.execution_agents),
            )

        except Exception as exc:
            logger.error("Interaction agent failed", extra={"error": str(exc)})
            return InteractionResult(
                success=False,
                response="",
                error=str(exc),
            )

    # Handle incoming messages from execution agents and generate appropriate responses
    async def handle_agent_message(
        self, agent_message: str, source_results: Optional[List[Dict[str, str]]] = None,
        correlation_id: Optional[str] = None, catalog_generation: Optional[int] = None,
    ) -> InteractionResult:
        """Process a status update emitted by an execution agent."""

        try:
            context = self._new_context(source_results, correlation_id, catalog_generation)
            self._ensure_context_current(context)
            transcript_before = self._load_conversation_transcript()
            self.conversation_log.record_agent_message(agent_message)

            system_prompt = build_system_prompt(self.discovery_mode)
            messages = self._prepare_messages(agent_message, transcript_before, "agent")

            logger.info("Processing execution agent results")
            summary = await self._run_interaction_loop(system_prompt, messages, context)
            self._ensure_context_current(context)

            final_response = self._finalize_response(summary)

            if final_response and not summary.user_messages:
                self.conversation_log.record_reply(final_response)

            return InteractionResult(
                success=True,
                response=final_response,
                execution_agents_used=len(summary.execution_agents),
            )

        except Exception as exc:
            logger.error("Interaction agent (agent message) failed", extra={"error": str(exc)})
            return InteractionResult(
                success=False,
                response="",
                error=str(exc),
            )

    def _new_context(self, source_results=None, correlation_id=None, catalog_generation=None) -> ToolContext:
        sources = {}
        for source in source_results or []:
            if not isinstance(source, dict) or any(not isinstance(source.get(key), str) or not source[key] for key in ("result_id", "agent_name", "correlation_id")):
                raise ValueError("Invalid trusted execution-result metadata")
            if source["result_id"] in sources:
                raise ValueError("Duplicate execution result ID")
            sources[source["result_id"]] = {key: source[key] for key in ("result_id", "agent_name", "correlation_id")}
        source_correlations = {source["correlation_id"] for source in sources.values()}
        if len(source_correlations) > 1:
            raise ValueError("Execution results from different originating turns cannot share a callback")
        correlation = correlation_id or next(iter(source_correlations), str(uuid.uuid4()))
        if source_correlations and source_correlations != {correlation}:
            raise ValueError("Execution result correlation does not match its callback")
        generation = self.catalog.get_generation() if self.catalog is not None else 0
        if catalog_generation is not None:
            generation = catalog_generation
        self.last_discovery_trace = {"mode": self.discovery_mode, "initial": None, "expansions": [], "state_refreshes": []}
        self._offered_versions = {}
        return ToolContext(self.discovery_mode, correlation, self.catalog, generation, sources, self._offered_versions)

    def _ensure_context_current(self, context: ToolContext) -> None:
        if context.catalog is not None and context.generation != context.catalog.get_generation():
            raise RuntimeError("Conversation was cleared; dropping this old turn")

    def _prepare_messages(self, latest_text, transcript, message_type):
        if self.discovery_mode == "legacy":
            return prepare_message_with_history(latest_text, transcript, message_type=message_type)
        self.catalog.ensure_legacy_backfill(get_agent_roster())
        shortlist = self.catalog.shortlist_for_turn(
            latest_text, transcript, message_type=message_type,
            include_focus=self.discovery_mode == "focus", max_cards=12, max_chars=6000
        )
        self.last_discovery_trace["initial"] = shortlist["trace"]
        self._offered_versions.update({card["agent_name"]: card["version"] for card in shortlist["cards"]})
        return prepare_message_with_history(
            latest_text, transcript, message_type=message_type, agents_block=shortlist["rendered"]
        )

    # Each turn has independent tool metadata, search budget, and clear-generation guard.
    async def _run_interaction_loop(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        context: Optional[ToolContext] = None,
    ) -> _LoopSummary:
        context = context or self._new_context()
        previous = self._active_context
        self._active_context = context
        self._search_expansions = 0
        try:
            with tool_context(context):
                return await self._run_scoped_interaction_loop(system_prompt, messages)
        finally:
            self._active_context = previous

    async def _run_scoped_interaction_loop(self, system_prompt, messages) -> _LoopSummary:
        """Iteratively query the LLM until it issues a final response."""

        summary = _LoopSummary()

        for iteration in range(self.MAX_TOOL_ITERATIONS):
            self._ensure_context_current(self._active_context)
            response = await self._make_llm_call(system_prompt, messages)
            self._ensure_context_current(self._active_context)
            assistant_message = self._extract_assistant_message(response)

            assistant_content = (assistant_message.get("content") or "").strip()
            if assistant_content:
                summary.last_assistant_text = assistant_content

            raw_tool_calls = assistant_message.get("tool_calls") or []
            parsed_tool_calls = self._parse_tool_calls(raw_tool_calls)

            assistant_entry: Dict[str, Any] = {
                "role": "assistant",
                "content": assistant_message.get("content", "") or "",
            }
            if raw_tool_calls:
                assistant_entry["tool_calls"] = raw_tool_calls
            messages.append(assistant_entry)

            if not parsed_tool_calls:
                break

            for tool_call in parsed_tool_calls:
                summary.tool_names.append(tool_call.name)

                if tool_call.name == "send_message_to_agent":
                    agent_name = tool_call.arguments.get("agent_name")
                    if isinstance(agent_name, str) and agent_name:
                        summary.execution_agents.add(agent_name)

                result = self._execute_tool(tool_call)

                if tool_call.name == "wait" and result.success:
                    summary.waiting = True

                if result.user_message:
                    summary.user_messages.append(result.user_message)

                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.identifier or tool_call.name,
                    "content": self._format_tool_result(tool_call, result),
                }
                messages.append(tool_message)
            # A successful wait yields this turn, not its pending workers. Finish
            # the whole batch first: a dispatch/publication can follow wait in
            # the same response and must not be silently dropped.
            if summary.waiting:
                break
        else:
            raise RuntimeError("Reached tool iteration limit without final response")

        if not summary.waiting and not summary.user_messages and not summary.last_assistant_text:
            logger.warning("Interaction loop exited without assistant content")

        return summary

    # Load conversation history, preferring summarized version if available
    def _load_conversation_transcript(self) -> str:
        if self.settings.summarization_enabled:
            rendered = self.working_memory_log.render_transcript()
            if rendered.strip():
                return rendered
        return self.conversation_log.load_transcript()

    # Execute API call to OpenRouter with system prompt, messages, and tool schemas
    async def _make_llm_call(
        self,
        system_prompt: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Make an LLM call via OpenRouter."""

        logger.debug(
            "Interaction agent calling LLM",
            extra={"model": self.model, "tools": len(self.tool_schemas)},
        )
        client = self.model_client or request_chat_completion
        return await client(
            model=self.model,
            messages=messages,
            system=system_prompt,
            api_key=self.api_key,
            tools=self.tool_schemas,
        )

    # Extract the assistant's message from the OpenRouter API response structure
    def _extract_assistant_message(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Return the assistant message from the raw response payload."""

        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("LLM response did not include an assistant message")
        return message

    # Convert raw LLM tool calls into structured _ToolCall objects with validation
    def _parse_tool_calls(self, raw_tool_calls: List[Dict[str, Any]]) -> List[_ToolCall]:
        """Normalize tool call payloads from the LLM."""

        parsed: List[_ToolCall] = []
        for raw in raw_tool_calls:
            function_block = raw.get("function") or {}
            name = function_block.get("name")
            if not isinstance(name, str) or not name:
                logger.warning("Skipping tool call without name", extra={"tool": raw})
                continue

            arguments, error = self._parse_tool_arguments(function_block.get("arguments"))
            if error:
                logger.warning("Tool call arguments invalid", extra={"tool": name, "error": error})
                parsed.append(
                    _ToolCall(
                        identifier=raw.get("id"),
                        name=name,
                        arguments={"__invalid_arguments__": error},
                    )
                )
                continue

            parsed.append(
                _ToolCall(identifier=raw.get("id"), name=name, arguments=arguments)
            )

        return parsed

    # Parse and validate tool arguments from various formats (dict, JSON string, etc.)
    def _parse_tool_arguments(
        self, raw_arguments: Any
    ) -> tuple[Dict[str, Any], Optional[str]]:
        """Convert tool arguments into a dictionary, reporting errors."""

        if raw_arguments is None:
            return {}, None

        if isinstance(raw_arguments, dict):
            return raw_arguments, None

        if isinstance(raw_arguments, str):
            if not raw_arguments.strip():
                return {}, None
            try:
                parsed = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                return {}, f"invalid json: {exc}"
            if isinstance(parsed, dict):
                return parsed, None
            return {}, "decoded arguments were not an object"

        return {}, f"unsupported argument type: {type(raw_arguments).__name__}"

    # Execute tool calls with error handling and logging, returning standardized results
    def _execute_tool(self, tool_call: _ToolCall) -> ToolResult:
        """Execute a tool call and convert low-level errors into structured results."""

        if "__invalid_arguments__" in tool_call.arguments:
            error = tool_call.arguments["__invalid_arguments__"]
            self._log_tool_invocation(tool_call, stage="rejected", detail={"error": error})
            return ToolResult(success=False, payload={"error": error})

        try:
            if self._active_context is not None:
                self._ensure_context_current(self._active_context)
            self._log_tool_invocation(tool_call, stage="start")
            if tool_call.name == "find_agents" and self.discovery_mode != "legacy":
                result = self._find_agents(tool_call.arguments)
            else:
                result = handle_tool_call(tool_call.name, tool_call.arguments)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(
                "Tool execution crashed",
                extra={"tool": tool_call.name, "error": str(exc)},
            )
            self._log_tool_invocation(
                tool_call,
                stage="error",
                detail={"error": str(exc)},
            )
            return ToolResult(success=False, payload={"error": str(exc)})

        if not isinstance(result, ToolResult):
            logger.warning(
                "Tool did not return ToolResult; coercing",
                extra={"tool": tool_call.name},
            )
            wrapped = ToolResult(success=True, payload=result)
            self._log_tool_invocation(tool_call, stage="done", result=wrapped)
            return wrapped

        status = "success" if result.success else "error"
        logger.debug(
            "Tool executed",
            extra={
                "tool": tool_call.name,
                "status": status,
            },
        )
        self._log_tool_invocation(tool_call, stage="done", result=result)
        if isinstance(result.payload, dict) and result.payload.get("code") == "agent_state_refresh":
            self.last_discovery_trace["state_refreshes"].append({"agent_name": tool_call.arguments.get("agent_name"), "dispatched": False})
        return result

    def _find_agents(self, arguments: Dict[str, Any]) -> ToolResult:
        if self._search_expansions >= 1:
            return ToolResult(success=False, payload={"error": "Discovery expansion limit reached (one per turn)"})
        query = arguments.get("query")
        exact_name = arguments.get("exact_name")
        if not isinstance(query, str) or (exact_name is not None and not isinstance(exact_name, str)) or set(arguments) - {"query", "exact_name"}:
            return ToolResult(success=False, payload={"error": "find_agents requires query text and an optional exact_name"})
        if len(query) > 1000 or (exact_name is not None and len(exact_name) > 1000):
            return ToolResult(success=False, payload={"error": "Search input is too long (maximum 1000 characters per field)"})
        if len(json.dumps(arguments)) > 1500:
            return ToolResult(success=False, payload={"error": "Encoded search input is too long"})
        self._search_expansions += 1
        if exact_name is not None:
            card = self.catalog.get_agent(exact_name)
            cards = [card] if card is not None else []
            rendered = render_cards(cards, max_cards=12, max_chars=6000)
            trace = {"method": "exact_name", "query": exact_name, "agent_names": [card["agent_name"] for card in cards], "rendered_chars": len(rendered)}
        else:
            shortlist = self.catalog.shortlist(query, include_focus=False, max_cards=12, max_chars=6000)
            cards, rendered, trace = shortlist["cards"], shortlist["rendered"], shortlist["trace"]
        # Bound the actual serialized tool message, including JSON escaping and
        # echoed arguments. Drop whole cards rather than corrupting identities.
        while True:
            result = ToolResult(success=True, payload={
                "agents": rendered, "match_count": len(cards),
                "note": "Bounded discovery, not the entire roster. No match does not prove a task is new.",
            })
            message_chars = len(self._format_tool_result(_ToolCall(None, "find_agents", arguments), result))
            if message_chars <= 6000 or not cards:
                break
            cards = cards[:-1]
            rendered = render_cards(cards, max_cards=12, max_chars=6000)
        trace = dict(trace, returned_agent_names=[card["agent_name"] for card in cards], model_result_chars=message_chars)
        self.last_discovery_trace["expansions"].append(trace)
        self._offered_versions.update({card["agent_name"]: card["version"] for card in cards})
        return result

    # Format tool execution results into JSON for LLM consumption
    def _format_tool_result(self, tool_call: _ToolCall, result: ToolResult) -> str:
        """Render a tool execution result back to the LLM."""

        payload: Dict[str, Any] = {
            "tool": tool_call.name,
            "status": "success" if result.success else "error",
            "arguments": {
                key: value
                for key, value in tool_call.arguments.items()
                if key != "__invalid_arguments__"
            },
        }

        if tool_call.name in {"find_agents", "send_message_to_agent"} and self.discovery_mode != "legacy":
            # The model already supplied the query. Echoing it can make even an
            # input-validation error arbitrarily large and defeat output limits.
            payload["arguments"] = {}

        if result.payload is not None:
            key = "result" if result.success else "error"
            payload[key] = result.payload

        rendered = self._safe_json_dump(payload)
        if tool_call.name == "find_agents" and self.discovery_mode != "legacy" and not result.success and len(rendered) > 6000:
            return self._safe_json_dump({"tool": "find_agents", "status": "error", "error": "Discovery failed; error detail exceeded the output budget"})
        return rendered

    # Safely serialize objects to JSON with fallback to string representation
    def _safe_json_dump(self, payload: Any) -> str:
        """Serialize payload to JSON, falling back to repr on failure."""

        try:
            return json.dumps(payload, default=str)
        except TypeError:
            return repr(payload)

    # Log tool execution stages (start, done, error) with structured metadata
    def _log_tool_invocation(
        self,
        tool_call: _ToolCall,
        *,
        stage: str,
        result: Optional[ToolResult] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit structured logs for tool lifecycle events."""

        cleaned_args = {
            key: value
            for key, value in tool_call.arguments.items()
            if key != "__invalid_arguments__"
        }

        log_payload: Dict[str, Any] = {
            "tool": tool_call.name,
            "stage": stage,
            "arguments": cleaned_args,
        }

        if result is not None:
            log_payload["success"] = result.success
            if result.payload is not None:
                log_payload["payload"] = result.payload

        if detail:
            log_payload.update(detail)

        if stage == "done":
            logger.info(f"Tool '{tool_call.name}' completed")
        elif stage in {"error", "rejected"}:
            logger.warning(f"Tool '{tool_call.name}' {stage}")
        else:
            logger.debug(f"Tool '{tool_call.name}' {stage}")

    # Determine final user-facing response from interaction loop summary
    def _finalize_response(self, summary: _LoopSummary) -> str:
        """Decide what text should be exposed to the user as the final reply."""

        if summary.user_messages:
            return summary.user_messages[-1]

        if summary.waiting:
            return ""

        return summary.last_assistant_text
