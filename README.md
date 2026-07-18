# revline

A rich, single-file status line for [Claude Code](https://claude.com/claude-code) — your model, cost, git state, and a live **read/write "mirror"** that charts the tokens the model reads vs writes over time, rendered right in the terminal footer. Zero dependencies, one Python file, drops in with three lines of config.

![revline in horizontal layout](docs/horizontal.png)

_The **mirror** splits each row down the middle: tokens **read** (non-cache input, warm) fill the left half from the center, tokens **written** (output, cool) fill the right — newest at the center, five timescales stacked (`tps` → `1d`)._

## Why

Claude Code's built-in footer is a single line. When you're deep in a long session you often want more at a glance: how full is the context window, what has this session cost, what's my git state, and — the fun part — how much the model is reading and writing right now. `revline` puts all of that in a compact, color-coded dashboard, and it's a single self-contained script you can read top to bottom and trust.

## Features

- **Four sections, color-grouped by system** — Claude Code (model · thinking · effort · session · cost · context · cache/burn/api), Git (repo · branch · ahead/behind · PR · dirty count · last-commit age), Gauges (the read/write mirror), Usage (rate-limit budgets).
- **The read/write "mirror"** — a butterfly chart of tokens **read** (non-cache input, warm) vs **written** (output, cool) over time. Reading fills the left half from the center outward; writing fills the right. Newest is at the center, oldest at the outer edges, with the read and write totals flanking each row.
- **Five timescales** — `tps` is a live, EMA-smoothed tokens-per-second needle (how fast right now); `1m / 10m / 4h / 1d` are the timeline. Both bar height and hue rise with throughput.
- **Self-calibrating, per-timescale scale** — instead of a hardcoded peak, each row sets its own "full bar" from _your machine's_ real history: a robust high percentile of how many tokens a bucket of that row's width actually holds when it's active. A 5-second slice and a 2-hour slice hold wildly different amounts, so each timescale is scaled independently — a typical bucket lands mid-bar and only your busiest ones peg. The references are pooled across all your sessions into one small, self-pruning, gitignored `token_rates.log` (more history → a steadier scale) and **frozen** between refreshes, so a given amount always maps to the same height — no render-to-render strobing — and buckets are grid-anchored so a row steps one whole slice at a time as time passes.
- **Two layouts, toggled live** — `vertical` (stacked) or `horizontal` (two aligned columns), switched with `revline flip` without restarting.
- **Clickable git links** — repo and PR are OSC-8 hyperlinks in terminals that support them.
- **Never crashes your footer** — any malformed input prints nothing and exits cleanly, rather than leaking a traceback.
- **Zero dependencies** — Python 3 standard library only. No `pip install`.

## Requirements

- **Python 3** (standard library only — no third-party packages).
- A terminal with 256-color / truecolor support (most modern terminals: iTerm2, WezTerm, VS Code, the JetBrains terminal, Windows Terminal). OSC-8 links are an optional nicety that degrades gracefully where unsupported.

## Install

1. **Save the script** somewhere stable and make it executable:

   ```sh
   curl -o ~/.claude/revline.py https://raw.githubusercontent.com/hawkesj12/revline/main/revline.py
   chmod +x ~/.claude/revline.py
   ```

2. **Point Claude Code at it** in `~/.claude/settings.json`:

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "~/.claude/revline.py",
       "padding": 0,
       "refreshInterval": 1
     }
   }
   ```

3. **Start a session.** The footer picks it up — no restart of anything else needed.

That's the whole install. Claude Code runs the command each refresh and pipes it the session JSON on stdin; `revline` reads that and prints the dashboard. It never makes a network call and costs zero API tokens (the status line is never sent to the model).

### Optional: clean up session state on close

revline keeps a tiny per-session store and calibration log in your temp dir and next to the script. They're bounded and self-expire after a day idle, but if you want a session's files removed the moment it closes, add a `SessionEnd` hook in the same `settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/revline.py cleanup",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Claude Code pipes the session id to `revline cleanup`, which deletes that session's files. It intentionally skips `/clear` and `resume` (the session lives on). Purely optional — the built-in garbage collection is the backstop.

### Optional: the `revline` command

To control the layout from the terminal, put the script on your `PATH`:

```sh
ln -s ~/.claude/revline.py ~/.local/bin/revline   # any dir on your PATH
```

## Layout

`revline` renders in one of two orientations.

**vertical** _(default)_ — all four sections stacked:

![vertical layout](docs/vertical.png)

**horizontal** — Claude Code + Git on the left, Gauges + Usage on the right, in two aligned columns. Good for a wide, short footer (shown at the top of this README).

Switch layouts from the terminal (takes effect on the next render, ~1s — no restart):

```sh
revline flip     # toggle H <-> V
revline H        # horizontal
revline V        # vertical
revline help     # usage
```

## Configuration

Orientation is resolved in this order:

1. **`revline.config.json`** next to the script — written by the commands above, e.g. `{"layout": "H"}` (`H` or `V`).
2. **`CC_STATUSLINE_LAYOUT`** environment variable (`H` / `V` / `horizontal` / `vertical`) — a fallback if you'd rather set it declaratively.
3. **Default:** vertical.

## What the mirror measures (read this before trusting the numbers)

**"Read" here means non-cache input** — the tokens the model genuinely ingests fresh each turn (new/uncached input plus tokens newly written to cache), **not** the whole context re-read from cache. Claude Code's `total_input_tokens` is that full re-read, and it's mostly cache; charting it would make reading a churn number ~100× the write side. Instead revline sums `current_usage.input_tokens + cache_creation_input_tokens`, so the read side reflects **real new work** and ends up comparable to writing. Output is `total_output_tokens`. Both are per-response snapshots (not cumulative in the JSON since v2.1.132), accumulated locally and charted over the timescales.

This is a **throughput / activity** view, not a billing figure. For spend, watch the `cost: $…` line (Claude Code's own estimate) and the `cache:` hit-ratio; the `context:` line still shows the full context size (`total_input_tokens`). Tokens ≈ activity, `$` ≈ cost.

## Platform notes

Developed and used on macOS. It uses only portable standard-library calls (resolving the temp dir from the standard `TMPDIR` / `TEMP` / `TMP` environment variables for its small state files, no POSIX-only APIs), so it should run on Linux and Windows as well — but Windows hasn't been verified yet. Reports welcome.

## Contributing

Issues and PRs welcome — open an issue first to talk it through. It's one file by design; the goal is to keep it self-contained and dependency-free.

## License

[MIT](LICENSE) © 2026 Justin Hawkes
