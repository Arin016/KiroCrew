"""Tests for carrying a Kiro Crew agent onto a KAS session.

KAS has no ``--agent`` flag, so the agent has to be named AND defined in
``session/new``. The failure this guards is silent: KAS binds ``modeId`` only to
an agent already in its registry and ignores an unresolvable name rather than
rejecting it, so selecting without defining yields a completely successful
``session/new`` that runs KAS's own default mode -- with none of the agent's
prompt, tool grants or MCP servers in effect and every log line looking healthy.

Measured against the real KAS that kiro-cli extracts: ``modeId`` alone came back
with ``configOptions.currentValue == "vibe"``; sending the definition alongside it
came back ``"kirocrew"``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp import kas_agent, kas_auth
from kiro_crew.acp._dispatch import build_session_new_params


class TestTokenEnvelopeParsing:
    """The host mediates KAS's token, so every malformed reply must be caught here.

    KAS rejects a response without ``accessToken`` or with an unparseable
    ``expiresAt``, and its refusal surfaces as an opaque protocol error far from
    the cause — so the shaping is validated on this side.
    """

    def test_last_json_object_wins_over_progress_lines(self) -> None:
        """The subcommand may print progress before its result envelope."""
        out = 'refreshing...\nnot json\n{"kind":"x","data":{"a":1}}\n'

        assert kas_auth.parse_last_json_line(out) == {"kind": "x", "data": {"a": 1}}

    def test_no_json_object_raises_the_user_facing_error(self) -> None:
        for out in ("", "   \n", "plain text\n", "[1, 2]\n"):
            with pytest.raises(kas_auth.KasTokenError):
                kas_auth.parse_last_json_line(out)

    def test_complete_envelope_is_shaped_for_kas(self) -> None:
        """``provider`` must be forwarded: KAS otherwise infers enterprise status
        from ``profileArn`` presence and fail-closes governance."""
        resp = kas_auth.response_from_envelope(
            {
                "kind": kas_auth._OK_KIND,
                "data": {
                    "accessToken": "tok",
                    "expiresAt": "2026-08-13T18:07:11Z",
                    "profileArn": "arn:aws:x",
                    "provider": "Internal",
                },
            }
        )

        assert resp["accessToken"] == "tok"
        assert resp["expiresAt"] == "2026-08-13T18:07:11Z"
        assert resp["profileArn"] == "arn:aws:x"
        assert resp["provider"] == "Internal"
        # Omitted-not-empty: Builder ID / IdC / Social send no authMethod at all.
        assert "authMethod" not in resp

    def test_incomplete_or_bad_envelopes_all_raise(self) -> None:
        bad: list[dict[str, object]] = [
            {"kind": "error", "data": "no credentials"},
            {"kind": "surprise", "data": {}},
            {"kind": kas_auth._OK_KIND, "data": "not-an-object"},
            {"kind": kas_auth._OK_KIND, "data": {"expiresAt": "2026-01-01T00:00:00Z"}},
            {"kind": kas_auth._OK_KIND, "data": {"accessToken": "tok"}},
        ]
        for envelope in bad:
            with pytest.raises(kas_auth.KasTokenError):
                kas_auth.response_from_envelope(envelope)


class TestFetchAccessToken:
    @pytest.mark.asyncio
    async def test_unspawnable_binary_raises_token_error(self) -> None:
        """Every failure mode raises KasTokenError so callers catch one thing."""
        with pytest.raises(kas_auth.KasTokenError):
            await kas_auth.fetch_access_token("/nonexistent/kiro-cli-binary")

    @pytest.mark.asyncio
    async def test_successful_run_returns_the_shaped_response(self, monkeypatch) -> None:
        envelope = '{"kind":"getKasToken","data":{"accessToken":"tok","expiresAt":"2026-08-13T18:07:11Z"}}'

        class _Proc:
            returncode = 0

            async def communicate(self):
                return envelope.encode("utf-8"), b""

        async def _exec(*_a, **_k):
            return _Proc()

        monkeypatch.setattr(kas_auth.asyncio, "create_subprocess_exec", _exec)

        resp = await kas_auth.fetch_access_token("kiro-cli")

        assert resp["accessToken"] == "tok"

    @pytest.mark.asyncio
    async def test_timeout_kills_the_child_before_raising(self, monkeypatch) -> None:
        """A live child would hold the cross-process refresh lock and wedge every
        other host waiting on it."""
        killed: list[bool] = []

        class _Hang:
            async def communicate(self):
                raise AssertionError("communicate should be cancelled by wait_for")

            def kill(self):
                killed.append(True)

            async def wait(self):
                return 0

        async def _exec(*_a, **_k):
            return _Hang()

        async def _timeout(awaitable, *_a, **_k):
            # Close the coroutine we are refusing to await, or Python reports it as
            # a never-awaited coroutine and the warning outlives this test.
            awaitable.close()
            # kas_auth.asyncio.TimeoutError, NOT the builtin: they are the same
            # class only from 3.11 on. On 3.10 asyncio.TimeoutError is
            # concurrent.futures.TimeoutError, so raising the builtin escapes the
            # source's ``except asyncio.TimeoutError`` and fails that shard alone.
            raise kas_auth.asyncio.TimeoutError

        monkeypatch.setattr(kas_auth.asyncio, "create_subprocess_exec", _exec)
        monkeypatch.setattr(kas_auth.asyncio, "wait_for", _timeout)

        with pytest.raises(kas_auth.KasTokenError):
            await kas_auth.fetch_access_token("kiro-cli")

        assert killed == [True], "the timed-out child must be killed"


class TestTokenCallbackIsBackendGated:
    """Only a KAS runtime may answer the credential callback.

    The method name alone is not authorization: any ACP child can emit it, and
    answering would hand a live access token to a process that was never meant to
    have one. The classifier deliberately has NO case for it, so on a non-KAS
    transport it falls through to the unknown-request path and is refused there.
    """

    def test_classifier_has_no_case_for_the_auth_method(self) -> None:
        from kiro_crew.acp import kas_auth
        from kiro_crew.acp._dispatch import classify_notification
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method=kas_auth.GET_ACCESS_TOKEN_METHOD, params={}, id="req-1", result=None
        )

        # Unknown => answered with -32601 by the dispatch sites, never served.
        assert classify_notification(msg) == "server_request_unknown"

    def test_capability_membership_decides_who_may_be_answered(self) -> None:
        """kiro-cli must NOT be a member: it resolves its own credentials.

        Membership is the authorization. A backend outside the set has no code
        path that reaches the handler, which is stronger than a comparison
        evaluated per frame — and it keeps the shared runtime harness-agnostic
        (harness-parity H13).
        """
        from kiro_crew.acp.types import (
            ACP_BACKEND_KAS,
            ACP_BACKEND_KIRO,
            ACP_BACKENDS_HOST_MEDIATED_AUTH,
        )

        assert ACP_BACKEND_KAS in ACP_BACKENDS_HOST_MEDIATED_AUTH
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_HOST_MEDIATED_AUTH

    def test_reader_loop_gates_on_the_capability_not_a_harness_name(self) -> None:
        """The shared loop reads a capability flag, never a backend comparison.

        Pinned on the source because the security property and the parity rule
        pull the same way here: an inline ``== ACP_BACKEND_KAS`` would both name a
        harness on the shared Kiro path and re-derive authorization at the call
        site instead of reading the one set that owns it.
        """
        import inspect

        from kiro_crew.acp import runtime

        src = inspect.getsource(runtime.AcpRuntime._reader_loop)
        idx = src.find("GET_ACCESS_TOKEN_METHOD")
        assert idx > 0, "the auth-callback branch moved; re-point this guard"
        window = src[max(0, idx - 400) : idx]
        assert "_answers_auth_callback" in window, "callback branch is not gated"
        assert "ACP_BACKEND_KAS" not in window, "shared loop must not name a harness"

    def test_the_flags_are_resolved_from_the_named_sets(self) -> None:
        """A flag hand-set to True would bypass the set that owns the decision."""
        import inspect

        from kiro_crew.acp import runtime

        src = inspect.getsource(runtime.AcpRuntime.__init__)
        assert "ACP_BACKENDS_HOST_MEDIATED_AUTH" in src
        assert "ACP_BACKENDS_CLIENT_DEFINED_AGENT" in src


class TestUnresolvableAgentRefusesTheSession:
    """An unresolvable agent must RAISE, never fall through to session/new.

    Sending session/new without the definition does not degrade politely: KAS
    binds no mode and runs its own default, which carries BROADER tool access than
    the configured agent. The raise is the only thing standing between a config
    typo and a silent privilege widening, so it is pinned rather than left to the
    reader loop's error path.
    """

    @pytest.mark.asyncio
    async def test_create_session_raises_when_the_agent_cannot_be_resolved(
        self, monkeypatch
    ) -> None:
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KAS)
        rt._initialized = True
        rt._process = object()  # only truthiness is checked before the refusal
        assert rt._defines_agent_client_side, "precondition: KAS defines its agent"

        # The definition cannot be loaded — the case a config typo produces.
        monkeypatch.setattr(runtime_mod, "load_client_custom_agent", lambda *a, **k: None)
        sent: list[str] = []

        async def _record(method, params, timeout=None):
            sent.append(method)
            return {"sessionId": "sid"}

        monkeypatch.setattr(rt, "_send_and_await", _record)

        with pytest.raises(AcpRuntimeError):
            await rt.create_session(cwd="/w", agent="missing-agent")

        assert sent == [], "session/new must NOT be sent without the definition"


class TestBackendStaysHiddenWhileUnderTest:
    """The wiring lands; the exposure does not.

    Worth pinning separately because the two are independent: with the agent
    wiring in place it is tempting to widen the selectable set in the same change,
    and that is the line between "ready to test" and "shipped".
    """

    def test_kas_is_not_offered_by_default(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS, selectable_backends

        assert ACP_BACKEND_KAS not in selectable_backends()

    def test_only_a_truthy_optin_opens_the_gate(self, monkeypatch) -> None:
        """A falsy value is an operator turning the preview OFF, not setting it.

        Gating on "the variable is non-empty" admits ``=0`` and ``=false``, which
        would launch an under-test backend — and hand it an access token — for
        someone who explicitly disabled it. Routed through the shared
        ``env_flag_enabled`` predicate rather than a hand-rolled check so this
        agrees with every other flag in the codebase.
        """
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ENV_KAS_PREVIEW, selectable_backends

        for off in ("0", "false", "False", "no", "off", "", "  "):
            monkeypatch.setenv(ENV_KAS_PREVIEW, off)
            assert ACP_BACKEND_KAS not in selectable_backends(), off
        for on in ("1", "true", "TRUE", "yes", "on", " 1 "):
            monkeypatch.setenv(ENV_KAS_PREVIEW, on)
            assert ACP_BACKEND_KAS in selectable_backends(), on

    def test_kas_is_listed_in_the_schema_domain(self) -> None:
        """The enum is the JSON-Schema domain, NOT the exposure surface.

        ``validate_config_data`` REMOVES a value outside this enum before the
        loader sees the key, so listing 'kas' is what lets the preview opt-in take
        effect at all — without it the value is stripped, not degraded, and the
        degrade log never even fires. Exposure is controlled by
        ``selectable_backends()`` above, and no settings surface reads this field.
        """
        from kiro_crew.config.loader import AgentConfig

        meta = AgentConfig.__dataclass_fields__["acp_backend"].metadata
        assert "kas" in (meta.get("enum") or [])

    def test_no_settings_surface_reads_the_field(self) -> None:
        """Pins the reason listing 'kas' in the enum exposes nothing.

        If a frontend panel ever starts rendering this field, the enum stops being
        purely a schema domain and this test is the reminder to gate it there too.
        """
        import subprocess

        repo = Path(__file__).resolve().parents[1]
        hits = subprocess.run(
            ["grep", "-rl", "acp_backend", str(repo / "website" / "src")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert hits.stdout.strip() == "", f"frontend now reads acp_backend: {hits.stdout}"

    def test_preview_env_opens_the_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ENV_KAS_PREVIEW, selectable_backends

        monkeypatch.setenv(ENV_KAS_PREVIEW, "1")

        assert ACP_BACKEND_KAS in selectable_backends()

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_preview_value_does_not_open_the_set(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exported-but-empty variable is the shell's idea of unset."""
        from kiro_crew.acp.types import ACP_BACKEND_KAS, ENV_KAS_PREVIEW, selectable_backends

        monkeypatch.setenv(ENV_KAS_PREVIEW, value)

        assert ACP_BACKEND_KAS not in selectable_backends()

    def test_claude_stays_unselectable_either_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate opens KAS specifically, not the dormant claude seam."""
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ENV_KAS_PREVIEW, selectable_backends

        monkeypatch.setenv(ENV_KAS_PREVIEW, "1")

        assert ACP_BACKEND_CLAUDE not in selectable_backends()


class TestAgentConfigResolution:
    """Resolution follows dispatch's own precedence, not a filename guess.

    Load matters more now that a missing config REFUSES the session: a lookup that
    misses a legitimate project-local agent turns a working setup into a hard
    failure rather than a silent widening.
    """

    def test_project_local_config_wins(self, tmp_path: Path, monkeypatch) -> None:
        proj = tmp_path / "proj" / ".kiro" / "agents"
        proj.mkdir(parents=True)
        (proj / "a.json").write_text(json.dumps({"prompt": "from project"}))
        globals_dir = tmp_path / "global"
        globals_dir.mkdir()
        (globals_dir / "a.json").write_text(json.dumps({"prompt": "from global"}))
        monkeypatch.setattr(kas_agent, "kiro_agents_dir", lambda: globals_dir)

        out = kas_agent.load_client_custom_agent("a", cwd=tmp_path / "proj")

        assert out is not None
        assert out["prompt"] == "from project"

    def test_global_config_is_the_fallback(self, tmp_path: Path, monkeypatch) -> None:
        globals_dir = tmp_path / "global"
        globals_dir.mkdir()
        (globals_dir / "a.json").write_text(json.dumps({"prompt": "from global"}))
        monkeypatch.setattr(kas_agent, "kiro_agents_dir", lambda: globals_dir)

        out = kas_agent.load_client_custom_agent("a", cwd=tmp_path / "empty")

        assert out is not None
        assert out["prompt"] == "from global"

    def test_declared_name_wins_over_filename(self, tmp_path: Path, monkeypatch) -> None:
        """kiro-cli dispatches on the spec's `name`, not its stem.

        A renamed or package-prefixed agent has a filename matching nothing, so a
        stem comparison accepts exactly the files whose author happened not to
        rename them — and since resolution is fail-closed, the rest break outright.
        """
        globals_dir = tmp_path / "global"
        globals_dir.mkdir()
        (globals_dir / "some-file-name.json").write_text(
            json.dumps({"name": "declared-agent", "prompt": "p"})
        )
        monkeypatch.setattr(kas_agent, "kiro_agents_dir", lambda: globals_dir)

        assert kas_agent.load_client_custom_agent("declared-agent") is not None
        # The stem must NOT resolve: it is not a dispatchable name.
        assert kas_agent.load_client_custom_agent("some-file-name") is None

    def test_absent_everywhere_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(kas_agent, "kiro_agents_dir", lambda: tmp_path)

        assert kas_agent.load_client_custom_agent("nope", cwd=tmp_path) is None


class TestSessionNewParams:
    def test_kas_agent_is_selected_and_defined_together(self) -> None:
        """Both keys, one namespace: the definition registers, modeId selects."""
        definition = {"id": "kirocrew", "prompt": "p"}

        params = build_session_new_params("/w", mcp_servers=[], kas_agent=definition)

        assert params["_meta"] == {
            "kiro": {"modeId": "kirocrew", "customAgents": [definition]}
        }

    def test_mode_id_is_taken_from_the_definition_not_a_second_argument(self) -> None:
        """One source for the name, so selection cannot disagree with definition."""
        params = build_session_new_params(
            "/w", mcp_servers=[], kas_agent={"id": "other", "prompt": "p"}
        )

        assert params["_meta"]["kiro"]["modeId"] == "other"

    def test_absent_kas_agent_sends_no_meta(self) -> None:
        """kiro-cli gets its agent from --agent; an empty _meta would be noise."""
        params = build_session_new_params("/w", mcp_servers=[])

        assert "_meta" not in params

    def test_cwd_and_mcp_servers_are_still_always_present(self) -> None:
        """kiro-cli treats a missing mcpServers as malformed and exits rc=0."""
        params = build_session_new_params("/w", kas_agent={"id": "a", "prompt": "p"})

        assert params["cwd"] == "/w"
        assert params["mcpServers"] == []

    def test_claude_meta_wins_over_kas_agent(self) -> None:
        """The two backends are mutually exclusive; one _meta slot, no merging."""
        params = build_session_new_params(
            "/w", mcp_servers=[], claude_meta=True, kas_agent={"id": "a", "prompt": "p"}
        )

        assert params["_meta"] == {"claudeCode": {"options": {}}}


class TestClientCustomAgent:
    def test_id_and_prompt_are_the_required_core(self) -> None:
        out = kas_agent.client_custom_agent("kirocrew", {"prompt": "be helpful"})

        assert out == {"id": "kirocrew", "prompt": "be helpful"}

    def test_star_tools_stay_the_wildcard(self) -> None:
        """KAS accepts "*" or a list; the wildcard must not become ["*"]."""
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "tools": "*"})

        assert out["tools"] == "*"

    def test_tool_list_is_passed_through(self) -> None:
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "tools": ["fs_read"]})

        assert out["tools"] == ["fs_read"]

    def test_empty_tool_list_is_forwarded_not_omitted(self) -> None:
        """An empty list means "no tools" — omitting it WIDENS the grant.

        KAS applies its own broader default access when the key is absent, so
        dropping a deliberately empty list turns a narrow grant into a wide one.
        Only a missing value means "unspecified".
        """
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "tools": []})

        assert out["tools"] == []

    def test_absent_tools_key_is_omitted(self) -> None:
        out = kas_agent.client_custom_agent("a", {"prompt": "p"})

        assert "tools" not in out

    def test_allowed_tools_is_never_forwarded(self) -> None:
        """allowedTools is an approval concern Kiro Crew's own gate owns.

        Forwarding it as tool ACCESS would widen what the agent can reach beyond
        what its `tools` grant says.
        """
        out = kas_agent.client_custom_agent(
            "a", {"prompt": "p", "tools": ["fs_read"], "allowedTools": ["execute_bash"]}
        )

        assert out["tools"] == ["fs_read"]
        assert "allowedTools" not in out

    @pytest.mark.parametrize("key", ["description", "model", "mcpServers"])
    def test_shared_keys_pass_through(self, key: str) -> None:
        out = kas_agent.client_custom_agent("a", {"prompt": "p", key: {"x": 1}})

        assert out[key] == {"x": 1}

    def test_resources_are_never_forwarded(self) -> None:
        """KAS resolves ``resources`` in its OWN process, past Crew's path gate.

        Forwarding them lets an agent config carrying
        ``resources: ["file://~/.aws/credentials"]`` pull the file into model
        context. It cannot be validated here without reimplementing KAS's resolver
        — an entry is either a bare ``file://``/``skill://`` URI or a
        ``knowledgeBase`` object whose ``include`` holds GLOB patterns — and a
        validator covering only the bare-URI form would read as a gate while the
        glob form walked through.
        """
        out = kas_agent.client_custom_agent(
            "a",
            {
                "prompt": "p",
                "resources": [
                    "file://~/.aws/credentials",
                    {"type": "knowledgeBase", "source": "/etc", "include": ["**/*"]},
                ],
            },
        )

        assert "resources" not in out
        assert "resources" not in kas_agent._PASSTHROUGH_KEYS

    def test_falsy_optional_values_are_omitted(self) -> None:
        """KAS reads its own defaults for an absent key, not for an empty one."""
        out = kas_agent.client_custom_agent("a", {"prompt": "p", "model": ""})

        assert set(out) == {"id", "prompt"}


class TestLoadClientCustomAgent:
    """Reading the config is best-effort: an unusable one degrades, never raises.

    Raising would fail session/new outright and take down a surface over a
    configuration problem the operator can fix without a restart.
    """

    @pytest.fixture(autouse=True)
    def _agents_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        d = tmp_path / "agents"
        d.mkdir()
        monkeypatch.setattr(kas_agent, "kiro_agents_dir", lambda: d)
        return d

    def test_reads_and_shapes_a_config(self, _agents_dir: Path) -> None:
        (_agents_dir / "kirocrew.json").write_text(
            json.dumps({"prompt": "hello", "tools": "*", "description": "d"})
        )

        out = kas_agent.load_client_custom_agent("kirocrew")

        assert out == {"id": "kirocrew", "prompt": "hello", "tools": "*", "description": "d"}

    def test_resolves_a_file_uri_prompt(self, _agents_dir: Path, tmp_path: Path) -> None:
        """KAS rejects a file:// URI here and makes resolution the client's job."""
        prompt_file = tmp_path / "p.md"
        prompt_file.write_text("from a file")
        (_agents_dir / "a.json").write_text(
            json.dumps({"prompt": f"file://{prompt_file}"})
        )

        out = kas_agent.load_client_custom_agent("a")

        assert out is not None
        assert out["prompt"] == "from a file"

    def test_a_sensitive_prompt_path_is_refused(self, _agents_dir: Path) -> None:
        """A prompt URI is agent-config data, so it must not bypass path controls.

        Without ``safe_read_file`` a config carrying
        ``file:///home/u/.aws/credentials`` would put credential contents into the
        agent's system prompt and straight into the model. Asserted through the
        real gate rather than a mock, so a future refactor that drops it fails.
        """
        blocked = Path.home() / ".aws" / "credentials"
        (_agents_dir / "a.json").write_text(json.dumps({"prompt": f"file://{blocked}"}))

        assert kas_agent.load_client_custom_agent("a") is None

    def test_unresolvable_file_uri_degrades(self, _agents_dir: Path) -> None:
        (_agents_dir / "a.json").write_text(json.dumps({"prompt": "file:///nope/x.md"}))

        assert kas_agent.load_client_custom_agent("a") is None

    def test_every_authority_form_resolves(self, tmp_path: Path) -> None:
        """One assertion per row of ``_file_uri_to_path``'s authority table.

        Asserted against the real function rather than a reconstruction
        expression: the earlier tests re-implemented the concatenation they were
        checking, so they stayed green while the function mishandled ``.``.
        Properties rather than exact strings, because ``url2pathname`` uses the
        host separator and CI runs a Windows shard.
        """
        base = tmp_path / "cfgdir"
        base.mkdir()
        f = kas_agent._file_uri_to_path

        # No authority, and the localhost spelling RFC 8089 treats as identical.
        # Asserted on PARTS, not on a rebuilt string: on Windows url2pathname
        # anchors a POSIX-style absolute path to the current DRIVE, so
        # "file:///abs/p.md" yields "C:\\abs\\p.md" while str(Path("/abs/p.md"))
        # yields "\\abs\\p.md". Comparing parts covers both hosts without this
        # test re-implementing the transform it is checking.
        for uri in ("file:///abs/p.md", "file://localhost/abs/p.md"):
            got = Path(f(uri, base) or "")
            assert got.is_absolute(), uri
            assert got.parts[-2:] == ("abs", "p.md"), uri
        # A Windows drive mis-parsed as a host: the drive must survive.
        assert "C:" in (f("file://C:/proj/p.md", base) or "")
        # A real remote host stays UNC instead of becoming the local /share/p.md.
        unc = f("file://server/share/p.md", base) or ""
        assert "server" in unc and unc.startswith(("//", "\\\\"))
        # Relative forms resolve against the config's directory, never the cwd.
        for uri in ("file://./p.md", "file:p.md"):
            assert f(uri, base) == str(base / "p.md"), uri
        assert Path(f("file://../p.md", base) or "").name == "p.md"

    def test_malformed_uri_degrades_instead_of_raising(self, tmp_path: Path) -> None:
        """``urlparse`` RAISES on a bracket it reads as a bad IPv6 authority.

        The URI is hand-written in an agent config, so a typo must degrade to "no
        prompt" like any other unreadable reference. An uncaught ValueError here
        aborts session creation instead.
        """
        for bad in ("file://[bad/path", "file://a]b/p.md"):
            assert kas_agent._file_uri_to_path(bad, tmp_path) is None, bad

    def test_relative_uri_never_falls_back_to_the_cwd(self) -> None:
        """With no base directory a relative reference is REFUSED, not guessed.

        Resolving against the gateway's working directory would inline whatever
        file happens to sit there into the agent's system prompt.
        """
        assert kas_agent._file_uri_to_path("file://./p.md", None) is None
        assert kas_agent._file_uri_to_path("file:p.md", None) is None

    def test_relative_prompt_resolves_beside_its_config(self, _agents_dir: Path) -> None:
        """End to end: the prompt file sits next to the config that names it."""
        (_agents_dir / "prompt.md").write_text("beside-config", encoding="utf-8")
        (_agents_dir / "a.json").write_text(json.dumps({"prompt": "file://./prompt.md"}))

        agent = kas_agent.load_client_custom_agent("a")

        assert agent is not None
        assert agent["prompt"] == "beside-config"

    def test_posix_file_uri_is_read(self, tmp_path: Path) -> None:
        target = tmp_path / "p.md"
        target.write_text("posix-form", encoding="utf-8")

        assert kas_agent._read_file_uri(f"file://{target}") == "posix-form"

    def test_missing_config_degrades(self) -> None:
        assert kas_agent.load_client_custom_agent("absent") is None

    def test_malformed_json_degrades(self, _agents_dir: Path) -> None:
        (_agents_dir / "a.json").write_text("{not json")

        assert kas_agent.load_client_custom_agent("a") is None

    def test_non_object_config_degrades(self, _agents_dir: Path) -> None:
        (_agents_dir / "a.json").write_text("[1, 2]")

        assert kas_agent.load_client_custom_agent("a") is None

    @pytest.mark.parametrize("prompt", ["", "   ", None])
    def test_promptless_config_degrades(self, _agents_dir: Path, prompt: object) -> None:
        """KAS requires prompt; sending an empty one would define a mute agent."""
        (_agents_dir / "a.json").write_text(json.dumps({"prompt": prompt, "tools": "*"}))

        assert kas_agent.load_client_custom_agent("a") is None

    @pytest.mark.parametrize("name", ["", "   "])
    def test_blank_agent_name_needs_no_filesystem_hit(self, name: str) -> None:
        assert kas_agent.load_client_custom_agent(name) is None
