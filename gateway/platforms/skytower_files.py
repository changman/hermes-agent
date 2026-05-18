"""Skytower 파일시스템 접근 핸들러.

Web UI가 Socket.IO를 통해 에이전트 로컬 파일시스템에 접근할 수 있도록
file:* 이벤트를 처리합니다.

지원 이벤트 (Relay → Agent):
  file:list          — 디렉토리 목록
  file:read          — 파일 내용 읽기 (텍스트 or base64)
  file:download      — 대용량 파일 청크 스트리밍 다운로드
  file:upload_start  — 업로드 세션 시작
  file:upload_chunk  — 업로드 청크 수신 (마지막 청크에서 파일 저장)
  file:delete        — 파일/디렉토리 삭제

지원 이벤트 (Agent → Relay):
  file:list_result
  file:read_result
  file:chunk                — 다운로드 청크 스트리밍
  file:download_error
  file:upload_start_result
  file:upload_chunk_result
  file:delete_result

보안:
  - agent/file_safety.py 의 is_write_denied() / get_read_block_error() 재사용
  - .ssh, .aws, .gnupg 등 민감 디렉토리 읽기 차단
  - Path.resolve() 로 symlink / ../ traversal 방지
  - 업로드 최대 크기 제한 (_MAX_UPLOAD_BYTES)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from agent.file_safety import get_read_block_error, is_write_denied

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 상수
# ---------------------------------------------------------------------------

_MAX_READ_CHARS  = 100_000          # 텍스트 파일 최대 반환 크기 (chars)
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 업로드 한 파일 최대 크기 (100 MB)
_CHUNK_SIZE       = 256 * 1024         # 다운로드 청크 크기 (256 KB)

# 읽기/목록 조회가 차단되는 민감 경로 접두어
_BLOCKED_READ_PREFIXES: tuple[str, ...] = (
    str(Path.home() / ".ssh"),
    str(Path.home() / ".aws"),
    str(Path.home() / ".gnupg"),
    str(Path.home() / ".kube"),
    str(Path.home() / ".docker"),
    str(Path.home() / ".azure"),
    str(Path.home() / ".config" / "gh"),
    "/etc/sudoers.d",
    "/etc/systemd",
)


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

def _resolve(raw: str) -> Path:
    """~ 확장 후 symlink / ../ 컴포넌트를 해소한 절대 경로를 반환합니다."""
    return Path(raw).expanduser().resolve()


def _get_wiki_root() -> Path:
    """Wiki 루트 디렉토리를 반환합니다.

    WIKI_PATH 환경변수(llm-wiki 스킬 표준) → ~/wiki 순으로 폴백합니다.
    """
    val = os.getenv("WIKI_PATH", "").strip()
    if val:
        try:
            p = Path(val).expanduser().resolve()
            if p.exists() and p.is_dir():
                return p
        except Exception:
            pass
    return (Path.home() / "wiki").resolve()


def _find_wiki_file(name: str) -> Optional[Path]:
    """Wiki 루트에서 파일명(확장자 제외)으로 .md 파일을 찾습니다.

    Shortest Path (A방식): 동일 이름 파일이 여러 개일 때 wiki 루트 기준
    경로 depth가 가장 작은 파일(루트에 가장 가까운 파일)을 반환합니다.

    Args:
        name: 확장자를 제외한 파일명 (예: 'multimodal-memory-system')

    Returns:
        찾은 파일의 절대 경로, 없으면 None
    """
    wiki_root = _get_wiki_root()
    if not wiki_root.exists():
        logger.debug("_find_wiki_file: wiki root does not exist — %s", wiki_root)
        return None

    candidates: list[tuple[int, Path]] = []
    for p in wiki_root.rglob("*.md"):
        if p.stem == name:
            try:
                depth = len(p.relative_to(wiki_root).parts)
                candidates.append((depth, p))
            except ValueError:
                pass

    if not candidates:
        logger.debug("_find_wiki_file: no match for %r in %s", name, wiki_root)
        return None

    candidates.sort(key=lambda x: x[0])
    found = candidates[0][1]
    logger.debug("_find_wiki_file: %r → %s (depth=%d, %d candidates)",
                 name, found, candidates[0][0], len(candidates))
    return found


def _session_cwd(conv_id: Optional[str] = None) -> Path:
    """세션의 기본 작업 디렉토리를 반환합니다.

    우선순위:
      1. TERMINAL_CWD 환경변수
      2. HERMES_HOME 환경변수
      3. 홈 디렉토리 (항상 존재)

    모든 대화가 동일한 HERMES_HOME을 공유하므로 conv_id 기반 격리 경로는
    사용하지 않습니다.
    """
    for env_key in ("TERMINAL_CWD", "HERMES_HOME"):
        val = os.getenv(env_key, "")
        if val:
            try:
                p = Path(val).expanduser().resolve()
                if p.exists() and p.is_dir():
                    return p
            except Exception:
                pass

    return Path.home().resolve()


def _is_read_blocked(resolved: Path) -> Optional[str]:
    """읽기/목록 조회가 차단되어야 하면 오류 문자열을, 허용이면 None을 반환합니다."""
    err = get_read_block_error(str(resolved))
    if err:
        return err

    s = str(resolved)
    for prefix in _BLOCKED_READ_PREFIXES:
        real_prefix = str(Path(prefix).resolve())
        if s == real_prefix or s.startswith(real_prefix + os.sep):
            return f"Access denied: {resolved} is a sensitive path"

    return None


def _entry_info(p: Path) -> Dict[str, Any]:
    """단일 파일/디렉토리 항목 정보를 딕셔너리로 반환합니다.

    - path: Web UI가 절대 경로로 네비게이션할 수 있도록 full path 포함
    - size: 디렉토리는 키를 제외 (null/0 대신 공백 표시를 위해)
    """
    is_dir = p.is_dir()
    try:
        st = p.stat()
        entry: Dict[str, Any] = {
            "name":  p.name,
            "path":  str(p),
            "type":  "dir" if is_dir else "file",
            "mtime": st.st_mtime,
        }
        if not is_dir:
            entry["size"] = st.st_size
        return entry
    except OSError:
        return {
            "name": p.name,
            "path": str(p),
            "type": "dir" if is_dir else "unknown",
        }


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# FileAccessHandler
# ---------------------------------------------------------------------------

class FileAccessHandler:
    """Skytower 어댑터에서 file:* Socket.IO 이벤트를 처리하는 핸들러."""

    def __init__(self, sio: Any) -> None:
        self._sio = sio
        # transfer_id → { path, chunks: {index: bytes}, total_size, started }
        self._upload_state: Dict[str, Dict[str, Any]] = {}

    # ── 공통 emit 헬퍼 ────────────────────────────────────────────────────────

    async def _emit(self, event: str, data: dict) -> None:
        if self._sio and self._sio.connected:
            await self._sio.emit(event, data)

    # ── file:roots ────────────────────────────────────────────────────────────

    async def handle_roots(self, data: dict) -> None:
        """
        Web UI가 탐색을 시작할 기준 디렉토리 목록을 반환합니다.
        하드코딩된 경로(/workspace 등) 대신 이 이벤트로 실제 경로를 조회하세요.

        data: { request_id: str }
        emit: file:roots_result {
                request_id,
                home: str,       — 사용자 홈 디렉토리 (항상 존재)
                cwd:  str,       — 에이전트 현재 작업 디렉토리
                roots: [{ path, label }]  — Web UI 사이드바용 루트 목록
              }
        """
        request_id = data.get("request_id", "")
        conv_id    = data.get("conv_id")
        logger.debug("file:roots received — request_id=%s conv_id=%s", request_id, conv_id)

        session_cwd = str(_session_cwd(conv_id))
        home        = str(Path.home().resolve())

        # 세션 작업 디렉토리를 최상단에 배치
        roots: list = [{"path": session_cwd, "label": "Working Directory"}]
        if home != session_cwd:
            roots.append({"path": home, "label": "Home"})

        # HERMES_WRITE_SAFE_ROOT 가 설정된 경우 Workspace 항목 추가
        safe_root = os.getenv("HERMES_WRITE_SAFE_ROOT", "")
        if safe_root:
            try:
                resolved_root = str(Path(safe_root).expanduser().resolve())
                if resolved_root not in (session_cwd, home) and Path(resolved_root).exists():
                    roots.append({"path": resolved_root, "label": "Workspace"})
            except Exception:
                pass

        await self._emit("file:roots_result", {
            "request_id":  request_id,
            "home":        home,
            "cwd":         session_cwd,
            "roots":       roots,
        })

    # ── file:list ─────────────────────────────────────────────────────────────

    async def handle_list(self, data: dict) -> None:
        """
        data: { request_id: str, path: str }
        emit: file:list_result { request_id, path, entries } | { request_id, error }
        """
        request_id = data.get("request_id", "")
        raw_path   = data.get("path", "~")
        conv_id    = data.get("conv_id")
        logger.debug("file:list received — request_id=%s path=%s conv_id=%s",
                     request_id, raw_path, conv_id)

        async def _err(msg: str) -> None:
            await self._emit("file:list_result", {"request_id": request_id, "error": msg})

        try:
            resolved = _resolve(raw_path)
        except Exception as e:
            await _err(f"Invalid path: {e}")
            return

        if blocked := _is_read_blocked(resolved):
            await _err(blocked)
            return

        session_root = _session_cwd(conv_id)

        if not resolved.exists():
            # 요청 경로가 없으면 세션 작업 디렉토리로 폴백 (예: /workspace → conv_home)
            logger.info(
                "file:list path not found (%s), falling back to session root: %s",
                resolved, session_root,
            )
            resolved = session_root

        # 루트 경계 강제: 세션 작업 디렉토리 상위로 이동 불가
        try:
            resolved.relative_to(session_root)
        except ValueError:
            logger.info(
                "file:list path above session root (%s), clamping to: %s",
                resolved, session_root,
            )
            resolved = session_root

        if not resolved.is_dir():
            await _err(f"Not a directory: {resolved}")
            return

        try:
            entries = [_entry_info(p) for p in sorted(resolved.iterdir())]
        except PermissionError as e:
            await _err(f"Permission denied: {e}")
            return

        # 루트가 아닌 경우 상위 이동 엔트리 "[..]" 추가
        if resolved != session_root:
            parent = resolved.parent
            # 부모가 세션 루트보다 상위면 루트로 클램핑
            try:
                parent.relative_to(session_root)
            except ValueError:
                parent = session_root
            entries.insert(0, {
                "name": "..",
                "path": str(parent),
                "type": "dir",
            })

        await self._emit("file:list_result", {
            "request_id": request_id,
            "path":       str(resolved),
            "root":       str(session_root),
            "entries":    entries,
        })

    # ── file:read ─────────────────────────────────────────────────────────────

    async def handle_read(self, data: dict) -> None:
        """
        data: { request_id: str, path: str }
              | { request_id: str, type: 'filename', name: str }  — wiki Shortest Path 검색
        emit: file:read_result { request_id, path, content|base64_content, ... }
              | { request_id, error }
        """
        request_id = data.get("request_id", "")
        raw_path   = data.get("path", "")
        logger.debug("file:read received — request_id=%s path=%s type=%s",
                     request_id, raw_path, data.get("type"))

        async def _err(msg: str) -> None:
            await self._emit("file:read_result", {"request_id": request_id, "error": msg})

        # 파일명 기반 Wiki 검색 (type='filename')
        if data.get("type") == "filename":
            name = (data.get("name") or "").strip()
            if not name:
                await _err("name is required for type=filename")
                return
            found = _find_wiki_file(name)
            if found is None:
                await _err(f"Wiki file not found: {name!r} (wiki root: {_get_wiki_root()})")
                return
            raw_path = str(found)

        if not raw_path:
            await _err("path is required")
            return

        try:
            resolved = _resolve(raw_path)
        except Exception as e:
            await _err(f"Invalid path: {e}")
            return

        if blocked := _is_read_blocked(resolved):
            await _err(blocked)
            return

        if not resolved.exists():
            await _err(f"File not found: {resolved}")
            return

        if resolved.is_dir():
            await _err(f"Path is a directory — use file:list instead")
            return

        try:
            raw_bytes = resolved.read_bytes()
        except PermissionError as e:
            await _err(f"Permission denied: {e}")
            return
        except OSError as e:
            await _err(str(e))
            return

        file_size = len(raw_bytes)

        # 바이너리 판별
        try:
            content = raw_bytes.decode("utf-8")
            is_binary = False
        except UnicodeDecodeError:
            is_binary = True

        if is_binary:
            await self._emit("file:read_result", {
                "request_id":     request_id,
                "path":           str(resolved),
                "file_size":      file_size,
                "is_binary":      True,
                "base64_content": base64.b64encode(raw_bytes).decode(),
                "mime_type":      _guess_mime(resolved),
            })
            return

        truncated = len(content) > _MAX_READ_CHARS
        if truncated:
            content = content[:_MAX_READ_CHARS]

        await self._emit("file:read_result", {
            "request_id":  request_id,
            "path":        str(resolved),
            "content":     content,
            "total_lines": content.count("\n") + 1,
            "file_size":   file_size,
            "truncated":   truncated,
            "is_binary":   False,
        })

    # ── file:download ─────────────────────────────────────────────────────────

    async def handle_download(self, data: dict) -> None:
        """
        대용량 파일을 256 KB 청크로 분할 스트리밍합니다.

        data: { transfer_id: str, path: str }
        emit (반복): file:chunk { transfer_id, index, data (base64), done, total_size }
        emit (오류): file:download_error { transfer_id, error }
        """
        transfer_id = data.get("transfer_id", "")
        raw_path    = data.get("path", "")
        logger.debug("file:download received — transfer_id=%s path=%s", transfer_id, raw_path)

        async def _err(msg: str) -> None:
            await self._emit("file:download_error", {"transfer_id": transfer_id, "error": msg})

        try:
            resolved = _resolve(raw_path)
        except Exception as e:
            await _err(f"Invalid path: {e}")
            return

        if blocked := _is_read_blocked(resolved):
            await _err(blocked)
            return

        if not resolved.exists() or not resolved.is_file():
            await _err(f"File not found: {resolved}")
            return

        total_size = resolved.stat().st_size
        logger.info("file:download start — %s (%d bytes)", resolved, total_size)

        try:
            with open(resolved, "rb") as f:
                idx = 0
                while chunk := f.read(_CHUNK_SIZE):
                    await self._emit("file:chunk", {
                        "transfer_id": transfer_id,
                        "index":       idx,
                        "data":        base64.b64encode(chunk).decode(),
                        "done":        False,
                        "total_size":  total_size,
                    })
                    idx += 1
                    await asyncio.sleep(0)  # 이벤트 루프에 제어권 양보

            # 종료 마커
            await self._emit("file:chunk", {
                "transfer_id": transfer_id,
                "index":       idx,
                "data":        "",
                "done":        True,
                "total_size":  total_size,
            })
            logger.info("file:download done — %s (%d chunks)", resolved, idx)

        except PermissionError as e:
            await _err(f"Permission denied: {e}")
        except OSError as e:
            await _err(str(e))

    # ── file:upload_start ─────────────────────────────────────────────────────

    async def handle_upload_start(self, data: dict) -> None:
        """
        업로드 세션을 초기화합니다.

        data: { transfer_id: str, path: str, total_size: int }
        emit: file:upload_start_result { transfer_id, ok } | { transfer_id, error }
        """
        transfer_id = data.get("transfer_id", "")
        raw_path    = data.get("path", "")
        total_size  = int(data.get("total_size", 0))
        logger.debug("file:upload_start received — transfer_id=%s path=%s size=%d",
                     transfer_id, raw_path, total_size)

        async def _err(msg: str) -> None:
            await self._emit("file:upload_start_result",
                             {"transfer_id": transfer_id, "error": msg})

        if total_size > _MAX_UPLOAD_BYTES:
            await _err(
                f"File too large: {total_size:,} bytes "
                f"(max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
            )
            return

        if not raw_path:
            await _err("path is required")
            return

        try:
            resolved = _resolve(raw_path)
        except Exception as e:
            await _err(f"Invalid path: {e}")
            return

        if is_write_denied(str(resolved)):
            await _err(f"Write denied: {resolved}")
            return

        self._upload_state[transfer_id] = {
            "path":    resolved,
            "chunks":  {},
            "total":   total_size,
            "started": time.monotonic(),
        }
        logger.info("file:upload_start — transfer=%s path=%s size=%d",
                    transfer_id, resolved, total_size)

        await self._emit("file:upload_start_result",
                         {"transfer_id": transfer_id, "ok": True})

    # ── file:upload_chunk ─────────────────────────────────────────────────────

    async def handle_upload_chunk(self, data: dict) -> None:
        """
        업로드 청크를 수신합니다. done=True 일 때 파일을 디스크에 저장합니다.

        data: { transfer_id: str, index: int, data: str (base64), done: bool }
        emit: file:upload_chunk_result { transfer_id, ok, received } (중간)
              file:upload_chunk_result { transfer_id, ok, bytes_written, path } (완료)
              | { transfer_id, error }
        """
        transfer_id = data.get("transfer_id", "")
        logger.debug("file:upload_chunk received — transfer_id=%s index=%s done=%s",
                     transfer_id, data.get("index"), data.get("done"))
        state = self._upload_state.get(transfer_id)

        async def _err(msg: str) -> None:
            await self._emit("file:upload_chunk_result",
                             {"transfer_id": transfer_id, "error": msg})

        if state is None:
            await _err("Unknown transfer_id — call file:upload_start first")
            return

        index   = int(data.get("index", 0))
        b64data = data.get("data", "")
        done    = bool(data.get("done", False))

        if b64data:
            try:
                state["chunks"][index] = base64.b64decode(b64data)
            except Exception as e:
                await _err(f"Invalid base64 at chunk {index}: {e}")
                return

        if not done:
            await self._emit("file:upload_chunk_result", {
                "transfer_id": transfer_id,
                "ok":          True,
                "received":    index,
            })
            return

        # 모든 청크를 순서대로 조립 후 파일 저장
        resolved: Path = state["path"]
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with open(resolved, "wb") as f:
                for i in sorted(state["chunks"]):
                    f.write(state["chunks"][i])
            bytes_written = resolved.stat().st_size
        except OSError as e:
            del self._upload_state[transfer_id]
            await _err(str(e))
            return

        del self._upload_state[transfer_id]
        logger.info("file:upload done — %s (%d bytes)", resolved, bytes_written)

        await self._emit("file:upload_chunk_result", {
            "transfer_id":  transfer_id,
            "ok":           True,
            "bytes_written": bytes_written,
            "path":         str(resolved),
        })

    # ── file:delete ───────────────────────────────────────────────────────────

    async def handle_delete(self, data: dict) -> None:
        """
        파일 또는 디렉토리를 삭제합니다.

        data: { request_id: str, path: str }
        emit: file:delete_result { request_id, ok } | { request_id, error }
        """
        request_id = data.get("request_id", "")
        raw_path   = data.get("path", "")
        logger.debug("file:delete received — request_id=%s path=%s", request_id, raw_path)

        async def _err(msg: str) -> None:
            await self._emit("file:delete_result",
                             {"request_id": request_id, "error": msg})

        if not raw_path:
            await _err("path is required")
            return

        try:
            resolved = _resolve(raw_path)
        except Exception as e:
            await _err(f"Invalid path: {e}")
            return

        if is_write_denied(str(resolved)):
            await _err(f"Delete denied: {resolved}")
            return

        if not resolved.exists():
            await _err(f"Path not found: {resolved}")
            return

        try:
            if resolved.is_dir() and not resolved.is_symlink():
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
        except PermissionError as e:
            await _err(f"Permission denied: {e}")
            return
        except OSError as e:
            await _err(str(e))
            return

        logger.info("file:delete — %s", resolved)
        await self._emit("file:delete_result",
                         {"request_id": request_id, "ok": True})
