# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python CLI (`segment.py`) over the [Segment Public API](https://docs.segmentapis.com/)
for cloning Engage audiences and activations. Two flows:

1. `audiences clone` — creates a **new** audience from an existing one: definition → destination
   connections → activations.
2. `activations clone` — copies activations onto an audience + destination that **already exist**.
   Replaces by default (deletes the target destination's activations first).

A bare `./segment.py` asks which flow to run.

## Commands

```sh
python3 -m unittest test_payloads -v              # all 26 tests, no network
python3 -m unittest test_payloads.AudiencePayload # one class
python3 -m unittest test_payloads.ActivationPayload.test_drops_server_generated_fields
python3 -m py_compile segment.py                  # syntax check
./segment.py --dry-run                            # full plan, writes nothing
./segment.py --debug ...                          # log every request/response
```

No build step, no linter configured, no dependencies — **standard library only**
(`urllib.request`, not `requests`). Keep it that way; the zero-install property is deliberate.
Requires Python 3.9+; developed on 3.14.

## Non-negotiable: verify with `--dry-run` before any real write

`.env` points at a **live production Engage workspace** (EU region). `audiences clone` creates real
audiences; `activations clone` **deletes real activations**, which stops data flowing to third-party
destinations like Ortto. Every change in this repo so far has been validated read-only via
`--dry-run` plus a local mock. Ask before running anything that writes.

## Architecture

Layered inside `segment.py`, top to bottom:

- **`build_ssl_context()`** — verifying TLS context with a `certifi` → OS-bundle fallback chain.
  Never disable verification: the API token goes over that connection.
- **`Config`** — `.env` parsing (hand-rolled, no dotenv). Real `SEGMENT_*` env vars override the
  file. Region maps `us`/`eu` → host; `SEGMENT_API_HOST` overrides and may include a scheme (that's
  how tests point at a local mock).
- **`SegmentClient`** — `request()` returns an `ApiResult` and **never raises on an HTTP status**;
  `require()` treats non-2xx as fatal. That split is what lets a partial clone report itself instead
  of dying halfway. `paged()` walks cursors and returns `(items, total_entries)`.
- **Payload builders** — `audience_payload`, `connection_payload`, `activation_payload`. Pure
  functions, no I/O, fully unit-tested. New API shapes belong here.
- **Pickers** — `choose_one` / `choose_many` (fzf when available, numbered menu otherwise;
  auto-select when there's one candidate), `choose_mode` for the entry-point question.
- **Flows** — `clone_audience` / `clone_activations`, each returning a report dataclass rendered by
  a matching `print_*_report` that decides the exit code.

Human-facing output goes to **stderr** via `Out`; stdout stays clean for piping.

## Segment API traps (hard-won — do not rediscover these)

Work from the OpenAPI spec, not the rendered docs. The docs HTML gave a wrong path and wrong
pagination style. The spec (v73.3.0) is extractable from the `redocly-state-*.js` bundle linked off
<https://docs.segmentapis.com/tag/Activations>; `api.segmentapis.com/openapi.yaml` returns 401.

- Destinations live at **`/destination-connections`**, not `/destinations`.
- Pagination is **`pagination.count=N`** / `pagination.cursor=…` (dot notation). Bracket form does
  not work.
- Destination-connection and activation endpoints are **`v1alpha`** and reject `application/json` —
  send `application/vnd.segment.v1alpha+json` (the `V1ALPHA` constant).
- **Every input schema sets `additionalProperties: false`.** Whitelist fields; never echo a read
  response back. An unexpected key is a 422.
- An audience `definition` accepts **only `query` + `targetEntity`**. There is no `type` field —
  that belongs to *trait* definitions. Sending it 422s.
- `backfillEventDataDays` is only valid when `includeHistoricalData` is true.
- `personalization.entities` must be **omitted entirely** for classic (USERS/ACCOUNTS) audiences,
  not sent as `[]`. Read responses include `entities: []` on classic audiences; echoing it is a 400.
  Hence `activation_payload(..., include_entities=...)`.
- **Activations are created, never patched into existence.** `updateActivationForAudience` (PATCH)
  only mutates an existing one, and `addDestinationToAudience` does not auto-create one. Use
  `POST …/destination-connections/{connectionId}/activations`.
- Activation **delete** is `DELETE /spaces/{spaceId}/audiences/{audienceId}/activations/{id}` —
  directly under the audience, *not* nested under the destination-connection.
- The connection list has **no `destination` vs `warehouse` discriminator**, so
  `create_connection()` tries `destination` and retries as `warehouse` on 400/422.
- Rate limits on these endpoints are 50–60 req/min, lower than the API default.

### The silently-short list

If the token cannot read a connection's underlying destination, `listDestinationsFromAudience`
returns **200 with `connections: []` but `totalEntries: 1`** — not a 403. Cloning in that state
drops every activation and still looks successful. Hence `paged()` returning `total_entries` and the
`--allow-incomplete` hard stop. The fix is a token permission, not code. (This cost real debugging
time; an initial correct diagnosis was second-guessed into a wrong "transient API" theory before the
user confirmed it was the token.)

## Destructive-operation invariants in `clone_activations`

Replace mode deletes before it creates, so a mid-run failure can leave the destination with *fewer*
activations than it started with. Two rules exist because of that — preserve them:

1. **If any delete fails, no creates run.** Otherwise the stale activation and its replacement are
   both live and double-send events to the destination.
2. **If the run ends short**, print the payloads of everything deleted (`removed_payloads`) so they
   can be rebuilt. These are the *original* definitions, not the source's.

Also: source and target being the same audience/destination pair is refused outright under replace —
it would delete the activations being copied. `--allow-duplicates` only overrides this under
`--no-replace`.

## Testing approach

`test_payloads.py` covers the pure builders and asserts the `additionalProperties: false` traps
above — the point is "this payload contains only what it should", including that `False`/`0`/`[]`
survive while `None` is dropped.

For end-to-end work, stand up a local `http.server` mock, point `SEGMENT_API_HOST` at
`http://127.0.0.1:PORT`, and **make the mock enforce the real schemas** (reject unexpected keys,
reject `entities` on classic audiences, 422 a warehouse sent as `destination`). A permissive mock
hides exactly the bugs that matter. Log requests to a JSONL file and assert on the wire: media
types, pagination style, that activations POST under the *new* connection id, that deletes precede
creates.

Interactive wizards are driven with `pty.fork()` plus a `select` loop that feeds an answer only once
output settles on a prompt. Two gotchas: write and read must interleave (macOS pty buffers are tiny
— a sleep-then-write harness deadlocks), and never name the harness `pty.py` (it shadows the stdlib).

## Environment notes

- **python.org macOS builds ship no CA bundle.** `ssl.create_default_context()` loads 0 certs and
  every HTTPS call fails `CERTIFICATE_VERIFY_FAILED`. Handled in `build_ssl_context()`; the
  machine-wide fix is `"/Applications/Python 3.x/Install Certificates.command"`. This is also why
  the original bash version worked — `curl` uses the OS trust store.
- The project was **ported from bash to Python** after the shell version hit repeated macOS bash 3.2
  bugs (`set -e` is ignored inside `$(…)`). Don't reintroduce shell for this.
- **zsh does not word-split unquoted variables**, so `$FLAGS` in a test one-liner arrives as a single
  argument. Inline the flags.
- Claude Code's secret scrubber rewrites Segment ids (`aud_…`, `spa_…`) as `{AWS_SECRET_KEY}` in tool
  output. They are fine in the user's terminal. When an id is needed, drive the interactive picker
  rather than trying to read the id back.

## Conventions

- Argparse flags are registered at every parser level with `default=argparse.SUPPRESS` so they work
  on either side of the subcommand (`-y activations clone` and `activations clone -y`). Without
  SUPPRESS a subparser's defaults clobber a value given earlier on the line. Read flags via the
  `flag()` helper / `getattr(args, name, default)`.
- Exit codes: `0` success, `1` failure, partial completion, or user abort; `130` on interrupt.
- New audiences default to **disabled**, and copied activations keep the source's own `enabled` flag,
  so nothing starts syncing to a third party before review.

## Known limits

Cloning is within a single space (cross-space needs a second token/space pair — the payload builders
are standalone to make that easy). Audience schedules (`/schedules`, linked-audience only) and
computed-trait dependencies are not copied. `connectionSettings` and `destinationMapping.settings`
are untyped in the spec and have only ever been exercised against a mock and one real Ortto
connection.
