#!/usr/bin/env python3
"""
lanshare — partage l'écran de ce PC vers n'importe quel autre appareil du
même réseau local (PC, TV, tablette, téléphone), sans Internet, sans compte.

  Sur le PC à partager :   python lanshare.py share
  Sur l'autre appareil :   ouvrir l'adresse affichée dans un navigateur
                           (ou: python lanshare.py find   pour la découvrir)

Le flux ne quitte jamais le réseau local (MJPEG sur HTTP, latence très faible).
"""
import argparse, hashlib, io, json, secrets, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse, parse_qs

DISCOVERY_PORT = 50505
BOUNDARY = b"lanshareframe"

PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>lanshare</title>
<style>
html,body{margin:0;height:100%;background:#000;color:#ccc;font:14px system-ui}
#v{width:100vw;height:100vh;object-fit:contain;display:block}
#bar{position:fixed;top:8px;right:8px;opacity:.0;transition:.3s}
body:hover #bar{opacity:.85}
button{background:#222;color:#fff;border:1px solid #555;border-radius:6px;padding:6px 12px;cursor:pointer}
</style>
<img id="v" src="/stream">
<div id="bar"><button onclick="document.documentElement.requestFullscreen()">Plein écran</button></div>
<script>
const v=document.getElementById('v');
v.onerror=()=>setTimeout(()=>v.src='/stream?'+Date.now(),1000); // reconnexion auto
</script></html>"""

LOGIN = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<body style="background:#111;color:#eee;font:18px system-ui;display:grid;place-items:center;height:100vh;margin:0">
<form method="get" action="/login"><p>Code PIN affiché sur le PC :</p>
<input name="pin" inputmode="numeric" autofocus style="font-size:28px;width:6em;text-align:center">
<button style="font-size:20px">OK</button>%s</form>"""


def lan_ip():
    """IP locale utilisée pour sortir sur le LAN (aucun paquet n'est envoyé)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ----------------------------------------------------------------- capture
class Capture(threading.Thread):
    """Capture l'écran en boucle; ne ré-encode que si l'image a changé."""

    def __init__(self, monitor, fps, quality, scale, pattern=False):
        super().__init__(daemon=True)
        self.monitor, self.fps, self.quality, self.scale = monitor, fps, quality, scale
        self.pattern = pattern
        self.cond = threading.Condition()
        self.frame, self.seq = None, 0
        self.error = None

    def _grab_pattern(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (960, 540), (20, 20, 40))
        ImageDraw.Draw(img).text((20, 20), time.strftime("lanshare test %H:%M:%S"), fill="white")
        return img

    def run(self):
        from PIL import Image
        try:
            if self.pattern:
                grab = self._grab_pattern
            else:
                import mss
                sct = mss.mss()
                mon = sct.monitors[self.monitor]

                def grab():
                    s = sct.grab(mon)
                    return Image.frombytes("RGB", s.size, s.bgra, "raw", "BGRX")
            last_hash, last_sent, period = None, 0.0, 1.0 / self.fps
            while True:
                t0 = time.time()
                img = grab()
                h = hashlib.blake2b(img.tobytes(), digest_size=8).digest()
                # envoie si changé, ou toutes les 2 s (garde la connexion vivante)
                if h != last_hash or t0 - last_sent > 2:
                    if self.scale < 1:
                        img = img.resize((int(img.width * self.scale), int(img.height * self.scale)),
                                         Image.BILINEAR)
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=self.quality)
                    with self.cond:
                        self.frame, self.seq = buf.getvalue(), self.seq + 1
                        self.cond.notify_all()
                    last_hash, last_sent = h, t0
                time.sleep(max(0, period - (time.time() - t0)))
        except Exception as e:  # noqa
            self.error = e
            with self.cond:
                self.cond.notify_all()


# ------------------------------------------------------------------ serveur
def make_handler(cap, pin, token):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _authed(self, q):
            if not pin:
                return True
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return "t" in c and secrets.compare_digest(c["t"].value, token)

        def _send(self, code, body, ctype="text/html; charset=utf-8", extra=()):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/login":
                if pin and secrets.compare_digest(q.get("pin", [""])[0], pin):
                    return self._send(302, "", extra=[("Location", "/"),
                                      ("Set-Cookie", f"t={token}; Path=/; HttpOnly; SameSite=Strict")])
                return self._send(403, LOGIN % "<p style='color:#f66'>Code incorrect</p>")
            if not self._authed(q):
                return self._send(401, LOGIN % "")
            if u.path == "/":
                return self._send(200, PAGE)
            if u.path == "/stream":
                return self._stream()
            self._send(404, "not found", "text/plain")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            seen = 0
            try:
                while True:
                    with cap.cond:
                        cap.cond.wait_for(lambda: cap.seq != seen or cap.error, timeout=5)
                        if cap.error:
                            return
                        frame, seen = cap.frame, cap.seq
                    if frame is None:
                        continue
                    self.wfile.write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
    return H


def announce(name, port, pin_required, stop):
    """Diffuse la présence du partage sur le LAN (UDP broadcast) pour `find`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = json.dumps({"app": "lanshare", "name": name, "port": port, "pin": pin_required}).encode()
    while not stop.is_set():
        try:
            s.sendto(msg, ("255.255.255.255", DISCOVERY_PORT))
        except OSError:
            pass
        stop.wait(2)


def cmd_share(a):
    if not a.test_pattern:
        try:
            import mss  # noqa
        except ImportError:
            sys.exit("Module manquant : pip install mss pillow")
    pin = None if a.no_pin else (a.pin or f"{secrets.randbelow(10**4):04d}")
    cap = Capture(a.monitor, a.fps, a.quality, a.scale, a.test_pattern)
    cap.start()
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(cap, pin, secrets.token_hex(16)))
    srv.daemon_threads = True
    ip = lan_ip()
    url = f"http://{ip}:{a.port}/"
    stop = threading.Event()
    threading.Thread(target=announce, args=(socket.gethostname(), a.port, bool(pin), stop),
                     daemon=True).start()
    print(f"\n  Partage d'écran actif sur le réseau local\n  Adresse : {url}")
    print(f"  Code PIN : {pin}" if pin else "  (aucun PIN — tout le réseau peut voir l'écran)")
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)
    except ImportError:
        print("  (pip install qrcode pour afficher un QR code)")
    print("  Ctrl+C pour arrêter.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if cap.error:
            print("Erreur de capture :", cap.error)


def cmd_find(a):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", DISCOVERY_PORT))
    s.settimeout(0.5)
    found, end = {}, time.time() + a.wait
    print(f"Recherche pendant {a.wait}s…")
    while time.time() < end:
        try:
            data, (ip, _) = s.recvfrom(1024)
            d = json.loads(data)
            if d.get("app") == "lanshare":
                found[ip] = d
        except (socket.timeout, ValueError):
            pass
    if not found:
        print("Aucun partage trouvé (pare-feu ? même réseau/Wi‑Fi ?).")
    for ip, d in found.items():
        print(f"  {d['name']:20} http://{ip}:{d['port']}/" + ("  [PIN requis]" if d["pin"] else ""))
    if found and a.open:
        import webbrowser
        webbrowser.open(f"http://{next(iter(found))}:{next(iter(found.values()))['port']}/")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("share", help="partager cet écran")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--fps", type=int, default=20, help="images/s max (défaut 20)")
    s.add_argument("--quality", type=int, default=70, help="qualité JPEG 1-95 (défaut 70)")
    s.add_argument("--scale", type=float, default=1.0, help="réduction, ex 0.5 (défaut 1.0)")
    s.add_argument("--monitor", type=int, default=1, help="1=écran principal, 0=tous (défaut 1)")
    s.add_argument("--pin", help="PIN imposé (sinon aléatoire à 4 chiffres)")
    s.add_argument("--no-pin", action="store_true", help="désactiver le PIN")
    s.add_argument("--test-pattern", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_share)
    f = sub.add_parser("find", help="trouver les partages sur le réseau")
    f.add_argument("--wait", type=int, default=4)
    f.add_argument("--open", action="store_true", help="ouvrir le premier trouvé")
    f.set_defaults(fn=cmd_find)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
