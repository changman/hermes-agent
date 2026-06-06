# ============================================================================
# Skytower Channel Add-on Installer for Hermes Agent (Windows / PowerShell)
# ============================================================================
# 공식 Hermes Agent에 Skytower 채널을 추가합니다.
# 먼저 공식 Hermes Agent가 설치되어 있어야 합니다.
#
# 1단계 - 공식 Hermes Agent 설치 (아직 설치 안 된 경우):
#   iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)
#
# 2단계 - Skytower 채널 추가:
#   iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)
#
# 또는 토큰을 미리 지정:
#   $env:SKYTOWER_TOKEN = "agentId:rawToken"
#   iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)
#
# ============================================================================

param(
    [string]$Token      = $env:SKYTOWER_TOKEN,
    [string]$Url        = $env:SKYTOWER_URL,
    [string]$HermesHome = ""
)

if (-not $Url) { $Url = "https://skytower-api.codescape.biz" }

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new() } catch {}

# ============================================================================
# Helpers
# ============================================================================

function Write-Banner {
    Write-Host ""
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Magenta
    Write-Host "|        [*] Skytower Channel Add-on Installer            |" -ForegroundColor Magenta
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Magenta
    Write-Host "|  공식 Hermes Agent에 Skytower 채널을 추가합니다         |" -ForegroundColor Magenta
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Magenta
    Write-Host ""
}

function Write-Info    { param([string]$m) Write-Host "-> $m"    -ForegroundColor Cyan    }
function Write-Success { param([string]$m) Write-Host "[OK] $m"  -ForegroundColor Green   }
function Write-Warn    { param([string]$m) Write-Host "[!!] $m"  -ForegroundColor Yellow  }
function Write-Err     { param([string]$m) Write-Host "[XX] $m"  -ForegroundColor Red     }

# ============================================================================
# Hermes 설치 위치 탐색
# ============================================================================

function Find-HermesInstall {
    $candidates = @(
        "$env:LOCALAPPDATA\hermes\hermes-agent",   # 공식 Windows 기본값
        "$env:USERPROFILE\.hermes\hermes-agent",   # 레거시 / WSL 스타일
        "$env:HERMES_INSTALL_DIR"
    )

    # hermes.exe / hermes.cmd 심볼릭 링크로부터 역추적
    $hermesCmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($hermesCmd) {
        $real = (Get-Item $hermesCmd.Source -ErrorAction SilentlyContinue)
        if ($real -and $real.Target) {
            # venv\Scripts\hermes.exe -> <install_dir>\venv\Scripts\hermes.exe
            $inferred = Split-Path (Split-Path (Split-Path $real.Target))
            if (Test-Path "$inferred\pyproject.toml") {
                $candidates = @($inferred) + $candidates
            }
        }
    }

    foreach ($dir in $candidates) {
        if (-not $dir) { continue }
        if ((Test-Path "$dir\pyproject.toml") -and (Test-Path "$dir\gateway")) {
            Write-Success "Hermes 설치 위치: $dir"
            return $dir
        }
    }

    Write-Err "Hermes Agent 설치를 찾을 수 없습니다."
    Write-Host ""
    Write-Host "  먼저 공식 Hermes Agent를 설치해주세요:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  iex (irm https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1)" -ForegroundColor Cyan
    Write-Host ""
    exit 1
}

# ============================================================================
# Hermes Home 결정
# ============================================================================

function Resolve-HermesHome {
    param([string]$InstallDir)

    if ($HermesHome -ne "") { return $HermesHome }
    if ($env:HERMES_HOME)   { return $env:HERMES_HOME }

    # InstallDir의 부모가 Hermes Home인 경우 (e.g. %LOCALAPPDATA%\hermes\hermes-agent)
    $parent = Split-Path $InstallDir
    if (Test-Path "$parent\.env") { return $parent }

    # 공식 기본값
    return "$env:LOCALAPPDATA\hermes"
}

# ============================================================================
# Python / uv 실행기 탐색
# ============================================================================

function Find-Python {
    param([string]$InstallDir)

    $venvPython = "$InstallDir\venv\Scripts\python.exe"
    if (Test-Path $venvPython) { return $venvPython }

    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }

    Write-Err "Python을 찾을 수 없습니다. Hermes가 올바르게 설치됐는지 확인하세요."
    exit 1
}

function Find-Uv {
    param([string]$InstallDir)

    # Hermes 번들 uv 우선
    $bundled = "$InstallDir\venv\Scripts\uv.exe"
    if (Test-Path $bundled) { return $bundled }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { return $uv.Source }

    return $null
}

# ============================================================================
# 플러그인 파일 설치
# ============================================================================

function Install-PluginFiles {
    param([string]$InstallDir, [string]$ScriptDir)

    $targetDir = "$InstallDir\plugins\platforms\skytower"
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

    # 이 스크립트가 저장소 안에 있는지 확인 (로컬 개발 모드)
    # iex (irm ...) 방식 실행 시 $ScriptDir = $PWD 이므로 실제 파일 존재 여부로 판단
    $localPluginDir = Join-Path $ScriptDir "..\plugins\platforms\skytower"
    $localFilesDir  = Join-Path $ScriptDir "..\gateway\platforms"
    $localPluginDir = [System.IO.Path]::GetFullPath($localPluginDir)

    if ((Test-Path "$localPluginDir\adapter.py") -and (Test-Path "$localPluginDir\..\..\..\gateway")) {
        Write-Info "로컬 소스에서 플러그인 파일 복사 중..."
        Copy-Item "$localPluginDir\*" -Destination $targetDir -Recurse -Force

        $localFilesPy = "$localFilesDir\skytower_files.py"
        if (Test-Path $localFilesPy) {
            Copy-Item $localFilesPy "$InstallDir\gateway\platforms\skytower_files.py" -Force
        }

        # Windows CP949 인코딩 버그 수정 파일
        $localGatewayWindows = [System.IO.Path]::GetFullPath("$ScriptDir\..\hermes_cli\gateway_windows.py")
        $localStatusPy       = [System.IO.Path]::GetFullPath("$ScriptDir\..\gateway\status.py")
        if (Test-Path $localGatewayWindows) {
            Copy-Item $localGatewayWindows "$InstallDir\hermes_cli\gateway_windows.py" -Force
        }
        if (Test-Path $localStatusPy) {
            Copy-Item $localStatusPy "$InstallDir\gateway\status.py" -Force
        }
        Write-Success "플러그인 파일 복사 완료"
    } else {
        # GitHub에서 다운로드
        Write-Info "GitHub에서 플러그인 파일 다운로드 중..."

        $baseUrl = "https://raw.githubusercontent.com/changman/hermes-agent/skytower"
        $files = @(
            "plugins/platforms/skytower/__init__.py",
            "plugins/platforms/skytower/adapter.py",
            "plugins/platforms/skytower/soul_sync.py",
            "plugins/platforms/skytower/plugin.yaml",
            "gateway/platforms/skytower_files.py",
            "gateway/commands_parser.py",
            "hermes_cli/gateway_windows.py",
            "gateway/status.py"
        )

        foreach ($file in $files) {
            $url  = "$baseUrl/$file"
            $dest = "$InstallDir\$($file -replace '/', '\')"
            New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
            try {
                Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
                Write-Success "다운로드: $file"
            } catch {
                Write-Err "다운로드 실패: $file — $_"
                exit 1
            }
        }
    }
}

# ============================================================================
# 의존성 설치
# ============================================================================

function Install-Deps {
    param([string]$InstallDir, [string]$PythonExe, [string]$UvExe)

    Write-Info "Skytower 의존성 설치 중 (python-socketio, psutil)..."
    $pkgs = @("python-socketio[asyncio_client]>=5.11", "psutil>=5.9")

    if ($UvExe) {
        $env:VIRTUAL_ENV = "$InstallDir\venv"
        & $UvExe pip install @pkgs -q
    } else {
        $pipExe = "$InstallDir\venv\Scripts\pip.exe"
        if (Test-Path $pipExe) {
            & $pipExe install @pkgs -q
        } else {
            & $PythonExe -m pip install @pkgs -q
        }
    }

    if ($LASTEXITCODE -ne 0) {
        Write-Err "의존성 설치 실패"
        exit 1
    }
    Write-Success "의존성 설치 완료"
}

# ============================================================================
# 에이전트 자동 등록
# ============================================================================

function Register-SkytowerAgent {
    param([string]$RelayUrl, [string]$AgentName)

    Write-Info "Skytower 서버에 에이전트 등록 중: $AgentName"
    $body = "{`"name`": `"$AgentName`"}"
    try {
        $resp = Invoke-RestMethod -Uri "$RelayUrl/api/agents/register" `
            -Method POST `
            -ContentType "application/json" `
            -Body $body `
            -ErrorAction Stop
        $token = $resp.token
        if (-not $token) { throw "응답에 token 필드가 없습니다" }
        Write-Success "에이전트 등록 완료: $($resp.agentId)"
        Write-Host ""
        Write-Host "  [!] 토큰은 재발급 불가 — 안전한 곳에 보관하세요:" -ForegroundColor Yellow
        Write-Host "      $token" -ForegroundColor White
        Write-Host ""
        return $token
    } catch {
        Write-Warn "자동 등록 실패: $_"
        return $null
    }
}

# ============================================================================
# 토큰 획득 (자동 등록 또는 수동 입력)
# ============================================================================

function Get-SkytowerToken {
    param([string]$RelayUrl, [string]$ExistingToken)

    if ($ExistingToken) { return $ExistingToken }

    Write-Host ""
    Write-Host "  Skytower 에이전트 토큰이 없습니다." -ForegroundColor Yellow
    Write-Host "  Skytower 서버에 새 에이전트를 자동 등록할 수 있습니다." -ForegroundColor Yellow
    Write-Host ""
    $doRegister = Read-Host "자동 등록하시겠습니까? (Y/n)"
    if ($doRegister -ne "n" -and $doRegister -ne "N") {
        $defaultName = "My Hermes Agent"
        $agentName = Read-Host "에이전트 이름 (기본값: $defaultName)"
        if (-not $agentName) { $agentName = $defaultName }
        $registered = Register-SkytowerAgent -RelayUrl $RelayUrl -AgentName $agentName
        if ($registered) { return $registered }
    }

    return (Read-Host "`nSkytower 에이전트 토큰 입력 (agentId:rawToken, 없으면 Enter)").Trim()
}

# ============================================================================
# .env 설정
# ============================================================================

function Set-EnvLine {
    param([string]$FileContent, [string]$Key, [string]$Value)
    if ($FileContent -match "(?m)^${Key}=.*") {
        return $FileContent -replace "(?m)^${Key}=.*", "${Key}=${Value}"
    } else {
        return $FileContent + "`n${Key}=${Value}"
    }
}

function Configure-Env {
    param([string]$HermesHomeDir, [string]$Token, [string]$Url)

    $envFile = "$HermesHomeDir\.env"
    if (-not (Test-Path $envFile)) { New-Item -ItemType Directory -Force -Path $HermesHomeDir | Out-Null; New-Item -ItemType File -Force -Path $envFile | Out-Null }

    [string]$envContent = (Get-Content $envFile -Raw -ErrorAction SilentlyContinue)
    if (-not $envContent) { $envContent = "" }

    $changed = $false
    if ($Token) { $envContent = Set-EnvLine $envContent "SKYTOWER_TOKEN" $Token; $changed = $true }
    if ($Url)   { $envContent = Set-EnvLine $envContent "SKYTOWER_URL"   $Url;   $changed = $true }

    if ($envContent -notmatch "(?m)^SKYTOWER_ALLOW_ALL_USERS=") {
        $envContent += "`nSKYTOWER_ALLOW_ALL_USERS=true"
    }
    if ($envContent -notmatch "(?m)^SKYTOWER_PRINT_PAIR_CODE=") {
        $envContent += "`nSKYTOWER_PRINT_PAIR_CODE=1"
    }

    Set-Content -Path $envFile -Value $envContent.TrimStart() -Encoding UTF8

    if ($changed) {
        Write-Success "Skytower 설정 저장됨: $envFile"
    } else {
        Write-Warn "토큰을 나중에 $envFile 에 직접 추가하세요:"
        Write-Host "  SKYTOWER_TOKEN=agentId:rawToken" -ForegroundColor Yellow
    }
}

# ============================================================================
# 동작 확인
# ============================================================================

function Test-Plugin {
    param([string]$InstallDir, [string]$PythonExe)

    Write-Info "플러그인 로드 확인 중..."
    $result = & $PythonExe -c @"
import sys
sys.path.insert(0, r'$InstallDir')
try:
    from plugins.platforms.skytower import register
    print('OK')
except Exception as e:
    print('FAIL:', e)
"@ 2>&1

    if ($result -match "^OK") {
        Write-Success "플러그인 로드 확인 완료"
    } else {
        Write-Warn "플러그인 로드 확인 실패 — 설치는 완료됐지만 import 테스트에 실패했습니다"
        Write-Warn "의존성(python-socketio)이 설치됐는지 확인하세요"
    }
}

# ============================================================================
# 완료 메시지
# ============================================================================

function Write-SuccessBanner {
    param([string]$InstallDir, [string]$HermesHomeDir)

    Write-Host ""
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Green
    Write-Host "|         [OK] Skytower 채널 추가 완료!                  |" -ForegroundColor Green
    Write-Host "+---------------------------------------------------------+" -ForegroundColor Green
    Write-Host ""
    Write-Host "설치된 위치:" -ForegroundColor Cyan
    Write-Host "  플러그인:  $InstallDir\plugins\platforms\skytower\"
    Write-Host "  설정:      $HermesHomeDir\.env"
    Write-Host ""
    Write-Host "시작 방법:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  hermes gateway              " -NoNewline; Write-Host "게이트웨이 시작" -ForegroundColor Green
    Write-Host "  hermes gateway install      " -NoNewline; Write-Host "Windows 서비스로 등록" -ForegroundColor Green
    Write-Host ""
    Write-Host "Skytower 채팅 명령어:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  /chatid    현재 대화 JID 확인"
    Write-Host "  /sethome   현재 대화를 홈 채널로 설정"
    Write-Host "  /skills    사용 가능한 스킬 목록"
    Write-Host "  /paircode  친구 추가 코드 발급 (10분 유효)"
    Write-Host ""

    $envFile = "$HermesHomeDir\.env"
    $tokenVal = (Get-Content $envFile -ErrorAction SilentlyContinue | Where-Object { $_ -match "^SKYTOWER_TOKEN=" }) -replace "^SKYTOWER_TOKEN=", ""
    if (-not $tokenVal) {
        Write-Host "[!!] SKYTOWER_TOKEN이 아직 설정되지 않았습니다." -ForegroundColor Yellow
        Write-Host "     $envFile 에 추가 후 게이트웨이를 재시작하세요." -ForegroundColor Yellow
        Write-Host ""
    }
}

# ============================================================================
# Main
# ============================================================================

Write-Banner

$InstallDir   = Find-HermesInstall
$HermesHomeResolved = Resolve-HermesHome -InstallDir $InstallDir
$PythonExe    = Find-Python   -InstallDir $InstallDir
$UvExe        = Find-Uv       -InstallDir $InstallDir
# iex (irm ...) 방식으로 실행하면 MyCommand.Path가 $null — 미리 guard
$ScriptDir = if ($MyInvocation.MyCommand.Path) {
    Split-Path -Parent $MyInvocation.MyCommand.Path
} else {
    $PWD.Path
}

Install-PluginFiles -InstallDir $InstallDir -ScriptDir $ScriptDir
Install-Deps        -InstallDir $InstallDir -PythonExe $PythonExe -UvExe $UvExe

# .env에 이미 토큰이 있으면 자동 등록 스킵
if (-not $Token) {
    $envFile = "$HermesHomeResolved\.env"
    if (Test-Path $envFile) {
        $existing = (Get-Content $envFile -ErrorAction SilentlyContinue |
            Where-Object { $_ -match "^SKYTOWER_TOKEN=.+" }) -replace "^SKYTOWER_TOKEN=", ""
        if ($existing) {
            $Token = $existing
            Write-Info "기존 토큰 발견 — 자동 등록 스킵"
        }
    }
}

$ResolvedToken = Get-SkytowerToken -RelayUrl $Url -ExistingToken $Token

Configure-Env       -HermesHomeDir $HermesHomeResolved -Token $ResolvedToken -Url $Url
Test-Plugin         -InstallDir $InstallDir -PythonExe $PythonExe

Write-SuccessBanner -InstallDir $InstallDir -HermesHomeDir $HermesHomeResolved
