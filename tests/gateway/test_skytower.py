"""Tests for the Skytower Relay gateway adapter."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform, PlatformConfig


def _make_config(**extra):
    return PlatformConfig(
        enabled=True,
        extra={
            "token": "testAgentId:testRawToken",
            "url": "http://localhost:4000",
            **extra,
        },
    )


def _make_adapter(**extra):
    from gateway.platforms.skytower import SkyTowerAdapter
    return SkyTowerAdapter(_make_config(**extra))


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestSkyTowerConfig:
    def test_apply_env_overrides_sets_platform(self, monkeypatch):
        monkeypatch.setenv("SKYTOWER_TOKEN", "agentX:rawT0ken")
        monkeypatch.setenv("SKYTOWER_URL", "http://localhost:4000")
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.SKYTOWER in config.platforms
        sc = config.platforms[Platform.SKYTOWER]
        assert sc.enabled is True
        assert sc.extra["token"] == "agentX:rawT0ken"
        assert sc.extra["url"] == "http://localhost:4000"

    def test_not_connected_without_url(self, monkeypatch):
        monkeypatch.setenv("SKYTOWER_TOKEN", "agentX:rawT0ken")
        monkeypatch.delenv("SKYTOWER_URL", raising=False)
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        # Platform should not appear in connected_platforms without both vars
        assert Platform.SKYTOWER not in config.get_connected_platforms()

    def test_not_connected_without_token(self, monkeypatch):
        monkeypatch.delenv("SKYTOWER_TOKEN", raising=False)
        monkeypatch.setenv("SKYTOWER_URL", "http://localhost:4000")
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform.SKYTOWER not in config.get_connected_platforms()


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------

class TestSkyTowerAdapterInit:
    def test_agent_id_parsed_from_token(self):
        adapter = _make_adapter()
        assert adapter._agent_id == "testAgentId"

    def test_token_stored(self):
        adapter = _make_adapter()
        assert adapter._token == "testAgentId:testRawToken"

    def test_relay_url_stored(self):
        adapter = _make_adapter()
        assert adapter._relay_url == "http://localhost:4000"

    def test_invalid_token_raises(self):
        from gateway.platforms.skytower import SkyTowerAdapter
        with pytest.raises(ValueError, match="agentId:rawToken"):
            SkyTowerAdapter(PlatformConfig(enabled=True, extra={"token": "badtoken", "url": "http://x"}))

    def test_missing_token_raises(self):
        from gateway.platforms.skytower import SkyTowerAdapter
        with pytest.raises(ValueError):
            SkyTowerAdapter(PlatformConfig(enabled=True, extra={"url": "http://x"}))


# ---------------------------------------------------------------------------
# Chat ID format
# ---------------------------------------------------------------------------

class TestChatIdFormat:
    def test_four_part_jid_with_conversation(self):
        chat_id = "skytower:testAgentId:7:3"
        parts = chat_id.split(":")
        assert parts[0] == "skytower"
        assert parts[1] == "testAgentId"
        assert parts[2] == "7"
        assert parts[3] == "3"

    def test_three_part_jid_without_conversation(self):
        chat_id = "skytower:testAgentId:7"
        parts = chat_id.split(":")
        assert len(parts) == 3
        assert parts[2] == "7"


# ---------------------------------------------------------------------------
# Inbound message handling
# ---------------------------------------------------------------------------

class TestHandleRelayMessage:
    @pytest.mark.asyncio
    async def test_ignores_inbound_direction(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({"direction": "inbound", "content": "ignored"})
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_text_type(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "image", "user_id": 7, "content": "ignored"
        })
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_missing_user_id(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text", "content": "hello"
        })
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_valid_message(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "안녕하세요",
            "user_id": 7,
            "user_name": "홍길동",
            "conversation_id": 3,
            "id": 42,
        })
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.text == "안녕하세요"
        assert event.source.chat_id == "skytower:testAgentId:7:3"
        assert event.source.user_id == "7"
        assert event.message_id == "42"

    @pytest.mark.asyncio
    async def test_chat_id_without_conversation(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 5,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_id == "skytower:testAgentId:5"


# ---------------------------------------------------------------------------
# Outbound send
# ---------------------------------------------------------------------------

class TestSend:
    @pytest.mark.asyncio
    async def test_send_with_conversation_id(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        result = await adapter.send("skytower:testAgentId:7:3", "Hello!")

        adapter._sio.emit.assert_called_once_with("message_done", {
            "content": "Hello!",
            "type": "text",
            "target_conversation_id": 3,
        })
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_without_conversation_id(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        result = await adapter.send("skytower:testAgentId:7", "Hello!")

        adapter._sio.emit.assert_called_once_with("message_done", {
            "content": "Hello!",
            "type": "text",
            "target_user_id": 7,
        })
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_returns_error_when_not_connected(self):
        adapter = _make_adapter()
        adapter._sio = None

        result = await adapter.send("skytower:testAgentId:7:3", "Hello!")
        assert result.success is False
        assert "Not connected" in result.error

    @pytest.mark.asyncio
    async def test_send_handles_emit_exception(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        adapter._sio.emit.side_effect = RuntimeError("network error")

        result = await adapter.send("skytower:testAgentId:7:3", "Hello!")
        assert result.success is False
        assert "network error" in result.error


# ---------------------------------------------------------------------------
# Streaming extras
# ---------------------------------------------------------------------------

class TestStreamingExtras:
    @pytest.mark.asyncio
    async def test_send_chunk(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        await adapter.send_chunk("partial text")
        adapter._sio.emit.assert_called_once_with("message_chunk", {"text": "partial text"})

    @pytest.mark.asyncio
    async def test_send_thinking_chunk(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        await adapter.send_thinking_chunk("reasoning...")
        adapter._sio.emit.assert_called_once_with("thinking_chunk", {"text": "reasoning..."})

    @pytest.mark.asyncio
    async def test_send_notification(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        await adapter.send_notification("Title", "Body text", level="warning")
        adapter._sio.emit.assert_called_once_with(
            "notify", {"level": "warning", "title": "Title", "body": "Body text"}
        )

    @pytest.mark.asyncio
    async def test_chunk_silently_skipped_when_disconnected(self):
        adapter = _make_adapter()
        adapter._sio = None
        # Should not raise
        await adapter.send_chunk("text")
        await adapter.send_thinking_chunk("text")


# ---------------------------------------------------------------------------
# get_chat_info
# ---------------------------------------------------------------------------

class TestGetChatInfo:
    @pytest.mark.asyncio
    async def test_returns_dm_info(self):
        adapter = _make_adapter()
        info = await adapter.get_chat_info("skytower:testAgentId:42:1")
        assert info["type"] == "dm"
        assert info["platform"] == "skytower"
        assert "42" in info["name"]

    @pytest.mark.asyncio
    async def test_send_typing_is_noop(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        # Should not raise and should not emit
        await adapter.send_typing("skytower:testAgentId:7:3")
        adapter._sio.emit.assert_not_called()


# ---------------------------------------------------------------------------
# check_skytower_requirements
# ---------------------------------------------------------------------------

class TestRequirementsCheck:
    def test_returns_true_when_socketio_available(self):
        from gateway.platforms.skytower import check_skytower_requirements
        with patch.dict("sys.modules", {"socketio": MagicMock()}):
            assert check_skytower_requirements() is True

    def test_returns_false_when_socketio_missing(self):
        import sys
        from gateway.platforms.skytower import check_skytower_requirements
        original = sys.modules.pop("socketio", None)
        try:
            assert check_skytower_requirements() is False
        finally:
            if original is not None:
                sys.modules["socketio"] = original
