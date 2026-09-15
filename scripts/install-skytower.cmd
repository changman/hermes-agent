@echo off
REM ============================================================================
REM Skytower Channel Add-on Installer for Hermes Agent (Windows / CMD wrapper)
REM ============================================================================
REM 공식 Hermes Agent에 Skytower 채널을 추가합니다.
REM 먼저 공식 Hermes Agent가 설치되어 있어야 합니다.
REM
REM 1단계 - 공식 Hermes Agent 설치 (아직 설치 안 된 경우):
REM   curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.cmd -o install.cmd && install.cmd && del install.cmd
REM
REM 2단계 - Skytower 채널 추가:
REM   curl -fsSL https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.cmd -o install-skytower.cmd && install-skytower.cmd && del install-skytower.cmd
REM
REM 또는 토큰을 미리 지정:
REM   set SKYTOWER_TOKEN=agentId:rawToken
REM   install-skytower.cmd
REM
REM PowerShell을 직접 사용할 수 있다면:
REM   iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)
REM ============================================================================

echo.
echo  +---------------------------------------------------------+
echo  ^|        [*] Skytower Channel Add-on Installer           ^|
echo  +---------------------------------------------------------+
echo  ^|  공식 Hermes Agent에 Skytower 채널을 추가합니다        ^|
echo  +---------------------------------------------------------+
echo.
echo  PowerShell 설치 스크립트를 실행합니다...
echo.

powershell -ExecutionPolicy ByPass -NoProfile -Command ^
  "$env:SKYTOWER_TOKEN='%SKYTOWER_TOKEN%'; iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)"

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  [XX] 설치 실패. PowerShell에서 직접 실행해보세요:
    echo.
    echo    powershell -ExecutionPolicy ByPass -c "iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)"
    echo.
    pause
    exit /b 1
)
