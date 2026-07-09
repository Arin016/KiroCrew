"""Full new-path dispatch: WeComTransport -> TurnDriver -> WeComRenderer.

``WeComTransport.receive()`` authorizes + normalizes an inbound WS frame and
hands the ``WeComInbound`` (carrying the WS routing keys ``req_id`` /
``response_url``) to :meth:`WeComDispatcher.handle_message`, which mirrors the
Slack/Telegram transport dispatch:

    command intercept (/new, /compact)
    -> construct WeComRenderer + on_turn_start (immediate "🤔 …" placeholder)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

WeCom has no interactive buttons, so the dispatcher runs the driver
``decider``-less (deny-by-default for INTERACTIVE mode; ``auto``/``trust``
still work) and has no callback handler. The security ``tool_gate`` and the
``spawn_run`` auto-approve are wired inline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_claw.slack``.

Dependency direction is ``wechat -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_claw.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_claw.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_claw.messaging.link import canonical_key
from kiro_claw.sel import sel
from kiro_claw.wechat.client import new_stream_id
from kiro_claw.wechat.commands import ConversationState, parse_command
from kiro_claw.wechat.renderer import WeComRenderer
from kiro_claw.wechat.transport import WECOM_CAPABILITIES

if TYPE_CHECKING:
    from kiro_claw.config.loader import KiroClawConfig
    from kiro_claw.context import ContextBuilder
    from kiro_claw.history import ConversationLog
    from kiro_claw.session import SessionManager
    from kiro_claw.wechat.client import WeComClient, WeComInbound

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so WeCom sessions load kiroclaw-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the Slack /
# Telegram paths' _DEFAULT_KIROCLAW_AGENT.
_DEFAULT_KIROCLAW_AGENT = "kiroclaw"


class WeComDispatcher:
    """Coordinates WeCom turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-userid conversation state
    (generation counter + soft-threshold flag). ``handle_message`` is wired as
    the transport's dispatch callback. ``client`` is set by the gateway after
    construction.
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        ctx_builder: "ContextBuilder",
        cfg: "KiroClawConfig",
        owner_id: str = "",
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self.owner_id = owner_id
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "WeComClient | None" = None
        self._conv = ConversationState()

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(self, inbound: "WeComInbound") -> None:
        """Drive one authorized inbound WeCom message through TurnDriver."""
        assert self.client is not None, "WeComDispatcher.client must be set"
        userid = inbound.userid
        text = inbound.text
        logger.info("WeCom inbound from %s: %d chars", userid, len(text or ""))

        # ── Command intercept (no LLM session needed) ──
        cmd = parse_command(text)
        if cmd == "new":
            self._conv.bump_gen(userid)
            await self.client.send_reply(inbound.response_url, "✅ 已开始新对话")
            return
        if cmd == "compact":
            self._conv.clear_awaiting(userid)
            await self._handle_compact(inbound)
            return

        session_key = self._session_key(userid)
        channel_id = f"wecom:{userid}"
        # Resolve the kiro-cli agent: an explicit override wins, else the
        # configured default, else the canonical "kiroclaw" agent -- so the
        # session loads kiroclaw-core (spawn_run) instead of kiro-cli's bare
        # built-in default. Mirrors slack/telegram transport_dispatch.
        agent = self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCLAW_AGENT

        # WeCom has no interactive buttons -> no decider (deny-by-default for
        # INTERACTIVE; auto/trust still auto-approve via the driver ladder).
        renderer = WeComRenderer(
            self.client,
            inbound.req_id,
            inbound.response_url,
            WECOM_CAPABILITIES,
            session_key=session_key,
        )

        # Everything acquire-dependent runs INSIDE the try so the finally always
        # finalizes the placeholder (renderer.close -> no perma-"🤔 …"), even if
        # get_or_create itself raises on a cold-start failure. release() is gated
        # on _acquired so we never release a semaphore we didn't hold. Mirrors
        # slack/telegram transport_dispatch.
        _acquired = False
        try:
            # Ack placeholder first (before the potentially slow cold-start);
            # on_turn_start is idempotent so the driver's later call no-ops.
            await renderer.on_turn_start()
            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key, agent=agent, channel_id=channel_id
            )
            _acquired = True
            if is_new:
                await self.sessions.set_channel(session_key, channel_id)
            full_message, _ = self.ctx_builder.build_message(
                text,
                is_new,
                session_key,
                channel_id=channel_id,
                agent=agent,
                resumed=resumed,
            )

            # PreToolUse security gate (channel-neutral, off ctx_builder.hooks):
            # sensitive-path keystone + governance ceiling + deny-list. Returns
            # "deny" (un-overridable), "auto_approve", or "" (passthrough).
            def _tool_gate(event: Any) -> str:
                result = self.ctx_builder.hooks.on_tool_call(
                    getattr(event, "title", "") or "",
                    session_key=session_key,
                    agent=agent,
                    tool_kind=getattr(event, "tool_kind", "") or "",
                    raw_params=getattr(event, "raw_tool_params", None),
                )
                if result.action == TOOL_DENY:
                    return "deny"
                if result.action == TOOL_AUTO_APPROVE:
                    return "auto_approve"
                return ""

            driver = TurnDriver(
                provider,
                renderer,
                approval_mode=self.approval_mode,
                decider=None,  # WeCom can't render approve/deny buttons
                # Preserve the auto_approve_subagent_spawn hook for spawn_run
                # (replicated inline to avoid a wechat -> slack import).
                auto_approve_tool=lambda title: bool(
                    self.ctx_builder
                    and self.ctx_builder.hooks
                    and self.ctx_builder.hooks.auto_approve_subagent_spawn
                    and title == "spawn_run"
                ),
                tool_gate=_tool_gate,
            )
            accumulated = await driver.run(full_message)

            # ── Post-turn bookkeeping (each guarded so a failure here can't
            # fall through to the except and re-record the successful turn). ──
            self.sessions.record_success(session_key)
            try:
                self._persist_turn(session_key, text, accumulated, is_new)
            except Exception:
                logger.warning(
                    "WeCom: persist_turn failed session=%s", session_key, exc_info=True
                )
            try:
                await self._maybe_notice(inbound, session_key, provider)
            except Exception:
                logger.warning(
                    "WeCom: maybe_notice failed session=%s", session_key, exc_info=True
                )
            try:
                sel().log_api_access(
                    caller=f"wecom:{userid}",
                    operation="transport_dispatch.handle",
                    outcome="success",
                    source="wechat",
                    resources=f"session={session_key}",
                )
            except Exception:
                logger.debug("WeCom: success audit failed", exc_info=True)
        except Exception:
            logger.exception("WeCom transport_dispatch: error handling message")
            if _acquired:
                await self.sessions.record_failure(session_key)
        finally:
            # Always finalize the placeholder (no perma-"🤔 …"), even if
            # get_or_create raised before the semaphore was held. Only release
            # the semaphore if we actually acquired it.
            await renderer.close()
            if _acquired:
                self.sessions.release(session_key)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _session_key(self, userid: str) -> str:
        gen = self._conv.current_gen(userid)
        return canonical_key(f"wecom:{userid}:{gen}")

    def _persist_turn(
        self, session_key: str, user_text: str, reply_text: str, is_new: bool
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart)."""
        if self.conv_log is None:
            return
        self.conv_log.append(session_key, "user", user_text)
        if reply_text:
            self.conv_log.append(session_key, "assistant", reply_text)
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "WeChat"
            self.conv_log.set_title(session_key, title)

    async def _notice_bubble(self, req_id: str, text: str) -> None:
        """Send a threshold notice as a SEPARATE WS bubble (fresh stream_id).

        Kept out of the answer buffer -- and thus out of the persisted turn --
        so it is never replayed next turn as though the assistant said it
        (WeCom binds a reply to the inbound message, so this is the out-of-band
        equivalent of the Telegram/Slack separate-message notice).
        """
        assert self.client is not None
        if not req_id:
            return
        try:
            await self.client.send_stream(req_id, new_stream_id(), text, finish=True)
        except Exception:
            logger.debug("WeCom: notice bubble send failed", exc_info=True)

    async def _maybe_notice(
        self, inbound: "WeComInbound", session_key: str, provider: Any
    ) -> None:
        """Context-length handling, surfaced as a separate bubble post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold forces
        a compaction so the window never overflows. The backend autocompactor is
        an additional safety net.
        """
        userid = inbound.userid
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.wechat.hard_threshold_pct:
            self._conv.clear_awaiting(userid)
            try:
                await provider.compact()
                await provider.wait_for_compaction(timeout=120.0)
                await self._notice_bubble(inbound.req_id, "🗜️ 上下文接近上限，已自动压缩。")
            except Exception:
                logger.debug("WeCom hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.wechat.soft_threshold_pct and not self._conv.is_awaiting(userid):
            self._conv.set_awaiting(userid)
            await self._notice_bubble(
                inbound.req_id,
                "⚠️ 对话上下文已较长，回复 /compact 压缩，或 /new 开始新对话。",
            )

    async def _handle_compact(self, inbound: "WeComInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        session_key = self._session_key(inbound.userid)
        provider = self.sessions.get_provider(session_key)
        if provider is None:
            await self.client.send_reply(inbound.response_url, "ℹ️ 当前没有可压缩的对话。")
            return
        try:
            await provider.compact()
            await provider.wait_for_compaction(timeout=120.0)
            await self.client.send_reply(inbound.response_url, "🗜️ 已压缩上下文。")
        except Exception:
            logger.exception("WeCom /compact failed for %s", session_key)
            await self.client.send_reply(inbound.response_url, "⚠️ 压缩失败，请重试。")
