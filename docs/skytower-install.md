# Skytower 채널 설치 가이드

Hermes Agent에 Skytower 채널 애드온을 추가하는 방법을 설명합니다.

---

## 사전 요구 사항

Skytower 채널은 공식 Hermes Agent 위에 애드온으로 동작합니다. **먼저 공식 Hermes Agent를 설치해야 합니다.**

**Linux / macOS**
```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

**Windows (PowerShell)**
```powershell
iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)
```

**Windows (CMD)**
```bat
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.cmd -o install.cmd && install.cmd && del install.cmd
```

---

## Step 1 — Skytower 채널 애드온 설치

### Linux / macOS

```bash
curl -fsSL https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.sh | bash
```

토큰을 미리 지정하면 인터랙티브 입력 없이 자동 설치됩니다.

```bash
SKYTOWER_TOKEN="agentId:rawToken" \
curl -fsSL https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.sh | bash
```

### Windows (PowerShell)

```powershell
iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)
```

토큰을 미리 지정:

```powershell
$env:SKYTOWER_TOKEN = "agentId:rawToken"
iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)
```

### Windows (CMD)

```bat
set SKYTOWER_TOKEN=agentId:rawToken
curl -fsSL https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.cmd -o install-skytower.cmd && install-skytower.cmd && del install-skytower.cmd
```

---

## 설치 중 인터랙티브 흐름

토큰을 지정하지 않고 실행하면 설치 스크립트가 자동 등록을 제안합니다.  
Relay URL은 `https://skytower-api.codescape.biz`로 자동 설정됩니다.

```
  Skytower 에이전트 토큰이 없습니다.
  Skytower 서버에 새 에이전트를 자동 등록할 수 있습니다.

자동 등록하시겠습니까? (Y/n): Y
에이전트 이름 (기본값: My Hermes Agent): 

→  Skytower 서버에 에이전트 등록 중: My Hermes Agent
✓  에이전트 등록 완료: ePm9vhUooJx9

  [!] 토큰은 재발급 불가 — 안전한 곳에 보관하세요:
      ePm9vhUooJx9:rawtoken...
```

---

## 환경 변수 레퍼런스

설치 후 `~/.hermes/.env` (Linux/macOS) 또는 `%LOCALAPPDATA%\hermes\.env` (Windows)에 저장됩니다.

| 변수 | 필수 | 설명 |
|------|------|------|
| `SKYTOWER_TOKEN` | ✅ | 에이전트 토큰 (`agentId:rawToken` 형태) |
| `SKYTOWER_URL` | - | Skytower Relay 서버 URL (기본값: `https://skytower-api.codescape.biz`) |
| `SKYTOWER_ALLOW_ALL_USERS` | - | `true` 설정 시 모든 Skytower 사용자 허용 (기본값: `true`) |
| `SKYTOWER_ALLOWED_USERS` | - | 허용할 사용자 ID 목록 (쉼표 구분, `ALLOW_ALL_USERS=false`일 때 사용) |
| `SKYTOWER_PRINT_PAIR_CODE` | - | 연결 시 페어링 코드 출력 (`1`=출력, `0`=숨김, 기본값: `0`) |

`.env` 파일을 직접 수정할 수도 있습니다.

```env
SKYTOWER_TOKEN=ePm9vhUooJx9:rawtoken...
SKYTOWER_ALLOW_ALL_USERS=true
SKYTOWER_PRINT_PAIR_CODE=0
```

`SKYTOWER_URL`은 기본값(`https://skytower-api.codescape.biz`)이 자동으로 사용됩니다. 다른 서버를 사용할 경우에만 명시적으로 지정하세요.

---

## Step 2 — 게이트웨이 시작

**일회성 실행**
```bash
hermes gateway
```

**백그라운드 서비스로 등록**

Linux (systemd):
```bash
hermes gateway install
```

Windows:
```powershell
hermes gateway install
```

---

## Skytower 채팅 명령어

게이트웨이가 연결된 후 Skytower 채팅창에서 사용할 수 있는 명령어입니다.

| 명령어 | 설명 |
|--------|------|
| `/chatid` | 현재 대화의 JID 확인 |
| `/sethome` | 현재 대화를 홈 채널로 설정 |
| `/skills` | 사용 가능한 스킬 목록 조회 |

---

## 문제 해결

### 플러그인이 로드되지 않는 경우

의존성이 설치됐는지 확인합니다.

```bash
# Linux/macOS
~/.hermes/hermes-agent/venv/bin/pip list | grep -E "socketio|psutil"

# Windows (PowerShell)
& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\pip.exe" list | Select-String "socketio|psutil"
```

누락됐다면 직접 설치합니다.

```bash
pip install "python-socketio[asyncio_client]>=5.11" "psutil>=5.9"
```

### 연결이 되지 않는 경우

1. `SKYTOWER_URL`이 올바른지 확인합니다 (끝에 `/` 없이).
2. `SKYTOWER_TOKEN` 형식이 `agentId:rawToken`인지 확인합니다.
3. 게이트웨이 로그를 확인합니다: `hermes gateway --log-level debug`

### Windows에서 PowerShell 실행 정책 오류

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
```

또는 CMD 래퍼를 사용합니다 (`install-skytower.cmd`는 `ByPass` 정책으로 자동 실행합니다).

---

## 아키텍처 참고

Skytower 플러그인은 `gateway/run.py`를 수정하지 않는 **순수 플러그인(Option C)** 방식으로 구현됩니다. upstream Hermes Agent 업데이트 시 충돌 없이 `git rebase`가 가능합니다. 상세 내용은 [skytower-gateway-integration.md](skytower-gateway-integration.md)를 참조하세요.
