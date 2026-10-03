---
name: hermes-goal-variant-plugins
description: "Use when altering, forking, or extending Hermes /goal — judge verdicts, restart-on-blocked variants, GoalManager, plugin-registered slash commands."
version: 1.0.0
author: kuro
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, goals, plugins, slash-commands, autonomy]
    related_skills: [hermes-agent, hermes-plugin-lifecycle, hermes-plugin-hooks]
---

# Forking `/goal` in Hermes

`/goal` is three separable layers. Which one you replace decides how much you get for free.

| Layer | File | Replace it to… |
|---|---|---|
| State + judge | `hermes_cli/goals.py` (`GoalManager`) | change judging itself |
| Subcommand parsing | `hermes_cli/goal_command.py` (`dispatch_goal_command`) | change the *command surface* |
| Adapters | `gateway/slash_commands_goals.py`, `hermes_cli/cli_loops_mixin.py`, `tui_gateway/prompt_turn.py` | change dispatch |

**The load-bearing fact:** the retry loop never asks *which command* set the goal. Every
adapter calls `GoalManager.evaluate_after_turn()`, and its only precondition is
`mgr.is_active()`. So any code path calling `GoalManager.set()` gets the whole loop —
turn budget, `done`/`blocked`/`continue`/`wait` verdicts, `/goal wait <pid>` parking,
subgoals, auto-pause backstops — with no adapter changes.

## Hard limits

- **One goal per session.** `_meta_key(session_id)` is a single row. Two commands that
  both set a goal overwrite each other.
- **Plugin commands cannot shadow built-ins.** `register_command` rejects a name that
  `resolve_command()` already owns.
- **`register_command` is plugin-only.** No config file declares a variant command.

## No hook fires after the judge

`VALID_HOOKS` has per-turn observers (`post_llm_call`, `pre_verify`, `transform_llm_output`)
but **nothing past the goal judge**. To react to a verdict you must wrap
`GoalManager.evaluate_after_turn` at class level, from inside `register()`:

```python
original = GoalManager.evaluate_after_turn
def patched(self, last_response, **kw):
    decision = original(self, last_response, **kw)   # sync — judge_goal is a sync aux call
    my_policy(self, decision)
    return decision
patched._my_marker = True          # idempotency guard; check before re-patching
GoalManager.evaluate_after_turn = patched
```

`evaluate_after_turn` is **sync** even though the aux judge is an HTTP call — wrap it in
`_run_in_executor_with_context` at the adapter level, never `await` it.

## Getting a live gateway handle (for session resets)

`pre_gateway_dispatch` passes `gateway=self` to every subscriber
(`gateway/run_inbound.py:120`). Capture it there; it needs no import of the runner. The
hook runs **before** the auth gate, so it also fires for unauthorized senders — filter on
what you act on, not on whether it fired.

## Minting a fresh session

Do **not** call `async_session_store.reset_session()` alone. Mirror
`gateway/slash_commands_session.py::_handle_reset_command` in order:

1. `_invalidate_session_run_generation(session_key, reason=…)` — without this the in-flight
   run's guarded release returns False and leaves a **zombie running-agent slot** that
   silently drops every later message.
2. `_release_running_agent_state(session_key)` — idempotent.
3. `_cleanup_old_agent_for_reset(session_key)` — **async**; await it or it leaks a coroutine.
4. `_evict_cached_agent(session_key)`
5. `_clear_conversation_scope(session_key, reason=…)` — the funnel for all
   conversation-scoped state in `_CONVERSATION_SCOPED_STATE`.
6. `await async_session_store.reset_session(session_key)` — returns the entry with the new id.

`AsyncSessionStore.__getattr__` offloads sync methods to a thread, so this is safe to drive
from sync plugin code.

## Carrying a goal across the reset

`goals.migrate_goal_to_session(old, new, reason=…)` — the helper compression already uses.
It **archives the parent's row as `status="cleared"`** rather than deleting it, so
`load_goal(old)` still returns a state; assert on `.status`, never on `is None`. It refuses
when the child already has a goal.

Reset the counters you inherited: `turns_used=0`, `status="active"`, `last_verdict=None`,
`paused_reason=None`, both `consecutive_*_failures=0`, and the wait barriers
(`waiting_on_pid/_session/_until/_on_delegations`) or the new session starts parked.

## Verdict semantics — what is NOT a failure

A restart-on-blocked variant gets these wrong first:

- `wait` / `waiting` → the loop parked on a pid/session/deadline. `should_continue` is
  **False by design**. Restarting destroys the park the judge just asked for. Exempt by
  verdict, not by flag.
- `continue` + `should_continue=True` → healthy.
- `continue` + `status="paused"` → **budget exhausted**. This is a failure, and it does
  *not* come through as verdict `blocked`. Key on `status` too.
- `skipped` → judge no-op (empty goal/response). Not a failure.
- `blocked` → genuinely unachievable as stated.

Judge verdicts are parsed in `_parse_judge_response` (`goals.py:797`).

## Always cap the restarts

An impossible goal (missing credential, nonexistent host) otherwise spins forever —
strictly worse than the pause you replaced. On cap: drop your own bookkeeping, **leave the
goal row paused on its own session**, and notify. Clearing the user's goal because you gave
up is not yours to do.

## Verify by function

Probe against the real `GoalManager` in a throwaway `HERMES_HOME`
(`tempfile.mkdtemp(dir=scratch)`), importing `hermes_cli.goals` from
`/usr/local/lib/hermes-agent` — **system python returns `None` from every hook** and makes
working code look dead. Then confirm it landed in the *running* gateway:

```bash
hermes plugins enable <name>
grep -i "<name>" ~/.hermes/profiles/<p>/logs/gateway.log   # wrapper line proves the patch bound
```

A `capability_check … capability=tools.override decision=deny` line is **normal for every
plugin** — it is not a fault and does not mean your plugin was rejected.