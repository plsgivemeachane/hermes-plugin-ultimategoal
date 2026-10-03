# ultimategoal

A Hermes slash command that sets a goal which **restarts itself in a fresh session** instead
of stopping at a bad verdict.

Built on `kuro` (profile `kuro`), verified against Hermes `0.19.x`. Portable: no hardcoded
paths, no `HERMES_HOME` assumptions, one plugin + one skill.

## What it does

`/goal` treats `BLOCKED` as terminal — it pauses and hands the decision back to you.
`/ultimategoal` inverts that one branch:

| Goal verdict | `/goal` | `/ultimategoal` |
|---|---|---|
| `done` | stop | stop |
| `continue` | next turn | next turn |
| `wait` (pid/session/deadline) | parked | parked — untouched |
| `blocked` | **pause, ask you** | **fresh session, restart** |
| budget exhausted | pause | fresh session, restart |
| judge API/parse backstop | pause | fresh session, restart |

A fresh session gets the goal text plus the reason the previous attempt stopped, and starts
with a full turn budget.

## Install

Two routes, both landing the same code.

**A. Into an existing profile** — add the command to a profile you already have:

```bash
hermes plugins install plsgivemeachane/hermes-plugin-ultimategoal --enable
hermes skills install plsgivemeachane/hermes-plugin-ultimategoal/skills/hermes-goal-variant-plugins
```

**B. As a whole new profile**, pre-loaded with the plugin and the skill:

```bash
hermes profile install plsgivemeachane/hermes-plugin-ultimategoal -y
```

Route B is the one for prepping new profiles. The profile then needs its own API keys
(`hermes profile use <name>` → `setup`), but the goal machinery ships with it.

Or by hand — copy `plugins/ultimategoal/` into `~/.hermes/profiles/<name>/plugins/` and
`skills/hermes-goal-variant-plugins/` into that profile's `skills/`, then
`hermes plugins enable ultimategoal`.

> **Profile scoping:** `hermes …` acts on the *sticky active* profile, not on `$HERMES_PROFILE`.
> `HERMES_PROFILE` is only read by kanban as an author label (`hermes_constants.get_hermes_home`
> resolves from `HERMES_HOME`, falling back to the active profile). To target a specific profile,
> set `HERMES_HOME=~/.hermes/profiles/<name>` or `hermes profile use <name>` first — otherwise the
> install lands in the wrong profile with no error.

## Use

```
/ultimategoal <objective>                # default restart cap: 3
/ultimategoal <objective> --restarts 5   # per-goal cap
/ultimategoal status
/ultimategoal clear
```

Tune the default cap in `config.yaml`:

```yaml
ultimategoal:
  max_restarts: 3
```

## Verify it took

```bash
hermes plugins list --plain | grep ultimategoal          # enabled
grep -i ultimategoal ~/.hermes/profiles/<name>/logs/gateway.log
# expect: ultimategoal: wrapped GoalManager.evaluate_after_turn
```

The wrapper line is the one that matters — it proves the patch bound in the **running**
gateway process, not just on import. `capability_check … capability=tools.override
decision=deny` in the same log is normal for every plugin and does not indicate a fault.

## Design notes (why it is built this way)

- **The loop is command-agnostic.** Every adapter drives
  `GoalManager.evaluate_after_turn()`, whose only precondition is `mgr.is_active()`. It never
  asks which command set the goal — so `GoalManager.set()` from a plugin command inherits the
  entire loop: budget, verdicts, wait parking, subgoals, auto-pause backstops.
- **No plugin hook fires after the judge.** `VALID_HOOKS` has per-turn observers
  (`post_llm_call`, `pre_verify`) but nothing past the goal judge. So the plugin wraps
  `evaluate_after_turn` at class level, marked idempotent so `/reload` rebinds instead of
  stacking layers.
- **The gateway handle comes from `pre_gateway_dispatch`**, which passes `gateway=self` to
  every subscriber — no import of the runner needed.
- **The session reset mirrors `/new` exactly** (generation bump → running-agent release →
  async agent cleanup → agent eviction → conversation-scope funnel → id rotation). Skipping
  the generation bump leaves a zombie running-agent slot that silently drops later messages.

The full seam-by-seam recipe, the verdict-semantics table, and the reset order are in the
bundled **`hermes-goal-variant-plugins`** skill. Read it before forking another variant — it
also records the three bugs this build hit, all of which are easy to repeat.

## Known limits

- **One goal per session.** `GoalManager` is a single row per session id; `/goal` and
  `/ultimategoal` overwrite each other.
- **Restart fires on the next verdict, not mid-turn.** No plugin seam exists inside a turn, and
  patching an adapter to get one is a bad trade.
- **A fresh session loses conversation context.** That is the intent, but it means the new
  attempt starts from the goal text plus the stop reason — anything else that must survive has
  to live in the goal text.
- **CLI/TUI restarts degrade.** The gateway handle is captured from gateway dispatch only, so
  from a bare CLI there is no runner to reset; the plugin then leaves the goal paused rather
  than crashing. Gateway and chat surfaces are fully supported.

## Files

```
plugin.yaml                                  # root manifest (route A)
distribution.yaml                            # profile-distribution manifest (route B)
__init__.py                                  # root shim re-exporting register()
plugins/ultimategoal/__init__.py             # the plugin — single source of truth
plugins/ultimategoal/plugin.yaml             # plugin manifest
skills/hermes-goal-variant-plugins/SKILL.md  # fork recipe + pitfalls
```

The repo root carries a `plugin.yaml` + `__init__.py` shim because `hermes plugins install`
checks the clone's **root** for a manifest, while `hermes profile install` expects the
distribution layout (`plugins/`, `skills/`). The shim re-exports `register()` from
`plugins/ultimategoal/` — one implementation, two entry points.