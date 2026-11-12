# LanShare — partage d'écran sur le réseau local

## Version WebRTC (recommandée) — `lanshare_rtc.py`
Aucune installation hormis Python 3.8+.

1. Double-clic sur `start.bat` (Windows) / `start.command` (Mac) / `./start.sh` (Linux).
2. Le navigateur s'ouvre → **Partager mon écran** → choisis l'écran.
3. Sur l'autre appareil : ouvre l'adresse affichée (`http://192.168.x.x:8080/`) et tape le PIN.

- Vidéo + son en pair-à-pair, sans cloud ni STUN. Plusieurs spectateurs possibles.
- Le son système n'est capturé que par Chrome/Edge sous Windows.
- `?demo` sur `/share` (http://localhost:8080/share?demo) teste sans partager l'écran.
- Aide intégrée : `/help` (diagnostic automatique, dépannage, pare-feu, FAQ).
- Netteté : mode « Netteté maximale » (VP9, résolution native jusqu’à 4K, 30 Mb/s) ou « Vidéo et jeux » ; la qualité réellement envoyée est affichée en direct.
- **Contrôle à distance** (souris, clavier, molette) : désactivé par défaut. L'hôte l'active, puis accepte chaque demande ; il peut reprendre la main à tout moment. Windows (SendInput) et Linux/X11 (XTest), sans dépendance ; `--no-control` le désactive.
- Options : `--port`, `--pin 1234`, `--no-pin`, `--no-browser`, `--no-control`.

## Version MJPEG simple — `lanshare.py`
`pip install mss pillow` puis `python lanshare.py share` (sans son, plus lourde en réseau).

## Interface
Police **DM Sans** (DM Mono pour les commandes) et icônes **Lucide** (ISC), embarquées dans `web/` : l'interface fonctionne 100 % hors ligne.
Pages : `web/share.html`, `web/view.html`, `web/login.html`, `web/help.html` ; styles dans `web/app.css`.
Pour régénérer les icônes : `python tools/make_icons.py <dossier lucide-static/icons>`.

## LanShare.exe (Windows, sans Python)
Onglet **Actions** du dépôt GitHub → « Build LanShare.exe (Windows) » → *Run workflow* → télécharger l'artefact `LanShare-windows`.
Un tag `v1.0` publie aussi l'exe dans les Releases. L'exe n'est pas signé : SmartScreen peut demander « Informations complémentaires » puis « Exécuter quand même ».
