# ulanzi-studio-niri

[![PyPI version](https://img.shields.io/pypi/v/ulanzi-studio-niri)](https://pypi.org/project/ulanzi-studio-niri/)

Linux daemon for the **Ulanzi Stream Controller D200X**, with
[Niri](https://github.com/YaLTeR/niri) integration.

Bind buttons and encoders to desktop actions, commands, media controls, keystrokes,
and pages. Display clocks, system stats, AI usage, and coding-agent session counts.

## Quick start

Requires Python 3.11+. Install with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install ulanzi-studio-niri
ulanzi-niri setup
sudo ~/.local/bin/ulanzi-niri admin-setup
```

`setup` creates the example config and starts a systemd user service, preserving
existing config. `admin-setup` installs USB permissions; adjust the executable
path if needed and replug the device afterward. Without systemd, run
`ulanzi-niri run` manually. See [installation details](docs/guide.md#installation)
for pip installation and setup options.

Edit `~/.config/ulanzi-niri/config.toml` using the
[example config](src/ulanzi_niri/data/config.toml). Install the external tools used
by your actions (such as `niri`, `playerctl`, or `wtype`); icons are not bundled.

```sh
ulanzi-niri doctor                 # check your environment
ulanzi-niri control next-page      # control the running daemon
ulanzi-niri ai-usage --json         # read cached usage
ulanzi-niri agent-status --json     # read cached agent counts
```

## Documentation

- [Configuration, pages, and icons](docs/guide.md#configuration)
- [AI usage widgets](docs/guide.md#ai-usage-widgets) · [Agent status](docs/guide.md#agent-status)
- [Keyboard control](docs/guide.md#keyboard-control)
- [D200X inputs](docs/guide.md#hardware)
- [Releases](docs/guide.md#releases) · [Changelog](CHANGELOG.md)

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

See the [development guide](docs/guide.md#development) for local installation and
hardware diagnostics.

Protocol details adapted from [redphx/strmdck](https://github.com/redphx/strmdck).
