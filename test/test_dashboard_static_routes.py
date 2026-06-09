"""Tests for ``kiro_claw.dashboard.server._register_dist_static_routes``.

The dashboard serves the React ``dist/`` build by mounting each present
subdirectory at a fixed URL prefix. The font route in particular is load-
bearing: the self-hosted AWS Diatype woff2 files are referenced by absolute
``url('/fonts/...')`` in ``@font-face``, so without a ``/fonts`` static route
the request falls through to the SPA fallback (``index.html``) and the browser
fails to parse the HTML as a font ("invalid sfntVersion").
"""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from kiro_claw.dashboard.server import _register_dist_static_routes


def _registered_prefixes(app: web.Application) -> set[str]:
    """The set of static-route prefixes wired onto ``app``."""
    prefixes: set[str] = set()
    for resource in app.router.resources():
        info = resource.get_info()
        # aiohttp StaticResource exposes its mount point under "prefix".
        prefix = info.get("prefix")
        if prefix:
            prefixes.add(prefix)
    return prefixes


def _make_dist(root: Path, *subdirs: str) -> Path:
    """Create a fake dist/ dir with the given subdirectories populated."""
    dist = root / "dist"
    dist.mkdir()
    for sub in subdirs:
        (dist / sub).mkdir()
    return dist


def test_fonts_route_registered_when_fonts_dir_present(tmp_path) -> None:
    """A dist/ with a fonts/ subdir gets a /fonts static route."""
    dist = _make_dist(tmp_path, "assets", "fonts")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    prefixes = _registered_prefixes(app)
    assert "/fonts" in prefixes
    assert "/assets" in prefixes


def test_fonts_route_skipped_when_fonts_dir_absent(tmp_path) -> None:
    """No fonts/ subdir -> no /fonts route (only the always-on /assets)."""
    dist = _make_dist(tmp_path, "assets")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    prefixes = _registered_prefixes(app)
    assert "/fonts" not in prefixes
    assert "/assets" in prefixes


def test_optional_subdirs_registered_only_when_present(tmp_path) -> None:
    """sprites/ and vendor/ mount only when they exist; /assets is always on."""
    dist = _make_dist(tmp_path, "assets", "sprites", "fonts", "vendor")
    app = web.Application()

    _register_dist_static_routes(app, dist)

    prefixes = _registered_prefixes(app)
    assert {"/assets", "/sprites", "/fonts", "/vendor"} <= prefixes
