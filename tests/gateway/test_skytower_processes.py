"""Tests for SkyTowerAdapter — per-user home channel and standard routing."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Adapter helpers
# ---------------------------------------------------------------------------

def _make_adapter(monkeypatch, user_home: dict = None):
    monkeypatch.setenv("SKYTOWER_TOKEN", "agentId:rawToken")
    monkeypatch.setenv("SKYTOWER_URL",   "http://localhost:4000")
    from gateway.platforms.skytower import SkyTowerAdapter
    from gateway.config import PlatformConfig
    cfg = PlatformConfig(enabled=True, extra={
        "token": "agentId:rawToken",
        "url":   "http://localhost:4000",
    })
    adapter = SkyTowerAdapter(cfg)
    adapter._sio           = AsyncMock()
    adapter._sio.connected = True
    if user_home:
        adapter._home_channels = dict(user_home)
    return adapter


# ---------------------------------------------------------------------------
# /sethome
# ---------------------------------------------------------------------------

class TestSetHome:
    @pytest.mark.asyncio
    async def test_sethome_sets_and_persists(self, monkeypatch, tmp_path):
        state_path = tmp_path / "skytower_home_channels.json"
        monkeypatch.setattr(
            "gateway.platforms.skytower._home_channels_path",
            lambda: state_path,
        )
        adapter = _make_adapter(monkeypatch)
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "/sethome",
            "user_id": 7, "conversation_id": 5,
        })
        assert adapter._get_user_home_conv("7") == "5"
        assert json.loads(state_path.read_text())["7"] == "5"

    @pytest.mark.asyncio
    async def test_sethome_outside_conv_replies_error(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "/sethome",
            "user_id": 7,
        })
        emit_args = adapter._sio.emit.call_args[0]
        assert "❌" in emit_args[1]["content"]

    def test_no_home_channel_by_default(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        assert adapter._get_user_home_conv("7") is None

    def test_multiple_users_independent_home_channels(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, user_home={"7": "5", "42": "11"})
        assert adapter._get_user_home_conv("7")  == "5"
        assert adapter._get_user_home_conv("42") == "11"
        assert adapter._get_user_home_conv("99") is None


# ---------------------------------------------------------------------------
# 표준 메시지 라우팅
# ---------------------------------------------------------------------------

class TestStandardRouting:
    @pytest.mark.asyncio
    async def test_normal_message_calls_handle_message(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "안녕하세요",
            "user_id": 7, "conversation_id": 5,
        })
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_message_without_conv_calls_handle_message(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "hello",
            "user_id": 7,
        })
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_inbound_direction_dropped(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "inbound", "type": "text",
            "content": "hi", "user_id": 7,
        })
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_text_type_dropped(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "image",
            "content": "hi", "user_id": 7,
        })
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_user_id_dropped(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "content": "hi",
        })
        adapter.handle_message.assert_not_called()
