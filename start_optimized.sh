#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  优化启动脚本 - 适配 Z.ai 容器环境
#
#  核心改进：
#  - 使用 setsid 创建独立进程组，避免被容器清理
#  - 所有服务以独立进程运行，不受父进程退出影响
#  - 支持 timeout 控制，适合容器环境
#
#  用法：
#    bash start_optimized.sh          # 启动服务
#    bash start_optimized.sh status   # 查看状态
#    bash start_optimized.sh stop     # 停止服务
# ═══════════════════════════════════════════════════════════════

set -o pipefail

WORK_DIR="/home/z/my-project"
LOG_FILE="$WORK_DIR/seamless.log"
PID_FILE="$WORK_DIR/watchdog.pid"
PROXY_SCRIPT="$WORK_DIR/proxy.py"
WATCHDOG_SCRIPT="$WORK_DIR/watchdog.py"
CONFIG_ENV="$WORK_DIR/config.env"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_msg() {
    local ts=$(date +%H:%M:%S.%3N 2>/dev/null || date +%H:%M:%S)
    echo "[$ts] $1" | tee -a "$LOG_FILE" 2>/dev/null
}

info() { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; }

# ═══ 加载配置 ═══
load_config() {
    if [ -f "$CONFIG_ENV" ]; then
        source "$CONFIG_ENV" 2>/dev/null
    fi
    export NGROK_AUTHTOKEN="${NGROK_AUTHTOKEN:-}"
}

# ═══ 检查服务状态 ═══
check_proxy() {
    curl -s -m 2 http://127.0.0.1:8082/_ping 2>/dev/null | grep -q "pong"
}

check_ngrok() {
    pgrep -x ngrok >/dev/null 2>&1 && \
    curl -s -m 2 http://127.0.0.1:4040/api/tunnels 2>/dev/null | grep -q '"public_url"'
}

get_ngrok_url() {
    curl -s -m 2 http://127.0.0.1:4040/api/tunnels 2>/dev/null | \
    python3 -c "import sys,json; t=json.load(sys.stdin).get('tunnels',[]); print(t[0]['public_url'] if t else 'N/A')" 2>/dev/null || echo "N/A"
}

# ═══ 启动服务 ═══
start_services() {
    log_msg "════════════════════════════════════════"
    log_msg "🚀 启动 API Tunnel 服务..."
    log_msg "════════════════════════════════════════"

    # 加载配置
    load_config

    # 检查是否已在运行
    if check_proxy && check_ngrok; then
        info "服务已在运行"
        show_status
        return 0
    fi

    # 配置 ngrok authtoken
    if [ -n "$NGROK_AUTHTOKEN" ]; then
        ngrok config add-authtoken "$NGROK_AUTHTOKEN" 2>/dev/null
        info "ngrok authtoken 已配置"
    fi

    # 启动 proxy（独立进程组）
    if ! check_proxy; then
        log_msg "📦 启动 Python 代理..."
        pkill -9 -f "python3.*proxy.py" 2>/dev/null || true
        sleep 0.3
        
        # 使用 setsid 创建独立进程组
        setsid env SELF_RESTART=1 python3 "$PROXY_SCRIPT" --no-daemon \
            >> "$LOG_FILE" 2>&1 &
        
        # 等待 proxy 就绪
        for i in $(seq 1 25); do
            sleep 0.2
            if check_proxy; then
                info "代理启动成功 (端口 8082)"
                break
            fi
        done
        
        if ! check_proxy; then
            error "代理启动失败"
            return 1
        fi
    else
        info "代理已在运行"
    fi

    # 启动 ngrok（独立进程组）
    if ! check_ngrok; then
        log_msg "📦 启动 ngrok 隧道..."
        pkill -9 -f ngrok 2>/dev/null || true
        sleep 0.5
        
        # 使用 setsid 创建独立进程组
        setsid ngrok http http://127.0.0.1:8082 \
            --log=stdout --log-format=logfmt \
            >> "$LOG_FILE" 2>&1 &
        
        # 等待 ngrok 就绪
        for i in $(seq 1 100); do
            sleep 0.2
            if check_ngrok; then
                NGROK_URL=$(get_ngrok_url)
                info "ngrok 隧道建立成功: $NGROK_URL"
                break
            fi
        done
        
        if ! check_ngrok; then
            error "ngrok 隧道建立失败"
            return 1
        fi
    else
        info "ngrok 已在运行"
    fi

    # 启动 watchdog（独立进程组）
    log_msg "📦 启动 watchdog 守护进程..."
    setsid python3 "$WATCHDOG_SCRIPT" --holder "optimized" \
        >> "$LOG_FILE" 2>&1 &
    
    sleep 1
    info "watchdog 已启动"
    
    show_status
    return 0
}

# ═══ 显示状态 ═══
show_status() {
    echo ""
    echo "═══════════════════════════════════════════════════"
    
    PROXY_OK=$(check_proxy && echo "✅" || echo "❌")
    NGROK_OK=$(check_ngrok && echo "✅" || echo "❌")
    NGROK_URL=$(get_ngrok_url)
    
    echo -e "  ${CYAN}服务状态：${NC}"
    echo "  代理 (8082) : $PROXY_OK"
    echo "  ngrok 隧道  : $NGROK_OK"
    echo "  公网 URL    : $NGROK_URL"
    
    if check_proxy && check_ngrok; then
        echo ""
        echo -e "  ${GREEN}✅ 服务运行正常！${NC}"
        echo ""
        echo -e "  ${CYAN}API 调用信息：${NC}"
        echo "  Base URL : $NGROK_URL/v1"
        echo "  API Key  : Z.ai"
        echo ""
        echo -e "  ${CYAN}示例命令：${NC}"
        echo "  curl $NGROK_URL/v1/chat/completions \\"
        echo "    -H 'Authorization: Bearer Z.ai' \\"
        echo "    -H 'ngrok-skip-browser-warning: true' \\"
        echo "    -H 'Content-Type: application/json' \\"
        echo "    -d '{\"model\":\"glm-4-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"hello\"}]}'"
    fi
    
    echo "═══════════════════════════════════════════════════"
}

# ═══ 停止服务 ═══
stop_services() {
    log_msg "🛑 停止服务..."
    pkill -9 -f "python3.*watchdog" 2>/dev/null || true
    pkill -9 -f "python3.*proxy.py" 2>/dev/null || true
    pkill -9 -f ngrok 2>/dev/null || true
    rm -f "$PID_FILE"
    info "服务已停止"
}

# ═══ 测试 API ═══
test_api() {
    NGROK_URL=$(get_ngrok_url)
    if [ "$NGROK_URL" = "N/A" ]; then
        error "ngrok 隧道未建立"
        return 1
    fi
    
    log_msg "🧪 测试 API 调用..."
    RESPONSE=$(curl -s --http1.1 -m 30 "$NGROK_URL/v1/chat/completions" \
        -H "Authorization: Bearer Z.ai" \
        -H "ngrok-skip-browser-warning: true" \
        -H "Content-Type: application/json" \
        -d '{"model":"glm-4-flash","messages":[{"role":"user","content":"ping"}],"max_tokens":10,"stream":false}' 2>&1)
    
    if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'choices' in d" 2>/dev/null; then
        CONTENT=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])" 2>/dev/null)
        info "API 调用成功！AI 回复: $CONTENT"
        return 0
    else
        warn "API 调用异常: ${RESPONSE:0:200}"
        return 1
    fi
}

# ═══ 主入口 ═══
case "${1:-start}" in
    start)
        start_services
        ;;
    status)
        show_status
        ;;
    stop)
        stop_services
        ;;
    restart)
        stop_services
        sleep 2
        start_services
        ;;
    test)
        test_api
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|test}"
        exit 1
        ;;
esac
