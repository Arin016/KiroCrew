"""Config package.

The public config surface (``KiroClawConfig`` and the path helpers) is exposed
lazily via :pep:`562` ``__getattr__`` so that importing a lightweight submodule
such as :mod:`kiro_claw.config.paths` does NOT eagerly pull in the heavy
``kiro_claw.config.loader`` (DTOs, schema validation, the process-global cache,
and the lazily-imported provider factory). ``from kiro_claw.config import X``
continues to work for every name in ``__all__`` — it just resolves on first
access instead of at package import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "KiroClawConfig",
    "config_dir",
    "config_local_path",
    "config_path",
    "env_path",
    "resolve_agent_config_path",
]

if TYPE_CHECKING:  # imported for type checkers only; no runtime loader import
    from kiro_claw.config.loader import (
        KiroClawConfig,
        config_dir,
        config_local_path,
        config_path,
        env_path,
        resolve_agent_config_path,
    )


def __getattr__(name: str) -> object:
    """Resolve public config names lazily from the loader (PEP 562)."""
    if name in __all__:
        # circular import: config.loader imports from kiro_claw.config.paths,
        # which triggers this package __init__ — a top-level import of loader
        # here would create an init <-> loader runtime cycle AND eagerly pull the
        # heavy loader in whenever the config package is touched (including from
        # the lightweight config.paths leaf), defeating this PEP 562 lazy seam.
        from kiro_claw.config import loader

        return getattr(loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
