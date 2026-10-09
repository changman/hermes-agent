#!/bin/bash
# ============================================================================
# Skytower Channel Add-on Installer for Hermes Agent
# ============================================================================
# 공식 Hermes Agent에 Skytower 채널을 추가합니다.
# 먼저 공식 Hermes Agent가 설치되어 있어야 합니다.
#
# 1단계 — 공식 Hermes Agent 설치 (아직 설치 안 된 경우):
#   curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
#
# 2단계 — Skytower 채널 추가:
#   curl -fsSL https://raw.githubusercontent.com/changman/hermes-agent/skytower/scripts/install-skytower.sh | bash
#
# 또는 토큰/URL을 미리 지정:
#   curl -fsSL ... | bash -s -- --token "agentId:rawToken" --url "https://relay.example.com"
#
# ============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'
BOLD='\033[1m'
MAGENTA='\033[0;35m'

# Skytower plugin source (GitHub raw)
PLUGIN_REPO="https://raw.githubusercontent.com/changman/hermes-agent/skytower"
PLUGIN_FILES=(
    "plugins/platforms/skytower/__init__.py"
    "plugins/platforms/skytower/adapter.py"
    "plugins/platforms/skytower/confinement.py"
    "plugins/platforms/skytower/plugin.yaml"
    "gateway/platforms/skytower_files.py"
)

# Hermes home / install detection
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SKYTOWER_TOKEN="${SKYTOWER_TOKEN:-}"
SKYTOWER_URL="${SKYTOWER_URL:-}"

# Detect non-interactive mode
if [ -t 0 ]; then IS_INTERACTIVE=true; else IS_INTERACTIVE=false; fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --token)      SKYTOWER_TOKEN="$2"; shift 2 ;;
        --url)        SKYTOWER_URL="$2";   shift 2 ;;
        --hermes-home) HERMES_HOME="$2";   shift 2 ;;
        -h|--help)
            echo "Skytower Add-on Installer for Hermes Agent"
            echo ""
            echo "Usage: install-skytower.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --token TOKEN     Skytower agent token (agentId:rawToken)"
            echo "  --url URL         Skytower Relay URL (https://relay.example.com)"
            echo "  --hermes-home PATH  Hermes data directory (default: ~/.hermes)"
            echo ""
            echo "Environment variables:"
            echo "  SKYTOWER_TOKEN    Skytower agent token"
            echo "  SKYTOWER_URL      Skytower Relay URL"
            echo "  HERMES_HOME       Hermes data directory"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ============================================================================
# Helper functions
# ============================================================================

log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│           🗼 Skytower Channel Add-on Installer          │"
    echo "├─────────────────────────────────────────────────────────┤"
    echo "│  공식 Hermes Agent에 Skytower 채널을 추가합니다         │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
}

# ============================================================================
# Hermes 설치 위치 탐색
# ============================================================================

find_hermes_install() {
    local candidates=(
        "$HERMES_HOME/hermes-agent"
        "$HOME/.local/lib/hermes-agent"
        "/usr/local/lib/hermes-agent"
    )

    # hermes 심볼릭 링크로부터 역추적
    local hermes_bin
    hermes_bin="$(command -v hermes 2>/dev/null || true)"
    if [ -n "$hermes_bin" ] && [ -L "$hermes_bin" ]; then
        local real_bin; real_bin="$(readlink -f "$hermes_bin")"
        # venv/bin/hermes → <install_dir>/venv/bin/hermes
        local inferred; inferred="$(dirname "$(dirname "$(dirname "$real_bin")")")"
        [ -f "$inferred/pyproject.toml" ] && candidates=("$inferred" "${candidates[@]}")
    fi

    for dir in "${candidates[@]}"; do
        if [ -f "$dir/pyproject.toml" ] && [ -d "$dir/gateway" ]; then
            HERMES_INSTALL_DIR="$dir"
            log_success "Hermes 설치 위치: $HERMES_INSTALL_DIR"
            return 0
        fi
    done

    log_error "Hermes Agent 설치를 찾을 수 없습니다."
    echo ""
    echo "  먼저 공식 Hermes Agent를 설치해주세요:"
    echo ""
    echo "  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"
    echo ""
    exit 1
}

# ============================================================================
# Python / pip 실행기 탐색
# ============================================================================

find_python() {
    # venv가 있으면 venv pip 우선
    if [ -x "$HERMES_INSTALL_DIR/venv/bin/python" ]; then
        HERMES_PYTHON="$HERMES_INSTALL_DIR/venv/bin/python"
    elif command -v python3 &>/dev/null; then
        HERMES_PYTHON="python3"
    else
        HERMES_PYTHON="python"
    fi

    # uv 탐색
    UV_CMD=""
    for loc in "$(command -v uv 2>/dev/null)" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        [ -x "$loc" ] && UV_CMD="$loc" && break
    done

    # hermes CLI 탐색 (curl | bash 는 로그인 셸이 아니라 PATH 에 없을 수 있다)
    HERMES_CLI=""
    for loc in "$(command -v hermes 2>/dev/null)" "$HOME/.local/bin/hermes" "$HERMES_INSTALL_DIR/venv/bin/hermes"; do
        [ -n "$loc" ] && [ -x "$loc" ] && HERMES_CLI="$loc" && break
    done
}

# Hermes 가 실제로 도는 파이썬.
# PM(패키지 관리자) 을 쓰는 Hermes 는 설치 폴더의 venv 를 지우고
# $HERMES_HOME/installs/<key>/environments/<hash>/venv 에서 돈다. key 는 설치 폴더
# 실경로의 sha256 앞 16자 (pm/environments.py install_key), 선택된 환경은 그 facts.json 에 있다.
find_runtime_python() {
    RUNTIME_PYTHON=""
    if [ -x "$HERMES_INSTALL_DIR/venv/bin/python" ]; then
        RUNTIME_PYTHON="$HERMES_INSTALL_DIR/venv/bin/python"
        return 0
    fi
    local key facts env
    key="$("$HERMES_PYTHON" -c 'import hashlib, pathlib, sys; print(hashlib.sha256(str(pathlib.Path(sys.argv[1]).resolve()).encode()).hexdigest()[:16])' "$HERMES_INSTALL_DIR" 2>/dev/null || true)"
    for facts in ${key:+"$HERMES_HOME/installs/$key/facts.json"} "$HERMES_HOME"/installs/*/facts.json; do
        [ -f "$facts" ] || continue
        env="$(grep -o '"environment": *"[^"]*"' "$facts" | sed 's/.*"\([^"]*\)"$/\1/' | grep '/environments/' | head -1)"
        if [ -n "$env" ] && [ -x "$env/bin/python" ]; then
            RUNTIME_PYTHON="$env/bin/python"
            return 0
        fi
    done
    return 1
}

# PM 을 쓰는 Hermes 인가: 설치 폴더 venv 가 없고 installs/ 아래 기록이 있다.
is_pm_managed() {
    [ ! -d "$HERMES_INSTALL_DIR/venv" ] && compgen -G "$HERMES_HOME/installs/*/facts.json" >/dev/null
}

# ============================================================================
# 플러그인 파일 복사 (local dev) 또는 다운로드
# ============================================================================

install_plugin_files() {
    local target_dir="$HERMES_INSTALL_DIR/plugins/platforms/skytower"
    mkdir -p "$target_dir"

    # 이 스크립트가 이미 설치된 저장소 안에 있는지 확인 (로컬 개발 모드)
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
    local local_plugin_dir="$script_dir/../plugins/platforms/skytower"

    if [ -f "$local_plugin_dir/adapter.py" ]; then
        log_info "로컬 소스에서 플러그인 파일 복사 중..."
        cp -r "$local_plugin_dir/." "$target_dir/"

        # skytower_files.py (gateway/platforms/ 안에 있는 파일)
        local local_files_py="$script_dir/../gateway/platforms/skytower_files.py"
        if [ -f "$local_files_py" ]; then
            cp "$local_files_py" "$HERMES_INSTALL_DIR/gateway/platforms/skytower_files.py"
        fi
        log_success "플러그인 파일 복사 완료"
    else
        # 원격에서 다운로드
        log_info "GitHub에서 플러그인 파일 다운로드 중..."
        if ! command -v curl &>/dev/null; then
            log_error "curl을 찾을 수 없습니다. curl을 설치해주세요."
            exit 1
        fi

        for file in "${PLUGIN_FILES[@]}"; do
            local url="$PLUGIN_REPO/$file"
            local dest="$HERMES_INSTALL_DIR/$file"
            mkdir -p "$(dirname "$dest")"
            if curl -fsSL "$url" -o "$dest"; then
                log_success "다운로드: $file"
            else
                log_error "다운로드 실패: $file"
                exit 1
            fi
        done
    fi
}

# ============================================================================
# 의존성 설치
# ============================================================================

install_deps() {
    # PM 이 관리하는 환경에 손으로 깔면 다음 hermes update 때 사라진다.
    # 거기서는 plugin.yaml 의 python_dependencies 를 enable_plugin 이 PM 으로 설치한다.
    if is_pm_managed; then
        log_info "PM 관리 환경 — 의존성은 플러그인 활성화 때 Hermes 가 설치합니다"
        return 0
    fi

    log_info "Skytower 의존성 설치 중 (python-socketio, psutil)..."
    local pkgs=("python-socketio[asyncio_client]>=5.11,<6" "psutil>=5.9")

    if [ -n "$UV_CMD" ] && [ -d "$HERMES_INSTALL_DIR/venv" ]; then
        VIRTUAL_ENV="$HERMES_INSTALL_DIR/venv" "$UV_CMD" pip install "${pkgs[@]}" -q
    elif [ -x "$HERMES_INSTALL_DIR/venv/bin/pip" ]; then
        "$HERMES_INSTALL_DIR/venv/bin/pip" install "${pkgs[@]}" -q
    elif [ -x "$HERMES_INSTALL_DIR/venv/bin/python" ]; then
        "$HERMES_INSTALL_DIR/venv/bin/python" -m pip install "${pkgs[@]}" -q
    else
        $HERMES_PYTHON -m pip install "${pkgs[@]}" -q
    fi

    log_success "의존성 설치 완료"
}

# ============================================================================
# 플러그인 활성화 (plugins.enabled)
# ============================================================================

# PM 은 plugins.enabled 에 있는 플러그인의 python_dependencies 만 환경에 넣는다.
# 활성화하지 않으면 hermes update 가 환경을 다시 만들 때 python-socketio 가 빠진다.
# 구버전 Hermes 에서는 config.yaml 에 기록만 한다 (번들 플랫폼은 원래 자동 로드).
# stdin 을 /dev/null 로: curl | bash 에서 자식이 스크립트 본문을 읽어 가지 않게.
enable_plugin() {
    if [ -z "$HERMES_CLI" ]; then
        log_warn "hermes 명령을 찾지 못해 플러그인 활성화를 건너뜁니다"
        log_warn "  직접 실행하세요: hermes plugins enable skytower-platform"
        return 0
    fi
    log_info "플러그인 활성화 중 (hermes plugins enable skytower-platform)..."
    if HERMES_HOME="$HERMES_HOME" "$HERMES_CLI" plugins enable skytower-platform </dev/null; then
        log_success "플러그인 활성화 완료"
    else
        log_warn "플러그인 활성화 실패 — 직접 실행하세요: hermes plugins enable skytower-platform"
    fi
}

# ============================================================================
# .env 설정
# ============================================================================

configure_env() {
    local env_file="$HERMES_HOME/.env"
    [ -f "$env_file" ] || touch "$env_file"

    # 인터랙티브 모드에서 입력 받기
    if [ -e /dev/tty ]; then
        if [ -z "$SKYTOWER_TOKEN" ]; then
            printf "\n${CYAN}→${NC} Skytower 에이전트 토큰 입력 (agentId:rawToken, 없으면 Enter): " > /dev/tty
            IFS= read -r SKYTOWER_TOKEN < /dev/tty || SKYTOWER_TOKEN=""
            SKYTOWER_TOKEN="${SKYTOWER_TOKEN#"${SKYTOWER_TOKEN%%[![:space:]]*}"}"
        fi
        if [ -z "$SKYTOWER_URL" ]; then
            printf "${CYAN}→${NC} Skytower Relay URL 입력 (예: https://relay.example.com, 없으면 Enter): " > /dev/tty
            IFS= read -r SKYTOWER_URL < /dev/tty || SKYTOWER_URL=""
            SKYTOWER_URL="${SKYTOWER_URL#"${SKYTOWER_URL%%[![:space:]]*}"}"
        fi
    fi

    local changed=false

    _upsert_env() {
        local key="$1" val="$2"
        if grep -q "^${key}=" "$env_file" 2>/dev/null; then
            sed -i "s|^${key}=.*|${key}=${val}|" "$env_file"
        else
            printf "\n# Skytower Relay\n%s=%s\n" "$key" "$val" >> "$env_file"
        fi
    }

    if [ -n "$SKYTOWER_TOKEN" ]; then
        _upsert_env "SKYTOWER_TOKEN" "$SKYTOWER_TOKEN"
        changed=true
    fi
    if [ -n "$SKYTOWER_URL" ]; then
        _upsert_env "SKYTOWER_URL" "$SKYTOWER_URL"
        changed=true
    fi

    # 기본값 (없으면 추가)
    grep -q "^SKYTOWER_ALLOW_ALL_USERS=" "$env_file" 2>/dev/null || \
        printf "SKYTOWER_ALLOW_ALL_USERS=true\n" >> "$env_file"
    grep -q "^SKYTOWER_PRINT_PAIR_CODE=" "$env_file" 2>/dev/null || \
        printf "SKYTOWER_PRINT_PAIR_CODE=0\n" >> "$env_file"

    if [ "$changed" = true ]; then
        log_success "Skytower 설정 저장됨: $env_file"
    else
        log_warn "토큰/URL을 나중에 $env_file 에 직접 추가하세요:"
        log_warn "  SKYTOWER_TOKEN=agentId:rawToken"
        log_warn "  SKYTOWER_URL=https://relay.example.com"
    fi
}

# ============================================================================
# 동작 확인
# ============================================================================

# Hermes 가 실제로 도는 파이썬에서 확인한다. adapter 는 socketio 를 함수 안에서
# import 하므로 register import 만으로는 의존성 누락을 잡지 못한다 — 따로 본다.
_runtime_check() {
    "$RUNTIME_PYTHON" -c "
import sys
sys.path.insert(0, '$HERMES_INSTALL_DIR')
import socketio
from plugins.platforms.skytower import register
print('OK')
" 2>/dev/null | grep -q "OK"
}

verify_plugin() {
    log_info "플러그인 로드 확인 중..."
    if ! find_runtime_python; then
        log_warn "Hermes 실행 환경을 찾지 못해 확인을 건너뜁니다"
        return 0
    fi
    if _runtime_check; then
        log_success "플러그인 로드 확인 완료 ($RUNTIME_PYTHON)"
        return 0
    fi
    # 이미 활성화돼 있으면 enable 이 아무것도 하지 않는다 — PM 동기화로 의존성을 채운다.
    if is_pm_managed && [ -n "$HERMES_CLI" ]; then
        log_info "python-socketio 가 없습니다 — Hermes 환경 동기화 중 (hermes pm install)..."
        if HERMES_HOME="$HERMES_HOME" "$HERMES_CLI" pm install </dev/null && find_runtime_python && _runtime_check; then
            log_success "플러그인 로드 확인 완료 ($RUNTIME_PYTHON)"
            return 0
        fi
    fi
    log_warn "플러그인 로드 확인 실패 — python-socketio 가 Hermes 환경($RUNTIME_PYTHON)에 없습니다"
    log_warn "  hermes plugins enable skytower-platform 후 hermes update 를 실행하세요"
}

# ============================================================================
# 완료 메시지
# ============================================================================

print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│         ✓ Skytower 채널 추가 완료!                     │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}설치된 위치:${NC}"
    echo "  플러그인:  $HERMES_INSTALL_DIR/plugins/platforms/skytower/"
    echo "  설정:      $HERMES_HOME/.env"
    echo ""
    echo -e "${CYAN}${BOLD}시작 방법:${NC}"
    echo ""
    echo -e "  ${GREEN}hermes gateway${NC}              게이트웨이 시작"
    echo -e "  ${GREEN}hermes gateway install${NC}      systemd 서비스로 등록"
    echo ""
    echo -e "${CYAN}${BOLD}Skytower 채팅 명령어:${NC}"
    echo ""
    echo -e "  ${GREEN}/chatid${NC}                     현재 대화 JID 확인"
    echo -e "  ${GREEN}/sethome${NC}                    현재 대화를 홈 채널로 설정"
    echo -e "  ${GREEN}/skills${NC}                     사용 가능한 스킬 목록"
    echo ""

    local token_val
    token_val=$(grep "^SKYTOWER_TOKEN=" "$HERMES_HOME/.env" 2>/dev/null | cut -d'=' -f2-)
    if [ -z "$token_val" ]; then
        echo -e "${YELLOW}⚠  SKYTOWER_TOKEN이 아직 설정되지 않았습니다.${NC}"
        echo "   $HERMES_HOME/.env 에 추가 후 게이트웨이를 재시작하세요."
        echo ""
    fi
}

# ============================================================================
# Main
# ============================================================================

main() {
    print_banner
    find_hermes_install
    find_python
    install_plugin_files
    install_deps
    enable_plugin
    configure_env
    verify_plugin
    print_success
}

main
