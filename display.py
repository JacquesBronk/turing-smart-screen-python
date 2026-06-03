#!/usr/bin/env python3
"""Display dispatcher: one daemon owns the screen, display.yaml picks the app.

Modes:
  standard  -- the stock theme engine (execs main.py, untouched upstream)
  carousel  -- rotating YAML stat pages (apps/carousel.py + pages.yaml)
  netmap    -- live network map from Prometheus (apps/netmap.py)

Env:
  DISPLAY_MODE  override the mode in display.yaml
  REV=1         force reverse_landscape
  ONCE=1        render to /tmp/*.png and exit (no screen needed)

Changing the mode in display.yaml takes effect on service restart:
  sudo systemctl restart turing-display.service
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import yaml


def main():
    with open(os.path.join(BASE, "display.yaml")) as f:
        cfg = yaml.safe_load(f) or {}
    mode = os.environ.get("DISPLAY_MODE") or cfg.get("mode", "carousel")

    if mode == "standard":
        os.execv(sys.executable, [sys.executable, os.path.join(BASE, "main.py")])

    from apps import carousel, netmap
    apps = {"carousel": carousel, "netmap": netmap}
    if mode not in apps:
        sys.exit(f"unknown mode: {mode!r} (expected one of: standard, {', '.join(apps)})")

    if os.environ.get("ONCE") == "1":
        apps[mode].run(None, cfg)
        return

    from apps.common import init_lcd
    scr = cfg.get("screen") or {}
    orientation = "reverse_landscape" if os.environ.get("REV") == "1" \
        else scr.get("orientation", "landscape")
    lcd = init_lcd(orientation, scr.get("brightness", 20))
    apps[mode].run(lcd, cfg)


if __name__ == "__main__":
    main()
