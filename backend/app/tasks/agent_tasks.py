"""Background tasks for agent processing."""

import asyncio
import logging
from typing import Any

from celery import Task
from sqlalchemy import select

from app.models.proposal import Proposal
from app.tasks.celery_app import celery_app
from app.tasks.db import create_task_session as _create_async_session

logger = logging.getLogger(__name__)


async def _process_agent_request(
    proposal_id: str,
    request_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Async helper for processing agent requests using Deep Agents.

    This is the actual async implementation that runs inside the sync Celery task.

    Args:
        proposal_id: UUID of the proposal record.
        request_type: Type of agent request (e.g., "echo", "sprint_planning", "standup").
        payload: Request payload data.

    Returns:
        Processing result dictionary.
    """
    from app.agents.deep_agent import invoke_delivery_agent

    async with _create_async_session() as session:
        try:
            # Load proposal from database
            result = await session.execute(
                select(Proposal).where(Proposal.id == proposal_id)
            )
            proposal = result.scalar_one_or_none()

            if proposal is None:
                raise ValueError(f"Proposal not found: {proposal_id}")

            logger.info(f"Processing agent request: {proposal_id}, type={request_type}")

            # Extract message from payload
            message = payload.get("message", "")
            if not message:
                # Build default message based on request type
                if request_type == "sprint_planning":
                    message = "Analyze the backlog and team capacity, then propose a sprint composition."
                elif request_type == "standup":
                    message = "Collect status updates from the team and summarize any blockers."
                elif request_type == "echo":
                    message = f"Echo back this test request: {payload}"
                else:
                    message = f"Process this {request_type} request with the provided context."

            # Invoke Deep Agent
            agent_result = await invoke_delivery_agent(
                agent_type=request_type,
                message=message,
                context=payload.get("context"),
            )

            # Update proposal with result
            proposal.state = "pending_review"
            proposal.content = agent_result
            proposal.reasoning = agent_result.get("reasoning", "Processed by Deep Agent")
            proposal.confidence = agent_result.get("confidence", "medium")
            proposal.created_by_agent = agent_result.get("agent", "DeepAgent")

            await session.commit()

            logger.info(f"Agent request completed: {proposal_id}")

            # Broadcast update via WebSocket
            # Note: Redis pub/sub integration will be added in Phase 2
            # For now, clients poll the proposal endpoint

            return {
                "proposal_id": proposal_id,
                "status": "completed",
                "state": proposal.state,
            }

        except Exception as e:
            logger.error(f"Agent request failed: {proposal_id}, error={e}")

            # Update proposal to failed state
            try:
                result = await session.execute(
                    select(Proposal).where(Proposal.id == proposal_id)
                )
                proposal = result.scalar_one_or_none()
                if proposal:
                    proposal.state = "failed"
                    proposal.execution_result = {"error": str(e)}
                    await session.commit()
            except Exception as update_error:
                logger.error(f"Failed to update proposal state: {update_error}")
                await session.rollback()

            raise


@celery_app.task(
    bind=True,
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def process_agent_request(
    self: Task,
    proposal_id: str,
    request_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    Celery task for processing agent requests.

    This is the sync entry point that creates an event loop to run async code.
    Celery workers are sync by default, so we bridge to async here.

    Args:
        proposal_id: UUID of the proposal record.
        request_type: Type of agent request.
        payload: Request payload data.

    Returns:
        Processing result dictionary.
    """
    logger.info(f"Starting agent task: {proposal_id}, type={request_type}")

    try:
        # Create new event loop for async execution
        # Each task runs in its own loop to avoid conflicts
        result = asyncio.run(
            _process_agent_request(proposal_id, request_type, payload)
        )
        return result

    except Exception as e:
        logger.error(f"Agent task failed: {proposal_id}, error={e}, retrying...")
        # Celery's autoretry will handle retries with exponential backoff
        raise
