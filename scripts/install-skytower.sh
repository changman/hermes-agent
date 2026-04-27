#!/bin/bash
# ============================================================================
# Hermes Agent + Skytower Installer
# ============================================================================
# Skytower 연동이 포함된 Hermes Agent 설치 스크립트.
# changman/hermes-agent 포크의 skytower 브랜치를 설치합니다.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --skip-setup
#   curl -fsSL ... | bash -s -- --token "agentId:rawToken" --url "https://relay.example.com"
#
# ============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'

# ── Skytower fork configuration ──────────────────────────────────────────────
REPO_URL_SSH="git@github.com:changman/hermes-agent.git"
REPO_URL_HTTPS="https://github.com/changman/hermes-agent.git"
BRANCH="skytower"

# ── Hermes configuration ──────────────────────────────────────────────────────
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [ -n "${HERMES_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$HERMES_INSTALL_DIR"
    INSTALL_DIR_EXPLICIT=true
else
    INSTALL_DIR=""
    INSTALL_DIR_EXPLICIT=false
fi
PYTHON_VERSION="3.11"
NODE_VERSION="22"
ROOT_FHS_LAYOUT=false

# Options
USE_VENV=true
RUN_SETUP=true

# Skytower options (can be passed as args or set via env)
SKYTOWER_TOKEN="${SKYTOWER_TOKEN:-}"
SKYTOWER_URL="${SKYTOWER_URL:-}"
SKYTOWER_PROCESS_MODE="${SKYTOWER_PROCESS_MODE:-false}"

# Detect non-interactive mode
if [ -t 0 ]; then
    IS_INTERACTIVE=true
else
    IS_INTERACTIVE=false
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-venv)        USE_VENV=false;            shift ;;
        --skip-setup)     RUN_SETUP=false;           shift ;;
        --branch)         BRANCH="$2";               shift 2 ;;
        --dir)            INSTALL_DIR="$2"; INSTALL_DIR_EXPLICIT=true; shift 2 ;;
        --hermes-home)    HERMES_HOME="$2";           shift 2 ;;
        --token)          SKYTOWER_TOKEN="$2";        shift 2 ;;
        --url)            SKYTOWER_URL="$2";          shift 2 ;;
        --process-mode)   SKYTOWER_PROCESS_MODE=true; shift ;;
        -h|--help)
            echo "Hermes Agent + Skytower Installer"
            echo ""
            echo "Usage: install-skytower.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --token TOKEN     Skytower agent token (agentId:rawToken)"
            echo "  --url URL         Skytower Relay URL (https://relay.example.com)"
            echo "  --process-mode    Enable per-conversation process isolation (Level 2)"
            echo "  --skip-setup      Skip interactive setup wizard"
            echo "  --no-venv         Don't create virtual environment"
            echo "  --branch NAME     Git branch (default: skytower)"
            echo "  --dir PATH        Installation directory"
            echo "  --hermes-home PATH  Data directory (default: ~/.hermes)"
            echo ""
            echo "Environment variables:"
            echo "  SKYTOWER_TOKEN    Skytower agent token"
            echo "  SKYTOWER_URL      Skytower Relay URL"
            echo "  SKYTOWER_PROCESS_MODE  Enable process isolation (true/false)"
            echo ""
            echo "Example:"
            echo "  curl -fsSL https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.sh | \\"
            echo "    bash -s -- --token 'agentId:rawToken' --url 'https://relay.example.com'"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================================
# Helper functions (기존 install.sh와 동일)
# ============================================================================

print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│        ⚕ Hermes Agent + Skytower Installer              │"
    echo "├─────────────────────────────────────────────────────────┤"
    echo "│  Skytower Relay 연동 포함 버전 (changman/hermes-agent)  │"
    echo "│  Branch: skytower                                        │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
}

log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

prompt_yes_no() {
    local question="$1"
    local default="${2:-yes}"
    local prompt_suffix answer=""
    case "$default" in
        [yY]*|[tT]*|1) prompt_suffix="[Y/n]" ;;
        *)              prompt_suffix="[y/N]" ;;
    esac
    if [ "$IS_INTERACTIVE" = true ]; then
        read -r -p "$question $prompt_suffix " answer || answer=""
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s %s " "$question" "$prompt_suffix" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=""
    fi
    answer="${answer#"${answer%%[![:space:]]*}"}"
    answer="${answer%"${answer##*[![:space:]]}"}"
    if [ -z "$answer" ]; then
        case "$default" in [yY]*|[tT]*|1) return 0 ;; *) return 1 ;; esac
    fi
    case "$answer" in [yY]*) return 0 ;; *) return 1 ;; esac
}

is_termux() {
    [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *"com.termux/files/usr"* ]]
}

resolve_install_layout() {
    if [ "$INSTALL_DIR_EXPLICIT" = true ]; then
        log_info "Install directory: $INSTALL_DIR (explicit)"
        return 0
    fi
    if is_termux; then
        INSTALL_DIR="$HERMES_HOME/hermes-agent"
        return 0
    fi
    if [ "$OS" = "linux" ] && [ "$(id -u)" -eq 0 ]; then
        if [ -d "$HERMES_HOME/hermes-agent/.git" ]; then
            INSTALL_DIR="$HERMES_HOME/hermes-agent"
            log_info "Existing install detected at $INSTALL_DIR — keeping legacy layout"
            return 0
        fi
        INSTALL_DIR="/usr/local/lib/hermes-agent"
        ROOT_FHS_LAYOUT=true
        log_info "Root install on Linux — using FHS layout"
        return 0
    fi
    INSTALL_DIR="$HERMES_HOME/hermes-agent"
}

get_command_link_dir() {
    if is_termux && [ -n "${PREFIX:-}" ]; then echo "$PREFIX/bin"
    elif [ "$ROOT_FHS_LAYOUT" = true ]; then echo "/usr/local/bin"
    else echo "$HOME/.local/bin"
    fi
}

get_hermes_command_path() {
    local link_dir; link_dir="$(get_command_link_dir)"
    [ -x "$link_dir/hermes" ] && echo "$link_dir/hermes" || echo "hermes"
}

# ============================================================================
# System detection
# ============================================================================

detect_os() {
    case "$(uname -s)" in
        Linux*)
            if is_termux; then
                OS="android"; DISTRO="termux"
            else
                OS="linux"
                DISTRO="$(. /etc/os-release 2>/dev/null && echo "${ID:-unknown}" || echo "unknown")"
            fi ;;
        Darwin*) OS="macos"; DISTRO="macos" ;;
        CYGWIN*|MINGW*|MSYS*)
            log_error "Windows는 지원되지 않습니다. WSL2를 사용해주세요."
            exit 1 ;;
        *) OS="unknown"; DISTRO="unknown" ;;
    esac
    log_success "OS 감지: $OS ($DISTRO)"
}

# ============================================================================
# Dependencies
# ============================================================================

install_uv() {
    if [ "$DISTRO" = "termux" ]; then UV_CMD=""; return 0; fi
    if command -v uv &>/dev/null; then
        UV_CMD="uv"; log_success "uv found ($(uv --version 2>/dev/null))"; return 0
    fi
    for loc in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$loc" ]; then UV_CMD="$loc"; log_success "uv found at $loc"; return 0; fi
    done
    log_info "uv 설치 중..."
    curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null
    for loc in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$loc" ]; then UV_CMD="$loc"; log_success "uv 설치 완료"; return 0; fi
    done
    log_error "uv 설치 실패. https://docs.astral.sh/uv/ 를 참고해주세요."
    exit 1
}

check_python() {
    if [ "$DISTRO" = "termux" ]; then
        PYTHON_PATH="$(command -v python 2>/dev/null || true)"
        [ -z "$PYTHON_PATH" ] && pkg install -y python >/dev/null
        PYTHON_PATH="$(command -v python)"
        log_success "Python: $($PYTHON_PATH --version 2>/dev/null)"
        return 0
    fi
    log_info "Python $PYTHON_VERSION 확인 중..."
    if PYTHON_PATH="$($UV_CMD python find "$PYTHON_VERSION" 2>/dev/null)"; then
        log_success "Python 발견: $($PYTHON_PATH --version 2>/dev/null)"
    else
        log_info "Python $PYTHON_VERSION 설치 중..."
        $UV_CMD python install "$PYTHON_VERSION"
        PYTHON_PATH="$($UV_CMD python find "$PYTHON_VERSION")"
        log_success "Python 설치 완료: $($PYTHON_PATH --version 2>/dev/null)"
    fi
}

check_git() {
    command -v git &>/dev/null && { log_success "Git $(git --version | awk '{print $3}') found"; return 0; }
    log_error "Git을 찾을 수 없습니다."
    case "$OS" in
        linux) case "$DISTRO" in ubuntu|debian) log_info "  sudo apt install git" ;; *) log_info "  패키지 매니저로 git 설치" ;; esac ;;
        macos) log_info "  xcode-select --install  또는  brew install git" ;;
    esac
    exit 1
}

check_node() {
    log_info "Node.js 확인 중..."
    if command -v node &>/dev/null; then
        log_success "Node.js $(node --version) found"; HAS_NODE=true; return 0
    fi
    HAS_NODE=false
    log_warn "Node.js를 찾을 수 없습니다 (브라우저 도구가 제한됩니다)"
}

install_system_packages() {
    HAS_RIPGREP=false; HAS_FFMPEG=false
    command -v rg &>/dev/null    && HAS_RIPGREP=true && log_success "ripgrep found"
    command -v ffmpeg &>/dev/null && HAS_FFMPEG=true  && log_success "ffmpeg found"
    [ "$HAS_RIPGREP" = true ] && [ "$HAS_FFMPEG" = true ] && return 0

    if [ "$OS" = "linux" ]; then
        local pkgs=()
        [ "$HAS_RIPGREP" = false ] && pkgs+=("ripgrep")
        [ "$HAS_FFMPEG"  = false ] && pkgs+=("ffmpeg")
        if [ ${#pkgs[@]} -gt 0 ]; then
            case "$DISTRO" in
                ubuntu|debian)
                    if command -v sudo &>/dev/null && sudo -n true 2>/dev/null; then
                        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}" >/dev/null 2>&1 && \
                            log_success "${pkgs[*]} installed" || log_warn "${pkgs[*]} 설치 실패 (수동 설치 필요)"
                    else
                        log_warn "sudo 없이 시스템 패키지를 설치할 수 없습니다: sudo apt install ${pkgs[*]}"
                    fi ;;
            esac
        fi
    fi
}

# ============================================================================
# Installation
# ============================================================================

clone_repo() {
    log_info "설치 디렉터리: $INSTALL_DIR"
    log_info "저장소: $REPO_URL_HTTPS (브랜치: $BRANCH)"

    if [ -d "$INSTALL_DIR/.git" ]; then
        log_info "기존 설치 발견 — 업데이트 중..."
        cd "$INSTALL_DIR"
        if [ -n "$(git status --porcelain)" ]; then
            git stash push --include-untracked -m "hermes-skytower-autostash-$(date -u +%Y%m%d-%H%M%S)"
        fi
        git fetch origin
        git checkout "$BRANCH"
        git pull --ff-only origin "$BRANCH"
    else
        log_info "SSH로 클론 시도 중..."
        if GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5" \
           git clone --branch "$BRANCH" "$REPO_URL_SSH" "$INSTALL_DIR" 2>/dev/null; then
            log_success "SSH 클론 완료"
        else
            rm -rf "$INSTALL_DIR" 2>/dev/null
            log_info "HTTPS로 클론 중..."
            git clone --branch "$BRANCH" "$REPO_URL_HTTPS" "$INSTALL_DIR"
            log_success "HTTPS 클론 완료"
        fi
    fi

    cd "$INSTALL_DIR"
    log_success "저장소 준비 완료"
}

setup_venv() {
    [ "$USE_VENV" = false ] && return 0
    log_info "가상환경 생성 중 (Python $PYTHON_VERSION)..."
    [ -d "venv" ] && rm -rf venv
    if [ "$DISTRO" = "termux" ]; then
        "$PYTHON_PATH" -m venv venv
    else
        $UV_CMD venv venv --python "$PYTHON_VERSION"
    fi
    log_success "가상환경 준비 완료"
}

install_deps() {
    log_info "의존성 설치 중..."

    if [ "$USE_VENV" = true ]; then
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi

    if [ "$DISTRO" = "termux" ]; then
        "$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip >/dev/null
        "$INSTALL_DIR/venv/bin/python" -m pip install -e '.[termux]' -c constraints-termux.txt || \
            "$INSTALL_DIR/venv/bin/python" -m pip install -e '.'
    else
        # 빌드 도구 설치 (Ubuntu/Debian)
        if [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
            if command -v sudo &>/dev/null && sudo -n true 2>/dev/null; then
                sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
                    build-essential python3-dev libffi-dev >/dev/null 2>&1 || true
            fi
        fi
        if ! $UV_CMD pip install -e ".[all]" 2>/dev/null; then
            log_warn ".[all] 설치 실패, 기본 설치로 fallback..."
            $UV_CMD pip install -e "." || { log_error "패키지 설치 실패"; exit 1; }
        fi
    fi

    # ── Skytower 전용 의존성 ──────────────────────────────────────────────────
    log_info "Skytower 의존성 설치 중 (python-socketio, psutil)..."
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/venv/bin/python" -m pip install \
            "python-socketio[asyncio_client]>=5.11" "psutil>=5.9" -q
    else
        pip install "python-socketio[asyncio_client]>=5.11" "psutil>=5.9" -q
    fi
    log_success "Skytower 의존성 설치 완료"

    log_success "모든 의존성 설치 완료"
}

setup_path() {
    log_info "hermes 커맨드 설정 중..."
    HERMES_BIN="$INSTALL_DIR/venv/bin/hermes"
    [ "$USE_VENV" = false ] && HERMES_BIN="$(which hermes 2>/dev/null || echo "")"

    if [ ! -x "$HERMES_BIN" ]; then
        log_warn "hermes 엔트리포인트를 찾을 수 없습니다: $HERMES_BIN"
        return 0
    fi

    local link_dir; link_dir="$(get_command_link_dir)"
    mkdir -p "$link_dir"
    ln -sf "$HERMES_BIN" "$link_dir/hermes"
    log_success "hermes → $link_dir/hermes 심볼릭 링크 생성"

    if ! echo "$PATH" | tr ':' '\n' | grep -q "^$link_dir$"; then
        for cfg in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
            [ -f "$cfg" ] || continue
            if ! grep -q '\.local/bin' "$cfg" 2>/dev/null; then
                echo "" >> "$cfg"
                echo "# Hermes Agent" >> "$cfg"
                echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$cfg"
                log_success "PATH 추가: $cfg"
            fi
        done
    fi
    export PATH="$link_dir:$PATH"
    log_success "hermes 커맨드 준비 완료"
}

copy_config_templates() {
    log_info "설정 파일 준비 중..."
    mkdir -p "$HERMES_HOME"/{cron,sessions,logs,pairing,hooks,memories,skills}

    [ ! -f "$HERMES_HOME/.env" ] && \
        { [ -f "$INSTALL_DIR/.env.example" ] && cp "$INSTALL_DIR/.env.example" "$HERMES_HOME/.env" || touch "$HERMES_HOME/.env"; }
    [ ! -f "$HERMES_HOME/config.yaml" ] && \
        [ -f "$INSTALL_DIR/cli-config.yaml.example" ] && \
        cp "$INSTALL_DIR/cli-config.yaml.example" "$HERMES_HOME/config.yaml"

    log_success "설정 디렉터리 준비 완료: $HERMES_HOME/"

    if "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/tools/skills_sync.py" 2>/dev/null; then
        log_success "스킬 동기화 완료"
    fi
}

# ============================================================================
# Skytower 전용: .env에 설정 주입
# ============================================================================

configure_skytower() {
    local env_file="$HERMES_HOME/.env"
    local changed=false

    log_info "Skytower 설정 구성 중..."

    # 인터랙티브 모드에서 토큰/URL 입력 받기
    if [ "$IS_INTERACTIVE" = true ] || [ -e /dev/tty ]; then
        local tty_input=/dev/tty
        [ "$IS_INTERACTIVE" = false ] && [ ! -e /dev/tty ] && tty_input=/dev/null

        if [ -z "$SKYTOWER_TOKEN" ]; then
            printf "\n${CYAN}→${NC} Skytower 에이전트 토큰 입력 (agentId:rawToken, 없으면 Enter): " > /dev/tty
            IFS= read -r SKYTOWER_TOKEN < "$tty_input" || SKYTOWER_TOKEN=""
            SKYTOWER_TOKEN="${SKYTOWER_TOKEN#"${SKYTOWER_TOKEN%%[![:space:]]*}"}"
        fi

        if [ -z "$SKYTOWER_URL" ]; then
            printf "${CYAN}→${NC} Skytower Relay URL 입력 (예: https://relay.example.com, 없으면 Enter): " > /dev/tty
            IFS= read -r SKYTOWER_URL < "$tty_input" || SKYTOWER_URL=""
            SKYTOWER_URL="${SKYTOWER_URL#"${SKYTOWER_URL%%[![:space:]]*}"}"
        fi
    fi

    # .env 파일에 Skytower 설정 추가/업데이트
    local skytower_block=""

    if [ -n "$SKYTOWER_TOKEN" ]; then
        # 기존 항목 제거 후 재추가
        if grep -q "^SKYTOWER_TOKEN=" "$env_file" 2>/dev/null; then
            sed -i "s|^SKYTOWER_TOKEN=.*|SKYTOWER_TOKEN=$SKYTOWER_TOKEN|" "$env_file"
        else
            skytower_block+="SKYTOWER_TOKEN=$SKYTOWER_TOKEN\n"
        fi
        changed=true
    fi

    if [ -n "$SKYTOWER_URL" ]; then
        if grep -q "^SKYTOWER_URL=" "$env_file" 2>/dev/null; then
            sed -i "s|^SKYTOWER_URL=.*|SKYTOWER_URL=$SKYTOWER_URL|" "$env_file"
        else
            skytower_block+="SKYTOWER_URL=$SKYTOWER_URL\n"
        fi
        changed=true
    fi

    # 기본 Skytower 설정 추가 (없는 경우)
    if ! grep -q "^SKYTOWER_ALLOW_ALL_USERS=" "$env_file" 2>/dev/null; then
        skytower_block+="SKYTOWER_ALLOW_ALL_USERS=true\n"
        skytower_block+="SKYTOWER_PRINT_PAIR_CODE=0\n"
    fi

    if [ "$SKYTOWER_PROCESS_MODE" = "true" ] || [ "$SKYTOWER_PROCESS_MODE" = "1" ]; then
        if ! grep -q "^SKYTOWER_PROCESS_MODE=" "$env_file" 2>/dev/null; then
            skytower_block+="SKYTOWER_PROCESS_MODE=true\n"
        fi
    fi

    # .env에 Skytower 섹션 추가
    if [ -n "$skytower_block" ]; then
        {
            echo ""
            echo "# Skytower Relay"
            printf "%b" "$skytower_block"
        } >> "$env_file"
        changed=true
    fi

    if [ "$changed" = true ]; then
        log_success "Skytower 설정이 $env_file 에 저장됐습니다"
    else
        log_info "Skytower 토큰/URL을 나중에 $env_file 에 직접 추가해주세요:"
        log_info "  SKYTOWER_TOKEN=agentId:rawToken"
        log_info "  SKYTOWER_URL=https://relay.example.com"
    fi
}

# ============================================================================
# Setup wizard
# ============================================================================

run_setup_wizard() {
    [ "$RUN_SETUP" = false ] && { log_info "셋업 위저드 건너뜀 (--skip-setup)"; return 0; }
    [ ! -e /dev/tty ] && { log_info "터미널 없음 — 셋업 위저드 건너뜀. 나중에 'hermes setup' 실행"; return 0; }
    echo ""
    log_info "셋업 위저드 시작 중..."
    cd "$INSTALL_DIR"
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/venv/bin/python" -m hermes_cli.main setup < /dev/tty
    else
        python -m hermes_cli.main setup < /dev/tty
    fi
}

maybe_start_gateway() {
    local env_file="$HERMES_HOME/.env"
    [ ! -f "$env_file" ] && return 0

    # Skytower 토큰이 있는지 확인
    local skytower_token_val
    skytower_token_val=$(grep "^SKYTOWER_TOKEN=" "$env_file" 2>/dev/null | cut -d'=' -f2-)
    [ -z "$skytower_token_val" ] && return 0

    echo ""
    log_info "Skytower 토큰이 설정됐습니다!"
    log_info "게이트웨이를 서비스로 등록하면 서버 재시작 후에도 자동 실행됩니다."

    [ ! -e /dev/tty ] && { log_info "게이트웨이 서비스는 나중에 'hermes gateway install'로 설정하세요"; return 0; }

    echo ""
    if prompt_yes_no "게이트웨이를 서비스로 설치하시겠습니까?" "yes"; then
        local hermes_cmd; hermes_cmd="$(get_hermes_command_path)"
        if command -v systemctl &>/dev/null && [ "$DISTRO" != "termux" ]; then
            log_info "systemd 서비스 설치 중..."
            if $hermes_cmd gateway install 2>/dev/null; then
                log_success "게이트웨이 서비스 설치 완료"
                $hermes_cmd gateway start 2>/dev/null && \
                    log_success "게이트웨이 시작! Skytower가 온라인 상태입니다." || \
                    log_warn "서비스는 설치됐지만 시작 실패: hermes gateway start"
            else
                log_warn "systemd 설치 실패. 수동 실행: hermes gateway"
            fi
        else
            log_info "백그라운드로 게이트웨이 시작 중..."
            nohup $hermes_cmd gateway > "$HERMES_HOME/logs/gateway.log" 2>&1 &
            local gw_pid=$!
            log_success "게이트웨이 시작됨 (PID $gw_pid). 로그: ~/.hermes/logs/gateway.log"
        fi
    else
        log_info "나중에 'hermes gateway install'로 서비스를 설치하거나 'hermes gateway'로 직접 실행하세요"
    fi
}

print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│            ✓ Hermes + Skytower 설치 완료!               │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}📁 파일 위치:${NC}"
    echo ""
    echo -e "   ${YELLOW}설정:${NC}       $HERMES_HOME/config.yaml"
    echo -e "   ${YELLOW}API Keys:${NC}   $HERMES_HOME/.env"
    echo -e "   ${YELLOW}코드:${NC}       $INSTALL_DIR"
    echo -e "   ${YELLOW}홈채널:${NC}     $HERMES_HOME/skytower_home_channels.json"
    echo ""
    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}🚀 시작 방법:${NC}"
    echo ""
    echo -e "   ${GREEN}hermes gateway${NC}              게이트웨이 시작"
    echo -e "   ${GREEN}hermes gateway install${NC}      systemd 서비스로 등록"
    echo -e "   ${GREEN}hermes setup${NC}                셋업 위저드 실행"
    echo ""
    echo -e "${CYAN}${BOLD}📡 Skytower 채팅 명령어:${NC}"
    echo ""
    echo -e "   ${GREEN}/chatid${NC}                     현재 대화 JID 확인"
    echo -e "   ${GREEN}/sethome${NC}                    현재 대화를 홈 채널로 설정"
    echo -e "   ${GREEN}/process start <conv_id>${NC}    대화에 격리 프로세스 할당"
    echo -e "   ${GREEN}/process list${NC}               실행 중인 프로세스 목록"
    echo ""
    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""

    local skytower_token_val
    skytower_token_val=$(grep "^SKYTOWER_TOKEN=" "$HERMES_HOME/.env" 2>/dev/null | cut -d'=' -f2-)
    if [ -z "$skytower_token_val" ]; then
        echo -e "${YELLOW}⚠  Skytower 토큰이 아직 설정되지 않았습니다.${NC}"
        echo ""
        echo "   에이전트 등록 후 ~/.hermes/.env 에 추가:"
        echo ""
        echo "   SKYTOWER_TOKEN=agentId:rawToken"
        echo "   SKYTOWER_URL=https://relay.example.com"
        echo ""
    fi

    echo -e "${YELLOW}⚡ shell을 재로드하세요:${NC}"
    local login_shell; login_shell="$(basename "${SHELL:-/bin/bash}")"
    case "$login_shell" in
        zsh)  echo "   source ~/.zshrc"  ;;
        fish) echo "   source ~/.config/fish/config.fish" ;;
        *)    echo "   source ~/.bashrc" ;;
    esac
    echo ""
}

# ============================================================================
# Main
# ============================================================================

main() {
    print_banner
    detect_os
    resolve_install_layout
    install_uv
    check_python
    check_git
    check_node
    install_system_packages

    clone_repo
    setup_venv
    install_deps
    setup_path
    copy_config_templates
    configure_skytower
    run_setup_wizard
    maybe_start_gateway

    print_success
}

main
