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
    from plugins.platforms.skytower.adapter import SkyTowerAdapter
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
        assert Platform("skytower") in config.platforms
        sc = config.platforms[Platform("skytower")]
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
        assert Platform("skytower") not in config.get_connected_platforms()

    def test_not_connected_without_token(self, monkeypatch):
        monkeypatch.delenv("SKYTOWER_TOKEN", raising=False)
        monkeypatch.setenv("SKYTOWER_URL", "http://localhost:4000")
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
        assert Platform("skytower") not in config.get_connected_platforms()


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
        from plugins.platforms.skytower.adapter import SkyTowerAdapter
        with pytest.raises(ValueError, match="agentId:rawToken"):
            SkyTowerAdapter(PlatformConfig(enabled=True, extra={"token": "badtoken", "url": "http://x"}))

    def test_missing_token_raises(self):
        from plugins.platforms.skytower.adapter import SkyTowerAdapter
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
# YAML config bridging (channel_skill_bindings / default_skill / channel_names)
# ---------------------------------------------------------------------------

class TestApplyYamlConfig:
    def test_seeds_known_keys(self):
        from plugins.platforms.skytower.adapter import _apply_yaml_config

        seeded = _apply_yaml_config({}, {
            "token": "a:b",
            "url": "http://x",
            "default_skill": "my-skill",
            "channel_skill_bindings": [{"id": "42", "skill": "coding-assistant"}],
            "channel_names": {"42": "코딩 채널"},
        })
        assert seeded == {
            "default_skill": "my-skill",
            "channel_skill_bindings": [{"id": "42", "skill": "coding-assistant"}],
            "channel_names": {"42": "코딩 채널"},
        }

    def test_returns_none_when_no_relevant_keys(self):
        from plugins.platforms.skytower.adapter import _apply_yaml_config

        assert _apply_yaml_config({}, {"token": "a:b", "url": "http://x"}) is None


# ---------------------------------------------------------------------------
# check_skytower_requirements
# ---------------------------------------------------------------------------

class TestRequirementsCheck:
    def test_returns_true_when_socketio_available(self):
        from plugins.platforms.skytower.adapter import check_skytower_requirements
        with patch.dict("sys.modules", {"socketio": MagicMock()}):
            assert check_skytower_requirements() is True

    def test_returns_false_when_socketio_missing(self, monkeypatch):
        import sys
        from plugins.platforms.skytower.adapter import check_skytower_requirements
        # None in sys.modules makes ``import socketio`` raise ImportError even when installed.
        monkeypatch.setitem(sys.modules, "socketio", None)
        assert check_skytower_requirements() is False


# ---------------------------------------------------------------------------
# Reply (답글) support
# ---------------------------------------------------------------------------

class TestReplyTo:
    @pytest.mark.asyncio
    async def test_reply_to_mapped_into_event(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "이거 다시 설명해줘",
            "user_id": 7,
            "conversation_id": 3,
            "id": 43,
            "reply_to": {
                "id": 40,
                "direction": "inbound",
                "type": "text",
                "user_name": None,
                "content": "첫 번째 방법은 캐시를 쓰는 것입니다.",
            },
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.reply_to_message_id == "40"
        assert event.reply_to_text == "첫 번째 방법은 캐시를 쓰는 것입니다."

    @pytest.mark.asyncio
    async def test_reply_to_absent_leaves_event_fields_empty(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 7,
            "conversation_id": 3,
            "id": 44,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.reply_to_message_id is None
        assert event.reply_to_text is None

    @pytest.mark.asyncio
    async def test_malformed_reply_to_is_ignored(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 7,
            "id": 45,
            "reply_to": "not-a-dict",
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.reply_to_message_id is None

    @pytest.mark.asyncio
    async def test_send_includes_reply_to_id(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        result = await adapter.send("skytower:testAgentId:7:3", "답변입니다", reply_to="43")

        adapter._sio.emit.assert_called_once_with("message_done", {
            "content": "답변입니다",
            "type": "text",
            "target_conversation_id": 3,
            "reply_to_id": 43,
        })
        assert result.success is True

    @pytest.mark.asyncio
    async def test_send_drops_non_numeric_reply_to(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        await adapter.send("skytower:testAgentId:7:3", "답변입니다", reply_to="abc")

        payload = adapter._sio.emit.call_args[0][1]
        assert "reply_to_id" not in payload


# ---------------------------------------------------------------------------
# Thread (스레드) support
# ---------------------------------------------------------------------------

class TestThreads:
    @pytest.mark.asyncio
    async def test_thread_id_omitted_by_default(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text", "content": "스레드 안에서",
            "user_id": 7, "conversation_id": 3, "id": 50, "thread_root_id": 40,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.source.thread_id is None

    @pytest.mark.asyncio
    async def test_thread_id_used_when_enabled(self):
        adapter = _make_adapter(thread_sessions=True)
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text", "content": "스레드 안에서",
            "user_id": 7, "conversation_id": 3, "id": 50, "thread_root_id": 40,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.source.thread_id == "t40"

    @pytest.mark.asyncio
    async def test_no_thread_id_outside_a_thread(self):
        adapter = _make_adapter(thread_sessions=True)
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text", "content": "본문에서",
            "user_id": 7, "conversation_id": 3, "id": 51,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.source.thread_id is None

    @pytest.mark.asyncio
    async def test_send_chunk_targets_the_current_conversation(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text", "content": "안녕",
            "user_id": 7, "conversation_id": 3, "id": 52,
        })
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        await adapter.send_chunk("부분 응답")

        adapter._sio.emit.assert_called_once_with("message_chunk", {
            "text": "부분 응답",
            "target_conversation_id": 3,
        })

    @pytest.mark.asyncio
    async def test_send_chunk_without_a_known_conversation(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        await adapter.send_chunk("부분 응답")

        adapter._sio.emit.assert_called_once_with("message_chunk", {"text": "부분 응답"})
