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
import argparse, collections, json, queue, re, secrets, socket, sys, threading, time, webbrowser
from pathlib import Path
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))  # _MEIPASS : exe PyInstaller
WEB = (BASE / "web").resolve()  # résolu : comparé plus bas aux chemins résolus (noms courts Windows, liens)
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
        self.role, self.id, self.q, self.ip = role, cid, queue.Queue(), ""


class Hub:
    """Relais de signalisation : 1 émetteur, N spectateurs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sender, self.viewers = None, {}
        self.control = None  # défini plus tard

    def viewer_ip(self, vid):
        with self.lock:
            v = self.viewers.get(vid)
            return v.ip if v else ""

    def send_sender(self, msg):
        with self.lock:
            if self.sender:
                self.sender.q.put(msg)

    def send_viewer(self, vid, msg):
        with self.lock:
            if vid in self.viewers:
                self.viewers[vid].q.put(msg)

    def broadcast_viewers(self, msg):
        with self.lock:
            for v in self.viewers.values():
                v.q.put(msg)

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
        hook = None
        with self.lock:
            if c.role == "sender":
                if self.sender is c:
                    self.sender = None
                    for v in self.viewers.values():
                        v.q.put({"type": "sender-left"})
                    hook = ("sender", None)
            elif self.viewers.get(c.id) is c:
                del self.viewers[c.id]
                if self.sender:
                    self.sender.q.put({"type": "viewer-left", "id": c.id})
                hook = ("viewer", c.id)
        c.q.put(None)
        if hook and self.control:  # hors verrou du hub (évite les interblocages)
            if hook[0] == "sender":
                self.control.enable(False)
            else:
                self.control.on_viewer_left(hook[1])

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


class Control:
    """Contrôle à distance : désactivé par défaut, activé par l'hôte, accordé à UN spectateur à la fois."""
    MAX_EVENTS_PER_SEC = 800

    def __init__(self, hub, backend, why):
        self.hub, self.backend, self.why = hub, backend, why
        self.enabled, self.controller, self.seq = False, None, 0
        self.lock = threading.Lock()
        self.work = queue.Queue()
        self.stamps = collections.deque()
        hub.control = self
        threading.Thread(target=self._worker, daemon=True).start()

    def caps(self):
        return {"ok": self.backend is not None, "backend": getattr(self.backend, "name", None), "reason": self.why}

    def enable(self, on, w=0, h=0):
        with self.lock:
            if on and not self.backend:
                return False
            prev = self.enabled
            self.enabled = bool(on)
            if on and w and h:
                self.work.put(("monitor", int(w), int(h)))
            if not on:
                self._revoke_locked("disabled")
        if prev != self.enabled:
            self.hub.broadcast_viewers({"type": "control-available", "on": self.enabled})
        return True

    def request(self, vid):
        if not self.enabled:
            return False
        self.hub.send_sender({"type": "control-request", "from": vid, "ip": self.hub.viewer_ip(vid)})
        return True

    def grant(self, vid, allow):
        with self.lock:
            if not self.enabled or vid not in self.hub.viewers:
                return False
            if allow:
                if self.controller and self.controller != vid:
                    self._revoke_locked("replaced")
                self.controller, self.seq = vid, 0
        if allow:
            self.hub.send_viewer(vid, {"type": "control", "granted": True})
            self.hub.send_sender({"type": "control-active", "id": vid})
        else:
            self.hub.send_viewer(vid, {"type": "control", "granted": False, "reason": "denied"})
        return True

    def _revoke_locked(self, reason):
        vid, self.controller = self.controller, None
        if vid:
            self.work.put(("release",))
            self.hub.send_viewer(vid, {"type": "control", "granted": False, "reason": reason})
            self.hub.send_sender({"type": "control-released"})

    def revoke(self, reason="revoked"):
        with self.lock:
            self._revoke_locked(reason)

    def on_viewer_left(self, vid):
        with self.lock:
            if self.controller == vid:
                self._revoke_locked("left")

    def submit(self, vid, seq, events):
        with self.lock:
            if not (self.enabled and self.controller == vid) or seq <= self.seq:
                return False
            self.seq = seq
            now = time.time()
            while self.stamps and now - self.stamps[0] > 1:
                self.stamps.popleft()
            if len(self.stamps) + len(events) > self.MAX_EVENTS_PER_SEC:
                return True  # trop rapide : on ignore silencieusement
            self.stamps.extend([now] * len(events))
        self.work.put(("events", events))
        return True

    def _worker(self):
        while True:
            job = self.work.get()
            try:
                if job[0] == "monitor":
                    self.backend.set_monitor(job[1], job[2])
                elif job[0] == "release":
                    self.backend.release_all()
                else:
                    for e in job[1]:
                        self._apply(e)
            except Exception as ex:  # noqa
                print("Contrôle : erreur d'injection :", ex)

    @staticmethod
    def _f(v, lo, hi):
        return max(lo, min(hi, float(v)))

    def _apply(self, e):
        b, t = self.backend, e.get("t")
        if t == "m":
            b.move(self._f(e["x"], 0, 1), self._f(e["y"], 0, 1))
        elif t == "b":
            if "x" in e:
                b.move(self._f(e["x"], 0, 1), self._f(e["y"], 0, 1))
            if int(e["b"]) in (0, 1, 2):
                b.button(int(e["b"]), bool(e["d"]))
        elif t == "w":
            b.wheel(self._f(e.get("dx", 0), -50, 50), self._f(e.get("dy", 0), -50, 50))
        elif t == "k":
            key = str(e["key"])[:32]
            if key and key not in ("Unidentified", "Dead", "AltGraph", "Process"):
                b.key(bool(e["d"]), key, {k: bool(e.get(k)) for k in ("ctrl", "alt", "meta", "shift")})
        elif t == "s":
            b.text(str(e["s"])[:500])


def make_handler(hub, control, pin, token, lanip):
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
            if u.path == "/control/log" and getattr(control.backend, "name", "") == "fake" and self.loopback():
                return self.send(200, json.dumps(control.backend.log), "application/json")
            if u.path == "/control/caps":
                if not self.loopback():
                    return self.send(403, "forbidden", "text/plain")
                return self.send(200, json.dumps(control.caps()), "application/json")
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
            c.ip = self.ip
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            vip = lanip if self.loopback() else self.ip
            c.q.put({"type": "hello", "ip": vip, "sender": hub.has_sender(), "control": control.enabled and role != "sender"})
            hub.add(c)
            try:
                while True:
                    try:
                        m = c.q.get(timeout=4)
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

        def control_post(self, raw):
            try:
                m = json.loads(raw or b"{}")
                if not isinstance(m, dict):
                    raise ValueError
            except ValueError:
                return self.send(400, "", "text/plain")
            host_only = self.path in ("/control/enable", "/control/grant", "/control/revoke")
            if host_only and not self.loopback():
                return self.send(403, "", "text/plain")
            vid = str(m.get("id", ""))[:64]
            if not host_only and (not self.authed() or vid not in hub.viewers):
                return self.send(403, "", "text/plain")
            ok = False
            if self.path == "/control/enable":
                ok = control.enable(bool(m.get("on")), m.get("w") or 0, m.get("h") or 0)
            elif self.path == "/control/grant":
                ok = control.grant(vid, bool(m.get("allow")))
            elif self.path == "/control/revoke":
                control.revoke("revoked"); ok = True
            elif self.path == "/control/request":
                ok = control.request(vid)
            elif self.path == "/control/release":
                with control.lock:
                    if control.controller == vid:
                        control._revoke_locked("released")
                ok = True
            elif self.path == "/input":
                ev = m.get("ev")
                if not isinstance(ev, list) or len(ev) > 200 or not all(isinstance(e, dict) for e in ev):
                    return self.send(400, "", "text/plain")
                try:
                    ok = control.submit(vid, int(m.get("seq", 0)), ev)
                except (TypeError, ValueError):
                    return self.send(400, "", "text/plain")
            return self.send(200 if ok else 409, json.dumps({"ok": ok}), "application/json")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 300_000:
                return self.send(413, "", "text/plain")
            raw = self.rfile.read(n)
            if self.path == "/bye":
                if self.loopback() and hub.sender:
                    hub.remove(hub.sender)
                return self.send(204)
            if self.path.startswith("/control/") or self.path == "/input":
                return self.control_post(raw)
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
    p.add_argument("--no-control", action="store_true", help="désactive le contrôle à distance")
    p.add_argument("--fake-input", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-browser", action="store_true", help="n'ouvre pas le navigateur")
    a = p.parse_args()
    pin = None if a.no_pin else (a.pin or f"{secrets.randbelow(10**4):04d}")
    hub, token, ip = Hub(), secrets.token_hex(16), lan_ip()
    from lanshare_input import make_backend
    backend, why = make_backend(a.fake_input) if not a.no_control else (None, "Contrôle à distance désactivé au lancement (--no-control).")
    control = Control(hub, backend, why)
    srv = None
    for port in range(a.port, a.port + 20):
        try:
            ThreadingHTTPServer.daemon_threads = True
            srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(hub, control, pin, token, ip))
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
