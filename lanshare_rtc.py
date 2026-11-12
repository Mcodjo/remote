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
<title>LanShare — Partager mon écran</title><style>
:root{color-scheme:light dark;--bg:#fafaf8;--card:#fff;--fg:#14171f;--mut:#667085;--line:#e6e8ec;--acc:#16a34a;--acc2:#0f7a37;--soft:#e8f7ee;--shadow:0 1px 2px #0001,0 8px 30px #0000000d}
@media(prefers-color-scheme:dark){:root{--bg:#0c0e12;--card:#14171d;--fg:#eef0f4;--mut:#98a2b3;--line:#232832;--acc:#22c55e;--acc2:#4ade80;--soft:#10281a;--shadow:none}}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,"Segoe UI",sans-serif}
a{color:inherit}.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
button,.btn{font:inherit;font-weight:600;border:0;border-radius:12px;padding:12px 22px;cursor:pointer;text-decoration:none;display:inline-flex;gap:8px;align-items:center;transition:.15s}
.btn.p{background:var(--acc);color:#fff}.btn.p:hover{filter:brightness(.92)}
.btn.s{background:var(--card);color:var(--fg);border:1px solid var(--line)}.btn.s:hover{border-color:var(--acc)}
button:disabled{opacity:.5}
/* nav */
nav{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
nav .wrap{display:flex;align-items:center;gap:28px;height:62px}.logo{font-weight:800;font-size:19px;display:flex;gap:8px;align-items:center;text-decoration:none}
.logo i{width:30px;height:30px;border-radius:9px;background:var(--acc);display:grid;place-items:center;color:#fff;font-style:normal;font-size:16px}
nav .links{display:flex;gap:22px;color:var(--mut);font-size:15px;flex:1}nav .links a{text-decoration:none}nav .links a:hover{color:var(--fg)}
nav .btn{padding:8px 16px}
/* promo banner */
.promo{background:var(--soft);border-bottom:1px solid var(--line);font-size:14px}
.promo .wrap{display:flex;gap:10px;justify-content:center;align-items:center;padding:9px 20px;flex-wrap:wrap}
.promo code{font:700 15px ui-monospace,Consolas,monospace;background:var(--card);border:1px dashed var(--acc);border-radius:8px;padding:2px 10px;letter-spacing:.15em}
.promo button{padding:3px 12px;font-size:13px;border-radius:8px;background:var(--acc);color:#fff}
/* hero */
.hero{text-align:center;padding:72px 0 56px}
.badge{display:inline-block;background:var(--soft);color:var(--acc2);border-radius:99px;padding:4px 14px;font-size:13px;font-weight:600}
h1{font-size:clamp(34px,6vw,58px);line-height:1.08;letter-spacing:-.03em;margin:18px auto 14px;max-width:760px}
h1 em{font-style:normal;color:var(--acc)}.sub{color:var(--mut);font-size:19px;max-width:600px;margin:0 auto 28px}
.row{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.opts{display:flex;gap:18px;justify-content:center;flex-wrap:wrap;margin:0 0 22px;color:var(--mut);font-size:14px}
.opts select{font:inherit;padding:5px 8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
#err{color:#e5484d;min-height:1.4em;margin:14px 0 0;font-size:14px}
.live{max-width:640px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:var(--shadow);text-align:left}
.live .k{color:var(--mut);font-size:13px;text-transform:uppercase;letter-spacing:.08em;margin-top:12px}
.live .url{font:700 clamp(20px,4vw,28px) ui-monospace,Consolas,monospace;color:var(--acc);word-break:break-all}
.live video{width:100%;max-height:260px;border-radius:12px;background:#000;margin-top:14px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--acc);margin-right:6px;animation:p 1.6s infinite}@keyframes p{50%{opacity:.3}}
/* sections */
section{padding:56px 0}.head{text-align:center;margin-bottom:34px}.head h2{font-size:clamp(26px,4vw,38px);letter-spacing:-.02em;margin:0 0 8px}.head p{color:var(--mut);margin:0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:var(--shadow);overflow:hidden}
.card h3{margin:0 0 4px;font-size:18px}.card p{margin:0;color:var(--mut);font-size:15px}
.card.big{grid-column:1/-1;display:grid;grid-template-columns:1fr 1.1fr;gap:28px;align-items:center}
ul.ck{list-style:none;padding:0;margin:14px 0 0}ul.ck li{padding:4px 0 4px 28px;position:relative;color:var(--fg);font-size:15px}
ul.ck li:before{content:"✓";position:absolute;left:0;color:var(--acc);font-weight:800}
.mock{margin-top:16px;background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:12px;font-size:13px;color:var(--mut)}
.win{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--bg)}
.win .bar{display:flex;gap:6px;padding:9px 12px;border-bottom:1px solid var(--line);align-items:center;font-size:12px;color:var(--mut)}
.win .bar b{width:10px;height:10px;border-radius:50%;background:var(--line)}
.win .scr{aspect-ratio:16/9;background:linear-gradient(135deg,#0f3d2a,#16a34a 55%,#86efac);position:relative}
.win .scr:after{content:"";position:absolute;left:12%;top:18%;width:46%;height:46%;border-radius:8px;background:#ffffff30;border:1px solid #ffffff55}
.eq{display:flex;gap:4px;align-items:flex-end;height:40px}.eq i{flex:1;background:var(--acc);border-radius:3px;animation:eq 1.2s ease-in-out infinite}
.eq i:nth-child(2n){animation-delay:.3s}.eq i:nth-child(3n){animation-delay:.6s}@keyframes eq{0%,100%{height:20%}50%{height:100%}}
.pins{display:flex;gap:8px}.pins i{flex:1;height:38px;border-radius:9px;border:1px solid var(--line);display:grid;place-items:center;font:700 18px ui-monospace;font-style:normal;color:var(--fg)}
.devs{display:flex;gap:8px;align-items:center;justify-content:space-between}.devs span{font-size:26px}.devs hr{flex:1;border:0;border-top:2px dashed var(--acc);margin:0}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{background:var(--soft);color:var(--acc2);border-radius:99px;padding:3px 10px;font-size:12px;font-weight:600}
.two{display:grid;grid-template-columns:1fr 1fr;gap:44px;align-items:center}.two h2{font-size:clamp(26px,4vw,36px);letter-spacing:-.02em;margin:0 0 10px}.two p{color:var(--mut)}
.steps{counter-reset:s}.steps .card{position:relative}.steps .n{width:34px;height:34px;border-radius:50%;background:var(--soft);color:var(--acc2);display:grid;place-items:center;font-weight:800;margin-bottom:12px}
.who{display:flex;gap:12px;align-items:center;margin-top:14px;padding-top:14px;border-top:1px solid var(--line);font-size:14px}.who i{width:40px;height:40px;border-radius:50%;background:var(--soft);display:grid;place-items:center;font-style:normal;font-size:20px}
.who small{display:block;color:var(--mut)}
.cta{text-align:center;background:var(--card);border:1px solid var(--line);border-radius:24px;padding:56px 20px;box-shadow:var(--shadow)}
.cta h2{font-size:clamp(26px,4vw,40px);letter-spacing:-.02em;margin:0 0 8px}.cta p{color:var(--mut);margin:0 0 24px}.cta small{display:block;margin-top:18px;color:var(--mut)}
footer{border-top:1px solid var(--line);margin-top:56px;padding:44px 0 24px;color:var(--mut);font-size:14px}
footer .cols{display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr;gap:28px}footer h4{color:var(--fg);margin:0 0 10px;font-size:14px}
footer a{display:block;text-decoration:none;padding:3px 0}footer a:hover{color:var(--fg)}
footer .bot{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;border-top:1px solid var(--line);margin-top:28px;padding-top:18px}
@media(max-width:800px){.grid,.two,footer .cols{grid-template-columns:1fr}.card.big{grid-template-columns:1fr}nav .links{display:none}nav .wrap{justify-content:space-between}.hero{padding-top:44px}}
</style>
<nav><div class="wrap"><a class="logo" href="#top"><i>▶</i>LanShare</a>
 <div class="links"><a href="#fonctions">Fonctionnalités</a><a href="#comment">Comment ça marche</a><a href="#usages">Cas d'usage</a><a href="#aide">Aide</a></div>
 <a class="btn s" href="#aide">Aide</a><a class="btn p" href="#top">Partager</a></div></nav>

<div class="promo"><div class="wrap"><span>🔒 Code PIN à donner aux spectateurs :</span><code id="pinv">__PIN__</code><button id="cpin">Copier</button></div></div>

<header class="hero wrap" id="top">
 <span class="badge">Réseau local · Sans Internet · Sans compte</span>
 <h1>Affiche l'écran de ton PC sur <em>n'importe quel appareil</em></h1>
 <p class="sub">Comme sur une télé, mais en direct sur ton réseau Wi‑Fi : vidéo et son passent directement d'un appareil à l'autre.</p>

 <div id="setup">
  <div class="opts">
   <label>Mode <select id="mode"><option value="detail">Texte / documents (net)</option><option value="motion" selected>Vidéo / jeux (fluide)</option></select></label>
   <label><input type="checkbox" id="audio" checked> Partager le son</label>
  </div>
  <div class="row"><button class="btn p" id="go">🖥️ Partager mon écran</button><a class="btn s" href="#comment">Voir comment ça marche</a></div>
  <p id="err"></p>
 </div>

 <div class="live" id="live" hidden>
  <div><span class="dot"></span><b>Partage en cours</b> — <span class="mut">👥 <b id="n">0</b> spectateur(s) connecté(s)</span></div>
  <div class="k">Adresse à ouvrir sur l'autre appareil</div>
  <div class="url" id="urlv">__URL__</div>
  <div class="k">Code PIN</div><div class="url" style="letter-spacing:.2em">__PIN__</div>
  <div class="row" style="justify-content:flex-start;margin-top:14px">
   <button class="btn s" id="curl">Copier l'adresse</button><button class="btn s" id="direct" data-link="__DIRECT__">Copier le lien direct (avec PIN)</button></div>
  <video id="prev" muted autoplay playsinline></video>
  <div class="row" style="margin-top:16px"><button class="btn s" id="stop">⏹ Arrêter le partage</button></div>
 </div>
</header>

<section id="fonctions"><div class="wrap">
 <div class="head"><h2>Tout ce qu'il faut, rien de superflu</h2><p>Pensé pour le réseau local : rapide, privé, sans installation côté spectateur.</p></div>
 <div class="grid">
  <div class="card big"><div><h3>Partage en un clic</h3><p>Choisis l'écran ou la fenêtre, c'est parti.</p>
    <ul class="ck"><li>Écran entier, fenêtre ou onglet au choix</li><li>Curseur de la souris visible</li><li>Mode « texte net » ou « vidéo fluide »</li><li>Reconnexion automatique si le Wi‑Fi coupe</li></ul></div>
   <div class="win"><div class="bar"><b></b><b></b><b></b>&nbsp; Mon écran</div><div class="scr"></div></div></div>
  <div class="card"><h3>Pair‑à‑pair</h3><p>Aucun cloud : la vidéo ne quitte pas ta maison.</p><div class="mock devs"><span>💻</span><hr><span>📺</span></div></div>
  <div class="card"><h3>Son inclus</h3><p>Partage aussi le son (Chrome/Edge sous Windows).</p><div class="mock"><div class="eq"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div></div></div>
  <div class="card"><h3>Protégé par PIN</h3><p>Seuls ceux qui ont le code voient ton écran.</p><div class="mock"><div class="pins"><i>•</i><i>•</i><i>•</i><i>•</i></div></div></div>
  <div class="card"><h3>Plusieurs spectateurs</h3><p>PC, TV, tablette, téléphone : tous en même temps.</p><div class="mock chips"><span class="chip">💻 PC</span><span class="chip">📺 TV</span><span class="chip">📱 Téléphone</span><span class="chip">🧾 Tablette</span></div></div>
  <div class="card"><h3>Zéro installation</h3><p>Le spectateur n'a besoin que d'un navigateur.</p><div class="mock">http://<b style="color:var(--acc)">192.168.x.x</b>:8080</div></div>
  <div class="card"><h3>Basse latence</h3><p>Réglé pour le temps réel sur ton réseau.</p><div class="mock">Connecté · direct (LAN)</div></div>
 </div></div></section>

<section id="comment"><div class="wrap two">
 <div><h2>Côté spectateur, c'est simple</h2><p>Ouvre l'adresse, tape le PIN : l'écran s'affiche. Rien d'autre à faire.</p>
  <ul class="ck"><li>Plein écran en un clic (ou double‑clic sur la vidéo)</li><li>Bouton « Activer le son »</li><li>Affiche la résolution et le nombre d'images par seconde</li><li>Attend tout seul si le partage n'a pas encore démarré</li></ul>
  <p style="margin-top:18px"><button class="btn p" onclick="go()">Démarrer un partage</button></p></div>
 <div class="win"><div class="bar"><b></b><b></b><b></b>&nbsp; 🔒 Connecté<span style="margin-left:auto" class="chip">en direct</span></div>
  <div class="scr" style="aspect-ratio:4/3"></div>
  <div class="bar" style="border-top:1px solid var(--line);border-bottom:0"><span>🔊 Activer le son</span><span style="margin-left:auto">⛶ Plein écran</span></div></div>
</div></section>

<section><div class="wrap steps">
 <div class="head"><h2>En trois étapes</h2><p>Moins d'une minute, du premier clic à l'affichage.</p></div>
 <div class="grid">
  <div class="card"><div class="n">1</div><h3>Partage</h3><p>Clique sur « Partager mon écran » et choisis ce que tu veux montrer.</p></div>
  <div class="card"><div class="n">2</div><h3>Ouvre l'adresse</h3><p>Sur l'autre appareil, saisis l'adresse affichée, ou envoie le lien direct.</p></div>
  <div class="card"><div class="n">3</div><h3>Profite</h3><p>Entre le PIN : ton écran apparaît, avec le son si tu l'as activé.</p></div>
 </div></div></section>

<section id="usages"><div class="wrap">
 <div class="head"><h2>Pour quels usages ?</h2><p>Tout ce qu'on fait habituellement avec un câble HDMI.</p></div>
 <div class="grid">
  <div class="card"><p>« Je lance une présentation ou un document depuis mon PC et toute la salle le voit sur la TV. »</p><div class="who"><i>🎤</i><div><b>Présenter</b><small>Réunion, cours, famille</small></div></div></div>
  <div class="card"><p>« Je regarde un film ou une vidéo sur le grand écran, sans câble ni clé à brancher. »</p><div class="who"><i>🍿</i><div><b>Regarder</b><small>Film, photos, jeux</small></div></div></div>
  <div class="card"><p>« J'utilise une tablette ou un second PC comme écran supplémentaire pour suivre une page. »</p><div class="who"><i>🧑‍💻</i><div><b>Suivre</b><small>Second écran, dépannage</small></div></div></div>
 </div></div></section>

<section><div class="wrap"><div class="cta">
 <h2>Prêt à partager ton écran ?</h2><p>Un clic, une adresse, un PIN. C'est tout.</p>
 <div class="row"><button class="btn p" onclick="go()">🖥️ Partager mon écran</button><a class="btn s" href="#aide">Besoin d'aide ?</a></div>
 <small>Gratuit · Fonctionne hors ligne · Rien n'est envoyé sur Internet</small></div></div></section>

<footer id="aide"><div class="wrap"><div class="cols">
 <div><a class="logo" href="#top" style="padding:0 0 8px"><i>▶</i>LanShare</a>Partage d'écran WebRTC pour ton réseau local.</div>
 <div><h4>Produit</h4><a href="#fonctions">Fonctionnalités</a><a href="#comment">Comment ça marche</a><a href="#usages">Cas d'usage</a></div>
 <div><h4>Aide</h4><a href="#aide">Les deux appareils sur le même Wi‑Fi</a><a href="#aide">Autoriser Python dans le pare‑feu</a><a href="?demo">Tester avec une animation (?demo)</a></div>
 <div><h4>Technique</h4><a href="#aide">Port __PORT__</a><a href="#aide">Pair‑à‑pair WebRTC</a><a href="#aide">Aucune donnée collectée</a></div>
</div><div class="bot"><span>© LanShare — fonctionne en local</span><span>Adresse de ce PC : __IP__</span></div></div></footer>

<script>__SHARED__
const LAN_IP="__IP__", DEMO=location.search.includes('demo');
const $=id=>document.getElementById(id);
let stream,es,pcs={};
function go(){scrollTo({top:0});if(!$('setup').hidden)$('go').click();}
function copy(t,btn){const o=btn.textContent;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).catch(()=>{}).finally(()=>{btn.textContent='Copié ✓';setTimeout(()=>btn.textContent=o,1500);});}
$('cpin').onclick=e=>copy($('pinv').textContent,e.target);
$('curl').onclick=e=>copy($('urlv').textContent,e.target);
$('direct').onclick=e=>copy(e.target.dataset.link,e.target);
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
                                                DIRECT=direct, IP=lanip, PORT=str(port)))
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
