#!/usr/bin/env python3
"""
API Proxy v3 — ngrok 兼容的 HTTP 代理，自动注入认证 Headers。

关键改进（v3）：
- 使用 signal-aware 事件循环替代 time.sleep(9999)，进程更健壮
- 支持 --daemon 模式：双 fork 完全脱离终端，不受父进程死亡影响
- 支持从 watchdog 内联启动（默认）和独立守护模式
- 退出时自动尝试自重启（SELF_RESTART=1 时）

架构：ngrok(random domain) → localhost:8082(Python proxy) → 内网API

监听: 127.0.0.1:8082
上游: 由 config.env 中的 API_HOST:API_PORT 决定
"""

import http.server
import http.client
import json
import threading
import time
import sys
import os
import re
import signal

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
                        value = value.strip()
                        if '#' in value:
                            value = value[:value.index('#')].strip()
                        config[key] = value
            break
    # 环境变量覆盖（TUNNEL_ 前缀）
    env_map = {
        'TUNNEL_API_HOST': 'API_HOST',
        'TUNNEL_API_PORT': 'API_PORT',
        'TUNNEL_API_KEY': 'API_KEY',
        'TUNNEL_X_TOKEN': 'X_TOKEN',
        'TUNNEL_X_CHAT_ID': 'X_CHAT_ID',
        'TUNNEL_X_USER_ID': 'X_USER_ID',
    }
    for env_key, conf_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config[conf_key] = val
    return config

CFG = load_config()

# ═════════════ 配置值 ═════════════
TOKEN     = CFG.get('X_TOKEN', '')
CHAT_ID   = CFG.get('X_CHAT_ID', '')
USER_ID   = CFG.get('X_USER_ID', '')
UPSTREAM  = CFG.get('API_HOST', '172.25.136.193')
UPSTREAM_PORT = int(CFG.get('API_PORT', '8080'))
PROXY_PORT = 8082


class Handler(http.server.BaseHTTPRequestHandler):
    """HTTP/1.0 代理处理器 — ngrok 兼容"""

    def do_GET(self):
        if self.path == '/_ping':
            b = b'pong'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self._proxy(None)

    def do_POST(self):
        cl = int(self.headers.get('Content-Length', 0))
        self._proxy(self.rfile.read(cl) if cl > 0 else None)

    def do_PUT(self):
        cl = int(self.headers.get('Content-Length', 0))
        self._proxy(self.rfile.read(cl) if cl > 0 else None)

    def do_DELETE(self):
        self._proxy(None)

    def do_OPTIONS(self):
        self._proxy(None)

    def do_PATCH(self):
        cl = int(self.headers.get('Content-Length', 0))
        self._proxy(self.rfile.read(cl) if cl > 0 else None)

    def _proxy(self, req_body):
        """转发请求到上游，自动注入认证 Headers"""
        try:
            headers = {}
            for k, v in self.headers.items():
                if k.lower() not in ('host', 'transfer-encoding', 'connection'):
                    headers[k] = v
            headers['Host'] = f'{UPSTREAM}:{UPSTREAM_PORT}'
            headers['X-Token'] = TOKEN
            headers['X-Chat-Id'] = CHAT_ID
            headers['X-User-Id'] = USER_ID
            headers['X-Z-AI-From'] = 'Z'

            conn = http.client.HTTPConnection(UPSTREAM, UPSTREAM_PORT, timeout=120)
            conn.request(self.command, self.path, body=req_body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            status = resp.status
            conn.close()

            self.send_response(status)
            for k, v in resp.getheaders():
                if k.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        except Exception as e:
            err = json.dumps({'error': str(e)}).encode()
            self.send_response(502)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def log_message(self, *args):
        pass


class GracefulServer:
    """支持优雅退出的 HTTP Server 封装"""

    def __init__(self):
        self.server = http.server.HTTPServer(('127.0.0.1', PROXY_PORT), Handler)
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        """启动 HTTP 服务（非阻塞，在独立线程中运行）"""
        self._running = True
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    def _serve_loop(self):
        """服务循环，支持优雅退出"""
        # 设置超时，使 server.handle_request() 不会永久阻塞
        self.server.timeout = 1.0
        while self._running:
            try:
                self.server.handle_request()
            except Exception:
                pass
        self.server.server_close()

    def stop(self):
        """优雅停止"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self.server.server_close()

    def is_alive(self):
        return self._running and self._thread and self._thread.is_alive()


def daemonize():
    """双 fork 完全脱离终端，成为独立守护进程"""
    # 第一次 fork
    pid = os.fork()
    if pid > 0:
        # 父进程退出
        sys.exit(0)
    # 脱离控制终端
    os.setsid()
    # 第二次 fork
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    # 重定向标准文件描述符
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = open(os.devnull, 'r')
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    devnull.close()
    # stdout/stderr 重定向到日志
    log_path = '/home/z/my-project/seamless.log'
    try:
        log_fd = open(log_path, 'a')
        os.dup2(log_fd.fileno(), sys.stdout.fileno())
        os.dup2(log_fd.fileno(), sys.stderr.fileno())
        log_fd.close()
    except:
        pass
    # 设置工作目录
    os.chdir('/home/z/my-project')
    # 设置 umask
    os.umask(0o022)


def self_restart():
    """自重启：重新执行自己"""
    try:
        import subprocess
        env = os.environ.copy()
        env['SELF_RESTART'] = '1'
        # 用 nohup 确保完全独立
        subprocess.Popen(
            [sys.executable, sys.argv[0], '--no-daemon'],
            stdout=open('/home/z/my-project/seamless.log', 'a'),
            stderr=open('/home/z/my-project/seamless.log', 'a'),
            preexec_fn=os.setpgrp,
            env=env,
        )
    except:
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="API Proxy v3")
    parser.add_argument('--daemon', action='store_true', help='完全守护模式（双fork脱离终端）')
    parser.add_argument('--no-daemon', action='store_true', help='非守护模式')
    args = parser.parse_args()

    if not TOKEN:
        print("WARNING: X_TOKEN not configured", flush=True)

    # 守护模式
    if args.daemon and not args.no_daemon:
        daemonize()

    server = GracefulServer()
    server.start()

    pid = os.getpid()
    print(f'PROXY_OK :{PROXY_PORT} -> {UPSTREAM}:{UPSTREAM_PORT} (pid={pid})', flush=True)

    # 自重启模式：收到 SIGTERM 时自动重启
    should_restart = os.environ.get('SELF_RESTART') == '1'

    def handle_term(signum, frame):
        if should_restart:
            print(f'[{time.strftime("%H:%M:%S")}] proxy received SIGTERM, self-restarting...', flush=True)
            # 先停止服务器释放端口
            server.stop()
            time.sleep(0.5)
            self_restart()
        else:
            server.stop()

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    # 事件循环（替代 time.sleep(9999)）
    while server.is_alive():
        try:
            # 每 60 秒做一次健康自检
            server._stop_event.wait(timeout=60)
        except (KeyboardInterrupt, SystemExit):
            break

    server.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
