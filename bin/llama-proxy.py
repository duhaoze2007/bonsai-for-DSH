#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bonsai-proxy —— 惰性加载的 OpenAI 兼容代理。

DSH（或任何 OpenAI 客户端）连到本代理；代理在第一次推理请求时才拉起
llama-server（把 7.5 GB 权重读进 Metal），空闲 idle_unload_seconds 秒后
自动关掉它，把内存还回去。这样模型既不常驻吃 8 GB 内存，也不需要手动预热。

对外端点（proxy_host:proxy_port）：
  GET  /v1/models       —— 静态模型列表（不触发加载）
  GET  /health          —— 代理 + 后端状态
  GET  /admin/status    —— 详细状态（JSON）
  POST /admin/load      —— 立即加载模型（预热）
  POST /admin/unload    —— 立即卸载模型
  *    /v1/...          —— 透传到 llama-server（流式）
"""

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


CFG = load_config()
LOGS_DIR = CFG["logs_dir"]
os.makedirs(LOGS_DIR, exist_ok=True)

SERVER_BIN = os.path.join(CFG["runtime_dir"], "llama-server")
BACKEND = "http://%s:%d" % (CFG["backend_host"], CFG["backend_port"])
ALIAS = CFG["model_alias"]


def log(msg):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (stamp, msg), flush=True)


class BackendError(RuntimeError):
    pass


def thinking_args(cfg):
    """把 config.json 里的 thinking 配置翻译成 llama-server 的思考开关。

    thinking.mode:
      "xhigh"/"medium"/"low" —— 模板支持的推理力度
      "off"                  —— 完全关掉思考（模板直接给空 think 块）
    另外 thinking.budget（整数）给思考 token 设硬上限，防止想了半天收不回来。
    """
    th = cfg.get("thinking") or {}
    mode = str(th.get("mode") or "").strip().lower()
    if mode == "off":
        return ["--reasoning", "off"]
    args = ["--reasoning", "on"]
    if mode in ("xhigh", "medium", "low"):
        # allow 列表挡住客户端瞎传的 effort（模板会对未知值直接 500），越界的回落到 medium
        args += ["--reasoning-effort", mode,
                 "--reasoning-effort-allow", "xhigh,medium,low",
                 "--reasoning-effort-fallback", "medium"]
    budget = th.get("budget")
    if isinstance(budget, int) and not isinstance(budget, bool) and budget >= 0:
        args += ["--reasoning-budget", str(budget)]
    return args


class State(object):
    """llama-server 子进程的生命周期 + 空闲回收。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.loaded_at = None
        self.last_activity = time.time()
        self.active_requests = 0
        self.load_count = 0
        self.request_count = 0
        self.last_error = None
        self.started_at = time.time()

    # ---- 低层 ----
    def _proc_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def healthy(self):
        try:
            with urllib.request.urlopen(BACKEND + "/health", timeout=3) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8") or "{}")
                return data.get("status") == "ok"
        except Exception:
            return False

    def rss_mb(self):
        if not self._proc_alive():
            return 0
        try:
            out = subprocess.run(
                ["ps", "-o", "rss=", "-p", str(self.proc.pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            return int(out) // 1024 if out else 0
        except Exception:
            return 0

    def log_tail(self, lines=12):
        path = os.path.join(LOGS_DIR, "llama-server.log")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:]).strip()
        except Exception:
            return ""

    # ---- 生命周期 ----
    def start(self):
        global CFG
        with self.lock:
            # 每次加载都重读配置：改完 config.json（比如切思考模式）只要 unload 就能生效
            try:
                CFG = load_config()
            except Exception as exc:
                log("config reload failed, keep cached config: %r" % (exc,))
            if self._proc_alive() and self.healthy():
                self.last_activity = time.time()
                return
            if self._proc_alive() and not self.healthy():
                self._terminate("unhealthy process")
            if not os.path.exists(SERVER_BIN):
                raise BackendError("找不到 llama-server: %s" % SERVER_BIN)
            model = CFG["model_path"]
            if not os.path.exists(model):
                raise BackendError("找不到模型文件: %s" % model)

            cmd = [
                SERVER_BIN, "-m", model,
                "--alias", ALIAS,
                "--host", CFG["backend_host"],
                "--port", str(CFG["backend_port"]),
                "-c", str(CFG["context_size"]),
            ] + [str(a) for a in CFG.get("server_args", [])] + thinking_args(CFG)

            log("loading model: %s" % " ".join(shlex.quote(c) for c in cmd))
            logfile = open(os.path.join(LOGS_DIR, "llama-server.log"), "ab")
            logfile.write(("\n\n===== %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
            logfile.flush()
            self.proc = subprocess.Popen(
                cmd,
                cwd=CFG["runtime_dir"],
                stdout=logfile,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = time.time() + float(CFG.get("start_timeout_seconds", 240))
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    code = self.proc.returncode
                    self.proc = None
                    raise BackendError(
                        "llama-server 启动后退出 (code %s)\n%s" % (code, self.log_tail())
                    )
                if self.healthy():
                    self.loaded_at = time.time()
                    self.last_activity = time.time()
                    self.load_count += 1
                    log("model ready in %.1fs (pid %d)" % (time.time() - self.loaded_at, self.proc.pid))
                    return
                time.sleep(0.4)
            self._terminate("load timeout")
            raise BackendError(
                "加载超时 (%ss)\n%s" % (CFG.get("start_timeout_seconds"), self.log_tail())
            )

    def _terminate(self, reason):
        proc = self.proc
        self.proc = None
        self.loaded_at = None
        if proc is None:
            return
        if proc.poll() is None:
            log("unloading model (%s, pid %d)" % (reason, proc.pid))
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except Exception:
                    pass

    def stop(self, reason="requested"):
        with self.lock:
            self._terminate(reason)

    def ensure(self):
        with self.lock:
            self.last_activity = time.time()
            if self._proc_alive() and self.healthy():
                return
            self.start()

    def touch(self):
        with self.lock:
            self.last_activity = time.time()

    def status(self):
        # 状态永远读磁盘上的配置，避免刚改完 thinking 却显示旧值
        global CFG
        try:
            CFG = load_config()
        except Exception:
            pass
        with self.lock:
            loaded = self._proc_alive()
            idle = time.time() - self.last_activity
            return {
                "proxy": "up",
                "backend": "loaded" if loaded else "unloaded",
                "model": CFG["model_path"].rsplit("/", 1)[-1],
                "model_alias": ALIAS,
                "pid": self.proc.pid if loaded else None,
                "rss_mb": self.rss_mb() if loaded else 0,
                "uptime_seconds": round(time.time() - self.started_at),
                "loaded_seconds": round(time.time() - self.loaded_at) if loaded and self.loaded_at else 0,
                "idle_seconds": round(idle),
                "idle_unload_seconds": CFG.get("idle_unload_seconds", 900),
                "active_requests": self.active_requests,
                "load_count": self.load_count,
                "request_count": self.request_count,
                "context_size": CFG["context_size"],
                "thinking": CFG.get("thinking") or {},
                "endpoint": "http://%s:%d/v1" % (CFG["proxy_host"], CFG["proxy_port"]),
                "last_error": self.last_error,
            }

    def idle_loop(self):
        idle_limit = float(CFG.get("idle_unload_seconds", 900) or 0)
        while True:
            time.sleep(5)
            try:
                if idle_limit <= 0:
                    continue
                with self.lock:
                    if not self._proc_alive() or self.active_requests > 0:
                        continue
                    if time.time() - self.last_activity >= idle_limit:
                        self._terminate("idle %ss" % int(idle_limit))
            except Exception as exc:  # 监控线程不能死
                log("idle monitor error: %r" % (exc,))


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bonsai-proxy/1.0"

    # ---- 工具 ----
    def log_message(self, fmt, *args):
        if os.environ.get("BONSAI_PROXY_VERBOSE"):
            log("%s - %s" % (self.address_string(), fmt % args))

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        return self.rfile.read(length) if length > 0 else None

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/v1/models", "/models"):
            return self._send_json(200, {
                "object": "list",
                "data": [{
                    "id": ALIAS,
                    "object": "model",
                    "owned_by": "local",
                    "created": int(STATE.started_at),
                }],
            })
        if path in ("/health", "/v1/health"):
            st = STATE.status()
            code = 200 if st["backend"] == "loaded" else 503
            return self._send_json(code, st)
        if path == "/admin/status":
            return self._send_json(200, STATE.status())
        return self._proxy("GET")

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._read_body()
        if path == "/admin/load":
            try:
                STATE.ensure()
                return self._send_json(200, STATE.status())
            except BackendError as exc:
                STATE.last_error = str(exc)
                return self._send_json(503, {"error": str(exc), "status": STATE.status()})
        if path == "/admin/unload":
            STATE.stop("admin request")
            return self._send_json(200, STATE.status())
        return self._proxy("POST", body)

    def do_DELETE(self):
        return self._proxy("DELETE")

    # ---- 透传 ----
    def _proxy(self, method, body=None):
        if body is None and method in ("POST", "PUT", "PATCH"):
            body = self._read_body()
        STATE.request_count += 1
        try:
            STATE.ensure()
        except BackendError as exc:
            STATE.last_error = str(exc)
            log("backend unavailable: %s" % exc)
            return self._send_json(503, {
                "error": {
                    "message": "local model backend unavailable: %s" % exc,
                    "type": "backend_unavailable",
                }
            })

        STATE.active_requests += 1
        STATE.touch()
        upstream = BACKEND + self.path
        req = urllib.request.Request(upstream, data=body, method=method)
        hop = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding"}
        for key, value in self.headers.items():
            if key.lower() in hop:
                continue
            req.add_header(key, value)
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except Exception:
                pass
            STATE.active_requests -= 1
            return
        except Exception as exc:
            STATE.active_requests -= 1
            STATE.last_error = repr(exc)
            return self._send_json(502, {
                "error": {"message": "proxy transport failure: %r" % (exc,), "type": "transport"}
            })

        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            reader = getattr(resp, "read1", resp.read)
            while True:
                chunk = reader(8192)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                STATE.touch()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected mid-stream")
        except Exception as exc:
            log("stream relay error: %r" % (exc,))
            self.close_connection = True
        finally:
            STATE.active_requests -= 1
            STATE.touch()
            try:
                resp.close()
            except Exception:
                pass


def main():
    threading.Thread(target=STATE.idle_loop, name="idle-monitor", daemon=True).start()

    def shutdown(signum, _frame):
        log("signal %s: stopping backend and exiting" % signum)
        STATE.stop("proxy shutdown")
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, shutdown)

    server = ThreadingHTTPServer((CFG["proxy_host"], CFG["proxy_port"]), Handler)
    server.daemon_threads = True
    log("proxy listening on http://%s:%d/v1 (backend %s, idle unload %ss)" % (
        CFG["proxy_host"], CFG["proxy_port"], BACKEND, CFG.get("idle_unload_seconds")))
    try:
        server.serve_forever()
    finally:
        STATE.stop("proxy exit")


if __name__ == "__main__":
    main()
