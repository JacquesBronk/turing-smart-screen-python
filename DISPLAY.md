# Display platform (fork addition)

Additive extension to [turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python):
a small dispatcher that lets one daemon drive the screen in different **modes**,
with **YAML-defined pages** and **pluggable data sources** (Prometheus today).
No upstream files are modified.

```
display.py        entry point: reads display.yaml, runs the selected app
display.yaml      mode + screen + named data sources + per-app config
pages.yaml        carousel page definitions (live-reloaded)
apps/
  common.py       palette, fonts, sources, widgets, LCD init
  carousel.py     rotating stat pages, wipe-free in-page refresh
  netmap.py       live network map (star topology, Prometheus-driven)
systemd/turing-display.service
```

Developed/tested on a TURZX 3.5" (REV A, `USB35INCHIPSV2`) in landscape.

## Modes

| mode | what it does |
|---|---|
| `standard` | execs the stock `main.py` theme engine, untouched |
| `carousel` | rotates pages from `pages.yaml`; layouts, PromQL queries, colors, dwell all YAML; file is re-read every cycle so edits apply live |
| `netmap`   | star topology of every host Prometheus scrapes (`up` join `node_uname_info`), green/red status, monospace aesthetic |

Switch by editing `mode:` in `display.yaml` and restarting the service, or
one-off with `DISPLAY_MODE=netmap ./venv/bin/python display.py`.

## Data sources

`display.yaml` declares named sources; pages reference them by name (default
`prom`). Currently implemented: `prometheus` (stdlib-only instant queries).
The interface is two methods — `value(query)` and `series(query)` — so adding
Home Assistant, kubectl, etc. is one small class in `apps/common.py`
(`SOURCE_TYPES`).

## Page schema

See the comment block at the top of `pages.yaml` — widgets (`bar`, `metric`),
format strings over a value namespace (local psutil values + per-page query
results + derived expressions), threshold/conditional color specs, optional
`background:` art from any stock theme.

## Preview without a screen

```
ONCE=1 ./venv/bin/python display.py                      # carousel pages -> /tmp/carousel_*.png
ONCE=1 DISPLAY_MODE=netmap ./venv/bin/python display.py  # netmap -> /tmp/netmap.png
```

## Hardware gotchas learned the hard way (REV A)

- **`Clear()` resets the panel to portrait** (upstream calls no-arg
  `SetOrientation()` after clearing). Always set orientation *after* `Clear()`.
- **The stock app turns the backlight off on exit** (`clean_stop()` →
  `ScreenOff`). Any replacement must call `ScreenOn()` or you'll push pixels
  to a dark panel.
- **Full-frame pushes take ~1–2 s** over the CDC-ACM serial link (visible
  top-down wipe). The carousel therefore refreshes in-page values by pushing
  only each widget's region; the wipe remains only as the page-transition
  effect.
- `RESET_ON_STARTUP: true` makes the device re-enumerate (port vanishes
  mid-start). Keep it false / don't call `Reset()`.
- Only one process may own the serial port: disable `turing-screen.service`
  (or any other driver) before enabling `turing-display.service`.

## Install (as a service)

```
sudo cp systemd/turing-display.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl disable --now turing-screen.service   # if present
sudo systemctl enable --now turing-display.service
```
