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
import argparse, json, queue, secrets, socket, sys, threading, time, webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SHARED_JS = r"""
function gathered(pc){return new Promise(res=>{
  if(pc.iceGatheringState==='complete')return res();
  const t=setTimeout(res,2500);
  pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete'){clearTimeout(t);res();}});
});}
// Les navigateurs masquent l'IP locale derrière un nom « xxxx.local » (mDNS) qui
// ne se résout pas toujours : on le remplace par l'IP réelle connue du serveur.
function fixSdp(sdp,ip){return sdp.replace(/(a=candidate:\S+ \d+ \w+ \d+ )\S+\.local( \d+ typ)/g,'$1'+ip+'$2');}
function post(body){return fetch('/signal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
"""

CSS = r"""
:root{color-scheme:dark;--bg:#0f1115;--card:#181b22;--fg:#e8eaf0;--mut:#9aa3b2;--acc:#4f8cff;--ok:#3ecf8e}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,Segoe UI,sans-serif}
button{font:inherit;background:var(--acc);color:#fff;border:0;border-radius:10px;padding:12px 22px;cursor:pointer}
button.sec{background:#2a2f3a}button:disabled{opacity:.5}
"""

SHARE_PAGE = r"""<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LanShare — Partager</title><style>__CSS__
main{max-width:720px;margin:0 auto;padding:24px 16px}h1{margin:.2em 0}
.card{background:var(--card);border-radius:14px;padding:18px;margin:16px 0}
.url{font:600 26px ui-monospace,Consolas,monospace;color:var(--ok);word-break:break-all}
.pin{font:700 34px ui-monospace,monospace;letter-spacing:.2em}.mut{color:var(--mut)}
video{width:100%;border-radius:10px;background:#000;max-height:300px}
label{display:block;margin:8px 0}select{font:inherit;padding:6px;border-radius:8px}
</style><main>
<h1>📺 LanShare</h1><p class="mut">Partage d'écran sur ton réseau local, sans Internet.</p>
<div class="card" id="setup">
  <label>Mode <select id="mode"><option value="detail">Texte / documents (net)</option><option value="motion" selected>Vidéo / jeux (fluide)</option></select></label>
  <label><input type="checkbox" id="audio" checked> Partager aussi le son (si le navigateur le permet)</label>
  <p><button id="go">Partager mon écran</button></p>
  <p class="mut" id="err"></p>
</div>
<div class="card" id="live" hidden>
  <p>Sur l'autre appareil, ouvre cette adresse dans le navigateur :</p>
  <div class="url">__URL__</div>
  <p>Code PIN : <span class="pin">__PIN__</span></p>
  <p class="mut">Lien direct sans saisie du PIN : <a id="direct" href="#" style="color:var(--acc)">__DIRECT__</a></p>
  <p>👥 Spectateurs connectés : <b id="n">0</b></p>
  <video id="prev" muted autoplay playsinline></video>
  <p><button class="sec" id="stop">Arrêter le partage</button></p>
</div>
<p class="mut">Astuce : le son système n'est capturé que par Chrome/Edge sous Windows (coche « Partager le son du système »).</p>
</main>
<script>__SHARED__
const LAN_IP="__IP__", DEMO=location.search.includes('demo');
const $=id=>document.getElementById(id);
let stream,es,pcs={};
function demoStream(){const c=document.createElement('canvas');c.width=1280;c.height=720;const g=c.getContext('2d');let t=0;
  setInterval(()=>{t++;g.fillStyle='#123';g.fillRect(0,0,1280,720);g.fillStyle='#4f8cff';g.fillRect((t*8)%1200,300,80,80);
  g.fillStyle='#fff';g.font='48px sans-serif';g.fillText('LanShare demo '+new Date().toLocaleTimeString(),40,80);},33);
  return c.captureStream(30);}
$('go').onclick=async()=>{
  $('err').textContent='';
  try{
    stream=DEMO?demoStream():await navigator.mediaDevices.getDisplayMedia({video:{frameRate:{ideal:30},cursor:'always'},audio:$('audio').checked});
  }catch(e){$('err').textContent='Partage annulé ou refusé : '+e.message;return;}
  const v=stream.getVideoTracks()[0];v.contentHint=$('mode').value;v.onended=stop;
  $('prev').srcObject=stream;$('setup').hidden=true;$('live').hidden=false;
  es=new EventSource('/events?role=sender');
  es.onmessage=async e=>{const m=JSON.parse(e.data);
    if(m.type==='viewer-joined')addViewer(m.id);
    else if(m.type==='viewer-left')drop(m.id);
    else if(m.type==='answer'&&pcs[m.from])try{await pcs[m.from].setRemoteDescription({type:'answer',sdp:m.sdp});}catch(_){}
    else if(m.type==='replaced')stop();};
};
function count(){$('n').textContent=Object.values(pcs).filter(p=>p.connectionState==='connected').length;window.__viewers=+$('n').textContent;}
function drop(id){if(pcs[id]){pcs[id].close();delete pcs[id];count();}}
async function addViewer(id){
  drop(id);
  const pc=new RTCPeerConnection({iceServers:[]});pcs[id]=pc;
  stream.getTracks().forEach(t=>pc.addTrack(t,stream));
  pc.onconnectionstatechange=async()=>{count();
    if(pc.connectionState==='connected'){ // débit LAN généreux
      for(const s of pc.getSenders()){if(s.track&&s.track.kind==='video')try{const p=s.getParameters();
        if(!p.encodings||!p.encodings.length)p.encodings=[{}];p.encodings[0].maxBitrate=15e6;
        p.degradationPreference=$('mode').value==='detail'?'maintain-resolution':'balanced';await s.setParameters(p);}catch(_){}}}};
  await pc.setLocalDescription(await pc.createOffer());await gathered(pc);
  post({role:'sender',to:id,type:'offer',sdp:fixSdp(pc.localDescription.sdp,LAN_IP)});
}
function stop(){fetch('/bye',{method:'POST'});if(es)es.close();Object.keys(pcs).forEach(drop);
  if(stream)stream.getTracks().forEach(t=>t.stop());$('live').hidden=true;$('setup').hidden=false;}
$('stop').onclick=stop;addEventListener('pagehide',()=>navigator.sendBeacon('/bye'));
$('direct').onclick=e=>{e.preventDefault();navigator.clipboard&&navigator.clipboard.writeText($('direct').textContent);$('direct').textContent+=' (copié)';};
</script></html>"""

VIEW_PAGE = r"""<!doctype html><html lang="fr"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>LanShare</title><style>__CSS__
body{background:#000;overflow:hidden}video{width:100vw;height:100vh;object-fit:contain;display:block;background:#000}
#st{position:fixed;inset:0;display:grid;place-items:center;text-align:center;color:var(--mut);pointer-events:none;padding:20px}
#bar{position:fixed;top:10px;right:10px;display:flex;gap:8px;opacity:0;transition:.3s}
body:hover #bar,body.touch #bar{opacity:.9}#info{position:fixed;left:10px;bottom:8px;font-size:12px;color:#fff8;pointer-events:none}
</style>
<video id="v" autoplay muted playsinline></video>
<div id="st"><div><div style="font-size:42px">📺</div><div id="msg">Connexion…</div></div></div>
<div id="bar"><button class="sec" id="snd">🔇 Activer le son</button><button class="sec" id="fs">⛶ Plein écran</button></div>
<div id="info"></div>
<script>__SHARED__
const ID=Math.random().toString(36).slice(2)+Date.now().toString(36);
const $=id=>document.getElementById(id),v=$('v');let pc=null,myIp='',timer;
const say=t=>{$('msg').textContent=t;$('st').style.display=t?'grid':'none';};
function closePc(){clearInterval(timer);if(pc){pc.close();pc=null;}v.srcObject=null;$('info').textContent='';}
async function onOffer(m){
  closePc();say('Connexion au PC…');
  pc=new RTCPeerConnection({iceServers:[]});const me=pc;
  pc.ontrack=e=>{if(v.srcObject!==e.streams[0])v.srcObject=e.streams[0];try{e.receiver.jitterBufferTarget=50;}catch(_){}};
  pc.onconnectionstatechange=()=>{if(me!==pc)return;
    if(pc.connectionState==='connected'){say('');v.play().catch(()=>{});}
    else if(['failed','disconnected'].includes(pc.connectionState))say('Connexion perdue, reconnexion…');};
  await pc.setRemoteDescription({type:'offer',sdp:m.sdp});
  await pc.setLocalDescription(await pc.createAnswer());await gathered(pc);
  post({role:'viewer',id:ID,type:'answer',sdp:fixSdp(pc.localDescription.sdp,myIp)});
  timer=setInterval(async()=>{if(!pc)return;for(const r of (await pc.getStats()).values())
    if(r.type==='inbound-rtp'&&r.kind==='video'){window.__stats=r;$('info').textContent=(r.frameWidth||'?')+'×'+(r.frameHeight||'?')+' · '+Math.round(r.framesPerSecond||0)+' fps';}},1000);
}
const es=new EventSource('/events?role=viewer&id='+ID);
es.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.type==='hello'){myIp=m.ip;say(m.sender?'Connexion au PC…':'En attente du démarrage du partage sur le PC…');}
  else if(m.type==='offer')onOffer(m);
  else if(m.type==='sender-left'){closePc();say('Le partage est terminé. En attente…');}};
es.onerror=()=>say('Connexion au PC perdue… nouvelle tentative');
$('snd').onclick=()=>{v.muted=!v.muted;v.play();$('snd').textContent=v.muted?'🔇 Activer le son':'🔊 Son activé';};
$('fs').onclick=()=>document.documentElement.requestFullscreen?document.documentElement.requestFullscreen():v.webkitEnterFullscreen();
v.ondblclick=$('fs').onclick;addEventListener('touchstart',()=>document.body.classList.add('touch'),{once:true});
</script></html>"""

LOGIN_PAGE = r"""<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LanShare</title><style>__CSS__ body{display:grid;place-items:center;text-align:center}
input{font:700 34px ui-monospace,monospace;width:5em;text-align:center;border-radius:10px;border:1px solid #444;background:#222;color:#fff;padding:8px}
</style><form action="/" method="get"><p style="font-size:20px">Entre le code PIN affiché sur le PC</p>
<input name="pin" inputmode="numeric" autofocus autocomplete="off"> <button>OK</button><p style="color:#f77">__ERR__</p></form></html>"""


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

        def page(self, tpl, **kw):
            s = tpl.replace("__CSS__", CSS).replace("__SHARED__", SHARED_JS)
            for k, v in kw.items():
                s = s.replace(f"__{k}__", v)
            return s

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                given = q.get("pin", [None])[0]
                if given is not None and pin:
                    n, until = fails.get(self.ip, (0, 0))
                    if time.time() < until:
                        return self.send(429, self.page(LOGIN_PAGE, ERR="Trop d'essais, patiente 30 s."))
                    if secrets.compare_digest(given, pin):
                        fails.pop(self.ip, None)
                        return self.send(302, headers=[("Location", "/"),
                                         ("Set-Cookie", f"t={token}; Path=/; HttpOnly; SameSite=Strict")])
                    n += 1
                    fails[self.ip] = (n, time.time() + 30 if n >= 5 else 0)
                    return self.send(403, self.page(LOGIN_PAGE, ERR="Code incorrect"))
                if not self.authed():
                    return self.send(401, self.page(LOGIN_PAGE, ERR=""))
                return self.send(200, self.page(VIEW_PAGE))
            if u.path == "/share":
                if not self.loopback():
                    return self.send(403, "Cette page doit être ouverte sur le PC qui partage (http://localhost).",
                                     "text/plain; charset=utf-8")
                port = self.server.server_address[1]
                url = f"http://{lanip}:{port}/"
                direct = f"{url}?pin={pin}" if pin else url
                return self.send(200, self.page(SHARE_PAGE, URL=url, PIN=pin or "(aucun)",
                                                DIRECT=direct, IP=lanip))
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
