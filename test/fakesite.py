#!/usr/bin/env python3
"""Serve fake vendor download pages for the end-to-end test.

Resolution keys off the page's domain, so a test on localhost proves nothing.
Paired with /etc/hosts entries pointing real vendor domains at 127.0.0.1, this
makes the browser believe it is on zoom.us while everything stays local.

The page auto-starts its download, so launching the browser at it is enough to
exercise the sensor -- no CDP driving required.
"""
import argparse, http.server, socketserver

PAGES = {
    "/download": ("Zoom", "zoom_amd64.deb"),
    "/downloads/linux": ("Slack", "slack-desktop.deb"),
    "/idea/download/": ("IntelliJ IDEA", "ideaIU.tar.gz"),
    "/pricing": ("Zoom Pricing", "whitepaper.pdf"),
    # Windows-only vendor page: exercises the compatibility path.
    "/flstudio": ("FL Studio", "flstudio_win64_21.exe"),
}

PAGE = """<!doctype html><title>{name} — Download</title>
<h1>Download {name} for Linux</h1>
<p>Your download should begin automatically.</p>
<a id="dl" href="/{filename}">Download {filename}</a>
<script>setTimeout(function(){{ document.getElementById('dl').click(); }}, 800);</script>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        host = self.headers.get("Host", "?").split(":")[0]
        path = self.path

        if path in PAGES:
            name, filename = PAGES[path]
            body = PAGE.format(name=name, filename=filename).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Anything else is "the installer file" -- a few KB of nothing, served
        # as an attachment so the browser treats it as a download.
        body = b"not a real package\n" * 512
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{path.lstrip("/") or "file.bin"}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"  [fakesite] {self.headers.get('Host','?')}{self.path}", flush=True)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=80)
    a = ap.parse_args()
    print(f"fakesite on :{a.port}", flush=True)
    Server(("0.0.0.0", a.port), Handler).serve_forever()
