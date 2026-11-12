#!/usr/bin/env python3
"""
LanShare RTC — partage d'écran WebRTC sur le réseau local.

  Lance :  python lanshare_rtc.py      (ou double-clic sur start.bat / start.command)
  1. Le navigateur du PC s'ouvre : clique « Partager mon écran », choisis l'écran.
  2. Sur l'autre appareil (PC, TV, tablette, téléphone) : ouvre l'adresse affichée.

Aucune dépendance (Python 3.8+ seulement). La vidéo (et le son) circule en
pair-à-pair directement entre les deux appareils : pas de cloud, pas de STUN,
la machine qui héberge ne fait que l'échange initial (signalisation).
"""
import argparse, json, queue, re, secrets, socket, sys, threading, time, webbrowser
from pathlib import Path
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

WEB = Path(__file__).resolve().parent / "web"
STATIC = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
          ".woff2": "font/woff2"}
_ICON = re.compile(r"\{\{i:([\w-]+)(?::(\w+))?\}\}")


def render(name, **kw):
    """Charge web/<name>.html, remplace {{i:icône}} (sprite Lucide) et les __CLÉS__."""
    html = (WEB / f"{name}.html").read_text(encoding="utf-8")
    html = _ICON.sub(lambda m: f'<svg class="ic {m.group(2) or ""}" aria-hidden="true"><use href="/web/icons.svg#{m.group(1)}"/></svg>', html)
    for k, v in kw.items():
        html = html.replace(f"__{k}__", v)
    return html


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Client:
    def __init__(self, role, cid):
        self.role, self.id, self.q = role, cid, queue.Queue()


class Hub:
    """Relais de signalisation : 1 émetteur, N spectateurs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sender, self.viewers = None, {}

    def add(self, c):
        with self.lock:
            if c.role == "sender":
                if self.sender:
                    self.sender.q.put({"type": "replaced"})
                    self.sender.q.put(None)
                self.sender = c
                for v in self.viewers.values():
                    c.q.put({"type": "viewer-joined", "id": v.id})
            else:
                old = self.viewers.get(c.id)
                if old:
                    old.q.put(None)
                self.viewers[c.id] = c
                if self.sender:
                    self.sender.q.put({"type": "viewer-joined", "id": c.id})

    def remove(self, c):
        with self.lock:
            if c.role == "sender":
                if self.sender is c:
                    self.sender = None
                    for v in self.viewers.values():
                        v.q.put({"type": "sender-left"})
            elif self.viewers.get(c.id) is c:
                del self.viewers[c.id]
                if self.sender:
                    self.sender.q.put({"type": "viewer-left", "id": c.id})
        c.q.put(None)

    def route(self, msg):
        with self.lock:
            if msg.get("role") == "sender":
                v = self.viewers.get(msg.get("to"))
                if v:
                    v.q.put({"type": msg["type"], "sdp": msg["sdp"]})
                    return True
            elif self.sender:
                self.sender.q.put({"type": "answer", "from": msg.get("id"), "sdp": msg["sdp"]})
                return True
        return False

    def has_sender(self):
        return self.sender is not None


def make_handler(hub, pin, token, lanip):
    fails = {}  # ip -> (nb, bloqué jusqu'à)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        @property
        def ip(self):
            return self.client_address[0].replace("::ffff:", "")

        def loopback(self):
            return self.ip in ("127.0.0.1", "::1")

        def authed(self):
            if not pin or self.loopback():
                return True
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return "t" in c and secrets.compare_digest(c["t"].value, token)

        def send(self, code, body="", ctype="text/html; charset=utf-8", headers=()):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def login(self, err):
            return render("login", ERR=err, ERRHIDE="" if err else "hidden",
                          INVALID='aria-invalid="true"' if err else "")

        def static(self, path):
            f = (WEB / path[len("/web/"):]).resolve()
            if f.parent != WEB or f.suffix not in STATIC or not f.is_file():
                return self.send(404, "not found", "text/plain")
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC[f.suffix])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path.startswith("/web/"):
                return self.static(u.path)
            if u.path == "/":
                given = q.get("pin", [None])[0]
                if given is not None and pin:
                    n, until = fails.get(self.ip, (0, 0))
                    if time.time() < until:
                        return self.send(429, self.login("Trop d'essais, patiente 30 secondes."))
                    if secrets.compare_digest(given, pin):
                        fails.pop(self.ip, None)
                        return self.send(302, headers=[("Location", "/"),
                                         ("Set-Cookie", f"t={token}; Path=/; HttpOnly; SameSite=Strict")])
                    n += 1
                    fails[self.ip] = (n, time.time() + 30 if n >= 5 else 0)
                    return self.send(403, self.login("Code incorrect. Vérifie le PIN affiché sur le PC."))
                if not self.authed():
                    return self.send(401, self.login(""))
                return self.send(200, render("view"))
            if u.path == "/share":
                if not self.loopback():
                    return self.send(403, "Cette page doit être ouverte sur le PC qui partage (http://localhost).",
                                     "text/plain; charset=utf-8")
                port = self.server.server_address[1]
                url = f"http://{lanip}:{port}/"
                direct = f"{url}?pin={pin}" if pin else url
                return self.send(200, render("share", URL=url, PIN=pin or "(aucun)",
                                                DIRECT=direct, IP=lanip, PORT=str(port)))
            if u.path == "/help":
                port = self.server.server_address[1]
                home = "/share" if self.loopback() else "/"
                return self.send(200, render("help", HOME=home, IP=lanip, PORT=str(port)))
            if u.path == "/status":
                body = json.dumps({"sender": hub.has_sender(), "viewers": len(hub.viewers),
                                   "ip": lanip, "port": self.server.server_address[1]})
                return self.send(200, body, "application/json")
            if u.path == "/events":
                return self.events(q)
            self.send(404, "not found", "text/plain")

        def events(self, q):
            role = q.get("role", ["viewer"])[0]
            if role == "sender" and not self.loopback():
                return self.send(403, "forbidden", "text/plain")
            if role != "sender" and not self.authed():
                return self.send(401, "pin", "text/plain")
            c = Client(role, q.get("id", [secrets.token_hex(4)])[0][:64])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            vip = lanip if self.loopback() else self.ip
            c.q.put({"type": "hello", "ip": vip, "sender": hub.has_sender()})
            hub.add(c)
            try:
                while True:
                    try:
                        m = c.q.get(timeout=10)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    if m is None:
                        break
                    self.wfile.write(b"data: " + json.dumps(m).encode() + b"\n\n")
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                hub.remove(c)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 300_000:
                return self.send(413, "", "text/plain")
            raw = self.rfile.read(n)
            if self.path == "/bye":
                if self.loopback() and hub.sender:
                    hub.remove(hub.sender)
                return self.send(204)
            if self.path == "/signal":
                try:
                    msg = json.loads(raw)
                    ok = msg["role"] == "sender" and self.loopback() or msg["role"] == "viewer" and self.authed()
                    if not ok or msg.get("type") not in ("offer", "answer") or not isinstance(msg.get("sdp"), str):
                        return self.send(403, "", "text/plain")
                except (ValueError, KeyError, TypeError):
                    return self.send(400, "", "text/plain")
                return self.send(200 if hub.route(msg) else 404, "", "text/plain")
            self.send(404, "", "text/plain")

    return H


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Partage d'écran WebRTC sur le réseau local")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--pin", help="PIN imposé (sinon 4 chiffres aléatoires)")
    p.add_argument("--no-pin", action="store_true", help="pas de PIN")
    p.add_argument("--no-browser", action="store_true", help="n'ouvre pas le navigateur")
    a = p.parse_args()
    pin = None if a.no_pin else (a.pin or f"{secrets.randbelow(10**4):04d}")
    hub, token, ip = Hub(), secrets.token_hex(16), lan_ip()
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            ThreadingHTTPServer.daemon_threads = True
            srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(hub, pin, token, ip))
            break
        except OSError:
            continue
    if not srv:
        sys.exit("Aucun port libre trouvé.")
    port = srv.server_address[1]
    print("\n  ┌─ LanShare RTC ─────────────────────────────────────────")
    print(f"  │ 1) Sur CE PC, ouvre : http://localhost:{port}/share   (s'ouvre tout seul)")
    print(f"  │ 2) Sur l'autre appareil : http://{ip}:{port}/")
    print(f"  │    Code PIN : {pin}" if pin else "  │    (aucun PIN)")
    print("  └─ Ctrl+C pour quitter. Si Windows demande l'accès réseau : « Autoriser ».\n")
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://localhost:{port}/share")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Arrêt.")


if __name__ == "__main__":
    main()
