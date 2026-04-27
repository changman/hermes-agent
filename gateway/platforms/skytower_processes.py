"""Per-conversation subprocess manager for Skytower (Level 2 isolation).

Architecture
------------
Each Skytower conversation_id gets its own Python subprocess running a
Hermes API Server with an isolated HERMES_HOME — no Docker required.

    SkyTowerAdapter (main process)
        └─ ProcessManager
               ├─ conv 3  →  hermes gateway (pid 1234, port 19003)
               │              HERMES_HOME=~/.hermes/convs/3/
               ├─ conv 7  →  hermes gateway (pid 1235, port 19007)
               │              HERMES_HOME=~/.hermes/convs/7/
               └─ ...

The main adapter is the only Socket.IO connection to Skytower Relay.
Each subprocess exposes only the Hermes API Server (no Relay connection).

Message flow
------------
  Relay → SkyTowerAdapter.on_message()
        → ProcessManager.stream_response(conv_id, text, user_id)
        → POST /v1/chat/completions (SSE) → subprocess API Server
        ← content delta chunks
        → SkyTowerAdapter emits message_chunk / message_done to Relay

Isolation per conversation
--------------------------
  ~/.hermes/convs/{conv_id}/
      CLAUDE.md     ← copied from parent on first boot, editable per-conv
      config.yaml   ← API Server only config
      sessions/     ← per-conversation session history
      memories/     ← per-conversation long-term memory
      skills/       ← per-conversation skills

Delegation principle (from Hermes docs)
----------------------------------------
Each subprocess starts with a "completely blank slate" — only what exists
in its HERMES_HOME directory. The parent process passes messages via HTTP;
no parent context leaks into the child.
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import socket as _socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PORT_BASE        = 19000
DEFAULT_IDLE_TTL         = 3600   # 1시간 미사용 시 종료
DEFAULT_STARTUP_TIMEOUT  = 30     # 프로세스 기동 대기 시간(초)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ProcessInfo:
    conv_id:     str
    proc:        asyncio.subprocess.Process
    port:        int
    api_key:     str
    log_file:    Optional[object] = None
    last_active: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# ProcessManager
# ---------------------------------------------------------------------------

class ProcessManager:
    """Manages one Hermes subprocess per Skytower conversation_id."""

    def __init__(
        self,
        hermes_home: Path,
        idle_ttl:         int = DEFAULT_IDLE_TTL,
        port_base:        int = DEFAULT_PORT_BASE,
        startup_timeout:  int = DEFAULT_STARTUP_TIMEOUT,
        env_passthrough: Optional[List[str]] = None,
    ):
        self._hermes_home    = hermes_home
        self._idle_ttl       = idle_ttl
        self._port_base      = port_base
        self._startup_timeout = startup_timeout
        self._env_passthrough = env_passthrough or [
            # LLM API 키 — 메인 프로세스에서 사용하는 모든 키를 전달
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",        # Gemini
            "GEMINI_API_KEY",        # Gemini 대체 키명
            "OPENROUTER_API_KEY",
            "NOUS_API_KEY",
            "GROQ_API_KEY",
            "MISTRAL_API_KEY",
            "DEEPSEEK_API_KEY",
            "XAI_API_KEY",
            # 모델 설정
            "MODEL", "HERMES_MODEL", "OPENAI_BASE_URL",
            # 환경
            "TERMINAL_ENV", "PATH", "HOME", "LANG", "TZ",
            # Hermes 설정
            "HERMES_MAX_ITERATIONS",
        ]
        self._processes: Dict[str, ProcessInfo] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._cleanup_task = asyncio.create_task(self._idle_cleanup_loop())
        logger.info(
            "ProcessManager started (idle_ttl=%ds, port_base=%d)",
            self._idle_ttl, self._port_base,
        )

    async def stop(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
        await self.shutdown_all()

    # ── Public API ────────────────────────────────────────────────────────────

    def has_process(self, conv_id: str) -> bool:
        info = self._processes.get(conv_id)
        if info is None:
            return False
        # 프로세스가 살아있는지 확인
        if info.proc.returncode is not None:
            logger.warning("Process for conv %s exited (rc=%d)", conv_id, info.proc.returncode)
            del self._processes[conv_id]
            return False
        return True

    async def stream_response(
        self,
        conv_id:   str,
        text:      str,
        user_id:   str,
        user_name: str,
    ) -> AsyncIterator[str]:
        """Forward a user message to the conversation process, yield content chunks."""
        info = await self.get_or_create(conv_id)
        info.last_active = time.monotonic()

        headers = {
            "Authorization": f"Bearer {info.api_key}",
            "X-Hermes-Session-Id": f"skytower-conv{conv_id}-user{user_id}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": text}],
            "stream": True,
        }
        url = f"http://127.0.0.1:{info.port}/v1/chat/completions"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5, read=120, write=10, pool=5)
            ) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        chunk_data = line[6:]
                        if chunk_data.strip() == "[DONE]":
                            break
                        try:
                            obj   = json.loads(chunk_data)
                            delta = obj["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield delta
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
        except httpx.HTTPStatusError as e:
            logger.error("Process conv %s HTTP %s", conv_id, e.response.status_code)
            yield f"[Error: {e.response.status_code}]"
        except Exception as e:
            logger.error("Process conv %s stream error: %s", conv_id, e)
            yield f"[Error: {e}]"

    async def get_or_create(self, conv_id: str) -> ProcessInfo:
        async with self._lock:
            if not self.has_process(conv_id):
                logger.info("Spawning process for conversation %s", conv_id)
                info = await self._spawn(conv_id)
                self._processes[conv_id] = info
            return self._processes[conv_id]

    async def list_processes(self) -> List[Dict]:
        now = time.monotonic()
        return [
            {
                "conv_id":      info.conv_id,
                "pid":          info.proc.pid,
                "port":         info.port,
                "idle_seconds": int(now - info.last_active),
            }
            for info in self._processes.values()
            if info.proc.returncode is None
        ]

    # ── Config seeding ────────────────────────────────────────────────────────

    def _seed_conv_config(self, config_yaml: Path) -> None:
        """메인 HERMES_HOME의 모델 설정을 서브프로세스 config.yaml에 복사합니다."""
        try:
            import yaml as _yaml
            parent = self._hermes_home / "config.yaml"
            safe_cfg: dict = {"session_reset": {"mode": "none"}}

            if parent.exists():
                with open(parent, encoding="utf-8") as f:
                    cfg = _yaml.safe_load(f) or {}

                model_cfg = cfg.get("model")
                # 모델이 빈 문자열이면 세션 히스토리에서 실제 사용 모델 탐색
                if not model_cfg or (isinstance(model_cfg, str) and not model_cfg.strip()):
                    model_name = self._infer_model_from_sessions()
                    if model_name:
                        safe_cfg["model"] = {"default": model_name}
                else:
                    safe_cfg["model"] = model_cfg

            with open(config_yaml, "w", encoding="utf-8") as f:
                _yaml.dump(safe_cfg, f, allow_unicode=True, default_flow_style=False)
            logger.debug("Seeded config.yaml: %s", safe_cfg.get("model"))
        except Exception as e:
            logger.warning("Failed to seed conv config: %s", e)
            config_yaml.write_text(
                "# Auto-generated for conversation subprocess\n"
                "session_reset:\n"
                "  mode: none\n"
            )

    def _infer_model_from_sessions(self) -> Optional[str]:
        """세션 파일에서 가장 최근에 사용된 모델명을 추출합니다."""
        import glob as _glob
        sessions_dir = self._hermes_home / "sessions"
        pattern = str(sessions_dir / "session_*.json")
        latest_model = ""
        latest_ts = ""
        for path in _glob.glob(pattern):
            try:
                import json as _json
                data = _json.loads(Path(path).read_text())
                model = data.get("model", "")
                ts = data.get("last_updated", "")
                if model and ts > latest_ts:
                    latest_model = model
                    latest_ts = ts
            except Exception:
                continue
        return latest_model or None

    # ── Process spawn ─────────────────────────────────────────────────────────

    async def _spawn(self, conv_id: str) -> ProcessInfo:
        conv_home = self._ensure_conv_home(conv_id)
        port      = self._find_free_port()
        api_key   = secrets.token_urlsafe(32)

        env = self._build_env(conv_home, port, api_key)

        # 로그 파일 (conv_home/logs/api_server.log)
        log_dir  = conv_home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "api_server.log", "a", buffering=1)

        # hermes 실행파일 경로 (현재 프로세스와 동일한 venv 사용)
        hermes_bin = Path(sys.executable).parent / "hermes"
        if not hermes_bin.exists():
            hermes_bin = "hermes"  # PATH에서 찾기

        proc = await asyncio.create_subprocess_exec(
            str(hermes_bin), "gateway",
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
        logger.info(
            "Spawned hermes process for conv %s: pid=%d port=%d home=%s",
            conv_id, proc.pid, port, conv_home,
        )

        await self._wait_ready(port, conv_id)

        return ProcessInfo(
            conv_id=conv_id,
            proc=proc,
            port=port,
            api_key=api_key,
            log_file=log_file,
        )

    def _ensure_conv_home(self, conv_id: str) -> Path:
        """Create and seed the conversation HERMES_HOME directory."""
        conv_home = self._hermes_home / "convs" / str(conv_id)
        for sub in ("sessions", "memories", "skills", "cron", "logs"):
            (conv_home / sub).mkdir(parents=True, exist_ok=True)

        # CLAUDE.md: 부모 CLAUDE.md 복사, 없으면 기본값
        claude_md = conv_home / "CLAUDE.md"
        if not claude_md.exists():
            parent_md = self._hermes_home / "CLAUDE.md"
            if parent_md.exists():
                shutil.copy2(parent_md, claude_md)
            else:
                claude_md.write_text(
                    f"# Hermes — Conversation {conv_id}\n\n"
                    "You are Hermes, an AI assistant.\n"
                )

        # config.yaml: 메인 프로세스의 모델 설정을 복사 (플랫폼 토큰 제외)
        config_yaml = conv_home / "config.yaml"
        if not config_yaml.exists():
            self._seed_conv_config(config_yaml)

        return conv_home

    def _build_env(self, conv_home: Path, port: int, api_key: str) -> Dict[str, str]:
        env: Dict[str, str] = {}
        # 필요한 환경변수만 전달
        for key in self._env_passthrough:
            val = os.environ.get(key)
            if val:
                env[key] = val

        env.update({
            "HERMES_HOME":         str(conv_home),
            "API_SERVER_ENABLED":  "true",
            "API_SERVER_HOST":     "127.0.0.1",
            "API_SERVER_PORT":     str(port),
            "API_SERVER_KEY":      api_key,
            "GATEWAY_ALLOW_ALL_USERS": "true",
            # 하위 프로세스는 Relay에 연결하지 않음
            "SKYTOWER_TOKEN":      "",
            "SKYTOWER_URL":        "",
            "TELEGRAM_BOT_TOKEN":  "",
            "DISCORD_BOT_TOKEN":   "",
            "SLACK_BOT_TOKEN":     "",
        })
        return env

    # ── Health check ──────────────────────────────────────────────────────────

    async def _wait_ready(self, port: int, conv_id: str) -> None:
        url      = f"http://127.0.0.1:{port}/health"
        deadline = time.monotonic() + self._startup_timeout
        last_err: Optional[str] = None

        while time.monotonic() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    r = await client.get(url)
                    if r.status_code == 200:
                        logger.info("Process for conv %s ready on port %d", conv_id, port)
                        return
            except Exception as e:
                last_err = str(e)
            await asyncio.sleep(1)

        raise TimeoutError(
            f"Process for conv {conv_id} (port {port}) did not become ready "
            f"in {self._startup_timeout}s. Last error: {last_err}"
        )

    # ── Shutdown ──────────────────────────────────────────────────────────────

    async def shutdown_conversation(self, conv_id: str) -> bool:
        async with self._lock:
            info = self._processes.pop(conv_id, None)
        if not info:
            return False
        await self._terminate(info)
        return True

    async def shutdown_all(self) -> None:
        async with self._lock:
            infos = list(self._processes.values())
            self._processes.clear()
        for info in infos:
            await self._terminate(info)

    async def _terminate(self, info: ProcessInfo) -> None:
        if info.log_file:
            try:
                info.log_file.close()
            except Exception:
                pass
        if info.proc.returncode is None:
            try:
                info.proc.terminate()
                await asyncio.wait_for(info.proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    info.proc.kill()
                except ProcessLookupError:
                    pass
        logger.info("Process for conv %s (pid %d) terminated", info.conv_id, info.proc.pid)

    # ── Idle cleanup ──────────────────────────────────────────────────────────

    async def _idle_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            idle = [
                conv_id
                for conv_id, info in list(self._processes.items())
                if now - info.last_active > self._idle_ttl
                and info.proc.returncode is None
            ]
            for conv_id in idle:
                logger.info(
                    "Shutting down idle process for conv %s (idle > %ds)",
                    conv_id, self._idle_ttl,
                )
                await self.shutdown_conversation(conv_id)

    # ── Port allocation ───────────────────────────────────────────────────────

    def _find_free_port(self) -> int:
        used = {info.port for info in self._processes.values()}
        for port in range(self._port_base, self._port_base + 1000):
            if port in used:
                continue
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
        raise RuntimeError(
            f"No free port found in range {self._port_base}–{self._port_base + 999}"
        )
