"""Tests for FileAccessHandler — file:write and file:delete events."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _make_handler(emitted: list):
    """FileAccessHandler with a mock sio that records emitted events."""
    from gateway.platforms.skytower_files import FileAccessHandler

    sio = MagicMock()
    sio.connected = True

    async def _fake_emit(event, data):
        emitted.append((event, data))

    handler = FileAccessHandler(sio)
    handler._emit = _fake_emit
    return handler


# ---------------------------------------------------------------------------
# file:write
# ---------------------------------------------------------------------------

class TestHandleWrite:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)
        target = tmp_path / "test.md"

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_write({
                "request_id": "r1",
                "path": str(target),
                "content": "# Hello\nworld",
            })

        assert target.read_text(encoding="utf-8") == "# Hello\nworld"
        assert emitted == [("file:write_result", {"request_id": "r1", "success": True})]

    @pytest.mark.asyncio
    async def test_write_creates_parent_dirs(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)
        target = tmp_path / "a" / "b" / "c.md"

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_write({
                "request_id": "r2",
                "path": str(target),
                "content": "content",
            })

        assert target.exists()
        assert emitted[0] == ("file:write_result", {"request_id": "r2", "success": True})

    @pytest.mark.asyncio
    async def test_write_overwrites_existing_file(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)
        target = tmp_path / "existing.md"
        target.write_text("old content", encoding="utf-8")

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_write({
                "request_id": "r3",
                "path": str(target),
                "content": "new content",
            })

        assert target.read_text(encoding="utf-8") == "new content"
        assert emitted[0][1]["success"] is True

    @pytest.mark.asyncio
    async def test_write_error_on_empty_path(self):
        emitted = []
        handler = _make_handler(emitted)

        await handler.handle_write({"request_id": "r4", "path": "", "content": "x"})

        assert emitted[0] == ("file:write_result", {"request_id": "r4", "error": "path is required"})

    @pytest.mark.asyncio
    async def test_write_error_when_denied(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=True):
            await handler.handle_write({
                "request_id": "r5",
                "path": str(tmp_path / "denied.md"),
                "content": "x",
            })

        assert "error" in emitted[0][1]
        assert "denied" in emitted[0][1]["error"].lower()

    @pytest.mark.asyncio
    async def test_write_error_when_too_large(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)

        from gateway.platforms.skytower_files import _MAX_WRITE_CHARS
        huge = "x" * (_MAX_WRITE_CHARS + 1)

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_write({
                "request_id": "r6",
                "path": str(tmp_path / "huge.md"),
                "content": huge,
            })

        assert "error" in emitted[0][1]
        assert "large" in emitted[0][1]["error"].lower()

    @pytest.mark.asyncio
    async def test_write_error_on_non_string_content(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_write({
                "request_id": "r7",
                "path": str(tmp_path / "x.md"),
                "content": 12345,
            })

        assert "error" in emitted[0][1]

    @pytest.mark.asyncio
    async def test_write_request_id_echoed(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_write({
                "request_id": "unique-id-xyz",
                "path": str(tmp_path / "f.md"),
                "content": "ok",
            })

        assert emitted[0][1]["request_id"] == "unique-id-xyz"


# ---------------------------------------------------------------------------
# file:delete (existing handler — smoke tests)
# ---------------------------------------------------------------------------

class TestHandleDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_file(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)
        target = tmp_path / "del.md"
        target.write_text("bye")

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_delete({"request_id": "d1", "path": str(target)})

        assert not target.exists()
        assert emitted[0] == ("file:delete_result", {"request_id": "d1", "ok": True})

    @pytest.mark.asyncio
    async def test_delete_error_on_missing_file(self, tmp_path):
        emitted = []
        handler = _make_handler(emitted)

        with patch("gateway.platforms.skytower_files.is_write_denied", return_value=False):
            await handler.handle_delete({
                "request_id": "d2",
                "path": str(tmp_path / "ghost.md"),
            })

        assert "error" in emitted[0][1]

    @pytest.mark.asyncio
    async def test_delete_error_on_empty_path(self):
        emitted = []
        handler = _make_handler(emitted)

        await handler.handle_delete({"request_id": "d3", "path": ""})

        assert emitted[0] == ("file:delete_result", {"request_id": "d3", "error": "path is required"})
