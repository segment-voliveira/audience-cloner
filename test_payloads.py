#!/usr/bin/env python3
"""Tests for the request-body builders.

These guard the Segment schema traps that are easy to reintroduce. Every input
schema involved sets additionalProperties:false, so a stray key is a 422 rather
than a warning — which makes "does this payload contain only what it should" the
thing worth asserting.

    python3 -m unittest test_payloads -v
"""

import unittest

from segment import activation_payload, audience_payload, connection_payload, parse_env_file

AUDIENCE_KEYS = {"name", "enabled", "description", "definition", "audienceType", "options"}
DEFINITION_KEYS = {"query", "targetEntity"}
OPTION_KEYS = {"includeHistoricalData", "filterByExternalIds", "backfillEventDataDays"}
CONNECTION_KEYS = {"destination", "idSyncConfiguration", "connectionSettings"}
ACTIVATION_KEYS = {
    "enabled",
    "performResync",
    "activationType",
    "activationName",
    "displayName",
    "personalization",
    "destinationMapping",
}


def source_audience(**overrides):
    base = {
        "id": "aud_1",
        "name": "VIP",
        "key": "vip",
        "enabled": True,
        "audienceType": "USERS",
        "description": "High LTV",
        # A read response carries `type`, which the create schema rejects.
        "definition": {"query": "event('X').count() >= 1", "type": "USERS"},
        "options": {"includeHistoricalData": True, "filterByExternalIds": ["user_id"]},
    }
    base.update(overrides)
    return base


class AudiencePayload(unittest.TestCase):
    def test_only_whitelisted_top_level_keys(self):
        payload = audience_payload(source_audience(), "copy", "desc", False, False)
        self.assertLessEqual(set(payload), AUDIENCE_KEYS)

    def test_definition_never_carries_type(self):
        """AudienceDefinition allows query + targetEntity only. `type` belongs to
        trait definitions and is rejected on an audience."""
        payload = audience_payload(source_audience(), "copy", "", False, False)
        self.assertEqual(set(payload["definition"]), {"query"})
        self.assertNotIn("type", payload["definition"])

    def test_target_entity_preserved_for_linked_audiences(self):
        source = source_audience(
            audienceType="LINKED",
            definition={"query": "q", "type": "USERS", "targetEntity": "accounts"},
        )
        payload = audience_payload(source, "copy", "", False, False)
        self.assertEqual(payload["definition"]["targetEntity"], "accounts")
        self.assertEqual(payload["audienceType"], "LINKED")

    def test_null_target_entity_omitted(self):
        source = source_audience(definition={"query": "q", "targetEntity": None})
        payload = audience_payload(source, "copy", "", False, False)
        self.assertNotIn("targetEntity", payload["definition"])

    def test_unknown_option_keys_dropped(self):
        """The read path can return AudienceOptionsWithLookback; only the three
        create-side keys may be forwarded."""
        source = source_audience(
            options={"includeHistoricalData": True, "lookbackDays": 7, "somethingNew": 1}
        )
        payload = audience_payload(source, "copy", "", False, True)
        self.assertLessEqual(set(payload["options"]), OPTION_KEYS)

    def test_backfill_dropped_when_historical_disabled(self):
        source = source_audience(
            options={"includeHistoricalData": True, "backfillEventDataDays": 30}
        )
        payload = audience_payload(source, "copy", "", False, False)
        self.assertNotIn("backfillEventDataDays", payload["options"])
        self.assertIs(payload["options"]["includeHistoricalData"], False)

    def test_backfill_kept_when_historical_enabled(self):
        source = source_audience(
            options={"includeHistoricalData": True, "backfillEventDataDays": 30}
        )
        payload = audience_payload(source, "copy", "", False, True)
        self.assertEqual(payload["options"]["backfillEventDataDays"], 30)

    def test_empty_description_omitted(self):
        payload = audience_payload(source_audience(), "copy", "", False, False)
        self.assertNotIn("description", payload)

    def test_enabled_flag_is_explicit(self):
        self.assertIs(audience_payload(source_audience(), "c", "", False, False)["enabled"], False)
        self.assertIs(audience_payload(source_audience(), "c", "", True, False)["enabled"], True)


class ConnectionPayload(unittest.TestCase):
    def source_connection(self, **overrides):
        base = {
            "id": "ii_abc",  # the connection id — must NOT be sent
            "destinationId": "dest_braze",
            "name": "Braze Prod",
            "enabled": True,
            "createdAt": "2023-01-01T00:00:00Z",
            "settings": {},
            "metadata": {"id": "m", "name": "Braze", "slug": "braze"},
            "idSyncConfiguration": [{"externalId": "email", "strategy": "last", "mapTo": None}],
            "connectionSettings": {"audienceName": "vip"},
        }
        base.update(overrides)
        return base

    def test_only_whitelisted_keys(self):
        payload = connection_payload(self.source_connection(), "destination")
        self.assertLessEqual(set(payload), CONNECTION_KEYS)

    def test_uses_destination_id_not_connection_id(self):
        payload = connection_payload(self.source_connection(), "destination")
        self.assertEqual(payload["destination"], {"id": "dest_braze", "type": "destination"})

    def test_null_map_to_dropped(self):
        payload = connection_payload(self.source_connection(), "destination")
        self.assertEqual(payload["idSyncConfiguration"], [{"externalId": "email", "strategy": "last"}])

    def test_empty_id_sync_omitted(self):
        payload = connection_payload(
            self.source_connection(idSyncConfiguration=[], connectionSettings=None), "warehouse"
        )
        self.assertNotIn("idSyncConfiguration", payload)
        self.assertNotIn("connectionSettings", payload)
        self.assertEqual(payload["destination"]["type"], "warehouse")


class ActivationPayload(unittest.TestCase):
    def source_activation(self, **overrides):
        base = {
            # All of these are server-generated and must not be echoed back.
            "id": "act_1",
            "workspaceId": "ws_1",
            "spaceId": "spa_1",
            "audienceId": "aud_1",
            "connectionId": "ii_abc",
            "activationName": "Braze entered",
            "activationType": "Audience Entered",
            "enabled": True,
            "personalization": {
                "profile": {"properties": ["email"], "mapping": {"email": "Email"}},
                "entities": [{"properties": ["plan"], "relationshipSlug": "accounts"}],
                "syncEntityPropertyChanges": False,
            },
            "destinationMapping": {"actionId": "a1", "settings": {"k": 1}},
        }
        base.update(overrides)
        return base

    def test_drops_server_generated_fields(self):
        payload = activation_payload(self.source_activation(), False)
        self.assertLessEqual(set(payload), ACTIVATION_KEYS)
        for key in ("id", "workspaceId", "spaceId", "audienceId", "connectionId"):
            self.assertNotIn(key, payload)

    def test_required_fields_always_present(self):
        payload = activation_payload(self.source_activation(), True)
        for key in ("activationName", "activationType", "performResync"):
            self.assertIn(key, payload)
        self.assertIs(payload["performResync"], True)

    def test_false_enabled_is_preserved_not_dropped(self):
        payload = activation_payload(self.source_activation(enabled=False), False)
        self.assertIs(payload["enabled"], False)

    def test_empty_property_list_preserved(self):
        payload = activation_payload(
            self.source_activation(personalization={"profile": {"properties": []}}), False
        )
        self.assertEqual(payload["personalization"]["profile"], {"properties": []})

    def test_unknown_nested_keys_stripped(self):
        payload = activation_payload(
            self.source_activation(
                personalization={
                    "profile": {"properties": ["e"], "junk": 1},
                    "entities": [{"properties": ["p"], "relationshipSlug": "r", "extra": 2}],
                },
                destinationMapping={"actionId": "a1", "settings": {}, "nope": 3},
            ),
            False,
        )
        self.assertEqual(set(payload["personalization"]["profile"]), {"properties"})
        self.assertEqual(
            payload["personalization"]["entities"], [{"properties": ["p"], "relationshipSlug": "r"}]
        )
        self.assertEqual(set(payload["destinationMapping"]), {"actionId", "settings"})

    def test_empty_entities_omitted_for_classic_audiences(self):
        """Read responses on classic audiences include `entities: []`, but the create
        schema rejects the key for non-linked audiences with a 400."""
        payload = activation_payload(
            self.source_activation(
                personalization={"profile": {"properties": ["email"]}, "entities": []}
            ),
            False,
        )
        self.assertNotIn("entities", payload["personalization"])

    def test_non_empty_entities_kept_for_linked_audiences(self):
        payload = activation_payload(self.source_activation(), False)
        self.assertEqual(
            payload["personalization"]["entities"],
            [{"properties": ["plan"], "relationshipSlug": "accounts"}],
        )

    def test_include_entities_false_strips_them(self):
        """Copying a linked activation onto a classic audience must drop entities
        rather than 400."""
        payload = activation_payload(self.source_activation(), False, include_entities=False)
        self.assertNotIn("entities", payload["personalization"])
        self.assertIn("profile", payload["personalization"])

    def test_include_entities_false_leaves_no_empty_personalization(self):
        payload = activation_payload(
            self.source_activation(
                personalization={"entities": [{"properties": ["p"], "relationshipSlug": "r"}]}
            ),
            False,
            include_entities=False,
        )
        self.assertNotIn("personalization", payload)

    def test_missing_personalization_omitted(self):
        source = self.source_activation()
        del source["personalization"]
        del source["destinationMapping"]
        payload = activation_payload(source, False)
        self.assertNotIn("personalization", payload)
        self.assertNotIn("destinationMapping", payload)

    def test_display_name_only_when_set(self):
        self.assertNotIn("displayName", activation_payload(self.source_activation(), False))
        payload = activation_payload(self.source_activation(displayName="Table"), False)
        self.assertEqual(payload["displayName"], "Table")


class EnvFile(unittest.TestCase):
    def test_parses_comments_quotes_and_export(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write(
                "# comment\n"
                "\n"
                "SEGMENT_PUBLIC_API_TOKEN=abc123\n"
                'SEGMENT_SPACE_ID="spa_1"\n'
                "export SEGMENT_REGION=eu\n"
                "SEGMENT_API_HOST='http://localhost:1'\n"
                "MALFORMED\n"
            )
            path = handle.name
        values = parse_env_file(path)
        self.assertEqual(values["SEGMENT_PUBLIC_API_TOKEN"], "abc123")
        self.assertEqual(values["SEGMENT_SPACE_ID"], "spa_1")
        self.assertEqual(values["SEGMENT_REGION"], "eu")
        self.assertEqual(values["SEGMENT_API_HOST"], "http://localhost:1")
        self.assertNotIn("MALFORMED", values)

    def test_missing_file_is_empty(self):
        self.assertEqual(parse_env_file("/nonexistent/.env"), {})


if __name__ == "__main__":
    unittest.main()
