"""Tests for macOS absolute path resolution in handlers_system.py."""

from __future__ import annotations

import sys
from unittest.mock import patch


class TestMacOsSysctlPaths:
    """Verify _SYSCTL and _VM_STAT resolve correctly on macOS."""

    def test_sysctl_resolves_via_which(self) -> None:
        """When shutil.which finds sysctl, use that path."""
        with patch("shutil.which", side_effect=lambda cmd: f"/found/{cmd}" if cmd == "sysctl" else None):
            import importlib

            from kiro_claw.dashboard import handlers_system

            importlib.reload(handlers_system)
            assert handlers_system._SYSCTL == "/found/sysctl"

    def test_sysctl_falls_back_to_usr_sbin(self) -> None:
        """When shutil.which returns None, fall back to /usr/sbin/sysctl."""
        with patch("shutil.which", return_value=None):
            import importlib

            from kiro_claw.dashboard import handlers_system

            importlib.reload(handlers_system)
            assert handlers_system._SYSCTL == "/usr/sbin/sysctl"

    def test_vm_stat_falls_back_to_usr_bin(self) -> None:
        """When shutil.which returns None, fall back to /usr/bin/vm_stat."""
        with patch("shutil.which", return_value=None):
            import importlib

            from kiro_claw.dashboard import handlers_system

            importlib.reload(handlers_system)
            assert handlers_system._VM_STAT == "/usr/bin/vm_stat"

    def test_collect_metrics_returns_mem_on_darwin(self) -> None:
        """On macOS, _collect_system_metrics returns mem_used_gb when commands succeed."""
        if sys.platform != "darwin":
            return  # Skip on non-macOS

        from kiro_claw.dashboard import handlers_system

        with patch.object(handlers_system, "_get_static_system_info", return_value={}):
            handlers_system._metrics_cache = {}
            handlers_system._metrics_cache_ts = 0.0
            data = handlers_system._collect_system_metrics()

        assert "mem_total_gb" in data
        assert "mem_used_gb" in data
        assert data["mem_total_gb"] > 0
        assert data["mem_used_gb"] > 0
