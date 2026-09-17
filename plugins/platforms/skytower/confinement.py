"""프로젝트 작업 폴더 confinement (skytower workspace-projects).

공유 프로젝트 방에서 relay 가 ``project.workdir`` 를 실어 보내면, 그 세션의 도구 호출을
그 폴더 안으로 제한한다. hermes 의 ``terminal.cwd`` 는 게이트웨이 전역이라 세션 단위로
강제할 수 없으므로 플러그인 훅으로 한다.

  1. 어댑터가 메시지를 받을 때 세션 키 → 작업 폴더를 기억한다 (``remember``).
  2. ``pre_gateway_dispatch`` 훅에서 세션 스토어 핸들을 붙잡고, 세션 키 → 세션 id 를
     미리 매핑한다 (기존 세션). 새 세션은 첫 도구 호출 때 역으로 찾는다.
  3. ``pre_tool_call`` 훅에서 파일·터미널·코드 실행 도구의 경로를 검사해 폴더 밖이면
     막고, 세션 cwd 를 작업 폴더로 고정한다.

한계: 터미널 명령과 코드는 정적 검사다. 절대 경로·``~``·``..`` 로 나가는 토큰은 잡지만
``$(pwd)/..`` 같은 동적 경로는 못 잡는다. 진짜 격리는 docker 백엔드로 작업 폴더만 마운트하는
것이고, 이 훅은 그 전까지의 안전망이다.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 세션 키(``agent:main:skytower:group:...``) → 작업 폴더. 어댑터가 채운다.
_workdir_by_session_key: Dict[str, str] = {}
# 세션 id(agent.session_id / task_id) → 작업 폴더. 훅이 채운다.
_workdir_by_session_id: Dict[str, str] = {}
# 도구 호출 시 세션 id 로 세션 키를 찾기 위한 스토어 핸들 (pre_gateway_dispatch 가 준다).
_session_store: Any = None

# 경로 인자를 가진 파일 도구 → 인자 이름
_PATH_TOOLS = {
    "read_file": "path", "write_file": "path", "patch": "path", "search_files": "path",
    "list_dir": "path", "delete_file": "path",
}
_WRITE_TOOLS = {"write_file", "patch", "delete_file"}

_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")
_MNT_DRIVE_RE = re.compile(r"^/mnt/([a-z])(/|$)")
# 명령·코드 안에서 경로처럼 보이는 토큰
# ``:`` 뒤(URL 의 ``https://``, ``host:/path``)와 단어 뒤(``a/b``, 날짜 ``2026/09``)는 경로로 안 본다.
_PATH_TOKEN_RE = re.compile(
    r"""(?<![\w@:/])((?:[A-Za-z]:[\\/]|/(?!/)|~(?=[/\s"']|$)|\.\.?/)[^\s"'`;|&<>()]*)"""
)


# ── 경로 정규화 ────────────────────────────────────────────────────────────

def canonical(path: str) -> str:
    """OS·표기 차이를 지운 비교용 형태. ``D:\\work\\Acme`` 와 ``/mnt/d/work/acme`` 가 같아진다.

    WSL 에이전트가 Windows 경로로 적힌 작업 폴더를 받는 경우가 흔해서(관리자는 Windows 에서
    설정한다) 드라이브 문자 표기는 ``/mnt/<x>/`` 로 통일한다. 대소문자는 무시한다.
    """
    p = (path or "").strip().replace("\\", "/")
    m = _WIN_DRIVE_RE.match(p)
    if m:
        p = f"/mnt/{m.group(1).lower()}/{p[3:]}"
    p = re.sub(r"/{2,}", "/", p)
    # ``..`` / ``.`` 접기 (절대 경로 기준)
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/" + "/".join(parts).lower() if p.startswith("/") else "/".join(parts).lower()


def within(workdir: str, path: str) -> bool:
    """``path`` 가 ``workdir`` 안(같은 폴더 포함)인가. 둘 다 canonical 로 비교."""
    base = canonical(workdir)
    target = canonical(path)
    if not base:
        return True
    return target == base or target.startswith(base + "/")


def to_local_path(workdir: str) -> str:
    """이 호스트에서 실제로 쓸 수 있는 작업 폴더 경로. WSL/리눅스에서 Windows 표기를 받으면
    ``/mnt/<x>/...`` 로, Windows 에서 ``/mnt/<x>/`` 를 받으면 드라이브 표기로 바꾼다."""
    p = (workdir or "").strip()
    if not p:
        return p
    if os.name != "nt":
        m = _WIN_DRIVE_RE.match(p)
        if m:
            rest = p[3:].replace("\\", "/")
            return f"/mnt/{m.group(1).lower()}/{rest}"
        return p.replace("\\", "/")
    m = _MNT_DRIVE_RE.match(p.replace("\\", "/"))
    if m:
        rest = p.replace("\\", "/")[len(m.group(0)):]
        return f"{m.group(1).upper()}:/{rest}"
    return p


def _resolve_against(cwd: Optional[str], path: str) -> str:
    """상대 경로를 세션 cwd(없으면 그대로)에 붙인다. ``~`` 는 홈으로."""
    p = (path or "").strip()
    if not p:
        return p
    if p.startswith("~"):
        return os.path.expanduser(p)
    if _WIN_DRIVE_RE.match(p) or p.startswith("/"):
        return p
    if cwd:
        return f"{cwd.rstrip('/').rstrip(chr(92))}/{p}"
    return p


# ── 세션 ↔ 작업 폴더 ─────────────────────────────────────────────────────

def remember(session_key: str, workdir: Optional[str]) -> None:
    """어댑터가 메시지마다 부른다. 공유 방이 아니거나 폴더가 없으면 지운다."""
    if not session_key:
        return
    if workdir and workdir.strip():
        _workdir_by_session_key[session_key] = workdir.strip()
    else:
        _workdir_by_session_key.pop(session_key, None)


def workdir_for(session_id: str = "", task_id: str = "") -> Optional[str]:
    """도구 호출 시점의 세션 id/task id 로 작업 폴더를 찾는다."""
    for sid in (session_id, task_id):
        if sid and sid in _workdir_by_session_id:
            return _workdir_by_session_id[sid]
    store = _session_store
    if store is None:
        return None
    for sid in (session_id, task_id):
        if not sid:
            continue
        try:
            entry = store.lookup_by_session_id(sid)
        except Exception:  # pragma: no cover - 스토어 구현 차이
            entry = None
        key = getattr(entry, "session_key", None)
        if key and key in _workdir_by_session_key:
            wd = _workdir_by_session_key[key]
            _workdir_by_session_id[sid] = wd
            return wd
        if key:
            _workdir_by_session_id.pop(sid, None)
    return None


def _pin_cwd(task_id: str, workdir: str) -> None:
    """세션의 터미널 cwd 를 작업 폴더로. 이미 안에 있으면 그대로 둔다."""
    try:
        from tools.terminal_tool import get_session_cwd, register_task_env_overrides
    except Exception:  # pragma: no cover
        return
    local = to_local_path(workdir)
    current = get_session_cwd(task_id)
    if current and within(workdir, current):
        return
    if os.path.isdir(local):
        register_task_env_overrides(task_id, {"cwd": local})
        logger.info("skytower confinement: cwd pinned task=%s cwd=%s", task_id, local)
    else:
        logger.warning("skytower confinement: workdir %s not found on this host (cwd not pinned)", local)


def _session_cwd(task_id: str, workdir: str) -> str:
    try:
        from tools.terminal_tool import get_session_cwd
        return get_session_cwd(task_id) or to_local_path(workdir)
    except Exception:  # pragma: no cover
        return to_local_path(workdir)


# ── 훅 ────────────────────────────────────────────────────────────────────

def on_pre_gateway_dispatch(event: Any = None, gateway: Any = None, session_store: Any = None, **_: Any) -> None:
    """세션 스토어를 붙잡고, 이 이벤트의 세션 키 → 세션 id 를 미리 매핑한다."""
    global _session_store
    if session_store is not None:
        _session_store = session_store
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    if platform != "skytower" or gateway is None:
        return None
    try:
        from gateway.config import Platform
        adapter = getattr(gateway, "adapters", {}).get(Platform("skytower"))
        key = adapter._event_session_key(event) if adapter else None
        sid = session_store.peek_session_id(key) if (session_store and key) else None
    except Exception as exc:  # pragma: no cover - 방어
        logger.debug("skytower confinement: pre-map failed: %s", exc)
        return None
    if not sid:
        return None
    wd = _workdir_by_session_key.get(key)
    if wd:
        _workdir_by_session_id[sid] = wd
    else:
        _workdir_by_session_id.pop(sid, None)
    return None


def _deny(workdir: str, what: str) -> Dict[str, str]:
    return {
        "action": "block",
        "message": (
            f"이 프로젝트에서는 작업 폴더 `{workdir}` 밖에 접근할 수 없습니다: {what}. "
            f"작업 폴더 안의 경로만 사용하세요. (This project is confined to `{workdir}`.)"
        ),
    }


def _offending_tokens(text: str, cwd: str, workdir: str) -> list[str]:
    """명령·코드 안에서 작업 폴더 밖을 가리키는 경로 토큰."""
    bad: list[str] = []
    for m in _PATH_TOKEN_RE.finditer(text or ""):
        tok = m.group(1).rstrip(".,:")
        if not tok or tok in ("/", "./", "../"):
            if tok == "../" and not within(workdir, _resolve_against(cwd, "..")):
                bad.append(tok)
            continue
        resolved = _resolve_against(cwd, tok)
        if not within(workdir, resolved):
            bad.append(tok)
    return bad


def check_tool_call(tool_name: str, args: Optional[Dict[str, Any]], *, workdir: str, cwd: str) -> Optional[Dict[str, str]]:
    """도구 하나의 인자를 검사한다. 막아야 하면 block 딕셔너리, 아니면 None."""
    args = args or {}
    if tool_name in _PATH_TOOLS:
        raw = args.get(_PATH_TOOLS[tool_name])
        path = raw if isinstance(raw, str) and raw.strip() else "."
        resolved = _resolve_against(cwd, path)
        if not within(workdir, resolved):
            return _deny(workdir, f"{tool_name} {path}")
        return None
    if tool_name == "terminal":
        wd = args.get("workdir")
        if isinstance(wd, str) and wd.strip() and not within(workdir, _resolve_against(cwd, wd)):
            return _deny(workdir, f"workdir {wd}")
        cmd = args.get("command")
        if isinstance(cmd, str):
            bad = _offending_tokens(cmd, cwd, workdir)
            if bad:
                return _deny(workdir, "명령의 경로 " + ", ".join(bad[:3]))
        return None
    if tool_name == "execute_code":
        code = args.get("code")
        if isinstance(code, str):
            bad = _offending_tokens(code, cwd, workdir)
            if bad:
                return _deny(workdir, "코드의 경로 " + ", ".join(bad[:3]))
        return None
    return None


def on_pre_tool_call(tool_name: str = "", args: Any = None, task_id: str = "", session_id: str = "", **_: Any) -> Optional[Dict[str, str]]:
    """confinement 가 걸린 세션이면 경로를 검사하고 cwd 를 고정한다."""
    workdir = workdir_for(session_id=session_id, task_id=task_id)
    if not workdir:
        return None
    tid = task_id or session_id
    _pin_cwd(tid, workdir)
    verdict = check_tool_call(tool_name, args if isinstance(args, dict) else None, workdir=workdir, cwd=_session_cwd(tid, workdir))
    if verdict:
        logger.info("skytower confinement: blocked %s task=%s (%s)", tool_name, tid, verdict["message"][:80])
    return verdict


def register_hooks(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
