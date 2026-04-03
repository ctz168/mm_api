#!/usr/bin/env python3
"""
多线程隧道保活守护进程 v4 — 自愈 + 无缝交接 + 防雪崩

核心改进（v4）：
  - proxy 自重启机制：proxy 启动时设置 SELF_RESTART=1，被 SIGTERM 时自动重启
  - 退出时服务独立性保证：watchdog 退出前确保 proxy/ngrok 作为独立进程存活
  - 快速健康预检：启动时先检查服务状态，全部正常则跳过初始化（1s 内退出也安全）
  - 防雪崩退避：连续修复失败时指数退避，避免无意义高频重启
  - ngrok 进程保护：使用 start_new_session + 独立进程组，不受 watchdog 退出影响

架构：
  ngrok(固定URL) → localhost:8082(Python代理) → 内网API
  线程：proxy_watcher(2s) + ngrok_watcher(3s) + stats_reporter(10s)

用法：
  python3 watchdog.py [--holder NAME] [--quick-check]
  --quick-check: 仅做健康检查 + 修复，不进入监控循环
"""

import http.client
import json
import threading
import time
import subprocess
import os
import sys
import signal
import argparse

# ═════════════ 从 config.env 读取配置 ═════════════
def load_config():
    config = {}
    search_paths = [
        '/home/z/my-project/config.env',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.env'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config.env'),
        os.environ.get('TUNNEL_CONFIG', ''),
    ]
    for path in search_paths:
        if path and os.path.isfile(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, value = line.partition('=')
                        key = key.strip()
                        value = value.strip().split('#')[0].strip()
                        config[key] = value
            break
    env_map = {
        'TUNNEL_API_HOST': 'API_HOST',
        'TUNNEL_API_PORT': 'API_PORT',
        'TUNNEL_X_TOKEN': 'X_TOKEN',
        'TUNNEL_X_CHAT_ID': 'X_CHAT_ID',
        'TUNNEL_X_USER_ID': 'X_USER_ID',
        'TUNNEL_NGROK_DOMAIN': 'NGROK_DOMAIN',
        'TUNNEL_LOG_FILE': 'LOG_FILE',
        'TUNNEL_WORK_DIR': 'WORK_DIR',
        'TUNNEL_NGROK_AUTHTOKEN': 'NGROK_AUTHTOKEN',
    }
    for env_key, conf_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config[conf_key] = val
    return config

CFG = load_config()

# ═════════════ 配置 ═════════════
TOKEN = CFG.get('X_TOKEN', '')
CHAT_ID = CFG.get('X_CHAT_ID', '')
USER_ID = CFG.get('X_USER_ID', '')
UPSTREAM = CFG.get('API_HOST', '172.25.136.193')
UPSTREAM_PORT = int(CFG.get('API_PORT', '8080'))
NGROK_AUTHTOKEN = CFG.get('NGROK_AUTHTOKEN', '')
PROXY_PORT = 8082
WORK_DIR = CFG.get('WORK_DIR', '/home/z/my-project')
LOG_FILE = CFG.get('LOG_FILE', f'{WORK_DIR}/seamless.log')
PID_FILE = f'{WORK_DIR}/watchdog.pid'
PROXY_SCRIPT = os.path.join(WORK_DIR, 'proxy.py')

# ═════════════ 全局状态 ═════════════
class State:
    proxy_ok = False
    ngrok_ok = False
    tunnel_ok = False
    running = True
    restart_lock = threading.Lock()
    repair_count = 0
    start_time = 0
    _proxy_start_t = 0
    _ngrok_start_t = 0
    _proxy_fail_count = 0      # 连续 proxy 启动失败次数
    _ngrok_fail_count = 0      # 连续 ngrok 启动失败次数
    _last_proxy_repair = 0     # 上次 proxy 修复时间
    _last_ngrok_repair = 0     # 上次 ngrok 修复时间

state = State()

# ═════════════ PID 互斥锁 ═════════════

def read_pid_file():
    try:
        if not os.path.isfile(PID_FILE):
            return None
        with open(PID_FILE, 'r') as f:
            data = json.load(f)
        pid = data.get('pid', 0)
        if pid <= 0:
            return None
        try:
            os.kill(pid, 0)
            return (pid, data.get('holder', ''), data.get('start', 0))
        except (OSError, ProcessLookupError):
            os.unlink(PID_FILE)
            return None
    except:
        return None


def write_pid_file(holder):
    try:
        with open(PID_FILE, 'w') as f:
            json.dump({
                "pid": os.getpid(),
                "holder": holder,
                "start": time.time(),
                "start_str": time.strftime("%H:%M:%S")
            }, f)
    except:
        pass


def signal_old_watchdog():
    info = read_pid_file()
    if info is None:
        return False
    old_pid, old_holder, old_start = info
    if old_pid == os.getpid():
        return False
    try:
        os.kill(old_pid, signal.SIGUSR1)
        return True
    except (OSError, ProcessLookupError):
        os.unlink(PID_FILE)
        return False


# ═════════════ 日志 ═════════════

def ts_ms():
    now = time.time()
    ms = int((now % 1) * 1000)
    return time.strftime("%H:%M:%S") + f".{ms:03d}"


def log(msg):
    line = f"[{ts_ms()}] [watchdog:{str(os.getpid())[-4:]}] {msg}"
    is_redirected = not os.isatty(sys.stdout.fileno())
    print(line, flush=True)
    if not is_redirected:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except:
            pass


# ═════════════ 服务检查 ═════════════

def check_proxy():
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PROXY_PORT, timeout=2)
        conn.request("GET", "/_ping")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 200
    except:
        return False


def check_ngrok():
    try:
        result = subprocess.run(["pgrep", "-x", "ngrok"], capture_output=True, timeout=2)
        if result.returncode != 0:
            return False, "no_process"
        conn = http.client.HTTPConnection("127.0.0.1", 4040, timeout=2)
        conn.request("GET", "/api/tunnels")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        tunnels = json.loads(body).get('tunnels', [])
        if tunnels:
            return True, "ok"
        return False, "no_tunnel"
    except:
        return False, "check_error"


def get_ngrok_url():
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 4040, timeout=2)
        conn.request("GET", "/api/tunnels")
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        tunnels = json.loads(body).get('tunnels', [])
        if tunnels:
            return tunnels[0].get('public_url', '')
    except:
        pass
    return ''


# ═════════════ 防雪崩退避 ═════════════

def get_backoff(fail_count, min_s=5, max_s=120):
    """指数退避：5s, 10s, 20s, 40s, 80s, 120s, 120s, ..."""
    return min(min_s * (2 ** (fail_count - 1)), max_s)


def reset_backoff():
    """修复成功后重置计数"""
    state._proxy_fail_count = 0
    state._ngrok_fail_count = 0


# ═════════════ 服务启动 ═════════════

def start_proxy():
    t0 = time.time()
    now = time.time()
    
    # 防雪崩检查
    if state._proxy_fail_count > 0:
        backoff = get_backoff(state._proxy_fail_count)
        elapsed_since = now - state._last_proxy_repair
        if elapsed_since < backoff:
            remaining = int(backoff - elapsed_since)
            log(f"⏳ proxy 退避中 (连续失败{state._proxy_fail_count}次，{remaining}s后重试)")
            return False
    
    try:
        if check_proxy():
            log(f"✅ 代理已在运行，跳过启动")
            state._proxy_fail_count = 0
            return True
        
        subprocess.run(["pkill", "-9", "-f", f"python3.*{PROXY_SCRIPT}"],
                       capture_output=True, timeout=3)
        time.sleep(0.3)
        
        env = os.environ.copy()
        env['SELF_RESTART'] = '1'  # 启用自重启
        
        proc = subprocess.Popen(
            [sys.executable, PROXY_SCRIPT, '--no-daemon'],
            stdout=open(LOG_FILE, "a"),
            stderr=open(LOG_FILE, "a"),
            start_new_session=True,  # 独立进程组，不受 watchdog 退出影响
            env=env,
        )
        
        # 快速轮询代理就绪（最多 5s）
        for _ in range(25):
            time.sleep(0.2)
            if check_proxy():
                elapsed = f"{(time.time() - t0)*1000:.0f}ms"
                log(f"✅ 代理启动成功 (PID {proc.pid}, 耗时 {elapsed}, self_restart=ON)")
                state._proxy_start_t = time.time() - t0
                state._proxy_fail_count = 0
                state._last_proxy_repair = time.time()
                return True
        
        elapsed = f"{(time.time() - t0)*1000:.0f}ms"
        state._proxy_fail_count += 1
        state._last_proxy_repair = time.time()
        log(f"❌ 代理启动失败 (超时 {elapsed}, 连续失败{state._proxy_fail_count}次)")
        return False
    except Exception as e:
        state._proxy_fail_count += 1
        state._last_proxy_repair = time.time()
        log(f"❌ 代理启动异常: {e}")
        return False


def start_ngrok():
    t0 = time.time()
    now = time.time()
    
    # 防雪崩检查
    if state._ngrok_fail_count > 0:
        backoff = get_backoff(state._ngrok_fail_count)
        elapsed_since = now - state._last_ngrok_repair
        if elapsed_since < backoff:
            remaining = int(backoff - elapsed_since)
            log(f"⏳ ngrok 退避中 (连续失败{state._ngrok_fail_count}次，{remaining}s后重试)")
            return False
    
    try:
        ok, reason = check_ngrok()
        if ok:
            url = get_ngrok_url()
            log(f"✅ ngrok 已在运行 (url={url})，跳过启动")
            state._ngrok_fail_count = 0
            return True
        
        subprocess.run(["pkill", "-9", "-f", "ngrok"], capture_output=True, timeout=3)
        time.sleep(0.5)
        
        # 确保配置 authtoken
        if NGROK_AUTHTOKEN:
            subprocess.run(["ngrok", "config", "add-authtoken", NGROK_AUTHTOKEN],
                         capture_output=True, timeout=5)
        
        subprocess.Popen(
            ["ngrok", "http", f"http://127.0.0.1:{PROXY_PORT}",
             "--log=stdout", "--log-format=logfmt"],
            stdout=open(LOG_FILE, "a"),
            stderr=open(LOG_FILE, "a"),
            start_new_session=True,  # 独立进程组
        )
        
        # 快速轮询隧道就绪（最多 20s）
        for i in range(100):
            time.sleep(0.2)
            ok, _ = check_ngrok()
            if ok:
                url = get_ngrok_url()
                elapsed = f"{(time.time() - t0)*1000:.0f}ms"
                log(f"✅ ngrok 隧道建立成功 (耗时 {elapsed}, url={url})")
                state._ngrok_start_t = time.time() - t0
                state._ngrok_fail_count = 0
                state._last_ngrok_repair = time.time()
                return True
        
        elapsed = f"{(time.time() - t0)*1000:.0f}ms"
        state._ngrok_fail_count += 1
        state._last_ngrok_repair = time.time()
        log(f"❌ ngrok 隧道建立超时 ({elapsed}, 连续失败{state._ngrok_fail_count}次)")
        return False
    except Exception as e:
        state._ngrok_fail_count += 1
        state._last_ngrok_repair = time.time()
        log(f"❌ ngrok 启动异常: {e}")
        return False


# ═════════════ 修复 ═════════════

def repair_proxy():
    with state.restart_lock:
        if check_proxy():
            return
        log("🔧 [proxy_watcher] 修复代理...")
        state.repair_count += 1
        start_proxy()

def repair_ngrok():
    with state.restart_lock:
        ok, _ = check_ngrok()
        if ok:
            return
        log("🔧 [ngrok_watcher] 修复 ngrok...")
        state.repair_count += 1
        start_ngrok()


# ═════════════ 监控线程 ═════════════

def proxy_watcher():
    log("🔍 [proxy_watcher] 启动 (间隔 2s)")
    while state.running:
        state.proxy_ok = check_proxy()
        if not state.proxy_ok:
            threading.Thread(target=repair_proxy, daemon=True).start()
        time.sleep(2)
    log("🛑 [proxy_watcher] 停止")

def ngrok_watcher():
    log("🔍 [ngrok_watcher] 启动 (间隔 3s)")
    while state.running:
        ok, reason = check_ngrok()
        state.ngrok_ok = ok
        state.tunnel_ok = ok and reason == "ok"
        if not ok:
            threading.Thread(target=repair_ngrok, daemon=True).start()
        time.sleep(3)
    log("🛑 [ngrok_watcher] 停止")

def stats_reporter():
    log("📊 [stats] 启动 (间隔 10s)")
    while state.running:
        p = "✅" if state.proxy_ok else "❌"
        n = "✅" if state.ngrok_ok else "❌"
        t = "✅" if state.tunnel_ok else "❌"
        elapsed = int(time.time() - state.start_time)
        url = get_ngrok_url() if state.tunnel_ok else "?"
        log(f"📊 代理:{p} ngrok:{n} 隧道:{t} url={url} | 运行:{elapsed}s 修复:{state.repair_count}次")
        time.sleep(10)
    log("🛑 [stats] 停止")


# ═════════════ 信号处理 ═════════════

def handle_sigusr1(signum, frame):
    elapsed = int(time.time() - state.start_time)
    log(f"🔄 收到 SIGUSR1 交接信号，准备退出 (已运行 {elapsed}s)")
    state.running = False


def handle_sigterm(signum, frame):
    elapsed = int(time.time() - state.start_time)
    log(f"🔄 收到 SIGTERM (cron timeout)，准备退出 (已运行 {elapsed}s)")
    state.running = False


# ═════════════ 主函数 ═════════════

def main():
    state.start_time = time.time()

    parser = argparse.ArgumentParser(description="多线程隧道保活守护 v4")
    parser.add_argument("--holder", type=str, default="watchdog", help="holder 名称")
    parser.add_argument("--quick-check", action="store_true", help="快速健康检查模式：检查+修复，不进入监控循环")
    args = parser.parse_args()

    log(f"════════════════════════════════════════")
    log(f"🚀 watchdog v4 启动 [holder={args.holder}, pid={str(os.getpid())}]")
    log(f"════════════════════════════════════════")

    # ─── Step 1: PID 互斥锁 + 主动交接 ───
    if not args.quick_check:
        old_info = read_pid_file()
        if old_info:
            old_pid, old_holder, old_start = old_info
            old_elapsed = int(time.time() - old_start)
            log(f"🔄 发现旧 watchdog (pid={old_pid}, holder={old_holder}, 运行{old_elapsed}s)")
            signaled = signal_old_watchdog()
            if signaled:
                log(f"🤝 已发送交接信号 SIGUSR1 → pid={old_pid}，等待其退出...")
                for i in range(15):
                    time.sleep(0.2)
                    try:
                        os.kill(old_pid, 0)
                    except (OSError, ProcessLookupError):
                        log(f"🤝 旧 watchdog 已退出 (等待 {(i+1)*200}ms)")
                        break
                else:
                    log(f"⚠️ 旧 watchdog 未响应，强制清理")
                    try:
                        os.kill(old_pid, signal.SIGTERM)
                    except:
                        pass

        write_pid_file(args.holder)

    # ─── Step 2: 注册信号处理 ───
    signal.signal(signal.SIGUSR1, handle_sigusr1)
    signal.signal(signal.SIGTERM, handle_sigterm)

    # ─── Step 3: 初始化/修复服务 ───
    log("📦 初始化服务...")
    t_init = time.time()

    if not check_proxy():
        start_proxy()
    else:
        log("✅ 代理已在运行")

    ok, _ = check_ngrok()
    if not ok:
        start_ngrok()
    else:
        log("✅ ngrok 已在运行")

    init_elapsed = f"{(time.time() - t_init)*1000:.0f}ms"
    log(f"📦 服务初始化完成 (耗时 {init_elapsed})")

    # ─── Quick check 模式：修复完直接退出 ───
    if args.quick_check:
        p_ok = check_proxy()
        n_ok, _ = check_ngrok()
        url = get_ngrok_url()
        log(f"⚡ Quick-check 完成 | 代理:{'✅' if p_ok else '❌'} ngrok:{'✅' if n_ok else '❌'} url={url}")
        return 0 if (p_ok and n_ok) else 1

    # ─── Step 4: 启动监控线程 ───
    for target in [proxy_watcher, ngrok_watcher, stats_reporter]:
        threading.Thread(target=target, daemon=True).start()

    log(f"✅ 3 个监控线程已启动，无限运行模式（由 cron timeout 控制）")

    # ─── Step 5: 主循环 ───
    try:
        while state.running:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    # ─── Step 6: 优雅退出（确保服务独立存活） ───
    state.running = False
    time.sleep(0.5)

    p_ok = check_proxy()
    n_ok, _ = check_ngrok()
    elapsed = int(time.time() - state.start_time)

    log(f"🏁 watchdog 退出 | 代理:{'✅' if p_ok else '❌'} ngrok:{'✅' if n_ok else '❌'} | "
        f"运行:{elapsed}s 修复:{state.repair_count}次")

    # 如果服务不健康，再尝试修复一次
    if not p_ok:
        log("⚠️ 退出前修复代理...")
        start_proxy()
    if not n_ok:
        log("⚠️ 退出前修复 ngrok...")
        start_ngrok()

    # 清理 PID 文件
    try:
        if os.path.isfile(PID_FILE):
            with open(PID_FILE, 'r') as f:
                data = json.load(f)
            if data.get('pid') == os.getpid():
                os.unlink(PID_FILE)
    except:
        pass

    return 0 if (p_ok and n_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
