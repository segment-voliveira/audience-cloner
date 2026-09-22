# lensgroup-audience-cloner

A Python CLI over the [Segment Public API](https://docs.segmentapis.com/). Today it clones Engage
audiences — definition, destinations, and activations; the command structure
(`segment.py <group> <action>`) leaves room for more.

Standard library only: no `pip install`, no `jq`. Needs Python 3.9+.

## Setup

```sh
cp .env.example .env   # then fill in the token + space id
chmod +x segment.py
```

If `fzf` is on your PATH the audience picker becomes a fuzzy search; otherwise it falls back to a
numbered menu.

## Use

Just run it. The wizard asks what you want to clone first:

```
$ ./segment.py

What do you want to clone?
  1  Audience — create a NEW audience from an existing one, with its destinations and activations
  2  Activations only — copy activations onto an audience and destination that already exist

Choice (1-2) [1]:
```

Or jump straight to one of them:

```sh
./segment.py audiences clone            # clone a whole audience + destinations + activations
./segment.py audiences clone <id>       # skip the audience picker
./segment.py audiences list             # enabled/type/name/key
./segment.py activations clone          # copy activations onto an EXISTING audience/destination
./segment.py --help
```

Add `--dry-run` to any of them to see the exact plan without creating anything.

The question is skipped when intent is already clear: passing any `--from-*`/`--to-*`/`--activation`/
`--all` flag selects the activation flow, and `--no-destinations`/`--no-activations` selects the
audience flow. With `-y` it defaults to the audience clone.

### Global flags

| Flag | Effect |
| --- | --- |
| `--dry-run` | Print the whole plan and exit. |
| `-y`, `--yes` | Take all defaults, skip the confirmation. |
| `--resync` | New activations resync the whole audience on creation (default: only future changes). |
| `--debug` | Log every request and response — start here when the API rejects something. |
| `--env FILE` | Read config from `FILE` instead of `./.env`. |

### `audiences clone`

| Flag | Effect |
| --- | --- |
| `--no-destinations` | Clone the definition only. Implies `--no-activations`. |
| `--no-activations` | Clone the definition and destinations, but no activations. |
| `--allow-incomplete` | Proceed even when the API doesn't return every destination connection. |

### `activations clone`

Copies activations from one (audience, destination connection) pair onto **one or more** others that
**already exist** — every target audience must exist and already have the destination connected. It
creates only the activations.

Pick as many target audiences as you like in one run: the target picker is multi-select (`1,3`,
ranges like `2-4`, or `a` for all; tab-to-mark under fzf). The source and the activation selection
are chosen once and fanned out to every target, each with its own destination connection.

The **source audience is left out of the target list** — it is not a useful target, and under
replace it would be refused anyway. If the space has no other audience, the run stops and says so.
`--to-audience <source id>` is still honoured, for the one real case: the same audience on a
*different* destination connection.

**This replaces by default.** Every activation already on a target destination is **deleted** first,
then the copies are created, so each target ends up mirroring the source exactly instead of
accumulating. The wizard states how many will be deleted across all targets, lists them per target
by name and id, and asks once: `Delete 3 and create 6 activation(s) across 3 target audience(s)?`
Deleting an activation stops that data flowing to the destination, so nothing happens before you
confirm — and `--dry-run` shows the whole plan without touching anything.

| Flag | Effect |
| --- | --- |
| `--from-audience ID` | Skip the source audience picker. |
| `--to-audience ID` | Skip the target picker. **Repeatable** — one per target audience. |
| `--from-connection ID` | Skip the source destination picker (`ii_…` id). |
| `--to-connection ID` | Skip a target destination picker. Repeatable; paired **in order** with `--to-audience`, since a connection id belongs to a single audience. Omit it and each target's destination is picked (or auto-selected when there is only one). |
| `--activation ID` | Copy just this activation. Repeatable. |
| `--all` | Copy every activation on the source destination. |
| `--no-replace` | Add the copies alongside existing activations instead of replacing them. |
| `--allow-duplicates` | With `--no-replace`, copy even when that activation name already exists. |

Because replace deletes before it creates, three things are deliberate:

- **If any delete fails, no creates run** on that target. Otherwise the stale activation and its
  replacement would both be live, double-sending events to the destination.
- **A failed delete also stops the remaining targets.** The cause is rarely target-specific — a
  token, permission or rate-limit problem recurs — and continuing would delete more live
  activations before hitting it. Untouched targets are reported as `Not attempted`.
- **If a target ends short of activations** (a delete succeeded but a create failed, or creates were
  aborted), the payloads of everything deleted there are printed so they can be recreated without
  digging through the Segment UI.

Replace makes the operation idempotent: re-running it deletes the copies it previously made and
recreates them, leaving every target identical to the source.

Fully non-interactive, fanning out to two audiences:

```sh
./segment.py activations clone -y --all \
  --from-audience aud_AAA --from-connection ii_AAA \
  --to-audience   aud_BBB --to-connection   ii_BBB \
  --to-audience   aud_CCC --to-connection   ii_CCC
```

Guards, because this writes to third-party destinations. Each applies per target:

- The source audience is not offered as a target, and copying onto the same audience/destination
  pair is refused if named explicitly — under replace that would delete the activations you are
  copying from.
- Selecting the same target audience twice collapses to one run over it.
- If a target points at a **different destination** than the source, it warns:
  `destinationMapping.actionId` and destination-specific settings are unlikely to be valid there.
- Copying a linked activation onto a classic audience **drops entity personalization** with a
  warning, since classic audiences reject it with a 400.

Flags work on either side of the subcommand, so `-y activations clone` and `activations clone -y`
both parse.

## What a clone copies

Three API steps, in order — each needs an id returned by the previous one:

1. **Audience** — `POST /spaces/{spaceId}/audiences`, carrying the source `definition.query`,
   `audienceType`, `definition.targetEntity` (linked audiences), and `options`.
2. **Destinations** — `POST …/audiences/{id}/destination-connections` per source connection,
   carrying `destinationId`, `idSyncConfiguration`, and `connectionSettings`. Returns a new
   `ii_…` connection id.
3. **Activations** — `POST …/destination-connections/{connectionId}/activations` per source
   activation, carrying `activationName`, `activationType`, `enabled`, `personalization`
   (profile traits, mappings, linked-audience entities), and `destinationMapping`.

Each activation is re-pointed at the *new* connection that replaced its source one, matched on the
source `connectionId`.

Note that **activations are created, not patched**. `updateActivationForAudience` (PATCH) only
mutates an activation that already exists, and `addDestinationToAudience` does not auto-create one
— per the spec, *"To start syncing data, you must create an Activation for the connection created
here."* So cloning has to use `addActivationToAudience`.

New audiences default to **disabled**, and activations keep the source's own `enabled` flag, so
nothing starts syncing to a third party before you have reviewed it.

## Failure behaviour

The audience is created first, and failing there is fatal. After that, each destination and
activation is attempted independently: failures are collected, reported per item, summarised at the
end, and the process exits non-zero. A partial clone is left in place rather than silently rolled
back — the summary says exactly which pieces are missing.

```
Cloned VIP Buyers (copy)  (aud_abc123)
  audience     1/1
  destinations 2/3
  activations  2/3

! 2 part(s) of the clone failed:
    - destination 'Braze': HTTP 422 — destination not enabled in Engage
    - activation 'orphan': its destination connection was not cloned
```

## Troubleshooting

**`CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`** — the python.org macOS
installer points OpenSSL at an `etc/openssl/cert.pem` it never creates, so Python trusts zero CAs.
The CLI works around it by falling back to `certifi` and then the OS bundle, and it never disables
verification. To fix it for every Python program on the machine, run:

```sh
"/Applications/Python 3.14/Install Certificates.command"
```

**`reports N destination connection(s) … but only returned M`** — the token cannot read the
underlying destination. Segment counts such connections in `totalEntries` but silently omits them
from `connections`, returning 200 rather than 403. Every activation needs its source connection in
order to be re-pointed, so the CLI refuses rather than quietly produce a definition-only clone that
reports success. **Fix:** grant the token access to the destination (Workspace Settings → Access
Management → Tokens). `--allow-incomplete` proceeds with whatever is visible.

## Config

| Variable | Notes |
| --- | --- |
| `SEGMENT_PUBLIC_API_TOKEN` | Workspace Settings → Access Management → Tokens. Needs space read + write. |
| `SEGMENT_SPACE_ID` | Engage space id. |
| `SEGMENT_REGION` | `us` (default) → `api.segmentapis.com`, `eu` → `eu1.api.segmentapis.com`. |
| `SEGMENT_API_HOST` | Optional explicit host, overrides `SEGMENT_REGION`. May include a scheme. |

Real `SEGMENT_*` environment variables take precedence over the file, so one-off overrides work
without editing `.env`. Use `--env path/to/other.env` to point at a different file entirely.

## Notes / limits

- Cloning is **within a single space**. Cross-space cloning needs a second token/space pair — a
  natural next step, which is why the payload builders are standalone pure functions.
- A destination must already be added to Engage (Engage → Settings) before it can be connected via
  the API; the spec is explicit. Destinations that aren't are reported as failures.
- The list response carries no `destination` vs `warehouse` discriminator, so the CLI tries
  `destination` first and retries as `warehouse` on a validation error (one extra request for
  warehouses).
- **Not copied:** audience schedules (`/schedules`, linked audiences only) and computed-trait
  dependencies. Traits or audiences referenced by the definition must already exist.
- The destination-connection and activation endpoints are `v1alpha` and send
  `Content-Type: application/vnd.segment.v1alpha+json`. Segment may change them without notice.

## Development

```sh
python3 -m unittest test_payloads -v   # 35 tests, no network
```

The request shapes were built against the Segment OpenAPI spec (v73.3.0) rather than the rendered
docs, which proved unreliable — the HTML gave the destinations path as `/destinations` when it is
really `/destination-connections`, and bracket-style `pagination[count]` when the spec declares
`pagination.count`.

Every input schema sets `additionalProperties: false`, so payloads are built by whitelisting fields
rather than copying the read response. Two traps that cost real debugging time, both covered by
tests:

- An audience `definition` accepts only `query` + `targetEntity`. There is **no** `type` field —
  that belongs to trait definitions — and sending one is rejected.
- `backfillEventDataDays` is only valid when `includeHistoricalData` is true.
- `personalization.entities` must be **omitted entirely** for classic (USERS/ACCOUNTS) audiences,
  not sent as `[]`. Read responses include `entities: []` on classic audiences, but echoing it back
  is a 400: *"Providing entities for a Classic audience returns a 400 error."*

`--debug` prints each request line, body, status, and response, which is the fastest way to see
what the API actually objected to. It does not print the Authorization header.
