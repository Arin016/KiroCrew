"""Module Loader — isolated module loading for app hooks and routes.

Loads app modules using importlib.util.spec_from_file_location to avoid
sys.path pollution. Modules are registered in sys.modules with unique
namespaced keys to prevent collisions between apps.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Callable

from kiro_claw.sel import sel

logger = logging.getLogger(__name__)


def _module_namespace(app_name: str, dotted_path: str) -> str:
    """Build a unique sys.modules key for an app module."""
    return f"_kiroclaw_app_{app_name}.{dotted_path}"


def load_app_module(app_name: str, app_dir: Path, module_path: str) -> Callable[..., Any]:
    """Load an app module and return the specified callable.

    Uses importlib.util.spec_from_file_location to load directly from
    the file path, avoiding sys.path manipulation entirely.

    Module is registered in sys.modules as ``_kiroclaw_app_{app_name}.{module_name}``
    to prevent collisions between apps that have identically-named modules.

    Args:
        app_name: The app's identifier.
        app_dir: The app's root directory.
        module_path: Hook path in format ``module.path:callable_name``.

    Returns:
        The callable (function/class) specified by the hook path.

    Raises:
        ImportError: If the module file is not found, escapes the app directory,
                     or the callable doesn't exist in the module.
        ValueError: If the module_path format is invalid.
    """
    # Parse "backend.routes:register_routes" → file path + callable name
    if ":" not in module_path:
        raise ValueError(
            f"Invalid hook path format (missing ':'): {module_path!r}. "
            f"Expected 'module.path:callable_name'"
        )

    dotted_path, callable_name = module_path.rsplit(":", 1)
    if not dotted_path or not callable_name:
        raise ValueError(f"Invalid hook path format: {module_path!r}")

    rel_path = dotted_path.replace(".", "/") + ".py"
    file_path = app_dir / rel_path

    if not file_path.is_file():
        raise ImportError(
            f"Module file not found: {file_path} "
            f"(app={app_name}, hook={module_path})"
        )

    # Path containment check — reject paths that escape the app root
    try:
        resolved = file_path.resolve()
        app_resolved = app_dir.resolve()
        if not resolved.is_relative_to(app_resolved):
            raise ImportError(
                f"Module path escapes app directory: {file_path} "
                f"(resolved to {resolved}, app root is {app_resolved})"
            )
    except (OSError, ValueError) as exc:
        raise ImportError(f"Path resolution failed: {exc}") from exc

    # Unique module name to avoid sys.modules collisions
    unique_name = _module_namespace(app_name, dotted_path)

    # Load the module
    spec = importlib.util.spec_from_file_location(unique_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Failed to create module spec for {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # Clean up on failure
        sys.modules.pop(unique_name, None)
        raise ImportError(
            f"Failed to execute module {file_path}: {exc}"
        ) from exc

    # Get the callable
    if not hasattr(module, callable_name):
        sys.modules.pop(unique_name, None)
        raise ImportError(
            f"Module {file_path} has no attribute {callable_name!r}. "
            f"Available: {[a for a in dir(module) if not a.startswith('_')]}"
        )

    func = getattr(module, callable_name)
    if not callable(func):
        sys.modules.pop(unique_name, None)
        raise ImportError(
            f"{callable_name!r} in {file_path} is not callable "
            f"(got {type(func).__name__})"
        )

    logger.debug(
        "Loaded app module: %s -> %s:%s", unique_name, file_path, callable_name
    )
    sel().log_api_access(
        caller="gateway",
        operation="app_module_load",
        outcome="ok",
        resources=f"{app_name}:{module_path}",
    )
    return func


def unload_app_modules(app_name: str) -> int:
    """Remove all cached modules for an app from sys.modules.

    Called on app disable to ensure re-enable loads fresh code.
    Returns the number of modules removed.
    """
    prefix = f"_kiroclaw_app_{app_name}."
    to_remove = [k for k in sys.modules if k.startswith(prefix)]
    for k in to_remove:
        del sys.modules[k]
    if to_remove:
        logger.debug("Unloaded %d module(s) for app %s", len(to_remove), app_name)
    return len(to_remove)


def is_app_module_loaded(app_name: str) -> bool:
    """Check if any modules are loaded for an app."""
    prefix = f"_kiroclaw_app_{app_name}."
    return any(k.startswith(prefix) for k in sys.modules)
