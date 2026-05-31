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
    async def test_ignores_non_text_type_without_file_content(self):
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        # file_content 없는 image 타입은 무시됨
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
# Skill bindings (auto_skill, channel_prompts, channel_names)
# ---------------------------------------------------------------------------

class TestSkillBindings:
    @pytest.mark.asyncio
    async def test_auto_skill_from_channel_skill_bindings(self):
        """conv_id가 channel_skill_bindings에 매핑되면 auto_skill이 설정된다."""
        adapter = _make_adapter(
            channel_skill_bindings=[
                {"id": "3", "skill": "coding-assistant"},
            ]
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "안녕",
            "user_id": 7,
            "conversation_id": 3,
            "id": 1,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.auto_skill == ["coding-assistant"]

    @pytest.mark.asyncio
    async def test_auto_skill_multiple_skills(self):
        """channel_skill_bindings에 skills 리스트가 있으면 모두 설정된다."""
        adapter = _make_adapter(
            channel_skill_bindings=[
                {"id": "10", "skills": ["research", "writer"]},
            ]
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 1,
            "conversation_id": 10,
            "id": 2,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.auto_skill == ["research", "writer"]

    @pytest.mark.asyncio
    async def test_default_skill_fallback(self):
        """channel_skill_bindings에 해당 conv가 없으면 default_skill이 적용된다."""
        adapter = _make_adapter(
            default_skill="global-helper",
            channel_skill_bindings=[
                {"id": "99", "skill": "other"},
            ]
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 5,
            "conversation_id": 42,
            "id": 3,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.auto_skill == ["global-helper"]

    @pytest.mark.asyncio
    async def test_no_skill_when_no_binding_no_default(self):
        """바인딩도 default_skill도 없으면 auto_skill은 None이다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 5,
            "conversation_id": 7,
            "id": 4,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.auto_skill is None

    @pytest.mark.asyncio
    async def test_channel_prompt_injected(self):
        """channel_prompts에 매핑된 conv_id는 channel_prompt가 설정된다."""
        adapter = _make_adapter(
            channel_prompts={"5": "You are a senior engineer."}
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 1,
            "conversation_id": 5,
            "id": 5,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.channel_prompt == "You are a senior engineer."

    @pytest.mark.asyncio
    async def test_channel_name_as_chat_topic(self):
        """channel_names에 매핑된 conv_id는 source.chat_topic이 설정된다."""
        adapter = _make_adapter(
            channel_names={"3": "코딩 채널"}
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 7,
            "conversation_id": 3,
            "id": 6,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_topic == "코딩 채널"

    @pytest.mark.asyncio
    async def test_no_chat_topic_without_channel_names(self):
        """channel_names 없이는 chat_topic이 None이다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hello",
            "user_id": 7,
            "conversation_id": 3,
            "id": 7,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_topic is None

    @pytest.mark.asyncio
    async def test_channel_skill_binding_wins_over_default(self):
        """binding이 있는 채널은 default_skill 대신 binding이 사용된다."""
        adapter = _make_adapter(
            default_skill="fallback-skill",
            channel_skill_bindings=[
                {"id": "77", "skill": "specific-skill"},
            ]
        )
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "hi",
            "user_id": 1,
            "conversation_id": 77,
            "id": 8,
        })
        event = adapter.handle_message.call_args[0][0]
        assert event.auto_skill == ["specific-skill"]


# ---------------------------------------------------------------------------
# /skills command
# ---------------------------------------------------------------------------

class TestSkillsCommand:
    @pytest.mark.asyncio
    async def test_skills_command_intercepted(self):
        """/skills 명령은 handle_message를 거치지 않고 즉시 처리된다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        adapter._handle_skills_command = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "/skills",
            "user_id": 7,
            "conversation_id": 3,
            "id": 9,
        })
        adapter._handle_skills_command.assert_called_once_with("7", "3")
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skill_list_command_intercepted(self):
        """/skill-list 도 동일하게 처리된다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        adapter._handle_skills_command = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "/skill-list",
            "user_id": 7,
            "id": 10,
        })
        adapter._handle_skills_command.assert_called_once_with("7", None)
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skills_command_sends_list(self):
        """스킬이 있으면 목록을 응답한다."""
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        fake_skills = {
            "/my-skill": {"name": "my-skill", "description": "My test skill"},
        }
        with patch("agent.skill_commands.scan_skill_commands", return_value=fake_skills):
            await adapter._handle_skills_command("7", "3")

        adapter._sio.emit.assert_called_once()
        payload = adapter._sio.emit.call_args[0][1]
        assert "my-skill" in payload["content"]
        assert "/my-skill" in payload["content"]

    @pytest.mark.asyncio
    async def test_skills_command_empty(self):
        """스킬이 없으면 안내 메시지를 응답한다."""
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("agent.skill_commands.scan_skill_commands", return_value={}):
            await adapter._handle_skills_command("7", None)

        payload = adapter._sio.emit.call_args[0][1]
        assert "없습니다" in payload["content"]


# ---------------------------------------------------------------------------
# request:agent-skills 이벤트
# ---------------------------------------------------------------------------

class TestAgentSkillsRequest:
    @pytest.mark.asyncio
    async def test_responds_with_skills(self):
        """request:agent-skills 수신 시 agent:skills-response를 응답한다."""
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        fake_skills = {
            "/analyze": {"name": "analyze", "description": "코드 분석"},
            "/refactor": {"name": "refactor", "description": "코드 리팩토링"},
        }
        with patch("agent.skill_commands.scan_skill_commands", return_value=fake_skills):
            await adapter._handle_agent_skills_request({"requestId": "req-abc"})

        adapter._sio.emit.assert_called_once()
        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert payload["requestId"] == "req-abc"
        assert any(s["command"] == "/analyze" for s in payload["skills"])
        assert any(s["command"] == "/refactor" for s in payload["skills"])
        assert payload["metadata"]["agentType"] == "hermes"

    @pytest.mark.asyncio
    async def test_responds_with_empty_skills(self):
        """스킬이 없으면 빈 배열로 응답한다."""
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("agent.skill_commands.scan_skill_commands", return_value={}):
            await adapter._handle_agent_skills_request({"requestId": "req-xyz"})

        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert payload["skills"] == []
        assert payload["requestId"] == "req-xyz"

    @pytest.mark.asyncio
    async def test_not_connected_does_nothing(self):
        """연결되지 않은 경우 아무것도 전송하지 않는다."""
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = False

        await adapter._handle_agent_skills_request({"requestId": "req-1"})
        adapter._sio.emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_scan_error_responds_with_empty_skills(self):
        """scan_skill_commands 실패 시 빈 배열로 응답한다."""
        adapter = _make_adapter()
        adapter._sio = AsyncMock()
        adapter._sio.connected = True

        with patch("agent.skill_commands.scan_skill_commands", side_effect=RuntimeError("오류")):
            await adapter._handle_agent_skills_request({"requestId": "req-err"})

        event, payload = adapter._sio.emit.call_args[0]
        assert event == "agent:skills-response"
        assert payload["skills"] == []


# ---------------------------------------------------------------------------
# file_content 처리
# ---------------------------------------------------------------------------

class TestFileContent:
    """file_content 필드를 포함한 메시지 처리 테스트."""

    def _make_small_png(self) -> bytes:
        """1x1 PNG 이미지 바이트를 반환합니다."""
        import base64
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )

    # ── 이미지 처리 ────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_image_file_content_caches_and_sets_media(self):
        """이미지 file_content가 캐시되어 media_urls에 추가되고 PHOTO 타입이 된다."""
        import base64
        from unittest.mock import patch as _patch

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        img_bytes = self._make_small_png()
        b64data = base64.b64encode(img_bytes).decode()

        with _patch("gateway.platforms.skytower.cache_image_from_bytes", return_value="/cache/img_abc.png") as mock_cache:
            await adapter._handle_relay_message({
                "direction": "outbound",
                "type": "image",
                "content": "이 이미지를 설명해줘",
                "user_id": 7,
                "conversation_id": 3,
                "id": 1,
                "file_name": "photo.png",
                "file_content": {
                    "type": "image",
                    "data": b64data,
                    "mimeType": "image/png",
                },
            })

        mock_cache.assert_called_once()
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.message_type.value == "photo"
        assert "/cache/img_abc.png" in event.media_urls
        assert "image/png" in event.media_types
        assert event.text == "이 이미지를 설명해줘"

    @pytest.mark.asyncio
    async def test_image_fallback_on_cache_error(self):
        """이미지 캐싱 실패 시 오류 메시지가 텍스트에 추가된다."""
        import base64
        from unittest.mock import patch as _patch

        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        with _patch("gateway.platforms.skytower.cache_image_from_bytes", side_effect=ValueError("bad image")):
            await adapter._handle_relay_message({
                "direction": "outbound",
                "type": "image",
                "content": "caption",
                "user_id": 7,
                "id": 2,
                "file_name": "bad.jpg",
                "file_content": {
                    "type": "image",
                    "data": base64.b64encode(b"not-an-image").decode(),
                    "mimeType": "image/jpeg",
                },
            })

        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.message_type.value == "text"
        assert not event.media_urls
        assert "처리 실패" in event.text

    # ── 텍스트 파일 처리 ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_text_file_content_appended_to_prompt(self):
        """텍스트 file_content가 프롬프트에 포함된다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "이 파일 설명해줘",
            "user_id": 7,
            "id": 3,
            "file_name": "config.json",
            "file_content": {
                "type": "text",
                "data": '{"key": "value"}',
            },
        })

        event = adapter.handle_message.call_args[0][0]
        assert event.message_type.value == "text"
        assert "config.json" in event.text
        assert '{"key": "value"}' in event.text
        assert not event.media_urls

    @pytest.mark.asyncio
    async def test_text_file_content_truncated_flag(self):
        """truncated=True 이면 안내 문구가 포함된다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "",
            "user_id": 7,
            "id": 4,
            "file_name": "large.txt",
            "file_content": {
                "type": "text",
                "data": "partial content",
                "truncated": True,
            },
        })

        event = adapter.handle_message.call_args[0][0]
        assert "처음 100KB만 표시" in event.text

    # ── 기타 파일 처리 ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_binary_file_content_metadata_appended(self):
        """타입이 'file'인 경우 메타데이터가 프롬프트에 추가된다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "이 PDF에 대해 알려줘",
            "user_id": 7,
            "id": 5,
            "file_name": "report.pdf",
            "file_path": "~/documents/report.pdf",
            "file_content": {
                "type": "file",
                "size": 2097152,
                "mimeType": "application/pdf",
            },
        })

        event = adapter.handle_message.call_args[0][0]
        assert event.message_type.value == "text"
        assert "report.pdf" in event.text
        assert "application/pdf" in event.text
        assert "2048.0KB" in event.text
        assert not event.media_urls

    # ── 하위 호환성 ─────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_text_message_without_file_content_unchanged(self):
        """file_content 없는 일반 text 메시지는 기존과 동일하게 처리된다."""
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()

        await adapter._handle_relay_message({
            "direction": "outbound",
            "type": "text",
            "content": "안녕하세요",
            "user_id": 7,
            "conversation_id": 3,
            "id": 6,
        })

        event = adapter.handle_message.call_args[0][0]
        assert event.text == "안녕하세요"
        assert event.message_type.value == "text"
        assert not event.media_urls


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
