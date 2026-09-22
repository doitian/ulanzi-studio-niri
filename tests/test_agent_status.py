import asyncio
import json
import stat
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image, ImageDraw

from ulanzi_niri import agent_status, service
from ulanzi_niri.agent_status import (
    AgentStatusFetcher,
    AgentStatusSnapshot,
    AgentSummary,
    parse_stats,
    read_stats,
    render_agent_widget,
    summarize,
)
from ulanzi_niri.config import (
    AgentStatusWidget,
    ButtonEntry,
    Config,
    PageConfig,
    UsageWidget,
)
from ulanzi_niri.protocol.device import DeckEvent, DeckEventKind

STATS = [
    {"provider": "claude", "waiting": 2, "running": 2, "done": 0, "idle": 3, "total": 7},
    {"provider": "codex", "waiting": 0, "running": 1, "done": 4, "idle": 0, "total": 5},
]
PROVIDERS = {
    "claude": {"waiting": 2, "running": 2, "done": 0, "idle": 3},
    "codex": {"waiting": 0, "running": 1, "done": 4, "idle": 0},
}


def test_parse_stats_merges_repeated_providers() -> None:
    assert parse_stats(STATS) == PROVIDERS
    assert parse_stats([]) == {}
    assert parse_stats(
        [
            {"provider": "Claude", "waiting": 1, "running": 0, "done": 0, "idle": 0},
            {"provider": "claude", "waiting": 2, "running": 0, "done": 0, "idle": 1},
        ]
    ) == {"claude": {"waiting": 3, "running": 0, "done": 0, "idle": 1}}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        "x",
        [None],
        [{"provider": "", "waiting": 0, "running": 0, "done": 0, "idle": 0}],
        [{"provider": "a b", "waiting": 0, "running": 0, "done": 0, "idle": 0}],
        [{"provider": "claude", "waiting": -1, "running": 0, "done": 0, "idle": 0}],
        [{"provider": "claude", "waiting": 1.5, "running": 0, "done": 0, "idle": 0}],
        [{"provider": "claude", "waiting": "1", "running": 0, "done": 0, "idle": 0}],
        [{"provider": "claude", "waiting": True, "running": 0, "done": 0, "idle": 0}],
        [{"provider": "claude", "running": 0, "done": 0, "idle": 0}],
    ],
)
def test_parse_stats_rejects_malformed_rows(payload) -> None:
    with pytest.raises(RuntimeError, match="invalid_response"):
        parse_stats(payload)


def _fake_agent_berth(tmp_path, body: str, *, exit_code: int = 0):
    script = tmp_path / "agent-berth"
    script.write_text(f"#!/bin/sh\nprintf '%s' '{body}'\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


async def test_read_stats_runs_agent_berth(monkeypatch, tmp_path) -> None:
    script = _fake_agent_berth(tmp_path, json.dumps(STATS))
    monkeypatch.setenv("ULANZI_AGENT_BERTH", str(script))
    assert await read_stats() == PROVIDERS


async def test_read_stats_missing_binary_reports_a_code(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ULANZI_AGENT_BERTH", str(tmp_path / "no-such-agent-berth"))
    with pytest.raises(RuntimeError, match="agent_berth_missing"):
        await read_stats()


async def test_read_stats_failure_and_garbage_report_codes(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ULANZI_AGENT_BERTH", str(_fake_agent_berth(tmp_path, "boom", exit_code=1)))
    with pytest.raises(RuntimeError, match="command_failed"):
        await read_stats()
    monkeypatch.setenv("ULANZI_AGENT_BERTH", str(_fake_agent_berth(tmp_path, "not json")))
    with pytest.raises(RuntimeError, match="invalid_response"):
        await read_stats()


async def test_read_stats_timeout_reports_a_code(monkeypatch, tmp_path) -> None:
    script = tmp_path / "agent-berth"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("ULANZI_AGENT_BERTH", str(script))
    monkeypatch.setattr(agent_status, "COMMAND_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(RuntimeError, match="timeout"):
        await read_stats()


async def test_fetcher_caches_shares_in_flight_and_throttles_force(monkeypatch) -> None:
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return dict(PROVIDERS)

    monkeypatch.setattr(agent_status, "fetch_agent_status", fetch)
    fetcher = AgentStatusFetcher(ttl=60)
    fetcher.refresh()
    fetcher.refresh()  # joins the in-flight read instead of spawning another
    await asyncio.gather(fetcher._task)
    assert calls == 1
    fetcher.refresh()  # within the TTL
    fetcher.refresh(force=True)  # forced reads are throttled too
    await asyncio.sleep(0)
    assert calls == 1
    assert fetcher.get().providers == PROVIDERS


async def test_fetcher_failure_keeps_last_counts(monkeypatch) -> None:
    results = [dict(PROVIDERS), RuntimeError("agent-berth stats: /home/me/secret-session")]
    calls = 0

    async def fetch():
        nonlocal calls
        result = results[min(calls, len(results) - 1)]
        calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(agent_status, "fetch_agent_status", fetch)
    fetcher = AgentStatusFetcher(ttl=60)
    await fetcher.refresh_and_wait()
    fetched_at = fetcher.get().fetched_at
    assert fetched_at is not None
    fetcher._attempted_at = None  # bypass the throttle for the next read
    await fetcher.refresh_and_wait()
    snapshot = fetcher.get()
    assert snapshot.error == "command_failed"  # no agent-berth output leaks
    assert snapshot.providers == PROVIDERS
    assert snapshot.fetched_at == fetched_at


async def test_fetcher_first_failure_has_no_counts(monkeypatch) -> None:
    async def fetch():
        raise RuntimeError("agent_berth_missing")

    monkeypatch.setattr(agent_status, "fetch_agent_status", fetch)
    fetcher = AgentStatusFetcher()
    await fetcher.refresh_and_wait()
    assert fetcher.get().error == "agent_berth_missing"
    assert fetcher.get().fetched_at is None
    assert fetcher.get().providers == {}


def test_summarize_picks_the_dominant_status() -> None:
    assert summarize(PROVIDERS, "all") == AgentSummary(
        "waiting", 2, {"waiting": 2, "running": 3, "done": 4, "idle": 3}
    )
    assert summarize(PROVIDERS, "codex") == AgentSummary(
        "running", 1, {"waiting": 0, "running": 1, "done": 4, "idle": 0}
    )
    # An agent with no sessions shows idle 0.
    assert summarize(PROVIDERS, "pi") == AgentSummary(
        "idle", 0, {"waiting": 0, "running": 0, "done": 0, "idle": 0}
    )
    assert summarize({"claude": {"done": 2}}, "all").status == "done"
    # Malformed rows are ignored instead of failing the render.
    assert summarize({"claude": None, "grok": {"idle": "x"}}, "all").count == 0


def _pixels(png: bytes) -> Image.Image:
    return Image.open(BytesIO(png)).convert("RGB")


def test_render_widget_dimensions() -> None:
    widget = AgentStatusWidget(pos=0)
    img = _pixels(render_agent_widget(widget, None))
    assert img.size == (196, 196)


def _bar_center(img: Image.Image, index: int) -> tuple[int, int, int]:
    x0, bar_y, seg_w = agent_status._bar_geometry(img.width)
    return img.getpixel((x0 + index * (seg_w + 6) + seg_w // 2, bar_y + 5))


def test_render_widget_lights_the_segments_that_have_sessions() -> None:
    snapshot = AgentStatusSnapshot(providers=PROVIDERS, fetched_at=100.0, error=None)
    img = _pixels(render_agent_widget(AgentStatusWidget(pos=0), snapshot, now=100.0))
    assert _bar_center(img, 0) == (242, 166, 90)  # waiting
    assert _bar_center(img, 1) == (232, 207, 120)  # running
    assert _bar_center(img, 2) == (125, 170, 220)  # done
    assert _bar_center(img, 3) == (133, 201, 149)  # idle


def test_render_widget_selected_agent_and_empty_statuses() -> None:
    snapshot = AgentStatusSnapshot(providers=PROVIDERS, fetched_at=100.0, error=None)
    img = _pixels(render_agent_widget(AgentStatusWidget(pos=0, agent="codex"), snapshot, now=100.0))
    assert _bar_center(img, 0) == (50, 50, 50)  # codex has no waiting sessions
    assert _bar_center(img, 1) == (232, 207, 120)


def test_render_widget_failure_keeps_counts_in_gray() -> None:
    snapshot = AgentStatusSnapshot(providers=PROVIDERS, fetched_at=100.0, error="command_failed")
    img = _pixels(render_agent_widget(AgentStatusWidget(pos=0), snapshot, now=100.0))
    assert _bar_center(img, 0) == (160, 160, 160)  # stale segments turn gray


def test_render_widget_stale_after_thirty_seconds() -> None:
    snapshot = AgentStatusSnapshot(providers=PROVIDERS, fetched_at=100.0, error=None)
    fresh = _pixels(render_agent_widget(AgentStatusWidget(pos=0), snapshot, now=110.0))
    old = _pixels(render_agent_widget(AgentStatusWidget(pos=0), snapshot, now=200.0))
    assert _bar_center(fresh, 0) == (242, 166, 90)
    assert _bar_center(old, 0) == (160, 160, 160)


def test_render_widget_reports_the_error_code_before_any_reading() -> None:
    snapshot = AgentStatusSnapshot(providers={}, fetched_at=None, error="agent_berth_missing")
    img = _pixels(render_agent_widget(AgentStatusWidget(pos=0), snapshot))
    # No bottom bar is drawn without a reading; the area stays black.
    assert _bar_center(img, 0) == (0, 0, 0)


def test_render_widget_label_fits_beside_the_icon() -> None:
    from ulanzi_niri.icons import _font as load_font

    img = Image.new("RGB", (196, 196))
    draw = ImageDraw.Draw(img)
    snapshot = AgentStatusSnapshot(providers=PROVIDERS, fetched_at=100.0, error=None)
    # OC is the default label for opencode; unknown agents fall back to upper().
    assert agent_status._DEFAULT_LABELS["opencode"] == "OC"
    assert agent_status._DEFAULT_LABELS["all"] == "AGENTS"
    for agent in ("opencode", "pi", "claude", "codex", "grok", "all"):
        widget = AgentStatusWidget(pos=0, agent=agent)
        render_agent_widget(widget, snapshot, now=100.0)
        label = widget.label or agent_status._DEFAULT_LABELS.get(agent, agent.upper())
        assert draw.textlength(label, font=load_font(22)) <= 196 - 2 * 20 - 40 - 8


# --------------------------------------------------------------------------- config
def test_config_accepts_agent_status_widgets() -> None:
    page = PageConfig(
        name="agents",
        agent_status=[
            AgentStatusWidget(pos=0),
            AgentStatusWidget(pos=1, agent="claude", url="https://example.com/agents"),
        ],
    )
    assert page.agent_status[0].agent == "all"
    assert page.agent_status[1].agent == "claude"


@pytest.mark.parametrize("agent", ["", "Agent One", "../etc"])
def test_config_rejects_bad_agent_names(agent) -> None:
    with pytest.raises(ValueError, match="agent"):
        AgentStatusWidget(pos=0, agent=agent)


def test_config_agent_widget_pos_must_not_overlap() -> None:
    with pytest.raises(ValueError, match="overlaps a button pos"):
        PageConfig(
            name="agents",
            button=[ButtonEntry(pos=0)],
            agent_status=[AgentStatusWidget(pos=0)],
        )
    with pytest.raises(ValueError, match="duplicate widget pos"):
        PageConfig(
            name="agents",
            widget=[UsageWidget(pos=0, provider="claude", limit="five_hour")],
            agent_status=[AgentStatusWidget(pos=0)],
        )


# --------------------------------------------------------------------------- service
def _agent_service(monkeypatch, tmp_path, widget: AgentStatusWidget):
    cfg = Config(page=[PageConfig(name="agents", agent_status=[widget])])
    monkeypatch.setattr(service, "load_config", lambda _: cfg)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return service.Service("unused.toml")


async def test_widget_press_refreshes_agents_and_opens_url(monkeypatch, tmp_path) -> None:
    widget = AgentStatusWidget(pos=0, url="https://example.com/agents")
    app = _agent_service(monkeypatch, tmp_path, widget)
    app._agents = Mock()
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "dispatch", dispatch)

    await app._on_button(DeckEvent(kind=DeckEventKind.LCD_BUTTON, pos=0, pressed=True))
    await asyncio.gather(*app._widget_tasks)

    app._agents.refresh.assert_called_once_with(force=True)
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[0].url == "https://example.com/agents"


async def test_widget_press_without_url_only_refreshes(monkeypatch, tmp_path) -> None:
    app = _agent_service(monkeypatch, tmp_path, AgentStatusWidget(pos=0))
    app._agents = Mock()
    dispatch = AsyncMock()
    monkeypatch.setattr(service, "dispatch", dispatch)

    await app._on_button(DeckEvent(kind=DeckEventKind.LCD_BUTTON, pos=0, pressed=True))

    app._agents.refresh.assert_called_once_with(force=True)
    dispatch.assert_not_awaited()


async def test_control_agent_status_reads_cache(monkeypatch, tmp_path) -> None:
    app = _agent_service(monkeypatch, tmp_path, AgentStatusWidget(pos=0))
    app._agents = Mock()
    app._agents.get.return_value = AgentStatusSnapshot(
        providers=PROVIDERS, fetched_at=1.0, error=None
    )
    reply = await app.apply_control("agent-status")
    assert json.loads(reply[3:]) == {"providers": PROVIDERS, "error": None}
    app._agents.refresh.assert_not_called()


async def test_control_agent_status_refresh_waits(monkeypatch, tmp_path) -> None:
    app = _agent_service(monkeypatch, tmp_path, AgentStatusWidget(pos=0))

    async def fetch():
        return dict(PROVIDERS)

    monkeypatch.setattr(agent_status, "fetch_agent_status", fetch)
    reply = await app.apply_control("agent-status --refresh")
    assert json.loads(reply[3:]) == {"providers": PROVIDERS, "error": None}


async def test_control_refresh_agent_status_runs_in_background(monkeypatch, tmp_path) -> None:
    app = _agent_service(monkeypatch, tmp_path, AgentStatusWidget(pos=0))
    release = asyncio.Event()

    async def fetch():
        await release.wait()
        return dict(PROVIDERS)

    monkeypatch.setattr(agent_status, "fetch_agent_status", fetch)
    app._agents._snapshot = AgentStatusSnapshot(providers={}, fetched_at=1.0, error=None)
    try:
        assert await app.apply_control("refresh-agent-status") == "OK refresh-requested"
        task = app._agents._task
        assert task is not None and not task.done()
    finally:
        release.set()
        await task
    assert app._agents.get().providers == PROVIDERS


async def test_control_agent_status_socket_roundtrip(monkeypatch, tmp_path) -> None:
    from ulanzi_niri import cli

    app = _agent_service(monkeypatch, tmp_path, AgentStatusWidget(pos=0))
    app._agents = Mock()
    app._agents.get.return_value = AgentStatusSnapshot(
        providers=PROVIDERS, fetched_at=1.0, error=None
    )
    assert await app.start_control()
    try:
        payload = await asyncio.to_thread(cli._read_cached_agent_status, 1)
        assert payload == {"providers": PROVIDERS, "error": None}
    finally:
        await app.stop_control()


async def test_agent_update_repaints_only_when_counts_change(monkeypatch, tmp_path) -> None:
    app = _agent_service(monkeypatch, tmp_path, AgentStatusWidget(pos=0))
    app._device = Mock()
    renders = 0

    async def render():
        nonlocal renders
        renders += 1

    monkeypatch.setattr(app, "_render_current_page", render)
    app._last_agent_render_key = (PROVIDERS, None)
    app._agents._snapshot = AgentStatusSnapshot(providers=PROVIDERS, fetched_at=1.0, error=None)
    await app._on_agent_update()
    assert renders == 0  # unchanged counts do not repush the page
    app._agents._snapshot = AgentStatusSnapshot(providers={}, fetched_at=1.0, error="timeout")
    await app._on_agent_update()
    assert renders == 1
