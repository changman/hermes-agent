"""프로젝트 작업 폴더 confinement (plugins/platforms/skytower/confinement.py)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from plugins.platforms.skytower import confinement as c


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setattr(c, "_workdir_by_session_key", {})
    monkeypatch.setattr(c, "_workdir_by_session_id", {})
    monkeypatch.setattr(c, "_session_store", None)
    # cwd 고정은 실제 폴더가 있어야 하므로 테스트에서는 끈다
    monkeypatch.setattr(c, "_pin_cwd", lambda task_id, workdir: None)


class TestPaths:
    def test_canonical_unifies_windows_and_wsl(self):
        assert c.canonical("D:\\work\\Acme") == "/mnt/d/work/acme"
        assert c.canonical("/mnt/d/work/acme/") == "/mnt/d/work/acme"
        assert c.canonical("d:/work//acme/src/../src") == "/mnt/d/work/acme/src"

    def test_within(self):
        assert c.within("D:/work/acme", "/mnt/d/work/acme/src/a.ts")
        assert c.within("D:/work/acme", "D:\\work\\ACME")
        assert not c.within("D:/work/acme", "D:/work/acme-secret/x")
        assert not c.within("/home/vanilla/proj", "/home/vanilla")
        assert not c.within("/home/vanilla/proj", "/home/vanilla/proj/../other")

    def test_to_local_path(self, monkeypatch):
        monkeypatch.setattr(c.os, "name", "posix")
        assert c.to_local_path("D:\\work\\acme") == "/mnt/d/work/acme"
        assert c.to_local_path("/home/v/proj") == "/home/v/proj"
        monkeypatch.setattr(c.os, "name", "nt")
        assert c.to_local_path("/mnt/d/work/acme") == "D:/work/acme"


class TestCheckToolCall:
    WD = "/home/vanilla/proj"
    CWD = "/home/vanilla/proj"

    def check(self, tool, args, cwd=None):
        return c.check_tool_call(tool, args, workdir=self.WD, cwd=cwd or self.CWD)

    def test_file_tools_inside_pass(self):
        assert self.check("read_file", {"path": "src/a.py"}) is None
        assert self.check("write_file", {"path": "/home/vanilla/proj/README.md", "content": "x"}) is None
        assert self.check("search_files", {"pattern": "TODO"}) is None  # 기본값 "."

    def test_file_tools_outside_blocked(self):
        v = self.check("read_file", {"path": "/home/vanilla/.ssh/id_rsa"})
        assert v and v["action"] == "block" and "proj" in v["message"]
        assert self.check("read_file", {"path": "../other/x"})["action"] == "block"
        assert self.check("read_file", {"path": "~/secret"})["action"] == "block"
        assert self.check("search_files", {"pattern": "x", "path": "/etc"})["action"] == "block"

    def test_relative_paths_resolve_against_session_cwd(self):
        # cd src 한 뒤 ../../ 는 프로젝트 밖
        assert self.check("read_file", {"path": "../a.py"}, cwd="/home/vanilla/proj/src") is None
        assert self.check("read_file", {"path": "../../a.py"}, cwd="/home/vanilla/proj/src")["action"] == "block"

    def test_terminal(self):
        assert self.check("terminal", {"command": "ls -la src && git status"}) is None
        assert self.check("terminal", {"command": "cat ./src/a.py"}) is None
        assert self.check("terminal", {"command": "cat /home/vanilla/.bashrc"})["action"] == "block"
        assert self.check("terminal", {"command": "ls /mnt/c/Users"})["action"] == "block"
        assert self.check("terminal", {"command": "cd ~ && ls"})["action"] == "block"
        assert self.check("terminal", {"command": "cd ../.. && ls"})["action"] == "block"
        assert self.check("terminal", {"command": "pwd", "workdir": "/tmp"})["action"] == "block"
        assert self.check("terminal", {"command": "pwd", "workdir": "/home/vanilla/proj/src"}) is None

    def test_terminal_ignores_non_path_tokens(self):
        # URL, 옵션, 날짜 표기는 경로가 아니다
        assert self.check("terminal", {"command": "curl https://example.com/a/b -o out.json"}) is None
        assert self.check("terminal", {"command": "git log --since=2026/09/01"}) is None
        assert self.check("terminal", {"command": "echo user@host:/etc/passwd"}) is None

    def test_execute_code(self):
        assert self.check("execute_code", {"code": "open('data/x.csv').read()"}) is None
        assert self.check("execute_code", {"code": "open('/etc/passwd').read()"})["action"] == "block"
        assert self.check("execute_code", {"code": "open('C:/Users/me/x').read()"})["action"] == "block"

    def test_unknown_tools_pass(self):
        assert self.check("web_search", {"query": "/etc/passwd"}) is None

    def test_windows_workdir_with_wsl_paths(self):
        # 관리자는 Windows 표기로 설정하고 에이전트는 WSL 에서 돈다
        v = c.check_tool_call("read_file", {"path": "/mnt/d/work/acme/src/a.ts"}, workdir="D:\\work\\acme", cwd="/mnt/d/work/acme")
        assert v is None
        v = c.check_tool_call("read_file", {"path": "/mnt/d/work/other"}, workdir="D:\\work\\acme", cwd="/mnt/d/work/acme")
        assert v["action"] == "block"


class TestSessionMapping:
    def test_remember_and_clear(self):
        c.remember("k1", "D:/work/acme")
        assert c._workdir_by_session_key["k1"] == "D:/work/acme"
        c.remember("k1", None)
        assert "k1" not in c._workdir_by_session_key

    def test_pre_dispatch_maps_existing_session(self):
        c.remember("agent:main:skytower:group:skytower:a:conv:40", "D:/work/acme")
        adapter = MagicMock()
        adapter._event_session_key.return_value = "agent:main:skytower:group:skytower:a:conv:40"
        from gateway.config import Platform
        gateway = MagicMock(); gateway.adapters = {Platform("skytower"): adapter}
        store = MagicMock(); store.peek_session_id.return_value = "sid-1"
        event = MagicMock(); event.source.platform = Platform("skytower")
        c.on_pre_gateway_dispatch(event=event, gateway=gateway, session_store=store)
        assert c._workdir_by_session_id["sid-1"] == "D:/work/acme"
        assert c._session_store is store

    def test_tool_call_resolves_via_store_for_new_session(self):
        c.remember("key-x", "/home/v/proj")
        entry = MagicMock(); entry.session_key = "key-x"
        store = MagicMock(); store.lookup_by_session_id.return_value = entry
        c._session_store = store
        v = c.on_pre_tool_call(tool_name="read_file", args={"path": "/etc/hosts"}, task_id="sid-new", session_id="sid-new")
        assert v and v["action"] == "block"
        # 매핑이 캐시된다
        assert c._workdir_by_session_id["sid-new"] == "/home/v/proj"
        assert c.on_pre_tool_call(tool_name="read_file", args={"path": "a.py"}, task_id="sid-new") is None

    def test_unconfined_session_untouched(self):
        store = MagicMock(); store.lookup_by_session_id.return_value = None
        c._session_store = store
        assert c.on_pre_tool_call(tool_name="read_file", args={"path": "/etc/hosts"}, task_id="other") is None


class TestAdapterIntegration:
    @pytest.mark.asyncio
    async def test_shared_message_records_workdir_by_session_key(self):
        from tests.gateway.test_skytower import _make_adapter
        adapter = _make_adapter()
        adapter.handle_message = AsyncMock()
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text", "content": "@Hermes 정리", "user_id": 7,
            "conversation_id": 40, "id": 1, "shared": True, "mentioned": True,
            "project": {"id": 1, "name": "Acme", "workdir": "D:/work/acme"}, "participants": [],
        })
        key = adapter._source_session_key(adapter.handle_message.call_args[0][0].source)
        assert c._workdir_by_session_key[key] == "D:/work/acme"
        # 개인 방 메시지는 제한을 걸지 않는다
        await adapter._handle_relay_message({
            "direction": "outbound", "type": "text", "content": "hi", "user_id": 7, "conversation_id": 3, "id": 2,
        })
        key2 = adapter._source_session_key(adapter.handle_message.call_args[0][0].source)
        assert key2 not in c._workdir_by_session_key
