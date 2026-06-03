"""Shared helpers for display apps: palette, fonts, data sources, drawing.

Apps render full 480x320 PIL frames (landscape) and push them -- or cropped
regions of them -- through the REV A LCD comm layer.
"""
import os
import time
import json
import socket
import collections
import urllib.parse
import urllib.request

import psutil
from serial import SerialException
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


_RATE_STATE = {}


def _delta_rate(key, current, now):
    """Per-second rate from a monotonically increasing counter; None on first call."""
    prev = _RATE_STATE.get(key)
    _RATE_STATE[key] = (current, now)
    if prev is None:
        return None
    dv, dt = current - prev[0], now - prev[1]
    return dv / dt if dt > 0 and dv >= 0 else None


_NET_SKIP = ("lo", "veth", "docker", "br-", "cni", "flannel", "kube", "tailscale")


def _net_counters():
    """Aggregate rx/tx bytes over physical-ish NICs (skips loopback/bridge/CNI)."""
    rx = tx = 0
    for name, io in psutil.net_io_counters(pernic=True).items():
        if any(name.startswith(p) for p in _NET_SKIP):
            continue
        rx += io.bytes_recv
        tx += io.bytes_sent
    return rx, tx


def human_rate(bps):
    if bps is None:
        return "-"
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1000 or unit == "GB/s":
            break
        bps /= 1000.0
    return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"


def _cpu_watts(now):
    """CPU package power from intel RAPL (None if unreadable/AMD without rapl)."""
    try:
        with open("/sys/class/powercap/intel-rapl:0/energy_uj") as f:
            uj = int(f.read())
    except Exception:
        return None
    rate = _delta_rate("rapl_uj", uj, now)
    return rate / 1e6 if rate is not None else None


def _fan_rpm():
    try:
        for entries in psutil.sensors_fans().values():
            if entries:
                return entries[0].current
    except Exception:
        pass
    return None


def _nvme_temp():
    try:
        t = psutil.sensors_temperatures().get("nvme")
        if t:
            return max(s.current for s in t)
    except Exception:
        pass
    return None


def local_values():
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    now = time.monotonic()
    rx, tx = _net_counters()
    net_down = _delta_rate("net_rx", rx, now)
    net_up = _delta_rate("net_tx", tx, now)
    return {
        "net_down": net_down,
        "net_up": net_up,
        "net_down_h": human_rate(net_down),
        "net_up_h": human_rate(net_up),
        "cpu_cores": psutil.cpu_percent(percpu=True),
        "cpu_watts": _cpu_watts(now),
        "fan_rpm": _fan_rpm(),
        "nvme_temp": _nvme_temp(),
        "cpu_pct": psutil.cpu_percent(),
        "ram_pct": vm.percent,
        "ram_used_g": vm.used / 2**30,  # GiB, matching free -h / df -h (#449)
        "ram_total_g": vm.total / 2**30,
        "disk_pct": du.percent,
        "disk_used_g": du.used / 2**30,
        "disk_total_g": du.total / 2**30,
        "temp": host_temp(),
        "load1": os.getloadavg()[0],
        "uptime": fmt_uptime(),
        "time_hm": time.strftime("%H:%M"),
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


def draw_radial(d, w, vals):
    """Arc gauge: fills clockwise from 12 o'clock. With `track: true` draws its
    own ring; without, it traces ring art from a background image."""
    x, y, r = w["x"], w["y"], w.get("r", 40)
    pct = vals.get(w.get("pct")) or 0
    width = w.get("width", 8)
    color = resolve_color(w["color"], vals) if w.get("color") else threshold_color(pct)
    if w.get("track"):
        d.arc([x - r, y - r, x + r, y + r], 0, 360, fill=TRACK, width=width)
    if pct > 0:
        d.arc([x - r, y - r, x + r, y + r], -90, -90 + min(pct, 100) * 3.6,
              fill=color, width=width)
    if w.get("text"):
        d.text((x, y), fmt(w["text"], vals), font=font(w.get("size", 20), "Bold"),
               fill=COLORS["fg"], anchor="mm")
    if w.get("label"):
        d.text((x, y + r + 4), str(w["label"]), font=font(12, "Medium"),
               fill=COLORS["dim"], anchor="ma")


def draw_text(d, w, vals):
    """Free-form text at (x, y) -- for value boxes on background art pages."""
    d.text((w["x"], w["y"]), fmt(w.get("text", ""), vals),
           font=font(w.get("size", 16), w.get("weight", "Medium"),
                     w.get("family", "roboto")),
           fill=resolve_color(w.get("color"), vals), anchor=w.get("anchor", "mm"))


_HISTORY = {}
_SERIES_COLORS = ("accent", "ok", "warn", "bad")


def draw_graph(d, w, vals):
    """Line graph over a value's recent history (sampled while the page shows).
    Single series via `value:`, multiple via `values: [a, b]` (+ `colors:`)."""
    x, y = w["x"], w["y"]
    gw, gh = w.get("w", 200), w.get("h", 60)
    samples = int(w.get("samples", 60))
    series = w.get("values") or ([w["value"]] if w.get("value") else [])
    colors = w.get("colors") or []
    d.rectangle([x, y, x + gw, y + gh], outline=TRACK)
    if w.get("label"):
        d.text((x, y - 18), str(w["label"]), font=font(13, "Medium"),
               fill=COLORS["dim"], anchor="la")
    if w.get("text"):
        d.text((x + gw, y - 18), fmt(w["text"], vals), font=font(13, "Medium"),
               fill=COLORS["fg"], anchor="ra")
    vmin = float(w.get("min", 0))
    for si, name in enumerate(series):
        key = f"{name}@{x},{y}"
        hist = _HISTORY.get(key)
        if hist is None or hist.maxlen != samples:
            hist = _HISTORY[key] = collections.deque(hist or [], maxlen=samples)
        v = vals.get(name)
        if isinstance(v, (int, float)):
            hist.append(float(v))
        if len(hist) < 2:
            continue
        vmax = float(w["max"]) if w.get("max") is not None else (max(hist) or 1.0)
        span = max(vmax - vmin, 1e-9)
        pad = samples - len(hist)  # anchor newest sample to the right edge
        pts = [(x + gw * (pad + i) / (samples - 1),
                y + gh - gh * min(max((val - vmin) / span, 0.0), 1.0))
               for i, val in enumerate(hist)]
        cname = colors[si] if si < len(colors) else _SERIES_COLORS[si % len(_SERIES_COLORS)]
        d.line(pts, fill=COLORS.get(cname, COLORS["accent"]), width=2)


def draw_cores(d, w, vals):
    """One mini vertical bar per CPU core."""
    cores = vals.get("cpu_cores") or []
    x, y, h = w["x"], w["y"], w.get("h", 40)
    bw, gap = w.get("bar_w", 10), w.get("gap", 4)
    if w.get("label"):
        d.text((x, y - 18), str(w["label"]), font=font(13, "Medium"),
               fill=COLORS["dim"], anchor="la")
    for i, p in enumerate(cores):
        cx = x + i * (bw + gap)
        d.rectangle([cx, y, cx + bw, y + h], fill=TRACK)
        fh = int(h * min(p, 100) / 100)
        if fh:
            d.rectangle([cx, y + h - fh, cx + bw, y + h], fill=threshold_color(p))


WIDGETS = {"bar": draw_bar, "metric": draw_metric, "radial": draw_radial,
           "text": draw_text, "graph": draw_graph, "cores": draw_cores}


def _push_bands(lcd, new, old, band):
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


def push_frame(lcd, new, old=None, band=20):
    """Push only the horizontal bands of `new` that differ from `old`.

    Value ticks push a few tiny rectangles; page transitions skip unchanged
    chrome and blank space. With old=None the full frame is pushed.

    On a serial hiccup (devices are known to wedge after hours, upstream #562)
    the port is reopened once and the full frame re-pushed.
    """
    try:
        _push_bands(lcd, new, old, band)
    except (SerialException, OSError):
        try:
            lcd.closeSerial()
        except Exception:
            pass
        time.sleep(2)
        lcd.openSerial()
        _push_bands(lcd, new, None, band)  # panel state unknown -> full redraw


def _parse_hm(s):
    hh, mm = str(s).split(":")
    return int(hh) * 60 + int(mm)


def _in_window(now_min, frm, to):
    return frm <= now_min < to if frm <= to else (now_min >= frm or now_min < to)


class ScreenScheduler:
    """Applies `screen.schedule` windows from display.yaml: dim the backlight
    or switch the panel off during time ranges (night/away mode)."""

    def __init__(self, lcd, screen_cfg):
        self.lcd = lcd
        cfg = screen_cfg or {}
        self.default = int(cfg.get("brightness", 20))
        self.windows = cfg.get("schedule") or []
        self.applied = ("on", self.default)

    def tick(self):
        """Apply the window for the current time; False while the panel is off."""
        t = time.localtime()
        now_min = t.tm_hour * 60 + t.tm_min
        state = ("on", self.default)
        for win in self.windows:
            try:
                if _in_window(now_min, _parse_hm(win.get("from", "00:00")),
                              _parse_hm(win.get("to", "00:00"))):
                    state = ("off", 0) if win.get("off") \
                        else ("on", int(win.get("brightness", self.default)))
                    break
            except Exception:
                continue
        if state != self.applied:
            if state[0] == "off":
                self.lcd.ScreenOff()
            else:
                if self.applied[0] == "off":
                    self.lcd.ScreenOn()
                self.lcd.SetBrightness(state[1])
            self.applied = state
        return state[0] == "on"


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
