# Handoff

Maintainer's notes — the operational picture. What `README.md` and the comments at
the top of `revline.py` don't cover: how this repo is wired, what state it's in,
what will bite you, and what's unfinished.

`README.md` is for users. The header comment in `revline.py` explains the *design*
(the mirror, the calibration, the colour semantics) and is the best thing to read
before changing rendering. This file is for whoever maintains it.

---

## Read this first: your working tree is production

`~/.claude/revline.py` is a **symlink** to `revline.py` in this repo.

```
~/.claude/revline.py → <this repo>/revline.py
```

Claude Code invokes that path on every render. So an unsaved-then-saved edit is
live in your own footer immediately — there is no staging step, no install, no
restart. That's excellent for iteration and it means:

- **A syntax error breaks your status line right now**, in every open session. It
  fails quietly (the footer just goes blank or shows an error line), so you may not
  notice which edit did it.
- Anything you're mid-experiment on is what you're running. Use a branch if you're
  about to do something structural.
- Conversely, you cannot reproduce "the released version" by running your tree.
  There is no released version — see below.

Sanity-check before walking away from an edit:

```bash
echo '{}' | python3 revline.py            # should render, not traceback
python3 -m pytest test_revline.py -q      # 7 tests, ~1s
```

---

## State of the repo

| | |
| --- | --- |
| Shape | one file, `revline.py`, ~1,260 lines, 41 functions/classes |
| Dependencies | **none** — 8 stdlib imports, zero third-party (verified) |
| Tests | `test_revline.py`, 7 tests, all passing |
| CI | **the audit only** — nothing runs the tests. See Unfinished. |
| Versioning | none — no `__version__`, no git tags, no GitHub releases |
| Visibility | public, MIT |

Zero dependencies is a deliberate design property, not an accident: this runs on
every footer render, so an import that isn't in the stdlib is a startup cost paid
hundreds of times an hour. **Keep it at zero.** If you ever need a third-party
package, that's the moment to reconsider the feature.

---

## What runs automatically

**`.github/workflows/audit.yml`** — weekly, Mondays 09:00 ET. Calls the shared
audit in [`hawkesj12/workflows`](https://github.com/hawkesj12/workflows), which
runs four checks:

| Lane | Here |
| --- | --- |
| `zizmor` — workflow security | runs |
| `lychee` — dead links in `README.md` | runs |
| `osv-scanner` — CVEs | **skipped** (`deps: none`) — no dependencies to scan |
| OpenSSF Scorecard — repo posture | runs (public repo) |

A failure emails you. Nothing to remember to run.

**Two things to know about that:**

The CVE lane is declared off with `deps: none` rather than being left to pass
silently. That's the honest form: a skip reads as "not applicable", where a pass
would read as "no CVEs found". Delete that line the day revline grows a dependency.

**The schedule can switch itself off.** GitHub auto-disables scheduled workflows in
a *public* repo after 60 days with no repository activity — and revline is exactly
the profile that hits: finished-feeling, public, easy to leave alone for two months.
When it happens the audit stops with no announcement. If this repo has been quiet a
while, check the Actions tab before trusting a green history.

---

## Unfinished

**1. Nothing runs the tests.** This is the real gap. There are 7 passing tests and
no machine has ever executed them — no `ci.yml`, no branch protection, no required
check. They pass because you run them by hand, which means they gate nothing.

The audit is the *level-triggered* layer (it catches things that rot on their own).
There is no *edge-triggered* layer at all — nothing checks a change when you make
it. For a public repo that people install into their own Claude Code, that's the
piece worth adding next: a `ci.yml` running `ruff`, and `pytest` on the Python
versions you claim to support, plus a branch rule requiring it.

**2. No versioning.** No tags, no releases, no `__version__`. Users install by
copying a file, so there is no upgrade path and no way for someone to say which
revision they're on when reporting a bug. Not urgent while it's a single file people
symlink, but the first bug report from a stranger will want it.

**3. Test coverage is thin relative to the surface.** 7 tests against 41
functions/classes. The untested majority is mostly rendering, which is genuinely
awkward to assert on — but the *calibration* logic (`_full_scales`, the per-machine
reference learning) is pure computation over inputs and is both the most subtle code
in the file and the easiest to test properly.

---

## Things not to break

These are decisions with reasons that aren't obvious from the code alone. The full
rationale is in the header comment of `revline.py`; this is the short list of what
regressions to watch for.

- **Renders must never throw.** A status line that raises produces a broken footer
  in every open session. Failure should degrade to less information, never to an
  error.
- **Bars are frozen between refreshes on purpose.** The per-row references are
  learned per-machine and held stable so the timeline doesn't dance render to
  render — only the newest partial bucket fills live. If bars start jittering,
  something re-derived a reference mid-flight.
- **Git is cached per session (12s TTL)** so a large dirty repo can't lag renders.
  Any new git call needs to respect that or it will be felt on every keystroke.
- **Each timescale is calibrated to itself.** A 5-second bucket and a 2-hour bucket
  hold wildly different token counts; one shared scale can't serve both. Don't
  "simplify" them onto a common reference.

---

## Local checks

Nothing here is installed by the repo — these are the same tools the shared audit
runs, useful before you push:

```bash
python3 -m pytest test_revline.py -q      # the tests
ruff check .                              # lint
actionlint                                # workflow correctness (+ shellcheck)
zizmor .                                  # workflow security
lychee --no-progress README.md            # dead links
echo '{}' | python3 revline.py            # does it actually render
```
