"""Local-only, read-only server for prepared private B/S review pages."""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class ReviewHandler(SimpleHTTPRequestHandler):
    report_root: Path

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/healthz":
            body = b'{"status":"ok","scope":"local_stock_sb_review"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if not path.startswith("/review/"):
            self.send_error(404)
            return
        relative = path[len("/review/"):].strip("/")
        candidate = (self.report_root / relative).resolve()
        if candidate.is_dir():
            candidate /= "index.html"
        if not candidate.is_relative_to(self.report_root) or candidate.name not in {"index.html", "echarts.min.js"}:
            self.send_error(404)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8" if candidate.suffix == ".html" else "application/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'none'; img-src 'self' data:; object-src 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        # Avoid noisy console output from a hidden local helper.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(r"G:\StockPlatform\reports\stock-sb-review"))
    parser.add_argument("--port", type=int, default=15791)
    args = parser.parse_args()
    ReviewHandler.report_root = args.root.resolve()
    if not ReviewHandler.report_root.is_dir():
        parser.error("复盘报告目录不存在")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReviewHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
