#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# mirror_demo.py — a live smoke test of revline's read/write mirror
# ═══════════════════════════════════════════════════════════════
# Drives the REAL revline.py mirror functions through a scripted session so you can
# watch reading (input, warm, left) and writing (output, cool, right) move over the
# five timescales. Nothing here re-implements the chart — it imports revline and
# calls mirror_tach / mirror_hist / ema_rate exactly as _render does.
#
#   python3 mirror_demo.py            # animate live in this terminal (Ctrl-C to stop)
#   python3 mirror_demo.py --capture  # emit the frames as HTML (for the artifact)
# ═══════════════════════════════════════════════════════════════
import importlib.util
import os
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location(
    "revline", os.path.join(_HERE, "revline.py")
)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


# ── build a realistic (timestamp, in_raw, out_raw) event stream ──────────────
# in_raw = context size that turn (a FLOW, re-read whole each turn); out_raw = the
# per-response output counter that grows mid-stream and resets each turn. We feed
# these through revline's OWN accumulator so the cumulative history is faithful.
def build_events(now):
    ev = []  # (t, in_raw, out_raw)
    # A day of sparse prior turns so 4h / 1d rows have shape (context creeps up).
    ctx = 40000
    t = now - 86400
    while t < now - 900:
        ctx += 5000
        out = 200 + (int(ctx) % 2600)  # varied output per turn
        ev.append((t, ctx, 3))  # turn start: small output
        ev.append((t + 4, ctx, out))  # turn end: streamed output
        t += 1900 + (int(ctx) % 700)  # uneven spacing → varied buckets
    # The last ~10 min: denser turns, the active part you watch.
    t = now - 560
    while t < now - 8:
        ctx += 4000 + (int(t) % 5000)
        peak = 400 + (int(t) % 3200)
        # stream the output over ~5s in a few growing samples (what really happens)
        ev.append((t, ctx, 2))
        ev.append((t + 2, ctx, peak // 3))
        ev.append((t + 4, ctx, peak))
        t += 22 + (int(ctx) % 20)
    # A live streaming turn right at "now" so the 1s needle is moving.
    ctx += 6000
    ev.append((now - 6, ctx, 5))
    ev.append((now - 4, ctx, 900))
    ev.append((now - 2, ctx, 2100))
    ev.append((now - 1, ctx, 3050))
    return ev


# ── run the events through revline's corrected accumulator → store rows ──────
def accumulate(events):
    rows = []
    in_cum = out_cum = 0
    prev_in = prev_out = None
    for t, in_raw, out_raw in events:
        in_raw = max(0, int(in_raw))
        out_raw = max(0, int(out_raw))
        changed = prev_in is None or in_raw != prev_in or out_raw != prev_out
        if changed:
            if prev_in is None:
                in_cum += in_raw
                out_cum += out_raw
            else:
                if in_raw != prev_in:
                    in_cum += in_raw
                out_cum += out_raw - prev_out if out_raw >= prev_out else out_raw
            rows.append((int(t), in_cum, out_cum, in_raw, out_raw))
        prev_in, prev_out = in_raw, out_raw
    return rows


# ── assemble the Gauges block exactly as revline._render does ────────────────
IND = "⠀" * 2
LABEL, R, DIM = rl.LABEL, rl.R, rl.DIM
_RN = 6


def glabel(t):
    return f"{LABEL}{t.rjust(4)}:{R} "


def grow(label, chart, rnum, wnum):
    return f"{IND}{glabel(label)}{DIM}{rnum:>{_RN}}{R} {rl._EDL}{chart}{rl._EDR} {DIM}{wnum}{R}"


def gauges(all_rows, now):
    # fine store = last hour of change-points; day store = the rest (coarse)
    rows = [r for r in all_rows if r[0] >= now - 3660 and r[0] <= now]
    day_rows = [r for r in all_rows if r[0] <= now]
    _cw = 2 * rl.HALF + 1
    header = (
        f"{rl.hdr('Gauges:', rl.EFF)} \033[38;5;208mreading{R}"
        + " " * (_RN + 1 + 1 + _cw + 1 + 1 - len("reading"))
        + f"\033[38;5;45mwriting{R}"
    )
    scales = rl._full_scales()
    tps_r, tps_w = scales[1]  # per-second full-scale for the live needle
    read_r = rl.ema_rate(rows, now, rl.IN, rl._TACH_HALFLIFE)
    write_r = rl.ema_rate(rows, now, rl.OUT, rl._TACH_HALFLIFE)
    lines = [
        header,
        grow(
            "tps",
            rl.mirror_tach(read_r, write_r, tps_r, tps_w),
            rl.ktok(round(read_r)),
            rl.ktok(round(write_r)),
        ),
    ]
    for lab, span in (("1m", 60), ("10m", 600), ("4h", 14400), ("1d", 86400)):
        src = day_rows if span > 3600 else rows
        i = max(0, rl._cum_at(src, now, rl.IN) - rl._cum_at(src, now - span, rl.IN))
        o = max(0, rl._cum_at(src, now, rl.OUT) - rl._cum_at(src, now - span, rl.OUT))
        fr, fw = scales[span]
        lines.append(
            grow(
                lab,
                rl.mirror_hist(src, now, span, fr, fw),
                rl.ktok(i),
                rl.ktok(o),
            )
        )
    return "\n".join(lines)


# ── live terminal animation ──────────────────────────────────────────────────
def animate():
    base = int(time.time())
    events = build_events(base)
    end = base
    start = base - 300  # replay the last 5 minutes of the session
    print("\033[?25l", end="")  # hide cursor
    try:
        span = end - start
        frames = 120
        for f in range(frames + 1):
            now = start + int(span * f / frames)
            block = gauges([e for e in accumulate(events)], now)
            sys.stdout.write("\033[H\033[2J")  # home + clear
            sys.stdout.write("revline — read/write mirror (live smoke test)\n\n")
            sys.stdout.write(block + "\n")
            sys.stdout.flush()
            time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        print("\033[?25h", end="")  # show cursor


# ── capture frames as HTML (for the artifact) ────────────────────────────────
_BASE16 = [
    (0, 0, 0),
    (205, 49, 49),
    (13, 188, 121),
    (229, 229, 16),
    (36, 114, 200),
    (188, 63, 188),
    (17, 168, 205),
    (229, 229, 229),
    (102, 102, 102),
    (241, 76, 76),
    (35, 209, 139),
    (245, 245, 67),
    (59, 142, 234),
    (214, 112, 214),
    (41, 184, 219),
    (255, 255, 255),
]
_CUBE = [0, 95, 135, 175, 215, 255]


def _xterm_rgb(n):
    if n < 16:
        return _BASE16[n]
    if n < 232:
        n -= 16
        return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def ansi_to_html(text):
    import re

    text = re.sub(r"\033\]8;;[^\033]*\033\\", "", text)  # strip OSC-8 wrappers
    out = []
    color = None
    bold = False
    dim = False
    i = 0
    open_span = False

    def close():
        nonlocal open_span
        if open_span:
            out.append("</span>")
            open_span = False

    def opn():
        nonlocal open_span
        css = []
        if color:
            css.append(f"color:rgb({color[0]},{color[1]},{color[2]})")
        if bold:
            css.append("font-weight:700")
        if dim:
            css.append("opacity:.55")
        out.append(f'<span style="{";".join(css)}">' if css else "<span>")
        open_span = True

    tok = re.compile(r"\033\[([0-9;]*)m")
    while i < len(text):
        m = tok.match(text, i)
        if m:
            close()
            codes = [int(x) for x in m.group(1).split(";") if x != ""] or [0]
            j = 0
            while j < len(codes):
                c = codes[j]
                if c == 0:
                    color, bold, dim = None, False, False
                elif c == 1:
                    bold = True
                elif c == 2:
                    dim = True
                elif c == 38 and j + 1 < len(codes) and codes[j + 1] == 2:
                    color = (codes[j + 2], codes[j + 3], codes[j + 4])
                    j += 4
                elif c == 38 and j + 1 < len(codes) and codes[j + 1] == 5:
                    color = _xterm_rgb(codes[j + 2])
                    j += 2
                elif 30 <= c <= 37:
                    color = _BASE16[c - 30]
                elif 90 <= c <= 97:
                    color = _BASE16[c - 90 + 8]
                j += 1
            opn()
            i = m.end()
        else:
            ch = text[i]
            if not open_span:
                opn()
            out.append(
                {"&": "&amp;", "<": "&lt;", ">": "&gt;", " ": "&nbsp;"}.get(ch, ch)
            )
            i += 1
    close()
    return "".join(out)


def capture():
    import json

    base = int(time.time())
    events = build_events(base)
    rows = accumulate(events)
    start, end = base - 300, base
    frames = []
    N = 90
    for f in range(N + 1):
        now = start + int((end - start) * f / N)
        block = gauges(rows, now)
        frames.append("<br>".join(ansi_to_html(ln) for ln in block.split("\n")))
    print(json.dumps(frames))


if __name__ == "__main__":
    if "--capture" in sys.argv:
        capture()
    else:
        animate()
