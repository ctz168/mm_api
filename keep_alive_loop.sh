#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  持续保活脚本 - 适配 Z.ai 容器环境
#
#  设计理念：
#  - 使用 timeout 控制运行时间（280秒，留 20 秒缓冲）
#  - 持续监控服务状态，自动修复
#  - 适合在容器中通过持续 Bash 调用保持运行
#
#  用法：
#    timeout 280 bash keep_alive_loop.sh
# ═══════════════════════════════════════════════════════════════

WORK_DIR="/home/z/my-project"
LOG_FILE="$WORK_DIR/seamless.log"
PROXY_SCRIPT="$WORK_DIR/proxy.py"
WATCHDOG_SCRIPT="$WORK_DIR/watchdog.py"
CONFIG_ENV="$WORK_DIR/config.env"

# 加载配置
if [ -f "$CONFIG_ENV" ]; then
    source "$CONFIG_ENV" 2>/dev/null
fi

log_msg() {
    local ts=$(date +%H:%M:%S)
    echo "[$ts] [keepalive] $1" | tee -a "$LOG_FILE" 2>/dev/null
}

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

repair_proxy() {
    log_msg "🔧 修复 proxy..."
    pkill -9 -f "python3.*proxy.py" 2>/dev/null || true
    sleep 0.3
    setsid env SELF_RESTART=1 python3 "$PROXY_SCRIPT" --no-daemon >> "$LOG_FILE" 2>&1 &
    
    for i in $(seq 1 25); do
        sleep 0.2
        if check_proxy; then
            log_msg "✅ proxy 修复成功"
            return 0
        fi
    done
    log_msg "❌ proxy 修复失败"
    return 1
}

repair_ngrok() {
    log_msg "🔧 修复 ngrok..."
    pkill -9 -f ngrok 2>/dev/null || true
    sleep 0.5
    
    if [ -n "$NGROK_AUTHTOKEN" ]; then
        ngrok config add-authtoken "$NGROK_AUTHTOKEN" 2>/dev/null
    fi
    
    setsid ngrok http http://127.0.0.1:8082 --log=stdout --log-format=logfmt >> "$LOG_FILE" 2>&1 &
    
    for i in $(seq 1 100); do
        sleep 0.2
        if check_ngrok; then
            log_msg "✅ ngrok 修复成功: $(get_ngrok_url)"
            return 0
        fi
    done
    log_msg "❌ ngrok 修复失败"
    return 1
}

# ═══ 主循环 ═══
log_msg "════════════════════════════════════════"
log_msg "🚀 保活循环启动 (PID $$)"
log_msg "════════════════════════════════════════"

# 初始检查和修复
if ! check_proxy; then
    repair_proxy
fi

if ! check_ngrok; then
    repair_ngrok
fi

# 启动 watchdog（如果没在运行）
if ! pgrep -f "python3.*watchdog" >/dev/null 2>&1; then
    log_msg "📦 启动 watchdog..."
    setsid python3 "$WATCHDOG_SCRIPT" --holder "keepalive" >> "$LOG_FILE" 2>&1 &
fi

# 显示初始状态
log_msg "📊 初始状态: proxy=$(check_proxy && echo OK || echo FAIL) ngrok=$(check_ngrok && echo OK || echo FAIL) url=$(get_ngrok_url)"

# 持续监控循环
LOOP_COUNT=0
while true; do
    sleep 5
    LOOP_COUNT=$((LOOP_COUNT + 1))
    
    # 每 30 秒报告一次状态
    if [ $((LOOP_COUNT % 6)) -eq 0 ]; then
        PROXY_STATUS=$(check_proxy && echo "✅" || echo "❌")
        NGROK_STATUS=$(check_ngrok && echo "✅" || echo "❌")
        NGROK_URL=$(get_ngrok_url)
        log_msg "📊 [$LOOP_COUNT] proxy:$PROXY_STATUS ngrok:$NGROK_STATUS url:$NGROK_URL"
    fi
    
    # 检查并修复
    if ! check_proxy; then
        repair_proxy
    fi
    
    if ! check_ngrok; then
        repair_ngrok
    fi
done
