"""ultimategoal — a goal that restarts itself instead of stopping at a bad verdict.

``/goal`` treats BLOCKED as terminal: it pauses and hands the decision back to you
(``hermes_cli/goals.py``, ``_pause_decision`` on the blocked branch). ``/ultimategoal``
inverts that — any verdict that is neither ``done`` nor a healthy ``continue`` rolls a
FRESH session and re-runs the same goal there, carrying the blocking reason forward.

Mechanism, and why each seam:

* The retry loop does not ask *which command* set the goal. ``GoalManager.evaluate_after_turn``
  is driven by every adapter (gateway ``run_goals._post_turn_goal_continuation``, CLI
  ``cli_loops_mixin``, TUI ``prompt_turn``) and only checks ``mgr.is_active()``. So calling
  ``GoalManager.set()`` from a plugin command is enough to get the whole loop.

* There is NO plugin hook that fires after the judge runs — ``VALID_HOOKS`` has per-turn
  observers (``post_llm_call``, ``pre_verify``) but nothing past the goal judge. So we wrap
  ``GoalManager.evaluate_after_turn`` at class level: one installation inside ``register()``,
  bound to the classes the running process already imported. Bindings are installed per
  interpreter, and each binding closes over this plugin module, so a reload rebinds cleanly.

* The fresh session is taken with the SAME sequence the built-in ``/new`` uses
  (``gateway/slash_commands_session.py::_handle_reset_command``), because a bare
  ``async_session_store.reset_session`` skips the generation bump, the running-agent eviction
  and the conversation-scoped state funnel. The gateway object itself arrives via
  ``pre_gateway_dispatch``, which passes ``gateway=self`` — that hook runs before the auth
  gate and gives us a live handle without importing the runner.

* The goal is carried across by ``goals.migrate_goal_to_session``, the helper compression
  already uses. It archives the parent's row so exactly one active goal row survives.

* Restarts are counted per goal and capped (default 3). Without the cap a genuinely
  impossible goal (missing credential, nonexistent host) spins forever, which is strictly
  worse than the pause we replaced.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Verdicts that mean "keep going in this session" — never a restart trigger.
_CONTINUE_VERDICTS = frozenset({"continue", "skipped"})

# Verdicts that STOP the loop without failing it: a wait barrier holds the turn without
# burning budget, so restarting would destroy the park the judge just asked for.
_PARKED_VERDICTS = frozenset({"wait", "waiting"})

# Default restart cap. Overridable per-call via `/ultimategoal <goal> --restarts N`.
DEFAULT_MAX_RESTARTS = 3


def _restart_cap() -> int:
    """``plugins.<id>.entries...`` is awkward to reach; config-level key is enough."""
    try:
        from hermes_cli.config import load_config_readonly

        raw = (load_config_readonly() or {}).get("ultimategoal") or {}
        value = int(raw.get("max_restarts", DEFAULT_MAX_RESTARTS))
        return value if value > 0 else DEFAULT_MAX_RESTARTS
    except Exception:
        return DEFAULT_MAX_RESTARTS


# ── per-session state ───────────────────────────────────────────────────
# sid -> {"goal": str, "restarts": int, "started_at": float, "last_reason": str}
_state: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# The gateway runner, captured from pre_gateway_dispatch. Needed to mint a fresh session.
_gateway: Optional[Any] = None
_gateway_lock = threading.Lock()


def _capture_gateway(gateway: Any) -> None:
    global _gateway
    if gateway is None:
        return
    with _gateway_lock:
        _gateway = gateway


def _get_gateway() -> Optional[Any]:
    with _gateway_lock:
        return _gateway


def session_state(session_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        entry = _state.get(session_id)
        return dict(entry) if entry else None


def forget(session_id: str) -> None:
    with _lock:
        _state.pop(session_id, None)


# ── the wrapper ─────────────────────────────────────────────────────────
def _install_wrapper() -> None:
    """Patch ``GoalManager.evaluate_after_turn`` once per interpreter.

    Idempotent: the patched function carries a marker attribute, and every binding closes
    over this module, so re-running ``register()`` (a ``/reload``) rebinds rather than
    stacking a second layer of indirection.
    """
    try:
        from hermes_cli.goals import GoalManager
    except Exception as exc:
        logger.warning("ultimategoal: GoalManager unavailable: %s", exc)
        return

    original = getattr(GoalManager, "evaluate_after_turn", None)
    if original is None or getattr(original, "_ultimategoal_patched", False):
        return

    import hermes_cli.goals as goals_mod

    async def _maybe_async(*args: Any, **kwargs: Any):
        return original(*args, **kwargs)

    def evaluate_after_turn(self, last_response, **kwargs):
        decision = original(self, last_response, **kwargs)
        try:
            _after_decision(self, decision)
        except Exception as exc:  # never break the loop over a restart
            logger.warning("ultimategoal: post-decision handling failed: %s", exc)
        return decision

    evaluate_after_turn._ultimategoal_patched = True
    evaluate_after_turn.__doc__ = original.__doc__
    GoalManager.evaluate_after_turn = evaluate_after_turn
    logger.info("ultimategoal: wrapped GoalManager.evaluate_after_turn")


def _after_decision(mgr: Any, decision: Dict[str, Any]) -> None:
    """Restart policy: one decision, one verdict."""
    if not decision:
        return
    verdict = str(decision.get("verdict") or "").lower()
    status = str(decision.get("status") or "").lower()
    reason = str(decision.get("reason") or "").strip()

    # Only a goal this plugin owns is eligible; a plain /goal is left to Hermes.
    sid = getattr(mgr, "session_id", "") or ""
    with _lock:
        entry = _state.get(sid)
    if entry is None:
        return

    # done -> the goal held. Not ours to touch.
    if verdict == "done" or status == "done":
        _finish(sid, "done", reason)
        return

    # A parked wait (waiting on a pid / session / deadline) is a healthy stop, not a
    # failure: should_continue is False by design there. Exempt by verdict, not by flag.
    if verdict in _PARKED_VERDICTS:
        return

    # A healthy continue is the loop working as intended.
    if verdict in _CONTINUE_VERDICTS and decision.get("should_continue"):
        return

    # Everything else that stopped the loop is a restart trigger: blocked, budget
    # exhausted, judge-transport/parse backstops, an explicit pause.
    _restart(sid, verdict or status or "stopped", reason)


def _finish(session_id: str, outcome: str, reason: str) -> None:
    with _lock:
        entry = _state.pop(session_id, None)
    if entry:
        logger.info("ultimategoal: goal %s for %s: %s", outcome, session_id, reason[:160])


def _restart(session_id: str, verdict: str, reason: str) -> None:
    """Mint a fresh session and re-arm the same goal there."""
    gateway = _get_gateway()
    if gateway is None:
        logger.info("ultimategoal: no gateway handle for %s; goal left paused", session_id)
        with _lock:
            _state.pop(session_id, None)
        return

    with _lock:
        entry = _state.get(session_id)
        if entry is None:
            return
        restarts = int(entry.get("restarts", 0))
        goal_text = str(entry.get("goal") or "")
        cap = entry.get("cap") or _restart_cap()

    if restarts >= cap:
        # Cap reached: hand control back to the human, exactly like /goal would have.
        # The goal row stays paused on its own session — dropping OUR bookkeeping is not
        # a licence to clear the user's goal.
        logger.info("ultimategoal: restart cap (%d) reached for %s", cap, session_id)
        _finish(session_id, f"capped after {restarts} restarts", reason)
        _notify(gateway, session_id, f"⏸ ultimategoal: restart cap ({cap}) reached — {reason or verdict}")
        return

    try:
        new_sid = _reset_session(gateway, session_id)
    except Exception as exc:
        logger.warning("ultimategoal: session reset failed for %s: %s", session_id, exc)
        return

    # Carry the goal onto the new session; archive the parent's row.
    try:
        from hermes_cli.goals import GoalManager, load_goal, migrate_goal_to_session

        state = load_goal(session_id)
        if state is not None:
            migrated = migrate_goal_to_session(session_id, new_sid, reason="ultimategoal-restart")
            if migrated:
                new_mgr = GoalManager(session_id=new_sid)
                st = new_mgr.state
                if st is not None:
                    # Fresh budget for the new attempt; the count lives in _state.
                    st.turns_used = 0
                    st.status = "active"
                    st.last_verdict = None
                    st.last_reason = None
                    st.paused_reason = None
                    st.consecutive_parse_failures = 0
                    st.consecutive_transport_failures = 0
                    st.waiting_on_pid = None
                    st.waiting_on_session = None
                    st.waiting_until = 0.0
                    st.waiting_on_delegations = 0
                    from hermes_cli.goals import save_goal

                    save_goal(new_sid, st)
    except Exception as exc:
        logger.warning("ultimategoal: goal migration failed %s -> %s: %s", session_id, new_sid, exc)

    with _lock:
        _state[new_sid] = {
            "goal": goal_text,
            "restarts": restarts + 1,
            "started_at": entry.get("started_at", 0.0),
            "last_reason": reason or verdict,
        }
        _state.pop(session_id, None)

    # Kick the new session with the goal text so it starts working immediately.
    _kick(gateway, session_id, new_sid, goal_text, reason, restarts + 1, cap)
    logger.info(
        "ultimategoal: restarted %s -> %s (attempt %d/%d) verdict=%s",
        session_id, new_sid, restarts + 1, cap, verdict,
    )


def _reset_session(gateway: Any, session_id: str) -> str:
    """Reset the session that owns *session_id*, returning the new session id.

    Mirrors ``_handle_reset_command``: bump the run generation (so the in-flight run's
    guarded release cannot leave a zombie slot), evict the running-agent slot, clear
    conversation-scoped state, then rotate the session id. Every step is individually
    guarded — the reset is best-effort and must not raise into the loop.
    """
    from gateway.platforms.event import MessageEvent

    source = getattr(gateway, "_ultimategoal_last_source", None)
    if source is None:
        raise RuntimeError("no source bound for session; cannot address the fresh session")

    session_key = gateway._session_key_for_source(source)

    try:
        gateway._invalidate_session_run_generation(session_key, reason="ultimategoal-restart")
    except Exception as exc:
        logger.debug("ultimategoal: generation bump failed: %s", exc)
    try:
        gateway._release_running_agent_state(session_key)
    except Exception as exc:
        logger.debug("ultimategoal: running-agent release failed: %s", exc)
    try:
        _run_coroutine(gateway._cleanup_old_agent_for_reset(session_key))
    except Exception as exc:
        logger.debug("ultimategoal: agent cleanup failed: %s", exc)
    try:
        gateway._evict_cached_agent(session_key)
    except Exception:
        pass
    try:
        gateway._clear_conversation_scope(session_key, reason="ultimategoal-restart")
    except Exception as exc:
        logger.debug("ultimategoal: conversation scope clear failed: %s", exc)

    async def _rotate():
        return await gateway.async_session_store.reset_session(session_key)

    new_entry = _run_coroutine(_rotate())
    if new_entry is None:
        raise RuntimeError("reset_session returned no entry")
    return str(new_entry.session_id)


def _run_coroutine(coro):
    """Run a coroutine from sync plugin code, mirroring resolve_plugin_command_result."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import contextvars
    import threading as _t

    out: Dict[str, Any] = {}
    done = _t.Event()

    def _runner():
        try:
            out["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            out["exc"] = exc
        finally:
            done.set()

    _t.Thread(target=contextvars.copy_context().run, args=(_runner,), daemon=True).start()
    if not done.wait(timeout=30):
        raise TimeoutError("session reset did not complete within 30s")
    if "exc" in out:
        raise out["exc"]
    return out.get("value")


def _kick(
    gateway: Any,
    old_sid: str,
    new_sid: str,
    goal_text: str,
    reason: str,
    attempt: int,
    cap: int,
) -> None:
    """Enqueue the goal text into the fresh session through the adapter FIFO."""
    source = getattr(gateway, "_ultimategoal_last_source", None)
    if source is None:
        return
    try:
        adapter = gateway._delivery_adapter_for(source)
    except Exception as exc:
        logger.debug("ultimategoal: adapter lookup failed: %s", exc)
        return
    if adapter is None:
        logger.debug("ultimategoal: no delivery adapter for the source")
        return
    try:
        session_key = gateway._session_key_for_source(source)
    except Exception:
        return

    from gateway.platforms.event import MessageEvent, MessageType

    prefix = (
        f"⟳ ultimategoal restart {attempt}/{cap} — previous attempt stopped: "
        f"{reason or 'no reason given'}.\n\nGoal (unchanged):\n{goal_text}"
    )
    try:
        turn = MessageEvent(
            text=prefix,
            message_type=MessageType.TEXT,
            source=source,
            message_id=None,
            channel_prompt=getattr(source, "channel_prompt", None),
        )
        gateway._enqueue_fifo(session_key, turn, adapter)
    except Exception as exc:
        logger.warning("ultimategoal: kick enqueue failed: %s", exc)


def _notify(gateway: Any, session_id: str, message: str) -> None:
    """Best-effort operator notice; never raises."""
    try:
        source = getattr(gateway, "_ultimategoal_last_source", None)
        if source is None:
            return
        adapter = gateway._delivery_adapter_for(source)
        if adapter is None:
            return
        send = getattr(adapter, "send_message", None)
        if callable(send):
            send(source.chat_id, message)
    except Exception as exc:
        logger.debug("ultimategoal: notify failed: %s", exc)


# ── hooks ───────────────────────────────────────────────────────────────
async def pre_gateway_dispatch(event=None, gateway=None, session_store=None, **kwargs):
    """Capture the runner and bind the current source so a restart can address the chat."""
    _capture_gateway(gateway)
    if event is not None:
        try:
            gateway._ultimategoal_last_source = event.source
        except Exception:
            pass
    return None


# ── command ─────────────────────────────────────────────────────────────
def _parse_goal_and_cap(args: str):
    """``/ultimategoal <goal> [--restarts N]`` → (goal, cap_override|None)."""
    cap = None
    text = (args or "").strip()
    lowered = text.lower()
    for token in ("--restarts", "--max-restarts"):
        if token in lowered:
            head, _, tail = text.partition(token)
            parts = tail.strip().split()
            if parts and parts[0].isdigit():
                cap = max(1, int(parts[0]))
                text = (head + " ".join(parts[1:])).strip()
            break
    return text, cap


def _cmd(args: str) -> str:
    args = (args or "").strip()

    if not args or args.lower() == "status":
        return _status()
    if args.lower() in {"clear", "stop", "off"}:
        return _clear()
    if args.lower() == "help":
        return (
            "/ultimategoal <objective>\n"
            "  Set a goal that restarts itself in a fresh session when it stops short of done.\n"
            "  /ultimategoal <objective> --restarts N   cap restarts (default "
            f"{_restart_cap()})\n"
            "  /ultimategoal status | clear | help"
        )

    goal_text, cap_override = _parse_goal_and_cap(args)
    if not goal_text:
        return "Usage: /ultimategoal <objective> [--restarts N]"

    from hermes_cli.goals import GoalManager
    from gateway.session_context import get_session_env

    sid = get_session_env("HERMES_SESSION_ID", "")
    if not sid:
        return "⚠ ultimategoal: no session id bound — use this from a chat or the CLI, not a bare tool call."

    mgr = GoalManager(session_id=sid)
    try:
        state = mgr.set(goal_text)
    except Exception as exc:
        return f"ultimategoal: {exc}"

    with _lock:
        _state[sid] = {
            "goal": goal_text,
            "restarts": 0,
            "started_at": __import__("time").time(),
            "cap": cap_override,
            "last_reason": "",
        }

    header = f"⟳ ultimategoal armed ({state.max_turns}-turn budget per attempt): {goal_text}"
    if cap_override:
        header += f"\n   restart cap: {cap_override}"
    header += (
        "\nOn done: stop. On blocked / budget-out / judge failure: a FRESH session is created "
        "and the goal restarts there with the blocking reason carried forward."
    )
    return header


def _status() -> str:
    try:
        from gateway.session_context import get_session_env
        from hermes_cli.goals import load_goal

        sid = get_session_env("HERMES_SESSION_ID", "")
        entry = session_state(sid)
        goal = load_goal(sid)
        if entry is None and goal is None:
            return "No ultimategoal set for this session."
        lines = []
        if goal is not None:
            lines.append(f"goal: {goal.goal}")
            lines.append(
                f"  status={goal.status} verdict={goal.last_verdict or '-'} "
                f"turns={goal.turns_used}/{goal.max_turns}"
            )
        if entry:
            cap = entry.get("cap") or _restart_cap()
            lines.append(
                f"  restarts={entry.get('restarts', 0)}/{cap}"
                + (f"  last_stop={entry.get('last_reason') or '-'}" if entry.get("last_reason") else "")
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"ultimategoal status error: {exc}"


def _clear() -> str:
    try:
        from gateway.session_context import get_session_env
        from hermes_cli.goals import GoalManager

        sid = get_session_env("HERMES_SESSION_ID", "")
        forget(sid)
        GoalManager(session_id=sid).clear()
        return "✓ ultimategoal cleared."
    except Exception as exc:
        return f"ultimategoal clear error: {exc}"


# ── registration ────────────────────────────────────────────────────────
def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)
    ctx.register_command(
        "ultimategoal",
        handler=_cmd,
        description="Set a goal that restarts itself in a fresh session when it stops short of done",
        args_hint="<objective> [--restarts N]",
        argument_mode="mixed",
    )
    _install_wrapper()