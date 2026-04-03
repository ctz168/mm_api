#!/usr/bin/env python3
"""
隧道状态检查脚本 v2

显示代理、ngrok、隧道、watchdog、API 的实时状态。

用法：python3 status.py
"""

import subprocess
import http.client
import json
import os
import sys

WORK_DIR = '/home/z/my-project'
LOG_FILE = f'{WORK_DIR}/seamless.log'
PID_FILE = f'{WORK_DIR}/watchdog.pid'

def cmd_result(cmd, timeout=3):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=isinstance(cmd, str))
        return r.stdout.decode().strip(), r.returncode
    except:
        return "", 1

def check(label, cmd):
    out, rc = cmd_result(cmd)
    if rc == 0 and out:
        return f"  ✅ {label}: {out}"
    return f"  ❌ {label}: 未运行"

def check_http(label, host, port, path="/"):
    try:
        conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode()[:200]
        conn.close()
        return f"  ✅ {label}: HTTP {resp.status}"
    except Exception as e:
        return f"  ❌ {label}: {e}"

def check_tunnel():
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 4040, timeout=3)
        conn.request("GET", "/api/tunnels")
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        tunnels = data.get('tunnels', [])
        if tunnels:
            url = tunnels[0].get('public_url', 'unknown')
            return f"  ✅ 隧道: {url}"
        return "  ❌ 隧道: 无隧道"
    except Exception as e:
        return f"  ❌ 隧道: {e}"

def check_api():
    return check_http("API(代理)", "127.0.0.1", 8082, "/_ping")

def check_watchdog():
    """检查 watchdog 状态"""
    try:
        if not os.path.isfile(PID_FILE):
            return "  🔓 watchdog: 未运行 (无 PID 文件)"
        with open(PID_FILE) as f:
            data = json.load(f)
        pid = data.get('pid', 0)
        holder = data.get('holder', 'unknown')
        start = data.get('start', 0)
        age = int(__import__('time').time() - start)
        
        # 检查进程是否存活
        try:
            os.kill(pid, 0)
            return f"  ✅ watchdog: PID {pid} holder={holder} (运行 {age}s)"
        except (OSError, ProcessLookupError):
            return f"  ❌ watchdog: PID 文件存在但进程已死 (pid={pid}, holder={holder})"
    except Exception as e:
        return f"  🔓 watchdog: {e}"

def check_proxy_restart():
    """检查 proxy 是否启用了 self-restart"""
    out, rc = cmd_result("pgrep -af 'python3.*proxy.py'")
    if rc == 0:
        if 'SELF_RESTART' in os.environ or 'self_restart' in out.lower():
            return "  ✅ proxy self-restart: 已启用"
        # 检查 proxy 进程的环境变量
        try:
            pid = out.strip().split()[0]
            env_out, _ = cmd_result(f"cat /proc/{pid}/environ 2>/dev/null | tr '\\0' '\\n' | grep SELF_RESTART")
            if 'SELF_RESTART=1' in env_out:
                return "  ✅ proxy self-restart: 已启用"
        except:
            pass
        return "  ⚠️  proxy self-restart: 未确认"
    return "  ❌ proxy: 未运行"

def check_cron():
    """显示最近日志"""
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        recent = [l.strip() for l in lines[-8:] if l.strip() and '[watchdog:' in l or '[daemon:' in l]
        if recent:
            return "  最近日志:\n    " + "\n    ".join(recent[-5:])
        return "  日志: 无 watchdog 记录"
    except:
        return "  日志: 无法读取"

def main():
    print()
    print("═══════════════════════════════════════")
    print("  API Tunnel 状态检查 v4")
    print("═══════════════════════════════════════")
    print()

    # 基础信息
    print("── 基础信息 ──")
    out, rc = cmd_result("ngrok version")
    print(f"  ngrok: {out if rc == 0 else '未安装'}")
    print(f"  Python: {sys.version.split()[0]}")
    print()

    # 服务状态
    print("── 服务状态 ──")
    out, rc = cmd_result("pgrep -a ngrok")
    print(f"  {'✅' if rc == 0 else '❌'} ngrok: {'运行中' if rc == 0 else '未运行'}")
    out, rc = cmd_result("pgrep -af 'python3.*proxy.py'")
    print(f"  {'✅' if rc == 0 else '❌'} 代理:   {'运行中' if rc == 0 else '未运行'}")
    print(check_api())
    print(check_tunnel())
    print(check_watchdog())
    print(check_proxy_restart())
    print()

    # 最近日志
    print("── 保活日志（最近）──")
    print(check_cron())
    print()

    print("═══════════════════════════════════════")

if __name__ == "__main__":
    main()
