from __future__ import annotations

import json
import unittest

import remctl_host_protocol as protocol


def request(operation, **fields):
    return {
        "protocolVersion": protocol.PROTOCOL_VERSION,
        "schemaManifestVersion": protocol.SCHEMA_MANIFEST_VERSION,
        "schemaManifestDigest": protocol.SCHEMA_MANIFEST_DIGEST,
        "requestId": "test-request-1",
        "operation": operation,
        **fields,
    }


class HostProtocolTests(unittest.TestCase):
    def test_manifest_digest_is_stable_for_process(self):
        self.assertEqual(
            protocol.SCHEMA_MANIFEST_DIGEST,
            protocol.schema_manifest_digest(),
        )
        self.assertEqual(
            protocol.schema_manifest()["implementedOperations"],
            list(protocol.IMPLEMENTED_OPERATIONS),
        )

    def test_accepts_closed_health_request(self):
        payload = request("health")
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_accepts_typed_resolver_request(self):
        payload = request("resolve.list", name="Produce")
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_accepts_list_resolution_options(self):
        payload = request("resolve.list", id=42, allowGroups=True)
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_accepts_smart_list_sections_snapshot(self):
        payload = request("snapshot.smartListSectionsByPk", listId=42)
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_accepts_template_with_items_snapshot(self):
        payload = request("snapshot.templateWithItems", identifier=42)
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_accepts_location_alarm_snapshot(self):
        payload = request(
            "snapshot.locationAlarm",
            identifier="REM-1",
            title="Apple Park",
            latitudeE7=373349000,
            longitudeE7=-1220090000,
        )
        self.assertEqual(protocol.validate_request(payload), payload)

    def test_rejects_mutation_operation(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(request("add", title="Buy milk"))
        self.assertEqual(caught.exception.code, "unsupported_operation")

    def test_rejects_raw_cli_arguments(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(request("health", argv=["show", "Work"]))
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_rejects_unknown_field(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(
                request("resolve.list", id=1, path="/tmp/store")
            )
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_rejects_schema_digest_mismatch(self):
        payload = request("health")
        payload["schemaManifestDigest"] = "0" * 64
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(payload)
        self.assertEqual(caught.exception.code, "schema_mismatch")

    def test_rejects_ambiguous_resolver_target(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(
                request("resolve.list", name="Work", id=12)
            )
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_rejects_boolean_as_integer(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.validate_request(
                request("resolve.list", id=True)
            )
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_decode_rejects_non_utf8(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.decode_request(b"\xff")
        self.assertEqual(caught.exception.code, "invalid_encoding")

    def test_decode_rejects_oversized_request(self):
        with self.assertRaises(protocol.ProtocolError) as caught:
            protocol.decode_request(b"x" * (protocol.MAX_REQUEST_BYTES + 1))
        self.assertEqual(caught.exception.code, "request_too_large")

    def test_decode_accepts_json_request(self):
        payload = request("resolve.list", id=123)
        decoded = protocol.decode_request(json.dumps(payload).encode("utf-8"))
        self.assertEqual(decoded, payload)


if __name__ == "__main__":
    unittest.main()
