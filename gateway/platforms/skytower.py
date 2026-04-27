"""Skytower Relay platform adapter.

Connects Hermes to the Skytower Relay Server via Socket.IO.

Authentication: agentId:rawToken (issued by Relay Server /api/agents/register)
Protocol: Socket.IO (WebSocket with polling fallback)

Per-user Home Channel
---------------------
각 유저가 /sethome 명령으로 자신의 홈 채널을 지정합니다.
설정은 ~/.hermes/skytower_home_channels.json 에 유저별로 저장됩니다.
정적 환경변수 없음 — 모두 런타임에 동적으로 설정.

Process Mode (SKYTOWER_PROCESS_MODE=true)
-----------------------------------------
conversation_id별로 독립된 Hermes 프로세스를 띄웁니다.
각 프로세스는 자체 HERMES_HOME을 가지며 Docker 없이 동작합니다.

홈 채널에서 사용하는 명령어:
  /sethome                     — 현재 대화를 내 홈 채널로 설정
  /process start <conv_id>     — 프로세스 생성 및 conversation 연결
  /process stop  <conv_id>     — 프로세스 종료
  /process list                — 실행 중인 프로세스 목록
  /process info  <conv_id>     — 프로세스 상세 정보
  /process restart <conv_id>   — 프로세스 재시작

Auto-spawn (SKYTOWER_PROCESS_AUTO_SPAWN=true)
---------------------------------------------
첫 메시지가 도착한 conversation에 자동으로 프로세스를 생성합니다.
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

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

        # Process mode
        self._process_mode: bool = os.getenv(
            "SKYTOWER_PROCESS_MODE", ""
        ).lower() in ("1", "true", "yes")
        self._process_auto_spawn: bool = os.getenv(
            "SKYTOWER_PROCESS_AUTO_SPAWN", ""
        ).lower() in ("1", "true", "yes")
        self._proc_mgr: Optional[Any] = None  # ProcessManager

        # Per-user home channel: {user_id → conv_id}
        self._home_channels: Dict[str, str] = _load_home_channels()

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

        if self._process_mode:
            await self._init_process_manager()

        import socketio

        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_delay=2,
            reconnection_delay_max=30,
        )

        @self._sio.event
        async def connect():
            self._mark_connected()
            logger.info(
                "SkyTower connected: %s (agentId=%s, process_mode=%s)",
                self._relay_url, self._agent_id, self._process_mode,
            )
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
        if self._proc_mgr:
            await self._proc_mgr.stop()
        if self._sio:
            await self._sio.disconnect()
        self._mark_disconnected()

    # ── Process manager init ──────────────────────────────────────────────────

    async def _init_process_manager(self) -> None:
        from gateway.platforms.skytower_processes import ProcessManager
        from hermes_constants import get_hermes_home

        self._proc_mgr = ProcessManager(
            hermes_home=get_hermes_home(),
            idle_ttl=int(os.getenv("SKYTOWER_PROCESS_IDLE_TTL", "3600")),
            port_base=int(os.getenv("SKYTOWER_PROCESS_PORT_BASE", "19000")),
            startup_timeout=int(os.getenv("SKYTOWER_PROCESS_STARTUP_TIMEOUT", "30")),
        )
        await self._proc_mgr.start()
        logger.info(
            "Process mode active — idle_ttl=%ss auto_spawn=%s",
            os.getenv("SKYTOWER_PROCESS_IDLE_TTL", "3600"),
            self._process_auto_spawn,
        )

    # ── Inbound routing ───────────────────────────────────────────────────────

    async def _handle_relay_message(self, data: dict) -> None:
        if data.get("direction") != "outbound":
            return
        if data.get("type") != "text":
            return

        user_id = data.get("user_id")
        if not user_id:
            return

        user_str  = str(user_id)
        conv_str  = str(data["conversation_id"]) if data.get("conversation_id") else None
        user_name = data.get("user_name") or f"User {user_id}"
        content   = (data.get("content") or "").strip()

        # ── Skytower 전용 커맨드 ─────────────────────────────────────────────────
        # Hermes 게이트웨이의 슬래시 커맨드 인터셉터(run.py)에 도달하기 전에
        # 여기서 처리해야 "Unknown command" 오류가 발생하지 않습니다.

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

        if content.startswith("/process"):
            await self._dispatch_process_command(content, user_str, conv_str)
            return

        # 유저의 홈 채널 확인
        user_home_conv  = self._get_user_home_conv(user_str)
        is_home_channel = bool(
            conv_str and conv_str == user_home_conv and self._process_mode
        )

        # chat_id 생성
        chat_id = (
            f"skytower:{self._agent_id}:{user_str}:{conv_str}"
            if conv_str
            else f"skytower:{self._agent_id}:{user_str}"
        )

        # 프로세스 모드 라우팅 (홈 채널 제외)
        if self._process_mode and conv_str and not is_home_channel:
            if self._proc_mgr and self._proc_mgr.has_process(conv_str):
                await self._handle_via_process(conv_str, user_str, user_name, content)
                return
            elif self._process_auto_spawn:
                await self._handle_via_process(conv_str, user_str, user_name, content)
                if user_home_conv:
                    await self._reply_conv(
                        user_home_conv,
                        f"🆕 Conv #{conv_str} — 프로세스가 자동으로 생성됐습니다.",
                    )
                return

        # 표준 모드: 공유 Hermes 에이전트
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

    # ── /process 진입점 (항상 어댑터에서 처리) ───────────────────────────────

    async def _dispatch_process_command(
        self, content: str, user_id: str, conv_id: Optional[str]
    ) -> None:
        """SKYTOWER_PROCESS_MODE 및 홈채널 설정 여부를 확인 후 커맨드를 실행합니다."""
        if not self._process_mode:
            await self._reply_user(user_id, conv_id,
                "❌ 프로세스 모드가 비활성화 상태입니다.\n"
                "`.env`에 `SKYTOWER_PROCESS_MODE=true`를 추가하고 "
                "`hermes gateway`를 재시작하세요.")
            return

        user_home_conv = self._get_user_home_conv(user_id)
        if not user_home_conv:
            await self._reply_user(user_id, conv_id,
                "❌ 홈 채널이 설정되지 않았습니다.\n"
                "먼저 홈 채널로 사용할 대화방에서 `/sethome`을 입력하세요.")
            return

        if conv_id != user_home_conv:
            await self._reply_user(user_id, conv_id,
                f"❌ `/process` 커맨드는 홈 채널(Conv #{user_home_conv})에서만 사용할 수 있습니다.")
            return

        await self._handle_process_command(content, user_id, conv_id)

    # ── 메시지 전송 헬퍼 (conv_id 유무에 따라 라우팅) ─────────────────────────

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

    # ── /chatid ───────────────────────────────────────────────────────────────

    async def _handle_chatid(self, user_id: str, conv_id: Optional[str]) -> None:
        """현재 채팅방의 JID를 응답합니다."""
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
        if self._process_mode:
            msg += "\n\n`/process help` 로 프로세스 관리 명령어를 확인하세요."

        await self._reply_conv(conv_id, msg)

    # ── /process 커맨드 ───────────────────────────────────────────────────────

    async def _handle_process_command(
        self, text: str, user_id: str, home_conv_id: str
    ) -> None:
        parts = text.strip().split()
        sub   = parts[1] if len(parts) > 1 else ""
        arg   = parts[2] if len(parts) > 2 else ""

        if   sub == "list":               await self._cmd_list(home_conv_id)
        elif sub == "start"   and arg:    await self._cmd_start(arg, home_conv_id)
        elif sub == "stop"    and arg:    await self._cmd_stop(arg, home_conv_id)
        elif sub == "restart" and arg:    await self._cmd_restart(arg, home_conv_id)
        elif sub == "info"    and arg:    await self._cmd_info(arg, home_conv_id)
        else:                             await self._reply_conv(home_conv_id, _HELP_TEXT)

    async def _cmd_start(self, conv_id: str, home_conv_id: str) -> None:
        if not self._proc_mgr:
            await self._reply_conv(home_conv_id, "❌ 프로세스 모드가 비활성화 상태입니다.")
            return
        if self._proc_mgr.has_process(conv_id):
            await self._reply_conv(home_conv_id,
                f"ℹ️ Conv #{conv_id}는 이미 프로세스가 실행 중입니다.")
            return
        await self._reply_conv(home_conv_id, f"⏳ Conv #{conv_id} 프로세스 생성 중...")
        try:
            info = await self._proc_mgr.get_or_create(conv_id)
            from hermes_constants import get_hermes_home
            await self._reply_conv(home_conv_id,
                f"✅ Conv #{conv_id} 프로세스 연결 완료\n"
                f"• PID: {info.proc.pid}\n"
                f"• Port: {info.port}\n"
                f"• HERMES_HOME: `{get_hermes_home()}/convs/{conv_id}/`"
            )
        except Exception as e:
            logger.error("Process start failed for conv %s: %s", conv_id, e)
            await self._reply_conv(home_conv_id,
                f"❌ 프로세스 생성 실패 (conv #{conv_id}): {e}")

    async def _cmd_stop(self, conv_id: str, home_conv_id: str) -> None:
        if not self._proc_mgr:
            await self._reply_conv(home_conv_id, "❌ 프로세스 모드가 비활성화 상태입니다.")
            return
        stopped = await self._proc_mgr.shutdown_conversation(conv_id)
        if stopped:
            await self._reply_conv(home_conv_id, f"🛑 Conv #{conv_id} 프로세스 종료 완료.")
        else:
            await self._reply_conv(home_conv_id,
                f"ℹ️ Conv #{conv_id}에 실행 중인 프로세스가 없습니다.")

    async def _cmd_restart(self, conv_id: str, home_conv_id: str) -> None:
        if not self._proc_mgr:
            await self._reply_conv(home_conv_id, "❌ 프로세스 모드가 비활성화 상태입니다.")
            return
        await self._reply_conv(home_conv_id, f"🔄 Conv #{conv_id} 프로세스 재시작 중...")
        await self._proc_mgr.shutdown_conversation(conv_id)
        try:
            info = await self._proc_mgr.get_or_create(conv_id)
            await self._reply_conv(home_conv_id,
                f"✅ Conv #{conv_id} 재시작 완료 (pid: {info.proc.pid}, port: {info.port})")
        except Exception as e:
            await self._reply_conv(home_conv_id, f"❌ 재시작 실패: {e}")

    async def _cmd_list(self, home_conv_id: str) -> None:
        if not self._proc_mgr:
            await self._reply_conv(home_conv_id, "❌ 프로세스 모드가 비활성화 상태입니다.")
            return
        procs = await self._proc_mgr.list_processes()
        if not procs:
            await self._reply_conv(home_conv_id, "📭 실행 중인 프로세스가 없습니다.")
            return
        lines = ["**실행 중인 프로세스 목록**\n"]
        for p in procs:
            lines.append(
                f"• Conv #{p['conv_id']} — pid {p['pid']} "
                f"port {p['port']} (idle: {p['idle_seconds'] // 60}분)"
            )
        await self._reply_conv(home_conv_id, "\n".join(lines))

    async def _cmd_info(self, conv_id: str, home_conv_id: str) -> None:
        if not self._proc_mgr:
            await self._reply_conv(home_conv_id, "❌ 프로세스 모드가 비활성화 상태입니다.")
            return
        if not self._proc_mgr.has_process(conv_id):
            await self._reply_conv(home_conv_id,
                f"ℹ️ Conv #{conv_id}에 실행 중인 프로세스가 없습니다.")
            return
        procs = await self._proc_mgr.list_processes()
        info  = next((p for p in procs if p["conv_id"] == conv_id), None)
        if info:
            from hermes_constants import get_hermes_home
            conv_home = get_hermes_home() / "convs" / conv_id
            await self._reply_conv(home_conv_id,
                f"**Conv #{conv_id} 프로세스 정보**\n"
                f"• PID: {info['pid']}\n"
                f"• Port: {info['port']}\n"
                f"• Idle: {info['idle_seconds']}초\n"
                f"• HERMES_HOME: `{conv_home}/`\n"
                f"• Log: `{conv_home}/logs/api_server.log`"
            )

    # ── 프로세스 응답 스트리밍 ────────────────────────────────────────────────

    async def _handle_via_process(
        self, conv_id: str, user_id: str, user_name: str, content: str
    ) -> None:
        accumulated: List[str] = []
        try:
            async for chunk in self._proc_mgr.stream_response(
                conv_id=conv_id, text=content,
                user_id=user_id, user_name=user_name,
            ):
                accumulated.append(chunk)
                if self._sio and self._sio.connected:
                    await self._sio.emit("message_chunk", {"text": chunk})
        except Exception as e:
            logger.error("Process stream error (conv=%s): %s", conv_id, e)
            accumulated.append(f"\n\n[Error: {e}]")

        if self._sio and self._sio.connected:
            await self._sio.emit("message_done", {
                "content": "".join(accumulated),
                "type": "text",
                "target_conversation_id": int(conv_id),
            })

    # ── 메시지 전송 헬퍼 ──────────────────────────────────────────────────────

    async def _reply_conv(self, conv_id: str, text: str) -> None:
        if self._sio and self._sio.connected:
            await self._sio.emit("message_done", {
                "content": text,
                "type": "text",
                "target_conversation_id": int(conv_id),
            })

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

    async def send_thinking_chunk(self, text: str) -> None:
        if self._sio and self._sio.connected:
            try:
                await self._sio.emit("thinking_chunk", {"text": text})
            except Exception:
                pass

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


# ---------------------------------------------------------------------------
_HELP_TEXT = """**프로세스 관리 명령어** (홈 채널에서만 사용 가능)

`/process start <conv_id>`   — 프로세스 생성 및 conversation 연결
`/process stop  <conv_id>`   — 프로세스 종료
`/process restart <conv_id>` — 프로세스 재시작
`/process list`              — 실행 중인 프로세스 목록
`/process info  <conv_id>`   — 프로세스 상세 정보

홈 채널 설정: 아무 대화에서 `/sethome` 입력"""
