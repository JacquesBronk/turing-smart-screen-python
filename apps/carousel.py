"""Carousel app: rotate YAML-defined stat pages with wipe-free in-page refresh.

Pages live in pages.yaml (see DISPLAY.md for the schema) and are re-read every
cycle, so edits apply live. A full-frame push (visible top-down wipe, ~1-2s of
serial bandwidth) happens only on page transitions; between them, each widget
region is refreshed individually so values tick in place.
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
    for name, expr in (page.get("derived") or {}).items():
        try:
            vals[name] = eval(expr, {"__builtins__": {}}, dict(vals))
        except Exception:
            vals[name] = None
    return vals


def render_page(page, idx, npages, sources):
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


def page_boxes(page):
    boxes = [c.widget_bbox(w) for w in page.get("widgets") or []]
    if page.get("footer", True):
        boxes.append(c.FOOTER_CLOCK_BOX)
    return boxes


def run(lcd, display_cfg):
    pages_file = os.path.join(
        c.BASE, (display_cfg.get("carousel") or {}).get("pages_file", "pages.yaml"))
    sources = c.build_sources(display_cfg)
    cfg = load_pages_cfg(pages_file)

    if os.environ.get("ONCE") == "1":
        pages = cfg.get("pages") or []
        for k, page in enumerate(pages):
            p = f"/tmp/carousel_{k}.png"
            render_page(page, k, len(pages), sources).save(p)
            print("wrote", p)
        return

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    import psutil
    psutil.cpu_percent()  # prime the first reading

    idx = 0
    while True:
        try:
            cfg = load_pages_cfg(pages_file)  # live reload; keep last good on error
        except Exception:
            pass
        pages = cfg.get("pages") or []
        if not pages:
            time.sleep(2)
            continue
        idx %= len(pages)
        page = pages[idx]
        dwell = float(cfg.get("dwell", 8))
        refresh = float(cfg.get("refresh", 2))

        lcd.DisplayPILImage(render_page(page, idx, len(pages), sources), 0, 0)
        t0 = time.monotonic()
        while True:
            remaining = dwell - (time.monotonic() - t0)
            if remaining <= 0.05:
                break
            time.sleep(min(refresh, remaining))
            if dwell - (time.monotonic() - t0) <= 0.05:
                break
            # wipe-free refresh: redraw in memory, push only widget regions
            frame = render_page(page, idx, len(pages), sources)
            for box in page_boxes(page):
                lcd.DisplayPILImage(frame.crop(box), box[0], box[1])
        idx += 1
