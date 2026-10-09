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
# 또는 토큰/URL을 미리 지정:
#   $env:SKYTOWER_TOKEN = "agentId:rawToken"
#   $env:SKYTOWER_URL   = "https://relay.example.com"
#   iex (irm https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.ps1)
#
# ============================================================================

param(
    [string]$Token     = $env:SKYTOWER_TOKEN,
    [string]$Url       = $env:SKYTOWER_URL,
    [string]$HermesHome = ""
)

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

    # PM 관리 Hermes 는 설치 폴더 venv 가 없고 시스템 Python 도 없을 수 있다 — 그때는 필요 없다.
    return $null
}

# PM(패키지 관리자) 을 쓰는 Hermes 인가: 설치 폴더 venv 가 없고 installs\ 아래 기록이 있다.
function Test-PmManaged {
    param([string]$InstallDir, [string]$HermesHomeDir)
    return (-not (Test-Path "$InstallDir\venv")) -and [bool](Get-ChildItem "$HermesHomeDir\installs\*\facts.json" -ErrorAction SilentlyContinue)
}

# Hermes 가 실제로 도는 파이썬.
# PM 을 쓰는 Hermes 는 설치 폴더의 venv 를 지우고 <HermesHome>\installs\<key>\environments\<hash>\venv
# 에서 돈다. key 는 설치 폴더 실경로의 sha256 앞 16자 (pm/environments.py install_key).
function Find-RuntimePython {
    param([string]$InstallDir, [string]$HermesHomeDir)

    $venvPython = "$InstallDir\venv\Scripts\python.exe"
    if (Test-Path $venvPython) { return $venvPython }

    $facts = @()
    try {
        $full = (Get-Item $InstallDir).FullName.TrimEnd('\')
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $hash = -join ($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($full)) | ForEach-Object { $_.ToString("x2") })
        $facts += "$HermesHomeDir\installs\$($hash.Substring(0, 16))\facts.json"
    } catch {}
    $facts += @(Get-ChildItem "$HermesHomeDir\installs\*\facts.json" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })

    foreach ($f in $facts) {
        if (-not (Test-Path $f)) { continue }
        try {
            $envDir = (Get-Content $f -Raw | ConvertFrom-Json).packages.venv.environment
        } catch { continue }
        if ($envDir -and (Test-Path "$envDir\Scripts\python.exe")) { return "$envDir\Scripts\python.exe" }
    }
    return $null
}

function Find-HermesCli {
    param([string]$InstallDir, [string]$HermesHomeDir)

    $cmd = Get-Command hermes -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @("$HermesHomeDir\bin\hermes.exe", "$HermesHomeDir\bin\hermes.cmd", "$InstallDir\venv\Scripts\hermes.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
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

    # 이 스크립트가 이미 저장소 안에 있는지 확인 (로컬 개발 모드)
    $localPluginDir = Join-Path $ScriptDir "..\plugins\platforms\skytower"
    $localFilesDir  = Join-Path $ScriptDir "..\gateway\platforms"

    if (Test-Path "$localPluginDir\adapter.py") {
        Write-Info "로컬 소스에서 플러그인 파일 복사 중..."
        Copy-Item "$localPluginDir\*" -Destination $targetDir -Recurse -Force

        $localFilesPy = "$localFilesDir\skytower_files.py"
        if (Test-Path $localFilesPy) {
            Copy-Item $localFilesPy "$InstallDir\gateway\platforms\skytower_files.py" -Force
        }
        Write-Success "플러그인 파일 복사 완료"
    } else {
        # GitHub에서 다운로드
        Write-Info "GitHub에서 플러그인 파일 다운로드 중..."

        $baseUrl = "https://raw.githubusercontent.com/changman/hermes-agent/skytower"
        $files = @(
            "plugins/platforms/skytower/__init__.py",
            "plugins/platforms/skytower/adapter.py",
            "plugins/platforms/skytower/confinement.py",
            "plugins/platforms/skytower/plugin.yaml",
            "gateway/platforms/skytower_files.py"
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
    param([string]$InstallDir, [string]$HermesHomeDir, [string]$PythonExe, [string]$UvExe)

    # PM 이 관리하는 환경에 손으로 깔면 다음 hermes update 때 사라진다.
    # 거기서는 plugin.yaml 의 python_dependencies 를 Enable-Plugin 이 PM 으로 설치한다.
    if (Test-PmManaged -InstallDir $InstallDir -HermesHomeDir $HermesHomeDir) {
        Write-Info "PM 관리 환경 — 의존성은 플러그인 활성화 때 Hermes 가 설치합니다"
        return
    }
    if (-not $PythonExe -and -not $UvExe) {
        Write-Err "Python을 찾을 수 없습니다. Hermes가 올바르게 설치됐는지 확인하세요."
        exit 1
    }

    Write-Info "Skytower 의존성 설치 중 (python-socketio, psutil)..."
    $pkgs = @("python-socketio[asyncio_client]>=5.11,<6", "psutil>=5.9")

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
# 플러그인 활성화 (plugins.enabled)
# ============================================================================

# PM 은 plugins.enabled 에 있는 플러그인의 python_dependencies 만 환경에 넣는다.
# 활성화하지 않으면 hermes update 가 환경을 다시 만들 때 python-socketio 가 빠진다.
# 구버전 Hermes 에서는 config.yaml 에 기록만 한다 (번들 플랫폼은 원래 자동 로드).
function Enable-Plugin {
    param([string]$HermesCli, [string]$HermesHomeDir)

    if (-not $HermesCli) {
        Write-Warn "hermes 명령을 찾지 못해 플러그인 활성화를 건너뜁니다"
        Write-Warn "  직접 실행하세요: hermes plugins enable skytower-platform"
        return
    }
    Write-Info "플러그인 활성화 중 (hermes plugins enable skytower-platform)..."
    $env:HERMES_HOME = $HermesHomeDir
    & $HermesCli plugins enable skytower-platform
    if ($LASTEXITCODE -eq 0) {
        Write-Success "플러그인 활성화 완료"
    } else {
        Write-Warn "플러그인 활성화 실패 — 직접 실행하세요: hermes plugins enable skytower-platform"
    }
}

# ============================================================================
# .env 설정
# ============================================================================

function Configure-Env {
    param([string]$HermesHomeDir)

    $envFile = "$HermesHomeDir\.env"
    if (-not (Test-Path $envFile)) { New-Item -ItemType File -Force -Path $envFile | Out-Null }

    # 인터랙티브 입력
    if (-not $Token) {
        $Token = Read-Host "`nSkytower 에이전트 토큰 입력 (agentId:rawToken, 없으면 Enter)"
        $Token = $Token.Trim()
    }
    if (-not $Url) {
        $Url = Read-Host "Skytower Relay URL 입력 (예: https://relay.example.com, 없으면 Enter)"
        $Url = $Url.Trim()
    }

    $content = Get-Content $envFile -Raw -ErrorAction SilentlyContinue
    if (-not $content) { $content = "" }

    function Upsert-EnvVar {
        param([string]$Key, [string]$Value)
        if ($content -match "(?m)^${Key}=.*") {
            $script:content = $content -replace "(?m)^${Key}=.*", "${Key}=${Value}"
        } else {
            $script:content += "`n${Key}=${Value}"
        }
    }

    $changed = $false
    if ($Token) { Upsert-EnvVar "SKYTOWER_TOKEN" $Token; $changed = $true }
    if ($Url)   { Upsert-EnvVar "SKYTOWER_URL"   $Url;   $changed = $true }

    if ($content -notmatch "(?m)^SKYTOWER_ALLOW_ALL_USERS=") {
        $content += "`nSKYTOWER_ALLOW_ALL_USERS=true"
    }
    if ($content -notmatch "(?m)^SKYTOWER_PRINT_PAIR_CODE=") {
        $content += "`nSKYTOWER_PRINT_PAIR_CODE=0"
    }

    Set-Content $envFile $content.TrimStart()

    if ($changed) {
        Write-Success "Skytower 설정 저장됨: $envFile"
    } else {
        Write-Warn "토큰/URL을 나중에 $envFile 에 직접 추가하세요:"
        Write-Host "  SKYTOWER_TOKEN=agentId:rawToken" -ForegroundColor Yellow
        Write-Host "  SKYTOWER_URL=https://relay.example.com" -ForegroundColor Yellow
    }
}

# ============================================================================
# 동작 확인
# ============================================================================

# Hermes 가 실제로 도는 파이썬에서 확인한다. adapter 는 socketio 를 함수 안에서
# import 하므로 register import 만으로는 의존성 누락을 잡지 못한다 — 따로 본다.
function Test-RuntimeImport {
    param([string]$InstallDir, [string]$RuntimePython)

    $code = @"
import sys
sys.path.insert(0, r'$InstallDir')
try:
    import socketio
    from plugins.platforms.skytower import register
    print('OK')
except Exception as e:
    print('FAIL:', e)
"@
    $result = & $RuntimePython -c $code 2>$null
    return [bool]($result -match "^OK")
}

function Test-Plugin {
    param([string]$InstallDir, [string]$HermesHomeDir, [string]$HermesCli)

    Write-Info "플러그인 로드 확인 중..."
    $runtimePython = Find-RuntimePython -InstallDir $InstallDir -HermesHomeDir $HermesHomeDir
    if (-not $runtimePython) {
        Write-Warn "Hermes 실행 환경을 찾지 못해 확인을 건너뜁니다"
        return
    }
    if (Test-RuntimeImport -InstallDir $InstallDir -RuntimePython $runtimePython) {
        Write-Success "플러그인 로드 확인 완료 ($runtimePython)"
        return
    }
    # 이미 활성화돼 있으면 enable 이 아무것도 하지 않는다 — PM 동기화로 의존성을 채운다.
    if ((Test-PmManaged -InstallDir $InstallDir -HermesHomeDir $HermesHomeDir) -and $HermesCli) {
        Write-Info "python-socketio 가 없습니다 — Hermes 환경 동기화 중 (hermes pm install)..."
        $env:HERMES_HOME = $HermesHomeDir
        & $HermesCli pm install
        $pmOk = ($LASTEXITCODE -eq 0)
        $runtimePython = Find-RuntimePython -InstallDir $InstallDir -HermesHomeDir $HermesHomeDir
        if ($pmOk -and $runtimePython -and (Test-RuntimeImport -InstallDir $InstallDir -RuntimePython $runtimePython)) {
            Write-Success "플러그인 로드 확인 완료 ($runtimePython)"
            return
        }
    }
    Write-Warn "플러그인 로드 확인 실패 — python-socketio 가 Hermes 환경($runtimePython)에 없습니다"
    Write-Warn "  hermes plugins enable skytower-platform 후 hermes update 를 실행하세요"
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
$HermesCli    = Find-HermesCli -InstallDir $InstallDir -HermesHomeDir $HermesHomeResolved
$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptDir) { $ScriptDir = $PWD.Path }

Install-PluginFiles -InstallDir $InstallDir -ScriptDir $ScriptDir
Install-Deps        -InstallDir $InstallDir -HermesHomeDir $HermesHomeResolved -PythonExe $PythonExe -UvExe $UvExe
Enable-Plugin       -HermesCli $HermesCli -HermesHomeDir $HermesHomeResolved
Configure-Env       -HermesHomeDir $HermesHomeResolved
Test-Plugin         -InstallDir $InstallDir -HermesHomeDir $HermesHomeResolved -HermesCli $HermesCli

Write-SuccessBanner -InstallDir $InstallDir -HermesHomeDir $HermesHomeResolved
