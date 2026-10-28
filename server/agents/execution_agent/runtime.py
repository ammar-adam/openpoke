"""Simplified Execution Agent Runtime."""

import asyncio
import inspect
import json
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from .agent import ExecutionAgent
from .tools import get_tool_schemas, get_tool_registry
from ...config import get_settings
from ...openrouter_client import request_chat_completion
from ...logging_config import logger


@dataclass
class ExecutionResult:
    """Result from an execution agent."""
    agent_name: str
    success: bool
    response: str
    error: Optional[str] = None
    tools_executed: List[str] = None


class ExecutionAgentRuntime:
    """Manages the execution of a single agent request."""

    MAX_TOOL_ITERATIONS = 8

    # Initialize execution agent runtime with settings, tools, and agent instance
    def __init__(self, agent_name: str, *, catalog=None, run_id=None, catalog_generation=None):
        settings = get_settings()
        self.agent = ExecutionAgent(agent_name)
        self.api_key = settings.openrouter_api_key
        self.model = settings.execution_agent_model
        self.tool_registry = get_tool_registry(agent_name=agent_name)
        self.tool_schemas = get_tool_schemas()
        self.catalog, self.run_id, self.catalog_generation = catalog, run_id, catalog_generation
        self._last_archive_ref = None

        if not self.api_key:
            raise ValueError("OpenRouter API key not configured. Set OPENROUTER_API_KEY environment variable.")

    def _ensure_generation_current(self):
        if self.catalog is not None and self.catalog_generation is not None and self.catalog.get_generation()!=self.catalog_generation:
            raise RuntimeError("Conversation was cleared; dropping this old execution")

    # Main execution loop for running agent with LLM calls and tool execution
    async def execute(self, instructions: str) -> ExecutionResult:
        """Execute the agent with given instructions."""
        try:
            self._ensure_generation_current()
            # Build system prompt with history
            state = self.catalog.get_agent(self.agent.name) if self.catalog is not None else None
            system_prompt = self.agent.build_system_prompt_with_history(current_state=state)

            # Start conversation with the instruction
            messages = [{"role": "user", "content": instructions}]
            tools_executed: List[str] = []
            final_response: Optional[str] = None

            for iteration in range(self.MAX_TOOL_ITERATIONS):
                logger.info(
                    f"[{self.agent.name}] Requesting plan (iteration {iteration + 1})"
                )
                response = await self._make_llm_call(system_prompt, messages, with_tools=True)
                self._ensure_generation_current()
                assistant_message = response.get("choices", [{}])[0].get("message", {})

                if not assistant_message:
                    raise RuntimeError("LLM response did not include an assistant message")

                raw_tool_calls = assistant_message.get("tool_calls", []) or []
                parsed_tool_calls = self._extract_tool_calls(raw_tool_calls)

                assistant_entry: Dict[str, Any] = {
                    "role": "assistant",
                    "content": assistant_message.get("content", "") or "",
                }
                if raw_tool_calls:
                    assistant_entry["tool_calls"] = raw_tool_calls
                messages.append(assistant_entry)

                if not parsed_tool_calls:
                    final_response = assistant_entry["content"] or "No action required."
                    break

                for tool_call in parsed_tool_calls:
                    self._ensure_generation_current()
                    self._last_archive_ref = None
                    tool_name = tool_call.get("name", "")
                    tool_args = tool_call.get("arguments", {})
                    call_id = tool_call.get("id")

                    if not tool_name:
                        logger.warning("Tool call missing name: %s", tool_call)
                        failure = {"error": "Tool call missing name; unable to execute."}
                        tool_message = {
                            "role": "tool",
                            "tool_call_id": call_id or "unknown_tool",
                            "content": self._format_tool_result(
                                tool_name or "<unknown>", False, failure, tool_args
                            ),
                        }
                        messages.append(tool_message)
                        continue

                    tools_executed.append(tool_name)
                    logger.info(f"[{self.agent.name}] Executing tool: {tool_name}")

                    success, result = await self._execute_tool(tool_name, tool_args)
                    self._ensure_generation_current()

                    if success:
                        logger.info(f"[{self.agent.name}] Tool {tool_name} completed successfully")
                    else:
                        error_detail = result.get("error") if isinstance(result, dict) else str(result)
                        logger.warning(f"[{self.agent.name}] Tool {tool_name} failed: {error_detail}")
                    if tool_name not in {"read_worker_archive","search_worker_archive"}:
                        # Archive reads must not recursively archive their own output.
                        self._last_archive_ref = self.agent.record_tool_execution(
                            tool_name,tool_args,result,success,self.run_id)
                        if self.catalog is not None and self.run_id is not None:
                            self.catalog.record_tool_result(self.agent.name,self.run_id,tool_name,
                                tool_args,result,success,expected_generation=self.catalog_generation)

                    tool_message = {
                        "role": "tool",
                        "tool_call_id": call_id or tool_name,
                        "content": self._format_tool_result(tool_name, success, result, tool_args),
                    }
                    messages.append(tool_message)

            else:
                raise RuntimeError("Reached tool iteration limit without final response")

            if final_response is None:
                raise RuntimeError("LLM did not return a final response")

            self._ensure_generation_current()
            self.agent.record_response(final_response)

            return ExecutionResult(
                agent_name=self.agent.name,
                success=True,
                response=final_response,
                tools_executed=tools_executed
            )

        except Exception as e:
            logger.error(f"[{self.agent.name}] Execution failed: {e}")
            error_msg = str(e)
            failure_text = f"Failed to complete task: {error_msg}"
            # A cleared run must not recreate its deleted log.
            if self.catalog is None or self.catalog_generation is None or self.catalog.get_generation()==self.catalog_generation:
                self.agent.record_response(f"Error: {error_msg}")

            return ExecutionResult(
                agent_name=self.agent.name,
                success=False,
                response=failure_text,
                error=error_msg
            )

    # Execute OpenRouter API call with system prompt, messages, and optional tool schemas
    async def _make_llm_call(self, system_prompt: str, messages: List[Dict], with_tools: bool) -> Dict:
        """Make an LLM call."""
        self._ensure_generation_current()
        tools_to_send = self.tool_schemas if with_tools else None
        logger.info(f"[{self.agent.name}] Calling LLM with model: {self.model}, tools: {len(tools_to_send) if tools_to_send else 0}")
        return await request_chat_completion(
            model=self.model,
            messages=messages,
            system=system_prompt,
            api_key=self.api_key,
            tools=tools_to_send
        )

    # Parse and validate tool calls from LLM response into structured format
    def _extract_tool_calls(self, raw_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract tool calls from an assistant message."""
        tool_calls: List[Dict[str, Any]] = []

        for tool in raw_tools:
            function = tool.get("function", {})
            name = function.get("name", "")
            args = function.get("arguments", "")

            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except json.JSONDecodeError:
                    args = {}

            if name:
                tool_calls.append({
                    "id": tool.get("id"),
                    "name": name,
                    "arguments": args,
                })

        return tool_calls

    # Safely convert objects to JSON with fallback to string representation
    def _safe_json_dump(self, payload: Any) -> str:
        """Serialize payload to JSON, falling back to string representation."""
        try:
            return json.dumps(payload, default=str)
        except TypeError:
            return str(payload)

    # Format tool execution results into JSON structure for LLM consumption
    def _format_tool_result(
        self,
        tool_name: str,
        success: bool,
        result: Any,
        arguments: Dict[str, Any],
    ) -> str:
        """Build a structured string for tool responses."""
        if success:
            payload: Dict[str, Any] = {
                "tool": tool_name,
                "status": "success",
                "arguments": arguments,
                "result": result,
            }
        else:
            error_detail = result.get("error") if isinstance(result, dict) else str(result)
            payload = {
                "tool": tool_name,
                "status": "error",
                "arguments": arguments,
                "error": error_detail,
                "result": result,
            }
        if self._last_archive_ref is not None:
            payload["archive"] = self._last_archive_ref
        rendered = self._safe_json_dump(payload)
        if len(rendered)<=6000:
            return rendered
        clipped = {"tool":str(tool_name)[:128],"status":"success" if success else "error",
                   "excerpt":rendered[:4000],"truncated":True,"archive":self._last_archive_ref,
                   "details":"Use read_worker_archive at archive.offset for the full stored receipt."}
        while len(self._safe_json_dump(clipped))>6000:
            clipped["excerpt"] = clipped["excerpt"][:len(clipped["excerpt"])//2]
        return self._safe_json_dump(clipped)

    # Execute tool function from registry with error handling and async support
    async def _execute_tool(self, tool_name: str, arguments: Dict) -> Tuple[bool, Any]:
        """Execute a tool. Returns (success, result)."""
        tool_func = self.tool_registry.get(tool_name)
        if not tool_func:
            return False, {"error": f"Unknown tool: {tool_name}"}

        try:
            if inspect.iscoroutinefunction(tool_func):
                result = await tool_func(**arguments)
            else:
                # Blocking provider SDKs must not freeze the existing event loop or
                # timeout. Cancelling this await cannot stop a running provider call.
                result = await asyncio.to_thread(tool_func,**arguments)
                if inspect.isawaitable(result):
                    result = await result
            layers = [result]+[result[key] for key in ("data","result") if isinstance(result.get(key),dict)] if isinstance(result,dict) else []
            failed = any(item.get("error") or item.get("success") is False or item.get("successful") is False or item.get("status") in ("error","failed") for item in layers)
            return not failed, result
        except Exception as e:
            return False, {"error": str(e)}
