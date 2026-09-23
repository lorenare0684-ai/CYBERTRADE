"""External strategy plugin loader.

Drop python files into ``~/.cybertrade/plugins`` (or a configured directory).
Each module may define ``STRATEGY_CLASS`` (a ``Strategy`` subclass) or a
``register()`` function returning one or more instances.  Plugins are imported
in a sandboxed namespace with failures quarantined — a broken third-party
strategy can never take the bot down (``PluginLoadError`` logged, not raised
into the engine path).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..exceptions import CybertradeError
from ..strategies.base import Strategy

log = logging.getLogger("cybertrade.plugins")

PLUGIN_ENV_DIR = "~/.cybertrade/plugins"


class PluginLoadError(CybertradeError):
    """A plugin module failed to import or expose a valid strategy."""


@dataclass
class PluginInfo:
    name: str
    path: str
    strategy_names: List[str] = field(default_factory=list)
    error: str = ""
    version: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "strategies": self.strategy_names,
            "error": self.error,
            "version": self.version,
            "ok": self.ok,
        }


class PluginRegistry:
    """Loads, validates, and tracks user strategy plugins."""

    def __init__(self, plugin_dir: Optional[str] = None) -> None:
        self.plugin_dir = Path(plugin_dir or PLUGIN_ENV_DIR).expanduser()
        self._strategies: Dict[str, Strategy] = {}
        self._infos: List[PluginInfo] = []
        self._lock = threading.RLock()

    # -- loading -----------------------------------------------------------
    def load_all(self) -> List[PluginInfo]:
        with self._lock:
            self._strategies.clear()
            self._infos.clear()
            if not self.plugin_dir.exists():
                log.info("plugin dir absent (%s) — plugins skipped", self.plugin_dir)
                return []
            for path in sorted(self.plugin_dir.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                info = self._load_file(path)
                self._infos.append(info)
            loaded = [i for i in self._infos if i.ok]
            log.info(
                "plugins loaded=%d failed=%d dir=%s",
                len(loaded),
                len(self._infos) - len(loaded),
                self.plugin_dir,
            )
            return list(self._infos)

    def _load_file(self, path: Path) -> PluginInfo:
        info = PluginInfo(name=path.stem, path=str(path))
        mod_name = f"cybertrade_plugins.{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                raise PluginLoadError(f"cannot create import spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 — quarantine, never crash boot
            info.error = f"{type(exc).__name__}: {exc}"
            log.warning("plugin import failed %s: %s", path.name, info.error)
            return info

        info.version = str(getattr(module, "__version__", ""))
        found = self._extract_strategies(module)
        if not found:
            info.error = "no STRATEGY_CLASS or register() strategies found"
            return info
        for strat in found:
            if not isinstance(strat, Strategy):
                info.error = f"{type(strat).__name__} is not a Strategy"
                continue
            if strat.name in self._strategies:
                info.error = f"duplicate strategy name {strat.name!r}"
                continue
            self._strategies[strat.name] = strat
            info.strategy_names.append(strat.name)
        return info

    def _extract_strategies(self, module: Any) -> List[Any]:
        out: List[Any] = []
        register = getattr(module, "register", None)
        if callable(register):
            try:
                res = register()
                if isinstance(res, (list, tuple)):
                    out.extend(res)
                elif res is not None:
                    out.append(res)
            except Exception as exc:  # noqa: BLE001
                raise PluginLoadError(f"register() raised {exc}") from exc
        cls = getattr(module, "STRATEGY_CLASS", None)
        if cls is not None:
            out.append(cls())
        return out

    # -- access ------------------------------------------------------------
    def strategies(self) -> List[Strategy]:
        with self._lock:
            return list(self._strategies.values())

    def get(self, name: str) -> Optional[Strategy]:
        with self._lock:
            return self._strategies.get(name)

    def infos(self) -> List[PluginInfo]:
        with self._lock:
            return list(self._infos)

    def to_list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [i.to_dict() for i in self._infos]


__all__ = [
    "PluginRegistry",
    "PluginInfo",
    "PluginLoadError",
    "PLUGIN_ENV_DIR",
]
