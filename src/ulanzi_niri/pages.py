"""Page-set helper bound to the active Config.

Encapsulates lookup, switching, history (for `page.back`), and toggling.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Config, PageConfig

log = logging.getLogger(__name__)


class PageSet:
    def __init__(self, config: Config) -> None:
        self._cfg = config
        self._current: PageConfig = config.default_page()
        self._history: list[str] = []
        self._max_history = 16

    @property
    def current(self) -> PageConfig:
        return self._current

    @property
    def name(self) -> str:
        return self._current.name

    def replace_config(self, config: Config) -> PageConfig:
        """Swap config; keep current page if its name still exists."""
        self._cfg = config
        target = config.get_page(self._current.name) or config.default_page()
        self._current = target
        # purge history entries that no longer exist
        valid = {p.name for p in config.page}
        self._history = [n for n in self._history if n in valid]
        return target

    def switch(self, name: str) -> Optional[PageConfig]:
        page = self._cfg.get_page(name)
        if page is None:
            log.warning("no such page: %s", name)
            return None
        if page.name == self._current.name:
            return page
        self._push_history(self._current.name)
        self._current = page
        return page

    def back(self) -> Optional[PageConfig]:
        while self._history:
            name = self._history.pop()
            page = self._cfg.get_page(name)
            if page is not None and page.name != self._current.name:
                self._current = page
                return page
        return None

    def toggle(self, name: str) -> Optional[PageConfig]:
        if self._current.name == name:
            return self.back() or self._current
        return self.switch(name)

    def _push_history(self, name: str) -> None:
        self._history.append(name)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]
