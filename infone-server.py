#!/usr/bin/env python3
"""
infone-server — the local helper behind the Infone launcher.

Three jobs, one process, standard library only:

  GET  /                  serve the launcher
  GET  /proxy?url=...     fetch a page and strip its anti-framing headers
  POST /launch            run an allowlisted native program

Binds to 127.0.0.1 only. The proxy will fetch any http(s) URL, so do not
move it off the loopback interface without adding an allowlist.
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.expanduser("~/infone")
HOST, PORT = "127.0.0.1", 8080
TIMEOUT = 20

# Only these can be launched. The key is what the HTML sends as `cmd`.
ALLOWED = {
    "chocolate-doom": ["chocolate-doom"],
    "sol":            ["sol"],
    "retroarch":      ["retroarch", "--fullscreen"],
}

UA = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Headers that stop a page being framed, plus hop-by-hop and encoding headers
# we must not pass through unchanged.
STRIP = {
    "x-frame-options", "content-security-policy",
    "content-security-policy-report-only", "content-encoding",
    "content-length", "transfer-encoding", "connection",
    "keep-alive", "strict-transport-security", "report-to",
}

# Sent into the framed page so in-frame link clicks stay proxied. Without it
# the first page frames fine and every link after it gets blocked again.
SHIM = """<script>(function(){
var P=%s;
document.addEventListener('click',function(e){
  var a=e.target.closest&&e.target.closest('a[href]');
  if(!a||a.target==='_blank')return;
  var u;try{u=new URL(a.getAttribute('href'),document.baseURI)}catch(_){return}
  if(!/^https?:$/.test(u.protocol))return;
  e.preventDefault();
  window.location.href=P+encodeURIComponent(u.href);
},true);
})();</script>"""

current_child = None


class Handler(BaseHTTPRequestHandler):
    server_version = "infone"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---------- helpers ----------

    def send_bytes(self, body, ctype="text/html; charset=utf-8", code=200,
                   extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def fail(self, code, msg):
        self.send_bytes(msg.encode(), "text/plain; charset=utf-8", code)

    # ---------- routes ----------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/proxy":
            return self.proxy(urllib.parse.parse_qs(parts.query).get("url", [""])[0])
        return self.static(parts.path)

    def do_POST(self):
        if urllib.parse.urlsplit(self.path).path != "/launch":
            return self.fail(404, "not found")

        length = int(self.headers.get("Content-Length") or 0)
        try:
            cmd = json.loads(self.rfile.read(length) or b"{}").get("cmd", "")
        except json.JSONDecodeError:
            return self.fail(400, "bad json")

        argv = ALLOWED.get(cmd)
        if not argv:
            return self.fail(403, "not allowed: %s" % cmd)

        global current_child
        if current_child and current_child.poll() is None:
            current_child.terminate()          # one game at a time

        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        try:
            current_child = subprocess.Popen(argv, env=env,
                                             start_new_session=True)
        except OSError as exc:
            return self.fail(500, "could not start %s: %s" % (cmd, exc))

        self.send_bytes(b'{"ok":true}', "application/json")

    def static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(ROOT, rel))
        if not full.startswith(ROOT) or not os.path.isfile(full):
            return self.fail(404, "not found")

        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".js":   "text/javascript; charset=utf-8",
            ".json": "application/json",
            ".svg":  "image/svg+xml",
            ".png":  "image/png",
        }.get(os.path.splitext(full)[1], "application/octet-stream")

        with open(full, "rb") as fh:
            body = fh.read()
        # No caching: you are going to be editing index.html constantly.
        self.send_bytes(body, ctype, extra=[("Cache-Control", "no-store")])

    def proxy(self, target):
        if not re.match(r"^https?://", target or ""):
            return self.fail(400, "pass ?url= with an http(s) address")

        req = urllib.request.Request(target, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "identity",   # keeps the body editable
            "Accept-Language": self.headers.get("Accept-Language", "en"),
        })

        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                body, headers, final = res.read(), res.headers, res.geturl()
                code = res.status
        except urllib.error.HTTPError as exc:
            body, headers, final, code = exc.read(), exc.headers, target, exc.code
        except Exception as exc:
            return self.fail(502, "upstream failed: %s" % exc)

        ctype = headers.get("Content-Type", "application/octet-stream")
        if "html" in ctype.lower():
            body = self.rewrite(body, final)

        passthru = [(k, v) for k, v in headers.items()
                    if k.lower() not in STRIP]
        self.send_bytes(body, ctype, code, passthru)

    def rewrite(self, body, final):
        """Point relative URLs at the real origin, and keep links proxied."""
        html = body.decode("utf-8", "replace")
        root = "http://%s/proxy?url=" % self.headers.get("Host", "localhost:8080")
        inject = '<base href="%s">%s' % (
            final.replace('"', "%22"), SHIM % json.dumps(root))

        if re.search(r"<head[^>]*>", html, re.I):
            html = re.sub(r"(<head[^>]*>)", r"\1" + inject, html,
                          count=1, flags=re.I)
        else:
            html = inject + html
        return html.encode("utf-8")


def main():
    if not os.path.isdir(ROOT):
        sys.exit("No such directory: %s" % ROOT)

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True

    def bye(*_):
        if current_child and current_child.poll() is None:
            current_child.terminate()
        srv.shutdown()
    signal.signal(signal.SIGTERM, bye)

    print("infone-server on http://%s:%d serving %s" % (HOST, PORT, ROOT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        bye()


if __name__ == "__main__":
    main()
