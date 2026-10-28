"""Coordinate execution agents and batch their results for the interaction agent."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional

from .runtime import ExecutionAgentRuntime, ExecutionResult
from ...logging_config import logger


@dataclass
class PendingExecution:
    """Track a pending execution request."""

    request_id: str
    agent_name: str
    instructions: str
    batch_id: str
    correlation_id: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class _BatchState:
    """Collect results for a single interaction-agent turn."""

    batch_id: str
    created_at: datetime = field(default_factory=datetime.now)
    pending: int = 0
    results: List[ExecutionResult] = field(default_factory=list)
    sources: List[Dict[str, str]] = field(default_factory=list)
    correlation_id: Optional[str] = None
    discovery_mode: str = "legacy"
    catalog: Any = None
    catalog_generation: Optional[int] = None


class ExecutionBatchManager:
    """Run execution agents and deliver their combined outcome."""

    # Initialize batch manager with timeout and coordination state for execution agents
    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = timeout_seconds
        self._pending: Dict[str, PendingExecution] = {}
        self._batch_lock = asyncio.Lock()
        self._batches: Dict[str, _BatchState] = {}
        self._legacy_batch_id: Optional[str] = None

    # Run execution agent with timeout handling and batch coordination for interaction agent
    async def execute_agent(
        self,
        agent_name: str,
        instructions: str,
        request_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        discovery_mode: Optional[str] = None,
        catalog: Any = None,
        catalog_generation: Optional[int] = None,
    ) -> ExecutionResult:
        """Execute an agent asynchronously and buffer the result for batch dispatch."""

        # Direct scheduler callers have no IA ToolContext, so capture the active
        # discovery configuration and clear-generation before starting work.
        discovery_mode = discovery_mode if discovery_mode is not None else os.getenv("OPENPOKE_AGENT_DISCOVERY", "legacy")
        if discovery_mode not in {"legacy", "retrieval", "focus"}:
            raise ValueError("OPENPOKE_AGENT_DISCOVERY must be legacy, retrieval, or focus")
        if discovery_mode != "legacy":
            if catalog is None:
                from ...services.execution.catalog import get_agent_catalog
                catalog = get_agent_catalog()
            if catalog_generation is None:
                catalog_generation = catalog.get_generation()
            elif catalog_generation != catalog.get_generation():
                return ExecutionResult(agent_name, False, "Conversation was cleared before execution", error="Stale generation")
            correlation_id = correlation_id or str(uuid.uuid4())

        if not request_id:
            request_id = str(uuid.uuid4())

        batch_id = await self._register_pending_execution(
            agent_name, instructions, request_id, correlation_id, discovery_mode,
            catalog, catalog_generation,
        )

        cancelled = False
        try:
            logger.info(f"[{agent_name}] Execution started")
            if discovery_mode != "legacy":
                if not catalog.record_run_started(
                    agent_name, request_id, instructions,
                    expected_generation=catalog_generation,
                ):
                    raise RuntimeError("Conversation changed before this worker could start")
                runtime = ExecutionAgentRuntime(
                    agent_name=agent_name, catalog=catalog, run_id=request_id,
                    catalog_generation=catalog_generation,
                )
            else:
                runtime = ExecutionAgentRuntime(agent_name=agent_name)
            result = await asyncio.wait_for(
                runtime.execute(instructions),
                timeout=self.timeout_seconds,
            )
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"[{agent_name}] Execution finished: {status}")
        except asyncio.CancelledError:
            # Balance the in-memory batch even if the caller stops waiting.
            # An already running provider/thread may still finish remotely.
            cancelled = True
            result = ExecutionResult(
                agent_name=agent_name, success=False,
                response="Execution was interrupted; any in-flight external action may have an unknown outcome.",
                error="Cancelled",
            )
        except asyncio.TimeoutError:
            logger.error(f"[{agent_name}] Execution timed out after {self.timeout_seconds}s")
            result = ExecutionResult(
                agent_name=agent_name,
                success=False,
                response=f"Execution timed out after {self.timeout_seconds} seconds",
                error="Timeout",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"[{agent_name}] Execution failed unexpectedly")
            result = ExecutionResult(
                agent_name=agent_name,
                success=False,
                response=f"Execution failed: {exc}",
                error=str(exc),
            )
        finally:
            self._pending.pop(request_id, None)

        if discovery_mode != "legacy":
            # Run completion is an observation, not proof the user's goal is done.
            catalog.record_run_finished(
                agent_name, request_id,
                "completed" if result.success else ("interrupted" if result.error in {"Timeout", "Cancelled"} else "failed"),
                result.response, expected_generation=catalog_generation,
            )
        await asyncio.shield(self._complete_execution(batch_id, result, agent_name, request_id))
        if cancelled:
            raise asyncio.CancelledError
        return result

    # Add execution request to current batch or create new batch if none exists
    async def _register_pending_execution(
        self,
        agent_name: str,
        instructions: str,
        request_id: str,
        correlation_id: Optional[str] = None,
        discovery_mode: str = "legacy",
        catalog: Any = None,
        catalog_generation: Optional[int] = None,
    ) -> str:
        """Attach a new execution to the active batch, opening one when required."""

        async with self._batch_lock:
            # Legacy callers retain their previous shared batching behavior. New
            # discovery turns group only results from the same originating turn.
            if correlation_id is None and self._legacy_batch_id is None:
                self._legacy_batch_id = str(uuid.uuid4())
            batch_id = correlation_id or self._legacy_batch_id
            if batch_id not in self._batches:
                self._batches[batch_id] = _BatchState(
                    batch_id=batch_id, correlation_id=correlation_id,
                    discovery_mode=discovery_mode, catalog=catalog,
                    catalog_generation=catalog_generation,
                )
            self._batches[batch_id].pending += 1
            self._pending[request_id] = PendingExecution(
                request_id=request_id,
                agent_name=agent_name,
                instructions=instructions,
                batch_id=batch_id,
                correlation_id=correlation_id,
            )

            return batch_id

    # Store execution result and send combined batch to interaction agent when complete
    async def _complete_execution(
        self,
        batch_id: str,
        result: ExecutionResult,
        agent_name: str,
        request_id: Optional[str] = None,
    ) -> None:
        """Record the execution result and dispatch when the batch drains."""

        dispatch_payload: Optional[str] = None
        completed_state: Optional[_BatchState] = None

        async with self._batch_lock:
            state = self._batches.get(batch_id)
            if state is None:
                logger.warning(f"[{agent_name}] Dropping result for unknown batch")
                return

            state.results.append(result)
            if state.discovery_mode != "legacy":
                state.sources.append({
                    "result_id": request_id or str(uuid.uuid4()),
                    "agent_name": result.agent_name,
                    "correlation_id": state.correlation_id or batch_id,
                })
            state.pending -= 1

            if state.pending == 0:
                dispatch_payload = self._format_batch_payload(state.results, state.sources or None)
                agent_names = [entry.agent_name for entry in state.results]
                logger.info(f"Execution batch completed: {', '.join(agent_names)}")
                completed_state = self._batches.pop(batch_id)
                if batch_id == self._legacy_batch_id:
                    self._legacy_batch_id = None

        if dispatch_payload:
            if completed_state.discovery_mode == "legacy":
                await self._dispatch_to_interaction_agent(dispatch_payload)
            else:
                await self._dispatch_to_interaction_agent(
                    dispatch_payload, source_results=completed_state.sources,
                    correlation_id=completed_state.correlation_id,
                    discovery_mode=completed_state.discovery_mode,
                    catalog=completed_state.catalog,
                    catalog_generation=completed_state.catalog_generation,
                )

    # Return list of currently pending execution requests for monitoring purposes
    def get_pending_executions(self) -> List[Dict[str, str]]:
        """Expose pending executions for observability."""

        return [
            {
                "request_id": pending.request_id,
                "agent_name": pending.agent_name,
                "batch_id": pending.batch_id,
                "correlation_id": pending.correlation_id,
                "created_at": pending.created_at.isoformat(),
                "elapsed_seconds": (datetime.now() - pending.created_at).total_seconds(),
            }
            for pending in self._pending.values()
        ]

    # Clean up all pending executions and batch state on shutdown
    async def shutdown(self) -> None:
        """Clear pending bookkeeping (no background work remains)."""

        self._pending.clear()
        async with self._batch_lock:
            self._batches.clear()
            self._legacy_batch_id = None

    # Format multiple execution results into single message for interaction agent
    def _format_batch_payload(self, results: List[ExecutionResult], source_results=None) -> str:
        """Render execution results into the interaction-agent format."""

        entries: List[str] = []
        for index, result in enumerate(results):
            status = "SUCCESS" if result.success else "FAILED"
            response_text = (result.response or "(no response provided)").strip()
            if source_results:
                source = source_results[index]
                entries.append(
                    f'<execution_result source_result_id="{escape(source["result_id"], quote=True)}" '
                    f'agent_name="{escape(result.agent_name, quote=True)}" '
                    f'correlation_id="{escape(source["correlation_id"], quote=True)}" status="{status}">\n'
                    f'{escape(response_text)}\n</execution_result>'
                )
            else:
                entries.append(f"[{status}] {result.agent_name}: {response_text}")
        return "\n".join(entries)

    # Forward combined execution results to interaction agent for user response generation
    async def _dispatch_to_interaction_agent(
        self, payload: str, source_results=None, correlation_id=None,
        discovery_mode="legacy", catalog=None, catalog_generation=None,
    ) -> None:
        """Send the aggregated execution summary to the interaction agent."""

        from ..interaction_agent.runtime import InteractionAgentRuntime

        runtime = InteractionAgentRuntime(discovery_mode=discovery_mode, catalog=catalog)
        if discovery_mode == "legacy":
            callback = runtime.handle_agent_message(payload)
        else:
            callback = runtime.handle_agent_message(
                payload, source_results=source_results, correlation_id=correlation_id,
                catalog_generation=catalog_generation,
            )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(callback)
            return

        loop.create_task(callback)
