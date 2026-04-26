"""Skytower Relay platform adapter.

Connects Hermes to the Skytower Relay Server via Socket.IO.
The relay acts as a gateway between web browser users and Hermes AI.

Authentication: agentId:rawToken (issued by Relay Server /api/agents/register)
Protocol: Socket.IO (WebSocket with polling fallback)
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)


def check_skytower_requirements() -> bool:
    """Return True if python-socketio[asyncio_client] is installed."""
    try:
        import socketio  # noqa: F401
        return True
    except ImportError:
        return False


class SkyTowerAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Skytower Relay Server.

    Message flow:
      Web user  →  Relay (direction=outbound)  →  this adapter  →  Hermes
      Hermes    →  send() / send_chunk()        →  Relay          →  Web user
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SKYTOWER)
        extra = config.extra or {}

        raw_token = extra.get("token") or os.getenv("SKYTOWER_TOKEN", "")
        if not raw_token or ":" not in raw_token:
            raise ValueError("SKYTOWER_TOKEN must be in 'agentId:rawToken' format")

        self._agent_id, _ = raw_token.split(":", 1)
        self._token = raw_token
        self._relay_url = extra.get("url") or os.getenv("SKYTOWER_URL", "")
        self._sio: Optional[Any] = None  # socketio.AsyncClient
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ── Connection ────────────────────────────────────────────────────────────

    async def connect(self) -> bool:
        if not self._relay_url:
            logger.error("SKYTOWER_URL is not set")
            return False

        import socketio

        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_delay=2,
            reconnection_delay_max=30,
        )

        @self._sio.event
        async def connect():
            self._mark_connected()
            logger.info("SkyTower connected: %s (agentId=%s)", self._relay_url, self._agent_id)
            await self._sio.emit("heartbeat", self._collect_metrics())
            if os.getenv("SKYTOWER_PRINT_PAIR_CODE", "").lower() in ("1", "true", "yes"):
                await self._print_pairing_code()

        @self._sio.event
        async def disconnect():
            self._mark_disconnected()
            logger.warning("SkyTower disconnected")

        @self._sio.on("message")
        async def on_message(data: dict):
            await self._handle_relay_message(data)

        try:
            await self._sio.connect(
                self._relay_url,
                auth={"token": self._token},
                transports=["websocket", "polling"],
                wait_timeout=10,
            )
        except Exception as e:
            logger.error("SkyTower connect error: %s", e)
            return False

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._sio:
            await self._sio.disconnect()
        self._mark_disconnected()

    # ── Inbound ───────────────────────────────────────────────────────────────

    async def _handle_relay_message(self, data: dict) -> None:
        # Only handle outbound messages (web user → agent direction)
        if data.get("direction") != "outbound":
            return
        if data.get("type") != "text":
            return

        user_id = data.get("user_id")
        if not user_id:
            return

        conversation_id = data.get("conversation_id")
        user_name = data.get("user_name") or f"User {user_id}"
        content = data.get("content") or ""

        # JID: skytower:{agentId}:{userId}:{conversationId}  (or without conversationId)
        if conversation_id:
            chat_id = f"skytower:{self._agent_id}:{user_id}:{conversation_id}"
        else:
            chat_id = f"skytower:{self._agent_id}:{user_id}"

        source = self.build_source(
            chat_id=chat_id,
            chat_name=user_name,
            chat_type="dm",
            user_id=str(user_id),
            user_name=user_name,
        )
        event = MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(data.get("id", "")),
        )
        await self.handle_message(event)

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a completed response via message_done (triggers DB save on Relay)."""
        if not self._sio or not self._sio.connected:
            return SendResult(success=False, error="Not connected to Skytower Relay")

        parts = chat_id.split(":")
        # parts: ["skytower", agentId, userId, conversationId?]
        try:
            user_id = int(parts[2]) if len(parts) > 2 else None
            conversation_id = int(parts[3]) if len(parts) > 3 else None
        except (ValueError, IndexError):
            user_id = None
            conversation_id = None

        payload: Dict[str, Any] = {"content": content, "type": "text"}
        if conversation_id:
            payload["target_conversation_id"] = conversation_id
        elif user_id:
            payload["target_user_id"] = user_id

        try:
            await self._sio.emit("message_done", payload)
            return SendResult(success=True)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        # Skytower Relay does not currently support typing indicators.
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        parts = chat_id.split(":")
        user_id = parts[2] if len(parts) > 2 else "unknown"
        return {
            "name": f"SkyTower User {user_id}",
            "type": "dm",
            "platform": "skytower",
        }

    # ── Streaming extras (optional, called by gateway stream consumer) ────────

    async def send_chunk(self, text: str) -> None:
        """Stream a text chunk to the web client (not saved to DB)."""
        if self._sio and self._sio.connected:
            try:
                await self._sio.emit("message_chunk", {"text": text})
            except Exception:
                pass

    async def send_thinking_chunk(self, text: str) -> None:
        """Stream a reasoning chunk to the web client (not saved to DB)."""
        if self._sio and self._sio.connected:
            try:
                await self._sio.emit("thinking_chunk", {"text": text})
            except Exception:
                pass

    async def send_notification(
        self, title: str, body: str = "", level: str = "info"
    ) -> None:
        """Send a push notification via the Relay."""
        if self._sio and self._sio.connected:
            try:
                await self._sio.emit("notify", {"level": level, "title": title, "body": body})
            except Exception:
                pass

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        while self._running:
            await asyncio.sleep(30)
            if self._sio and self._sio.connected:
                try:
                    await self._sio.emit("heartbeat", self._collect_metrics())
                except Exception:
                    pass

    @staticmethod
    def _collect_metrics() -> dict:
        try:
            import psutil
            return {
                "cpu": psutil.cpu_percent(interval=None),
                "mem": psutil.virtual_memory().percent,
                "disk": psutil.disk_usage("/").percent,
            }
        except ImportError:
            return {"cpu": 0.0, "mem": 0.0, "disk": 0.0}

    # ── Onboarding helpers ────────────────────────────────────────────────────

    async def _print_pairing_code(self) -> None:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._relay_url}/api/agents/pairing-code",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json={"expiresMinutes": 10, "maxUses": 1},
                )
                resp.raise_for_status()
                data = resp.json()
            print("=" * 40)
            print(f"  친구 추가 코드: {data.get('code', 'N/A')}")
            print(f"  URL: {data.get('pairUrl', 'N/A')}")
            print("  유효시간: 10분")
            print("=" * 40)
        except Exception as e:
            logger.warning("Failed to fetch pairing code: %s", e)
