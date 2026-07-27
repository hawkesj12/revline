#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# revline.py — a rich, multi-section Claude Code status line
# ═══════════════════════════════════════════════════════════════
# A color-coded dashboard rendered in the Claude Code footer. Four sections,
# color-grouped by which system each line belongs to:
#   Claude Code:  model · thinking · effort · session · cost · context · cache/burn/api
#   Git:          repo (clickable) · branch · ↑↓ upstream · PR · staged/mod/new · last-commit age
#   Gauges:       the read/write "mirror" — a butterfly of tokens READ (input) vs
#                 WRITTEN (output): a live tps needle + a timeline at 1m/10m/4h/1d
#   Usage:        5h / 7d rate-limit budgets as bars (the Max-plan runway)
#
# The mirror, per row:  <label>  <read#> ▏<chart>▕ <write#>
#   • Reading (input, incl. cache) is WARM and fills the left half from the center
#     outward; writing (output) is COOL and fills the right half. A dim │ marks
#     "now" at the center; the ▏ ▕ rails frame the chart. Newest is at the center,
#     oldest at the outer edges. 12 cells per side.
#   • 'tps' is the LIVE tokens-per-second needle (how fast right now, EMA-smoothed);
#     the rows below are the TIMELINE (each cell one time-bucket). Both height and hue
#     rise with throughput.
#
# Design notes:
#   • Color encodes MEANING: brand colors group by system (Claude coral, GitHub
#     green); usage % uses a green→yellow→red HEALTH scale; the mirror uses WARM
#     for reading, COOL for writing, brighter/taller = higher throughput.
#   • Each row is calibrated to ITSELF: the 1m/10m/4h/1d timeline bars plot each grid
#     bucket's token AMOUNT linearly against that ROW's own reference — a robust
#     percentile of how many tokens a bucket of that width actually holds on this
#     machine (see _full_scales). So a busy bucket pegs, a typical one sits mid-bar, and
#     a taller bar simply read MORE than its neighbours. Per-timescale is the point: a
#     5 s bucket and a 2 h bucket hold wildly different amounts, so one shared scale
#     can't serve both. The references are learned PER-MACHINE (pooled across sessions)
#     and FROZEN between refreshes, so the bars are static — nothing dances render to
#     render; only the newest (partial) bucket fills live. Buckets are grid-anchored, so
#     the row scrolls one bucket at a time. The 'tps' needle is separate: the live
#     tokens/sec rate against a per-second reference.
#   • Repo + PR are OSC 8 hyperlinks — Cmd-click to open on GitHub.
#   • Git is cached per session (12s TTL) so a big dirty repo never lags renders.
#   • TEXT ENCODING is decided by WHO IS READING (see _emit / _ASCII_FALLBACK). Every
#     glyph above is outside Latin-1, and before Python 3.15 (PEP 686) Windows takes a
#     PIPE's encoding from the locale — cp1252 — which can encode none of them. The
#     footer has exactly ONE reader (Claude Code, over a pipe, which takes UTF-8), so
#     the render path pins stdout to UTF-8 and always ships the real chart. The control
#     verbs print wherever the USER redirected them, so THERE a declared
#     PYTHONIOENCODING wins and the chart degrades to an ASCII ramp that still charts.
#     stdin is pinned to UTF-8 too (errors="replace"), so a non-ASCII cwd or branch
#     can't mangle the Git section. Getting this wrong is invisible: an encode failure
#     inside _render is caught by main()'s never-throw guard, which prints "", so the
#     footer goes BLANK at exit 0 — the safety net HIDES the failure. That is why the
#     tests assert information survival, not absence-of-traceback.
#
# ── What the mirror MEASURES (read this before trusting the numbers) ──
#   READING is NON-CACHE input: the tokens the model genuinely reads fresh each turn —
#   new/uncached input plus tokens newly written to cache — NOT the whole context
#   re-read from cache. Claude Code's `total_input_tokens` is that full re-read (mostly
#   cache) and would make reading a churn number ~100× writing; revline instead sums
#   `current_usage.input_tokens + cache_creation_input_tokens`, so reading reflects
#   real new work and ends up comparable to WRITING (output). Both are per-response
#   snapshots (NOT cumulative in the JSON since v2.1.132), accumulated into a small
#   local store and charted over five timescales. This is a THROUGHPUT / activity view,
#   NOT a dollar figure. For spend, watch `cost: $…` and the `cache:` hit-ratio; the
#   `context:` line still shows the full context size (total_input). Tokens ≈ activity.
#
# Runs locally, reads Claude Code's status JSON on stdin, prints text.
# It costs ZERO API tokens — the status line is never sent to the model.
# ═══════════════════════════════════════════════════════════════
import json
import os
import re
import sys
import time

# The status line spawns a FRESH process every render (~1/sec), so every top-level
# import is paid on EVERY render. `subprocess` (git, ~1 render in 12), `glob` (only
# throttled/cold paths), and `unicodedata` (horizontal layout only) are therefore
# imported LAZILY inside the functions that need them — `subprocess`/`tempfile` alone
# cost ~5 ms each at module load (tempfile drags in random/shutil/bz2/lzma to call
# gettempdir() once), and none is needed on the common warm render.

# Per-session state files live here. Resolved from the standard temp-dir env vars
# (the same ones tempfile.gettempdir() consults) so the git cache + token gauges also
# work on Windows (TEMP/TMP are set there) — without importing tempfile. Basenames
# stay "cc-statusline-*" so _gc's glob still matches existing files.
TMP = (
    os.environ.get("TMPDIR")
    or os.environ.get("TEMP")
    or os.environ.get("TMP")
    or "/tmp"
).rstrip("/")
# Token-store schema version. BUMP THIS whenever the meaning of what's stored changes
# (e.g. reading switched from total-input to non-cache), so every session abandons its
# now-invalid old store and re-accumulates from scratch instead of carrying a stale
# base forward. Old-version files still match the "cc-statusline-*" GC glob and expire.
_STORE_VER = 2

# ── ANSI ──────────────────────────────────────────────────────
# Truecolor (24-bit) for the brand colors — exact hex. Needs a terminal with
# 24-bit color (PyCharm terminal, iTerm2, VS Code, WezTerm, modern tmux all do).
R = "\033[0m"  # reset
DIM = "\033[2m"
GREEN = "\033[32m"  # the threshold green used in the gauge percentages
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
# Semantic group colors — color says WHICH SYSTEM a line belongs to:
ORANGE = "\033[38;2;217;119;87m"  # Claude coral #D97757 — the Claude Code group
GH = "\033[38;2;77;171;247m"  # light azure blue #4DABF7 — the repo / branch group
EFF = "\033[38;5;141m"  # violet — the Gauges section header
LABEL = "\033[38;5;245m"  # readable gray — brighter than DIM so labels show


def hdr(name, color):
    """A section header: bold, in the section's theme color."""
    return f"{color}{BOLD}{name}{R}"


def col(pct):
    """Threshold color: green calm, yellow watch, red danger."""
    if pct is None:
        return DIM
    if pct >= 90:
        return RED
    if pct >= 70:
        return YELLOW
    return GREEN


def bar(pct, width=10):
    """Justin's █/░ meter, colored by fill level."""
    p = 0 if pct is None else max(0, min(100, int(pct)))
    filled = round(p * width / 100)
    return f"{col(p)}{'█' * filled}{'░' * (width - filled)}{R}"


def until(epoch):
    """Compact countdown to a unix-epoch reset time (when a budget refills)."""
    if not epoch:
        return ""
    s = int(epoch - time.time())
    if s <= 0:
        return "now"
    if s >= 86400:
        d, h = s // 86400, (s % 86400) // 3600
        return f"{d}d{h}h" if h else f"{d}d"
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    return f"{max(1, s // 60)}m"


def ago(epoch):
    """Compact 'time since' a past unix epoch (last-commit age)."""
    if not epoch:
        return ""
    s = int(time.time() - epoch)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def ktok(n):
    """Human token count: 212000 → '212k', 1200000 → '1.2M'."""
    n = int(n or 0)
    if n >= 999_500:  # 999_500+ rounds to 1.0M, so roll it to the M branch
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(n)


def link(url, text):
    """Wrap text as an OSC 8 hyperlink (Cmd/Ctrl-click). Plain text if no url.
    Terminated with ST (ESC \\) rather than BEL — a terminal that doesn't consume
    OSC 8 silently swallows ST, whereas BEL would ring the bell (up to ~4×/sec at
    this refresh in a non-OSC-8 terminal like PyCharm's)."""
    if not url:
        return text
    st = "\033\\"
    return f"\033]8;;{url}{st}{text}\033]8;;{st}"


SPARK = "▁▂▃▄▅▆▇█"  # 8 heights — indexed directly by the mirror cells / tach

# ── output encoding: degrade the GLYPHS, never the information ──
# Every glyph above is outside Latin-1. Until Python 3.15 ships PEP 686 (UTF-8 mode on
# by default), Windows picks the default encoding for a PIPE — which is exactly how
# Claude Code reads this — from the locale, historically cp1252. Encoding then fails,
# _render raises, main()'s never-throw guard catches it and prints "", and the footer
# goes BLANK at exit 0. The guard holds and the feature is silently 100% dead.
#
# So the fallback is a ramp that still CHARTS in pure ASCII. A plainer mirror reads
# fine; an empty footer tells you nothing. Ordered by visual weight so the bars still
# show relative magnitude at a glance.
_ASCII_FALLBACK = str.maketrans(
    {
        "▁": "_",
        "▂": ".",
        "▃": ":",
        "▄": "-",
        "▅": "=",
        "▆": "+",
        "▇": "*",
        "█": "#",
        "░": ".",
        "│": "|",
        "▏": "[",
        "▕": "]",
        "─": "-",
        "–": "-",
        "—": "-",
        "·": "-",  # the _USAGE section separator — 11 of them; missed on the first pass
        "↑": "^",
        "↓": "v",
        "✓": "+",
        "●": "o",
        "✎": "~",
        "◌": ".",
        "→": "->",
        "←": "<-",
        "≤": "<=",
        # The Braille-blank indent has no ASCII twin that survives Claude Code's
        # per-line trim. A plain space may get trimmed, costing the indent — still
        # strictly better than losing the line.
        "⠀": " ",
    }
)


def _emit(text, honour_declared=False):
    """Print `text`, degrading glyphs to ASCII when stdout cannot carry them.

    `honour_declared` splits the two callers by WHO is reading, which is the whole
    design. The FOOTER has exactly one consumer — Claude Code, reading a pipe — and it
    takes UTF-8, so the render path upgrades unconditionally: a Windows pipe defaulting
    to cp1252 is an accident, and an unrelated global PYTHONIOENCODING left over from
    some other script is not Claude Code declaring anything. The CONTROL VERBS
    (help/flip/layout) print wherever the USER pointed them — a terminal, a redirect, a
    pipe into something else — so there a declared encoding IS authoritative and gets
    honoured, dropping to the ASCII ramp rather than overriding them.

    That split is also what keeps _ASCII_FALLBACK a live, reachable, testable path:
    reconfigure succeeds on every Python 3.7+, so with an unconditional upgrade
    everywhere the ramp would be dead code."""
    out = sys.stdout
    enc = (getattr(out, "encoding", None) or "utf-8").lower()
    declared = honour_declared and os.environ.get("PYTHONIOENCODING")
    if enc.replace("-", "") != "utf8" and not declared:
        try:
            out.reconfigure(encoding="utf-8")
            enc = "utf-8"
        except Exception:
            pass
    # The probe is a SECOND encode of the same text and that is deliberate — measured at
    # 0.83us against a ~21ms render (0.004%), and it is load-bearing: if stdout carries a
    # lenient handler (PYTHONIOENCODING=ascii:replace) print() never raises and you get
    # "?" soup. text.encode() is always STRICT, so the probe catches that and hands back
    # the readable ramp instead. Do not "optimise" it away, and do not probe only the
    # glyph set — that would miss a non-ASCII branch or directory name.
    try:
        text.encode(enc)
    except (UnicodeEncodeError, LookupError):
        text = text.translate(_ASCII_FALLBACK)
    try:
        print(text)
    except UnicodeEncodeError:
        # Belt and braces: a glyph added later with no entry in the table above.
        print(text.encode("ascii", "replace").decode("ascii"))


def _atomic_write(path, text):
    """Write via a temp file + os.replace, so a concurrent reader always sees a
    complete old or complete new file — never a torn line. The status line fires
    on both the 1s timer AND after a message, so same-session writes can overlap."""
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _read_store(path):
    """Parse a token store, skipping any single malformed (e.g. torn) line
    rather than dropping the entire history on one bad row. Rows are int tuples
    of whatever arity was written — the token store is
    (t, in_cum, out_cum, in_raw, out_raw)."""
    rows = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            for ln in lines:
                try:
                    rows.append(tuple(int(x) for x in ln.split(",")))
                except Exception:
                    continue
    except Exception:
        rows = []
    return rows


def _write_store(path, rows):
    _atomic_write(path, "\n".join(",".join(str(x) for x in r) for r in rows))


def _gc():
    """Best-effort, once per hour: delete cc-statusline-* state files older than
    a day so /tmp doesn't accumulate one set per session forever."""
    marker = os.path.join(TMP, "cc-statusline-gc")
    try:
        now = time.time()
        if os.path.exists(marker) and now - os.path.getmtime(marker) < 3600:
            return
        import glob  # only reached past the hourly throttle — keep it off warm renders

        _atomic_write(marker, str(int(now)))
        for f in glob.glob(os.path.join(TMP, "cc-statusline-*")):
            try:
                # state files expire after a day idle; but a `.tmp.PID` file is an
                # orphaned atomic-write scratch (left only if a render was SIGKILLed
                # between open() and os.replace()) — pure garbage, so sweep it after
                # an hour rather than making it wait the full day.
                age = now - os.path.getmtime(f)
                if age > 86400 or (".tmp." in os.path.basename(f) and age > 3600):
                    os.remove(f)
            except Exception:
                pass
        # per-session calibration logs/scales live beside the script (not in TMP);
        # sweep a session's files once they've been idle for a day.
        for f in glob.glob(os.path.join(_here(), "token_rates-*")):
            try:
                if now - os.path.getmtime(f) > 86400:
                    os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


# Per-MACHINE, per-TIMESCALE scale calibration. The gauge full-scale isn't hardcoded
# (that would be wrong on any machine/plan/usage but the one it was tuned on) — revline
# records the real token flow to a gitignored MACHINE-LEVEL history log and, for each
# row, sets "full bar" to a robust high percentile of how many tokens a bucket of that
# width actually holds on this machine (see _full_scales). Per-TIMESCALE is the whole
# point: a 5 s bucket and a 2 h bucket hold wildly different amounts, so one scale can't
# serve both — it pegs the short rows or flattens the long ones. Per-MACHINE (pooled
# across sessions) because more history = a steadier reference, and the statistic is a
# DISTRIBUTION of bucket sizes, not a rate over total time, so overlapping sessions
# don't distort it. The references are FROZEN between refreshes, so a given amount
# always maps to the same height — static, no render-to-render dancing. The log is one
# append-only line per activity event ("epoch,read,write"); idle events (both 0)
# skipped. Lives next to the script (realpath) and is gitignored; self-prunes.
_RATE_LOG = "token_rates.log"  # the machine's throughput memory (the calibration data)
_SCALE_CACHE = "token_rates.scale"  # the frozen per-timescale references (JSON)
_RATE_LOG_KEEP = 20000  # lines retained — enough history for the long (4h/1d) rows
_RATE_LOG_TRIM_AT = (
    800_000  # trim (to _KEEP lines) once the file passes ~this many bytes
)
_SCALE_TTL = 1800  # refresh the frozen references at most every 30 min (else static)
_SCALE_MIN_N = 8  # min active buckets before trusting a row's fit (else scaled default)

# (span_seconds, bucket_slot_seconds, percentile). The percentile is the "how full is
# full" knob — lower = fuller/livelier bars (a typical bucket reads high), higher =
# only your busiest buckets peg. 1m is lower so the short row isn't too quiet. The
# leading span=1 entry is NOT a timeline row — it's the tps needle's own per-SECOND
# reference (a busy second's tokens), so the live rate isn't scaled off a 5 s bucket ÷ 5
# (which pegged the needle on a median turn, since a turn's tokens land in a lump).
_SCALE_SPECS = (
    (1, 1, 80),
    (60, 5, 70),
    (600, 50, 80),
    (14400, 1200, 88),
    (86400, 7200, 90),
)


def _log_rate(t, read, write):
    """Append one activity sample to the machine-level history log, then keep it small:
    once it passes _RATE_LOG_TRIM_AT it's rewritten to the most recent _RATE_LOG_KEEP
    lines. One shared log is fine — calibration is a distribution of bucket sizes, not a
    rate over total time, so concurrent sessions don't distort it. Best-effort: any
    error is swallowed so logging never disturbs a render."""
    if read <= 0 and write <= 0:
        return
    try:
        path = os.path.join(_here(), _RATE_LOG)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{t},{int(read)},{int(write)}\n")
        if os.path.getsize(path) > _RATE_LOG_TRIM_AT:
            with open(path, encoding="utf-8") as fh:
                kept = fh.read().splitlines()[-_RATE_LOG_KEEP:]
            _atomic_write(path, "\n".join(kept) + "\n")
    except Exception:
        pass


def _pctl(sorted_vals, p):
    """p-th percentile of a pre-sorted list by the NEAREST-RANK method: rank =
    ceil(p/100 · n), returning the value at that 1-based rank (index rank−1). Uses
    integer ceil (−(−a // b)) to avoid a math import. The old int(p/100·n) was a floor
    index, biased ~1 rank high. Clamped to the ends; 0.0 when empty. (At small n a high
    percentile legitimately resolves to the max — that's the estimator, not a bug.)"""
    n = len(sorted_vals)
    if not n:
        return 0.0
    idx = -(-(int(p) * n) // 100) - 1  # ceil(p·n/100) − 1, as a 0-based index
    return float(sorted_vals[max(0, min(n - 1, idx))])


def _full_scales():
    """FROZEN per-machine, per-timescale full-scale AMOUNTS — one (read_full, write_full)
    per timeline span. For each span, bin the machine history into that row's bucket
    width and take a robust percentile of the ACTIVE (non-empty) bucket amounts: how
    many tokens a busy bucket holds, so a typical bucket lands mid-bar and only the
    busiest peg (a taller bar read more than its neighbours). Returns {span: (fr, fw)}.
    Cached to _SCALE_CACHE and held constant for _SCALE_TTL secs → the bars are static.
    A row with < _SCALE_MIN_N active buckets falls back to the per-second default scaled
    to its slot. Reads token_rates*.log (the machine log + any legacy per-session logs
    still on disk) so history isn't lost on the switch. Best-effort."""
    here = _here()
    cache = os.path.join(here, _SCALE_CACHE)
    now = time.time()
    try:
        if os.path.exists(cache) and now - os.path.getmtime(cache) < _SCALE_TTL:
            with open(cache, encoding="utf-8") as fh:
                d = json.loads(fh.read())
            return {int(k): tuple(v) for k, v in d.items()}
    except Exception:
        pass

    import glob  # only reached on a cold recompute (TTL miss), not the warm cache hit

    # default per-row full = the per-second default scaled to that bucket's width
    out = {
        span: (float(_FULL_READ_DEFAULT * slot), float(_FULL_WRITE_DEFAULT * slot))
        for span, slot, _ in _SCALE_SPECS
    }
    try:
        events = []
        for path in glob.glob(os.path.join(here, "token_rates*.log")):
            with open(path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            for ln in lines:
                p = ln.split(",")
                if len(p) == 3:
                    try:  # skip ONE torn/garbled line, don't abandon all calibration
                        events.append((int(p[0]), int(p[1]), int(p[2])))
                    except ValueError:
                        continue
        events.sort()  # for the tps needle's consecutive-event rate
        # tps needle (span=1): percentile of the INSTANTANEOUS rate (read ÷ gap-seconds),
        # which is what ema_rate actually displays — NOT per-second token sums (a turn's
        # tokens land in one lump, so summing them pegged the needle). dt filtered to
        # real gaps (skip sub-second and cross-session mega-gaps).
        rrate, wrate = [], []
        prev = None
        for t, r, w in events:
            if prev is not None and 1 <= t - prev <= 300:
                if r:
                    rrate.append(r / (t - prev))
                if w:
                    wrate.append(w / (t - prev))
            prev = t
        rrate.sort()
        wrate.sort()
        for span, slot, pctl in _SCALE_SPECS:
            if span == 1:
                rv, wv = rrate, wrate  # instantaneous rates (tok/sec)
            else:  # timeline row: per-bucket token amounts at this slot width
                rb, wb = {}, {}
                for t, r, w in events:
                    if r:
                        rb[t // slot] = rb.get(t // slot, 0) + r
                    if w:
                        wb[t // slot] = wb.get(t // slot, 0) + w
                rv, wv = sorted(rb.values()), sorted(wb.values())
            fr = _pctl(rv, pctl) if len(rv) >= _SCALE_MIN_N else out[span][0]
            fw = _pctl(wv, pctl) if len(wv) >= _SCALE_MIN_N else out[span][1]
            out[span] = (max(1.0, fr), max(1.0, fw))
        _atomic_write(cache, json.dumps({str(k): list(v) for k, v in out.items()}))
    except Exception:
        pass
    return out


def record_tokens(sid, in_raw, out_raw, size=None):
    """Update the token stores and return (fine_rows, day_rows, now). in_raw is the
    CURRENT-RESPONSE non-cache input (uncached input + cache-creation, computed in
    _render); out_raw is total_output_tokens. Both are per-response snapshots (NOT
    cumulative in the JSON since v2.1.132). We build the only cumulative token history
    that exists here so every gauge draws from one store — but the two sides accumulate
    exists here so every gauge draws from one store — but the two sides accumulate
    DIFFERENTLY (see the `changed` block): input is a per-turn FLOW counted once per
    turn, output is a per-response counter summed as a sawtooth. Rows are
    (t, in_cum, out_cum, in_raw, out_raw). Two stores: a fine one
    (1 h, written on every change) drives the seconds/1m/10m rows; a throttled coarse
    one (25 h, ≥120 s between writes) drives the 4h/1d rows without unbounded row
    growth at 1 s refresh. `size` (context window) clamps in_raw as a guard: input
    can never exceed the window, so if the per-response premise ever broke and the
    field went cumulative, this bounds the damage to one window/turn instead of
    letting a millions-token cumulative explode every reading gauge — and it never
    touches correct data, where in_raw ≤ size already holds."""
    now = int(time.time())
    if size and size > 0:
        in_raw = min(in_raw, size)  # hard invariant, not a heuristic: input ≤ window
    fpath = os.path.join(TMP, f"cc-statusline-io-v{_STORE_VER}-{sid}")
    dpath = os.path.join(TMP, f"cc-statusline-ioday-v{_STORE_VER}-{sid}")
    fine = [r for r in _read_store(fpath) if len(r) >= 5]  # ignore torn short rows
    if fine:
        in_cum, out_cum, prev_in, prev_out = fine[-1][1:5]
    else:
        in_cum = out_cum = 0
        prev_in = prev_out = None
    # A snapshot only moves when a response streams or a new turn lands, so at 1s
    # refresh most renders are unchanged — only append + rewrite when a raw total
    # actually moved. This keeps the store to change-points only (~100 rows/hr, not
    # 3600), so reads/bucketing stay cheap.
    # NOTE: this read-modify-write is not locked. The render fires on both the 1s
    # timer and after a message, so two instances can overlap; _atomic_write keeps
    # the file itself intact (never a torn read), but the later os.replace can drop
    # the other's appended row. Effect is a rare (~1%) undercount of a throughput
    # gauge — never corruption. A lock would be POSIX-only (fcntl) and break the
    # Windows portability above, so we accept the cosmetic gap rather than add one.
    changed = prev_in is None or in_raw != prev_in or out_raw != prev_out
    if changed:
        # in_raw and out_raw are DIFFERENT kinds of per-response snapshot, so they
        # accumulate differently:
        #   • input is the NON-CACHE tokens read this turn (a per-turn FLOW): count
        #     in_raw ONCE per turn, i.e. only when it actually changes. The status line
        #     re-fires ~1s DURING a response with in_raw unchanged (only out_raw
        #     growing); adding in_raw on those restreams was a ~2× reading over-count.
        #     Two genuine turns with an identical unchanged in_raw undercount the read
        #     by one turn — rare, since fresh input differs turn to turn.
        #   • output is a per-RESPONSE cumulative counter that grows mid-stream and
        #     resets to ~0 each new turn: sum the SAWTOOTH (rise → the delta, drop →
        #     the new value) so each response's final output is counted exactly once,
        #     not once per mid-stream sample (which over-counts up to ~6× on long
        #     replies).
        if prev_in is None:
            read_add, write_add = in_raw, out_raw
        else:
            read_add = in_raw if in_raw != prev_in else 0
            write_add = out_raw - prev_out if out_raw >= prev_out else out_raw
        in_cum += read_add
        out_cum += write_add
        fine.append((now, in_cum, out_cum, in_raw, out_raw))
        # Prune to the last ~61 min, but ALWAYS retain the newest dropped row as a
        # baseline ANCHOR. After a long idle gap every pre-idle row ages out at once,
        # leaving only post-idle rows that carry the lifetime cumulative — then
        # _cum_at() finds no row before a window's start, the baseline falls to 0, and
        # the 1m/10m gauge reads the WHOLE session's tokens (and can exceed the 4h row).
        # One carried-forward anchor keeps the window math floored to real recent work.
        cutoff = now - 3660
        kept = [r for r in fine if r[0] >= cutoff]
        dropped = [r for r in fine if r[0] < cutoff]
        if dropped and kept[0][0] > cutoff:
            kept = [dropped[-1]] + kept
        fine = kept
        _log_rate(now, read_add, write_add)  # machine-level calibration history
        _write_store(fpath, fine)
    day = [r for r in _read_store(dpath) if len(r) >= 5]
    if changed and (not day or now - day[-1][0] >= 120):
        day.append((now, in_cum, out_cum, in_raw, out_raw))
        day = [r for r in day if r[0] >= now - 90000]
        _write_store(dpath, day)
    return fine, day, now


# A store row is (t, in_cum, out_cum, in_raw, out_raw); these pick which
# cumulative a gauge charts — input (reading), output (writing), or their total.
def IN(r):
    return r[1]


def OUT(r):
    return r[2]


def TOT(r):
    return r[1] + r[2]


def _cum_at(rows, ts, val=TOT):
    """Cumulative value (per `val`) at a point in time (carry-forward last known)."""
    c = 0
    for r in rows:
        if r[0] <= ts:
            c = val(r)
        else:
            break
    return c


def bucket_series(rows, now, span_s, buckets=12, val=TOT, full=None):
    """0–100 per-bucket intensity of `val` over the last span_s seconds — each bucket's
    total tokens as a LINEAR fraction of `full` (this timescale's frozen per-machine
    reference; see _full_scales). Two properties:
      • GRID-ANCHORED buckets — edges snap to fixed slot boundaries, so the row scrolls
        one whole bucket at a time when time crosses a boundary, instead of every bucket
        creeping every render. Only the newest (partial) bucket fills live; the rest sit
        still. That's what makes it STATIC — no dancing.
      • AMOUNT vs a per-timescale reference — a taller bar simply read MORE than its
        neighbours; a typical bucket lands mid-bar and only a busy one pegs, because
        `full` is sized to this row's own bucket width.
    Any active bucket floors to a visible minimum; 0 stays blank. [] when nothing was
    consumed (or no `full`). One forward pass — O(rows + buckets)."""
    if not rows or not full:
        return []
    slot = span_s / buckets
    nb = int(now // slot)  # grid-aligned index of the current slot
    # 12 grid buckets ending at 'now'; the last one runs from its slot boundary to now
    edges = [(nb - (buckets - 1) + i) * slot for i in range(buckets)] + [now]
    cums, j, c = [], 0, 0
    for e in edges:  # carry-forward cumulative at each bucket edge, single walk
        while j < len(rows) and rows[j][0] <= e:
            c = val(rows[j])
            j += 1
        cums.append(c)
    out = []
    for i in range(buckets):
        amt = max(0, cums[i + 1] - cums[i])  # tokens in this bucket
        # the newest bucket is PARTIAL (its slot isn't over yet), so judge it against a
        # reference scaled to its elapsed fraction — else it under-reads a steady rate
        # (badly on the long rows: a 2 h bucket reads ~0 for its first hour) and pulses
        # 0→full each tick. Full-width buckets use `full` unchanged (frac = 1).
        ref = full * max(0.12, (edges[i + 1] - edges[i]) / slot)
        # 0 → blank (idle); any activity floors to a visible 12 so a faint bucket shows
        out.append(0 if amt <= 0 else max(12, min(100, round(100 * amt / ref))))
    return out


def ema_rate(rows, now, val=OUT, halflife=8.0):
    """Exponentially-smoothed rate of `val` in tokens/sec — the mph needle. Unlike
    a trailing window (a hard edge, so the needle snaps when a burst ages out), an
    EMA weights recent activity and lets older activity fade on a smooth exponential
    curve, so the speedometer glides. `halflife` = seconds for old activity to lose
    half its weight. Decays toward 0 while idle so a stalled session reads ~0."""
    if len(rows) < 2:
        return 0.0
    ema = 0.0
    pt, pv = None, None
    for r in rows:
        t, v = r[0], val(r)
        if pt is not None and t > pt:
            dt = t - pt
            rate = max(0, v - pv) / dt
            a = 1 - 0.5 ** (dt / halflife)
            ema += a * (rate - ema)
        pt, pv = t, v
    if pt is not None and now > pt:  # decay from the last change up to now
        a = 1 - 0.5 ** ((now - pt) / halflife)
        ema += a * (0.0 - ema)
    return ema


# cells per side of the mirror chart (reading | writing). 12 keeps the chart
# compact and buckets each span into clean slices (1m→5s, 10m→50s, 4h→20m, 1d→2h).
# The full chart is 2*HALF+1 wide (the +1 is the center divider).
HALF = 12
# the two mirror palettes: reading (input) warm, writing (output) cool. A cell's
# color walks up its ramp with magnitude, so busier = brighter.
_WARM = [52, 88, 124, 166, 202, 208, 214, 220]  # mirror reading (input) — warm
_COOLH = [17, 18, 24, 31, 38, 44, 45, 51]  # mirror writing (output) — cool
# DEFAULT full-scale RATES (tokens/sec) — the cold-start fallback for a row that hasn't
# yet seen _SCALE_MIN_N active buckets of history. _full_scales() scales these by a
# row's slot to get a default AMOUNT, then replaces them with the machine's own
# percentile once enough data accrues. Just sane non-cache starting points.
_FULL_READ_DEFAULT = 800
_FULL_WRITE_DEFAULT = 120
# tps needle responsiveness: the EMA halflife in seconds — how fast old activity fades
# from the live rate. Smaller = snappier (reacts to the current second), larger =
# smoother/laggier. 1.0s is snappy and live; raise to 1.5–2.0+ to calm it down.
_TACH_HALFLIFE = 1.0
_SEP = "\033[38;5;240m│" + R  # dim center divider marking "now" between read | write
_EDL = "\033[38;5;240m▏" + R  # left rail — where the chart starts (next to reading#)
_EDR = "\033[38;5;240m▕" + R  # right rail — where the chart ends (next to writing#)


def _rampc(ramp, depth):
    """Color at `depth` cells out from center (0 = center) along `ramp`, so a longer
    fill walks further up the ramp toward its bright end. Used by the tach row."""
    return f"\033[38;5;{ramp[round(depth / max(1, HALF - 1) * (len(ramp) - 1))]}m"


def mirror_tach(read_rate, write_rate, full_read, full_write):
    """The live 'tps' row: the tokens-per-second needle split down the middle. Reading
    fills LEDs leftward from center in the WARM palette (scaled to full_read tok/s),
    writing rightward in the COOL palette (scaled to full_write) — brighter toward the
    edge, a dim divider at 'now'. Fills grow with the current rate and drain as work
    quiets, so the needle slides both ways on the machine's calibrated scale."""

    def fill(rate, full):
        if not full:  # guard a degenerate 0 scale — never divide-by-zero the footer
            return 0
        return round(min(1.0, max(0.0, rate) / full) * HALF)  # linear vs peak

    lf, wf = fill(read_rate, full_read), fill(write_rate, full_write)
    left = [
        (f"{_rampc(_WARM, HALF - 1 - p)}█{R}" if HALF - 1 - p < lf else " ")
        for p in range(HALF)
    ]
    right = [(f"{_rampc(_COOLH, p)}█{R}" if p < wf else " ") for p in range(HALF)]
    return "".join(left) + _SEP + "".join(right)


def _mcell(ramp, v):
    """One mirror-history cell: a colored block whose height AND hue rise with
    magnitude; blank when the bucket is idle."""
    if v <= 1:
        return " "
    c = ramp[round(min(100, v) / 100 * (len(ramp) - 1))]
    return f"\033[38;5;{c}m{SPARK[round(min(100, v) / 100 * 7)]}{R}"


def mirror_hist(rows, now, span, full_read, full_write):
    """A history row of the butterfly: reading (warm) runs from the far-left
    (oldest) inward to the center (newest); writing (cool) runs from the center
    (newest) out to the far-right (oldest), a dim divider at 'now'. Both sides use
    the SAME calibrated rate scale as the tps needle (full_read / full_write), so a
    response streams IN at the center exactly like the needle and nothing strobes as
    the window slides (see bucket_series)."""
    rd = bucket_series(rows, now, span, HALF, IN, full_read) or [0] * HALF
    wr = bucket_series(rows, now, span, HALF, OUT, full_write) or [0] * HALF
    left = [_mcell(_WARM, v) for v in rd]
    right = [_mcell(_COOLH, v) for v in wr[::-1]]
    return "".join(left) + _SEP + "".join(right)


def git_state(cwd, sid):
    """Return git facts for cwd, cached per session (12s TTL) so a big dirty
    repo never lags a render (branch/commit-age/dirty-count don't need faster).
    Fields: repo, branch, staged, modified, untracked, ahead, behind,
    has_upstream, last_epoch, url."""
    keys = [
        "repo",
        "branch",
        "staged",
        "modified",
        "untracked",
        "ahead",
        "behind",
        "has_upstream",
        "last_epoch",
        "url",
    ]
    cache = os.path.join(TMP, f"cc-statusline-{sid}")
    if os.path.exists(cache) and (time.time() - os.path.getmtime(cache) < 12):
        try:
            with open(cache, encoding="utf-8") as fh:
                vals = fh.read().split("\t")
            if len(vals) == len(keys):
                g = dict(zip(keys, vals))
                for k in (
                    "staged",
                    "modified",
                    "untracked",
                    "ahead",
                    "behind",
                    "has_upstream",
                    "last_epoch",
                ):
                    g[k] = int(g[k] or 0)
                return g
        except Exception:
            pass

    g = {k: 0 for k in keys}
    g["repo"] = g["branch"] = g["url"] = ""

    import subprocess  # lazy: only reached on a git cache-miss (see the top-of-file note)

    stalled = []  # set once any git call times out — see below

    def run(*args):
        # If an earlier git call already timed out this render, the filesystem is
        # slow/wedged; don't stack another 2 s wait for each of the ~6 calls (that
        # stacks to a ~12 s footer stall). Bail instantly — the 12 s negative cache
        # means we won't retry for a while anyway.
        if stalled:
            return ""
        try:
            # git emits raw bytes for refs and paths, so text=True alone would decode
            # them with the LOCALE encoding (cp1252 on Windows before Python 3.15's
            # PEP 686). A branch or directory with a non-ASCII character would then
            # mangle or raise. Pin utf-8 — what git actually uses — and replace any
            # stray byte rather than let one odd character take out the whole footer.
            r = subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            stalled.append(1)
            return ""
        except Exception:
            return ""

    top = run("rev-parse", "--show-toplevel")
    if top:
        g["repo"] = os.path.basename(top)
        g["branch"] = run("rev-parse", "--abbrev-ref", "HEAD")
        for ln in run("status", "--porcelain").splitlines():
            if not ln.strip():
                continue
            if ln.startswith("??"):
                g["untracked"] += 1
                continue
            if ln[0] not in (" ", "?"):
                g["staged"] += 1
            if len(ln) > 1 and ln[1] in ("M", "D"):
                g["modified"] += 1
        ab = run("rev-list", "--left-right", "--count", "@{u}...HEAD").split()
        if len(ab) == 2:  # two ints only when a tracking branch exists
            g["behind"], g["ahead"] = int(ab[0]), int(ab[1])
            g["has_upstream"] = 1
        ct = run("log", "-1", "--format=%ct")
        g["last_epoch"] = int(ct) if ct.isdigit() else 0
        raw = run("remote", "get-url", "origin")
        if raw:
            url = re.sub(r"^git@([^:]+):", r"https://\1/", raw)
            g["url"] = re.sub(r"\.git$", "", url)
    # Cache the result even when cwd is NOT a repo (top == "" → g stays empty):
    # negative-caching the non-repo case for 12s means we don't re-spawn
    # `git rev-parse` on every render in a non-git directory.
    _atomic_write(cache, "\t".join(str(g[k]) for k in keys))
    return g


# ── layout ────────────────────────────────────────────────────
# Orientation: "vertical" (default) stacks all four sections; "horizontal" puts
# Claude Code + Git on the left and Gauges + Usage on the right as two aligned
# columns — handy for a wide, short footer. Chosen (in precedence order) by:
#   1. revline.config.json next to this script, e.g. {"layout": "H"}  (H | V)
#   2. the CC_STATUSLINE_LAYOUT env var (fallback for those who prefer env)
#   3. default "vertical"
# Set it with the CLI: `revline flip` (toggle), `revline horizontal`, `revline
# vertical` — these WRITE the config file, which the next render (~1s) picks up
# live. The script never receives settings.json (Claude Code only pipes the
# session payload on stdin), so config can't live in that block.
_ANSI_SGR = re.compile(r"\033\[[0-9;]*m")  # color / style codes
_OSC8 = re.compile(r"\033\]8;;[^\033]*\033\\")  # OSC-8 hyperlink open+close wrappers


def _here():
    """The directory holding this script — resolving symlinks, so the config file and
    per-machine calibration log are found next to the REAL file even when run via a PATH
    symlink. REVLINE_STATE_DIR overrides it: the test suite points it at a temp dir so
    tests never touch (or clobber) the live token_rates.log the real statusline shares."""
    return os.environ.get("REVLINE_STATE_DIR") or os.path.dirname(
        os.path.realpath(__file__)
    )


def _config_path():
    return os.path.join(_here(), "revline.config.json")


def _config():
    """Optional per-user settings from revline.config.json beside this script.
    Missing or malformed → {} (config is always optional; never crash on it)."""
    try:
        with open(_config_path(), encoding="utf-8") as fh:
            c = json.load(fh)
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _norm(value):
    """Any of H / h / horizontal / wide → 'horizontal'; everything else → 'vertical'.
    Accepts both the short codes stored in the config and the long words."""
    return (
        "horizontal"
        if str(value).strip().lower() in ("h", "horizontal", "wide")
        else "vertical"
    )


def _code(orientation):
    """The short code stored in the config file: 'horizontal' → 'H', else 'V'."""
    return "H" if orientation == "horizontal" else "V"


def _layout():
    return _norm(_config().get("layout") or os.environ.get("CC_STATUSLINE_LAYOUT", ""))


def _set_layout(value, announce=None):
    """Persist the orientation into revline.config.json as a short H/V code
    (merging, not clobbering other keys). The next render reads it live."""
    orientation = _norm(value)
    cfg = _config()
    cfg["layout"] = _code(orientation)  # store "H" / "V", not the long word
    try:
        _atomic_write(_config_path(), json.dumps(cfg, indent=2) + "\n")
        return announce or f"revline: layout → {_code(orientation)} ({orientation})"
    except Exception as e:
        return f"revline: could not write {_config_path()} ({e})"


def _flip():
    """Toggle orientation and persist it."""
    cur = _layout()
    new = "vertical" if cur == "horizontal" else "horizontal"
    return _set_layout(new, announce=f"revline: {_code(cur)} → {_code(new)} ({new})")


def _vis_width(s):
    """Displayed column width of a rendered line: strip SGR color codes and the
    invisible OSC-8 hyperlink wrappers (keeping the clickable TEXT between them),
    then count East-Asian-wide (CJK) glyphs as 2 cells and combining marks as 0.
    This is what lets the horizontal layout align two columns. (revline avoids
    emoji in aligned rows — terminals disagree on their width by a cell.)"""
    import unicodedata  # horizontal layout only — off the default vertical render

    s = _OSC8.sub("", _ANSI_SGR.sub("", s))
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _join_columns(left, right, gap=4):
    """Merge two rendered column blocks side by side: pad each left line to the
    widest left line (+gap) so the right column starts at a fixed offset. Uneven
    row counts are fine — the shorter side just runs out and its rows go blank."""
    lw = max((_vis_width(x) for x in left), default=0)
    rows = []
    for i in range(max(len(left), len(right))):
        lrow = left[i] if i < len(left) else ""
        rrow = right[i] if i < len(right) else ""
        if not rrow:
            rows.append(lrow)  # no trailing pad when the right side is empty
            continue
        pad = lw - _vis_width(lrow) + gap
        rows.append(f"{lrow}{' ' * pad}{rrow}")
    return rows


def obj(x):
    """Coerce a field to a dict for safe .get() chaining. Guards against a
    wrong-typed scalar (e.g. a future schema shipping `model` as a bare string):
    `"x".get(...)` would raise and — via main()'s outer guard — blank the WHOLE
    footer. obj() degrades that one field to defaults instead. (`or {}` alone
    doesn't help: a truthy non-dict like a string passes straight through.)"""
    return x if isinstance(x, dict) else {}


def numf(x, default=0.0):
    """obj()'s scalar sibling: coerce a numeric field to a float for math, so a
    wrong-typed value (e.g. a future schema shipping a token count as the string
    "5") can't raise mid-render and — via main()'s outer guard — blank the WHOLE
    footer. None / non-numeric → default. float() first so both 5 and "5" work."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _render(d):
    # ── fields (all defensive: many are null early / absent / wrong-typed) ──
    model = obj(d.get("model")).get("display_name") or "?"
    effort = obj(d.get("effort")).get("level")
    thinking = obj(d.get("thinking")).get("enabled")
    cwd = obj(d.get("workspace")).get("current_dir") or d.get("cwd") or ""
    sid = d.get("session_id") or "nosession"
    if not re.fullmatch(
        r"[A-Za-z0-9._-]{4,}", sid
    ):  # keep it path-safe (it names files)
        sid = "nosession"
    name = d.get("session_name")  # set via /rename or --name; absent when unnamed

    ctx = obj(d.get("context_window"))
    up = ctx.get("used_percentage")
    used = None if up is None else numf(up)  # keep None (unknown early) vs a number
    tokens = max(
        0, int(numf(ctx.get("total_input_tokens")))
    )  # context size, for display
    # The reading gauge counts NON-CACHE input — the tokens genuinely read fresh each
    # turn (uncached input + tokens newly written to cache), NOT the whole context
    # re-read from cache. That cache re-read is the bulk of total_input_tokens and would
    # make "reading" a churn number ~100× writing; non-cache reflects real new work, so
    # read and write end up comparable. Falls back to the total if a schema lacks the
    # current_usage breakdown.
    cu = obj(ctx.get("current_usage"))
    if (
        cu.get("input_tokens") is not None
        or cu.get("cache_creation_input_tokens") is not None
    ):
        in_raw = max(
            0,
            int(
                numf(cu.get("input_tokens"))
                + numf(cu.get("cache_creation_input_tokens"))
            ),
        )
    else:
        in_raw = tokens  # older schema without the breakdown → fall back to total
    out_raw = max(0, int(numf(ctx.get("total_output_tokens"))))
    size = ctx.get("context_window_size")
    size = int(size) if isinstance(size, (int, float)) else None
    size_lbl = "1M" if size and size >= 1_000_000 else ("200k" if size else "")

    cost = obj(d.get("cost"))
    usd = numf(cost.get("total_cost_usd"))
    added = cost.get("total_lines_added") or 0
    removed = cost.get("total_lines_removed") or 0
    dur_ms = cost.get("total_duration_ms") or 0

    rl = obj(d.get("rate_limits"))
    five = obj(rl.get("five_hour")).get("used_percentage")
    week = obj(rl.get("seven_day")).get("used_percentage")
    five_reset = obj(rl.get("five_hour")).get("resets_at")
    week_reset = obj(rl.get("seven_day")).get("resets_at")

    pr = obj(d.get("pr"))
    pr_num = pr.get("number")
    pr_url = pr.get("url")
    pr_state = pr.get("review_state")

    # repo URL: prefer the native repo identity, fall back to git remote
    rmeta = obj(obj(d.get("workspace")).get("repo"))
    native_url = ""
    if rmeta.get("owner") and rmeta.get("name"):
        native_url = f"https://{rmeta.get('host', 'github.com')}/{rmeta['owner']}/{rmeta['name']}"

    g = git_state(cwd, sid)
    repo = g["repo"] or (os.path.basename(cwd) if cwd else "")
    repo_url = native_url or g["url"]

    def lbl(text):
        """Readable-gray label prefix with a trailing colon — says what a
        value is. Brighter than DIM so labels are easy to see."""
        return f"{LABEL}{text}:{R} "

    IND = (
        "\u2800" * 2
    )  # Braille-blank indent (renders blank but survives Claude Code's per-line trim; ASCII/NBSP get trimmed)

    def glabel(t):  # right-aligned gauge row label (mph / secs / 1m / 4h …)
        return f"{LABEL}{t.rjust(4)}:{R} "

    upct = 0 if used is None else int(used)
    # Update the token store ONCE up front (in_raw/out_raw coerced at the top);
    # both the burn stat (below) and the Gauges section derive from it — no
    # separate burn-rate file. `size` lets record_tokens clamp an implausible
    # input to the window (a guard against the per-response premise ever breaking).
    rows, day_rows, cnow = record_tokens(sid, in_raw, out_raw, size)
    cum_tok = (rows[-1][1] + rows[-1][2]) if rows else 0

    # Each section is its own block of lines, so the layout can stack them
    # (vertical) or place them in two columns (horizontal). Building them
    # separately changes nothing about their content — only how they're assembled.

    # ═══ Claude Code ═══
    # The model gets its own row with thinking (on/off) beside it; effort is its own
    # row below. No emoji here on purpose — a wide glyph like 🧠 renders as 1 cell on
    # some terminals and 2 on others, which would knock the horizontal layout's
    # column alignment off by a cell.
    sec_cc = [hdr("Claude Code:", ORANGE)]

    # model on its own row, with thinking (on/off) to its right
    lm = f"{IND}{lbl('model')}{ORANGE}{BOLD}{model}{R}"
    if thinking is not None:
        tv = f"{ORANGE}{BOLD}on{R}" if thinking else f"{DIM}off{R}"
        lm += f"   {lbl('thinking')}{tv}"
    sec_cc.append(lm)

    # effort as radio buttons — the active level is bracketed (a live INDICATOR,
    # not an input: the status line can't capture clicks; change it with /effort
    # or /fast and the brackets follow the JSON's live effort.level)
    if effort:
        opts = [
            f"{ORANGE}{BOLD}[{o}]{R}" if o == effort else f"{DIM}{o}{R}"
            for o in ("low", "medium", "high", "xhigh", "max")
        ]
        sec_cc.append(f"{IND}{lbl('effort')}" + " ".join(opts))

    if name:
        title = f"{ORANGE}{BOLD}{name}{R}"
    elif sid and sid != "nosession":
        title = f"{DIM}#{sid[:6]}{R}"
    else:
        title = ""
    sec_cc.append(f"{IND}{lbl('session name')}{title}")

    secs = dur_ms // 1000
    tstr = (
        f"{secs // 3600}h{(secs % 3600) // 60}m" if secs >= 3600 else f"{secs // 60}m"
    )
    lc = f"{lbl('cost')}{ORANGE}${usd:.2f}{R}"
    if added or removed:
        lc += f"   {lbl('lines')}{GREEN}+{added}{R}{DIM}/{R}{RED}-{removed}{R}"
    lc += f"   {lbl('time')}{ORANGE}{tstr}{R}"
    sec_cc.append(f"{IND}{lc}")

    # context bar/% keep their threshold color (health); the token tail is orange
    tail = f" {DIM}:{R} {ORANGE}{ktok(tokens)} / {size_lbl or '?'}{R}"
    sec_cc.append(f"{IND}{lbl('context')}{bar(used)} {col(upct)}{upct}%{tail}")

    stats = []
    cr = cu.get("cache_read_input_tokens")  # cu computed up top (feeds in_raw too)
    if cr is not None:
        tot_in = (
            (cu.get("input_tokens") or 0)
            + (cu.get("cache_creation_input_tokens") or 0)
            + cr
        )
        if tot_in > 0:
            hit = round(100 * cr / tot_in)  # high = good (more cheap cache reads)
            hc = GREEN if hit >= 80 else YELLOW if hit >= 50 else RED
            stats.append(f"{lbl('cache')}{hc}{hit}%{R}")
    burn_mins = dur_ms / 60000  # burn = cumulative consumed / session minutes
    if burn_mins >= 0.1 and cum_tok > 0:
        stats.append(f"{lbl('burn')}{ORANGE}{ktok(round(cum_tok / burn_mins))}/min{R}")
    api_ms = cost.get("total_api_duration_ms") or 0
    if dur_ms > 0 and api_ms > 0:
        stats.append(f"{lbl('api')}{ORANGE}{round(100 * api_ms / dur_ms)}% of wall{R}")
    if stats:
        sec_cc.append(f"{IND}" + "   ".join(stats))

    # ═══ Git ═══ (only for a real repo — g["repo"] is set iff inside a worktree)
    sec_git = []
    if g["repo"]:
        sec_git.append(hdr("Git:", GH))
        l2 = f"{lbl('repo')}{GH}{BOLD}{link(repo_url, repo)}{R}"
        if g["branch"]:
            l2 += f"   {lbl('branch')}{GH}{BOLD}{g['branch']}{R}"
            if g.get("has_upstream"):
                if g["ahead"]:
                    l2 += f" {GH}↑{g['ahead']}{R}"
                if g["behind"]:
                    l2 += f" {GH}↓{g['behind']}{R}"
                if not g["ahead"] and not g["behind"]:
                    l2 += f" {GH}✓synced{R}"
            else:
                l2 += f" {DIM}local{R}"  # no tracking branch — not "synced"
        if pr_num:
            gl = {
                "approved": "✓ approved",
                "pending": "● review pending",
                "changes_requested": "✎ changes requested",
                "draft": "◌ draft",
            }.get(pr_state, "")
            l2 += f"   {lbl('PR')}{GH}{link(pr_url, '#' + str(pr_num))}{R} {DIM}{gl}{R}"
        sec_git.append(f"{IND}{l2}")

        if g["branch"]:
            dirty = g["staged"] + g["modified"] + g["untracked"]
            if dirty:
                parts = []
                if g["staged"]:
                    parts.append(f"{lbl('staged')}{GH}+{g['staged']}{R}")
                if g["modified"]:
                    parts.append(f"{lbl('mod')}{GH}~{g['modified']}{R}")
                if g["untracked"]:
                    parts.append(f"{lbl('new')}{GH}?{g['untracked']}{R}")
                change = "  ".join(parts)
            else:
                change = f"{GH}clean{R}"
            age = ago(g["last_epoch"])
            cl = change
            if age:
                cl += f"   {lbl('last commit')}{GH}{age}{R}"
            sec_git.append(f"{IND}{cl}")

    # ═══ Gauges — the read/write "mirror" chart ═══
    # rows / day_rows / cnow come from the single record_tokens call up top.
    # Each row reads:  <label> <reading#> ▏<chart>▕ <writing#> — the reading total
    # sits on the reading (left) side, the writing total on the writing (right) side,
    # and the ▏ ▕ rails frame exactly where the chart starts and ends.
    _cw = 2 * HALF + 1  # chart width incl. the center divider
    _RN = 6  # reading-number field width (right-aligned, just left of the chart)
    # The header carries the time axis BETWEEN the read and write labels: 'now' is
    # the CENTER of every row (over the │), oldest is at both edges — time runs
    # OUTWARD. The 25-col axis sits exactly where the charts do (its 'now' at offset
    # 12 lands under the center divider). A row's chart starts at column
    # IND(2)+glabel(6)+reading#(_RN)+space+rail = 16, so the read label is padded out
    # to that column and the write label pinned two cols past the axis (the wnum
    # column), keeping the whole header aligned to the rows below.
    _RLAB, _WLAB = "read", "write"  # left = input (read), right = output (written)
    _axis = f"\033[38;5;240molder ────{R} {LABEL}now{R} \033[38;5;240m──── older{R}"
    _chart_col = 2 + 6 + _RN + 1 + 1  # IND + glabel + reading# + space + rail = 16
    _lpad = " " * (_chart_col - len("Gauges: ") - len(_RLAB))
    sec_gauge = [
        f"{hdr('Gauges:', EFF)} \033[38;5;208m{_RLAB}{R}{_lpad}{_axis}  "
        f"\033[38;5;45m{_WLAB}{R}"
    ]

    def grow(label, chart, rnum, wnum):
        return (
            f"{IND}{glabel(label)}{DIM}{rnum:>{_RN}}{R} "
            f"{_EDL}{chart}{_EDR} {DIM}{wnum}{R}"
        )

    # the mirror: 'tps' is the live tokens-per-second needle split read|write (a
    # RATE — "how fast right now"); the rows below are the timeline history — reading
    # (warm) left, writing (cool) right, newest at center. Each row uses ITS OWN frozen
    # per-machine reference (a busy-bucket amount for that timescale); the tps needle
    # uses the span=1 per-second reference (a busy second's tokens).
    scales = _full_scales()
    tps_r, tps_w = scales[1]  # per-second full-scale for the live needle
    read_r = ema_rate(rows, cnow, IN, _TACH_HALFLIFE)  # responsiveness knob (secs)
    write_r = ema_rate(rows, cnow, OUT, _TACH_HALFLIFE)
    sec_gauge.append(
        grow(
            "tps",
            mirror_tach(read_r, write_r, tps_r, tps_w),
            ktok(round(read_r)),
            ktok(round(write_r)),
        )
    )

    # The CURRENT cumulative always comes from the fine store (updated every change —
    # fresh); only the OLD end of a window uses the throttled day store. This keeps the
    # windows nested correctly — a longer span can never total LESS than a shorter one,
    # which a lagging day-store "current" value used to break.
    cur_in, cur_out = _cum_at(rows, cnow, IN), _cum_at(rows, cnow, OUT)
    for lab, span in (("1m", 60), ("10m", 600), ("4h", 14400), ("1d", 86400)):
        src = day_rows if span > 3600 else rows
        i = max(0, cur_in - _cum_at(src, cnow - span, IN))
        o = max(0, cur_out - _cum_at(src, cnow - span, OUT))
        fr, fw = scales[span]
        sec_gauge.append(
            grow(lab, mirror_hist(src, cnow, span, fr, fw), ktok(i), ktok(o))
        )

    # ═══ Usage — rate-limit budgets as bars ═══════════════════
    sec_use = []
    if five is not None or week is not None:
        sec_use.append(hdr("Usage:", GREEN))

        def usage_bar(label, pct, reset):
            seg = f"{lbl(label)}{bar(pct)} {col(pct)}{int(round(pct))}%{R}"
            t = until(reset)
            if t:
                seg += f" {DIM}(resets {t}){R}"
            return seg

        if five is not None:
            sec_use.append(f"{IND}{usage_bar('5h', five, five_reset)}")
        if week is not None:
            sec_use.append(f"{IND}{usage_bar('7d', week, week_reset)}")

    # ── assemble by orientation ──
    # horizontal: Claude Code + Git on the left, Gauges + Usage on the right.
    # vertical (default): all four stacked, byte-identical to the original.
    if _layout() == "horizontal":
        lines = _join_columns(sec_cc + sec_git, sec_gauge + sec_use)
    else:
        lines = sec_cc + sec_git + sec_gauge + sec_use

    _emit("\n".join(lines))


def _cleanup_session(sid):
    """Remove a session's per-session state (its token stores + git cache in TMP) on
    SessionEnd, so a closed session leaves nothing behind (the hourly GC is the backstop
    for crashes where the hook never fires). Does NOT touch the machine-level
    token_rates.log — that's shared calibration memory that outlives any one session.
    The sid is VALIDATED to a safe charset and glob-escaped before use: a wildcard in
    sid must never let one session's cleanup wipe every session's files."""
    if not sid or not re.fullmatch(r"[A-Za-z0-9._-]{4,}", sid):
        return
    import glob  # SessionEnd-only path — not imported on a normal render

    esc = glob.escape(sid)
    targets = glob.glob(os.path.join(TMP, f"cc-statusline-*{esc}*"))
    targets += glob.glob(
        os.path.join(_here(), f"token_rates-{esc}.*")
    )  # legacy, if any
    for f in targets:
        try:
            os.remove(f)
        except Exception:
            pass


_USAGE = """revline — a rich status line for Claude Code

Four sections in the footer:
  Claude Code  model · thinking · effort · session · cost · context · cache/burn/api
  Git          repo (clickable) · branch · ahead/behind · PR · dirty · last-commit age
  Gauges       the read/write "mirror" — tokens READ (non-cache input, warm, left) vs
               WRITTEN (output, cool, right): a live tps needle + 1m / 10m / 4h / 1d

                 10m:   42k ▏▁▂▃▄▅▆▇█│█▇▆▅▄▃▂▁▕ 3k
                             read →  now  ← written

  Usage        5h / 7d rate-limit budgets as bars

Rendering (Claude Code runs this for you; it reads the session JSON on stdin):
  revline                 render the status line

Layout control (writes revline.config.json as H/V; next render picks it up ~1s):
  revline flip            toggle H <-> V
  revline H               horizontal — two columns (Claude Code + Git | Gauges + Usage)
  revline V               vertical — stacked (the default)
  revline help            show this
  (horizontal / vertical also accepted)

Session cleanup (wire to a SessionEnd hook so a closed session leaves nothing behind):
  revline cleanup <sid>   delete that session's calibration log + stores
  revline cleanup         same, reading session_id from the JSON piped on stdin

Orientation is also read from the CC_STATUSLINE_LAYOUT env var as a fallback."""


def _stdin_isatty():
    """isatty() guarded: sys.stdin is None when stdin is closed, and both call sites
    live OUTSIDE main()'s never-throw guard (which only wraps _render), so an
    unguarded call tracebacks — a SessionEnd hook firing with stdin closed does
    exactly that. Default True: no readable stdin means treat it like a terminal and
    show usage, never block on a read that can't complete."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return True


def main():
    # Pin stdin to UTF-8 before ANY read. Claude Code writes UTF-8 JSON, but stdin's
    # decoding is locale-derived on Windows just like stdout's was, so a non-ASCII
    # cwd, branch, or model name arrives mangled — and a mangled current_dir makes
    # `git -C <path>` fail, silently emptying the whole Git section.
    #
    # errors="replace", NOT the platform default: Windows defaults to surrogateescape,
    # which yields lone surrogates that are unencodable in EVERY codec — so one odd
    # input byte would blow past the ASCII ramp and hit the final replace-net,
    # flattening the ENTIRE footer. U+FFFD from "replace" encodes cleanly in UTF-8, so
    # the damage stays confined to the one mangled field.
    #
    # Wrapped, and placed before any read: sys.stdin is None when stdin is closed
    # (reconfigure would raise AttributeError), and a TextIOWrapper refuses to
    # reconfigure once read from. Failing here is exactly today's behaviour.
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # Control verbs run WITHOUT touching stdin (so `revline flip` never blocks).
    verb = sys.argv[1].strip().lower().lstrip("-") if len(sys.argv) > 1 else ""
    if verb == "flip":
        _emit(_flip(), honour_declared=True)
        return
    if verb in ("horizontal", "wide", "h"):
        _emit(_set_layout("horizontal"), honour_declared=True)
        return
    if verb in ("vertical", "v"):
        _emit(_set_layout("vertical"), honour_declared=True)
        return
    if verb == "cleanup":
        # `revline cleanup <sid>` (explicit sid) OR a SessionEnd hook (sid + reason
        # piped as JSON on stdin). An EXPLICIT argv sid is authoritative — we do NOT
        # fall through to stdin then (even if it's empty), or `cleanup ""` would block
        # forever on json.load. Only read stdin when NO argv sid was given.
        sid = sys.argv[2] if len(sys.argv) > 2 else None
        if sid is None and not _stdin_isatty():
            try:
                j = obj(json.load(sys.stdin))
                # SessionEnd also fires on /clear and resume, but those KEEP the session
                # alive — skip them so we don't wipe live calibration/history.
                if j.get("reason") in ("clear", "resume"):
                    return
                sid = j.get("session_id", "")
            except Exception:
                sid = ""
        _cleanup_session(sid or "")
        return
    if verb in ("help", "usage") or _stdin_isatty():
        # `revline help`, a bare `revline`, OR a mistyped verb in a terminal — any
        # tty invocation that isn't a known control verb. There's no piped JSON, so
        # show help instead of falling through to json.load(stdin), which would block
        # the terminal forever waiting on input that never comes.
        _emit(_USAGE, honour_declared=True)
        return

    # ── render path ──
    try:
        d = json.load(sys.stdin)
    except Exception:
        print("")
        return
    _gc()  # best-effort, throttled to hourly — prunes stale state files
    try:
        _render(d)
    except Exception:
        # A status line must never crash the footer: on any unexpected shape
        # (e.g. a wrong-typed field from a future Claude Code schema), print
        # nothing and exit 0 rather than leaking a traceback to a blank footer.
        print("")


if __name__ == "__main__":
    main()
