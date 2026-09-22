#!/usr/bin/env python3
"""segment — a small CLI over the Segment Public API.

Clones an Engage audience: its definition, its destination connections, and the
activations that sit on those connections.

Standard library only — no pip install, no jq.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# The destination-connection and activation endpoints are alpha and reject
# application/json.
V1ALPHA = "application/vnd.segment.v1alpha+json"

REGIONS = {
    "us": "api.segmentapis.com",
    "us-west-2": "api.segmentapis.com",
    "eu": "eu1.api.segmentapis.com",
    "eu1": "eu1.api.segmentapis.com",
    "eu-west-1": "eu1.api.segmentapis.com",
}

PAGE_SIZE = 200


class SegmentError(Exception):
    """Fatal, user-facing error. Carries an optional hint."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


# -------------------------------------------------------------------- tls ----

# Fallback CA bundles, tried in order when Python's own trust store is empty.
CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/cert.pem",  # macOS system bundle
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew OpenSSL
    "/usr/local/etc/openssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
)

CERT_HINT = (
    "Python has no CA certificates loaded. The python.org macOS installer does not "
    "populate its trust store — run:\n"
    '    "/Applications/Python 3.x/Install Certificates.command"\n'
    "  (substitute your version) or `pip install certifi`. That fixes it for every "
    "Python program, not just this one."
)


def build_ssl_context() -> ssl.SSLContext:
    """A verifying TLS context that still works on a stock python.org install.

    Those builds point at an etc/openssl/cert.pem the installer never creates, so
    create_default_context() trusts nothing and every request fails with
    CERTIFICATE_VERIFY_FAILED. curl uses the OS trust store, which is why the shell
    version of this tool was unaffected.

    Verification is never disabled — an unverified connection would put the API
    token on the wire against an unauthenticated peer.
    """
    context = ssl.create_default_context()
    if context.get_ca_certs():
        return context

    try:
        import certifi
    except ImportError:
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
        if context.get_ca_certs():
            return context

    for candidate in CA_BUNDLE_CANDIDATES:
        if os.path.exists(candidate):
            try:
                context.load_verify_locations(cafile=candidate)
            except (ssl.SSLError, OSError):
                continue
            if context.get_ca_certs():
                return context

    raise SegmentError("No TLS certificate authorities available.", CERT_HINT)


# ----------------------------------------------------------------- output ----


class Out:
    """Everything human-facing goes to stderr, so stdout stays pipeable."""

    def __init__(self, stream=sys.stderr) -> None:
        self.stream = stream
        tty = hasattr(stream, "isatty") and stream.isatty()
        self.bold = "\033[1m" if tty else ""
        self.dim = "\033[2m" if tty else ""
        self.red = "\033[31m" if tty else ""
        self.green = "\033[32m" if tty else ""
        self.yellow = "\033[33m" if tty else ""
        self.cyan = "\033[36m" if tty else ""
        self.reset = "\033[0m" if tty else ""

    def _w(self, text: str) -> None:
        print(text, file=self.stream)

    def info(self, text: str = "") -> None:
        self._w(text)

    def note(self, text: str) -> None:
        self._w(f"{self.dim}{text}{self.reset}")

    def ok(self, text: str) -> None:
        self._w(f"{self.green}✔{self.reset} {text}")

    def warn(self, text: str) -> None:
        self._w(f"{self.yellow}!{self.reset} {text}")

    def error(self, text: str) -> None:
        self._w(f"{self.red}✘{self.reset} {text}")

    def json_block(self, payload: Any, indent: str = "     ") -> None:
        text = json.dumps(payload, indent=2)
        for line in text.splitlines():
            self._w(indent + line)

    def json_line(self, payload: Any, indent: str = "     ") -> None:
        self._w(indent + json.dumps(payload, separators=(",", ":")))


# ----------------------------------------------------------------- config ----


def parse_env_file(path: str) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE, # comments, optional quotes, optional export."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return values

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass
class Config:
    token: str
    space_id: str
    region: str
    host: str

    @property
    def base_url(self) -> str:
        if self.host.startswith(("http://", "https://")):
            return self.host.rstrip("/")
        return "https://" + self.host.rstrip("/")

    @classmethod
    def load(cls, env_file: str) -> "Config":
        # Real environment variables win over the file, so one-off overrides work.
        values = parse_env_file(env_file)
        values.update({k: v for k, v in os.environ.items() if k.startswith("SEGMENT_")})

        token = values.get("SEGMENT_PUBLIC_API_TOKEN", "").strip()
        space_id = values.get("SEGMENT_SPACE_ID", "").strip()
        region = (values.get("SEGMENT_REGION") or "us").strip()
        host = (values.get("SEGMENT_API_HOST") or "").strip()

        if not token:
            raise SegmentError(
                "SEGMENT_PUBLIC_API_TOKEN is not set.",
                f"Copy .env.example to {env_file} and fill it in.",
            )
        if not space_id:
            raise SegmentError(
                "SEGMENT_SPACE_ID is not set.",
                f"Copy .env.example to {env_file} and fill it in.",
            )
        if not host:
            resolved = REGIONS.get(region.lower())
            if not resolved:
                raise SegmentError(
                    f"Unknown SEGMENT_REGION {region!r} (expected 'us' or 'eu').",
                    "Set SEGMENT_API_HOST to override the region mapping.",
                )
            host = resolved

        return cls(token=token, space_id=space_id, region=region, host=host)


# -------------------------------------------------------------------- api ----


@dataclass
class ApiResult:
    status: int
    data: dict[str, Any] | None
    raw: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def message(self) -> str:
        """Best available human-readable description of a failure."""
        if isinstance(self.data, dict):
            errors = self.data.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                text = errors[0].get("message")
                if text:
                    return f"HTTP {self.status} — {text}"
            text = self.data.get("message")
            if text:
                return f"HTTP {self.status} — {text}"
        body = " ".join((self.raw or "no response body").split())
        return f"HTTP {self.status} — {body[:300]}"


class SegmentClient:
    def __init__(self, config: Config, out: Out, debug: bool = False) -> None:
        self.config = config
        self.out = out
        self.debug = debug
        self._ssl_context: ssl.SSLContext | None = None

    @property
    def ssl_context(self) -> ssl.SSLContext:
        # Built lazily and cached, so an http:// mock never needs a trust store.
        if self._ssl_context is None:
            self._ssl_context = build_ssl_context()
        return self._ssl_context

    # -- plumbing ----------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        media: str = "application/json",
    ) -> ApiResult:
        """Perform one call. Never raises on an HTTP error status — the caller
        decides whether a failure is fatal, which is what lets a partial clone
        report itself instead of dying halfway through."""
        url = self.config.base_url + path
        payload = None
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Accept": f"{media}, application/json",
            "User-Agent": "lensgroup-audience-cloner/1.0",
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = media

        if self.debug:
            self.out.note(f"→ {method} {url}")
            if body is not None:
                self.out.note(f"  body: {json.dumps(body, separators=(',', ':'))}")

        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        context = self.ssl_context if url.startswith("https://") else None
        try:
            with urllib.request.urlopen(request, context=context) as response:
                status = response.status
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLCertVerificationError):
                raise SegmentError(
                    f"TLS verification failed for {self.config.base_url}: {exc.reason}",
                    CERT_HINT,
                ) from exc
            raise SegmentError(
                f"Could not reach {self.config.base_url}: {exc.reason}",
                "Check the network, SEGMENT_REGION, and SEGMENT_API_HOST.",
            ) from exc

        try:
            data = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            data = None

        if self.debug:
            self.out.note(f"← {status} {' '.join((raw or '').split())[:400]}")

        return ApiResult(status=status, data=data, raw=raw)

    def require(
        self,
        method: str,
        path: str,
        body: Any = None,
        media: str = "application/json",
    ) -> dict[str, Any]:
        """Like request(), but a non-2xx is fatal."""
        result = self.request(method, path, body, media)
        if not result.ok:
            hint = None
            if result.status in (401, 403):
                hint = "Check SEGMENT_PUBLIC_API_TOKEN and that it can access this space."
            elif result.status == 404:
                hint = (
                    f"Check SEGMENT_SPACE_ID and SEGMENT_REGION "
                    f"(currently {self.config.region} → {self.config.host})."
                )
            elif result.status == 429:
                hint = "Rate limited — these endpoints allow 50-60 requests/minute."
            raise SegmentError(result.message, hint)
        return result.data or {}

    def paged(
        self, path: str, key: str, media: str = "application/json"
    ) -> tuple[list[dict], int | None]:
        """Walk Segment's cursor pagination.

        The parameters serialise as pagination.count / pagination.cursor — dot
        notation, which is what the OpenAPI spec declares. Bracket form does not
        work.

        Also returns the reported totalEntries. That matters because these endpoints
        can report a count higher than the number of items they actually return —
        resources the token cannot read are counted but filtered out of the body —
        and callers need to notice rather than treat a short list as complete.
        """
        items: list[dict] = []
        total: int | None = None
        cursor: str | None = None
        while True:
            params = {"pagination.count": str(PAGE_SIZE)}
            if cursor:
                params["pagination.cursor"] = cursor
            sep = "&" if "?" in path else "?"
            data = self.require("GET", path + sep + urllib.parse.urlencode(params), media=media)

            block = data.get("data") or {}
            items.extend(block.get(key) or [])
            pagination = block.get("pagination") or {}
            if total is None:
                reported = pagination.get("totalEntries")
                total = reported if isinstance(reported, int) else None
            cursor = pagination.get("next")
            if not cursor:
                return items, total

    # -- audiences ---------------------------------------------------------

    def list_audiences(self) -> list[dict]:
        audiences, _ = self.paged(f"/spaces/{self.config.space_id}/audiences", "audiences")
        return audiences

    def get_audience(self, audience_id: str) -> dict:
        data = self.require("GET", f"/spaces/{self.config.space_id}/audiences/{audience_id}")
        audience = (data.get("data") or {}).get("audience")
        if not audience:
            raise SegmentError(f"Audience {audience_id} returned no audience object.")
        return audience

    def create_audience(self, payload: dict) -> dict:
        data = self.require("POST", f"/spaces/{self.config.space_id}/audiences", payload)
        return (data.get("data") or {}).get("audience") or {}

    def list_connections(self, audience_id: str) -> tuple[list[dict], int | None]:
        return self.paged(
            f"/spaces/{self.config.space_id}/audiences/{audience_id}/destination-connections",
            "connections",
            media=V1ALPHA,
        )

    def list_activations(self, audience_id: str) -> tuple[list[dict], int | None]:
        return self.paged(
            f"/spaces/{self.config.space_id}/audiences/{audience_id}/activations",
            "activations",
            media=V1ALPHA,
        )

    def add_connection(self, audience_id: str, payload: dict) -> ApiResult:
        return self.request(
            "POST",
            f"/spaces/{self.config.space_id}/audiences/{audience_id}/destination-connections",
            payload,
            media=V1ALPHA,
        )

    def add_activation(self, audience_id: str, connection_id: str, payload: dict) -> ApiResult:
        return self.request(
            "POST",
            f"/spaces/{self.config.space_id}/audiences/{audience_id}"
            f"/destination-connections/{connection_id}/activations",
            payload,
            media=V1ALPHA,
        )

    def remove_activation(self, audience_id: str, activation_id: str) -> ApiResult:
        # Note the path: activations are deleted directly under the audience, not
        # under the destination-connection they belong to.
        return self.request(
            "DELETE",
            f"/spaces/{self.config.space_id}/audiences/{audience_id}/activations/{activation_id}",
            media=V1ALPHA,
        )


# --------------------------------------------------------------- payloads ----
#
# Every Segment input schema below sets additionalProperties:false, so fields are
# whitelisted rather than copied wholesale — an unexpected key is a 422, not a
# warning.


def _without_nones(mapping: dict) -> dict:
    """Drop None values but keep False, 0 and [] — those are meaningful here."""
    return {k: v for k, v in mapping.items() if v is not None}


def _pick(source: Any, *keys: str) -> dict:
    if not isinstance(source, dict):
        return {}
    return _without_nones({k: source.get(k) for k in keys})


def audience_payload(
    source: dict,
    name: str,
    description: str,
    enabled: bool,
    include_historical: bool,
) -> dict:
    """Body for POST /audiences.

    AudienceDefinition accepts only query + targetEntity. There is no `type` field
    on an audience definition (that belongs to trait definitions) and sending one
    is rejected.
    """
    definition_src = source.get("definition") or {}
    definition: dict[str, Any] = {"query": definition_src.get("query")}
    if definition_src.get("targetEntity") is not None:
        definition["targetEntity"] = definition_src["targetEntity"]

    options = _pick(
        source.get("options"),
        "includeHistoricalData",
        "filterByExternalIds",
        "backfillEventDataDays",
    )
    options["includeHistoricalData"] = include_historical
    if not include_historical:
        # Only valid alongside includeHistoricalData.
        options.pop("backfillEventDataDays", None)

    payload: dict[str, Any] = {
        "name": name,
        "enabled": enabled,
        "audienceType": source.get("audienceType") or "USERS",
        "definition": definition,
        "options": options,
    }
    if description:
        payload["description"] = description
    return payload


def connection_payload(connection: dict, destination_type: str) -> dict:
    """Body for POST /destination-connections.

    destination.id is the source connection's destinationId — the two share the
    same field description in the spec. `type` distinguishes a streaming
    Destination from a warehouse.
    """
    payload: dict[str, Any] = {
        "destination": {
            "id": connection.get("destinationId"),
            "type": destination_type,
        }
    }
    id_sync = connection.get("idSyncConfiguration") or []
    if id_sync:
        payload["idSyncConfiguration"] = [
            _pick(entry, "externalId", "strategy", "mapTo") for entry in id_sync
        ]
    if connection.get("connectionSettings") is not None:
        payload["connectionSettings"] = connection["connectionSettings"]
    return payload


def activation_payload(
    activation: dict, perform_resync: bool, include_entities: bool = True
) -> dict:
    """Body for POST /destination-connections/{id}/activations.

    id / workspaceId / spaceId / audienceId / connectionId are all server-side and
    must not be sent.
    """
    payload: dict[str, Any] = {
        "activationName": activation.get("activationName"),
        "activationType": activation.get("activationType"),
        "enabled": bool(activation.get("enabled")),
        "performResync": perform_resync,
    }
    if activation.get("displayName") is not None:
        payload["displayName"] = activation["displayName"]

    personalization = activation.get("personalization")
    if isinstance(personalization, dict):
        block: dict[str, Any] = {}
        if isinstance(personalization.get("profile"), dict):
            block["profile"] = _pick(personalization["profile"], "properties", "mapping")
        # Only forward entities when there are some, and only onto a linked audience.
        # Read responses on classic audiences include `entities: []`, but the create
        # schema rejects the key outright for non-linked audiences: "Providing
        # entities for a Classic audience returns a 400 error."
        if include_entities and personalization.get("entities"):
            block["entities"] = [
                _pick(entity, "properties", "relationshipSlug")
                for entity in personalization["entities"]
            ]
        if personalization.get("syncEntityPropertyChanges") is not None:
            block["syncEntityPropertyChanges"] = personalization["syncEntityPropertyChanges"]
        if block:
            payload["personalization"] = block

    mapping = activation.get("destinationMapping")
    if isinstance(mapping, dict):
        payload["destinationMapping"] = _pick(mapping, "actionId", "settings")

    return payload


# ---------------------------------------------------------------- prompts ----


class Prompter:
    """Reads from the terminal so piped stdin doesn't silently answer prompts."""

    def __init__(self, assume_yes: bool = False) -> None:
        self.assume_yes = assume_yes
        self._tty = None
        if not assume_yes:
            try:
                self._tty = open("/dev/tty", encoding="utf-8")
            except OSError:
                self._tty = None

    @property
    def interactive(self) -> bool:
        return not self.assume_yes and self._tty is not None

    def ask(self, question: str, default: str = "") -> str:
        if self.assume_yes or self._tty is None:
            return default
        suffix = f" [{default}]" if default else ""
        print(f"{question}{suffix}: ", end="", file=sys.stderr, flush=True)
        reply = self._tty.readline()
        if not reply:  # EOF
            return default
        return reply.strip() or default

    def ask_bool(self, question: str, default: bool) -> bool:
        reply = self.ask(f"{question} (y/n)", "y" if default else "n")
        return reply.strip().lower().startswith("y")

    def confirm(self, question: str) -> bool:
        if self.assume_yes:
            return True
        return self.ask(f"{question} [y/N]").strip().lower().startswith("y")


def _fzf_pick(items: list[dict], label, heading: str, multi: bool) -> list[dict] | None:
    """Returns the chosen items, or None when fzf is unavailable or cancelled."""
    if not shutil.which("fzf"):
        return None
    lines = "".join(f"{index}\t{label(item)}\n" for index, item in enumerate(items))
    command = [
        "fzf",
        "--with-nth=2..",
        "--delimiter=\t",
        "--height=40%",
        "--reverse",
        f"--prompt={heading} > ",
        "--header=type to filter, enter to select"
        + (", tab to mark multiple" if multi else ""),
    ]
    if multi:
        command.append("--multi")
    try:
        proc = subprocess.run(command, input=lines, capture_output=True, text=True)
    except OSError:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    chosen = []
    for line in proc.stdout.splitlines():
        index = line.split("\t", 1)[0].strip()
        if index.isdigit():
            chosen.append(items[int(index)])
    return chosen or None


def choose_one(
    items: list[dict], label, heading: str, question: str, out: Out, prompter: Prompter
) -> dict:
    """Pick exactly one item: fzf when available, numbered menu otherwise.

    A single candidate is selected automatically — there is nothing to decide.
    """
    if not items:
        raise SegmentError(f"Nothing to choose from: no {heading.lower()} available.")
    if len(items) == 1:
        out.note(f"  only one option, selecting: {label(items[0])}")
        return items[0]

    if prompter.interactive:
        picked = _fzf_pick(items, label, heading, multi=False)
        if picked:
            return picked[0]

    out.info()
    out.info(f"{out.bold}{heading}{out.reset}")
    for index, item in enumerate(items, start=1):
        out.info(f"  {out.cyan}{index:3d}{out.reset}  {label(item)}")
    out.info()

    while True:
        reply = prompter.ask(f"{question} (1-{len(items)})")
        if reply.isdigit() and 1 <= int(reply) <= len(items):
            return items[int(reply) - 1]
        if not prompter.interactive:
            raise SegmentError(
                f"Cannot choose {heading.lower()} non-interactively.",
                "Pass the id explicitly instead of relying on the picker.",
            )
        out.warn(f"Enter a number between 1 and {len(items)}.")


def choose_many(
    items: list[dict], label, heading: str, question: str, out: Out, prompter: Prompter
) -> list[dict]:
    """Pick one or more items. Accepts '1,3', ranges like '2-4', or 'a' for all."""
    if not items:
        raise SegmentError(f"Nothing to choose from: no {heading.lower()} available.")

    if prompter.interactive:
        picked = _fzf_pick(items, label, heading, multi=True)
        if picked:
            return picked

    out.info()
    out.info(f"{out.bold}{heading}{out.reset}")
    for index, item in enumerate(items, start=1):
        out.info(f"  {out.cyan}{index:3d}{out.reset}  {label(item)}")
    out.info()

    while True:
        reply = prompter.ask(f"{question} (1-{len(items)}, comma separated, or 'a' for all)", "a")
        if reply.strip().lower() in ("a", "all"):
            return list(items)
        selected: list[dict] = []
        valid = True
        for part in reply.replace(" ", "").split(","):
            if not part:
                continue
            if "-" in part.lstrip("-"):
                start, _, end = part.partition("-")
                if not (start.isdigit() and end.isdigit()):
                    valid = False
                    break
                bounds = range(int(start), int(end) + 1)
            elif part.isdigit():
                bounds = range(int(part), int(part) + 1)
            else:
                valid = False
                break
            for number in bounds:
                if not 1 <= number <= len(items):
                    valid = False
                    break
                if items[number - 1] not in selected:
                    selected.append(items[number - 1])
            if not valid:
                break
        if valid and selected:
            return selected
        if not prompter.interactive:
            raise SegmentError(f"Cannot choose {heading.lower()} non-interactively.")
        out.warn(f"Enter numbers between 1 and {len(items)}, or 'a' for all.")


def audience_label(audience: dict) -> str:
    state = "enabled" if audience.get("enabled") else "disabled"
    kind = audience.get("audienceType") or "-"
    return (
        f"{audience.get('name') or '(unnamed)'}   "
        f"{audience.get('key') or ''}   [{kind}, {state}]"
    )


def connection_label(connection: dict) -> str:
    name = (connection.get("metadata") or {}).get("name") or connection.get("name") or "?"
    state = "enabled" if connection.get("enabled") else "disabled"
    return f"{name}   {connection.get('destinationId') or ''}   [{state}]"


def activation_label(activation: dict) -> str:
    state = "enabled" if activation.get("enabled") else "disabled"
    return (
        f"{activation.get('activationName') or '(unnamed)'}   "
        f"[{activation.get('activationType') or '-'}, {state}]"
    )


# What a bare `./segment.py` offers, in menu order.
MODES = (
    (
        "audiences",
        "Audience",
        "create a NEW audience from an existing one, with its destinations and activations",
    ),
    (
        "activations",
        "Activations only",
        "copy activations onto an audience and destination that already exist",
    ),
)


def choose_mode(out: Out, prompter: Prompter) -> str:
    """Ask what to clone. Falls back to the audience clone when non-interactive."""
    if not prompter.interactive:
        return MODES[0][0]

    out.info()
    out.info(f"{out.bold}What do you want to clone?{out.reset}")
    for index, (_, title, description) in enumerate(MODES, start=1):
        out.info(f"  {out.cyan}{index}{out.reset}  {out.bold}{title}{out.reset} — {description}")
    out.info()

    while True:
        reply = prompter.ask(f"Choice (1-{len(MODES)})", "1")
        if reply.isdigit() and 1 <= int(reply) <= len(MODES):
            return MODES[int(reply) - 1][0]
        out.warn(f"Enter a number between 1 and {len(MODES)}.")


def pick_audience(audiences: list[dict], out: Out, prompter: Prompter) -> dict:
    return choose_one(
        sorted(audiences, key=lambda a: (a.get("name") or "").lower()),
        audience_label,
        "Audiences",
        "Number of the audience to clone",
        out,
        prompter,
    )


# ------------------------------------------------------------------ clone ----


@dataclass
class CloneOptions:
    dry_run: bool = False
    with_destinations: bool = True
    with_activations: bool = True
    resync: bool | None = None
    allow_incomplete: bool = False


@dataclass
class CloneReport:
    audience_id: str = ""
    audience_name: str = ""
    connections_total: int = 0
    connections_done: int = 0
    activations_total: int = 0
    activations_done: int = 0
    failures: list[str] = field(default_factory=list)


def create_connection(
    client: SegmentClient, audience_id: str, connection: dict
) -> tuple[str | None, ApiResult]:
    """Create one destination connection, returning its new id.

    The list response carries no destination/warehouse discriminator, so try
    `destination` and fall back to `warehouse` on a validation error.
    """
    last: ApiResult | None = None
    for destination_type in ("destination", "warehouse"):
        result = client.add_connection(
            audience_id, connection_payload(connection, destination_type)
        )
        last = result
        if result.ok:
            new_id = ((result.data or {}).get("data") or {}).get("connection", {}).get("id")
            return (new_id or None), result
        if result.status not in (400, 422):
            break
    return None, last  # type: ignore[return-value]


def clone_audience(
    client: SegmentClient,
    out: Out,
    prompter: Prompter,
    source_id: str | None,
    options: CloneOptions,
) -> CloneReport:
    if not source_id:
        out.info(f"Fetching audiences from {out.dim}{client.config.host}{out.reset}…")
        source = pick_audience(client.list_audiences(), out, prompter)
        source_id = source["id"]
        source = client.get_audience(source_id)
    else:
        source = client.get_audience(source_id)

    name = source.get("name") or ""
    query = (source.get("definition") or {}).get("query")
    if not query:
        raise SegmentError(
            f"Audience {name!r} has no definition query the API can read — cannot clone it."
        )

    # Fetched up front so the plan is complete before anything is written.
    connections: list[dict] = []
    connections_total: int | None = None
    activations: list[dict] = []
    if options.with_destinations:
        connections, connections_total = client.list_connections(source_id)
    if options.with_activations:
        activations, _ = client.list_activations(source_id)

    # When the token cannot read a connection's underlying destination, this endpoint
    # still counts it in totalEntries but omits it from `connections` — a 200 with a
    # silently short list rather than a 403. A clone run in that state would drop
    # every activation (each needs its source connection to map onto a new one) and
    # still report success, so treat a short list as a hard stop.
    hidden = (connections_total or 0) - len(connections)
    if hidden > 0 and not options.allow_incomplete:
        visible = {connection.get("id") for connection in connections}
        orphans = [a for a in activations if a.get("connectionId") not in visible]
        raise SegmentError(
            f"The API reports {connections_total} destination connection(s) on this audience "
            f"but only returned {len(connections)}, so {len(orphans)} activation(s) could not "
            "be cloned.",
            "The token almost certainly lacks access to the underlying destination — Segment "
            "counts those but filters them out of the response instead of returning 403. Grant "
            "it destination access (Workspace Settings → Access Management → Tokens). Use "
            "--allow-incomplete to clone the definition plus whatever is visible.",
        )
    if hidden > 0:
        out.warn(
            f"{hidden} destination connection(s) were not returned by the API and will be "
            "skipped, along with any activation that targets them."
        )

    out.info()
    out.info(
        f"{out.bold}Source{out.reset} {name}  "
        f"{out.dim}({source.get('audienceType')}, {source_id}){out.reset}"
    )
    out.note(f"  query:        {query[:200]}{'…' if len(query) > 200 else ''}")
    out.note(f"  destinations: {len(connections)}")
    out.note(f"  activations:  {len(activations)}")
    for connection in connections:
        nice = (connection.get("metadata") or {}).get("name") or connection.get("name") or ""
        state = "enabled" if connection.get("enabled") else "disabled"
        out.note(f"    - {nice}  [{state}]")
    out.info()

    new_name = prompter.ask("New audience name", f"{name} (copy)")
    if not new_name:
        raise SegmentError("A name is required.")
    description = prompter.ask("Description", source.get("description") or "")
    enabled = prompter.ask_bool("Enable the new audience immediately?", False)
    historical = prompter.ask_bool(
        "Include historical data?", bool((source.get("options") or {}).get("includeHistoricalData"))
    )
    resync = options.resync
    if resync is None:
        resync = (
            prompter.ask_bool("Resync the full audience to destinations on creation?", False)
            if activations
            else False
        )

    payload = audience_payload(source, new_name, description, enabled, historical)

    out.info(f"{out.bold}Plan{out.reset}")
    out.info("  1. create audience:")
    out.json_block(payload)
    if connections:
        out.info(f"  2. attach {len(connections)} destination connection(s):")
        for connection in connections:
            out.json_line(connection_payload(connection, "destination"))
    if activations:
        out.info(f"  3. create {len(activations)} activation(s):")
        for activation in activations:
            out.json_line(activation_payload(activation, resync))
    out.info()

    report = CloneReport(
        audience_name=new_name,
        connections_total=len(connections),
        activations_total=len(activations),
    )

    if options.dry_run:
        out.warn("--dry-run: nothing was created.")
        return report

    if not prompter.confirm(f"Create this in space {client.config.space_id}?"):
        out.warn("Aborted.")
        raise SystemExit(1)

    # 1. The audience itself. Fatal, since nothing else can proceed without it.
    created = client.create_audience(payload)
    new_id = created.get("id") or ""
    report.audience_id = new_id
    key = created.get("key") or ""
    out.ok(f"Audience {new_name!r} — id {new_id}" + (f", key {key}" if key else ""))

    # 2. Destination connections. Past this point failures are collected rather
    #    than fatal: the audience exists, so bailing out would leave a half-built
    #    clone with no summary of what is missing.
    id_map: dict[str, str] = {}
    for connection in connections:
        nice = (
            (connection.get("metadata") or {}).get("name")
            or connection.get("name")
            or connection.get("destinationId")
            or "?"
        )
        new_connection_id, result = create_connection(client, new_id, connection)
        if new_connection_id:
            id_map[connection.get("id") or ""] = new_connection_id
            report.connections_done += 1
            out.ok(f"Destination {nice!r} — connection {new_connection_id}")
        else:
            detail = result.message if result else "no response"
            report.failures.append(f"destination {nice!r}: {detail}")
            out.warn(f"Destination {nice!r} failed: {detail}")

    # 3. Activations, each against the new connection that replaced its source.
    for activation in activations:
        act_name = activation.get("activationName") or activation.get("id") or "?"
        new_connection_id = id_map.get(activation.get("connectionId") or "")
        if not new_connection_id:
            report.failures.append(
                f"activation {act_name!r}: its destination connection was not cloned"
            )
            out.warn(f"Activation {act_name!r} skipped — its destination connection was not cloned.")
            continue

        result = client.add_activation(
            new_id, new_connection_id, activation_payload(activation, resync)
        )
        if result.ok:
            report.activations_done += 1
            created_id = ((result.data or {}).get("data") or {}).get("activation", {}).get("id", "?")
            out.ok(f"Activation {act_name!r} — id {created_id}")
        else:
            report.failures.append(f"activation {act_name!r}: {result.message}")
            out.warn(f"Activation {act_name!r} failed: {result.message}")

    return report


def print_report(out: Out, report: CloneReport, dry_run: bool) -> int:
    if dry_run:
        return 0

    out.info()
    out.info(
        f"{out.bold}Cloned{out.reset} {report.audience_name}  "
        f"{out.dim}({report.audience_id}){out.reset}"
    )
    out.info("  audience     1/1")
    out.info(f"  destinations {report.connections_done}/{report.connections_total}")
    out.info(f"  activations  {report.activations_done}/{report.activations_total}")
    out.note(f"  https://app.segment.com/_/audiences/{report.audience_id}")

    if report.failures:
        out.info()
        out.warn(f"{len(report.failures)} part(s) of the clone failed:")
        for failure in report.failures:
            out.info(f"    - {failure}")
        out.note("  The audience exists; re-run for the missing pieces or finish them in the UI.")
        return 1

    out.ok("Clone complete.")
    return 0


# ------------------------------------------------- activations: copy across ----


@dataclass
class ActivationCloneOptions:
    dry_run: bool = False
    resync: bool = False
    allow_duplicates: bool = False
    take_all: bool = False
    replace: bool = True
    from_audience: str | None = None
    from_connection: str | None = None
    to_audiences: list[str] = field(default_factory=list)
    to_connections: list[str] = field(default_factory=list)
    activation_ids: list[str] = field(default_factory=list)


@dataclass
class ActivationCloneReport:
    """What happened on one target audience/destination pair."""

    target_name: str = ""
    target_audience_id: str = ""
    total: int = 0
    done: int = 0
    skipped: int = 0
    remove_total: int = 0
    removed: int = 0
    failures: list[str] = field(default_factory=list)
    # Payloads of everything deleted, so a failed run can be rebuilt by hand.
    removed_payloads: list[dict] = field(default_factory=list)
    creates_aborted: bool = False
    # False when an earlier target's failed delete stopped the run before this one.
    attempted: bool = True


@dataclass
class ActivationCloneRun:
    """One `activations clone` invocation, which may cover several targets."""

    targets: list[ActivationCloneReport] = field(default_factory=list)


@dataclass
class TargetPlan:
    """A single target's resolved pair and the work queued against it."""

    audience: dict
    connection: dict
    include_entities: bool
    to_remove: list[dict] = field(default_factory=list)
    planned: list[dict] = field(default_factory=list)
    skipped_names: list[str] = field(default_factory=list)

    @property
    def audience_id(self) -> str:
        return self.audience.get("id") or ""

    @property
    def connection_id(self) -> str:
        return self.connection.get("id") or ""


def pair_connections(target_ids: list[str], connection_ids: list[str]) -> list[str | None]:
    """Line up `--to-connection` ids with `--to-audience` ids, positionally.

    A connection id is scoped to one audience/destination pair, so one id cannot
    serve several targets — either give one per target, in order, or none at all
    and let the picker resolve each.
    """
    if not connection_ids:
        return [None] * len(target_ids)
    if len(connection_ids) == len(target_ids):
        return list(connection_ids)
    raise SegmentError(
        f"Got {len(connection_ids)} --to-connection id(s) for {len(target_ids)} target "
        "audience(s).",
        "Connection ids belong to a single audience, so pass one --to-connection per "
        "--to-audience in the same order, or none and pick them interactively.",
    )


def plan_target(
    selected: list[dict],
    on_target: list[dict],
    replace: bool,
    allow_duplicates: bool,
) -> tuple[list[dict], list[dict], list[str]]:
    """Decide what to delete and create on one target. Returns (remove, create, skipped).

    Replace mode (the default) clears the target destination first, so it ends up
    mirroring the source exactly rather than accumulating. Without it, names that
    already exist are skipped instead.
    """
    if replace:
        return list(on_target), list(selected), []

    existing_names = {a.get("activationName") for a in on_target}
    planned: list[dict] = []
    skipped_names: list[str] = []
    for activation in selected:
        name = activation.get("activationName")
        if name in existing_names and not allow_duplicates:
            skipped_names.append(str(name))
            continue
        planned.append(activation)
    return [], planned, skipped_names


def _resolve_audience(
    client: SegmentClient,
    out: Out,
    prompter: Prompter,
    audience_id: str | None,
    heading: str,
    question: str,
    cache: dict[str, list[dict]],
) -> dict:
    if audience_id:
        return client.get_audience(audience_id)
    if "audiences" not in cache:
        cache["audiences"] = client.list_audiences()
    ordered = sorted(cache["audiences"], key=lambda a: (a.get("name") or "").lower())
    chosen = choose_one(ordered, audience_label, heading, question, out, prompter)
    # The list payload is a summary; fetch the full object for audienceType etc.
    return client.get_audience(chosen["id"])


def _resolve_audiences(
    client: SegmentClient,
    out: Out,
    prompter: Prompter,
    audience_ids: list[str],
    heading: str,
    question: str,
    cache: dict[str, list[dict]],
    exclude_id: str = "",
) -> list[dict]:
    """Target-side counterpart of `_resolve_audience`: one or more audiences.

    `exclude_id` is kept out of the picker — the source audience is not a useful
    target, and under replace it is refused outright later anyway. An explicit
    --to-audience is still honoured, for the one real case: the same audience on a
    *different* destination connection.

    Duplicates are collapsed — picking the same audience twice is one run over it,
    not two.
    """
    if audience_ids:
        chosen = [client.get_audience(audience_id) for audience_id in audience_ids]
    else:
        # choose_many defaults to "all", and a non-interactive prompt takes the
        # default — which here would mean every audience in the space, with replace
        # mode deleting the activations on each. Refuse instead.
        if not prompter.interactive:
            raise SegmentError(
                "No target audience given and there is no terminal to pick one.",
                "Pass --to-audience ID (repeatable) — this flow will not default to every "
                "audience in the space.",
            )
        if "audiences" not in cache:
            cache["audiences"] = client.list_audiences()
        candidates = [a for a in cache["audiences"] if (a.get("id") or "") != exclude_id]
        if not candidates:
            raise SegmentError(
                "There is no other audience in this space to copy activations into.",
                "Create the target audience first, or pass --to-audience to target the source "
                "audience itself on a different destination.",
            )
        ordered = sorted(candidates, key=lambda a: (a.get("name") or "").lower())
        picked = choose_many(ordered, audience_label, heading, question, out, prompter)
        # The list payload is a summary; fetch the full object for audienceType etc.
        chosen = [client.get_audience(audience["id"]) for audience in picked]

    seen: set[str] = set()
    unique: list[dict] = []
    for audience in chosen:
        audience_id = audience.get("id") or ""
        if audience_id in seen:
            out.note(f"  skipping repeated target: {audience.get('name')} ({audience_id})")
            continue
        seen.add(audience_id)
        unique.append(audience)
    return unique


def _resolve_connection(
    client: SegmentClient,
    out: Out,
    prompter: Prompter,
    audience_id: str,
    connection_id: str | None,
    heading: str,
    question: str,
) -> dict:
    connections, total = client.list_connections(audience_id)
    hidden = (total or 0) - len(connections)
    if hidden > 0:
        out.warn(
            f"{hidden} of {total} destination connection(s) on this audience are not readable "
            "with this token and are not listed below."
        )
    if not connections:
        raise SegmentError(
            f"Audience {audience_id} has no readable destination connections.",
            "Connect the destination in Engage first, and make sure the token can read it.",
        )
    if connection_id:
        for connection in connections:
            if connection.get("id") == connection_id:
                return connection
        raise SegmentError(
            f"Connection {connection_id} not found on audience {audience_id}.",
            "Available: "
            + ", ".join(str(c.get("id")) for c in connections),
        )
    return choose_one(connections, connection_label, heading, question, out, prompter)


def clone_activations(
    client: SegmentClient,
    out: Out,
    prompter: Prompter,
    options: ActivationCloneOptions,
) -> ActivationCloneRun:
    """Copy activations from one (audience, destination connection) pair onto others.

    Every side must already exist — this creates only the activations, using the same
    addActivationToAudience endpoint the full clone uses. One source fans out to any
    number of target audiences, each with its own destination connection.
    """
    cache: dict[str, list[dict]] = {}

    # --- source side ---
    out.info(f"Fetching audiences from {out.dim}{client.config.host}{out.reset}…")
    source_audience = _resolve_audience(
        client, out, prompter, options.from_audience,
        "Source audiences", "Number of the audience to copy activations FROM", cache,
    )
    source_id = source_audience["id"]
    out.note(f"  from audience: {source_audience.get('name')} ({source_id})")

    source_connection = _resolve_connection(
        client, out, prompter, source_id, options.from_connection,
        "Source destinations", "Number of the destination to copy activations FROM",
    )
    source_connection_id = source_connection.get("id") or ""
    out.note(f"  from destination: {connection_label(source_connection)}")

    all_activations, _ = client.list_activations(source_id)
    candidates = [
        activation
        for activation in all_activations
        if activation.get("connectionId") == source_connection_id
    ]
    if not candidates:
        raise SegmentError(
            "That destination has no activations to copy.",
            f"The audience has {len(all_activations)} activation(s) in total, but none on "
            "this connection.",
        )

    if options.activation_ids:
        wanted = set(options.activation_ids)
        by_id = {a.get("id"): a for a in candidates}
        missing = wanted - set(by_id)
        if missing:
            raise SegmentError(
                "Activation(s) not found on that destination: " + ", ".join(sorted(missing)),
                "Available: " + ", ".join(f"{a.get('id')} ({a.get('activationName')})" for a in candidates),
            )
        selected = [by_id[activation_id] for activation_id in options.activation_ids]
    elif options.take_all or not prompter.interactive:
        selected = candidates
    else:
        selected = choose_many(
            candidates, activation_label, "Activations on that destination",
            "Which activations to copy", out, prompter,
        )

    # --- target side ---
    target_audiences = _resolve_audiences(
        client, out, prompter, options.to_audiences,
        "Target audiences", "Which audiences to copy activations INTO", cache,
        exclude_id=source_id,
    )
    wanted_connections = pair_connections(
        [a.get("id") or "" for a in target_audiences], options.to_connections
    )

    plans: list[TargetPlan] = []
    for target_audience, wanted_connection in zip(target_audiences, wanted_connections):
        target_id = target_audience["id"]
        target_type = target_audience.get("audienceType") or "USERS"
        out.note(f"  into audience: {target_audience.get('name')} ({target_id})")

        target_connection = _resolve_connection(
            client, out, prompter, target_id, wanted_connection,
            f"Destinations on {target_audience.get('name')}",
            "Number of the destination to copy activations INTO",
        )
        out.note(f"  into destination: {connection_label(target_connection)}")

        # --- guards, per target ---
        if source_connection_id == (target_connection.get("id") or ""):
            if options.replace:
                # Replace deletes the target's activations first, which here are the very
                # ones being copied — a no-op at best, data loss at worst.
                raise SegmentError(
                    f"{target_audience.get('name')} is the same audience/destination pair as "
                    "the source.",
                    "Replace mode would delete the activations it is copying from. Pick a "
                    "different target.",
                )
            if not options.allow_duplicates:
                raise SegmentError(
                    f"{target_audience.get('name')} is the same audience/destination pair as "
                    "the source.",
                    "That would duplicate the activations in place. Pick a different target, or "
                    "pass --allow-duplicates if duplicates are really what you want.",
                )

        if source_connection.get("destinationId") != target_connection.get("destinationId"):
            out.warn(
                f"{target_audience.get('name')} points at a different destination than the "
                "source. destinationMapping.actionId and any destination-specific settings are "
                "unlikely to be valid there."
            )

        include_entities = target_type == "LINKED"
        if not include_entities and any(
            (a.get("personalization") or {}).get("entities") for a in selected
        ):
            out.warn(
                f"{target_audience.get('name')} is {target_type}, not LINKED — entity "
                "personalization will be dropped, since classic audiences reject it."
            )

        existing_activations, _ = client.list_activations(target_id)
        on_target = [
            a
            for a in existing_activations
            if a.get("connectionId") == (target_connection.get("id") or "")
        ]
        to_remove, planned, skipped_names = plan_target(
            selected, on_target, options.replace, options.allow_duplicates
        )
        for name in skipped_names:
            out.warn(
                f"Activation {name!r} already exists on {target_audience.get('name')} — skipping."
            )
        plans.append(
            TargetPlan(
                audience=target_audience,
                connection=target_connection,
                include_entities=include_entities,
                to_remove=to_remove,
                planned=planned,
                skipped_names=skipped_names,
            )
        )

    run = ActivationCloneRun(
        targets=[
            ActivationCloneReport(
                target_name=plan.audience.get("name") or "",
                target_audience_id=plan.audience_id,
                total=len(plan.planned),
                remove_total=len(plan.to_remove),
                skipped=len(plan.skipped_names),
            )
            for plan in plans
        ]
    )

    total_remove = sum(len(plan.to_remove) for plan in plans)
    total_create = sum(len(plan.planned) for plan in plans)
    if not total_remove and not total_create:
        out.warn("Nothing to do.")
        return run

    if total_remove:
        out.info()
        out.warn(
            f"{out.bold}This REPLACES the targets' activations.{out.reset} "
            f"{total_remove} existing activation(s) across {len(plans)} target(s) "
            "will be DELETED, then replaced with copies from the source."
        )
        out.note("  Deleting an activation stops that data flowing to the destination.")
        out.note("  Use --no-replace to add the copies alongside what is already there.")

    out.info()
    out.info(f"{out.bold}Plan{out.reset}")
    for index, plan in enumerate(plans, start=1):
        out.info(
            f"  {out.bold}target {index}/{len(plans)}{out.reset}  "
            f"{plan.audience.get('name')} → {connection_label(plan.connection)}"
        )
        step = 1
        if plan.to_remove:
            out.info(f"    {step}. delete {len(plan.to_remove)} existing activation(s):")
            for activation in plan.to_remove:
                out.info(f"         - {activation_label(activation)}  ({activation.get('id')})")
            step += 1
        out.info(f"    {step}. create {len(plan.planned)} activation(s):")
        for activation in plan.planned:
            out.json_line(
                activation_payload(activation, options.resync, plan.include_entities), "         "
            )
    out.info()

    if options.dry_run:
        out.warn("--dry-run: nothing was deleted or created.")
        return run

    scope = f"{len(plans)} target audience(s)"
    question = (
        f"Delete {total_remove} and create {total_create} activation(s) across {scope}?"
        if total_remove
        else f"Create {total_create} activation(s) across {scope}?"
    )
    if not prompter.confirm(question):
        out.warn("Aborted.")
        raise SystemExit(1)

    for index, (plan, report) in enumerate(zip(plans, run.targets)):
        out.info()
        out.info(
            f"{out.bold}target {index + 1}/{len(plans)}{out.reset}  "
            f"{plan.audience.get('name')} → {connection_label(plan.connection)}"
        )
        _apply_target(client, out, options, plan, report)

        # A failed delete means the old activation is still live, so this target's
        # creates were skipped. The cause is rarely target-specific — a token,
        # permission or rate-limit problem recurs — and each further target would
        # delete more live activations before hitting it, so stop the whole run.
        if report.creates_aborted:
            for remaining in run.targets[index + 1 :]:
                remaining.attempted = False
            if index + 1 < len(plans):
                out.warn(
                    f"Stopping: {len(plans) - index - 1} remaining target(s) were not touched."
                )
            break

    return run


def _apply_target(
    client: SegmentClient,
    out: Out,
    options: ActivationCloneOptions,
    plan: TargetPlan,
    report: ActivationCloneReport,
) -> None:
    """Delete then create one target's activations, recording what happened."""
    for activation in plan.to_remove:
        name = activation.get("activationName") or activation.get("id") or "?"
        result = client.remove_activation(plan.audience_id, activation.get("id") or "")
        if result.ok:
            report.removed += 1
            report.removed_payloads.append(
                activation_payload(activation, False, include_entities=True)
            )
            out.ok(f"Deleted {name!r} ({activation.get('id')})")
        else:
            report.failures.append(f"delete {name!r}: {result.message}")
            out.warn(f"Deleting {name!r} failed: {result.message}")

    # A failed delete means the old activation is still live. Creating the copy now
    # would leave two activations feeding the same destination and double-send
    # events, so stop here and let the operator resolve it.
    if any(f.startswith("delete ") for f in report.failures):
        report.creates_aborted = True
        out.warn("Skipping creation because at least one delete failed.")
        return

    for activation in plan.planned:
        name = activation.get("activationName") or activation.get("id") or "?"
        result = client.add_activation(
            plan.audience_id,
            plan.connection_id,
            activation_payload(activation, options.resync, plan.include_entities),
        )
        if result.ok:
            report.done += 1
            new_id = ((result.data or {}).get("data") or {}).get("activation", {}).get("id", "?")
            out.ok(f"Activation {name!r} — id {new_id}")
        else:
            report.failures.append(f"activation {name!r}: {result.message}")
            out.warn(f"Activation {name!r} failed: {result.message}")


def print_activation_run(out: Out, run: ActivationCloneRun, dry_run: bool) -> int:
    """Render every target's outcome and decide the exit code for the run."""
    if dry_run:
        return 0
    status = 0
    for report in run.targets:
        if print_activation_report(out, report, dry_run) != 0:
            status = 1
    return status


def print_activation_report(out: Out, report: ActivationCloneReport, dry_run: bool) -> int:
    if dry_run:
        return 0
    out.info()
    if not report.attempted:
        out.warn(
            f"Not attempted: {report.target_name}  "
            f"{out.dim}({report.target_audience_id}){out.reset} — the run stopped after a "
            "failed delete on an earlier target."
        )
        return 1
    out.info(
        f"{out.bold}Copied{out.reset} into {report.target_name}  "
        f"{out.dim}({report.target_audience_id}){out.reset}"
    )
    if report.remove_total:
        out.info(f"  deleted      {report.removed}/{report.remove_total}")
    out.info(f"  created      {report.done}/{report.total}")
    if report.skipped:
        out.info(f"  skipped      {report.skipped} (already present)")
    if report.target_audience_id:
        out.note(f"  https://app.segment.com/_/audiences/{report.target_audience_id}")

    if report.failures:
        out.info()
        out.warn(f"{len(report.failures)} step(s) failed:")
        for failure in report.failures:
            out.info(f"    - {failure}")

        # Deletes are the destructive half. If anything went wrong after one
        # succeeded, the target is missing activations — print what was removed so it
        # can be rebuilt without digging through the Segment UI.
        if report.removed_payloads and (report.creates_aborted or report.done < report.total):
            out.info()
            out.warn(
                f"The target is missing activations. These {len(report.removed_payloads)} "
                "were deleted — recreate them from these payloads if needed:"
            )
            for payload in report.removed_payloads:
                out.json_line(payload, "    ")
        return 1

    if report.total or report.remove_total:
        out.ok("Replace complete." if report.remove_total else "Copy complete.")
    return 0


# ------------------------------------------------------------------- list ----


def list_audiences(client: SegmentClient, out: Out) -> int:
    audiences = client.list_audiences()
    if not audiences:
        out.info(f"No audiences found in space {client.config.space_id}.")
        return 0

    for audience in sorted(audiences, key=lambda a: (a.get("name") or "").lower()):
        state = "on " if audience.get("enabled") else "off"
        kind = audience.get("audienceType") or "-"
        print(f"{state:4} {kind:9} {(audience.get('name') or ''):48} {audience.get('key') or ''}")
    sys.stdout.flush()  # keep the summary below the rows when both are redirected
    out.note(f"{len(audiences)} audience(s) in space {client.config.space_id} ({client.config.host}).")
    return 0


# --------------------------------------------------------------- dispatch ----


def _add_global_flags(parser: argparse.ArgumentParser) -> None:
    """Flags valid for every subcommand.

    Registered at every level with SUPPRESS defaults so they work on either side of
    the subcommand (`-y audiences clone` and `audiences clone -y` both parse).
    Without SUPPRESS a subparser's defaults would clobber a value given earlier on
    the line.
    """
    parser.add_argument(
        "--env", metavar="FILE", default=argparse.SUPPRESS, help="config file (default ./.env)"
    )
    parser.add_argument(
        "--debug", action="store_true", default=argparse.SUPPRESS,
        help="log every request and response",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=argparse.SUPPRESS,
        help="print the plan, create nothing",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", default=argparse.SUPPRESS,
        help="accept all defaults and skip confirmation",
    )
    parser.add_argument(
        "--resync", action="store_true", default=argparse.SUPPRESS,
        help="new activations resync the whole audience on creation",
    )


def _add_audience_clone_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-destinations", action="store_true", default=argparse.SUPPRESS,
        help="clone the definition only (implies --no-activations)",
    )
    parser.add_argument(
        "--no-activations", action="store_true", default=argparse.SUPPRESS,
        help="clone destinations but no activations",
    )
    parser.add_argument(
        "--allow-incomplete", action="store_true", default=argparse.SUPPRESS,
        help="proceed even when some destination connections are unreadable",
    )


def _add_activation_clone_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--from-audience", metavar="ID", default=argparse.SUPPRESS,
        help="source audience id (skips the picker)",
    )
    parser.add_argument(
        "--from-connection", metavar="ID", default=argparse.SUPPRESS,
        help="source destination connection id, ii_…",
    )
    parser.add_argument(
        "--to-audience", metavar="ID", action="append", default=argparse.SUPPRESS,
        help="target audience id (repeatable; skips the picker)",
    )
    parser.add_argument(
        "--to-connection", metavar="ID", action="append", default=argparse.SUPPRESS,
        help="target destination connection id, ii_… (one per --to-audience, same order)",
    )
    parser.add_argument(
        "--activation", metavar="ID", action="append", default=argparse.SUPPRESS,
        help="copy only this activation id (repeatable)",
    )
    parser.add_argument(
        "--all", action="store_true", default=argparse.SUPPRESS,
        help="copy every activation on the source destination",
    )
    parser.add_argument(
        "--no-replace", action="store_true", default=argparse.SUPPRESS,
        help="add the copies alongside existing activations instead of replacing them",
    )
    parser.add_argument(
        "--allow-duplicates", action="store_true", default=argparse.SUPPRESS,
        help="with --no-replace, copy even when that activation name already exists",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="segment.py",
        description="CLI for the Segment Public API.\n\n"
        "  audiences clone      clone an audience with its destinations and activations\n"
        "  audiences list       list audiences in the space\n"
        "  activations clone    copy activations onto an existing audience/destination pair",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "config (.env, see .env.example):\n"
            "  SEGMENT_PUBLIC_API_TOKEN   Public API token\n"
            "  SEGMENT_SPACE_ID           Engage space id\n"
            "  SEGMENT_REGION             us (default) or eu\n"
            "  SEGMENT_API_HOST           optional explicit host override\n"
        ),
    )
    _add_global_flags(parser)
    _add_audience_clone_flags(parser)
    _add_activation_clone_flags(parser)

    groups = parser.add_subparsers(dest="group")

    audiences = groups.add_parser("audiences", help="work with Engage audiences")
    _add_global_flags(audiences)
    _add_audience_clone_flags(audiences)
    audience_actions = audiences.add_subparsers(dest="action")

    _add_global_flags(audience_actions.add_parser("list", help="list audiences in the space"))

    clone = audience_actions.add_parser(
        "clone", help="clone an audience with its destinations and activations"
    )
    clone.add_argument("audience_id", nargs="?", help="skip the picker")
    _add_global_flags(clone)
    _add_audience_clone_flags(clone)

    activations = groups.add_parser("activations", help="work with audience activations")
    _add_global_flags(activations)
    _add_activation_clone_flags(activations)
    activation_actions = activations.add_subparsers(dest="action")

    activation_clone = activation_actions.add_parser(
        "clone", help="copy activations onto an existing audience/destination pair"
    )
    _add_global_flags(activation_clone)
    _add_activation_clone_flags(activation_clone)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    flag = lambda name: bool(getattr(args, name, False))  # noqa: E731
    out = Out()
    prompter = Prompter(assume_yes=flag("yes"))

    group = getattr(args, "group", None)
    action = getattr(args, "action", None) or "clone"

    env_file = getattr(args, "env", None) or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".env"
    )

    try:
        config = Config.load(env_file)
        client = SegmentClient(config, out, debug=flag("debug"))

        # A bare `./segment.py` asks which of the two clone flows to run. When the
        # flags already make the intent obvious, skip the question.
        if group is None:
            if any(
                getattr(args, name, None)
                for name in (
                    "from_audience", "from_connection", "to_audience", "to_connection",
                    "activation", "all", "allow_duplicates", "no_replace",
                )
            ):
                group = "activations"
            elif any(
                getattr(args, name, None)
                for name in ("no_destinations", "no_activations", "allow_incomplete")
            ):
                group = "audiences"
            else:
                group = choose_mode(out, prompter)

        if (group, action) == ("audiences", "list"):
            return list_audiences(client, out)

        if (group, action) == ("audiences", "clone"):
            options = CloneOptions(
                dry_run=flag("dry_run"),
                with_destinations=not flag("no_destinations"),
                with_activations=not (flag("no_activations") or flag("no_destinations")),
                resync=True if flag("resync") else None,
                allow_incomplete=flag("allow_incomplete"),
            )
            report = clone_audience(
                client, out, prompter, getattr(args, "audience_id", None), options
            )
            return print_report(out, report, options.dry_run)

        if (group, action) == ("activations", "clone"):
            activation_options = ActivationCloneOptions(
                dry_run=flag("dry_run"),
                resync=flag("resync"),
                allow_duplicates=flag("allow_duplicates"),
                take_all=flag("all"),
                replace=not flag("no_replace"),
                from_audience=getattr(args, "from_audience", None),
                from_connection=getattr(args, "from_connection", None),
                to_audiences=list(getattr(args, "to_audience", []) or []),
                to_connections=list(getattr(args, "to_connection", []) or []),
                activation_ids=list(getattr(args, "activation", []) or []),
            )
            activation_run = clone_activations(client, out, prompter, activation_options)
            return print_activation_run(out, activation_run, activation_options.dry_run)

        parser.print_help(sys.stderr)
        return 1

    except SegmentError as exc:
        out.error(str(exc))
        if exc.hint:
            out.note(f"  {exc.hint}")
        return 1
    except KeyboardInterrupt:
        out.warn("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
