"""Stable command-args and effective-env hashing shared across the MCP gateway.

Kept in its own dependency-free leaf module (only ``hashlib``) so every caller
imports it at module top level. The lightweight ``rewriter`` sits on
``config.loader``'s import path, while ``pool`` and ``stub`` are asyncio/socket
-heavy submodules that must stay unloaded until the gateway is actually enabled
(``test_loader_does_not_import_mcp_gateway_at_module_load``). Routing the shared
hash through this leaf lets the rewriter import it directly without dragging
those heavy submodules into CLI/test/MCP startup.
"""

from __future__ import annotations

import hashlib
from typing import Collection, Mapping


def hash_command(command: str, args: list[str]) -> str:
    """SHA-256 over ``command\\0`` + each ``arg\\0``.

    Single source of truth for the ``command_args_hash`` dimension of
    :class:`kiro_crew.mcp_gateway.pool.PoolKey`. The stub hashes its
    ``--target-command`` + split ``--target-args`` through this to register a
    pool key; the rewriter hashes the same inputs to build the
    ``KIROCREW_MCP_TARGET_<SERVER>__<hash>`` env entry that
    ``gatewayd.env_target_resolver`` looks up by that same key. Both call THIS
    function so the wire-format can never drift between writer and reader.
    """
    h = hashlib.sha256()
    h.update(command.encode("utf-8"))
    h.update(b"\0")
    for a in args:
        h.update(a.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


#: Env-key prefixes treated as ROTATING SECRETS and excluded from the
#: ``effective_env_hash`` PoolKey dimension, so a credential rotation does not
#: split an otherwise-identical pool.
#:
#: The exclusion has a second, security-critical consequence: it makes the hash
#: NON-INJECTIVE over these keys. Two sessions whose only difference is an
#: ``AWS_SECRET*`` value collide onto the same hash and therefore SHARE one
#: backend — so there is no single correct value for a secret-prefixed key in a
#: pooled backend, and one must never be forwarded into it. Servers that need a
#: per-session secret read it from disk (the platform credential helper / the
#: provider's default credential chain, unchanged by pooling) or stay ``poolable: false``.
#:
#: An operator can lift the exclusion for a NAMED variable via
#: ``mcp_gateway.pool_identity_env`` — see the ``identity_keys`` argument of
#: :func:`non_secret_env`. That is not a hole in the reasoning above, it is the
#: reasoning applied in reverse: naming a key makes it part of
#: ``effective_env_hash``, so the hash becomes INJECTIVE over it, two sessions
#: declaring different values no longer collide, and "no single correct value"
#: stops being true for that key. Forwarding it is then safe by exactly the
#: argument that already makes every other hashed key safe to forward.
ENV_SCRUB_PREFIXES: tuple[str, ...] = ("AWS_SECRET", "AWS_SESSION", "OAUTH")


def is_secret_env_key(key: str) -> bool:
    """Return ``True`` if ``key`` is a rotating-secret key.

    Single source of truth for the scrub decision, shared by the stub (which
    excludes these keys when hashing) and by ``gatewayd`` (which excludes them
    when forwarding declared env to a pooled backend). Sharing it is what keeps
    "every forwarded key is also a hashed key" a checkable invariant rather than
    a comment in two files.

    Forwarding applies a SECOND, independent filter on top of this one —
    ``manager.is_credential_env_key`` — so the forwarded set is a strict subset
    of the hashed set: keys the daemon's own credential scrub removes
    (``AWS_ACCESS``, ``SSH_AUTH_SOCK``, ``GNUPGHOME``, ``GIT_ASKPASS``) are in
    the hash but are still never forwarded.
    """
    return any(key.startswith(prefix) for prefix in ENV_SCRUB_PREFIXES)


def non_secret_env(
    env_pairs: Mapping[str, str], *, identity_keys: Collection[str] = ()
) -> dict[str, str]:
    """Return ``env_pairs`` minus every :func:`is_secret_env_key` entry.

    This is the set folded into :func:`hash_effective_env`, and the OUTER bound
    on what may be applied to a shared pooled backend. Because these keys are
    part of the PoolKey, every session sharing a backend agrees on their values,
    so applying them at spawn cannot make one co-tenant observe another's
    configuration.

    It is not sufficient on its own: the forwarding path in ``gatewayd`` also
    drops ``manager.is_credential_env_key`` matches, so a declared credential
    key that the daemon scrub removes is never re-introduced.

    ``identity_keys`` names variables an operator has declared pool-identity-
    relevant (``mcp_gateway.pool_identity_env``). A named key is KEPT even when
    :func:`is_secret_env_key` matches it, which folds its value into the hash and
    so restores the very property the exclusion gives up: two sessions declaring
    different values get different ``effective_env_hash`` values and therefore
    different backends. Matching is by exact name, not by prefix — the point is
    for an operator to accept the rotation-splits-the-pool cost for ONE variable,
    not to disable a whole prefix class.

    Default ``()`` is byte-for-byte today's behaviour: an installation that names
    nothing computes exactly the hash it computed before this argument existed,
    so no existing PoolKey is invalidated.
    """
    keep = frozenset(identity_keys)
    return {k: v for k, v in env_pairs.items() if k in keep or not is_secret_env_key(k)}


#: Env-key prefixes the rewriter writes one entry per stubbed server under,
#: valued ``"cmd arg arg"``. ``MC_MCP_TARGET_`` is the legacy spelling still
#: accepted for overlays written by older versions (#928).
#:
#: Canonical here rather than in ``resolve_once`` (its previous home) because
#: ``manager`` and ``gatewayd`` now both need it to agree on a target-set
#: fingerprint, and a prefix list that two processes compare across a socket is
#: exactly the kind of constant that must have one definition.
TARGET_ENV_PREFIXES: tuple[str, ...] = ("KIROCREW_MCP_TARGET_", "MC_MCP_TARGET_")


def target_env_pairs(env: Mapping[str, str]) -> dict[str, str]:
    """Return only the :data:`TARGET_ENV_PREFIXES` entries of ``env``.

    The set of servers a gatewayd process can resolve a launch command for, in
    the only form that set exists: ``gatewayd.env_target_resolver`` reads these
    keys straight out of ``os.environ``, so they ARE the daemon's routing table.
    """
    return {k: v for k, v in env.items() if any(k.startswith(p) for p in TARGET_ENV_PREFIXES)}


def hash_target_env(env: Mapping[str, str]) -> str:
    """Sorted ``K=V\\0``-delimited SHA-256 over ``env``'s target mappings.

    A daemon's routing table is fixed at spawn (a live process's environment
    cannot be changed), so this fingerprint identifies for its whole lifetime
    which servers it can serve and with which command. ``gatewayd`` reports it
    in its ``pong``; ``manager`` computes it over the environment it is about to
    write agent specs for, and refuses to adopt an incumbent whose value differs
    — the daemon would reject exactly the servers whose entry changed.

    Values are folded in, not just keys: a server whose launch command changed
    is as unservable by the incumbent as one it never had.

    DELIBERATELY OVER-SENSITIVE, and the direction matters. The hash covers every
    target entry, including one the resolver would never read -- a legacy
    ``MC_MCP_TARGET_<SERVER>`` shadowed by a modern key for the same server, or an
    args-disambiguated entry no live pool key asks for. Changing only a shadowed
    entry therefore moves the fingerprint even though every effective route is
    identical, and costs one needless broker replacement at startup.

    That is the safe error. Canonicalising to "effective routes" is not cleanly
    computable here: ``gatewayd.env_target_resolver`` picks between the
    disambiguated, bare and legacy spellings PER LOOKUP, using the requesting
    pool key's ``command_args_hash``, which no one knows at start time. An
    approximation would have to guess, and a guess that collapses two entries the
    resolver would have distinguished fails the other way -- reporting a daemon
    fit when it cannot serve a route -- which is the bug this whole fingerprint
    exists to catch. So the over-sensitivity is kept and paid for in one restart.
    """
    return _hash_pairs(target_env_pairs(env))


def _hash_pairs(pairs: Mapping[str, str]) -> str:
    """Sorted ``K=V\\0``-delimited SHA-256 over ``pairs``.

    The one hashing loop behind :func:`hash_effective_env` and
    :func:`hash_target_env`, shared so the two cannot drift in delimiter or sort
    order. Extracting it is output-preserving by construction — the byte
    sequence fed to SHA-256 is unchanged, so no existing ``PoolKey`` moves.
    """
    h = hashlib.sha256()
    for k in sorted(pairs):
        h.update(k.encode("utf-8"))
        h.update(b"=")
        h.update(pairs[k].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def hash_effective_env(env_pairs: Mapping[str, str], *, identity_keys: Collection[str] = ()) -> str:
    """Sorted ``K=V\\0``-delimited SHA-256 over the NON-SECRET env pairs.

    Feeds the ``effective_env_hash`` dimension of
    :class:`kiro_crew.mcp_gateway.pool.PoolKey`. Implemented on top of
    :func:`non_secret_env` so the hashed set and the forwardable set are the
    same set by construction — including for ``identity_keys``, which widens
    both together and can therefore never widen one without the other.

    WRITER AND READER MUST PASS THE SAME ``identity_keys``. The stub computes
    this hash for its Register frame; ``gatewayd._declared_env_pairs`` recomputes
    it at cold spawn and refuses to forward on a mismatch. That gate is what
    makes the stub's copy of the list untrusted data rather than authority: a
    stub that claims a different set than the daemon's configured one produces a
    hash the daemon does not reproduce, so forwarding fails closed.
    """
    filtered = non_secret_env(env_pairs, identity_keys=identity_keys)
    return _hash_pairs(filtered)
