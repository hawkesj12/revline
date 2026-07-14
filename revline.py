#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# statusline.py — a rich, multi-section Claude Code status line
# ═══════════════════════════════════════════════════════════════
# A vertical, color-coded dashboard rendered in the Claude Code footer. Four
# sections, color-grouped by which system each line belongs to:
#   Claude Code:  model · effort · 🧠thinking · style · session · cost · context · cache/burn/api
#   Git:          repo (clickable) · branch · ↑↓ upstream · PR · staged/mod/new · last-commit age
#   Gauges:       a tps throttle-tachometer (gradient LED fill) + token-consumption sparklines at 1m/10m/1h/1d
#   Usage:        5h / 7d rate-limit budgets as bars (the Max-plan runway)
#
# Design notes:
#   • Color encodes MEANING: brand colors group by system (Claude coral, GitHub
#     green); the gauge/usage percentages use a green→yellow→red HEALTH scale.
#   • Bars use the █/░ convention; charts are Unicode sparklines (▁▂▃▄▅▆▇█).
#   • Repo + PR are OSC 8 hyperlinks — Cmd-click to open on GitHub.
#   • Git is cached per session (3s TTL) so a big dirty repo never lags renders.
#
# ── What the token gauges MEASURE (read this before trusting the numbers) ──
#   The tps speedometer and the 1m/10m/1h/1d charts + TPM/TP10M/TPH/TPD totals
#   track how fast the CONTEXT WINDOW grows over time (the delta of
#   `total_input_tokens`). That is a THROUGHPUT gauge, NOT a billing figure:
#   most of those tokens are prompt-CACHE READS (billed at ~0.1×), not the
#   expensive fresh input / output tokens. So a big "TPH" means "a lot of context
#   moved through the model this hour," which correlates with activity but is
#   NOT your dollar cost. For actual spend, watch `cost: $…` (Claude Code's own
#   client-side estimate) and the `cache:` hit-ratio. Tokens ≈ activity, $ ≈ cost.
#
# Runs locally, reads Claude Code's status JSON on stdin, prints text.
# It costs ZERO API tokens — the status line is never sent to the model.
# ═══════════════════════════════════════════════════════════════
import glob
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata

# Per-session state files live here. tempfile.gettempdir() (not a hardcoded
# /tmp) so the git cache + token gauges also work on Windows. Basenames stay
# "cc-statusline-*" so _gc's glob still matches existing files.
TMP = tempfile.gettempdir()

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


SPARK = "▁▂▃▄▅▆▇█"


def spark(vals):
    """Render a 0–100 series as a Unicode sparkline (a mini line graph)."""
    return "".join(
        SPARK[round(max(0, min(100, v)) / 100 * (len(SPARK) - 1))] for v in vals
    )


def _atomic_write(path, text):
    """Write via a temp file + os.replace, so a concurrent reader always sees a
    complete old or complete new file — never a torn line. The status line fires
    on both the 1s timer AND after a message, so same-session writes can overlap."""
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _read_store(path):
    """Parse a token store, skipping any single malformed (e.g. torn) line
    rather than dropping the entire history on one bad row."""
    rows = []
    try:
        if os.path.exists(path):
            for ln in open(path).read().splitlines():
                try:
                    t, c, tot = ln.split(",")
                    rows.append((int(t), int(c), int(tot)))
                except Exception:
                    continue
    except Exception:
        rows = []
    return rows


def _write_store(path, rows):
    _atomic_write(path, "\n".join(f"{t},{c},{tot}" for t, c, tot in rows))


def _gc():
    """Best-effort, once per hour: delete cc-statusline-* state files older than
    a day so /tmp doesn't accumulate one set per session forever."""
    marker = os.path.join(TMP, "cc-statusline-gc")
    try:
        now = time.time()
        if os.path.exists(marker) and now - os.path.getmtime(marker) < 3600:
            return
        _atomic_write(marker, str(int(now)))
        for f in glob.glob(os.path.join(TMP, "cc-statusline-*")):
            try:
                if now - os.path.getmtime(f) > 86400:
                    os.remove(f)
            except Exception:
                pass
    except Exception:
        pass


def record_tokens(sid, cur_total):
    """Update the token stores and return (fine_rows, day_rows, now). Cumulative
    counts only growth (compaction drops ignored). Two stores: a fine one (1 h,
    written every call) drives tps/1m/10m/1h; a throttled coarse one (25 h, ≥120 s
    between writes) drives the 1-day gauge without unbounded row growth at 1 s refresh."""
    now = int(time.time())
    fpath = os.path.join(TMP, f"cc-statusline-tok-{sid}")
    dpath = os.path.join(TMP, f"cc-statusline-day-{sid}")
    fine = _read_store(fpath)
    prev_cum = fine[-1][1] if fine else 0
    prev_total = fine[-1][2] if fine else None
    # Token counts only move when a message completes, so at 1s refresh ~99% of
    # renders are unchanged — only append + rewrite when cur_total actually moved.
    # This kills the per-second file churn AND keeps the store to change-points
    # only (~100 rows/hr, not 3600), so reads and bucketing stay cheap.
    # NOTE: this read-modify-write is not locked. The render fires on both the 1s
    # timer and after a message, so two instances can overlap; _atomic_write keeps
    # the file itself intact (never a torn read), but the later os.replace can drop
    # the other's appended row. Effect is a rare (~1%) undercount of a throughput
    # gauge — never corruption. A lock would be POSIX-only (fcntl) and break the
    # Windows portability above, so we accept the cosmetic gap rather than add one.
    changed = prev_total is None or cur_total != prev_total
    if changed:
        cum = prev_cum + max(
            0, cur_total - (prev_total if prev_total is not None else cur_total)
        )
        fine.append((now, cum, cur_total))
        fine = [r for r in fine if r[0] >= now - 3660]
        _write_store(fpath, fine)
    day = _read_store(dpath)
    if changed and (not day or now - day[-1][0] >= 120):
        day.append((now, fine[-1][1], cur_total))
        day = [r for r in day if r[0] >= now - 90000]
        _write_store(dpath, day)
    return fine, day, now


def _cum_at(rows, ts):
    """Cumulative tokens at a point in time (carry-forward last known)."""
    c = 0
    for t, cc, _ in rows:
        if t <= ts:
            c = cc
        else:
            break
    return c


def bucket_series(rows, now, span_s, buckets=12):
    """Log-normalized 0–100 per-bucket consumption over the last span_s seconds;
    the busiest bucket is 100 (full █). [] when nothing was consumed. One forward
    pass over the time-sorted rows — O(rows + buckets), not O(rows × buckets)."""
    if not rows:
        return []
    slot = span_s / buckets
    edges = [now - (buckets - i) * slot for i in range(buckets + 1)]
    cums, j, c = [], 0, 0
    for e in edges:  # carry-forward cumulative at each bucket edge, single walk
        while j < len(rows) and rows[j][0] <= e:
            c = rows[j][1]
            j += 1
        cums.append(c)
    deltas = [max(0, cums[i + 1] - cums[i]) for i in range(buckets)]
    mx = max(deltas) if deltas else 0
    if mx <= 0:
        return []
    return [round(100 * math.log1p(d) / math.log1p(mx)) for d in deltas]


def tps_windowed(rows, now, window=15):
    """Tokens/sec over a trailing `window` seconds. Because it's a sliding
    window, a burst decays back toward 0 as it ages out — so with a fast refresh
    the speedometer needle glides instead of snapping (token data only changes
    per message, so a raw instantaneous rate would just sit still then jump)."""
    if not rows:
        return 0.0
    return max(0, _cum_at(rows, now) - _cum_at(rows, now - window)) / window


# position gradient (green low → yellow/amber mid → red flat-out), left→right
_RAMP = [46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196]


def speedo(tps, width=36, maxtps=10000):
    """An 80s-tachometer LED bar: fills left→right with tokens/sec (log-scaled so
    both slow and fast read well), each lit LED colored by its POSITION — green at
    the low end, up through yellow/amber, into red as throughput climbs toward
    flat-out. The fill edge rises while we're pushing tokens and drains back as it
    quiets, like a throttle. Unlit LEDs are ░. maxtps tops the scale — context
    throughput runs to a few k/s, so 10k rarely pegs."""
    dark = "\033[38;5;236m"
    n = len(_RAMP)
    frac = math.log1p(max(0.0, tps)) / math.log1p(maxtps)
    frac = max(0.0, min(1.0, frac))
    lit = round(frac * width)
    cells = []
    for i in range(width):
        if i < lit:
            c = _RAMP[min(n - 1, int(i / max(1, width - 1) * n))]
            cells.append(f"\033[38;5;{c}m█")
        else:
            cells.append(f"{dark}░")
    return "".join(cells) + R


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
            vals = open(cache).read().split("\t")
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

    def run(*args):
        try:
            r = subprocess.run(
                ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2
            )
            return r.stdout.strip() if r.returncode == 0 else ""
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
    """The directory holding this script — resolving symlinks, so the config
    file is found next to the REAL file even when run via a PATH symlink."""
    return os.path.dirname(os.path.realpath(__file__))


def _config_path():
    return os.path.join(_here(), "revline.config.json")


def _config():
    """Optional per-user settings from revline.config.json beside this script.
    Missing or malformed → {} (config is always optional; never crash on it)."""
    try:
        with open(_config_path()) as fh:
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
    then count East-Asian-wide / emoji glyphs (e.g. 🧠) as 2 cells and combining
    marks as 0. This is what lets the horizontal layout align two columns."""
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


def _render(d):
    # ── fields (all defensive: many are null early / absent / wrong-typed) ──
    model = obj(d.get("model")).get("display_name") or "?"
    effort = obj(d.get("effort")).get("level")
    thinking = obj(d.get("thinking")).get("enabled")
    style = obj(d.get("output_style")).get("name")
    cwd = obj(d.get("workspace")).get("current_dir") or d.get("cwd") or ""
    sid = d.get("session_id") or "nosession"
    name = d.get("session_name")  # set via /rename or --name; absent when unnamed

    ctx = obj(d.get("context_window"))
    used = ctx.get("used_percentage")
    tokens = ctx.get("total_input_tokens") or 0
    size = ctx.get("context_window_size")
    size_lbl = "1M" if size and size >= 1_000_000 else ("200k" if size else "")

    cost = obj(d.get("cost"))
    usd = cost.get("total_cost_usd") or 0
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

    def glabel(t):  # right-aligned gauge row label (tps / 1m / 10m / 1h)
        return f"{LABEL}{t.rjust(3)}:{R} "

    DASH = f"{DIM}{'─' * 12}{R}"  # placeholder when a chart has no data yet

    upct = 0 if used is None else int(used)
    cur_tok = (ctx.get("total_input_tokens") or 0) + (
        ctx.get("total_output_tokens") or 0
    )
    # Update the token store ONCE up front; both the burn stat (below) and the
    # Gauges section derive from it — no separate burn-rate file.
    rows, day_rows, cnow = record_tokens(sid, cur_tok)
    cum_tok = rows[-1][1] if rows else 0

    # Each section is its own block of lines, so the layout can stack them
    # (vertical) or place them in two columns (horizontal). Building them
    # separately changes nothing about their content — only how they're assembled.

    # ═══ Claude Code ═══
    sec_cc = [hdr("Claude Code:", ORANGE)]

    lm = f"{IND}{lbl('model')}{ORANGE}{BOLD}{model}{R}"
    if thinking:
        lm += " 🧠"
    if style:
        lm += f"   {lbl('style')}{ORANGE}{style}{R}"
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
    cu = obj(ctx.get("current_usage"))
    cr = cu.get("cache_read_input_tokens")
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

    # ═══ Gauges — multi-timescale token-consumption charts ════
    # rows / day_rows / cnow come from the single record_tokens call up top.
    sec_gauge = [hdr("Gauges:", EFF)]

    # tps: an LED throttle-tachometer — gradient fill (green low → amber → red)
    # grows with tokens/sec (trailing-window rate so it drains back as work quiets)
    tw = tps_windowed(rows, cnow)
    sec_gauge.append(f"{IND}{glabel('tps')}{speedo(tw)} {DIM}{ktok(round(tw))}/s{R}")

    # each window: self-normalized sparkline (shape) + total consumed (magnitude)
    def window_total(src, span_s):
        return max(0, _cum_at(src, cnow) - _cum_at(src, cnow - span_s))

    for lab, span, unit in (
        ("1m", 60, "TPM"),
        ("10m", 600, "TP10M"),
        ("1h", 3600, "TPH"),
    ):
        s = bucket_series(rows, cnow, span, 12)
        chart = f"{EFF}{spark(s)}{R}" if s else DASH
        sec_gauge.append(
            f"{IND}{glabel(lab)}{chart} {DIM}{ktok(window_total(rows, span))} {unit}{R}"
        )

    sd = bucket_series(day_rows, cnow, 86400, 12)
    cd = f"{EFF}{spark(sd)}{R}" if sd else DASH
    sec_gauge.append(
        f"{IND}{glabel('1d')}{cd} {DIM}{ktok(window_total(day_rows, 86400))} TPD{R}"
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

    print("\n".join(lines))


_USAGE = """revline — a rich status line for Claude Code

Rendering (Claude Code runs this for you; it reads the session JSON on stdin):
  revline                 render the status line

Layout control (writes revline.config.json as H/V; next render picks it up ~1s):
  revline flip            toggle H <-> V
  revline H               horizontal — two columns (Claude Code + Git | Gauges + Usage)
  revline V               vertical — stacked (the default)
  revline help            show this
  (horizontal / vertical also accepted)

Orientation is also read from the CC_STATUSLINE_LAYOUT env var as a fallback."""


def main():
    # Control verbs run WITHOUT touching stdin (so `revline flip` never blocks).
    verb = sys.argv[1].strip().lower().lstrip("-") if len(sys.argv) > 1 else ""
    if verb == "flip":
        print(_flip())
        return
    if verb in ("horizontal", "wide", "h"):
        print(_set_layout("horizontal"))
        return
    if verb in ("vertical", "v"):
        print(_set_layout("vertical"))
        return
    if verb in ("help", "usage") or (not verb and sys.stdin.isatty()):
        # bare `revline` in a terminal (no piped JSON) → show help, don't hang
        print(_USAGE)
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
