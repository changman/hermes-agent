"""Soul Sync — SkyTower → SOUL.md persona synchronization.

SkyTower Server에서 제공하는 persona를 $HERMES_HOME/SOUL.md에 저장합니다.
사용자가 Agent Settings에서 "Sync" 버튼을 클릭할 때만 동기화됩니다 (이벤트 기반).

환경 변수:
  SOUL_ALLOW_OVERWRITE  "1"/"true"/"yes"로 설정하면 기존 SOUL.md를 덮어씁니다.
                        기본값 false — 이미 파일이 있으면 건너뜁니다.
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SoulSync:
    """SkyTower persona → SOUL.md file synchronizer.

    이벤트 기반으로 동작합니다 (정기 폴링 없음).
    agent:sync-requested / soul:persona-updated Socket.IO 이벤트에 응답합니다.
    """

    def __init__(
        self,
        agent_id: str,
        token: str,
        relay_url: str,
        hermes_home: Optional[Path] = None,
        allow_overwrite: bool = False,
    ):
        self._agent_id = agent_id
        self._token = token
        self._relay_url = relay_url.rstrip("/")
        self._hermes_home = hermes_home
        self._allow_overwrite = allow_overwrite
        self._is_syncing = False
        self._last_sync_time: Optional[float] = None

    def _get_soul_path(self) -> Path:
        if self._hermes_home is not None:
            return self._hermes_home / "SOUL.md"
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "SOUL.md"

    async def fetch_personas(self) -> Optional[List[Dict[str, Any]]]:
        """SkyTower API에서 personas 조회.

        Returns:
            persona 딕셔너리 목록, 또는 오류 시 None.
            각 항목: {"user_id": ..., "persona": ..., "agent_id": ...}
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self._relay_url}/api/agents/{self._agent_id}/soul",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("personas") or []
        except Exception as e:
            logger.error("[SOUL] Failed to fetch personas: %s", e)
            return None

    async def update_soul_file(self, user_id: str, persona_content: str) -> bool:
        """persona를 $HERMES_HOME/SOUL.md에 저장.

        Returns:
            True이면 파일 저장 성공, False이면 건너뜀 또는 오류.
        """
        soul_path = self._get_soul_path()
        try:
            soul_path.parent.mkdir(parents=True, exist_ok=True)

            if soul_path.exists() and not self._allow_overwrite:
                logger.info(
                    "[SOUL] SOUL.md already exists — skipping overwrite (userId: %s, path: %s)",
                    user_id, soul_path,
                )
                return False

            soul_path.write_text(persona_content, encoding="utf-8")
            logger.info("[SOUL] SOUL.md updated (userId: %s, path: %s)", user_id, soul_path)
            return True
        except OSError as e:
            logger.error("[SOUL] Failed to write SOUL.md: %s", e)
            return False

    async def sync_personas(self) -> Dict[str, Any]:
        """모든 사용자의 persona를 동기화.

        agent:sync-requested 이벤트에서 호출됩니다.
        중복 실행은 _is_syncing 플래그로 방지됩니다.
        """
        if self._is_syncing:
            logger.warning("[SOUL] Sync already in progress")
            return {"ok": False, "error": "sync in progress"}

        self._is_syncing = True
        try:
            logger.info("[SOUL] Syncing personalities from SkyTower...")

            personas = await self.fetch_personas()
            if personas is None:
                return {"ok": False, "error": "failed to fetch personas"}
            if not personas:
                logger.info("[SOUL] No personas to sync")
                return {"ok": True, "synced": 0}

            logger.info("[SOUL] Found %d persona(s)", len(personas))

            synced = 0
            for p in personas:
                user_id = p.get("user_id", "unknown")
                persona = p.get("persona", "")
                if persona and await self.update_soul_file(user_id, persona):
                    synced += 1

            self._last_sync_time = time.time()
            logger.info("[SOUL] Sync completed — %d updated", synced)
            return {"ok": True, "synced": synced}
        except Exception as e:
            logger.error("[SOUL] Sync failed: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            self._is_syncing = False

    def get_status(self) -> Dict[str, Any]:
        """현재 동기화 상태 반환 (디버깅/헬스체크용)."""
        soul_path = self._get_soul_path()
        return {
            "soul_path": str(soul_path),
            "soul_exists": soul_path.exists(),
            "allow_overwrite": self._allow_overwrite,
            "last_sync_time": self._last_sync_time,
            "is_syncing": self._is_syncing,
            "mode": "event-driven",
        }
