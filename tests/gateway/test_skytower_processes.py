"""Tests for Skytower Level 2 — per-conversation subprocess isolation."""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.platforms.skytower_processes import ProcessManager, ProcessInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(tmp_path: Path, **kwargs) -> ProcessManager:
    defaults = dict(
        hermes_home=tmp_path,
        idle_ttl=3600,
        port_base=19000,
        startup_timeout=1,
    )
    defaults.update(kwargs)
    return ProcessManager(**defaults)


def _fake_proc(pid=1234, returncode=None):
    p = MagicMock(spec=asyncio.subprocess.Process)
    p.pid        = pid
    p.returncode = returncode
    p.terminate  = MagicMock()
    p.kill       = MagicMock()
    p.wait       = AsyncMock(return_value=0)
    return p


# ---------------------------------------------------------------------------
# conv_home setup
# ---------------------------------------------------------------------------

class TestConvHomeSetup:
    def test_creates_subdirectories(self, tmp_path):
        mgr       = _make_manager(tmp_path)
        conv_home = mgr._ensure_conv_home("42")
        for sub in ("sessions", "memories", "skills", "cron", "logs"):
            assert (conv_home / sub).is_dir()

    def test_seeds_claude_md_from_parent(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# Parent")
        mgr       = _make_manager(tmp_path)
        conv_home = mgr._ensure_conv_home("5")
        assert (conv_home / "CLAUDE.md").read_text() == "# Parent"

    def test_creates_default_claude_md_when_no_parent(self, tmp_path):
        mgr       = _make_manager(tmp_path)
        conv_home = mgr._ensure_conv_home("99")
        assert "99" in (conv_home / "CLAUDE.md").read_text()

    def test_does_not_overwrite_existing_claude_md(self, tmp_path):
        conv_home = tmp_path / "convs" / "7"
        conv_home.mkdir(parents=True)
        custom = conv_home / "CLAUDE.md"
        custom.write_text("# Custom")
        _make_manager(tmp_path)._ensure_conv_home("7")
        assert custom.read_text() == "# Custom"

    def test_creates_config_yaml(self, tmp_path):
        mgr       = _make_manager(tmp_path)
        conv_home = mgr._ensure_conv_home("3")
        assert (conv_home / "config.yaml").exists()


# ---------------------------------------------------------------------------
# _build_env
# ---------------------------------------------------------------------------

class TestBuildEnv:
    def test_api_server_vars_set(self, tmp_path):
        mgr = _make_manager(tmp_path)
        env = mgr._build_env(tmp_path / "convs" / "3", 19003, "secret")
        assert env["API_SERVER_ENABLED"] == "true"
        assert env["API_SERVER_PORT"]    == "19003"
        assert env["API_SERVER_KEY"]     == "secret"
        assert env["HERMES_HOME"]        == str(tmp_path / "convs" / "3")

    def test_skytower_tokens_cleared(self, tmp_path):
        mgr = _make_manager(tmp_path)
        env = mgr._build_env(tmp_path / "convs" / "3", 19003, "key")
        assert env["SKYTOWER_TOKEN"]     == ""
        assert env["SKYTOWER_URL"]       == ""
        assert env["TELEGRAM_BOT_TOKEN"] == ""

    def test_api_keys_forwarded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mgr = _make_manager(tmp_path)
        env = mgr._build_env(tmp_path / "convs" / "3", 19003, "key")
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"

    def test_host_is_loopback(self, tmp_path):
        mgr = _make_manager(tmp_path)
        env = mgr._build_env(tmp_path / "convs" / "3", 19003, "key")
        assert env["API_SERVER_HOST"] == "127.0.0.1"


# ---------------------------------------------------------------------------
# has_process
# ---------------------------------------------------------------------------

class TestHasProcess:
    def test_false_when_no_process(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.has_process("3") is False

    def test_true_when_process_running(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._processes["3"] = ProcessInfo(
            conv_id="3", proc=_fake_proc(), port=19003, api_key="k"
        )
        assert mgr.has_process("3") is True

    def test_false_and_removes_when_process_exited(self, tmp_path):
        mgr  = _make_manager(tmp_path)
        dead = _fake_proc(returncode=1)
        mgr._processes["3"] = ProcessInfo(
            conv_id="3", proc=dead, port=19003, api_key="k"
        )
        assert mgr.has_process("3") is False
        assert "3" not in mgr._processes


# ---------------------------------------------------------------------------
# get_or_create
# ---------------------------------------------------------------------------

class TestGetOrCreate:
    @pytest.mark.asyncio
    async def test_spawns_process_on_first_call(self, tmp_path):
        mgr       = _make_manager(tmp_path)
        fake_proc = _fake_proc(pid=9999)

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=fake_proc), \
             patch.object(mgr, "_wait_ready", new_callable=AsyncMock):
            info = await mgr.get_or_create("3")

        assert info.conv_id       == "3"
        assert info.proc.pid      == 9999
        assert "3" in mgr._processes

    @pytest.mark.asyncio
    async def test_reuses_existing_process(self, tmp_path):
        mgr      = _make_manager(tmp_path)
        existing = ProcessInfo(conv_id="3", proc=_fake_proc(), port=19003, api_key="k")
        mgr._processes["3"] = existing

        with patch("asyncio.create_subprocess_exec") as mock_spawn:
            info = await mgr.get_or_create("3")
            mock_spawn.assert_not_called()
        assert info is existing


# ---------------------------------------------------------------------------
# stream_response
# ---------------------------------------------------------------------------

class TestStreamResponse:
    @pytest.mark.asyncio
    async def test_yields_content_chunks(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._processes["3"] = ProcessInfo(
            conv_id="3", proc=_fake_proc(), port=19003, api_key="testkey"
        )
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"안녕"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"하세요"},"finish_reason":null}]}',
            "data: [DONE]",
        ]

        async def fake_aiter_lines():
            for line in sse_lines:
                yield line

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.aiter_lines      = fake_aiter_lines
        mock_resp.__aenter__       = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__        = AsyncMock(return_value=False)

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_stream.__aexit__  = AsyncMock(return_value=False)

        mock_client = MagicMock()
        mock_client.stream         = MagicMock(return_value=mock_stream)
        mock_client.__aenter__     = AsyncMock(return_value=mock_client)
        mock_client.__aexit__      = AsyncMock(return_value=False)

        chunks = []
        with patch("gateway.platforms.skytower_processes.httpx.AsyncClient", return_value=mock_client):
            async for chunk in mgr.stream_response("3", "안녕", "7", "Alice"):
                chunks.append(chunk)

        assert chunks == ["안녕", "하세요"]

    @pytest.mark.asyncio
    async def test_yields_error_on_exception(self, tmp_path):
        mgr = _make_manager(tmp_path)
        mgr._processes["3"] = ProcessInfo(
            conv_id="3", proc=_fake_proc(), port=19003, api_key="testkey"
        )
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.stream.side_effect = Exception("connection refused")

        chunks = []
        with patch("gateway.platforms.skytower_processes.httpx.AsyncClient", return_value=mock_client):
            async for chunk in mgr.stream_response("3", "Hi", "7", "Alice"):
                chunks.append(chunk)

        assert len(chunks) == 1 and "Error" in chunks[0]


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_conversation_terminates_process(self, tmp_path):
        mgr  = _make_manager(tmp_path)
        proc = _fake_proc()
        mgr._processes["3"] = ProcessInfo(
            conv_id="3", proc=proc, port=19003, api_key="k"
        )
        result = await mgr.shutdown_conversation("3")
        assert result is True
        assert "3" not in mgr._processes
        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_nonexistent_returns_false(self, tmp_path):
        mgr    = _make_manager(tmp_path)
        result = await mgr.shutdown_conversation("99")
        assert result is False


# ---------------------------------------------------------------------------
# Adapter — per-user home channel + process mode
# ---------------------------------------------------------------------------

class TestAdapterProcessMode:
    def _make_adapter(self, monkeypatch, user_home: dict = None):
        monkeypatch.setenv("SKYTOWER_TOKEN",       "agentId:rawToken")
        monkeypatch.setenv("SKYTOWER_URL",         "http://localhost:4000")
        monkeypatch.setenv("SKYTOWER_PROCESS_MODE", "true")
        from gateway.platforms.skytower import SkyTowerAdapter
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(enabled=True, extra={
            "token": "agentId:rawToken",
            "url":   "http://localhost:4000",
        })
        adapter = SkyTowerAdapter(cfg)
        adapter._sio           = AsyncMock()
        adapter._sio.connected = True
        adapter._proc_mgr      = MagicMock()
        if user_home:
            adapter._home_channels = dict(user_home)
        return adapter

    def test_process_mode_flag(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch)
        assert adapter._process_mode is True

    @pytest.mark.asyncio
    async def test_sethome_sets_and_persists(self, monkeypatch, tmp_path):
        state_path = tmp_path / "skytower_home_channels.json"
        monkeypatch.setattr(
            "gateway.platforms.skytower._home_channels_path",
            lambda: state_path,
        )
        adapter = self._make_adapter(monkeypatch)
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "/sethome",
            "user_id": 7, "conversation_id": 5,
        })
        assert adapter._get_user_home_conv("7") == "5"
        assert json.loads(state_path.read_text())["7"] == "5"

    @pytest.mark.asyncio
    async def test_process_command_in_home_channel(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, user_home={"7": "1"})
        adapter.handle_message = AsyncMock()
        procs = [{"conv_id": "5", "pid": 1234, "port": 19005, "idle_seconds": 30}]
        adapter._proc_mgr.list_processes = AsyncMock(return_value=procs)

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "/process list",
            "user_id": 7, "conversation_id": 1,
        })

        adapter.handle_message.assert_not_called()
        emit_args = adapter._sio.emit.call_args[0]
        assert emit_args[0] == "message_done"
        assert "Conv #5" in emit_args[1]["content"]

    @pytest.mark.asyncio
    async def test_normal_message_in_home_channel_uses_standard_mode(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, user_home={"7": "1"})
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "안녕하세요",
            "user_id": 7, "conversation_id": 1,
        })
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_linked_conv_routes_to_process(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, user_home={"7": "1"})
        adapter._proc_mgr.has_process.return_value = True

        async def fake_stream(*a, **kw):
            yield "응답"
        adapter._proc_mgr.stream_response = fake_stream

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "Hi", "user_id": 7, "conversation_id": 5,
        })

        events = [c[0][0] for c in adapter._sio.emit.call_args_list]
        assert "message_done" in events

    @pytest.mark.asyncio
    async def test_unlinked_conv_uses_standard_mode(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, user_home={"7": "1"})
        adapter._proc_mgr.has_process.return_value = False
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "Hi", "user_id": 7, "conversation_id": 9,
        })
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_command_spawns_process(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, user_home={"7": "1"})
        adapter._proc_mgr.has_process.return_value = False
        mock_info = MagicMock()
        mock_info.proc.pid = 9999
        mock_info.port     = 19005
        adapter._proc_mgr.get_or_create = AsyncMock(return_value=mock_info)

        await adapter._cmd_start("5", "1")

        adapter._proc_mgr.get_or_create.assert_called_once_with("5")
        contents = [
            c[0][1]["content"]
            for c in adapter._sio.emit.call_args_list
            if c[0][0] == "message_done"
        ]
        assert any("✅" in c for c in contents)

    @pytest.mark.asyncio
    async def test_stop_command_terminates_process(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, user_home={"7": "1"})
        adapter._proc_mgr.shutdown_conversation = AsyncMock(return_value=True)

        await adapter._cmd_stop("5", "1")

        adapter._proc_mgr.shutdown_conversation.assert_called_once_with("5")
        contents = [
            c[0][1]["content"]
            for c in adapter._sio.emit.call_args_list
            if c[0][0] == "message_done"
        ]
        assert any("🛑" in c for c in contents)

    @pytest.mark.asyncio
    async def test_no_home_channel_by_default(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch)
        assert adapter._get_user_home_conv("7") is None

    @pytest.mark.asyncio
    async def test_multiple_users_independent_home_channels(self, monkeypatch):
        adapter = self._make_adapter(monkeypatch, user_home={"7": "5", "42": "11"})
        assert adapter._get_user_home_conv("7")  == "5"
        assert adapter._get_user_home_conv("42") == "11"
        assert adapter._get_user_home_conv("99") is None
