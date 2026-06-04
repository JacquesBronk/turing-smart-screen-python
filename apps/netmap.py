"""Netmap app: live network map of monitored hosts.

Hosts come from Prometheus -- every target matching `up_query` (default: all
node-exporter style jobs), joined with node_uname_info for pretty hostnames.
Rendered as a star topology around the gateway with green/red status dots and
a monospace, terminal-ish aesthetic.

Unmonitored devices (phones, IoT, ...) are read PASSIVELY from the kernel
neighbour table (`ip neigh`) and shown as a footer badge: `+N clients`, plus a
NEW flag while a first-seen MAC is younger than `new_window` hours. This sends
ZERO packets -- it only reports neighbours the host already learned from normal
traffic, so it cannot add load to the network. (An earlier version actively
swept the /16 with arp-scan; that broadcast burst could disrupt WiFi and was
removed -- see DISPLAY.md.)
"""
import json
import os
import socket
import subprocess
import sys
import math
import time
import signal

from PIL import Image, ImageDraw

from apps import common as c

LINE_UP = (40, 90, 70)
LINE_ACTIVE = (60, 140, 105)
LINE_DOWN = (90, 40, 40)

_DEV_SKIP = 'device!~"lo|veth.+|cni.+|flannel.+|docker.+|br.+|vmbr.+|fw.+|tap.+"'
DEFAULT_TRAFFIC_QUERY = (
    f"sum by (instance) (rate(node_network_receive_bytes_total{{{_DEV_SKIP}}}[2m])"
    f" + rate(node_network_transmit_bytes_total{{{_DEV_SKIP}}}[2m]))")

_CACHE = {"t": 0.0, "hosts": []}
_ARP = {"t": 0.0, "clients": None, "new": []}
_RESOLVE = {}  # instance hostname -> ip, cached for the process lifetime


def _infra_ips(src, nm_cfg):
    """IPs of the prom-monitored hosts -- ARP entries matching these are
    spokes already, not anonymous clients. Bare instance hostnames get a
    `.local` fallback; hosts that resolve nowhere (mDNS-only etc.) can be
    listed explicitly in `infra_ips`. Successes cache for the process
    lifetime, failures retry (a DNS blip must not reclassify a host)."""
    ips = set(str(ip) for ip in nm_cfg.get("infra_ips") or [])
    for m, _ in src.series("node_uname_info"):
        host = m.get("instance", "").split(":")[0]
        if host not in _RESOLVE:
            for cand in (host, None if "." in host else host + ".local"):
                try:
                    _RESOLVE[host] = socket.gethostbyname(cand)
                    break
                except (OSError, TypeError):
                    continue
        if _RESOLVE.get(host):
            ips.add(_RESOLVE[host])
    return ips


def _default_route():
    """(iface, gateway_ip) for the default route -- hex little-endian in
    /proc/net/route. Used to scope the neighbour read to the LAN NIC (so K3s
    flannel/cni pod neighbours on 10.42.x don't get counted as LAN clients)."""
    try:
        for line in open("/proc/net/route"):
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                return f[0], socket.inet_ntoa(bytes.fromhex(f[2])[::-1])
    except (OSError, ValueError):
        pass
    return None, None


def _gateway_ip():
    return _default_route()[1]


def _read_neighbours(nm_cfg):
    """{mac: first_ip} from the kernel neighbour table -- passive, sends nothing.
    Only IPv4 entries that resolved to a MAC (skips FAILED/INCOMPLETE)."""
    cmd = nm_cfg.get("neigh_cmd")
    if not cmd:  # scope to the LAN NIC, else flannel/cni pod neighbours leak in
        cmd = ["ip", "-4", "neigh", "show"]
        iface = _default_route()[0]
        if iface:
            cmd += ["dev", iface]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    devs = {}
    for line in out.splitlines():
        f = line.split()
        if not f or f[0].count(".") != 3 or "lladdr" not in f:
            continue  # no hardware address -> FAILED/INCOMPLETE, skip
        mac = f[f.index("lladdr") + 1].lower()
        devs.setdefault(mac, f[0])
    return devs


def gather_clients(src, nm_cfg):
    """Update _ARP with (client count, NEW ips) from the kernel neighbour table.
    Dedupes by MAC (multi-IP hosts collapse to their first address), drops the
    gateway + monitored infra, and persists first-seen times per MAC so a
    newcomer can be flagged NEW for `new_window` hours. clients=None if the
    table can't be read (badge hidden)."""
    now = time.monotonic()
    if _ARP["t"] and now - _ARP["t"] < float(nm_cfg.get("data_refresh", 5)):
        return
    _ARP["t"] = now
    devs = _read_neighbours(nm_cfg)
    if devs is None:
        _ARP.update(clients=None, new=[])
        return
    skip = _infra_ips(src, nm_cfg) | {_gateway_ip()}
    clients = {mac: ip for mac, ip in devs.items() if ip not in skip}

    state_path = os.path.expanduser(nm_cfg.get(
        "state_file", "~/.local/state/turing-display/seen-macs.json"))
    try:
        seen = json.load(open(state_path))
    except (OSError, ValueError):
        seen = {mac: 0 for mac in devs}  # first run: seed quietly, nothing NEW
    wall = time.time()
    missing = [mac for mac in devs if mac not in seen]
    for mac in missing:
        seen[mac] = wall
    if missing or not os.path.exists(state_path):
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w") as fh:
            json.dump(seen, fh)
    window = float(nm_cfg.get("new_window", 24)) * 3600
    new = sorted(ip for mac, ip in clients.items()
                 if wall - seen.get(mac, 0) < window)
    _ARP.update(clients=len(clients), new=new)


def gather_hosts(src, nm_cfg):
    """[(name, up, traffic_bps)] -- cached so fast animation frames don't hammer
    Prometheus (data refreshes every `data_refresh` seconds, default 5)."""
    now = time.monotonic()
    if _CACHE["hosts"] and now - _CACHE["t"] < float(nm_cfg.get("data_refresh", 5)):
        return _CACHE["hosts"]
    up_query = nm_cfg.get("up_query", 'up{job=~".*node.*"}')
    names = {m.get("instance"): m.get("nodename")
             for m, _ in src.series("node_uname_info")}
    traffic = {}
    for m, v in src.series(nm_cfg.get("traffic_query", DEFAULT_TRAFFIC_QUERY)):
        name = names.get(m.get("instance")) or m.get("instance", "?").split(":")[0]
        traffic[name] = traffic.get(name, 0.0) + v
    hosts = {}
    for m, v in src.series(up_query):
        inst = m.get("instance", "?")
        name = names.get(inst) or inst.split(":")[0].removesuffix(".local")
        hosts[name] = max(hosts.get(name, 0.0), v)  # any up target counts as up
    result = sorted((n, u, traffic.get(n, 0.0)) for n, u in hosts.items())
    _CACHE.update(t=now, hosts=result)
    gather_clients(src, nm_cfg)  # warm the ARP badge alongside (render reads _ARP)
    return result


def render(hosts, nm_cfg, idx=None, npages=None):
    img = Image.new("RGB", (c.W, c.H), c.BG)
    d = ImageDraw.Draw(img)
    n_up = sum(1 for _, v, _ in hosts if v >= 1)
    c.header(d, nm_cfg.get("title", "NETWORK MAP"), idx, npages)
    count_x = c.W - 16 - (18 * npages + 16 if npages else 0)  # clear the page dots
    d.text((count_x, 22), f"{n_up}/{len(hosts)} up",
           font=c.font(16, "B", "mono"),
           fill=c.COLORS["ok"] if n_up == len(hosts) else c.COLORS["bad"], anchor="rm")

    cx, cy, rx, ry = c.W // 2, 176, 168, 92
    traffic_min = float(nm_cfg.get("traffic_min", 5000))  # B/s to count as active
    phase = (time.monotonic() % 2.0) / 2.0  # shared animation clock, 2s cycle
    # spokes + nodes
    for k, (name, up, traffic) in enumerate(hosts):
        a = 2 * math.pi * k / max(1, len(hosts)) - math.pi / 2
        px = cx + int(rx * math.cos(a))
        py = cy + int(ry * math.sin(a))
        ok = up >= 1
        active = ok and traffic >= traffic_min
        d.line([cx, cy, px, py],
               fill=(LINE_ACTIVE if active else LINE_UP) if ok else LINE_DOWN, width=2)
        if active:  # packets flowing outward along the spoke
            for j in range(2):
                f = 0.22 + 0.62 * ((phase + j / 2) % 1.0)
                dx, dy = cx + (px - cx) * f, cy + (py - cy) * f
                d.ellipse([dx - 3, dy - 3, dx + 3, dy + 3], fill=c.COLORS["accent"])
        col = c.COLORS["ok"] if ok else c.COLORS["bad"]
        d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=col)
        if not ok:  # ring downed hosts so they pop
            d.ellipse([px - 10, py - 10, px + 10, py + 10], outline=col, width=2)
        f = c.font(13, "B", "mono")
        ly = py - 22 if py < cy else py + 11
        lx = min(max(px, 40), c.W - 40)
        d.text((lx, ly), name, font=f,
               fill=c.COLORS["fg"] if ok else c.COLORS["bad"], anchor="ma")
        if active:  # current rate under the hostname
            ry_off = ly - 14 if py < cy else ly + 16
            d.text((lx, ry_off), c.human_rate(traffic), font=c.font(10),
                   fill=c.COLORS["dim"], anchor="ma")
    # gateway hub on top of the spokes
    label = str(nm_cfg.get("center_label", "LAN"))
    d.ellipse([cx - 26, cy - 26, cx + 26, cy + 26], fill=c.PANEL,
              outline=c.COLORS["accent"], width=2)
    d.text((cx, cy), label, font=c.font(14, "B", "mono"),
           fill=c.COLORS["accent"], anchor="mm")
    c.footer(d)
    if _ARP["clients"] is not None:  # ARP sweep badge, footer center
        new = _ARP["new"]
        txt = f"+{_ARP['clients']} clients"
        if new:  # e.g. "+8 clients · NEW 1 (.76.112)"
            short = "." + ".".join(new[0].split(".")[2:])
            txt += f" · NEW {len(new)} ({short})"
        d.text((c.W // 2, 311), txt, font=c.font(13, "B", "mono"),
               fill=c.COLORS["warn"] if new else c.COLORS["dim"], anchor="mm")
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
