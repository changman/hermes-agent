"""Skytower Relay platform adapter.

Connects Hermes to the Skytower Relay Server via Socket.IO.

Authentication: agentId:rawToken (issued by Relay Server /api/agents/register)
Protocol: Socket.IO (WebSocket with polling fallback)

Per-user Home Channel
---------------------
각 유저가 /sethome 명령으로 자신의 홈 채널을 지정합니다.
설정은 ~/.hermes/skytower_home_channels.json 에 유저별로 저장됩니다.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.skytower_files import FileAccessHandler

logger = logging.getLogger(__name__)

_HOME_CHANNELS_FILE = "skytower_home_channels.json"


# ---------------------------------------------------------------------------
# Per-user home channel persistence
# ---------------------------------------------------------------------------

def _home_channels_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / _HOME_CHANNELS_FILE


def _load_home_channels() -> Dict[str, str]:
    try:
        return json.loads(_home_channels_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_home_channels(mapping: Dict[str, str]) -> None:
    try:
        _home_channels_path().write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2)
        )
    except OSError as e:
        logger.warning("Failed to save home channels: %s", e)


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

def check_skytower_requirements() -> bool:
    try:
        import socketio  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SkyTowerAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Skytower Relay Server."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SKYTOWER)
        extra = config.extra or {}

        raw_token = extra.get("token") or os.getenv("SKYTOWER_TOKEN", "")
        if not raw_token or ":" not in raw_token:
            raise ValueError("SKYTOWER_TOKEN must be in 'agentId:rawToken' format")

        self._agent_id, _ = raw_token.split(":", 1)
        self._token = raw_token
        self._relay_url = extra.get("url") or os.getenv("SKYTOWER_URL", "")
        self._sio: Optional[Any] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._intentional_disconnect: bool = False
        self._pending_thinking: Optional[str] = None  # 다음 send()에 포함할 reasoning

        # Per-user home channel: {user_id → conv_id}
        self._home_channels: Dict[str, str] = _load_home_channels()

        # File access handler — re-initialized in connect() with the live sio
        self._file_handler: Optional[FileAccessHandler] = None

    # ── Per-user home channel ─────────────────────────────────────────────────

    def _get_user_home_conv(self, user_id: str) -> Optional[str]:
        return self._home_channels.get(user_id)

    def _set_user_home_conv(self, user_id: str, conv_id: str) -> None:
        self._home_channels[user_id] = conv_id
        _save_home_channels(self._home_channels)
        logger.info("Home channel set: user=%s → conv=%s", user_id, conv_id)

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
            self._intentional_disconnect = False
            self._mark_connected()
            logger.info(
                "SkyTower connected: %s (agentId=%s)",
                self._relay_url, self._agent_id,
            )
            await self._sio.emit("heartbeat", self._collect_metrics())
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            if os.getenv("SKYTOWER_PRINT_PAIR_CODE", "").lower() in ("1", "true", "yes"):
                await self._print_pairing_code()

        @self._sio.event
        async def disconnect():
            self._mark_disconnected()
            logger.warning("SkyTower disconnected")

        @self._sio.on("__disconnect_final")
        async def on_disconnect_final():
            if self._intentional_disconnect:
                return
            logger.warning(
                "SkyTower: socket.io gave up reconnecting — triggering gateway-level reconnect"
            )
            self._set_fatal_error(
                "disconnected", "SkyTower server disconnected", retryable=True
            )
            await self._notify_fatal_error()

        @self._sio.on("message")
        async def on_message(data: dict):
            await self._handle_relay_message(data)

        # ── 파일시스템 접근 이벤트 ───────────────────────────────────────────

        self._file_handler = FileAccessHandler(self._sio)

        @self._sio.on("file:list")
        async def on_file_list(data: dict):
            try:
                await self._file_handler.handle_list(data)
            except Exception:
                logger.exception("file:list handler error — data=%s", data)

        @self._sio.on("file:read")
        async def on_file_read(data: dict):
            try:
                await self._file_handler.handle_read(data)
            except Exception:
                logger.exception("file:read handler error — data=%s", data)

        @self._sio.on("file:download")
        async def on_file_download(data: dict):
            asyncio.create_task(self._file_handler.handle_download(data))

        @self._sio.on("file:upload_start")
        async def on_file_upload_start(data: dict):
            try:
                await self._file_handler.handle_upload_start(data)
            except Exception:
                logger.exception("file:upload_start handler error — data=%s", data)

        @self._sio.on("file:upload_chunk")
        async def on_file_upload_chunk(data: dict):
            try:
                await self._file_handler.handle_upload_chunk(data)
            except Exception:
                logger.exception("file:upload_chunk handler error — data=%s", data)

        @self._sio.on("file:delete")
        async def on_file_delete(data: dict):
            try:
                await self._file_handler.handle_delete(data)
            except Exception:
                logger.exception("file:delete handler error — data=%s", data)

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
        self._intentional_disconnect = True
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._sio:
            await self._sio.disconnect()
        self._mark_disconnected()

    # ── Inbound routing ───────────────────────────────────────────────────────

    async def _handle_relay_message(self, data: dict) -> None:
        logger.debug("on_message received: direction=%r type=%r user_id=%r content=%r",
                     data.get("direction"), data.get("type"),
                     data.get("user_id"), str(data.get("content", ""))[:80])
        if data.get("direction") != "outbound":
            logger.warning("on_message dropped: direction=%r (expected 'outbound')", data.get("direction"))
            return
        if data.get("type") != "text":
            logger.warning("on_message dropped: type=%r (expected 'text')", data.get("type"))
            return

        user_id = data.get("user_id")
        if not user_id:
            logger.warning("on_message dropped: user_id missing")
            return

        user_str  = str(user_id)
        conv_str  = str(data["conversation_id"]) if data.get("conversation_id") else None
        user_name = data.get("user_name") or f"User {user_id}"
        content   = (data.get("content") or "").strip()

        if content == "/chatid":
            await self._handle_chatid(user_str, conv_str)
            return

        if content == "/sethome":
            if conv_str:
                await self._handle_sethome(user_str, conv_str)
            else:
                await self._reply_user(user_str, conv_str,
                    "❌ `/sethome`은 대화방 안에서만 사용할 수 있습니다.")
            return

        chat_id = (
            f"skytower:{self._agent_id}:{user_str}:{conv_str}"
            if conv_str
            else f"skytower:{self._agent_id}:{user_str}"
        )

        source = self.build_source(
            chat_id=chat_id,
            chat_name=user_name,
            chat_type="dm",
            user_id=user_str,
            user_name=user_name,
        )
        await self.handle_message(MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(data.get("id", "")),
        ))

    # ── 메시지 전송 헬퍼 ──────────────────────────────────────────────────────

    async def _reply_user(
        self, user_id: str, conv_id: Optional[str], text: str
    ) -> None:
        if not self._sio or not self._sio.connected:
            return
        payload: Dict[str, Any] = {"content": text, "type": "text"}
        if conv_id:
            payload["target_conversation_id"] = int(conv_id)
        else:
            payload["target_user_id"] = int(user_id)
        await self._sio.emit("message_done", payload)

    async def _reply_conv(self, conv_id: str, text: str) -> None:
        if self._sio and self._sio.connected:
            await self._sio.emit("message_done", {
                "content": text,
                "type": "text",
                "target_conversation_id": int(conv_id),
            })

    # ── /chatid ───────────────────────────────────────────────────────────────

    async def _handle_chatid(self, user_id: str, conv_id: Optional[str]) -> None:
        if conv_id:
            jid = f"skytower:{self._agent_id}:{user_id}:{conv_id}"
        else:
            jid = f"skytower:{self._agent_id}:{user_id}"

        text = (
            f"**현재 채팅방 JID**\n"
            f"`{jid}`\n\n"
            f"• Agent ID: `{self._agent_id}`\n"
            f"• User ID: `{user_id}`\n"
            f"• Conversation ID: `{conv_id or '없음'}`"
        )

        if self._sio and self._sio.connected:
            payload: Dict[str, Any] = {"content": text, "type": "text"}
            if conv_id:
                payload["target_conversation_id"] = int(conv_id)
            else:
                payload["target_user_id"] = int(user_id)
            await self._sio.emit("message_done", payload)

    # ── /sethome ──────────────────────────────────────────────────────────────

    async def _handle_sethome(self, user_id: str, conv_id: str) -> None:
        prev = self._get_user_home_conv(user_id)
        self._set_user_home_conv(user_id, conv_id)

        msg = "✅ 이 대화가 홈 채널로 설정됐습니다."
        if prev and prev != conv_id:
            msg += f"\n이전 홈 채널: Conv #{prev}"

        await self._reply_conv(conv_id, msg)

    # ── Outbound (표준) ───────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._sio or not self._sio.connected:
            return SendResult(success=False, error="Not connected to Skytower Relay")

        parts = chat_id.split(":")
        try:
            user_id         = int(parts[2]) if len(parts) > 2 else None
            conversation_id = int(parts[3]) if len(parts) > 3 else None
        except (ValueError, IndexError):
            user_id = conversation_id = None

        payload: Dict[str, Any] = {"content": content, "type": "text"}
        # _pending_thinking: gateway/run.py가 reasoning을 임시 저장, 여기서 소비
        if self._pending_thinking:
            payload["thinking"] = self._pending_thinking
            self._pending_thinking = None
        elif metadata and metadata.get("thinking"):
            payload["thinking"] = metadata["thinking"]
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
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        parts   = chat_id.split(":")
        user_id = parts[2] if len(parts) > 2 else "unknown"
        return {"name": f"SkyTower User {user_id}", "type": "dm", "platform": "skytower"}

    # ── 스트리밍 extras ───────────────────────────────────────────────────────

    async def send_chunk(self, text: str) -> None:
        if self._sio and self._sio.connected:
            try:
                await self._sio.emit("message_chunk", {"text": text})
            except Exception:
                pass

    async def send_thinking_chunk(self, text: str, chat_id: Optional[str] = None) -> None:
        if not self._sio or not self._sio.connected:
            logger.warning("[REASONING_CONTEXT] send_thinking_chunk 실패: sio 미연결")
            return
        try:
            payload: Dict[str, Any] = {"text": text}
            if chat_id:
                parts = chat_id.split(":")
                try:
                    user_id         = int(parts[2]) if len(parts) > 2 else None
                    conversation_id = int(parts[3]) if len(parts) > 3 else None
                except (ValueError, IndexError):
                    user_id = conversation_id = None
                if conversation_id:
                    payload["target_conversation_id"] = conversation_id
                elif user_id:
                    payload["target_user_id"] = user_id
            logger.warning(
                "[REASONING_CONTEXT] thinking_chunk emit: %d자, payload_keys=%s",
                len(text), list(payload.keys()),
            )
            await self._sio.emit("thinking_chunk", payload)
        except Exception as e:
            logger.warning("[REASONING_CONTEXT] thinking_chunk emit 실패: %s", e)

    async def send_notification(
        self, title: str, body: str = "", level: str = "info"
    ) -> None:
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

    # ── 온보딩 ────────────────────────────────────────────────────────────────

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
