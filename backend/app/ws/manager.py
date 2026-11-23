"""WebSocket connection manager for real-time updates."""

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections for real-time updates.

    Supports multiple channels for different update types.
    Handles graceful disconnect and broadcast failures.

    Attributes:
        active_connections: Dictionary mapping channel names to lists of WebSocket connections.
    """

    def __init__(self):
        """Initialize the connection manager."""
        self.active_connections: dict[str, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, channel: str = "default") -> None:
        """
        Accept and register a WebSocket connection.

        Args:
            websocket: The WebSocket connection to register.
            channel: Channel name for grouping connections.
        """
        await websocket.accept()

        async with self._lock:
            if channel not in self.active_connections:
                self.active_connections[channel] = []
            self.active_connections[channel].append(websocket)

        logger.info(f"WebSocket connected to channel: {channel}")

    async def disconnect(self, websocket: WebSocket, channel: str = "default") -> None:
        """
        Remove a WebSocket connection.

        Args:
            websocket: The WebSocket connection to remove.
            channel: Channel the connection was on.
        """
        async with self._lock:
            if channel in self.active_connections:
                try:
                    self.active_connections[channel].remove(websocket)
                    if not self.active_connections[channel]:
                        del self.active_connections[channel]
                except ValueError:
                    pass  # Connection already removed

        logger.info(f"WebSocket disconnected from channel: {channel}")

    async def broadcast(
        self,
        message: dict[str, Any],
        channel: str = "default",
    ) -> None:
        """
        Broadcast a message to all connections on a channel.

        Handles disconnected clients gracefully by removing them.

        Args:
            message: JSON-serializable message to broadcast.
            channel: Channel to broadcast on.
        """
        if channel not in self.active_connections:
            return

        # Copy list to avoid modification during iteration
        connections = list(self.active_connections.get(channel, []))
        disconnected = []

        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.warning(f"Failed to send to WebSocket: {e}")
                disconnected.append(websocket)

        # Clean up disconnected clients
        for websocket in disconnected:
            await self.disconnect(websocket, channel)

    async def send_personal(
        self,
        websocket: WebSocket,
        message: dict[str, Any],
    ) -> bool:
        """
        Send a message to a specific connection.

        Args:
            websocket: Target WebSocket connection.
            message: JSON-serializable message to send.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            await websocket.send_json(message)
            return True
        except Exception as e:
            logger.warning(f"Failed to send personal message: {e}")
            return False

    def get_connection_count(self, channel: str | None = None) -> int:
        """
        Get the number of active connections.

        Args:
            channel: Optional channel to count. If None, counts all.

        Returns:
            Number of active connections.
        """
        if channel is not None:
            return len(self.active_connections.get(channel, []))
        return sum(len(conns) for conns in self.active_connections.values())


# Module-level singleton
ws_manager = ConnectionManager()
