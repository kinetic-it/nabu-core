# Migration from the private 0.4.x line (patched `hermes_agent`) to the standalone integration

**Status: plan, not yet run on a live system.** Do it only with a backup and a way back.
The standalone version is an experimental pre-release.

## What changes

| 0.4.x (private) | Standalone |
|---|---|
| Router published an object in `hass.data`; a **patched** `hermes_agent` conversation entity called it ("bridge API 2") | The router has its **own** conversation entity and its **own** minimal Hermes client. No patch of `hermes_agent` is needed or used. |
| Router entry had no connection data | Entry holds Hermes URL, token and TLS-check switch |
| Pipeline used the *Hermes Agent* agent | Pipeline uses the *Kinetic Voice Router* agent |

Config entry version 1.2 → 1.3. The chosen mode (Off / Observe / Active) is kept.

## Before you start

1. Take a snapshot / backup of your Home Assistant system.
2. Note the current mode. **Switch to *Observe* or *Off* first.**
3. Copy `custom_components/kinetic_voice_router` away (outside `custom_components`) so you can restore it.
4. Have URL and bearer token of your Hermes Companion at hand.

## Steps

1. Install the standalone version (HACS custom repository, or copy the folder) — replacing
   the old `kinetic_voice_router` folder. **Do not touch `hermes_agent`.**
2. Restart Home Assistant (integration code is loaded at start).
3. Home Assistant shows a **repair / re-authentication** for the router: enter URL and
   token. The connection is tested first. Your mode stays as it was.
4. Check *Configure → Hermes connection*: status should read "reachable and compatible".
5. In *Settings → Voice assistants*, switch the pipeline's conversation agent from
   *Hermes Agent* to *Kinetic Voice Router*. **This is deliberate and manual** — nothing
   switches it for you. Keep *Prefer handling commands locally* on.
6. Test in *Off*, then *Observe*; use *Active* only after you are satisfied.
7. Optional, after a successful period: restore the unpatched upstream `hermes_agent` files
   (the patch is no longer used; with the patch still present and the router's old bridge
   gone, `hermes_agent` simply works as plain Hermes).

## Until step 3 is done

The router entry cannot start (no connection data). A still-patched `hermes_agent` finds no
router and behaves like plain Hermes. Nothing else is affected.

## Rollback

- **Quick:** *Configure → Mode → Off* (the agent passes everything to Hermes unchanged), or
  switch the pipeline agent back to *Hermes Agent*.
- **Files:** stop Home Assistant, restore the saved `kinetic_voice_router` folder, start. The 0.4.x
  code ignores the stored connection data and keeps your mode (it accepts a minor-version
  increase of the entry). The stored token stays in the Home Assistant configuration until you
  remove the integration entry; remove it if you do not want the old version to keep it.
- **Last resort:** restore the snapshot.

## Not a feature of this migration

No automatic pipeline change, no automatic exposure of entities, no token creation, no change
to Hermes profiles, models or providers.

## Alarm database: schema 3 (`alarm_sessions`) and how to go back

The alarm store migrates itself the first time this code opens the database — **without any
option being set**. Verified on a copy of the live database (schema 2, 10 occurrences,
stop epoch 8): the step adds the table `alarm_sessions` and raises `PRAGMA user_version`
to 3; alarms, occurrences, metadata and the epoch survive, `PRAGMA integrity_check` is `ok`.

That also means: **a git checkout of the previous code is not a rollback.** The old code
compares the table set exactly and refuses the extra table
(`DatabaseCompatibilityError: database schema is incompatible; unexpected table set`), after
which the integration sets up without alarm services instead of failing loudly.

Rollback, in order of preference:

1. **Restore the copy (recommended).** Stop Home Assistant, remove
   `alarms.sqlite`, `alarms.sqlite-wal` and `alarms.sqlite-shm`, copy the pre-install backup
   back, verify `PRAGMA user_version` is 2 and `PRAGMA integrity_check` is `ok`, then restore
   the old `custom_components/kinetic_voice_router` folder and start. A leftover `-wal`
   replays the migration on the next open, so it must go.
2. **Controlled downgrade (offline, documented stopgap).** With the database closed, drop
   `alarm_sessions` and set `user_version = 2`; the previous code then accepts the file and
   the next start of this code migrates again. This discards the stored session identity that
   the stop fence relies on, so take a fresh backup first.

Notes for the person doing it: the alarm volume and the pilot filter are config-entry options,
not part of the database, so they have to be reverted separately. And a stop that was
recorded as `stop_confirmed` in schema 3 is gone after restoring an older copy — check the
journal afterwards instead of assuming silence.
