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
# All artifacts use "revtest-*" ids and are removed at the end.
# ═══════════════════════════════════════════════════════════════
import glob
import json
import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
TMP = tempfile.gettempdir()
PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}")


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
        ["python3", "revline.py"],
        input=payload,
        text=True,
        capture_output=True,
        cwd=HERE,
    )


def session_end(sid, reason="other"):
    """Simulate the SessionEnd hook: pipe {session_id, reason} to `revline cleanup`."""
    subprocess.run(
        ["python3", "revline.py", "cleanup"],
        input=json.dumps({"session_id": sid, "reason": reason}),
        text=True,
        capture_output=True,
        cwd=HERE,
    )


def files_for(sid):
    return glob.glob(os.path.join(TMP, f"cc-statusline-*{sid}*")) + glob.glob(
        os.path.join(HERE, f"token_rates-{sid}.*")
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
    check("store + log created on activity", bool(files_for(sid)) and row is not None)
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
        "each session has its OWN calibration log",
        os.path.exists(os.path.join(HERE, f"token_rates-{a}.log"))
        and os.path.exists(os.path.join(HERE, f"token_rates-{b}.log")),
    )
    session_end(a, "other")
    check("closing A removed A's files", not files_for(a))
    check("B's files untouched by A's close", bool(files_for(b)))
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
            ["python3", "revline.py"],
            input=payload,
            text=True,
            capture_output=True,
            cwd=HERE,
        )
        check(f"{label}: exit 0, no stderr", r.returncode == 0 and not r.stderr)
    wipe("revtest-x")


if __name__ == "__main__":
    print("═══ revline end-to-end tests ═══")
    test_lifecycle_and_noncache()
    test_two_concurrent_sessions_isolated()
    test_cleanup_reasons()
    test_store_versioning()
    test_window_nesting()
    test_never_crashes()
    print(f"\n═══ {len(PASS)} passed, {len(FAIL)} failed ═══")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        raise SystemExit(1)
    print("all green ✓")
