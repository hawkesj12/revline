# revline

A rich, single-file status line for [Claude Code](https://claude.com/claude-code) — your model, cost, git state, and a live **token-throughput tachometer**, rendered right in the terminal footer. Zero dependencies, one Python file, drops in with three lines of config.

<!-- Replace with a real screenshot of your footer: -->
<!-- ![revline](docs/screenshot.png) -->

```
Claude Code:
  model: Sonnet 4.5 🧠
  effort: low medium [high] xhigh max
  session name: #demo
  cost: $0.42   time: 15m
  context: ████░░░░░░ 38% : 76k / 200k
  api: 33% of wall
Git:
  repo: revline   branch: main ✓synced
  clean   last commit: 7m ago
Gauges:
  tps: ██████████░░░░░░░░░░░░░░░░░░░░░░░░░░ 1.2k/s
   1m: ▁▂▃▅▇█▆▄▂▁▁▁ 607 TPM
  10m: ▂▃▄▅▆▇█▇▅▄▃▂ 29k TP10M
   1h: ▁▁▂▃▄▅▆▇█▇▆▅ 76k TPH
   1d: ▂▄▆█▇▅▃▂▁▁▁▁ 210k TPD
Usage:
  5h: ██░░░░░░░░ 22%
  7d: ████░░░░░░ 41%
```

_(The `tps` bar fills left→right and shifts green → amber → red as throughput climbs — a tachometer for how hard the model is working. In a real terminal it's in color.)_

## Why

Claude Code's built-in footer is a single line. When you're deep in a long session you often want more at a glance: how full is the context window, what has this session cost, what's my git state, and — the fun part — how much token throughput is moving right now. `revline` puts all of that in a compact, color-coded dashboard, and it's a single self-contained script you can read top to bottom and trust.

## Features

- **Four sections, color-grouped by system** — Claude Code (model · effort · session · cost · context · cache/burn), Git (repo · branch · ahead/behind · PR · dirty count · last-commit age), Gauges (throughput), Usage (rate-limit budgets).
- **A throughput tachometer** — an LED bar that fills with tokens/sec (log-scaled) and is colored by position: green at idle, up through amber, into red at full tilt.
- **Multi-timescale sparklines** — 1m / 10m / 1h / 1d token-throughput mini-charts with totals.
- **Two layouts, toggled live** — `vertical` (stacked) or `horizontal` (two aligned columns), switched with `revline flip` without restarting.
- **Clickable git links** — repo and PR are OSC-8 hyperlinks in terminals that support them.
- **Never crashes your footer** — any malformed input prints nothing and exits cleanly, rather than leaking a traceback.
- **Zero dependencies** — Python 3 standard library only. No `pip install`.

## Requirements

- **Python 3** (standard library only — no third-party packages).
- A terminal with 256-color / truecolor support (most modern terminals: iTerm2, WezTerm, VS Code, the JetBrains terminal, Windows Terminal). OSC-8 links and the emoji glyph are optional niceties that degrade gracefully where unsupported.

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

### Optional: the `revline` command

To control the layout from the terminal, put the script on your `PATH`:

```sh
ln -s ~/.claude/revline.py ~/.local/bin/revline   # any dir on your PATH
```

## Layout

`revline` renders in one of two orientations:

- **vertical** _(default)_ — all four sections stacked.
- **horizontal** — Claude Code + Git on the left, Gauges + Usage on the right, in two aligned columns. Good for a wide, short footer.

```
Claude Code:                              Gauges:
  model: Sonnet 4.5 🧠                        tps: ██████████░░░░░░░░░░░░░░░░ 1.2k/s
  effort: low medium [high] xhigh max          1m: ▁▂▃▅▇█▆▄▂▁▁▁ 607 TPM
  session name: #demo                         10m: ▂▃▄▅▆▇█▇▅▄▃▂ 29k TP10M
  cost: $0.42   time: 15m                      1h: ▁▁▂▃▄▅▆▇█▇▆▅ 76k TPH
  context: ████░░░░░░ 38% : 76k / 200k         1d: ▂▄▆█▇▅▃▂▁▁▁▁ 210k TPD
  api: 33% of wall                          Usage:
Git:                                          5h: ██░░░░░░░░ 22%
  repo: revline   branch: main ✓synced        7d: ████░░░░░░ 41%
  clean   last commit: 7m ago
```

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

## What the gauges measure (read this before trusting the numbers)

The `tps` tachometer and the `1m/10m/1h/1d` charts track how fast the **context window grows** over time — a **throughput** gauge, not a billing figure. Most of those tokens are prompt-cache reads (billed at a fraction of fresh input), so a big "TPH" means _a lot of context moved through the model this hour_, which correlates with activity but is **not** your dollar cost. For spend, watch the `cost: $…` line (Claude Code's own estimate) and the `cache:` hit-ratio. Tokens ≈ activity, `$` ≈ cost.

## Platform notes

Developed and used on macOS. It uses only portable standard-library calls (`tempfile.gettempdir()` for its small state files, no POSIX-only APIs), so it should run on Linux and Windows as well — but Windows hasn't been verified yet. Reports welcome.

## Contributing

Issues and PRs welcome — open an issue first to talk it through. It's one file by design; the goal is to keep it self-contained and dependency-free.

## License

[MIT](LICENSE) © 2026 Justin Hawkes
