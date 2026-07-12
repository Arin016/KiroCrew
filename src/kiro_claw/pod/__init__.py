"""KiroClaw pod — isolated, throwaway, full-stack test instances per worktree.

A *pod* is an ephemeral KiroClaw gateway booted from one feature worktree's own
``.venv``, on its own deterministic port, with its own ``KIROCLAW_HOME`` (own DB /
sessions / memory), no Slack tunnel, ``--no-crons``, resource-capped, and
``rm -rf``'d on stop. It lets you test a worktree's full stack (backend ``/api/*``
+ the SPA bundle the gateway serves on the same port) **without touching the live
gateway or the shared ``~/.kiroclaw`` data**. Think ``kubectl`` for local worktree
test rigs.

This is the *test line* (multi-active, burn-on-evict). It is orthogonal to the
*live line* (a single gateway serving real data on the canonical port) and refuses
to ever bind the live port.

The user-facing surface is ``kiroclaw pod <verb>`` (see :mod:`kiro_claw.pod.cli`):

    up <wt>        schedule an isolated pod for a worktree  -> {base_url, token}
    down <wt>      evict it (zero residue)
    ls             list running pods                         (kubectl get pods)
    status <wt>    up/down + health
    token <wt>     (re)mint a dashboard token for a running pod
    url <wt>       print its base_url
    logs <wt>      tail its journal
    provision <wt> build the worktree's venv + dist so it can be podded
    install        lay down the systemd template unit (once per machine)

A friendly worktree *name* is resolved to a checkout path git-natively (see
:func:`kiro_claw.pod.runtime.resolve_checkout`) and pinned so the systemd-booted
gateway never re-resolves. Mechanism (Linux ``systemd --user``): a template unit
``kiroclaw-pod@<wt>.service`` whose ``ExecStart`` re-enters ``kiroclaw pod _run <wt>``
(boots the worktree's own gateway) and whose ``ExecStopPost`` ``rm -rf``'s the pod's
isolated HOME. Nothing is shipped outside this Python package.
"""

from __future__ import annotations

from kiro_claw.pod.config import PodConfig
from kiro_claw.pod.runtime import (
    PodError,
    derive_port,
    pod_home,
    pod_unit,
    resolve_checkout,
)

__all__ = [
    "PodConfig",
    "PodError",
    "derive_port",
    "pod_home",
    "pod_unit",
    "resolve_checkout",
]
