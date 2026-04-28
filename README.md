# ulanzi-studio-niri

A Linux daemon that drives the **Ulanzi Stream Controller D200X** and integrates
it with the [Niri](https://github.com/YaLTeR/niri) Wayland compositor.

Buttons can:

- Trigger Niri actions (`niri msg action ...`)
- Launch arbitrary commands
- Control media (`playerctl` / `wpctl`)
- Take screenshots (`grim` / `slurp`)
- Send keystrokes (`wtype` / `ydotool`)
- Switch between configured pages
- Adjust deck brightness

The wide bottom-right LCD displays a clock, system stats, or live encoder
information.

## Hardware

- 13 LCD buttons at 196×196
- 1 wide LCD button at 458×196 (bottom-right; driven by the small-window
  subsystem)
- 2 plain physical buttons
- 3 rotary encoders (each with click)

## Installation

This project uses [uv](https://github.com/astral-sh/uv) and runs against the
system Python interpreter.

```sh
# Development install
uv venv --python /usr/bin/python3 .venv
uv sync

# End-user install (creates ~/.local/bin/ulanzi-niri)
uv tool install .
```

### udev rule (required)

Out of the box the deck's `hidraw` nodes are owned by root. Install the udev
rule so the daemon can talk to it as your user:

```sh
ulanzi-niri install-udev
# follow the printed `sudo` commands; replug the deck afterwards
```

### Run as a service

```sh
mkdir -p ~/.config/systemd/user
cp packaging/ulanzi-niri.service ~/.config/systemd/user/
systemctl --user enable --now ulanzi-niri
```

## Configuration

Configuration lives at `~/.config/ulanzi-niri/config.toml`. See
[`examples/config.toml`](examples/config.toml).

## Development

```sh
uv run ulanzi-niri doctor       # diagnose environment
uv run ulanzi-niri push         # one-shot push of current config
uv run ulanzi-niri sniff        # observe HID traffic
uv run pytest                   # tests
uv run ruff check .             # lint
```

## Status

Wide-tile `mode = "encoders"` is **experimental**: the wire format is a
best-guess until verified against a packet capture from the official Ulanzi
software. It must be enabled with `experimental = true`.

## Credits

Protocol details adapted from [redphx/strmdck](https://github.com/redphx/strmdck).
