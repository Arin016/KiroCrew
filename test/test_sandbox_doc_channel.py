"""The document channel that artifact and widget frames load from.

These frames render HTML the model wrote. The channel exists because a ``blob:``
URL is refused outright by some WebKit-based in-app browsers, so the bytes are
served as a real document instead — which means the dashboard's own origin now
has a URL that returns model-authored HTML. What keeps that safe is asserted
here, not assumed:

* the path token is the credential, and it is bound to the requesting client
* every invalid condition fails closed as 404, so the route is not an oracle
* ``Content-Security-Policy: sandbox`` travels on the response, so the document
  has an opaque origin even when opened top-level rather than in a frame
* the stash is bounded, so a long session cannot grow it without limit
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import sandbox_doc as sd


@pytest.fixture(autouse=True)
def _clean_stash():
    with sd._lock:
        sd._stash.clear()
    yield
    with sd._lock:
        sd._stash.clear()


def test_a_token_is_bound_to_the_client_that_minted_it() -> None:
    """The token rides in the frame's own URL, where model script can read it."""
    token = sd._signer.mint("doc1", "10.0.0.1")
    assert sd._signer.verify(token, "doc1", "10.0.0.1")
    assert not sd._signer.verify(token, "doc1", "10.0.0.2"), (
        "a token lifted out of the frame's location worked from another client — "
        "binding it to the minting connection is what makes exfiltration useless"
    )


def test_a_token_does_not_carry_to_another_document() -> None:
    token = sd._signer.mint("doc1", "c")
    assert not sd._signer.verify(
        "doc2", token, "c"
    ), "a token minted for one document authorized another"


def test_an_expired_token_is_refused() -> None:
    exp = int(time.time()) - 1
    token = f"{exp}.{sd._signer._mac(exp, ('doc1', 'c'))}"
    assert not sd._signer.verify(token, "doc1", "c")


@pytest.mark.parametrize(
    "token",
    [
        "",
        "nonsense",
        "9999999999.",
        ".abc",
        "abc.def",
        "9" * 400 + ".x",  # digit-limit blowup guard
    ],
)
def test_malformed_tokens_are_refused_without_raising(token: str) -> None:
    assert sd._signer.verify(token, "doc1", "c") is False


def test_a_tampered_mac_is_refused() -> None:
    token = sd._signer.mint("doc1", "c")
    exp, _, mac = token.partition(".")
    flipped = ("0" if mac[0] != "0" else "1") + mac[1:]
    assert not sd._signer.verify(f"{exp}.{flipped}", "doc1", "c")


def test_the_stash_is_bounded_by_entry_count() -> None:
    """Entries past the in-flight grace window ARE evicted, oldest first.

    The cap is asserted on documents that already had their chance to be
    fetched. An entry still in flight is deliberately exempt — see
    `test_an_in_flight_document_is_never_evicted_to_make_room` — so this ages the
    entries past the grace window, which is exactly when eviction is legitimate.
    """
    now = time.time()
    stale_exp = now + sd._TOKEN_TTL_SECS - sd._IN_FLIGHT_GRACE_SECS - 1
    with sd._lock:
        for i in range(sd._MAX_ENTRIES + 20):
            sd._stash[f"d{i}"] = (stale_exp, "x")
        assert sd._prune(now) is True
        assert len(sd._stash) <= sd._MAX_ENTRIES, (
            "the stash grew past its entry cap; a long dashboard session would "
            "hold every document it ever rendered"
        )
        # Eviction is oldest-first, so the most recent entry must survive.
        assert f"d{sd._MAX_ENTRIES + 19}" in sd._stash


def test_an_unencodable_document_is_refused_at_the_mint() -> None:
    """Encoding must happen BEFORE the single-use pop, not after it.

    A lone surrogate survives JSON and lives happily in a Python str, but UTF-8
    cannot represent it. Encoding at serve time meant the entry was popped and
    THEN the encode raised: a 500 for the frame plus a document that could never
    be fetched again, since the pop had already spent it. The mint refuses it
    instead, while the caller still holds the bytes and can be told why.
    """
    src = sd.__file__ or ""
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    mint = body[
        body.index("async def api_stash_sandbox_doc") : body.index("async def serve_sandbox_doc")
    ]
    assert "UnicodeEncodeError" in mint and "bad_encoding" in mint, (
        "the mint no longer refuses a document it cannot encode, so the failure "
        "moves back to serve time — after the pop has already spent the entry"
    )
    serve = body[body.index("async def serve_sandbox_doc") :]
    assert (
        '.encode("utf-8")' not in serve
    ), "serving encodes again; that is the call that raised AFTER the pop"


def test_the_stash_holds_bytes() -> None:
    """The type is what keeps the encode off the serving path."""
    src = sd.__file__ or ""
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "OrderedDict[str, tuple[float, bytes]]" in body, (
        "the stash went back to holding str, which puts the encode — and its "
        "failure mode — back on the serving path"
    )


def test_the_stash_is_bounded_by_total_bytes() -> None:
    now = time.time()
    stale_exp = now + sd._TOKEN_TTL_SECS - sd._IN_FLIGHT_GRACE_SECS - 1
    big = "x" * (sd._MAX_BYTES // 4)
    with sd._lock:
        for i in range(8):
            sd._stash[f"d{i}"] = (stale_exp, big)
        assert sd._prune(now) is True
        total = sum(len(html) for _, html in sd._stash.values())
        assert total <= sd._MAX_BYTES, "the stash grew past its byte cap"


def test_a_stash_full_of_in_flight_documents_reports_no_room() -> None:
    """The other half of the contract: `_prune` says so instead of evicting.

    The mint turns this False into a 503 and drops its own entry, so the caps
    still hold at the handler even though `_prune` alone can sit above them for
    the length of the grace window.
    """
    now = time.time()
    with sd._lock:
        for i in range(sd._MAX_ENTRIES + 5):
            sd._stash[f"d{i}"] = (now + sd._TOKEN_TTL_SECS, "x")
        assert sd._prune(now) is False, (
            "_prune claimed it made room while every entry was in flight — it "
            "must have evicted a document nobody had fetched yet"
        )


def test_expired_entries_are_pruned() -> None:
    now = time.time()
    with sd._lock:
        sd._stash["stale"] = (now - 1, "x")
        sd._stash["fresh"] = (now + 900, "y")
        sd._prune(now)
        assert "stale" not in sd._stash and "fresh" in sd._stash


def test_the_response_grants_only_what_the_embedding_frame_grants() -> None:
    """The header is what makes a same-origin URL safe for model-authored HTML.

    Without the ``sandbox`` directive, opening the URL top-level would run that
    HTML on the dashboard's own origin with its cookies and storage. The flags
    must also be no WIDER than the embedding iframes': a flag granted here but not
    on the frame lets a document opened top-level do something the same document
    cannot do inside the frame.
    """
    src = sd.__file__ or ""
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    csp_start = body.index('resp.headers["Content-Security-Policy"]')
    csp = body[csp_start : csp_start + 400]
    assert "sandbox allow-scripts" in csp, (
        "the response lost its sandbox directive — a top-level open would then "
        "run model HTML on the dashboard origin"
    )
    for flag in ("allow-popups", "allow-popups-to-escape-sandbox"):
        assert flag in csp, (
            f"the CSP sandbox is missing {flag}, which the embedding iframe grants; "
            "a narrower CSP silently removes a capability widgets already rely on"
        )
    assert "allow-forms" not in csp, (
        "the CSP grants allow-forms, which NEITHER embedding frame grants — a "
        "document opened top-level could then submit forms it cannot submit in "
        "the frame, which is wider than the surface being replaced"
    )
    assert "nosniff" in body and "no-store" in body


def test_an_in_flight_document_is_never_evicted_to_make_room() -> None:
    """The failure mode this replaces was silent, and reachable without an attacker.

    An entry is in flight while nobody has fetched it yet AND for a short grace
    window after the first fetch, so the frame that owns it may re-issue its own
    GET. Dropping such an entry to fit a newer one invalidates a URL some frame is
    about to load -- and because the MINT succeeded, the frontend has no failure
    to show and that frame just renders blank. A gallery of image-bearing
    artifacts can push several MB of pending documents through at once, so this
    is normal use, not an edge case.
    """
    src = sd.__file__ or ""
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    prune = body[body.index("def _prune(") : body.index("def _audit(")]
    assert "_IN_FLIGHT_GRACE_SECS" in prune, (
        "eviction no longer protects an in-flight entry, so a busy gallery can "
        "silently blank an already-minted frame"
    )
    assert "popitem(last=False)" not in prune, (
        "unconditional oldest-first eviction is back — that is the shape that "
        "drops a document nobody has fetched yet"
    )
    mint = body[
        body.index("async def api_stash_sandbox_doc") : body.index("async def serve_sandbox_doc")
    ]
    assert "stash_full" in mint and "503" in mint, (
        "the mint no longer refuses when it cannot be given room; it must fail "
        "visibly rather than evict a live document"
    )


def test_the_stash_caps_still_bound_it() -> None:
    """Protecting in-flight entries must not turn the stash unbounded."""
    src = sd.__file__ or ""
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "_MAX_ENTRIES" in body and "_MAX_BYTES" in body
    prune = body[body.index("def _prune(") : body.index("def _audit(")]
    assert (
        "_MAX_ENTRIES" in prune and "_MAX_BYTES" in prune
    ), "_prune stopped consulting one of its caps"


def test_the_spend_happens_under_the_lock() -> None:
    """Two concurrent GETs must not each start their own grace window.

    The read and the deadline collapse are one critical section: if the write
    were outside the lock, the second GET could restore a deadline the first had
    already shortened, and the URL would stay live for the full mint TTL.
    """
    src = sd.__file__ or ""
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    serve = body[body.index("async def serve_sandbox_doc") :]
    lock_at = serve.index("with _lock:")
    get_at = serve.index("_stash.get(doc_id, None)")
    spend_at = serve.index("min(entry[0], now + _IN_FLIGHT_GRACE_SECS)")
    assert lock_at < get_at, "the stash read is outside the lock"
    assert lock_at < spend_at, "the deadline collapse is outside the lock"


def test_a_served_url_is_spent_rather_than_replayable_for_its_whole_ttl() -> None:
    """The control that holds when the client binding cannot.

    ``request.remote`` is the PROXY's address whenever the dashboard is reached
    through one, so every client shares it and the binding is worth nothing in
    exactly the deployments where a leaked URL is most reachable. What bounds an
    exfiltrated URL is that serving it collapses its deadline to the grace
    window — a plain read that left the 15-minute mint TTL in place would hand a
    co-tenant behind that proxy the whole window to replay it.
    """
    src = sd.__file__ or ""
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    serve = body[body.index("async def serve_sandbox_doc") :]
    assert "min(entry[0], now + _IN_FLIGHT_GRACE_SECS)" in serve, (
        "serving no longer shortens the entry's deadline, so an exfiltrated URL "
        "can be replayed for the full mint TTL by anyone sharing the address"
    )


def test_the_serving_route_is_on_the_auth_bypass_list() -> None:
    """The token is the credential, so the route must bypass session auth — and
    the prefix in the middleware must be the one the handler actually serves."""
    from kiro_crew.dashboard import token_auth

    assert sd.SANDBOX_DOC_PREFIX in token_auth._BYPASS_PREFIXES, (
        "the document route is not on the bypass list, so a sandboxed frame "
        "without a cookie would get a 403 page instead of the document"
    )


# ---------------------------------------------------------------------------
# Behavioural coverage of the two handlers.
#
# The guards above read the module's SOURCE text. That catches a deleted flag,
# but it never runs a request — so nothing above observes that the headers
# actually travel on a response, that the pop really spends the entry, or that a
# refused mint leaves the stash as it was. Those are the properties the channel
# is defended by, so they are exercised here rather than inferred from a grep.
# ---------------------------------------------------------------------------


def _mint_req(payload: object, remote: str = "127.0.0.1") -> MagicMock:
    req = MagicMock()
    req.remote = remote
    req.json = AsyncMock(return_value=payload)
    return req


def _serve_req(doc_id: str, token: str, remote: str = "127.0.0.1") -> MagicMock:
    req = MagicMock()
    req.match_info = {"doc_id": doc_id, "token": token}
    req.remote = remote
    return req


def _split(url: str) -> tuple[str, str]:
    assert url.startswith(sd.SANDBOX_DOC_PREFIX), url
    doc_id, _, token = url[len(sd.SANDBOX_DOC_PREFIX) :].partition("/")
    assert doc_id and token, url
    return doc_id, token


async def _mint(html: str, remote: str = "127.0.0.1") -> tuple[str, str]:
    resp = await sd.api_stash_sandbox_doc(_mint_req({"html": html}, remote))
    assert resp.status == 200, resp.body
    return _split(json.loads(resp.body)["url"])


@pytest.mark.asyncio
async def test_the_same_frame_can_re_issue_its_own_get() -> None:
    """The blank-frame defect this fixes, proven by fetching twice back to back.

    A renderer that re-issues the GET it just made (recovery, restore from tray,
    history navigation — Electron does all three) used to find the document
    already popped. The 404 lands INSIDE the frame, which nothing in the frontend
    observes, and the frame stays at ``opacity: 0`` — a permanently blank area.
    """
    doc_id, token = await _mint("<p>reloaded</p>")

    first = await sd.serve_sandbox_doc(_serve_req(doc_id, token))
    assert first.body == b"<p>reloaded</p>"

    second = await sd.serve_sandbox_doc(_serve_req(doc_id, token))
    assert second.body == b"<p>reloaded</p>"


@pytest.mark.asyncio
async def test_serving_collapses_the_deadline_to_the_grace_window() -> None:
    """The re-issue window is seconds, not the 15 minutes the mint handed out."""
    doc_id, token = await _mint("<p>spent</p>")
    minted_deadline = sd._stash[doc_id][0]

    await sd.serve_sandbox_doc(_serve_req(doc_id, token))

    served_deadline = sd._stash[doc_id][0]
    assert served_deadline < minted_deadline, "serving did not spend the entry"
    assert served_deadline <= time.time() + sd._IN_FLIGHT_GRACE_SECS


@pytest.mark.asyncio
async def test_replaying_inside_the_window_does_not_extend_it() -> None:
    """Otherwise a fetch loop keeps an exfiltrated URL alive indefinitely."""
    doc_id, token = await _mint("<p>no extension</p>")

    await sd.serve_sandbox_doc(_serve_req(doc_id, token))
    after_first = sd._stash[doc_id][0]
    await sd.serve_sandbox_doc(_serve_req(doc_id, token))

    assert sd._stash[doc_id][0] == after_first, "a replay pushed the deadline out"


@pytest.mark.asyncio
async def test_a_url_is_dead_once_the_grace_window_has_passed() -> None:
    """Past the window the entry is refused even though no mint has pruned it."""
    doc_id, token = await _mint("<p>expired</p>")
    await sd.serve_sandbox_doc(_serve_req(doc_id, token))

    with sd._lock:
        exp, html = sd._stash[doc_id]
        sd._stash[doc_id] = (exp - sd._IN_FLIGHT_GRACE_SECS - 1, html)

    with pytest.raises(web.HTTPNotFound):
        await sd.serve_sandbox_doc(_serve_req(doc_id, token))


@pytest.mark.asyncio
async def test_the_served_response_carries_the_sandbox_headers() -> None:
    """Asserted on the RESPONSE, not on the source line that sets it."""
    doc_id, token = await _mint("<p>hi</p>")
    resp = await sd.serve_sandbox_doc(_serve_req(doc_id, token))

    csp = resp.headers["Content-Security-Policy"]
    assert "sandbox allow-scripts" in csp, (
        "the response reached a frame without its sandbox directive — opened "
        "top-level, that HTML would run on the dashboard's own origin"
    )
    assert "frame-ancestors 'self'" in csp
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.content_type == "text/html"


@pytest.mark.asyncio
async def test_the_served_csp_does_not_grant_allow_forms() -> None:
    """Neither embedding frame grants it, so the top-level open must not either."""
    doc_id, token = await _mint("<p>hi</p>")
    resp = await sd.serve_sandbox_doc(_serve_req(doc_id, token))
    assert "allow-forms" not in resp.headers["Content-Security-Policy"]


@pytest.mark.asyncio
async def test_a_document_that_cannot_be_encoded_is_refused_and_not_stashed() -> None:
    """A lone surrogate survives JSON and a Python str; UTF-8 cannot hold it.

    Encoding at serve time popped the entry and THEN raised, which cost a 500
    and the document itself. The refusal has to land at the mint, while the
    caller still holds the bytes — and it must leave nothing behind.
    """
    resp = await sd.api_stash_sandbox_doc(_mint_req({"html": "<p>\ud800</p>"}))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_encoding"
    assert not sd._stash, "a document that was refused still took a slot"


@pytest.mark.asyncio
async def test_an_oversize_document_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(sd, "_MAX_BYTES", 16)
    resp = await sd.api_stash_sandbox_doc(_mint_req({"html": "x" * 17}))
    assert resp.status == 413
    assert json.loads(resp.body)["code"] == "too_large"
    assert not sd._stash


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"html": ""}, {"html": 5}, []])
async def test_a_request_without_usable_html_is_refused(payload: object) -> None:
    resp = await sd.api_stash_sandbox_doc(_mint_req(payload))
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_html"


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_refused() -> None:
    req = MagicMock()
    req.remote = "127.0.0.1"
    req.json = AsyncMock(side_effect=ValueError("not json"))
    resp = await sd.api_stash_sandbox_doc(req)
    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "bad_body"


@pytest.mark.asyncio
async def test_a_mint_with_no_room_is_refused_and_leaves_no_entry(monkeypatch) -> None:
    """Refusing is the visible failure; evicting would be a silent blank frame.

    Every live entry is one nobody has fetched yet, so dropping the oldest to
    make room invalidates a URL a frame is about to load — with the mint having
    succeeded, no failure surfaces anywhere.
    """
    monkeypatch.setattr(sd, "_MAX_ENTRIES", 2)
    now = time.time()
    with sd._lock:
        for i in range(2):
            sd._stash[f"inflight{i}"] = (now + sd._TOKEN_TTL_SECS, b"<p>pending</p>")

    resp = await sd.api_stash_sandbox_doc(_mint_req({"html": "<p>new</p>"}))
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "stash_full"
    assert set(sd._stash) == {"inflight0", "inflight1"}, (
        "the refused mint either evicted an in-flight document or left its own " "entry behind"
    )


@pytest.mark.asyncio
async def test_a_tampered_token_is_refused_without_returning_the_document() -> None:
    """Fails closed BEFORE the stash read, so a probe cannot access the document."""
    doc_id, token = await _mint("<p>hi</p>")
    with pytest.raises(web.HTTPNotFound):
        await sd.serve_sandbox_doc(_serve_req(doc_id, token[:-1] + "x"))
    assert doc_id in sd._stash, "a rejected request removed the entry anyway"


@pytest.mark.asyncio
async def test_an_unknown_document_is_refused_like_any_other_failure() -> None:
    """404 for every invalid condition keeps the route from being an oracle."""
    token = sd._signer.mint("never-stashed", "127.0.0.1")
    with pytest.raises(web.HTTPNotFound):
        await sd.serve_sandbox_doc(_serve_req("never-stashed", token))


@pytest.mark.asyncio
async def test_an_expired_entry_is_refused_even_with_a_valid_token() -> None:
    doc_id, token = await _mint("<p>hi</p>")
    with sd._lock:
        sd._stash[doc_id] = (time.time() - 1, b"<p>hi</p>")
    with pytest.raises(web.HTTPNotFound):
        await sd.serve_sandbox_doc(_serve_req(doc_id, token))


@pytest.mark.asyncio
async def test_serve_path_inline_ttl_check_refuses_stale_entry_without_prune() -> None:
    """The inline TTL guard in serve_sandbox_doc refuses a stale entry even when
    _prune has not run. This is what makes the grace window real rather than
    advisory: the entry still sits in the stash (no mint has happened, so no
    prune has cleared it), but serve_sandbox_doc treats it as absent because its
    deadline is in the past.
    """
    doc_id = "directly-stashed-stale"
    token = sd._signer.mint(doc_id, "127.0.0.1")
    # Insert directly into stash with an expiry in the past -- no mint handler
    # involved, so _prune never ran to clear it.
    with sd._lock:
        sd._stash[doc_id] = (time.time() - 60, b"<p>stale</p>")
    # The entry IS in the stash (not yet pruned)
    assert doc_id in sd._stash
    # But serve_sandbox_doc refuses it because the inline TTL check fires
    with pytest.raises(web.HTTPNotFound):
        await sd.serve_sandbox_doc(_serve_req(doc_id, token))
    # The entry was not removed by the serve path -- it just treated it as absent
    assert doc_id in sd._stash


@pytest.mark.asyncio
async def test_a_token_minted_for_another_client_is_refused() -> None:
    """The cheap second layer, which only bites on a direct connection."""
    doc_id, token = await _mint("<p>hi</p>", remote="10.0.0.1")
    with pytest.raises(web.HTTPNotFound):
        await sd.serve_sandbox_doc(_serve_req(doc_id, token, remote="10.0.0.2"))


def test_both_routes_are_registered() -> None:
    app = web.Application()
    sd.register_sandbox_doc_routes(app)
    routes = {(r.method, str(r.resource.canonical)) for r in app.router.routes()}
    assert ("POST", "/api/sandbox-doc") in routes
    assert ("GET", sd.SANDBOX_DOC_PREFIX + "{doc_id}/{token}") in routes
