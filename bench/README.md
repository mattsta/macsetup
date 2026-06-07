# Pager latency benchmark

`ttfp.exp` measures **time-to-first-page** for a `git log` pager configuration:
how long until the first screen is painted and the pager is parked waiting for
input — the latency a human actually feels.

## Why a PTY

git only spawns its configured pager when stdout is a TTY, and `less` only
paginates (and thus applies backpressure) on a TTY. Measuring through a plain
pipe with a continuously-draining reader removes backpressure and makes every
configuration look identically fast — a measurement trap. `ttfp.exp` uses
`expect`, which provides a real pty, so git behaves exactly as it does
interactively.

## Usage

```sh
expect bench/ttfp.exp <repo> '<pager.log value>'
```

It prints `RESULT first_paint_ms=<N> parked_ms=<N>`. `first_paint_ms` is the
honest signal (parked_ms includes a fixed settle window).

## Reference numbers (ghostty, ~16k commits)

| pager.log                   | first paint |
| --------------------------- | ----------- |
| direct delta / less         | ~25 ms      |
| compiled git-log-pager      | ~55 ms      |
| old shell temp-file wrapper | ~3700 ms    |

The old shell wrapper did `cat > "$tmp"` before choosing a pager, forcing git to
generate the entire `git log -p` history (175 MB) up front. The compiled
`git-log-pager` peeks only a bounded prefix and streams the rest, so it matches
a direct pager. See `macsetup/defaults/templates/git-log-pager.c`.
