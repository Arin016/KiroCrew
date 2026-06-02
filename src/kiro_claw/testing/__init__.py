"""Test utilities for KiroClaw consumers.

Everything under ``kiro_claw.testing`` is intended for downstream test
suites that want to exercise code against a populated ``$KIROCLAW_HOME``
without hand-rolling setup. The module is in the runtime wheel (not
``dev_requirements``) so third-party packages can ``pip install kiroclaw``
and use it immediately.

Public entry point:

- :mod:`kiro_claw.testing.fixtures` — ``seeded_home`` plain context manager
  and ``seeded_home_fixture`` pytest fixture.

Import submodules directly (``from kiro_claw.testing.fixtures import ...``);
no top-level re-exports, so the package namespace stays pytest-free for
non-pytest consumers.
"""
