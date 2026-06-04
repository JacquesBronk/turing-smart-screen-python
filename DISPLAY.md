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
| `netmap`   | star topology of every host Prometheus scrapes (`up` join `node_uname_info`), green/red status, monospace aesthetic. Unmonitored devices (phones, IoT) show as a footer badge — see below |

Switch by editing `mode:` in `display.yaml` and restarting the service, or
one-off with `DISPLAY_MODE=netmap ./venv/bin/python display.py`.

## Data sources

`display.yaml` declares named sources; pages reference them by name (default
`prom`). Currently implemented: `prometheus` (stdlib-only instant queries).
The interface is two methods — `value(query)` and `series(query)` — so adding
Home Assistant, kubectl, etc. is one small class in `apps/common.py`
(`SOURCE_TYPES`).

## Page schema

See the comment block at the top of `pages.yaml` — widgets (`bar`, `metric`,
`radial`, `text`, `graph`, `cores`), format strings over a value namespace
(local psutil values + per-page query results + derived expressions),
threshold/conditional color specs, optional `background:` art from any stock
theme (the shipped CYBERDECK page traces live radial gauges over the stock
Cyberdeck theme art), and `type: netmap` pages that embed the netmap app in
the rotation.

Local values include network rates with auto-scaled units (`net_down_h`),
per-core CPU (`cpu_cores`), CPU package power via RAPL (`cpu_watts`),
`fan_rpm` and `nvme_temp` where the platform exposes them.

## Netmap clients badge

A star with 20+ labeled spokes is unreadable at 3.5", so only Prometheus-
monitored infra gets spokes; everything else on the LAN collapses into a
footer badge: `+N clients`, plus an amber `NEW n (.x.y)` flag while a
first-seen MAC is younger than `new_window` hours (a who-just-joined-my-
network tripwire). First run seeds the seen-MAC state silently so the whole
network doesn't flag as NEW.

Devices come from the **kernel neighbour table** (`ip neigh`), read passively
by the display process itself — no root, no timer, **zero packets on the
wire**. The trade-off is coverage: it only lists neighbours the host already
learned from normal traffic, so the count is a subset of the LAN that grows
as the node talks to more devices, not a full scan. Deduped by MAC; gateway +
monitored hosts excluded by resolving prom instance names (`.local` fallback;
list anything unresolvable in `netmap.infra_ips`). State persists in
`~/.local/state/turing-display/seen-macs.json`.

> **Do NOT actively sweep the LAN for this.** An earlier version ran a root
> `arp-scan` timer over the whole `/16` (65 k ARP **broadcasts** in ~70 s,
> every 10 min). On a flat L2 that broadcast burst can starve WiFi airtime
> and wedge cheap IoT gear — it was a credible cause of intermittent drops
> and was removed. If you ever need more coverage than the neighbour table
> gives, scan a single `/24` at most, infrequently — never the `/16`.

`screen.schedule` in `display.yaml` dims or switches the panel off during
time windows (night/away mode). Serial hiccups are handled by reopening the
port and re-pushing the frame (panels are known to wedge after long sessions,
upstream #562).

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
  top-down wipe). All apps therefore diff each new frame against the previous
  one (`common.push_frame`) and push only changed bands — value ticks repaint
  tiny rectangles, page transitions skip unchanged chrome/blank space, and a
  full wipe happens only on the very first frame after start.
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
