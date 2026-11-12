"""本機網頁伺服器（標準函式庫 http.server）。"""
import gzip
import json
import mimetypes
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .util import BASE_DIR

WEB_DIR = os.path.join(BASE_DIR, "web")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TWScan/1.0"

        def log_message(self, fmt, *args):  # 不在終端機印出每個請求
            pass

        def _send(self, body, ctype, status=200, cache="no-store"):
            if "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 1400:
                body = gzip.compress(body, 5)
                enc = True
            else:
                enc = False
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            if enc:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", status)

        def _static(self, rel):
            path = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not path.startswith(WEB_DIR) or not os.path.isfile(path):
                self._json({"error": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            with open(path, "rb") as f:
                self._send(f.read(), ctype)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    self._static("index.html")
                elif u.path.startswith("/static/"):
                    self._static(u.path[len("/static/"):])
                elif u.path == "/api/status":
                    self._json(engine.api_status())
                elif u.path == "/api/top":
                    self._json(engine.api_top())
                elif u.path == "/api/market":
                    self._json(engine.api_market())
                elif u.path == "/api/stock":
                    self._json(engine.api_stock((q.get("code") or [""])[0]))
                elif u.path == "/api/pricecheck":
                    self._json(engine.api_pricecheck((q.get("code") or [""])[0]))
                elif u.path == "/api/stock/tf":
                    self._json(engine.api_stock_tf((q.get("code") or [""])[0], (q.get("tf") or ["d"])[0]))
                elif u.path == "/api/review":
                    self._json(engine.review.api())
                elif u.path == "/api/daytrade":
                    self._json(engine.daytrade.api())
                elif u.path == "/api/daytrade/review":
                    self._json(engine.daytrade.review())
                elif u.path == "/api/diag":
                    self._json(engine.api_diag())
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                traceback.print_exc()
                try:
                    self._json({"error": f"伺服器錯誤：{e}"}, 500)
                except Exception:
                    pass

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
            except (ValueError, TypeError):
                data = {}
            return data if isinstance(data, dict) else {}

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/rescan":
                    engine.trigger_rescan()
                    self._json({"ok": True})
                elif path == "/api/settings":
                    self._json(engine.api_settings(self._body()))
                elif path == "/api/daytrade":
                    self._json(engine.daytrade.action(self._body()))
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                traceback.print_exc()
                try:
                    self._json({"error": f"伺服器錯誤：{e}"}, 500)
                except Exception:
                    pass

    return Handler


def serve(engine, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(engine))
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
