"""Netmap app: live network map of monitored hosts.

Hosts come from Prometheus -- every target matching `up_query` (default: all
node-exporter style jobs), joined with node_uname_info for pretty hostnames.
Rendered as a star topology around the gateway with green/red status dots and
a monospace, terminal-ish aesthetic.
"""
import os
import sys
import math
import time
import signal

from PIL import Image, ImageDraw

from apps import common as c

LINE_UP = (40, 90, 70)
LINE_DOWN = (90, 40, 40)


def gather_hosts(src, nm_cfg):
    up_query = nm_cfg.get("up_query", 'up{job=~".*node.*"}')
    names = {m.get("instance"): m.get("nodename")
             for m, _ in src.series("node_uname_info")}
    hosts = {}
    for m, v in src.series(up_query):
        inst = m.get("instance", "?")
        name = names.get(inst) or inst.split(":")[0].removesuffix(".local")
        hosts[name] = max(hosts.get(name, 0.0), v)  # any up target counts as up
    return sorted(hosts.items())


def render(hosts, nm_cfg, idx=None, npages=None):
    img = Image.new("RGB", (c.W, c.H), c.BG)
    d = ImageDraw.Draw(img)
    n_up = sum(1 for _, v in hosts if v >= 1)
    c.header(d, nm_cfg.get("title", "NETWORK MAP"), idx, npages)
    count_x = c.W - 16 - (18 * npages + 16 if npages else 0)  # clear the page dots
    d.text((count_x, 22), f"{n_up}/{len(hosts)} up",
           font=c.font(16, "B", "mono"),
           fill=c.COLORS["ok"] if n_up == len(hosts) else c.COLORS["bad"], anchor="rm")

    cx, cy, rx, ry = c.W // 2, 176, 168, 92
    # spokes + nodes
    for k, (name, up) in enumerate(hosts):
        a = 2 * math.pi * k / max(1, len(hosts)) - math.pi / 2
        px = cx + int(rx * math.cos(a))
        py = cy + int(ry * math.sin(a))
        ok = up >= 1
        d.line([cx, cy, px, py], fill=LINE_UP if ok else LINE_DOWN, width=2)
        col = c.COLORS["ok"] if ok else c.COLORS["bad"]
        d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=col)
        if not ok:  # ring downed hosts so they pop
            d.ellipse([px - 10, py - 10, px + 10, py + 10], outline=col, width=2)
        f = c.font(13, "B", "mono")
        ly = py - 22 if py < cy else py + 11
        lx = min(max(px, 40), c.W - 40)
        d.text((lx, ly), name, font=f,
               fill=c.COLORS["fg"] if ok else c.COLORS["bad"], anchor="ma")
    # gateway hub on top of the spokes
    label = str(nm_cfg.get("center_label", "LAN"))
    d.ellipse([cx - 26, cy - 26, cx + 26, cy + 26], fill=c.PANEL,
              outline=c.COLORS["accent"], width=2)
    d.text((cx, cy), label, font=c.font(14, "B", "mono"),
           fill=c.COLORS["accent"], anchor="mm")
    c.footer(d)
    return img


def run(lcd, display_cfg):
    nm_cfg = display_cfg.get("netmap") or {}
    sources = c.build_sources(display_cfg)
    src = sources.get(nm_cfg.get("source", "prom"))

    if os.environ.get("ONCE") == "1":
        render(gather_hosts(src, nm_cfg), nm_cfg).save("/tmp/netmap.png")
        print("wrote /tmp/netmap.png")
        return

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    sched = c.ScreenScheduler(lcd, display_cfg.get("screen"))
    prev = None
    while True:
        if not sched.tick():
            time.sleep(30)
            continue
        frame = render(gather_hosts(src, nm_cfg), nm_cfg)
        c.push_frame(lcd, frame, prev)
        prev = frame
        time.sleep(float(nm_cfg.get("refresh", 30)))
