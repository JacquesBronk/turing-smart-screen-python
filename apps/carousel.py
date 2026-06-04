"""Carousel app: rotate YAML-defined stat pages with wipe-free in-page refresh.

Pages live in pages.yaml (see DISPLAY.md for the schema) and are re-read every
cycle, so edits apply live. Every push is a diff against the previous frame
(common.push_frame), so value ticks repaint a few small rectangles and page
transitions repaint only the regions that actually differ -- never a full
top-down wipe after the first frame.
"""
import os
import sys
import time
import signal

import yaml
from PIL import Image, ImageDraw

from apps import common as c


def load_pages_cfg(path):
    with open(path) as f:
        return yaml.safe_load(f)


def page_values(page, sources):
    vals = c.local_values()
    for name, q in (page.get("queries") or {}).items():
        if isinstance(q, dict):
            src = sources.get(q.get("source", "prom"))
            query = q.get("query", "")
        else:
            src, query = sources.get("prom"), q
        vals[name] = src.value(query) if src else None
    env = {"__builtins__": {}, "human_rate": c.human_rate,
           "min": min, "max": max, "round": round, "int": int,
           "str": str, "abs": abs}
    for name, expr in (page.get("derived") or {}).items():
        try:
            vals[name] = eval(expr, env, dict(vals))
        except Exception:
            vals[name] = None
    return vals


def render_page(page, idx, npages, sources, display_cfg=None):
    if page.get("type") == "netmap":  # embed the netmap app as a page
        from apps import netmap
        nm = dict((display_cfg or {}).get("netmap") or {})
        nm.update({k: v for k, v in page.items() if k != "type"})
        src = sources.get(nm.get("source", "prom"))
        return netmap.render(netmap.gather_hosts(src, nm), nm, idx, npages)
    img = Image.new("RGB", (c.W, c.H), c.BG)
    bg = page.get("background")
    if bg:
        try:
            art = Image.open(os.path.join(c.BASE, bg)).convert("RGB")
            if art.size != (c.W, c.H):
                art = art.resize((c.W, c.H))
            img.paste(art)
        except Exception:
            pass
    d = ImageDraw.Draw(img)
    try:
        vals = page_values(page, sources)
        if page.get("header", True):
            c.header(d, page.get("title", ""), idx, npages)
        for w in page.get("widgets") or []:
            try:
                c.WIDGETS.get(w.get("type"), lambda *a: None)(d, w, vals)
            except Exception:
                pass
        if page.get("footer", True):
            c.footer(d)
    except Exception as e:
        d.text((16, 150), f"render error:\n{e}", font=c.font(16), fill=c.COLORS["bad"])
    return img


def run(lcd, display_cfg):
    pages_file = os.path.join(
        c.BASE, (display_cfg.get("carousel") or {}).get("pages_file", "pages.yaml"))
    if not os.path.exists(pages_file):
        pages_file = os.path.join(c.BASE, "pages.example.yaml")  # generic fallback
    sources = c.build_sources(display_cfg)
    cfg = load_pages_cfg(pages_file)

    def active_pages(cfg):
        return [p for p in (cfg.get("pages") or []) if not p.get("disabled")]

    if os.environ.get("ONCE") == "1":
        pages = active_pages(cfg)
        for k, page in enumerate(pages):
            p = f"/tmp/carousel_{k}.png"
            render_page(page, k, len(pages), sources, display_cfg).save(p)
            print("wrote", p)
        return

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    import psutil
    psutil.cpu_percent()  # prime the first reading

    sched = c.ScreenScheduler(lcd, display_cfg.get("screen"))
    idx = 0
    prev = None  # last frame on the panel; every push is a diff against it
    while True:
        if not sched.tick():  # night/away window: panel off, check back shortly
            time.sleep(30)
            continue
        try:
            cfg = load_pages_cfg(pages_file)  # live reload; keep last good on error
        except Exception:
            pass
        pages = active_pages(cfg)
        if not pages:
            time.sleep(2)
            continue
        idx %= len(pages)
        page = pages[idx]
        dwell = float(page.get("dwell") or cfg.get("dwell", 8))
        refresh = float(page.get("refresh") or cfg.get("refresh", 2))

        frame = render_page(page, idx, len(pages), sources, display_cfg)
        c.push_frame(lcd, frame, prev)
        prev = frame
        t0 = time.monotonic()
        while True:
            remaining = dwell - (time.monotonic() - t0)
            if remaining <= 0.05:
                break
            time.sleep(min(refresh, remaining))
            if dwell - (time.monotonic() - t0) <= 0.05:
                break
            frame = render_page(page, idx, len(pages), sources, display_cfg)
            c.push_frame(lcd, frame, prev)
            prev = frame
        idx += 1
