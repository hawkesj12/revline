#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
# test_revline.py — end-to-end lifecycle + accounting tests
# ═══════════════════════════════════════════════════════════════
# Drives revline.py exactly the way Claude Code does — piping session JSON on stdin
# for renders, and `revline cleanup` for SessionEnd — so it exercises the REAL paths
# without opening or closing an actual Claude Code session. Run: python3 test_revline.py
#
# Covers: per-session isolation, non-cache reading, the accounting (read once/turn,
# write sawtooth), store versioning, per-session calibration, window nesting, the
# cleanup lifecycle (delete on exit, keep on /clear + resume), and never-crash.
#
# Plus the cross-platform group [8a/8b/8c/9/10], which exists because the working tree
# is production on TWO operating systems and Windows cannot be checked from macOS:
#   [8a] the render path under a FAITHFUL Windows simulation — PYTHONIOENCODING UNSET,
#        encoding derived from the locale. Setting that variable instead exercises the
#        OPPOSITE branch of _emit, which is how the real Windows path went untested.
#   [8b] the ASCII ramp is a faithful translation, asserted end-to-end against a real
#        render. EXPECTED_RAMP is duplicated here ON PURPOSE — importing the shipped
#        table would make the comparison self-referential and vacuous.
#   [8c] control verbs honour a declared encoding; a non-ASCII payload survives stdin.
#   [9]  no implicit locale-dependent encoding anywhere (PEP 597 EncodingWarning).
#   [10] temp-dir resolution and atomic writes behave on Windows too.
#
# The standard here is INFORMATION SURVIVAL, not "it didn't crash": an encoding failure
# is swallowed by main()'s never-throw guard and shows up as a blank footer at exit 0,
# so every crash-only assertion passes while the feature is 100% dead.
#
# All artifacts use "revtest-*" ids and are removed at the end.
# ═══════════════════════════════════════════════════════════════
import atexit
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
TMP = tempfile.gettempdir()

# The harness prints ═, ✓ and ✗, which cp1252 cannot encode — so on a Windows runner
# this file died on its own banner before running a single check, with the same
# UnicodeEncodeError it exists to catch. (Confirmed on real Windows, not simulated:
# the first CI run failed here on both 3.10 and 3.13 while Linux passed.)
#
# This does NOT weaken what is under test. Every assertion below reads the raw BYTES a
# subprocess wrote, with that subprocess's own encoding pinned explicitly via env — so
# the harness's stdout is independent of the thing being measured.
#
# The harness's own subprocess.run(text=True) calls carry encoding="utf-8" for the same
# reason: text mode decodes with the LOCALE encoding, so under LC_ALL=C they choked on
# revline's UTF-8 output. Third instance of the identical bug class, and the one the
# PEP 597 detector in [9] cannot see — it scans revline.py, not this file.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Redirect revline's script-relative state (the SHARED per-machine token_rates.log /
# .scale) to a throwaway dir for the whole test run, via REVLINE_STATE_DIR. Set in the
# process env BEFORE any render, so every revline subprocess inherits it — the tests
# never read, write, or clobber the live calibration log the real statusline shares.
# Works identically under `pytest` and `python3 test_revline.py` (no __main__-only
# guard, which was the old isolation's bug). Per-session stores stay in TMP under
# unique revtest-* ids, so they need no redirect. STATE_DIR is removed at the end.
# mkdtemp, NOT a fixed path: a shared name means two concurrent runs — two developers,
# the pre-commit hook firing while someone runs the suite, two CI matrix legs on one
# machine — delete each other's state mid-flight. The loser dies on a FileNotFoundError,
# which is not an AssertionError, so the runner below never catches it and the suite
# aborts with no summary at all.
STATE_DIR = tempfile.mkdtemp(prefix="revtest-state-")
os.environ["REVLINE_STATE_DIR"] = STATE_DIR
# atexit fires under BOTH `pytest` and `python3 test_revline.py`, so the throwaway
# state dir is always removed regardless of entry point.
atexit.register(lambda: shutil.rmtree(STATE_DIR, ignore_errors=True))

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")
    # Assert, so `pytest` actually FAILS on a bad check. The old check() only appended
    # to a list, so pytest stayed green even when internal checks failed (a blind gate).
    assert cond, name


def render(sid, total_in, fresh_in, cache_creation, out, cwd="/tmp"):
    """One render (~one 1s tick), the way Claude Code invokes it."""
    payload = json.dumps(
        {
            "session_id": sid,
            "workspace": {"current_dir": cwd},
            "context_window": {
                "total_input_tokens": total_in,
                "total_output_tokens": out,
                "context_window_size": 1_000_000,
                "current_usage": {
                    "input_tokens": fresh_in,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": max(
                        0, total_in - fresh_in - cache_creation
                    ),
                },
            },
        }
    )
    return subprocess.run(
        [sys.executable, "revline.py"],
        input=payload,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=HERE,
    )


def session_end(sid, reason="other"):
    """Simulate the SessionEnd hook: pipe {session_id, reason} to `revline cleanup`."""
    subprocess.run(
        [sys.executable, "revline.py", "cleanup"],
        input=json.dumps({"session_id": sid, "reason": reason}),
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=HERE,
    )


def files_for(sid):
    return glob.glob(os.path.join(TMP, f"cc-statusline-*{sid}*")) + glob.glob(
        os.path.join(STATE_DIR, f"token_rates-{sid}.*")
    )


def store_row(sid):
    """Last (t, in_cum, out_cum, in_raw, out_raw) row of a session's fine store."""
    p = os.path.join(TMP, f"cc-statusline-io-v2-{sid}")
    if not os.path.exists(p):
        return None
    rows = [tuple(map(int, ln.split(","))) for ln in open(p).read().splitlines()]
    return rows[-1] if rows else None


def wipe(*sids):
    for sid in sids:
        for f in files_for(sid):
            try:
                os.remove(f)
            except OSError:
                pass


# ── the tests ────────────────────────────────────────────────
def test_lifecycle_and_noncache():
    print("\n[1] open a session → run a turn → close it (the lifecycle you described)")
    sid = "revtest-life"
    wipe(sid)
    # open + one turn: context is 500k (mostly cache), but only 1500 fresh + 300 cached
    render(sid, 500_000, 1500, 300, 40)
    render(sid, 500_000, 1500, 300, 900)  # same turn streaming output
    row = store_row(sid)
    check("store created on activity", bool(files_for(sid)) and row is not None)
    check(
        "reading counts NON-CACHE (1500+300=1800), not the 500k total",
        row is not None and row[1] == 1800,
    )
    check(
        "writing accumulated (900, the streamed output)",
        row is not None and row[2] == 900,
    )
    session_end(sid, "other")
    check("SessionEnd cleanup removed ALL of the session's files", not files_for(sid))
    wipe(sid)


def test_two_concurrent_sessions_isolated():
    print(
        "\n[2] two sessions at once → isolated logs/stores → close one, other survives"
    )
    a, b = "revtest-A", "revtest-B"
    wipe(a, b)
    for i in range(3):  # interleave THREE DISTINCT turns from each (reads must differ
        render(a, 300_000, 2000 + i * 1000, 0, 100 + i)  # per turn, else counted once)
        render(b, 800_000, 500 + i * 100, 0, 50 + i)
    ra, rb = store_row(a), store_row(b)
    check("each session has its own store", ra is not None and rb is not None)
    check(
        "reading accumulates independently (A=2000+3000+4000=9000, B=500+600+700=1800)",
        ra is not None and rb is not None and ra[1] == 9000 and rb[1] == 1800,
    )
    check(
        "one shared machine-level calibration log (not per-session)",
        os.path.exists(os.path.join(STATE_DIR, "token_rates.log")),
    )
    session_end(a, "other")
    check("closing A removed A's files", not files_for(a))
    check("B's files untouched by A's close", bool(files_for(b)))
    check(
        "cleanup did NOT delete the shared machine log",
        os.path.exists(os.path.join(STATE_DIR, "token_rates.log")),
    )
    wipe(a, b)


def test_cleanup_reasons():
    print("\n[3] SessionEnd reasons: /clear + resume KEEP files, real exit DELETES")
    for reason, should_keep in (("clear", True), ("resume", True), ("other", False)):
        sid = f"revtest-{reason}"
        wipe(sid)
        render(sid, 200_000, 1000, 0, 50)
        session_end(sid, reason)
        kept = bool(files_for(sid))
        check(
            f"reason={reason}: files {'kept (session lives on)' if should_keep else 'deleted'}",
            kept == should_keep,
        )
        wipe(sid)


def test_store_versioning():
    print("\n[4] a stale OLD-version store is abandoned, not inherited")
    sid = "revtest-ver"
    wipe(sid)
    old = os.path.join(TMP, f"cc-statusline-io-{sid}")  # v1 name, huge inflated base
    open(old, "w").write("1784100000,65000000,400000,2337,468\n")
    render(sid, 500_000, 1500, 500, 900)
    row = store_row(sid)  # reads the v2 store
    check(
        "new v2 store starts fresh (2000 non-cache), ignores the 65M v1 base",
        row is not None and row[1] == 2000,
    )
    try:
        os.remove(old)
    except OSError:
        pass
    wipe(sid)


def test_window_nesting():
    print("\n[5] window totals nest: 1d ≥ 4h ≥ 10m ≥ 1m (never inverted)")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rl", os.path.join(HERE, "revline.py")
    )
    rl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rl)
    now = 100_000
    # fine store: fresh (has a recent burst); day store: throttled, LAGS by 2 min
    fine = [(now - 600, 5000, 0, 0, 0), (now - 5, 13000, 0, 4000, 0)]
    day = [(now - 600, 5000, 0, 0, 0), (now - 125, 9000, 0, 4000, 0)]  # stale current
    cur = rl._cum_at(fine, now, rl.IN)
    totals = []
    for span, src in ((60, fine), (600, fine), (14400, day), (86400, day)):
        totals.append(max(0, cur - rl._cum_at(src, now - span, rl.IN)))
    check(
        f"nested non-decreasing {totals} (fresh current from fine store)",
        all(totals[i] <= totals[i + 1] for i in range(len(totals) - 1)),
    )


def test_never_crashes():
    print("\n[6] never crashes the footer on bad input")
    for label, payload in (
        ("empty", ""),
        ("garbage", "not json {{{"),
        (
            "wrong-typed tokens",
            '{"session_id":"revtest-x","context_window":{"total_input_tokens":"oops"}}',
        ),
        (
            "null current_usage",
            '{"session_id":"revtest-x","context_window":{"current_usage":null}}',
        ),
    ):
        r = subprocess.run(
            [sys.executable, "revline.py"],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=HERE,
        )
        check(f"{label}: exit 0, no stderr", r.returncode == 0 and not r.stderr)
    wipe("revtest-x")


def test_cleanup_cannot_over_delete():
    print("\n[7] cleanup rejects wildcard/empty sids (can't wipe other sessions)")
    victim = "revtest-victim"
    for bad in ("*", "?", "", "a"):  # metachar / empty / too-short must be no-ops
        wipe(victim)
        render(victim, 200_000, 1500, 0, 50)
        before = bool(files_for(victim))
        subprocess.run(
            [sys.executable, "revline.py", "cleanup", bad],
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=HERE,
        )
        kept = bool(files_for(victim))
        check(
            f"cleanup {bad!r} left an unrelated session's files intact", before and kept
        )
    wipe(victim)
    render(victim, 200_000, 1500, 0, 50)
    subprocess.run(
        [sys.executable, "revline.py", "cleanup", victim],
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=HERE,
    )
    check("cleanup <valid-sid> still removes that session", not files_for(victim))
    wipe(victim)


# ── cross-platform / encoding ────────────────────────────────
# These exist because the working tree is production on TWO operating systems, and
# Windows is the one that can't be checked here. The authoritative statement is in the
# CPython `sys` docs, not PEP 686: "On Windows, UTF-8 is used for the console device.
# Non-character devices such as disk files and pipes use the system locale encoding"
# — historically cp1252. That console/pipe split is the whole bug: an interactive
# Windows console is fine (PEP 528 routes it through WriteConsoleW), but Claude Code
# reads the status line through a PIPE, which is not a console. PEP 686 is the FIX
# date, not the evidence — it makes UTF-8 mode the default in Python 3.15.
#
# Why "it didn't crash" is not enough here: an encoding failure inside _render is
# caught by main()'s never-throw guard, which prints "" — so the footer goes BLANK,
# silently, at exit 0. Every crash-only assertion passes while the feature is 100%
# dead. So these assert on INFORMATION SURVIVAL, not on the absence of a traceback.
# The ASCII ramp, duplicated ON PURPOSE. This is the independent oracle for test [8b]:
# importing _ASCII_FALLBACK from the module under test would make the assertion
# self-referential and VACUOUS — verified, a mutant that flattens every bar height to a
# single character passes when the table is imported and fails when it is duplicated.
# The duplication IS the test. If you change the table in revline.py, change it here too
# and the diff will show you exactly what you changed.
EXPECTED_RAMP = str.maketrans(
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
        "·": "-",
        "↑": "^",
        "↓": "v",
        "✓": "+",
        "●": "o",
        "✎": "~",
        "◌": ".",
        "→": "->",
        "←": "<-",
        "≤": "<=",
        "⠀": " ",
    }
)

PAYLOAD = {
    "session_id": "revtest-enc",
    "model": {"display_name": "Opus 5"},
    "context_window": {"total_input_tokens": 42000, "total_output_tokens": 3100},
    "cost": {"total_cost_usd": 1.23},
}


def run_revline(args=(), payload=None, env_extra=None, drop=()):
    """Spawn revline the way Claude Code does, returning raw BYTES (decoding is what's
    under test). `drop` removes env vars — needed to simulate Windows honestly."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(env_extra or {})
    # ensure_ascii=False is load-bearing: json.dumps() escapes non-ASCII to \\uXXXX by
    # default, so a "non-ASCII payload" would arrive as pure ASCII bytes and never
    # exercise the stdin decoder at all — the test would pass with the fix removed.
    # Claude Code is a Node app and JSON.stringify does NOT escape, so raw UTF-8 on
    # the wire is what production actually sends. (Caught by mutation testing.)
    body = (
        b""
        if payload is None
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    return subprocess.run(
        [sys.executable, "revline.py", *args],
        input=body,
        capture_output=True,
        cwd=HERE,
        env=env,
    )


def test_render_on_the_real_windows_path():
    print("\n[8a] the render path under a FAITHFUL Windows simulation")
    # This is the branch production actually takes, and nothing tested it before.
    # PYTHONIOENCODING is UNSET on real Windows; the encoding is DERIVED from the
    # locale. Setting PYTHONIOENCODING instead (as the first version of this test did)
    # exercises the opposite branch of _emit — the ASCII ramp — so the reconfigure path
    # that every Windows user actually hits could be deleted with the suite still green.
    #
    # PYTHONUTF8=0 defeats UTF-8 mode (and keeps this meaningful after Python 3.15's
    # PEP 686 makes it the default); LC_ALL/LANG=C drives the locale to ASCII.
    env = {"PYTHONUTF8": "0", "LC_ALL": "C", "LANG": "C"}
    r = run_revline(payload=PAYLOAD, env_extra=env, drop=("PYTHONIOENCODING",))
    check("windows-sim: exit 0, nothing on stderr", r.returncode == 0 and not r.stderr)
    check("windows-sim: footer is not blank", len(r.stdout.strip()) > 0)
    # The point of reconfigure: real UTF-8 goes out, so the chart keeps its real glyphs
    # rather than settling for the ramp.
    out = r.stdout.decode("utf-8")  # raises if the bytes are not valid UTF-8
    check("windows-sim: emitted bytes are valid UTF-8", True)
    check(
        "windows-sim: real block glyphs survived (not the ramp)",
        "█" in out or "▁" in out,
    )
    check("windows-sim: model + cost survived", "Opus 5" in out and "1.23" in out)
    wipe("revtest-enc")


def test_ascii_ramp_is_a_faithful_translation():
    print("\n[8b] the ASCII ramp preserves information (metamorphic, vs a real render)")
    # The oracle problem: you cannot say whether ONE render of 1,200 ANSI characters
    # over live git state is "correct". But you CAN assert a RELATION between two
    # renders of the same payload — translating the UTF-8 one through the expected
    # table must reproduce the ASCII one, byte for byte.
    #
    # This subsumes any "does it still chart" spot-check: it pins EVERY glyph at once,
    # is immune to layout changes (both sides move together), and turns a forgotten
    # table entry into an automatic failure instead of a bug someone has to notice.
    # The old check ('[' in out) was nearly vacuous — every ANSI escape contains '['.
    utf8 = run_revline(["help"], env_extra={"PYTHONIOENCODING": "utf-8"})
    ascii_ = run_revline(["help"], env_extra={"PYTHONIOENCODING": "ascii"})
    check("both renders succeeded", utf8.returncode == 0 and ascii_.returncode == 0)
    translated = utf8.stdout.decode("utf-8").translate(EXPECTED_RAMP)
    got = ascii_.stdout.decode("ascii")
    check(
        "every glyph maps exactly as documented (no '?' fallthrough)", translated == got
    )
    check("nothing reached the replace-net", "?" not in got)

    # The comparison above runs on `help`, which carries no BAR glyphs — and since the
    # render path upgrades to UTF-8 rather than ramping, no end-to-end output contains
    # a translated bar. So the eight heights need a direct property assertion or they
    # are unverified: mutation testing confirmed that flattening all eight to a single
    # character leaves the byte-comparison above completely green.
    #
    # The property that matters is not the exact characters, it is that magnitude
    # SURVIVES — eight distinct heights must stay eight distinguishable marks, in
    # increasing visual weight. A ramp that maps everything to "#" still "charts" and
    # tells you nothing.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rl", os.path.join(HERE, "revline.py")
    )
    rl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rl)
    marks = [rl._ASCII_FALLBACK.get(ord(g)) for g in rl.SPARK]
    check(
        f"all 8 bar heights are mapped (got {marks})", all(m is not None for m in marks)
    )
    check(
        "the 8 heights stay 8 DISTINCT marks (magnitude survives)", len(set(marks)) == 8
    )
    check(
        "the documented ramp and the shipped table agree",
        marks == [EXPECTED_RAMP.get(ord(g)) for g in rl.SPARK],
    )


def test_render_survives_hostile_encoding():
    print("\n[8c] control verbs honour a DECLARED encoding; renders keep their info")
    # The consumer differs, so the policy differs. A control verb prints wherever the
    # USER redirected it, so a declared PYTHONIOENCODING is authoritative there and the
    # ramp is the right answer. The render path has exactly one consumer (Claude Code,
    # over a pipe) which takes UTF-8, so it upgrades instead — covered by [8a].
    for enc in ("cp1252", "ascii", "latin-1"):
        r = run_revline(["help"], env_extra={"PYTHONIOENCODING": enc})
        out = r.stdout.decode(
            enc
        )  # raises if a byte is invalid in the declared encoding
        check(f"{enc}: exit 0, valid {enc} bytes out", r.returncode == 0 and out)
        check(f"{enc}: no glyph lost to the replace-net", "?" not in out)

    # A non-ASCII payload must survive the STDIN decode. Before stdin was pinned to
    # UTF-8 this rendered a blank footer under a strict handler and mojibake under
    # Windows' surrogateescape — and a mangled current_dir silently empties the whole
    # Git section, because `git -C <mangled>` just fails.
    cjk = dict(
        PAYLOAD,
        model={"display_name": "Opus 5 café"},
        workspace={"current_dir": "/tmp/丁"},
    )
    for label, env, drop in (
        ("declared ascii", {"PYTHONIOENCODING": "ascii"}, ()),
        ("windows-sim", {"PYTHONUTF8": "0", "LC_ALL": "C"}, ("PYTHONIOENCODING",)),
    ):
        r = run_revline(payload=cjk, env_extra=env, drop=drop)
        check(
            f"non-ASCII payload, {label}: exit 0, not blank",
            r.returncode == 0 and len(r.stdout.strip()) > 0,
        )
        check(
            f"non-ASCII payload, {label}: the accented name survived",
            b"caf" in r.stdout,
        )

    # An UNKNOWN verb falls through to json.load(sys.stdin); with stdin inherited and
    # neither a tty nor closed it blocks FOREVER. input=b"" in run_revline is what stops
    # the suite hanging under any wrapper that hands down an open pipe.
    r = run_revline(["nonsense-verb"], env_extra={"PYTHONIOENCODING": "ascii"})
    check("an unknown verb returns instead of blocking on stdin", r.returncode == 0)
    wipe("revtest-enc")


def test_no_implicit_locale_encoding():
    print("\n[9] no implicit locale-dependent encoding anywhere (PEP 597 detector)")
    # PEP 597's EncodingWarning fires on every open() that omits encoding= and every
    # subprocess text-mode decode — so zero warnings proves every FILE AND SUBPROCESS
    # open in this file is locale-independent. That is a detector for a whole bug
    # class rather than a test for the instances someone thought to check, and it
    # found the subprocess.run(text=True) site that a hand-audit of open() calls
    # missed entirely.
    #
    # What it does NOT cover, because the mechanism cannot: sys.stdin and sys.stdout
    # are handed to the process ALREADY BUILT, so no open() ever happens for them and
    # no warning can fire. Their encoding is pinned explicitly instead — see _emit()
    # and the stdin decode in main(). A green run here says nothing about either.
    #
    # Must run in `always` mode, NOT `-W error`: EncodingWarning subclasses Warning
    # subclasses Exception, so as an error it gets swallowed by revline's own broad
    # `except Exception:` never-throw handlers and the gate silently passes. Warnings
    # go to stderr, which no handler can intercept.
    payload = {
        "session_id": "revtest-warn",
        "model": {"display_name": "Opus 5"},
        "workspace": {"current_dir": HERE},
        "context_window": {"total_input_tokens": 1000, "total_output_tokens": 100},
    }
    # A warning only fires on a line that actually RUNS, so the detector is exactly as
    # good as the paths this exercises — that is its one real limitation. Seed every
    # optional state file first, or their readers never open and their encoding bugs
    # stay invisible. (Caught by mutation-testing this very check: without the config
    # file below, deleting encoding= from _config()'s open() went undetected.)
    with open(
        os.path.join(STATE_DIR, "revline.config.json"), "w", encoding="utf-8"
    ) as fh:
        json.dump({"layout": "vertical"}, fh)
    # Over _RATE_LOG_TRIM_AT (800 KB) so the append ALSO takes the trim-and-rewrite
    # branch — otherwise that read stays unexecuted and its encoding goes unchecked.
    with open(os.path.join(STATE_DIR, "token_rates.log"), "w", encoding="utf-8") as fh:
        fh.write(
            "".join(f"{1700000000 + i},{i * 100},{i * 10}\n" for i in range(40000))
        )
    with open(
        os.path.join(STATE_DIR, "token_rates.scale"), "w", encoding="utf-8"
    ) as fh:
        json.dump({"1m": [1000, 100]}, fh)

    # Clear this session's git cache FIRST. _git() caches for 12s, so on a warm cache
    # the subprocess never runs and its encoding goes unchecked while the check still
    # prints "found: none" — absence of a warning reading identically to absence of
    # execution. Running the suite twice inside 12s (exactly what a failed commit then
    # a retry does) used to silently drop that site's coverage.
    for f in glob.glob(os.path.join(TMP, "cc-statusline-revtest-warn*")):
        try:
            os.remove(f)
        except OSError:
            pass

    seen = set()
    # Token counts must GROW between renders. _log_rate() early-returns when there is
    # no activity, so a repeated identical payload never reaches the rate-log append —
    # another path that silently escaped the detector until mutation testing showed it.
    for i in range(3):
        payload["context_window"] = {
            "total_input_tokens": 1000 * (i + 1),
            "total_output_tokens": 100 * (i + 1),
        }
        r = subprocess.run(
            [
                sys.executable,
                "-X",
                "warn_default_encoding",
                "-W",
                "always::EncodingWarning",
                "revline.py",
            ],
            input=json.dumps(payload),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=HERE,
        )
        for ln in r.stderr.splitlines():
            if "EncodingWarning" in ln:
                m = re.search(r"revline\.py:(\d+):", ln)
                seen.add(m.group(1) if m else ln.strip())
    check(f"zero implicit-encoding sites (found: {sorted(seen) or 'none'})", not seen)
    wipe("revtest-warn")


def test_crossplatform_state_paths():
    print("\n[10] temp-dir resolution + atomic write behave on Windows too")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rl", os.path.join(HERE, "revline.py")
    )
    rl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rl)

    # os.replace over an EXISTING destination is the Windows-fragile case: unlike POSIX
    # it can raise PermissionError if the target is open elsewhere. _atomic_write must
    # swallow that (never-throw) AND leave no .tmp litter behind either way.
    p = os.path.join(STATE_DIR, "atomic-probe")
    rl._atomic_write(p, "first")
    rl._atomic_write(p, "second")  # the overwrite path
    with open(p, encoding="utf-8") as fh:
        check("atomic write overwrites an existing file", fh.read() == "second")
    check("no .tmp litter left behind", not glob.glob(p + ".tmp.*"))

    # Non-ASCII must round-trip regardless of the machine's locale — git branch and
    # repo names reach the cache, and a Windows box would otherwise mangle them.
    rl._atomic_write(p, "café ✓ naïve")
    with open(p, encoding="utf-8") as fh:
        check(
            "non-ASCII round-trips through the state file", fh.read() == "café ✓ naïve"
        )
    os.remove(p)

    # TMP resolution must work off TEMP/TMP alone — TMPDIR is not set on Windows.
    env = {k: v for k, v in os.environ.items() if k not in ("TMPDIR", "TMP", "TEMP")}
    env["TEMP"] = STATE_DIR
    r = subprocess.run(
        [sys.executable, "-c", "import revline; print(revline.TMP)"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=HERE,
        env=env,
    )
    check("TMP resolves from TEMP when TMPDIR is absent", r.stdout.strip() == STATE_DIR)


if __name__ == "__main__":
    # Isolation is handled by REVLINE_STATE_DIR (set at import); cleanup by atexit — so
    # the real calibration log is never touched and there's nothing to back up/restore.
    # Run every test even if one fails (check() now raises, so catch per-test) to print
    # a full summary.
    print("═══ revline end-to-end tests ═══")
    for _t in (
        test_lifecycle_and_noncache,
        test_two_concurrent_sessions_isolated,
        test_cleanup_reasons,
        test_store_versioning,
        test_window_nesting,
        test_never_crashes,
        test_cleanup_cannot_over_delete,
        test_render_on_the_real_windows_path,
        test_ascii_ramp_is_a_faithful_translation,
        test_render_survives_hostile_encoding,
        test_no_implicit_locale_encoding,
        test_crossplatform_state_paths,
    ):
        try:
            _t()
        except AssertionError:
            pass  # already recorded in FAIL + printed by check()
        except Exception as e:
            # A non-assertion crash (FileNotFoundError, a hang killed, a bad env) used
            # to abort the whole run with NO summary, so a green tally was the only
            # signal anyone ever saw. Record it as a failure and keep going.
            FAIL.append(f"{_t.__name__} crashed: {type(e).__name__}: {e}")
            print(f"  ✗ FAIL  {_t.__name__} crashed: {type(e).__name__}: {e}")
    print(f"\n═══ {len(PASS)} passed, {len(FAIL)} failed ═══")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        raise SystemExit(1)
    print("all green ✓")
