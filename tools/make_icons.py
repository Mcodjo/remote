#!/usr/bin/env python3
"""Génère web/icons.svg (sprite) à partir des icônes officielles Lucide (ISC).
Usage : npm pack lucide-static && tar xzf lucide-static-*.tgz && python tools/make_icons.py package/icons
"""
import re, sys, pathlib
NAMES = """monitor-up monitor tv smartphone tablet laptop lock shield-check copy check users wifi zap volume-2
volume-x maximize minimize arrow-left circle-help life-buoy stethoscope rocket wrench square presentation clapperboard
mouse-pointer-2 plug key-round activity circle-check triangle-alert circle-x loader-circle plus chevron-down
brick-wall network cast cloud-off wifi-off sliders-horizontal gauge graduation-cap user-round play terminal qr-code
refresh-cw type video eye hourglass info arrow-right file-text clipboard-check monitor-play link
keyboard mouse-pointer-click x bell hand""".split()
src = pathlib.Path(sys.argv[1])
out = ['<svg xmlns="http://www.w3.org/2000/svg"><!-- Lucide (ISC) https://lucide.dev -->']
for n in NAMES:
    body = re.search(r"<svg[^>]*>(.*)</svg>", (src / f"{n}.svg").read_text(), re.S).group(1)
    body = re.sub(r"\s+", " ", body).strip()
    out.append(f'<symbol id="{n}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
               f'stroke-linecap="round" stroke-linejoin="round">{body}</symbol>')
out.append("</svg>")
pathlib.Path(__file__).resolve().parent.parent.joinpath("web/icons.svg").write_text("\n".join(out))
print(len(NAMES), "icônes")
