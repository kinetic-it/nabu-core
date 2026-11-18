# Nabu Core

> **EXPERIMENTAL SOFTWARE / EXPERIMENTELLE SOFTWARE — NO WARRANTY / OHNE GEWÄHR**
>
> Not for safety-critical use: not for medical reminders, medication timing, emergency
> response, access control or unattended operation. An alarm that was accepted by a media
> player is not proof that anything was audible. Use at your own risk.
>
> Nicht für sicherheitskritische Nutzung: nicht für medizinische Erinnerungen, Medikamenten-
> zeitpläne, Notfälle, Zutrittskontrolle oder unbeaufsichtigten Betrieb. Ein vom Mediaplayer
> angenommener Befehl ist kein Beweis dafür, dass etwas hörbar war. Nutzung auf eigenes Risiko.

Nabu Core is the successor of the **Kinetic Voice Router** and keeps it as an internal
module. The repository therefore contains two things:

- **Nabu Core alarm engine** — the authoritative alarm store with ten stable slots, voice
  routing, a durable occurrence journal and a controlled playback channel. This is the part
  this project is being built around.
- **Kinetic Voice Router** — the existing conversation agent in front of a
  [Hermes Agent](https://github.com/Codename-11/hermes-homeassistant) Companion. It is kept
  unchanged and remains available under its HA domain `kinetic_voice_router`.

## Status

| | |
|---|---|
| State | Laboratory foundation, **not** production-ready |
| Alarm engine | complete against its contract, covered by tests, never used on real hardware |
| Router module | unchanged; its tests still pass |
| Tested against | Home Assistant 2026.11.1 in an isolated container (core objects, fake media player) and contract stubs in CI |
| Licence | MIT |

What "laboratory" means here: alarm states, restart behaviour, catch-up, migrations, device
policy and the playback state machine are exercised in tests, but there is **no** verified
real-device playback. No Echo device, no speaker and no production Home Assistant was used.

## Features

- **Ten stable slots** (`alarm-01` … `alarm-10`), optional unique names, automatic slot
  assignment, `NO_FREE_SLOT` on the eleventh alarm, no silent overwrite.
- **One-time and weekly alarms**, relative and absolute times, several weekdays in one
  sentence.
- **German voice input**, fail-closed: negations, missing or conflicting times and unknown
  extra words produce a clarification instead of a guess.
- **Per-alarm catch-up window**: five minutes by default, individually configurable,
  individually switchable off. Older occurrences are recorded as missed and stay silent.
- **Durable occurrence journal** (`in_flight`, `completed`, `failed`, `skipped`,
  `interrupted`) with a stop epoch, so a restart never replays a stale alarm.
- **Serial playback channel** with a bounded ringing period and a device allowlist.
- **Honest playback states**: command accepted, playback observed, stop requested, stop
  confirmed. Physical silence is never claimed.
- **Schema migration** (`PRAGMA user_version`) that only adds columns and never rewrites data.

## Installation

The integration carries its own copy of the engine. Build it before copying the
folder into `config/custom_components`, otherwise Home Assistant cannot import
`nabu_core`:

```bash
python3 scripts/bundle_integration.py          # writes kinetic_voice_router/nabu_core
python3 scripts/bundle_integration.py --check  # verifies it is up to date
```

The generated folder is not versioned; the script is the reproducible step.

Nabu Core is not on HACS yet. Two options:

1. Copy `custom_components/kinetic_voice_router` into your Home Assistant `config/custom_components`
   directory and make the `nabu_core` package importable in the same Python environment
   (for example by placing `nabu_core/` next to it and starting Home Assistant from `config/`).
2. Wait for the first pre-release.

The integration itself stays set up as before (*Add integration → Kinetic Voice Router*).
The alarm services are **opt-in** and appear only after you configure the options below.

### Enabling the alarm services

Add the options to the integration entry (UI options flow or `configuration.yaml`-free entry
options) and restart Home Assistant:

| Option | Meaning |
|---|---|
| `alarm_db` | SQLite file name relative to the Home Assistant config directory. **Without this option nothing is registered.** |
| `alarm_targets` | Allowlisted `media_player` entities. Empty means no alarm can ring. |
| `alarm_denied` | Explicitly denied entities. The denylist always wins, even if an entity is also allowlisted. |
| `alarm_media` | Media content id that is played (default `alarm.wav`). |
| `alarm_media_type` | Media content type (default `music`). |
| `alarm_ring_seconds` | Upper bound for one ringing period in seconds (default 120). |
| `alarm_tick_seconds` | Automatic scheduler interval in seconds (default 20, minimum 1). |

A denied or unknown device is rejected before any service call is made. Nothing is granted
automatically.

### Services

All services live under the `nabu_core` domain and answer with a mapping
(`ok`, `code`, optional `data`, optional `message`).

| Service | Purpose |
|---|---|
| `nabu_core.alarm_create` | create an alarm (`in_minutes` or ISO `when`, optional `name`, `weekdays`, `catchup_seconds`, `catchup_enabled`) |
| `nabu_core.alarm_update` | change schedule, name or catch-up window |
| `nabu_core.alarm_enable` / `nabu_core.alarm_disable` | switch an alarm on or off |
| `nabu_core.alarm_delete` | delete an alarm |
| `nabu_core.alarm_list` | list alarms |
| `nabu_core.alarm_status` | alarms, free slots, missed occurrences, schedule errors |
| `nabu_core.alarm_stop` | request a stop; returns `STOP_REQUESTED` and whether the device confirmed it |
| `nabu_core.alarm_configure_catch_up` | set the catch-up window or switch catching up off |
| `nabu_core.alarm_ring` | run one scheduler tick manually (also used by tests) |

Nabu Core is the only writable alarm truth. Do not mirror alarms into Home Assistant helpers;
the integration never reads them.

## Verified on real hardware

One supervised acceptance run on 2026-11-18 covered the whole chain against a real
speaker: alarm created two minutes ahead, automatic release 1.2 seconds after the due
time without any manual tick, audible ringing confirmed by the owner, stop confirmed,
player back to `idle` 18 ms later, journal entry `stop_confirmed`, no second playback on
the next tick, test alarm removed. 19 of 19 steps passed.

Scope of that evidence: **one alarm, one speaker, one run.** It shows that the chain
works end to end. It is not evidence of unattended operation.

## Known limitations

- A successful `play_media` call means *accepted*, not *audible*. Audibility is only
  established when a human confirms it; the code never claims silence or sound on its own.
- No long-term operation has been validated. Not tested: a Home Assistant restart while
  ringing, several alarms due at the same instant on real hardware, weekly series on a
  real device, and interaction with alarms owned by Alexa itself.
- The opt-in alarm layer registers an automatic Home Assistant scheduler tick (20 seconds by
  default) and unregisters it on unload. No extra automation is required.
- The media player is the only output channel; there is no fallback and no parallel ringing.
  If a device is not on the allowlist, nothing is played — there is no substitute device.
- Voice handling covers the documented German forms. Everything else deliberately asks back.
- The web GUI does not exist yet; the services are the interface.

## Optional: talking to a remote Home Assistant

`nabu_core/mcp_ha.py` contains a small client that addresses a speaker through an existing
Home Assistant MCP endpoint instead of a REST token. It exists for development and
diagnostics: it lets the engine drive a real speaker from a workstation without installing
anything in the running Home Assistant.

This path is optional and is **not** needed for normal alarm operation. It is not a second
alarm backend and stores no state. The endpoint URL carries a secret, so it is read from
the environment and must never be committed or logged. The same module family includes
`nabu_core/remote_ha.py`, a plain REST client used for the same diagnostic purpose.

## Licence

MIT. The router module keeps its original licence and attribution, see `LICENSE` and `NOTICE`.
