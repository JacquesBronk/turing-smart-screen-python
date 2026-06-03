"""Shared helpers for display apps: palette, fonts, data sources, drawing.

Apps render full 480x320 PIL frames (landscape) and push them -- or cropped
regions of them -- through the REV A LCD comm layer.
"""
import os
import time
import json
import socket
import urllib.parse
import urllib.request

import psutil
from PIL import Image, ImageChops, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
W, H = 480, 320

# ---------- palette ----------
BG = (8, 12, 18)
PANEL = (14, 20, 28)
TRACK = (30, 38, 48)
COLORS = {
    "fg": (224, 232, 240),
    "dim": (122, 142, 160),
    "accent": (0, 200, 255),
    "ok": (80, 220, 120),
    "warn": (240, 184, 64),
    "bad": (240, 84, 84),
}

# ---------- fonts ----------
_FONT_FILES = {
    "roboto": os.path.join(BASE, "res", "fonts", "roboto", "Roboto-{w}.ttf"),
    "mono": os.path.join(BASE, "res", "fonts", "generale-mono", "GeneraleMono{w}.ttf"),
}
_FCACHE = {}


def font(size, weight="Regular", family="roboto"):
    key = (family, size, weight)
    if key not in _FCACHE:
        _FCACHE[key] = ImageFont.truetype(_FONT_FILES[family].format(w=weight), size)
    return _FCACHE[key]


# ---------- data sources ----------
class PrometheusSource:
    """Instant-query client for the Prometheus HTTP API. No deps, stdlib only."""

    def __init__(self, url, timeout=4):
        self.url = url
        self.timeout = timeout

    def _query(self, query):
        u = self.url + "?" + urllib.parse.urlencode({"query": query})
        with urllib.request.urlopen(u, timeout=self.timeout) as r:
            return json.load(r)["data"]["result"]

    def value(self, query, retries=1):
        """First series value as float, or None. One retry for transient blips."""
        for _ in range(retries + 1):
            try:
                res = self._query(query)
                return float(res[0]["value"][1]) if res else None
            except Exception:
                continue
        return None

    def series(self, query, retries=1):
        """List of (labels-dict, float-value) tuples; [] on error."""
        for _ in range(retries + 1):
            try:
                return [(s["metric"], float(s["value"][1])) for s in self._query(query)]
            except Exception:
                continue
        return []


SOURCE_TYPES = {"prometheus": lambda s: PrometheusSource(s["url"], s.get("timeout", 4))}


def build_sources(display_cfg):
    """Named source instances from display.yaml `sources:`. `prom` always exists."""
    out = {}
    for name, spec in (display_cfg.get("sources") or {}).items():
        factory = SOURCE_TYPES.get(spec.get("type"))
        if factory:
            out[name] = factory(spec)
    if "prom" not in out:
        out["prom"] = PrometheusSource(
            os.environ.get("PROM", "http://prometheus.example/api/v1/query"))
    return out


# ---------- local host values ----------
def host_temp():
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "acpitz"):
            if temps.get(key):
                return max(s.current for s in temps[key])
    except Exception:
        pass
    return None


def fmt_uptime():
    s = int(time.time() - psutil.boot_time())
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    return f"{d}d {h}h" if d else f"{h}h {s // 60}m"


def local_values():
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    return {
        "cpu_pct": psutil.cpu_percent(),
        "ram_pct": vm.percent,
        "ram_used_g": vm.used / 1e9,
        "ram_total_g": vm.total / 1e9,
        "disk_pct": du.percent,
        "disk_used_g": du.used / 1e9,
        "disk_total_g": du.total / 1e9,
        "temp": host_temp(),
        "load1": os.getloadavg()[0],
        "uptime": fmt_uptime(),
    }


# ---------- color / format ----------
def threshold_color(v, warn=70.0, bad=90.0):
    v = v or 0
    return COLORS["ok"] if v < warn else (COLORS["warn"] if v < bad else COLORS["bad"])


def resolve_color(spec, vals):
    """fg|dim|accent|ok|warn|bad, auto:<name>[:warn:bad], ok_if:<expr>[:<else>]"""
    if not spec:
        return COLORS["fg"]
    if spec in COLORS:
        return COLORS[spec]
    parts = spec.split(":")
    if parts[0] == "auto" and len(parts) >= 2:
        warn = float(parts[2]) if len(parts) > 2 else 70.0
        bad = float(parts[3]) if len(parts) > 3 else 90.0
        return threshold_color(vals.get(parts[1]), warn, bad)
    if parts[0] == "ok_if" and len(parts) >= 2:
        else_color = COLORS.get(parts[2], COLORS["bad"]) if len(parts) > 2 else COLORS["bad"]
        try:
            ok = bool(eval(parts[1], {"__builtins__": {}}, dict(vals)))
        except Exception:
            return COLORS["dim"]
        return COLORS["ok"] if ok else else_color
    return COLORS["fg"]


def fmt(template, vals):
    try:
        return template.format(**vals)
    except Exception:
        return "-"


# ---------- chrome ----------
def header(d, title, idx=None, npages=None):
    d.rectangle([0, 0, W, 44], fill=PANEL)
    d.text((16, 22), str(title), font=font(24, "Medium"), fill=COLORS["accent"], anchor="lm")
    if idx is not None and npages:
        cx = W - 18
        for k in range(npages - 1, -1, -1):
            col = COLORS["accent"] if k == idx else (60, 72, 86)
            d.ellipse([cx - 5, 17, cx + 5, 27], fill=col)
            cx -= 18


def footer(d):
    d.line([0, 300, W, 300], fill=TRACK)
    d.text((16, 311), socket.gethostname(), font=font(13), fill=COLORS["dim"], anchor="lm")
    d.text((W - 16, 311), time.strftime("%H:%M:%S"), font=font(13), fill=COLORS["dim"], anchor="rm")


# ---------- widgets ----------
def draw_bar(d, w, vals):
    x, y, bw = w["x"], w["y"], w.get("w", 250)
    pct = vals.get(w.get("pct")) or 0
    d.text((x, y), str(w.get("label", "")), font=font(16, "Medium"), fill=COLORS["fg"], anchor="la")
    d.text((x + bw, y), fmt(w.get("text", ""), vals), font=font(16, "Medium"), fill=COLORS["fg"], anchor="ra")
    by = y + 22
    d.rounded_rectangle([x, by, x + bw, by + 16], radius=8, fill=TRACK)
    fw = max(0, min(bw, int(bw * pct / 100)))
    if fw >= 16:
        d.rounded_rectangle([x, by, x + fw, by + 16], radius=8, fill=threshold_color(pct))
    elif fw > 0:
        d.rectangle([x, by, x + fw, by + 16], fill=threshold_color(pct))


def draw_metric(d, w, vals):
    x, y = w["x"], w["y"]
    d.text((x, y), str(w.get("label", "")), font=font(14, "Medium"), fill=COLORS["dim"], anchor="la")
    d.text((x, y + 22), fmt(w.get("text", ""), vals), font=font(38, "Bold"),
           fill=resolve_color(w.get("color"), vals), anchor="la")


WIDGETS = {"bar": draw_bar, "metric": draw_metric}


def push_frame(lcd, new, old=None, band=20):
    """Push only the horizontal bands of `new` that differ from `old`.

    This is what keeps the screen wipe-free: value ticks push a few tiny
    rectangles, and even page transitions skip unchanged chrome and blank
    space. With old=None the full frame is pushed (first frame only).
    """
    if old is None:
        lcd.DisplayPILImage(new, 0, 0)
        return
    diff = ImageChops.difference(old, new)
    if not diff.getbbox():
        return
    for y0 in range(0, H, band):
        b = diff.crop((0, y0, W, min(H, y0 + band))).getbbox()
        if b:
            x0, by0, x1, by1 = b
            lcd.DisplayPILImage(new.crop((x0, y0 + by0, x1, y0 + by1)), x0, y0 + by0)


# ---------- lcd ----------
def init_lcd(orientation="landscape", brightness=20):
    from library.lcd.lcd_comm_rev_a import LcdCommRevA, Orientation

    lcd = LcdCommRevA(com_port="AUTO", display_width=320, display_height=480)
    lcd.InitializeComm()
    lcd.ScreenOn()  # stock clean_stop() leaves the backlight off -- always turn it on
    lcd.SetBrightness(int(brightness))
    lcd.Clear()  # upstream Clear() resets the panel to portrait...
    lcd.SetOrientation(  # ...so orientation MUST be set after it
        Orientation.REVERSE_LANDSCAPE if orientation == "reverse_landscape"
        else Orientation.LANDSCAPE)
    return lcd
