# ulanzi-studio-niri

[![PyPI version](https://img.shields.io/pypi/v/ulanzi-studio-niri)](https://pypi.org/project/ulanzi-studio-niri/)

A Linux daemon that drives the **Ulanzi Stream Controller D200X** and integrates
it with the [Niri](https://github.com/YaLTeR/niri) Wayland compositor.

Buttons can:

- Trigger Niri actions (`niri msg action ...`)
- Launch arbitrary commands
- Control media (`playerctl` / `wpctl`)
- Send keystrokes (`wtype` / `ydotool`)
- Switch between configured pages
- Adjust deck brightness
- Refresh usage widgets on demand
- Show coding agent session counts (waiting/running/done/idle) from agent-berth

The wide bottom-right LCD displays a clock (digital or dial, optionally with
date/weekday), system stats, or live encoder information.

LCD buttons can show remaining Claude, Codex, OpenCode Go, Kimi Code, and
xAI (Grok) plan usage, plus the Moonshot (Kimi API) account balance. The daemon reads credentials
maintained by `claude /login`, `codex login`, `/login` inside Kimi CLI, and
OpenCode `/connect`, then
fetches usage directly from each provider. Configure one `[[page.widget]]` per
provider/window; each renders on the button at its `pos`:

```toml
[[page.widget]]
pos = 1
provider = "claude"
account = ""                 # direct integration uses the active CLI account
limit = "five_hour"          # five_hour | seven_day | seven_day_fable (Claude)
label = "5H"
icon = "claude-desktop"
```

OpenCode Go provides `rolling`, `weekly`, and `monthly` windows:

```toml
[[page.widget]]
pos = 2
provider = "opencode-go"
limit = "rolling"           # rolling | weekly | monthly
label = "GO 5H"
```

Its API key is read from the `opencode-go` entry in
`$XDG_DATA_HOME/opencode/auth.json` (normally
`~/.local/share/opencode/auth.json`). Set `OPENCODE_GO_API_KEY` to override it.

xAI (Grok) reports SuperGrok remaining weekly allowance from `grok login`
credentials in `~/.grok/auth.json` (override with `GROK_HOME` or
`ULANZI_GROK_CREDENTIALS`):

```toml
[[page.widget]]
pos = 4
provider = "xai"
limit = "weekly"           # weekly only
label = "7D"
icon = "xai"
```

Kimi for Coding reports the plan's 5-hour and monthly quota from
`https://api.kimi.com/coding/v1/usages` (override with `KIMI_CODE_BASE_URL`):

```toml
[[page.widget]]
pos = 5
provider = "kimi-code"
limit = "five_hour"        # five_hour | monthly
label = "5H"
icon = "moonshot"
```

Credentials are resolved in order: Kimi CLI OAuth tokens from `/login` inside
Kimi CLI (`~/.kimi/credentials/kimi-code.json`; `KIMI_SHARE_DIR` is respected,
set `ULANZI_KIMI_CODE_CREDENTIALS` to override the exact path), then pi's
`kimi-coding` OAuth entry in `~/.pi/agent/auth.json` (`PI_CODING_AGENT_DIR` is
respected, set `ULANZI_PI_AUTH` to override), then the `kimi-code-plan-cn` /
`kimi-code-plan-global` API keys saved by OpenCode `/connect` (the global entry
targets api.kimi.ai instead). Near-expiry OAuth tokens are refreshed and the
rotated credentials are written back atomically; API keys cannot be refreshed,
so rotate them in OpenCode when they expire.

Moonshot's Kimi API is pay-as-you-go, so its widget shows the remaining
account balance instead of a percentage, colored green / yellow / red as it
drops below ¥70 / ¥36 (CNY) or $12 / $6 (USD):

```toml
[[page.widget]]
pos = 3
provider = "moonshot"
limit = "balance"           # balance only
label = "BAL"
icon = "moonshot"
```

The API key is read from `MOONSHOT_API_KEY` (international platform,
api.moonshot.ai, USD; set `MOONSHOT_BASE_URL` including `/v1` for the China
platform) or from the `moonshotai` / `moonshotai-cn` entries in OpenCode's
`auth.json`, which select the international / China (api.moonshot.cn, CNY)
platforms respectively.

Pressing a usage button requests a refresh and opens the provider's usage page in the browser.
Defaults: `https://claude.ai/new#settings/usage` (Claude),
`https://chatgpt.com/#settings/Usage` (Codex), `https://opencode.ai/go`
(OpenCode Go), `https://www.kimi.com/code/console` (Kimi Code),
`https://grok.com/?_s=usage` (xAI),
`https://platform.kimi.com/console/account` (Moonshot). Set
`url` on a widget to override:

```toml
[[page.widget]]
pos = 4
provider = "opencode-go"
limit = "rolling"
url = "https://opencode.ai/workspace/wrk_xxxxx/go"
```

Use `seven_day_fable` to show Claude's weekly Fable model allowance. Each
widget shows the remaining percentage and reset time. Provider data is fetched
concurrently in the background (never blocking the event loop) and updated
automatically when the fetch completes. Results are cached for 30 minutes to
limit provider API traffic. Claude, Codex, and Kimi Code access tokens near
expiry are
refreshed using the CLI's refresh token and the rotated credentials are written
back atomically. If a usage request returns HTTP 401, its access token is
refreshed and the request is retried once.
HTTP 429 responses are not retried automatically. Press any usage widget to
request a manual refresh. Manual refreshes are throttled to one fetch every
90 seconds.

The same cached data is available immediately from the running daemon:

```sh
ulanzi-niri ai-usage
```

Use `ulanzi-niri ai-usage --json` for machine-readable output. It returns a
`providers` object containing account limits and provider errors, or `null` if
no data is available. The command reads the daemon's in-memory cache without
contacting providers or waiting for an in-progress refresh. Restart the daemon
after upgrading to enable this command. `--timeout` controls how long to wait
for the daemon's reply (default: 2 seconds).
Use `ulanzi-niri ai-usage --refresh --json` to wait for a fresh result before
printing. This bypasses the cache and manual-refresh throttle, joins any fetch
already in progress, and defaults to a 30-second reply timeout. If the CLI
times out, the daemon continues refreshing its cache.
Both formats exit with status 0 when at least one provider
returns account data, 1 on timeout or when no provider returns account data,
including when the daemon is not running, and 2 for an invalid timeout.

Request a background refresh of all providers, regardless of the current page:

```sh
ulanzi-niri control refresh-ai-usage
```

This returns `refresh-requested` immediately. It shares the widget's 90-second
manual-refresh throttle and does not start another fetch while one is running.
Read the updated cache with `ulanzi-niri ai-usage --json` after the refresh
finishes; reading while it is running returns the previous result.
The raw control socket request is `refresh-ai-usage\n`, sent to
`$XDG_RUNTIME_DIR/ulanzi-niri.sock`; its reply is `OK refresh-requested\n`.
For a refresh followed by its result, send `ai-usage --refresh\n` instead.

## Agent status

LCD buttons can show how many coding agent sessions are in each state, as
tracked by [agent-berth](https://github.com/doitian/agent-berth). The daemon
runs `agent-berth stats --json`, so the executable must be on the daemon's
PATH; set `ULANZI_AGENT_BERTH` to its exact path otherwise. Run
`agent-berth setup` once to install the provider hooks — the widget only reads
its snapshots. Configure one `[[page.agent_status]]` per button:

```toml
[[page.agent_status]]
pos = 0
agent = "all"            # "all" sums every provider agent-berth reports
label = "AGENTS"

[[page.agent_status]]
pos = 1
agent = "claude"         # one provider per button
```

Each button shows the label at top left, the agent icon at top right, the most
urgent status as a large glyph next to its session count across the middle,
and a bar along the bottom with one segment per status. The center number is
the count of the highest-priority non-empty status (**waiting > running > done
> idle**) and takes that status's color: waiting is orange, running yellow,
done blue, and idle green. The bottom bar lights the segment of every status
that has sessions, in the same order and colors. An agent with no sessions
shows a green pause and `0`. `agent = "all"` also sums providers this daemon
has no icon for, so a new agent-berth provider is counted before the widget
knows its name.

All buttons share one reading, refreshed every two seconds while a page with
agent status widgets is visible; pressing a button requests an immediate
refresh (throttled to one read every 0.5 seconds) and opens the widget's
optional `url`. A failure keeps the last counts and renders them in gray with
a `stale` caption. When no reading has arrived yet, `n/a` / `NO CLI` means
`agent-berth` was not found, `Err` / `AGENT-BERTH` means it failed or returned
an unusable payload, and `TO` / `AGENT-BERTH` means it did not answer within
five seconds.

The same cached counts are available from the running daemon:

```sh
ulanzi-niri agent-status
ulanzi-niri agent-status --refresh --json
ulanzi-niri control refresh-agent-status
```

These mirror the `ai-usage` commands: the plain form prints the cached counts
without waiting, `--refresh` waits for a fresh reading (default 15-second
reply timeout), `--json` prints the `providers` object, and the control
command returns `refresh-requested` immediately. The raw socket requests are
`agent-status\n` (or `agent-status --refresh\n`) and `refresh-agent-status\n`.

## Hardware

- 13 LCD buttons at 196×196
- 1 wide LCD button at 458×196 (bottom-right; driven by the small-window
  subsystem)
- 2 plain physical buttons
- 3 rotary encoders (each with click, free rotate, and press-rotate)

Each `[[page.encoder]]` can bind `on_press`, `on_rotate_cw` / `on_rotate_ccw`,
and `on_press_rotate_cw` / `on_press_rotate_ccw`. Click fires on release and is
cancelled if the knob turned while held. Unset press-rotate keys fall through
to the free-rotate actions. An explicit `{ type = "noop" }` press-rotate
binding silences that direction instead.

## Installation

Install into a virtual environment with pip, then run setup as your desktop user:

```sh
python3 -m venv ~/.local/share/ulanzi-niri-venv
~/.local/share/ulanzi-niri-venv/bin/pip install ulanzi-studio-niri
~/.local/share/ulanzi-niri-venv/bin/ulanzi-niri setup
```

Alternatively, use `uv tool install ulanzi-studio-niri` and `ulanzi-niri setup`.
No repository checkout is needed. The package includes the example config,
udev rule, and systemd service template. Icons are not bundled.

Setup creates the example config if missing and installs, enables, and starts
the systemd user service. It checks the installed udev rule and asks you to run
`sudo ulanzi-niri admin-setup` if the rule is missing or outdated. Setup itself
never runs privileged commands. Existing
config files (including symlinks) are preserved. The service uses the Python
environment that ran setup and the selected XDG config path. Run setup again
after moving or replacing that environment; it updates and restarts the service.
Install or update the udev rule separately:

```sh
sudo ulanzi-niri admin-setup
```

If sudo cannot find an installation in your user PATH, use its absolute path,
for example `sudo ~/.local/share/ulanzi-niri-venv/bin/ulanzi-niri admin-setup`.
`admin-setup` requires root and only installs and reloads the udev rule; it does
not change user config or services. Replug the deck if device access is not
available immediately. The udev rule
grants the active local desktop session access through `uaccess`.

Each setup component can be skipped independently:

```sh
ulanzi-niri setup --no-service    # config only
ulanzi-niri setup --no-config     # use your existing config
ulanzi-niri setup --no-start      # install service without enabling/starting it
```

Options can be combined. `--no-service` also skips enabling and starting.
On systems without systemd or `systemctl`, setup prints a warning and skips
the service step successfully. Start the daemon manually with `ulanzi-niri run`.
`--no-start` leaves an already running/enabled service in that state.
Configuration and the user service respect `$XDG_CONFIG_HOME` (default
`~/.config`). Setup reports errors with a nonzero exit status and can be rerun.
The legacy `install-udev` command remains available to print manual commands.

The example uses desktop tools such as niri, kitty, firefox, playerctl, wpctl,
and wtype; install the tools used by your chosen actions separately. Use
`ulanzi-niri doctor` to check your environment.

## Configuration

Configuration lives at `~/.config/ulanzi-niri/config.toml`. See the bundled
[`config.toml`](src/ulanzi_niri/data/config.toml).

### Pages and layers

Each `[[page]]` belongs to a `layer` (default `"default"`). A `page` action
with `cycle = 1` / `cycle = -1` moves to the next/previous page *within the
current layer*, wrapping around. A `goto` (or `toggle`) that targets a page in
another layer switches layers — use this for "folder" buttons that open a
multi-page group (e.g. a `"web"` layer with several pages).

The wide tile shows a 12px-high page indicator near its bottom edge, with 12px
of padding on each side.
The current page's segment is `#9974F8` on a `#707070` track, in configuration
order within the current layer. Layers with only one page have no indicator.

### Icons

Icon names in `[[page.button]]` are resolved in this order, first match
wins:

1. `~/.config/ulanzi-niri/icons/<name>` — your own overrides
2. `~/.local/share/icons/`, `/usr/share/icons/`, `/usr/share/pixmaps/` —
   freedesktop icon directories, searched recursively

A name with an extension (`firefox.png`) matches that filename anywhere
under the search roots. A bare name (`firefox`) matches `firefox.png` or
`firefox.xpm`, preferring the largest available pixel size (parsed from
`NxN` directory components). SVG icons are supported and preferred over raster matches in system themes.

## Keyboard control

With the daemon running, niri (or any keybind) can turn pages without
touching the deck. Success prints the landed page name.

```sh
ulanzi-niri control next-page
ulanzi-niri control prev-page
ulanzi-niri control goto apps
ulanzi-niri control back
```

niri spawn examples (`~/.config/niri/config.kdl`):

```kdl
binds {
    Mod+Shift+Page_Down { spawn-sh "ulanzi-niri control next-page"; }
    Mod+Shift+Page_Up { spawn-sh "ulanzi-niri control prev-page"; }
    Mod+Shift+A { spawn-sh "ulanzi-niri control goto apps"; }
    Mod+Shift+BackSpace { spawn-sh "ulanzi-niri control back"; }
}
```

If the daemon is not running the command exits 1 with `driver not running`.

## Development

```sh
uv sync
./bin/install --no-start         # optional local tool install + setup
uv run ulanzi-niri doctor       # diagnose environment
uv run ulanzi-niri push         # one-shot push of current config
uv run ulanzi-niri sniff        # observe HID traffic
uv run pytest                   # tests
uv run ruff check .             # lint
```

## Releases

### Updating the changelog

[git-changelog](https://github.com/pawamoy/git-changelog) generates
[CHANGELOG.md](CHANGELOG.md) using the configuration in `pyproject.toml` and
`config/changelog.md.jinja`. Run from the repository root:

```sh
uv run python scripts/update_changelog.py
```

The command reads the package version from `pyproject.toml`:

- When it matches the latest release tag, changes accumulate under `Unreleased`.
- When it differs, changes go under that upcoming version, labeled `Unreleased`
  until tagged. After tagging, the next run uses the release commit's date.
- The `1.0.0` and `2.0.0` sections stay empty; only later changes are listed.

Use Conventional Commit subjects: `feat: ...` for Added, `fix: ...` for Fixed,
`docs: ...`, `perf: ...`, `refactor: ...`, `deps: ...`, or `revert: ...` for
Changed. Breaking changes (`!` or `BREAKING CHANGE:`) are marked. Routine
build, chore, CI, style, test, and merge commits are omitted. Write subjects
for readers and review generated notes before release.

The command regenerates the file from committed history, so direct edits to
entries are overwritten. Fetch full history and tags if needed
(`git fetch --tags`, or `git fetch --unshallow --tags` in a shallow clone).
Commit the changes to include before running the command; an uncommitted
package version bump is supported.

### Publishing

Pushing a stable semantic version tag such as `v2.0.0` runs
[the release workflow](.github/workflows/publish.yml). Tags must be exactly
`vMAJOR.MINOR.PATCH`, with no leading zeroes. Prerelease and build-metadata
tags are not published by this workflow.

The first job validates the tag and requires its version (without `v`) to
exactly match `[project].version` in `pyproject.toml`. An invalid tag or version
mismatch fails the release before tests, builds, or publishing. After tests,
lint, and type checks pass, the workflow builds the source distribution and
wheel, checks the installed wheel outside the repository, publishes those
artifacts to PyPI, and creates a GitHub Release with the distribution files.

Before the first release, configure a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
for `ulanzi-studio-niri` using these exact values:

| Setting | Value |
| --- | --- |
| Owner | `doitian` |
| Repository | `ulanzi-studio-niri` |
| Workflow filename | `publish.yml` |
| Environment | `pypi` |

If the PyPI project does not exist yet, register a
[pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
with the same values and project name. Configure the repository's GitHub
Actions environment named `pypi` to allow release tags. Leave required reviewers
disabled if releases should publish without manual approval. The publish job
uses GitHub OIDC with `id-token: write`; no PyPI API token or password secret
is needed.

To release, commit the changes to include, update `pyproject.toml`, run
`uv lock`, and run `uv run python scripts/update_changelog.py`. Review the changelog,
commit it with the version change, then tag and push that commit. Use a
`chore: release VERSION` commit subject to avoid adding a release bookkeeping
entry. For example, when the
committed version is `2.0.0`:

```sh
git tag -a v2.0.0 -m "Release 2.0.0"
git push origin v2.0.0
```

## Status

All physical inputs are wired: the 13 LCD buttons, the wide tile (pos 13),
the two plain hardware buttons (pos 14, 15), and all three rotary encoders
(press, rotate, and press-rotate). Streaming for the 4th-row buttons and encoders requires a
one-time `ENABLE_INPUT_STREAMING` (cmd 0x0002) packet on connect, which the
daemon sends automatically; without it the firmware silently consumes
encoder rotates (routing them to its built-in brightness handler). Opcode
discovered empirically by sweep.

## Credits

Protocol details adapted from [redphx/strmdck](https://github.com/redphx/strmdck).
