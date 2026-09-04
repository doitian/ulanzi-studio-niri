"""Fetch and render Claude, Codex, OpenCode Go, and Moonshot usage for D200X buttons.

Provider credentials are read from the files maintained by their CLIs. Usage
is fetched directly from each provider without an additional command-line
program. Moonshot (Kimi API) is pay-as-you-go, so instead of a rate-limit
window it reports the remaining account balance.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw

from .config import UsageWidget
from .icons import LABEL_BOTTOM_PADDING, _load_icon_image, resolve_icon_path
from .icons import _font as load_font
from .protocol.ulanzi_d200x import STD_ICON

log = logging.getLogger(__name__)

USAGE_REFRESH_SECONDS = 30 * 60
MANUAL_REFRESH_THROTTLE_SECONDS = 90

RETRY_BACKOFF_SECONDS = (300, 600, 1800)

WIDGET_SIZE = STD_ICON  # (196, 196)


@dataclass
class UsageLimit:
    remaining_percent: float | None
    resets_at: str
    reset_after_seconds: float
    remaining_amount: float | None = None  # pay-as-you-go balance (Moonshot)
    currency: str = ""


class FetchStatus(Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class UsageFetchResult:
    status: FetchStatus
    data: dict | None = None


USAGE_TTL_SECONDS = USAGE_REFRESH_SECONDS

_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
_MOONSHOT_BALANCE_URL_AI = "https://api.moonshot.ai/v1/users/me/balance"
_MOONSHOT_BALANCE_URL_CN = "https://api.moonshot.cn/v1/users/me/balance"
# (green, yellow) minimum available balance; below yellow the widget turns red
_MOONSHOT_BALANCE_THRESHOLDS = {"USD": (12.0, 6.0), "CNY": (70.0, 36.0)}
_CURRENCY_SYMBOLS = {"USD": "$", "CNY": "¥"}
_REFRESH_SKEW_SECONDS = 30


class _HTTPStatusError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class UsageFetcher:
    """Serve cached provider usage and refresh it in the background.

    ``get()`` returns the last known result immediately and never blocks;
    ``refresh()`` starts a background fetch only when the cache is older than
    ``ttl`` and no fetch is already in flight. Forced refreshes use a shorter
    90-second throttle window. A finished fetch invokes ``on_update`` so
    callers can repaint with fresh data.

    Failed fetches are retried with a capped exponential backoff
    (``RETRY_BACKOFF_SECONDS``) instead of waiting for the next external
    refresh, so a transient failure recovers on its own. Auth failures (e.g. an
    expired Claude token needing ``claude /login``) are cached as-is and never
    retried in the background after the request's one refresh-token attempt —
    the renderer surfaces them as ``401``.
    """

    def __init__(self, *, ttl: float = USAGE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._result: UsageFetchResult | None = None
        self._fetched_at: float | None = None
        self._task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._retry_attempt = 0
        self._on_update: Callable[[], Awaitable[None]] | None = None

    def set_on_update(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._on_update = callback

    def get(self) -> UsageFetchResult | None:
        return self._result

    def refresh(self, *, force: bool = False) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._fetched_at is not None:
            age = asyncio.get_running_loop().time() - self._fetched_at
            minimum_age = MANUAL_REFRESH_THROTTLE_SECONDS if force else self._ttl
            if age < minimum_age:
                log.debug(
                    "usage refresh throttled: manual=%s age=%.1fs minimum=%.1fs",
                    force,
                    age,
                    minimum_age,
                )
                return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        result = await fetch_usage()
        self._result = result
        self._fetched_at = asyncio.get_running_loop().time()
        log.debug("usage fetch complete: status=%s", result.status)
        await self._notify()
        if (
            result.status is FetchStatus.OK
            or result_auth_denied(result.data)
            or result_rate_limited(result.data)
        ):
            self._retry_attempt = 0
        else:
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        if self._retry_task is not None and not self._retry_task.done():
            return
        idx = min(self._retry_attempt, len(RETRY_BACKOFF_SECONDS) - 1)
        delay = RETRY_BACKOFF_SECONDS[idx]
        self._retry_attempt += 1
        log.info("usage fetch failed; retrying in %.0fs", delay)
        self._retry_task = asyncio.create_task(self._retry_later(delay))

    async def _retry_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._retry_task = None
        await self._run()

    async def _notify(self) -> None:
        callback = self._on_update
        if callback is not None:
            try:
                await callback()
            except Exception:  # noqa: BLE001
                log.exception("usage update callback failed")


async def fetch_usage(timeout: float = 20.0) -> UsageFetchResult:
    """Fetch all provider usage concurrently using their CLI credentials."""
    tasks = [
        asyncio.to_thread(_fetch_claude, timeout),
        asyncio.to_thread(_fetch_codex, timeout),
        asyncio.to_thread(_fetch_opencode_go, timeout),
        asyncio.to_thread(_fetch_moonshot, timeout),
    ]
    try:
        claude, codex, opencode_go, moonshot = await asyncio.wait_for(
            asyncio.gather(*tasks), timeout=timeout + 1
        )
    except TimeoutError:
        log.error("usage fetch timed out after %.0fs", timeout)
        return UsageFetchResult(FetchStatus.TIMEOUT)

    providers = {
        "claude": claude,
        "codex": codex,
        "opencode-go": opencode_go,
        "moonshot": moonshot,
    }
    status = (
        FetchStatus.ERROR
        if any("error" in value for value in providers.values())
        else FetchStatus.OK
    )
    return UsageFetchResult(status, {"providers": providers})


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(f"token not found at {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read credentials at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"credentials at {path} are not a JSON object")
    return value


def _request_json(
    url: str, token: str, timeout: float, *, extra_headers: dict[str, str] | None = None
) -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "ulanzi-niri/1.0",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:  # noqa: S310
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", "replace").strip()
        retry_after = exc.headers.get("Retry-After", "")
        prefix = "rate limited: " if exc.code == 429 else ""
        suffix = f" (retry after {retry_after}s)" if retry_after else ""
        raise _HTTPStatusError(
            exc.code, f"{prefix}HTTP {exc.code} from {url}: {detail}{suffix}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"request to {url} failed: {exc}") from exc
    if len(raw) > 1024 * 1024:
        raise RuntimeError(f"response from {url} is too large")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"non-JSON response from {url}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"response from {url} is not a JSON object")
    return value


def _post_form_json(url: str, values: dict[str, str], timeout: float) -> dict:
    request = Request(
        url,
        data=urlencode(values).encode(),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ulanzi-niri/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=min(timeout, 5.0)) as response:  # noqa: S310
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        detail = exc.read(512).decode("utf-8", "replace").strip()
        retry_after = exc.headers.get("Retry-After", "")
        prefix = "rate limited: " if exc.code == 429 else ""
        suffix = f" (retry after {retry_after}s)" if retry_after else ""
        raise RuntimeError(f"{prefix}HTTP {exc.code} from {url}: {detail}{suffix}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"request to {url} failed: {exc}") from exc
    if len(raw) > 1024 * 1024:
        raise RuntimeError(f"response from {url} is too large")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"non-JSON response from {url}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"response from {url} is not a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict, *, expected: dict | None = None) -> None:
    """Replace a CLI credential without exposing a partially written file."""
    if expected is not None and _read_json(path) != expected:
        raise RuntimeError(f"credentials at {path} changed while refreshing; keeping newer file")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _jwt_claim(token: str, claim: str) -> object | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return claims.get(claim) if isinstance(claims, dict) else None


def _refresh_token(url: str, client_id: str, refresh_token: str, timeout: float) -> dict:
    if not refresh_token:
        raise RuntimeError("credential expired and no refresh token is available; log in again")
    result = _post_form_json(
        url,
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
        timeout,
    )
    if not isinstance(result.get("access_token"), str) or not result["access_token"]:
        raise RuntimeError(f"refresh response from {url} is missing access_token")
    return result


def _claude_access_token(
    path: Path, credential: dict, timeout: float, *, force_refresh: bool = False
) -> str:
    oauth = credential.get("claudeAiOauth", {})
    if not isinstance(oauth, dict):
        raise RuntimeError("claude token not found — run `claude /login`")
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError("claude token not found — run `claude /login`")
    expires_at = float(oauth.get("expiresAt", 0)) / 1000
    if force_refresh or (
        expires_at and expires_at <= datetime.now(UTC).timestamp() + _REFRESH_SKEW_SECONDS
    ):
        original = copy.deepcopy(credential)
        refreshed = _refresh_token(
            _CLAUDE_TOKEN_URL, _CLAUDE_CLIENT_ID, str(oauth.get("refreshToken", "")), timeout
        )
        oauth["accessToken"] = refreshed["access_token"]
        oauth["refreshToken"] = refreshed.get("refresh_token") or oauth.get("refreshToken", "")
        expires_in = float(refreshed.get("expires_in", 0))
        oauth["expiresAt"] = (
            int((datetime.now(UTC).timestamp() + expires_in) * 1000) if expires_in > 0 else 0
        )
        _write_json_atomic(path, credential, expected=original)
        token = str(oauth["accessToken"])
    return token


def _codex_access_token(
    path: Path, credential: dict, timeout: float, *, force_refresh: bool = False
) -> str:
    tokens = credential.get("tokens", {})
    if not isinstance(tokens, dict):
        raise RuntimeError("codex token not found — run `codex login`")
    token = tokens.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("codex token not found — run `codex login`")
    expires_at = _jwt_claim(token, "exp")
    if force_refresh or (
        isinstance(expires_at, (int, float))
        and expires_at <= datetime.now(UTC).timestamp() + _REFRESH_SKEW_SECONDS
    ):
        original = copy.deepcopy(credential)
        refreshed = _refresh_token(
            _CODEX_TOKEN_URL, _CODEX_CLIENT_ID, str(tokens.get("refresh_token", "")), timeout
        )
        tokens["access_token"] = refreshed["access_token"]
        tokens["refresh_token"] = refreshed.get("refresh_token") or tokens.get("refresh_token", "")
        if refreshed.get("id_token"):
            tokens["id_token"] = refreshed["id_token"]
        credential["last_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        _write_json_atomic(path, credential, expected=original)
        token = str(tokens["access_token"])
    return token


def _request_json_retry_unauthorized(
    url: str,
    token: str,
    timeout: float,
    refresh: Callable[[], str],
    *,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Refresh the access token and retry once when a request returns HTTP 401."""
    try:
        return _request_json(url, token, timeout, extra_headers=extra_headers)
    except _HTTPStatusError as exc:
        if exc.status != 401:
            raise
    return _request_json(url, refresh(), timeout, extra_headers=extra_headers)


def _limit(used: float, resets_at: str | int | float) -> dict:
    if isinstance(resets_at, str):
        reset = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    else:
        reset = datetime.fromtimestamp(resets_at, UTC)
    reset = reset.astimezone(UTC).replace(microsecond=0)
    seconds = max(0, int((reset - datetime.now(UTC)).total_seconds()))
    return {
        "used_percent": used,
        "remaining_percent": max(0.0, min(100.0, 100.0 - used)),
        "resets_at": reset.isoformat(),
        "reset_after_seconds": seconds,
    }


def _account(email: str, limits: dict, *, error: str | None = None) -> dict:
    result = {"email": email, "active": True, "limits": limits}
    if error:
        result["error"] = error
    return result


def _provider_error(message: str) -> dict:
    lowered = message.lower()
    if any(
        marker in lowered
        for marker in ("http 401", "http 403", "invalid_grant", "already been used")
    ):
        kind = "auth denied"
    elif any(
        marker in lowered
        for marker in ("token not found", "credential expired", "no refresh token")
    ):
        kind = "auth missing"
    else:
        kind = "request failed"
    return {"error": f"{kind}: {message}"}


def _fetch_claude(timeout: float) -> dict:
    try:
        path = Path.home() / ".claude" / ".credentials.json"
        raw_credential = _read_json(path)
        token = _claude_access_token(path, raw_credential, timeout)
        headers = {"Anthropic-Beta": "oauth-2025-04-20", "User-Agent": "claude-code/0.0.0-dev"}
        usage = _request_json_retry_unauthorized(
            _CLAUDE_USAGE_URL,
            token,
            timeout,
            lambda: _claude_access_token(path, raw_credential, timeout, force_refresh=True),
            extra_headers=headers,
        )
        limits: dict[str, dict] = {}
        for key in ("five_hour", "seven_day", "seven_day_sonnet"):
            window = usage.get(key)
            if isinstance(window, dict) and window.get("resets_at") is not None:
                limits[key] = _limit(float(window.get("utilization", 0)), window["resets_at"])
        scoped = usage.get("limits")
        if isinstance(scoped, list):
            for entry in scoped:
                if not isinstance(entry, dict) or entry.get("resets_at") is None:
                    continue
                kind = entry.get("kind")
                scoped_key: str | None = {
                    "session": "five_hour",
                    "weekly_all": "seven_day",
                }.get(str(kind))
                if kind == "weekly_scoped":
                    scope = entry.get("scope", {})
                    model = scope.get("model", {}) if isinstance(scope, dict) else {}
                    name = model.get("display_name", "") if isinstance(model, dict) else ""
                    slug = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
                    scoped_key = f"seven_day_{slug}" if slug else None
                if scoped_key and scoped_key not in limits:
                    limits[scoped_key] = _limit(float(entry.get("percent", 0)), entry["resets_at"])
        return {"accounts": [_account("", limits)]}
    except (RuntimeError, TypeError, ValueError) as exc:
        log.warning("Claude usage fetch failed: %s", exc)
        return _provider_error(str(exc))


def _jwt_email(token: str) -> str:
    value = _jwt_claim(token, "email")
    return str(value) if value is not None else ""


def _codex_window_key(seconds: int) -> str:
    for duration, name in ((18000, "five_hour"), (604800, "seven_day"), (2592000, "thirty_day")):
        if duration * 0.95 <= seconds <= duration * 1.05:
            return name
    return f"window_{seconds}s"


def _fetch_codex(timeout: float) -> dict:
    try:
        path = Path.home() / ".codex" / "auth.json"
        raw_credential = _read_json(path)
        tokens = raw_credential.get("tokens", {})
        token = _codex_access_token(path, raw_credential, timeout)
        usage = _request_json_retry_unauthorized(
            _CODEX_USAGE_URL,
            token,
            timeout,
            lambda: _codex_access_token(path, raw_credential, timeout, force_refresh=True),
        )
        rate_limit = usage.get("rate_limit")
        if not isinstance(rate_limit, dict):
            raise RuntimeError("codex usage response missing rate_limit object")
        limits: dict[str, dict] = {}
        for field in ("primary_window", "secondary_window"):
            window = rate_limit.get(field)
            if not isinstance(window, dict) or float(window.get("reset_at", 0)) <= 0:
                continue
            key = _codex_window_key(int(window.get("limit_window_seconds", 0)))
            limits[key] = _limit(float(window.get("used_percent", 0)), window["reset_at"])
        id_token = tokens.get("id_token", "") if isinstance(tokens, dict) else ""
        return {"accounts": [_account(_jwt_email(str(id_token)), limits)]}
    except (RuntimeError, TypeError, ValueError) as exc:
        log.warning("Codex usage fetch failed: %s", exc)
        return _provider_error(str(exc))


def _opencode_go_key() -> str:
    key = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    if key:
        return key

    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    path = root / "opencode" / "auth.json"
    credential = _read_json(path).get("opencode-go")
    if not isinstance(credential, dict):
        raise RuntimeError("opencode-go token not found — run `opencode` and use `/connect`")
    stored_key = credential.get("key")
    if credential.get("type") != "api" or not isinstance(stored_key, str) or not stored_key:
        raise RuntimeError("opencode-go token not found — run `opencode` and use `/connect`")
    return stored_key


def _fetch_opencode_go(timeout: float) -> dict:
    try:
        usage = _request_json(_OPENCODE_GO_USAGE_URL, _opencode_go_key(), timeout)
        windows = usage.get("usage")
        if not isinstance(windows, dict):
            raise RuntimeError("opencode-go usage response missing usage object")
        limits: dict[str, dict] = {}
        for key in ("rolling", "weekly", "monthly"):
            window = windows.get(key)
            if (
                not isinstance(window, dict)
                or window.get("percent") is None
                or window.get("resetsAt") is None
            ):
                continue
            limits[key] = _limit(float(window["percent"]), window["resetsAt"])
        if not limits:
            raise RuntimeError("opencode-go usage response has no recognized windows")
        return {"accounts": [_account("", limits)]}
    except (RuntimeError, TypeError, ValueError) as exc:
        log.warning("OpenCode Go usage fetch failed: %s", exc)
        return _provider_error(str(exc))


def _moonshot_credential() -> tuple[str, str, str]:
    """Resolve the Kimi API key to (key, balance URL, currency).

    ``MOONSHOT_API_KEY`` wins and targets the international platform
    (api.moonshot.ai, USD); set ``MOONSHOT_BASE_URL`` (including ``/v1``) for
    the China platform. Otherwise the key is read from OpenCode's auth.json,
    which keeps separate ``moonshotai`` (USD) and ``moonshotai-cn`` (CNY)
    entries for the two platforms.
    """
    key = os.environ.get("MOONSHOT_API_KEY", "").strip()
    if key:
        base = os.environ.get("MOONSHOT_BASE_URL", "").strip()
        if not base:
            return key, _MOONSHOT_BALANCE_URL_AI, "USD"
        currency = "CNY" if ".cn" in base else "USD"
        return key, f"{base.rstrip('/')}/users/me/balance", currency

    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    path = root / "opencode" / "auth.json"
    auth = _read_json(path)
    for provider_id, url, currency in (
        ("moonshotai", _MOONSHOT_BALANCE_URL_AI, "USD"),
        ("moonshotai-cn", _MOONSHOT_BALANCE_URL_CN, "CNY"),
    ):
        credential = auth.get(provider_id)
        if not isinstance(credential, dict):
            continue
        stored_key = credential.get("key")
        if credential.get("type") == "api" and isinstance(stored_key, str) and stored_key:
            return stored_key, url, currency
    raise RuntimeError(
        "moonshot API key not found — set MOONSHOT_API_KEY or `/connect` Moonshot AI in `opencode`"
    )


def _fetch_moonshot(timeout: float) -> dict:
    try:
        key, url, currency = _moonshot_credential()
        response = _request_json(url, key, timeout)
        data = response.get("data")
        if response.get("code") != 0 or not isinstance(data, dict):
            raise RuntimeError("moonshot balance response missing data object")
        available = data.get("available_balance")
        if not isinstance(available, (int, float)):
            raise RuntimeError("moonshot balance response missing available_balance")
        limit = {
            "remaining_amount": float(available),
            "currency": currency,
        }
        return {"accounts": [_account("", {"balance": limit})]}
    except (RuntimeError, TypeError, ValueError) as exc:
        log.warning("Moonshot balance fetch failed: %s", exc)
        return _provider_error(str(exc))


_AUTH_ERROR_MARKERS = (
    "auth denied",
    "auth missing",
    "credential expired",
    "token not found",
    "tokens revoked",
    "token_invalidated",
    "token_revoked",
)


def _is_auth_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


def resolve_account(providers: dict, provider: str, account: str) -> dict | None:
    """Resolve a provider's account dict from usage's ``providers`` object.

    ``account`` matches an entry's email; when empty the provider's ``active``
    account is used (falling back to the first entry).
    """
    p = providers.get(provider)
    if not isinstance(p, dict):
        return None
    accounts = p.get("accounts")
    if not isinstance(accounts, list):
        return None
    if account:
        for a in accounts:
            if isinstance(a, dict) and a.get("email") == account:
                return a
        # Claude's usage response has no email. Direct integration has one
        # active credential, so keep existing email-based configs working.
        anonymous = [a for a in accounts if isinstance(a, dict) and not a.get("email")]
        if len(accounts) == 1 and anonymous:
            return anonymous[0]
        return None
    for a in accounts:
        if isinstance(a, dict) and a.get("active"):
            return a
    if accounts and isinstance(accounts[0], dict):
        return accounts[0]
    return None


def resolve_limit(providers: dict, provider: str, account: str, limit: str) -> UsageLimit | None:
    """Pick a limit out of usage's ``providers`` object for a provider/account."""
    target = resolve_account(providers, provider, account)
    if target is None:
        return None
    limits = target.get("limits")
    if not isinstance(limits, dict):
        return None
    lim = limits.get(limit)
    if not isinstance(lim, dict):
        return None
    percent = lim.get("remaining_percent")
    amount = lim.get("remaining_amount")
    return UsageLimit(
        remaining_percent=float(percent) if percent is not None else None,
        resets_at=str(lim.get("resets_at", "")),
        reset_after_seconds=float(lim.get("reset_after_seconds", 0.0)),
        remaining_amount=float(amount) if amount is not None else None,
        currency=str(lim.get("currency", "")),
    )


def resolve_auth_error(providers: dict, provider: str, account: str) -> str | None:
    """Return the auth error message for a provider/account, else ``None``.

    Checks the provider-level ``error`` first (e.g. ``auth missing``), then the
    resolved account's ``error`` (e.g. an expired token needing re-login).
    """
    p = providers.get(provider)
    if isinstance(p, dict):
        err = p.get("error")
        if isinstance(err, str) and err and _is_auth_error(err):
            return err
    target = resolve_account(providers, provider, account)
    if target is not None:
        err = target.get("error")
        if isinstance(err, str) and err and _is_auth_error(err):
            return err
    return None


_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


def resolve_http_status(providers: dict, provider: str, account: str) -> int | None:
    """Return the HTTP status code from a provider/account error, else ``None``.

    usage embeds failed status codes in its error text (e.g. ``"HTTP 401"``,
    ``"HTTP 429"``); this surfaces the code for a single widget's provider and
    account so one failing provider does not affect the others.
    """
    p = providers.get(provider)
    if isinstance(p, dict):
        err = p.get("error")
        if isinstance(err, str):
            m = _HTTP_STATUS_RE.search(err)
            if m is not None:
                return int(m.group(1))
    target = resolve_account(providers, provider, account)
    if target is not None:
        err = target.get("error")
        if isinstance(err, str):
            m = _HTTP_STATUS_RE.search(err)
            if m is not None:
                return int(m.group(1))
    return None


def result_auth_denied(data: dict | None) -> bool:
    """True when any provider/account in an usage result reports an auth error."""
    if not isinstance(data, dict):
        return False
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    for p in providers.values():
        if not isinstance(p, dict):
            continue
        err = p.get("error")
        if isinstance(err, str) and err and _is_auth_error(err):
            return True
        accounts = p.get("accounts")
        if isinstance(accounts, list):
            for a in accounts:
                if isinstance(a, dict):
                    err = a.get("error")
                    if isinstance(err, str) and err and _is_auth_error(err):
                        return True
    return False


def result_rate_limited(data: dict | None) -> bool:
    """True when a provider asks us to stop making usage requests for now."""
    if not isinstance(data, dict):
        return False
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return False
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        error = provider.get("error")
        if isinstance(error, str) and "rate limited: HTTP 429" in error:
            return True
        accounts = provider.get("accounts")
        if isinstance(accounts, list):
            for account in accounts:
                if isinstance(account, dict):
                    error = account.get("error")
                    if isinstance(error, str) and "rate limited: HTTP 429" in error:
                        return True
    return False


def format_reset(seconds: float) -> str:
    """Humanize a reset countdown with two units, e.g. ``"1h2m"``, ``"3d4h"``."""
    s = int(seconds)
    if s <= 0:
        return "now"
    if s < 60:
        return "<1m"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def format_balance(amount: float, currency: str) -> str:
    """Render a pay-as-you-go balance with its currency symbol, e.g. ``"$4.96"``."""
    symbol = _CURRENCY_SYMBOLS.get(currency)
    if symbol is None:
        return f"{currency} {amount:.2f}".lstrip()
    return f"{symbol}{amount:.2f}"


_COMPACT_SUFFIXES = ("K", "M", "B", "T")


def _balance_prefix(currency: str) -> str:
    symbol = _CURRENCY_SYMBOLS.get(currency)
    return symbol if symbol is not None else f"{currency} ".lstrip()


def _balance_parts(amount: float, currency: str) -> tuple[str, str]:
    """Split a balance into a short center text and a decimal-part footer.

    The widget renders the center large and the footer in the reset-time row,
    so the center stays short enough for a large font: at most 3 integer
    digits with 2 decimals in the footer (``"¥123"`` / ``".45"``), or a
    compact suffix with 3 decimals (``"¥12K"`` / ``".345"``) above 999.
    Negative amounts clamp to 0. All math is done in integer cents so decimal
    digits are exact.
    """
    prefix = _balance_prefix(currency)
    cents = round(max(0.0, amount) * 100)
    integer, frac = divmod(cents, 100)
    if integer <= 999:
        return f"{prefix}{integer}", f".{frac:02d}"
    divisor = 100
    suffix = _COMPACT_SUFFIXES[-1]
    for candidate in _COMPACT_SUFFIXES:
        divisor *= 1000
        suffix = candidate
        if cents // divisor < 1000:
            break
    frac3 = (cents % divisor) * 1000 // divisor
    return f"{prefix}{cents // divisor}{suffix}", f".{frac3:03d}"


def _balance_color(amount: float, currency: str) -> tuple[int, int, int]:
    green_min, yellow_min = _MOONSHOT_BALANCE_THRESHOLDS.get(
        currency, _MOONSHOT_BALANCE_THRESHOLDS["USD"]
    )
    if amount < yellow_min:
        return _COLOR_RED
    if amount < green_min:
        return _COLOR_YELLOW
    return _COLOR_GREEN


def _default_label(widget: UsageWidget) -> str:
    return {
        "five_hour": "5H",
        "seven_day": "7D",
        "seven_day_fable": "FABLE",
        "rolling": "5H",
        "weekly": "7D",
        "monthly": "30D",
        "balance": "BAL",
    }[widget.limit]


def _draw_centered(
    draw: ImageDraw.ImageDraw, center: tuple[int, int], text: str, font, fill
) -> None:
    """Draw ``text`` centered (horizontally and vertically) on ``center``."""
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    draw.text(
        (center[0] - w // 2 - bbox[0], center[1] - h // 2 - bbox[1]),
        text,
        font=font,
        fill=fill,
    )


def _draw_fit(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    text: str,
    font_size: int,
    fill: tuple[int, int, int],
    max_width: int,
    *,
    min_size: int = 10,
    reference: str | None = None,
) -> None:
    """Center ``text`` at ``center``, shrinking the font to fit ``max_width``.

    With ``reference``, the font size is chosen to fit the reference instead
    and ``text`` only shrinks further when wider than the reference, so short
    values render at the same size as reference-length ones.
    """
    target = reference if reference is not None else text
    size = font_size
    while size > min_size:
        bbox = draw.textbbox((0, 0), target, font=load_font(size))
        if bbox[2] - bbox[0] <= max_width:
            break
        size -= 2
    while size > min_size:
        font = load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            _draw_centered(draw, center, text, font, fill)
            return
        size -= 2
    _draw_centered(draw, center, text, load_font(min_size), fill)


def _draw_fit_bottom(
    draw: ImageDraw.ImageDraw,
    cx: int,
    bottom: int,
    text: str,
    font_size: int,
    fill: tuple[int, int, int],
    max_width: int,
    *,
    min_size: int = 10,
) -> None:
    """Draw ``text`` bottom-aligned at ``bottom``, shrinking to fit ``max_width``."""
    size = font_size
    while size >= min_size:
        font = load_font(size)
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            break
        size -= 2
    font = load_font(size)
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (cx - (bbox[2] - bbox[0]) // 2 - bbox[0], bottom - (bbox[3] - bbox[1]) - bbox[1]),
        text,
        font=font,
        fill=fill,
    )


def _load_corner_icon(name: str, size: int) -> Image.Image | None:
    """Resolve and scale an app icon to fit in a ``size``x``size`` corner."""
    path = resolve_icon_path(name)
    if path is None:
        log.warning("widget icon %r not found in any search path", name)
        return None
    try:
        icon = _load_icon_image(str(path), size, "#FFFFFF")
    except (OSError, ValueError, ImportError) as exc:
        log.warning("failed to load widget icon %s: %s", path, exc)
        return None
    scale = min(size / icon.width, size / icon.height)
    w = max(1, round(icon.width * scale))
    h = max(1, round(icon.height * scale))
    return icon.resize((w, h), Image.Resampling.LANCZOS)


_COLOR_GRAY = (160, 160, 160)
_COLOR_GREEN = (0, 200, 80)
_COLOR_YELLOW = (240, 190, 0)
_COLOR_RED = (230, 60, 50)


def render_widget(
    widget: UsageWidget,
    providers: dict,
    *,
    status: FetchStatus = FetchStatus.OK,
    size: int = WIDGET_SIZE[0],
    padding: int = 20,
) -> bytes:
    """Render a single usage widget to a square PNG (default 196x196)."""
    img = Image.new("RGB", (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    info = resolve_limit(providers, widget.provider, widget.account, widget.limit)
    http_status = resolve_http_status(providers, widget.provider, widget.account)
    auth_error = resolve_auth_error(providers, widget.provider, widget.account)
    label = widget.label or _default_label(widget)
    cx = size // 2
    max_width = size - 2 * padding

    icon = None
    if widget.icon:
        icon = _load_corner_icon(widget.icon, 40)

    font = load_font(28)
    bbox = draw.textbbox((0, 0), label, font=font)
    label_h = bbox[3] - bbox[1]
    icon_center = padding + icon.height // 2 if icon is not None else None
    label_y = (
        icon_center - label_h // 2 - bbox[1]
        if icon_center is not None
        else padding - 4
    )
    draw.text((padding, label_y), label, font=font, fill=(255, 255, 255))

    if icon is not None:
        img.paste(icon, (size - padding - icon.width, padding), icon)

    footer: str | None = None
    reference: str | None = None
    if status is FetchStatus.TIMEOUT:
        pct = "TO"
        color = _COLOR_RED
    elif http_status is not None:
        pct = str(http_status)
        color = _COLOR_RED
    elif auth_error is not None:
        pct = "401"
        color = _COLOR_RED
    elif info is None:
        if status is FetchStatus.ERROR:
            pct = "Err"
            color = _COLOR_RED
        else:
            pct = "n/a"
            color = _COLOR_GRAY
    elif info.remaining_amount is not None:
        pct, footer = _balance_parts(info.remaining_amount, info.currency)
        # "00M" is the widest 4-glyph form (2 digits + suffix), so any balance
        # of up to 3 glyphs after the symbol renders at the same font size.
        reference = f"{_balance_prefix(info.currency)}00M"
        color = _balance_color(info.remaining_amount, info.currency)
    else:
        percent = info.remaining_percent or 0.0
        pct = f"{percent:.0f}%"
        if percent >= 60:
            color = _COLOR_GREEN
        elif percent >= 30:
            color = _COLOR_YELLOW
        else:
            color = _COLOR_RED
    _draw_fit(draw, (cx, size // 2), pct, 56, color, max_width, reference=reference)

    if footer is None and info is not None and info.remaining_amount is None:
        footer = format_reset(info.reset_after_seconds)
    if footer is not None:
        _draw_fit_bottom(
            draw,
            cx,
            size - LABEL_BOTTOM_PADDING,
            footer,
            28,
            (180, 180, 180),
            max_width,
        )

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
