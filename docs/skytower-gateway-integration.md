# Skytower Gateway Integration — 아키텍처 옵션

## 배경

Hermes Gateway는 메시지 플랫폼과의 연결을 `gateway/run.py`가 중앙 관리한다.
Skytower 채널을 추가하는 방법에는 세 가지 선택지가 있으며, 각각 유지보수 비용과 기능 범위가 다르다.

현재 브랜치는 **Option C** 로 구현되어 있다.

---

## Option A — Gateway 직접 수정 (Fork 유지)

### 방식

`gateway/run.py`에 Skytower 전용 코드를 직접 삽입한다.

```
upstream (NousResearch/hermes-agent)       skytower branch
──────────────────────────────────         ─────────────────────────────────
gateway/run.py (원본)               →      gateway/run.py (Skytower 코드 삽입)
                                             ├─ _pending_thinking 메커니즘
                                             ├─ _pending_usage 메커니즘
                                             └─ _reasoning_delta_cb 콜백 설정
```

### 구현된 기능

| 기능 | 구현 방법 |
|------|----------|
| 실시간 reasoning 스트리밍 | `_reasoning_delta_cb` → `adapter.send_thinking_chunk()` |
| 최종 응답에 thinking 필드 포함 | `_pending_thinking` 임시 저장 → `send()` 에서 소비 |
| turn delta 토큰 사용량 포함 | `_pending_usage` 임시 저장 → `send()` 에서 소비 |

### 장단점

**장점**
- 추가 협의 없이 즉시 구현 가능
- reasoning, token usage 등 풍부한 메타데이터 전달 가능

**단점**
- upstream 업데이트 시마다 rebase 충돌 발생
  ```bash
  # upstream이 run.py를 크게 변경하면
  git rebase upstream/main
  # CONFLICT: 삽입 코드 위치가 upstream과 달라짐
  # 수동 해결 후 재테스트 반복
  ```
- `run.py`는 20,000줄 이상의 핵심 파일이라 충돌 해결 난이도가 높음
- Skytower 코드가 upstream 코드와 뒤섞여 가독성 저하

---

## Option B — Upstream PR 기여 (General Hook)

### 방식

`gateway/run.py`에 플랫폼 비종속적인 일반 훅(hook)을 추가하는 PR을 NousResearch에 제출한다.
Skytower는 플러그인으로서 해당 훅을 구현만 한다.

```
upstream (NousResearch/hermes-agent)       skytower branch
──────────────────────────────────         ─────────────────────────────────
gateway/run.py                      ←PR    plugins/platforms/skytower/
  + on_turn_complete(adapter, result)         adapter.py
  + adapter.on_reasoning_delta(text)            └─ on_turn_complete() 구현
  + (일반 메커니즘)                               └─ on_reasoning_delta() 구현
```

### 제안 훅 인터페이스 (예시)

```python
# gateway/platforms/base.py 에 추가 제안
class BasePlatformAdapter:
    async def on_turn_complete(self, chat_id: str, result: dict) -> None:
        """에이전트 턴 완료 시 호출. token usage, reasoning 전달."""
        pass

    async def on_reasoning_delta(self, text: str, chat_id: str) -> None:
        """실시간 reasoning 스트리밍 델타."""
        pass
```

```python
# plugins/platforms/skytower/adapter.py 구현
class SkyTowerAdapter(BasePlatformAdapter):
    async def on_turn_complete(self, chat_id: str, result: dict) -> None:
        payload = {"type": "metadata"}
        if result.get("last_reasoning"):
            payload["thinking"] = result["last_reasoning"]
        if result.get("input_tokens"):
            payload["usage"] = {
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
            }
        await self._sio.emit("message_done", payload)

    async def on_reasoning_delta(self, text: str, chat_id: str) -> None:
        await self._sio.emit("thinking_chunk", {"text": text})
```

### 장단점

**장점**
- upstream merge 후 `gateway/run.py` 수정 없음 → 자동 추적 가능
- 다른 플랫폼 플러그인도 동일 훅 활용 가능 (ecosystem 개선)
- 코드 책임 분리가 명확

**단점**
- PR 수락까지 수 주~수 개월 소요
- upstream maintainer가 거부할 수 있음
- PR이 거부되면 결국 Option A 또는 C로 회귀

### PR 전략

```markdown
제목: feat(gateway): add on_turn_complete / on_reasoning_delta hooks for platform adapters

동기:
- 플랫폼 어댑터가 에이전트 턴 완료 시 토큰 사용량과 reasoning을
  플랫폼 특화 방식으로 전달할 수 없음
- 현재는 run.py를 직접 수정해야 해서 플러그인 생태계를 저해함

변경:
- BasePlatformAdapter에 on_turn_complete(), on_reasoning_delta() 훅 추가
- GatewayRunner가 턴 완료/reasoning 이벤트 시 훅 호출
- 기본 구현은 no-op → 기존 플랫폼 무영향
```

---

## Option C — 순수 플러그인 (현재 구현)

### 방식

`gateway/run.py`를 전혀 수정하지 않는다.
Skytower 어댑터는 plugin 디렉토리 안에만 존재하며, 표준 gateway 인터페이스만 사용한다.

```
upstream (NousResearch/hermes-agent)       skytower branch
──────────────────────────────────         ─────────────────────────────────
gateway/run.py (원본 그대로)               plugins/platforms/skytower/
                                             ├─ __init__.py
                                             ├─ adapter.py
                                             └─ plugin.yaml
                                           gateway/platforms/
                                             └─ skytower_files.py
```

### 기능 범위

| 기능 | 지원 여부 | 비고 |
|------|----------|------|
| 메시지 수신·송신 | ✅ | `send()` → `message_done` |
| 파일 업로드/다운로드 | ✅ | file:* 이벤트 처리 |
| 스킬 바인딩 | ✅ | config.yaml channel_skill_bindings |
| 홈 채널 설정 | ✅ | /sethome, /chatid |
| 실시간 reasoning 스트리밍 | ❌ | gateway hook 없음 |
| thinking 필드 (message_done) | ❌ | gateway hook 없음 |
| 토큰 사용량 (message_done) | ❌ | gateway hook 없음 |

### 장단점

**장점**
- `gateway/run.py` diff 0 줄 → upstream rebase 시 충돌 없음
- 유지보수 부담 최소화
- 플러그인 아키텍처 원칙에 충실

**단점**
- reasoning, token usage 클라이언트 표시 불가
- Skytower 클라이언트가 받는 `message_done`은 `content`만 포함

### 업데이트 절차

```bash
# upstream 새 버전 반영
git fetch upstream
git rebase upstream/main
# gateway/run.py 충돌 없음 → 자동 완료

# Skytower 플러그인만 별도 관리
git log --oneline -- plugins/platforms/skytower/
```

---

## 선택 기준

```
┌─────────────────────────────────────┐
│ reasoning / token usage 필요?       │
│                                     │
│  No  ──────────────→  Option C     │
│                       (현재, 권장)  │
│                                     │
│  Yes                                │
│   ↓                                 │
│  upstream PR 시도할 의향?            │
│                                     │
│  Yes ──────────────→  Option B     │
│                       (장기 목표)   │
│                                     │
│  No  ──────────────→  Option A     │
│                       (단기 타협)   │
└─────────────────────────────────────┘
```

---

## 현재 브랜치 diff 현황

```bash
# 현재 skytower 브랜치가 upstream과 다른 파일
git diff upstream/main --name-only

# 예상 결과:
# gateway/platforms/skytower_files.py  ← 파일 접근 헬퍼 (신규)
# plugins/platforms/skytower/          ← Skytower 플러그인 (신규)
# scripts/install-skytower.sh          ← 설치 스크립트 (신규)
# scripts/install-skytower.ps1         ← Windows 설치 스크립트 (신규)
# hermes_cli/gateway_windows.py        ← CP949 버그 수정 (수정)
#
# gateway/run.py 는 없음 → upstream과 동일
```

`hermes_cli/gateway_windows.py`의 CP949 버그 수정은 upstream에도 기여할 수 있는 일반 버그 수정이다.
