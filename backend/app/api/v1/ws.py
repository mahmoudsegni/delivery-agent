"""WebSocket endpoints for real-time updates."""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.dependencies import (
    PASSCODE_COOKIE,
    passcode_locked,
    passcode_ok,
    register_passcode_failure,
)
from app.ws.manager import ws_manager

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket, channel: str = "default") -> None:
    """
    WebSocket endpoint for real-time updates.

    Clients connect to receive broadcast updates about proposal state changes,
    agent progress, and other real-time events.

    Query Parameters:
        channel: Channel to subscribe to (default: "default")

    Protocol:
        - Server sends JSON messages with update data
        - Server sends periodic ping to keep connection alive
        - Client can send pong or any message to confirm alive
    """
    # Passcode gate (issue #8): middleware can't cover websockets, and
    # browsers can't send headers on a WS handshake — cookie only.
    # Same per-IP rate limiter as the HTTP gate (issue #25).
    if settings.passcode:
        ip = websocket.client.host if websocket.client else "unknown"
        locked = passcode_locked(ip)
        if locked or not passcode_ok(websocket.cookies.get(PASSCODE_COOKIE)):
            if not locked:
                register_passcode_failure(ip)
            await websocket.close(code=4401)
            return
    await ws_manager.connect(websocket, channel)

    try:
        # Send connection confirmation
        await websocket.send_json({
            "type": "connected",
            "channel": channel,
            "message": "Connected to updates channel",
        })

        # Keep-alive loop with ping/pong
        while True:
            try:
                # Wait for client messages with timeout for ping
                message = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=30.0,  # 30 second timeout for ping
                )

                # Handle client messages (pong, etc.)
                if message.get("type") == "pong":
                    logger.debug(f"Received pong from client on channel {channel}")
                elif message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

            except asyncio.TimeoutError:
                # Send ping to check if client is still alive
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    logger.info(f"Client disconnected on ping (channel: {channel})")
                    break

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected from channel: {channel}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await ws_manager.disconnect(websocket, channel)
