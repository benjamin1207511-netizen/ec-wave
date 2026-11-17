#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EC 海浪预报图 —— 后端（基础仅标准库）

站点内容：
  /            ECMWF 海浪预报图（wave/ 下预生成 PNG）

服务端只读取本地 manifest 与 PNG；图像由 ec_wave_chart.py 生成，
auto_update.py 负责自动更新。
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from anti_bot import RequestGuard, SECURITY_HEADERS
BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
WAVE_DIR = BASE_DIR / "wave"
TZ_CN = timezone(timedelta(hours=8))
WAVE_PAGE = "https://data.ecmwf.int/forecasts/"
CACHE_TTL = 30
STAMP_RE = re.compile(r"^\d{10}$")
GUARD = RequestGuard()

SOURCES = [
    {
        "id": "wave",
        "name": "ECMWF WAM",
        "en": "IFS Wave 0.25° · Open Data",
        "url": WAVE_PAGE,
        "accent": "#1465b8",
    },
]

PRODUCTS = [
    {
        "id": "wave-swh",
        "source": "wave",
        "name": "有效波高+浪向",
        "title": "ECMWF WAM · 华南有效波高",
        "kind": "chart",
        "interval": 360,
        "coverage": "15–28°N · 105–122°E",
        "level": "有效波高 + 平均浪向",
        "page": WAVE_PAGE,
        "defaultWindow": 1440,
        "forecast": True,
        "subdir": "swh",
    },
    {
        "id": "wave-overview",
        "source": "wave",
        "name": "20 帧总览",
        "title": "ECMWF WAM · 华南海浪总览",
        "kind": "overview",
        "interval": 720,
        "coverage": "15–28°N · 105–122°E",
        "level": "20 小图 · +12h…+240h 每 12 小时",
        "page": WAVE_PAGE,
        "defaultWindow": 1440,
        "forecast": True,
        "subdir": "overview",
    },
]
DEFAULT_PRODUCT = "wave-swh"
PRODUCT_MAP = {p["id"]: p for p in PRODUCTS}

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def public_product(product: dict) -> dict:
    keys = (
        "id", "source", "name", "title", "kind", "interval",
        "coverage", "level", "page", "defaultWindow", "forecast",
    )
    return {k: product[k] for k in keys if k in product}


def public_frame(frame: dict) -> dict:
    return {k: frame[k] for k in ("s", "ts", "d", "t", "full")}


def make_frame(valid_utc: datetime) -> dict:
    dt = valid_utc.astimezone(TZ_CN)
    return {
        "s": valid_utc.strftime("%Y%m%d%H"),
        "ts": int(dt.timestamp()),
        "d": dt.strftime("%m/%d"),
        "t": dt.strftime("%H:%M"),
        "full": dt.strftime("%Y-%m-%d %H:%M"),
    }


_cache_lock = threading.Lock()
_frames_cache: dict[str, dict] = {}


def load_frames(pid: str) -> dict:
    product = PRODUCT_MAP[pid]
    subdir = str(product.get("subdir") or "")
    manifest_path = WAVE_DIR / subdir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("海浪图尚未生成，请先运行 ec_wave_chart.py")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    frames = []
    for item in manifest.get("frames") or []:
        name = str(item.get("file") or "")
        relative = (Path(subdir) / name).as_posix() if subdir else name
        target = (WAVE_DIR / relative).resolve()
        try:
            target.relative_to(WAVE_DIR.resolve())
        except ValueError:
            continue
        if not target.is_file():
            continue
        try:
            valid = datetime.strptime(
                str(item["valid_utc"]).replace(" UTC", "").strip(),
                "%Y-%m-%d %H:%M",
            ).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        frame = make_frame(valid)
        frame["s"] = str(item.get("s") or frame["s"])
        frame["file"] = relative
        frame["lead"] = item.get("lead")
        frames.append(frame)

    if not frames:
        raise RuntimeError("海浪图清单为空，请运行 ec_wave_chart.py")
    frames.sort(key=lambda f: f["ts"])
    return {
        "frames": frames,
        "fetched": int(time.time()),
        "run": manifest.get("run"),
        "generated": manifest.get("generated"),
        "source": WAVE_PAGE,
    }


def get_frames(pid: str, force: bool = False) -> dict:
    now = time.time()
    with _cache_lock:
        hit = _frames_cache.get(pid)
        if hit and not force and now - hit["fetched"] < CACHE_TTL:
            return hit
        try:
            data = load_frames(pid)
        except Exception:
            if hit:
                stale = dict(hit)
                stale["stale"] = True
                return stale
            raise
        _frames_cache[pid] = data
        return data


def resolve_frame(pid: str, stamp: str) -> dict | None:
    data = get_frames(pid)
    for frame in data["frames"]:
        if frame["s"] == stamp:
            return frame
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ECWaveSouthChina/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if getattr(self, "_set_cookie", ""):
            self.send_header("Set-Cookie", self._set_cookie)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, status: int = 200, cache: str = "no-store") -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": cache})

    def log_message(self, fmt: str, *args) -> None:
        if args and str(args[0]).startswith(("GET /api/img", "HEAD /api/img")):
            return
        log("%s %s" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        self._set_cookie = ""
        try:
            ip = GUARD.client_ip(self.headers, self.client_address[0])
            decision = GUARD.check(ip, path, self.headers, parsed.query)
            if not decision.allowed:
                headers = {}
                if decision.retry_after:
                    headers["Retry-After"] = str(decision.retry_after)
                return self._send(
                    decision.status,
                    decision.reason.encode("utf-8"),
                    "text/plain; charset=utf-8",
                    headers,
                )
            if decision.set_cookie:
                secure = str(self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
                self._set_cookie = GUARD.issue_cookie(secure=secure)

            if path == "/api/meta":
                return self.api_meta()
            if path == "/api/frames":
                return self.api_frames(qs)
            if path == "/api/img":
                return self.api_img(qs)
            if path == "/api/health":
                with _cache_lock:
                    cached = sorted(_frames_cache)
                return self._json({"ok": True, "products": cached})
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
            return self.serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            log("处理 %s 出错：%r" % (self.path, exc))
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    do_HEAD = do_GET

    def api_meta(self) -> None:
        now = datetime.now(TZ_CN)
        self._json(
            {
                "products": [public_product(p) for p in PRODUCTS],
                "sources": SOURCES,
                "default": DEFAULT_PRODUCT,
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "timezone": "Asia/Shanghai",
                "notice": "ECMWF IFS-WAM 0.25° 海浪数据来自 ECMWF Open Data，图表由本地脚本生成。",
            }
        )

    def api_frames(self, qs: dict) -> None:
        pid = (qs.get("p") or [DEFAULT_PRODUCT])[0]
        if pid not in PRODUCT_MAP:
            return self._json({"error": "未知产品 %s" % pid}, 400)
        force = (qs.get("force") or ["0"])[0] in ("1", "true")
        try:
            data = get_frames(pid, force=force)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": "无法读取海浪图：%s" % exc}, 502)

        frames = data["frames"]
        if not frames:
            return self._json({"error": "该产品当前没有图像"}, 502)
        product = PRODUCT_MAP[pid]
        latest = frames[-1]
        now = time.time()
        self._json(
            {
                "product": pid,
                "title": product["title"],
                "source": product["source"],
                "sourceName": "ECMWF WAM",
                "kind": product["kind"],
                "interval": product["interval"],
                "coverage": product["coverage"],
                "level": product["level"],
                "page": product["page"],
                "forecast": True,
                "run": data.get("run"),
                "generated": data.get("generated"),
                "count": len(frames),
                "frames": [public_frame(f) for f in frames],
                "stale": bool(data.get("stale")),
                "fetched": data["fetched"],
                "delayMin": max(0, int(round((now - latest["ts"]) / 60))),
                "spanMin": int(round((latest["ts"] - frames[0]["ts"]) / 60)),
            }
        )

    def api_img(self, qs: dict) -> None:
        pid = (qs.get("p") or [""])[0]
        stamp = (qs.get("s") or [""])[0]
        dl = (qs.get("dl") or ["0"])[0] in ("1", "true")
        if pid not in PRODUCT_MAP or not STAMP_RE.fullmatch(stamp or ""):
            return self._json({"error": "参数不合法"}, 400)
        frame = resolve_frame(pid, stamp)
        if not frame:
            return self._json({"error": "图像不存在"}, 404)

        target = (WAVE_DIR / frame["file"]).resolve()
        try:
            target.relative_to(WAVE_DIR.resolve())
        except ValueError:
            return self._json({"error": "图像路径不合法"}, 400)
        if not target.is_file():
            return self._json({"error": "图像文件不存在"}, 404)
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        # 图片文件名在重新生成后通常保持不变，不能使用 immutable 长缓存。
        headers = {"Cache-Control": "no-cache"}
        if dl:
            fname = "%s_%s.png" % (pid, stamp)
            headers["Content-Disposition"] = (
                "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                % (fname, urllib.parse.quote(fname))
            )
            headers["Cache-Control"] = "no-store"
        self._send(200, data, mime, headers)

    def serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        rel = urllib.parse.unquote(path).lstrip("/")
        root = PUBLIC_DIR.resolve()
        target = (PUBLIC_DIR / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._send(404, b"404 Not Found", "text/plain; charset=utf-8")
        if target.is_dir():
            target = (target / "index.html").resolve()
            try:
                target.relative_to(root)
            except ValueError:
                return self._send(404, b"404 Not Found", "text/plain; charset=utf-8")
        if not target.is_file():
            return self._send(404, b"404 Not Found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):  # noqa: N802
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def pick_port(host: str, preferred: int) -> int:
    bind_host = "0.0.0.0" if host in ("0.0.0.0", "") else host
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((bind_host, port))
                return port
            except OSError:
                continue
    raise SystemExit("找不到可用端口")


def main() -> None:
    ap = argparse.ArgumentParser(description="EC 海浪预报图站")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8890")))
    ap.add_argument(
        "--host",
        default=os.environ.get("SITE_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"),
    )
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = ap.parse_args()

    port = pick_port(args.host, args.port)
    server = Server((args.host, port), Handler)
    url = "http://%s:%d" % ("127.0.0.1" if args.host in ("0.0.0.0", "") else args.host, port)
    log("EC 海浪预报站已启动：%s" % url)
    log("共 %d 个海浪产品" % len(PRODUCTS))
    log("图像目录：%s" % WAVE_DIR)
    log("按 Ctrl+C 停止服务")

    if args.open:
        threading.Timer(0.6, lambda: __import__("webbrowser").open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
