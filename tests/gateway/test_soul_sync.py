"""Tests for gateway.soul_sync — SoulSync class and SkyTower adapter integration."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.soul_sync import SoulSync
from tests.gateway._plugin_adapter_loader import load_plugin_adapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soul_sync(tmp_path: Path, allow_overwrite: bool = False) -> SoulSync:
    return SoulSync(
        agent_id="testAgent",
        token="testAgent:rawToken",
        relay_url="http://localhost:4000",
        hermes_home=tmp_path,
        allow_overwrite=allow_overwrite,
    )


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------

class TestSoulSyncInit:
    def test_agent_id_stored(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss._agent_id == "testAgent"

    def test_token_stored(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss._token == "testAgent:rawToken"

    def test_relay_url_trailing_slash_stripped(self, tmp_path):
        ss = SoulSync("a", "a:t", "http://localhost:4000/", hermes_home=tmp_path)
        assert ss._relay_url == "http://localhost:4000"

    def test_allow_overwrite_default_false(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss._allow_overwrite is False

    def test_allow_overwrite_true(self, tmp_path):
        ss = _make_soul_sync(tmp_path, allow_overwrite=True)
        assert ss._allow_overwrite is True

    def test_not_syncing_initially(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss._is_syncing is False

    def test_last_sync_time_none_initially(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss._last_sync_time is None


# ---------------------------------------------------------------------------
# _get_soul_path
# ---------------------------------------------------------------------------

class TestGetSoulPath:
    def test_uses_hermes_home_when_set(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss._get_soul_path() == tmp_path / "SOUL.md"

    def test_uses_hermes_constants_when_no_home(self, monkeypatch):
        fake_home = Path("/fake/hermes")
        monkeypatch.setattr("gateway.soul_sync.SoulSync._hermes_home", None, raising=False)

        def _fake_get_hermes_home():
            return fake_home

        ss = SoulSync("a", "a:t", "http://x")
        ss._hermes_home = None
        with patch("hermes_constants.get_hermes_home", return_value=fake_home):
            assert ss._get_soul_path() == fake_home / "SOUL.md"


# ---------------------------------------------------------------------------
# update_soul_file
# ---------------------------------------------------------------------------

class TestUpdateSoulFile:
    @pytest.mark.asyncio
    async def test_creates_soul_md_when_not_exists(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        result = await ss.update_soul_file("user1", "# Persona\nBe helpful.")
        soul_path = tmp_path / "SOUL.md"
        assert result is True
        assert soul_path.exists()
        assert soul_path.read_text(encoding="utf-8") == "# Persona\nBe helpful."

    @pytest.mark.asyncio
    async def test_skips_overwrite_when_allow_overwrite_false(self, tmp_path):
        ss = _make_soul_sync(tmp_path, allow_overwrite=False)
        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text("original content", encoding="utf-8")

        result = await ss.update_soul_file("user1", "new content")

        assert result is False
        assert soul_path.read_text(encoding="utf-8") == "original content"

    @pytest.mark.asyncio
    async def test_overwrites_when_allow_overwrite_true(self, tmp_path):
        ss = _make_soul_sync(tmp_path, allow_overwrite=True)
        soul_path = tmp_path / "SOUL.md"
        soul_path.write_text("original content", encoding="utf-8")

        result = await ss.update_soul_file("user1", "new content")

        assert result is True
        assert soul_path.read_text(encoding="utf-8") == "new content"

    @pytest.mark.asyncio
    async def test_creates_parent_dirs_if_missing(self, tmp_path):
        deep_home = tmp_path / "a" / "b" / "c"
        ss = SoulSync("a", "a:t", "http://x", hermes_home=deep_home)

        result = await ss.update_soul_file("u1", "content")

        assert result is True
        assert (deep_home / "SOUL.md").exists()

    @pytest.mark.asyncio
    async def test_returns_false_on_os_error(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        soul_path = tmp_path / "SOUL.md"

        with patch.object(Path, "write_text", side_effect=OSError("permission denied")):
            result = await ss.update_soul_file("u1", "content")

        assert result is False

    @pytest.mark.asyncio
    async def test_saves_utf8_content(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        content = "# 한국어 persona\n당신은 친절한 어시스턴트입니다."
        await ss.update_soul_file("u1", content)
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# fetch_personas
# ---------------------------------------------------------------------------

class TestFetchPersonas:
    @pytest.mark.asyncio
    async def test_returns_personas_on_success(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "personas": [{"user_id": "u1", "persona": "Be helpful.", "agent_id": "testAgent"}]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            personas = await ss.fetch_personas()

        assert personas is not None
        assert len(personas) == 1
        assert personas[0]["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_sends_correct_auth_header(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"personas": []}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await ss.fetch_personas()

        call_kwargs = mock_client.get.call_args[1]
        assert call_kwargs["headers"]["Authorization"] == "Bearer testAgent:rawToken"

    @pytest.mark.asyncio
    async def test_calls_correct_endpoint(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"personas": []}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            await ss.fetch_personas()

        url = mock_client.get.call_args[0][0]
        assert url == "http://localhost:4000/api/agents/testAgent/soul"

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ss.fetch_personas()

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_personas_key(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ss.fetch_personas()

        assert result == []


# ---------------------------------------------------------------------------
# sync_personas
# ---------------------------------------------------------------------------

class TestSyncPersonas:
    @pytest.mark.asyncio
    async def test_sync_creates_soul_md(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss.fetch_personas = AsyncMock(return_value=[
            {"user_id": "u1", "persona": "You are helpful.", "agent_id": "testAgent"}
        ])

        result = await ss.sync_personas()

        assert result["ok"] is True
        assert result["synced"] == 1
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "You are helpful."

    @pytest.mark.asyncio
    async def test_sync_no_personas_returns_ok_zero(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss.fetch_personas = AsyncMock(return_value=[])

        result = await ss.sync_personas()

        assert result == {"ok": True, "synced": 0}

    @pytest.mark.asyncio
    async def test_sync_fetch_failure_returns_error(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss.fetch_personas = AsyncMock(return_value=None)

        result = await ss.sync_personas()

        assert result["ok"] is False
        assert "failed to fetch" in result["error"]

    @pytest.mark.asyncio
    async def test_sync_updates_last_sync_time(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss.fetch_personas = AsyncMock(return_value=[
            {"user_id": "u1", "persona": "content"}
        ])
        assert ss._last_sync_time is None

        await ss.sync_personas()

        assert ss._last_sync_time is not None

    @pytest.mark.asyncio
    async def test_concurrent_sync_blocked(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss._is_syncing = True

        result = await ss.sync_personas()

        assert result["ok"] is False
        assert "sync in progress" in result["error"]

    @pytest.mark.asyncio
    async def test_is_syncing_reset_after_completion(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss.fetch_personas = AsyncMock(return_value=[])

        await ss.sync_personas()

        assert ss._is_syncing is False

    @pytest.mark.asyncio
    async def test_is_syncing_reset_on_exception(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss.fetch_personas = AsyncMock(side_effect=RuntimeError("boom"))

        result = await ss.sync_personas()

        assert ss._is_syncing is False
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_skips_empty_persona_string(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        ss.fetch_personas = AsyncMock(return_value=[
            {"user_id": "u1", "persona": ""},
            {"user_id": "u2", "persona": "Real content."},
        ])

        result = await ss.sync_personas()

        assert result["synced"] == 1
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "Real content."

    @pytest.mark.asyncio
    async def test_multiple_personas_last_wins(self, tmp_path):
        ss = _make_soul_sync(tmp_path, allow_overwrite=True)
        ss.fetch_personas = AsyncMock(return_value=[
            {"user_id": "u1", "persona": "First persona."},
            {"user_id": "u2", "persona": "Second persona."},
        ])

        result = await ss.sync_personas()

        assert result["synced"] == 2
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "Second persona."


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_status_contains_soul_path(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        status = ss.get_status()
        assert str(tmp_path / "SOUL.md") in status["soul_path"]

    def test_soul_exists_false_initially(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss.get_status()["soul_exists"] is False

    def test_soul_exists_true_after_write(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        (tmp_path / "SOUL.md").write_text("content")
        assert ss.get_status()["soul_exists"] is True

    def test_mode_is_event_driven(self, tmp_path):
        ss = _make_soul_sync(tmp_path)
        assert ss.get_status()["mode"] == "event-driven"

    def test_allow_overwrite_reflected_in_status(self, tmp_path):
        ss = _make_soul_sync(tmp_path, allow_overwrite=True)
        assert ss.get_status()["allow_overwrite"] is True


# ---------------------------------------------------------------------------
# SkyTower adapter integration: SoulSync wired up
# ---------------------------------------------------------------------------

class TestSkyTowerSoulSyncIntegration:
    def _make_adapter(self, **extra):
        from gateway.config import PlatformConfig
        mod = load_plugin_adapter("skytower")
        config = PlatformConfig(
            enabled=True,
            extra={"token": "testAgent:rawToken", "url": "http://localhost:4000", **extra},
        )
        return mod.SkyTowerAdapter(config)

    def test_soul_sync_initialized_in_adapter(self):
        adapter = self._make_adapter()
        from gateway.soul_sync import SoulSync
        assert isinstance(adapter._soul_sync, SoulSync)

    def test_soul_sync_uses_adapter_agent_id(self):
        adapter = self._make_adapter()
        assert adapter._soul_sync._agent_id == "testAgent"

    def test_soul_sync_allow_overwrite_from_env(self, monkeypatch):
        monkeypatch.setenv("SOUL_ALLOW_OVERWRITE", "true")
        adapter = self._make_adapter()
        assert adapter._soul_sync._allow_overwrite is True

    def test_soul_sync_allow_overwrite_default_false(self, monkeypatch):
        monkeypatch.delenv("SOUL_ALLOW_OVERWRITE", raising=False)
        adapter = self._make_adapter()
        assert adapter._soul_sync._allow_overwrite is False

    @pytest.mark.asyncio
    async def test_handle_soul_sync_request_calls_sync_and_emits(self):
        adapter = self._make_adapter()
        adapter._soul_sync.sync_personas = AsyncMock(return_value={"ok": True, "synced": 1})

        mock_sio = MagicMock()
        mock_sio.connected = True
        mock_sio.emit = AsyncMock()
        adapter._sio = mock_sio

        await adapter._handle_soul_sync_request({})

        adapter._soul_sync.sync_personas.assert_awaited_once()
        mock_sio.emit.assert_awaited_once_with("agent:sync-result", {"ok": True, "synced": 1})

    @pytest.mark.asyncio
    async def test_handle_soul_sync_request_no_emit_when_disconnected(self):
        adapter = self._make_adapter()
        adapter._soul_sync.sync_personas = AsyncMock(return_value={"ok": True, "synced": 0})

        mock_sio = MagicMock()
        mock_sio.connected = False
        mock_sio.emit = AsyncMock()
        adapter._sio = mock_sio

        await adapter._handle_soul_sync_request({})

        mock_sio.emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_soul_sync_request_no_emit_when_sio_none(self):
        adapter = self._make_adapter()
        adapter._soul_sync.sync_personas = AsyncMock(return_value={"ok": True, "synced": 0})
        adapter._sio = None

        await adapter._handle_soul_sync_request({})

        adapter._soul_sync.sync_personas.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_soul_persona_updated_calls_update(self):
        adapter = self._make_adapter()
        adapter._soul_sync.update_soul_file = AsyncMock(return_value=True)

        await adapter._handle_soul_persona_updated({
            "userId": "user42",
            "persona": "You are a coding expert.",
        })

        adapter._soul_sync.update_soul_file.assert_awaited_once_with(
            "user42", "You are a coding expert."
        )

    @pytest.mark.asyncio
    async def test_handle_soul_persona_updated_skips_empty_persona(self):
        adapter = self._make_adapter()
        adapter._soul_sync.update_soul_file = AsyncMock(return_value=False)

        await adapter._handle_soul_persona_updated({"userId": "u1", "persona": ""})

        adapter._soul_sync.update_soul_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handle_soul_persona_updated_default_user_id(self):
        adapter = self._make_adapter()
        adapter._soul_sync.update_soul_file = AsyncMock(return_value=True)

        await adapter._handle_soul_persona_updated({"persona": "content"})

        adapter._soul_sync.update_soul_file.assert_awaited_once_with("unknown", "content")
