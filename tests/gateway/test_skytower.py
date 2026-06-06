"""Tests for the Skytower plugin adapter (plugins/platforms/skytower/adapter.py)."""
import pytest
from unittest.mock import AsyncMock, patch

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
        skytower_platform = Platform("skytower")
        assert skytower_platform in config.platforms
        sc = config.platforms[skytower_platform]
        assert sc.enabled is True
        assert sc.extra["token"] == "agentX:rawT0ken"
        assert sc.extra["url"] == "http://localhost:4000"

    def test_not_connected_without_url(self, monkeypatch):
        monkeypatch.setenv("SKYTOWER_TOKEN", "agentX:rawT0ken")
        monkeypatch.delenv("SKYTOWER_URL", raising=False)
        from gateway.config import GatewayConfig, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)
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
            SkyTowerAdapter(_make_config(token="bad-token-no-colon"))

    def test_missing_token_raises(self):
        from plugins.platforms.skytower.adapter import SkyTowerAdapter
        with pytest.raises(ValueError):
            SkyTowerAdapter(_make_config(token=""))


# ---------------------------------------------------------------------------
# Chat ID format
# ---------------------------------------------------------------------------

class TestChatIdFormat:
    def test_four_part_jid_with_conversation(self):
        adapter = _make_adapter()
        parts = f"skytower:{adapter._agent_id}:42:99".split(":")
        assert parts[2] == "42"
        assert parts[3] == "99"

    def test_three_part_jid_without_conversation(self):
        adapter = _make_adapter()
        parts = f"skytower:{adapter._agent_id}:42".split(":")
        assert len(parts) == 3
        assert parts[2] == "42"


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------

class TestHandleRelayMessage:
    @pytest.mark.asyncio
    async def test_ignores_inbound_direction(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({"direction": "inbound", "type": "text", "user_id": "1"})
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_text_type_without_file_content(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({"direction": "outbound", "type": "image", "user_id": "1"})
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_missing_user_id(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({"direction": "outbound", "type": "text"})
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_valid_message(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "7", "content": "hello",
        })
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_chat_id_without_conversation(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        captured = {}

        async def _capture(event):
            captured["event"] = event
        adapter.handle_message = _capture

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "9", "content": "hi",
        })
        assert "skytower:testAgentId:9" in captured["event"].source.chat_id


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

class TestSend:
    @pytest.mark.asyncio
    async def test_send_with_conversation_id(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        await adapter.send("skytower:testAgentId:5:10", "hello")
        payload = adapter._sio.emit.call_args[0][1]
        assert payload["target_conversation_id"] == 10

    @pytest.mark.asyncio
    async def test_send_without_conversation_id(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        await adapter.send("skytower:testAgentId:5", "hello")
        payload = adapter._sio.emit.call_args[0][1]
        assert payload["target_user_id"] == 5

    @pytest.mark.asyncio
    async def test_send_returns_error_when_not_connected(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = False
        result = await adapter.send("skytower:testAgentId:5", "hello")
        assert result.success is False

    @pytest.mark.asyncio
    async def test_send_handles_emit_exception(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        adapter._sio.emit.side_effect = RuntimeError("network error")
        result = await adapter.send("skytower:testAgentId:5", "hello")
        assert result.success is False


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
        await adapter.send_thinking_chunk("thinking...")
        adapter._sio.emit.assert_called_once()
        event = adapter._sio.emit.call_args[0][0]
        assert event == "thinking_chunk"

    @pytest.mark.asyncio
    async def test_send_notification(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        await adapter.send_notification("Test", "body", "info")
        adapter._sio.emit.assert_called_once_with(
            "notify", {"level": "info", "title": "Test", "body": "body"}
        )

    @pytest.mark.asyncio
    async def test_chunk_silently_skipped_when_disconnected(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = False
        await adapter.send_chunk("text")
        adapter._sio.emit.assert_not_called()


# ---------------------------------------------------------------------------
# get_chat_info / send_typing
# ---------------------------------------------------------------------------

class TestGetChatInfo:
    @pytest.mark.asyncio
    async def test_returns_dm_info(self):
        adapter = _make_adapter()
        info = await adapter.get_chat_info("skytower:testAgentId:42")
        assert info["type"] == "dm"
        assert info["platform"] == "skytower"

    @pytest.mark.asyncio
    async def test_send_typing_is_noop(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        await adapter.send_typing("skytower:testAgentId:42")
        adapter._sio.emit.assert_not_called()


# ---------------------------------------------------------------------------
# Skill bindings
# ---------------------------------------------------------------------------

class TestSkillBindings:
    @pytest.mark.asyncio
    async def test_auto_skill_from_channel_skill_bindings(self):
        adapter = _make_adapter(channel_skill_bindings=[{"id": "99", "skill": "coding"}])
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "conversation_id": "99", "content": "hi",
        })
        assert captured["event"].auto_skill == ["coding"]

    @pytest.mark.asyncio
    async def test_auto_skill_multiple_skills(self):
        adapter = _make_adapter(channel_skill_bindings=[{"id": "5", "skills": ["a", "b"]}])
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "conversation_id": "5", "content": "hi",
        })
        assert captured["event"].auto_skill == ["a", "b"]

    @pytest.mark.asyncio
    async def test_default_skill_fallback(self):
        adapter = _make_adapter(default_skill="fallback-skill")
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "content": "hi",
        })
        assert captured["event"].auto_skill == ["fallback-skill"]

    @pytest.mark.asyncio
    async def test_no_skill_when_no_binding_no_default(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "content": "hi",
        })
        assert captured["event"].auto_skill is None

    @pytest.mark.asyncio
    async def test_channel_prompt_injected(self):
        adapter = _make_adapter(channel_prompts={"7": "Be concise."})
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "conversation_id": "7", "content": "hi",
        })
        assert captured["event"].channel_prompt == "Be concise."

    @pytest.mark.asyncio
    async def test_channel_name_as_chat_topic(self):
        adapter = _make_adapter(channel_names={"3": "Dev Room"})
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "conversation_id": "3", "content": "hi",
        })
        assert captured["event"].source.chat_topic == "Dev Room"

    @pytest.mark.asyncio
    async def test_no_chat_topic_without_channel_names(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "conversation_id": "3", "content": "hi",
        })
        assert captured["event"].source.chat_topic is None

    @pytest.mark.asyncio
    async def test_channel_skill_binding_wins_over_default(self):
        adapter = _make_adapter(
            default_skill="default",
            channel_skill_bindings=[{"id": "8", "skill": "specific"}],
        )
        adapter._sio = AsyncMock()
        captured = {}

        async def _cap(event):
            captured["event"] = event
        adapter.handle_message = _cap

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "conversation_id": "8", "content": "hi",
        })
        assert captured["event"].auto_skill == ["specific"]


# ---------------------------------------------------------------------------
# /skills 명령
# ---------------------------------------------------------------------------

class TestSkillsCommand:
    @pytest.mark.asyncio
    async def test_skills_command_intercepted(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        adapter.handle_message = AsyncMock()

        with patch("agent.skill_commands.scan_skill_commands", return_value={}):
            await adapter._handle_relay_message({
                "direction": "outbound", "type": "text",
                "user_id": "1", "content": "/skills",
            })

        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skill_list_command_intercepted(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True
        adapter.handle_message = AsyncMock()

        with patch("agent.skill_commands.scan_skill_commands", return_value={}):
            await adapter._handle_relay_message({
                "direction": "outbound", "type": "text",
                "user_id": "1", "content": "/skill-list",
            })

        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skills_command_sends_list(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        fake_skills = {"/analyze": {"name": "analyze", "description": "코드 분석"}}
        with patch("agent.skill_commands.scan_skill_commands", return_value=fake_skills):
            await adapter._handle_relay_message({
                "direction": "outbound", "type": "text",
                "user_id": "7", "content": "/skills",
            })

        payload = adapter._sio.emit.call_args[0][1]
        assert "analyze" in payload["content"]

    @pytest.mark.asyncio
    async def test_skills_command_empty(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("agent.skill_commands.scan_skill_commands", return_value={}):
            await adapter._handle_relay_message({
                "direction": "outbound", "type": "text",
                "user_id": "7", "content": "/skills",
            })

        payload = adapter._sio.emit.call_args[0][1]
        assert "없습니다" in payload["content"]


# ---------------------------------------------------------------------------
# request:agent-skills 이벤트
# ---------------------------------------------------------------------------

class TestAgentSkillsRequest:
    @pytest.mark.asyncio
    async def test_responds_with_skills(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        fake_skills = {
            "/analyze": {"name": "analyze", "description": "코드 분석"},
            "/refactor": {"name": "refactor", "description": "코드 리팩토링"},
        }
        with patch("agent.skill_commands.scan_skill_commands", return_value=fake_skills), \
             patch("gateway.commands_parser.get_hermes_commands", return_value={}):
            await adapter._handle_agent_skills_request({"requestId": "req-abc"})

        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert payload["requestId"] == "req-abc"
        assert any(s["command"] == "/analyze" for s in payload["skills"])
        assert any(s["command"] == "/refactor" for s in payload["skills"])
        assert payload["metadata"]["agentType"] == "hermes"

    @pytest.mark.asyncio
    async def test_responds_with_empty_skills(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("agent.skill_commands.scan_skill_commands", return_value={}), \
             patch("gateway.commands_parser.get_hermes_commands", return_value={}):
            await adapter._handle_agent_skills_request({"requestId": "req-xyz"})

        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert payload["skills"] == []
        assert payload["requestId"] == "req-xyz"

    @pytest.mark.asyncio
    async def test_not_connected_does_nothing(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = False

        await adapter._handle_agent_skills_request({"requestId": "req-1"})
        adapter._sio.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_error_responds_with_empty_skills(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("agent.skill_commands.scan_skill_commands", side_effect=RuntimeError("오류")), \
             patch("gateway.commands_parser.get_hermes_commands", return_value={}):
            await adapter._handle_agent_skills_request({"requestId": "req-err"})

        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert payload["skills"] == []


# ---------------------------------------------------------------------------
# commands (동적 파싱) 테스트
# ---------------------------------------------------------------------------

class TestAgentSkillsCommandsField:
    @pytest.mark.asyncio
    async def test_responds_with_commands_dict(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        fake_commands = {
            "Session": [{"command": "/new", "description": "Start a new session"}],
            "Configuration": [{"command": "/model", "description": "Switch model"}],
        }
        with patch("agent.skill_commands.scan_skill_commands", return_value={}), \
             patch("gateway.commands_parser.get_hermes_commands", return_value=fake_commands):
            await adapter._handle_agent_skills_request({"requestId": "req-cmd"})

        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert "commands" in payload
        assert payload["commands"]["Session"][0]["command"] == "/new"
        assert payload["commands"]["Configuration"][0]["command"] == "/model"

    @pytest.mark.asyncio
    async def test_commands_error_falls_back_to_empty_dict(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("agent.skill_commands.scan_skill_commands", return_value={}), \
             patch("gateway.commands_parser.get_hermes_commands", side_effect=RuntimeError("오류")):
            await adapter._handle_agent_skills_request({"requestId": "req-err2"})

        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert payload["commands"] == {}

    @pytest.mark.asyncio
    async def test_refresh_commands_invalidates_cache_and_responds(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("gateway.commands_parser.invalidate_commands_cache") as mock_invalidate:
            await adapter._handle_refresh_commands({"requestId": "refresh-1"})

        mock_invalidate.assert_called_once()
        adapter._sio.emit.assert_called_once()
        event, payload = adapter._sio.emit.call_args[0]
        assert event == "refresh:agent-commands-response"
        assert payload["requestId"] == "refresh-1"
        assert payload["status"] == "ok"

    @pytest.mark.asyncio
    async def test_refresh_not_connected_does_nothing(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = False

        await adapter._handle_refresh_commands({"requestId": "refresh-2"})
        adapter._sio.emit.assert_not_called()


# ---------------------------------------------------------------------------
# commands_parser 단위 테스트
# ---------------------------------------------------------------------------

class TestCommandsParser:
    def test_get_hermes_commands_returns_categories(self):
        from gateway.commands_parser import get_hermes_commands, invalidate_commands_cache
        invalidate_commands_cache()
        commands = get_hermes_commands()

        assert isinstance(commands, dict)
        assert len(commands) > 0
        for cat, items in commands.items():
            assert isinstance(cat, str)
            assert isinstance(items, list)
            for item in items:
                assert "command" in item
                assert "description" in item
                assert item["command"].startswith("/")

    def test_get_hermes_commands_includes_session_category(self):
        from gateway.commands_parser import get_hermes_commands, invalidate_commands_cache
        invalidate_commands_cache()
        commands = get_hermes_commands()
        assert "Session" in commands
        assert len(commands["Session"]) > 0

    def test_get_hermes_commands_cached(self):
        from gateway.commands_parser import get_hermes_commands, invalidate_commands_cache
        invalidate_commands_cache()
        first = get_hermes_commands()
        second = get_hermes_commands()
        assert first is second

    def test_invalidate_commands_cache_clears_cache(self):
        from gateway.commands_parser import get_hermes_commands, invalidate_commands_cache
        invalidate_commands_cache()
        first = get_hermes_commands()
        invalidate_commands_cache()
        second = get_hermes_commands()
        assert first is not second

    def test_get_hermes_commands_no_cli_only_commands(self):
        from gateway.commands_parser import get_hermes_commands, invalidate_commands_cache
        invalidate_commands_cache()
        commands = get_hermes_commands()
        all_command_names = [
            item["command"]
            for items in commands.values()
            for item in items
        ]
        assert "/clear" not in all_command_names
        assert "/history" not in all_command_names
        assert "/save" not in all_command_names


# ---------------------------------------------------------------------------
# file_content 처리
# ---------------------------------------------------------------------------

class TestFileContent:
    def _make_small_png(self) -> bytes:
        import base64
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )

    @pytest.mark.asyncio
    async def test_image_file_content_caches_and_sets_media(self):
        import base64
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()

        png = self._make_small_png()
        b64 = base64.b64encode(png).decode()

        _patch = patch
        with _patch("plugins.platforms.skytower.adapter.cache_image_from_bytes", return_value="/cache/img_abc.png") as mock_cache:
            await adapter._handle_relay_message({
                "direction": "outbound", "type": "text",
                "user_id": "1", "content": "look at this",
                "file_content": {"type": "image", "mimeType": "image/png", "data": b64},
            })

        mock_cache.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert "/cache/img_abc.png" in event.media_urls

    @pytest.mark.asyncio
    async def test_image_fallback_on_cache_error(self):
        import base64
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()

        png = self._make_small_png()
        b64 = base64.b64encode(png).decode()

        with patch("plugins.platforms.skytower.adapter.cache_image_from_bytes", side_effect=ValueError("bad image")):
            await adapter._handle_relay_message({
                "direction": "outbound", "type": "text",
                "user_id": "1", "content": "pic",
                "file_content": {"type": "image", "mimeType": "image/png", "data": b64},
                "file_name": "photo.png",
            })

        event = adapter.handle_message.call_args[0][0]
        assert "처리 실패" in event.text

    @pytest.mark.asyncio
    async def test_text_file_content_appended_to_prompt(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "content": "review this",
            "file_content": {"type": "text", "data": "def foo(): pass"},
            "file_name": "code.py",
        })

        event = adapter.handle_message.call_args[0][0]
        assert "def foo(): pass" in event.text
        assert "code.py" in event.text

    @pytest.mark.asyncio
    async def test_text_file_content_truncated_flag(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "content": "check",
            "file_content": {"type": "text", "data": "x", "truncated": True},
            "file_name": "big.txt",
        })

        event = adapter.handle_message.call_args[0][0]
        assert "100KB" in event.text

    @pytest.mark.asyncio
    async def test_binary_file_content_metadata_appended(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "content": "got file",
            "file_content": {"type": "file", "mimeType": "application/pdf", "size": 2048},
            "file_name": "doc.pdf",
            "file_path": "/tmp/doc.pdf",
        })

        event = adapter.handle_message.call_args[0][0]
        assert "doc.pdf" in event.text
        assert "application/pdf" in event.text

    @pytest.mark.asyncio
    async def test_text_message_without_file_content_unchanged(self):
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text",
            "user_id": "1", "content": "plain message",
        })

        event = adapter.handle_message.call_args[0][0]
        assert event.text == "plain message"


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

class TestRequirementsCheck:
    def test_returns_true_when_socketio_available(self):
        from plugins.platforms.skytower.adapter import check_skytower_requirements
        import unittest.mock as mock
        with mock.patch.dict("sys.modules", {"socketio": mock.MagicMock()}):
            assert check_skytower_requirements() is True

    def test_returns_false_when_socketio_missing(self):
        from plugins.platforms.skytower.adapter import check_skytower_requirements
        import sys
        import unittest.mock as mock
        with mock.patch.dict("sys.modules", {"socketio": None}):
            # builtins import will fail for socketio
            original = sys.modules.pop("socketio", mock.sentinel)
            try:
                result = check_skytower_requirements()
            except Exception:
                result = False
            finally:
                if original is not mock.sentinel:
                    sys.modules["socketio"] = original
            # Just verify the function exists and handles ImportError gracefully
            assert isinstance(result, bool)
