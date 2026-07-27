#!/usr/bin/env python3
"""Tests for ovpf_cloud.py (stdlib unittest). Mocks the HTTP layer
(_request) so this never touches the real passport.skoor.ee provider --
uses a temp data dir so it never touches real session/passport files.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ovpf_cloud as cloud
import ovpf_producer as prod


class FakeHttp:
    """Programs canned (status, body) responses per call, in order, and
    records every call made so assertions can check what was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, path, body=None, token=None, timeout=10.0):
        self.calls.append({"method": method, "path": path, "body": body,
                           "token": token})
        return self.responses.pop(0)


class CloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        prod.paths.data_dir = lambda: self.tmp
        cloud.paths.data_dir = lambda: self.tmp
        self.vin = "TESTVIN0000000001"
        self._orig_request = cloud._request

    def tearDown(self):
        cloud._request = self._orig_request

    def test_session_roundtrip(self):
        self.assertIsNone(cloud.get_user_session())
        cloud.set_user_session("a@b.com", "tok1")
        self.assertEqual(cloud.get_user_session(),
                         {"email": "a@b.com", "token": "tok1"})
        cloud.clear_user_session()
        self.assertIsNone(cloud.get_user_session())

        cloud.set_workshop_session("shop.example", "tok2", {"role": "owner"})
        self.assertEqual(cloud.get_workshop_session(),
                         {"domain": "shop.example", "token": "tok2",
                          "role": "owner"})
        cloud.clear_workshop_session()
        self.assertIsNone(cloud.get_workshop_session())

    def test_best_auth_session_prefers_matching_workshop(self):
        cloud.set_user_session("a@b.com", "user-tok")
        cloud.set_workshop_session("shop.example", "ws-tok")
        self.assertEqual(cloud.best_auth_session("shop.example")["token"],
                         "ws-tok")
        self.assertEqual(cloud.best_auth_session("other.example")["token"],
                         "user-tok")
        self.assertEqual(cloud.best_auth_session(None)["token"], "user-tok")

    def test_best_auth_session_falls_back_to_workshop_only(self):
        cloud.set_workshop_session("shop.example", "ws-tok")
        self.assertEqual(cloud.best_auth_session(None)["token"], "ws-tok")

    def test_best_auth_session_none_when_signed_out(self):
        self.assertIsNone(cloud.best_auth_session("shop.example"))

    def test_verify_otp_stores_session_on_success(self):
        cloud._request = FakeHttp([(200, {"token": "abc123"})])
        cloud.verify_otp("me@example.com", "111111")
        self.assertEqual(cloud.get_user_session(),
                         {"email": "me@example.com", "token": "abc123"})

    def test_verify_otp_raises_on_error(self):
        cloud._request = FakeHttp([(401, {"error": "bad code"})])
        with self.assertRaises(cloud.CloudError):
            cloud.verify_otp("me@example.com", "000000")
        self.assertIsNone(cloud.get_user_session())

    def test_bare_id_strips_urn_prefix(self):
        self.assertEqual(cloud._bare_id("urn:ovpf:abc-123"), "abc-123")
        self.assertEqual(cloud._bare_id("already-bare"), "already-bare")

    def test_push_passport_no_local_data(self):
        r = cloud.push_passport("NEVER_SEEN_VIN")
        self.assertEqual(r["pushed"], 0)
        self.assertIn("note", r)

    def test_push_passport_requires_sign_in(self):
        prod.ensure_passport(self.vin)
        with self.assertRaises(cloud.CloudError):
            cloud.push_passport(self.vin)

    def test_push_passport_sends_and_tracks_synced(self):
        prod.ensure_passport(self.vin)
        prod.record_odometer(self.vin, 123456)
        cloud.set_user_session("me@example.com", "tok")

        fake = FakeHttp([
            (201, {"ok": True}),                    # POST /v1/passports
            (200, {"id": "ev1"}),                    # PassportOpened event
            (200, {"id": "ev2"}),                    # OdometerReading event
        ])
        cloud._request = fake
        result = cloud.push_passport(self.vin)
        self.assertEqual(result["pushed"], 2)
        self.assertEqual(result["errors"], [])

        # registration call used the bare uuid, not the urn:ovpf: form
        register_call = fake.calls[0]
        self.assertEqual(register_call["path"], "/v1/passports")
        self.assertFalse(register_call["body"]["id"].startswith("urn:"))

        # second push with nothing new queued is a no-network no-op
        fake2 = FakeHttp([])
        cloud._request = fake2
        result2 = cloud.push_passport(self.vin)
        self.assertEqual(result2["pushed"], 0)
        self.assertEqual(result2["skipped"], 2)
        self.assertEqual(fake2.calls, [])

    def test_push_passport_stops_on_auth_error(self):
        prod.ensure_passport(self.vin)
        prod.record_odometer(self.vin, 111)
        prod.record_odometer(self.vin, 222)
        cloud.set_user_session("me@example.com", "tok")

        fake = FakeHttp([
            (201, {"ok": True}),                  # register
            (401, {"error": "expired"}),          # first event fails
        ])
        cloud._request = fake
        result = cloud.push_passport(self.vin)
        self.assertEqual(result["pushed"], 0)
        self.assertEqual(len(result["errors"]), 1)

    def _append_workshop_event(self, urn, path):
        """A producer.type "Workshop" event with no cloud session behind
        it -- garagediag itself can no longer create these (the local
        self-asserted Workshop identity was removed as confusing UX), but
        push_passport still has to refuse one if it shows up (a hand-edited
        or pre-removal .ovpf.ndjson file) rather than trust it blindly."""
        import ovpf_core
        producer = {"type": "Workshop", "name": "Tooming Workshop",
                    "domain": "skoor.ee", "version": prod.VERSION}
        ovpf_core.append(path, ovpf_core.envelope(
            urn, "OdometerReading", {"value": 123456, "unit": "KMT"}, producer))

    def test_push_passport_blocks_unverified_self_asserted_workshop(self):
        """Signing into the cloud with a personal session and pushing must
        not smuggle an unverified Workshop-attributed event onto the
        passport as if it were a real workshop."""
        path, urn = prod.ensure_passport(self.vin)
        self._append_workshop_event(urn, path)
        cloud.set_user_session("me@example.com", "tok")

        fake = FakeHttp([])
        cloud._request = fake
        with self.assertRaises(cloud.CloudError):
            cloud.push_passport(self.vin)
        self.assertEqual(fake.calls, [])  # refused before any network call

    def test_push_passport_allows_verified_workshop_domain_match(self):
        path, urn = prod.ensure_passport(self.vin)
        self._append_workshop_event(urn, path)
        cloud.set_workshop_session("skoor.ee", "ws-tok")

        fake = FakeHttp([
            (201, {"ok": True}),
            (200, {"id": "ev1"}),
            (200, {"id": "ev2"}),
        ])
        cloud._request = fake
        result = cloud.push_passport(self.vin)
        self.assertEqual(result["pushed"], 2)
        self.assertEqual(result["errors"], [])

    def test_push_passport_treats_409_duplicate_as_synced(self):
        """A 409 means the provider already has this event -- e.g. a
        previous push succeeded server-side but the local sync-tracking
        file didn't get updated (the bug this guards against: those
        events used to be retried forever, always failing, and the UI's
        "pending" count never went down)."""
        prod.ensure_passport(self.vin)
        prod.record_odometer(self.vin, 111)
        cloud.set_user_session("me@example.com", "tok")

        fake = FakeHttp([
            (201, {"ok": True}),                              # register
            (409, {"error": "event already exists"}),         # PassportOpened
            (409, {"error": "event already exists"}),         # OdometerReading
        ])
        cloud._request = fake
        result = cloud.push_passport(self.vin)
        self.assertEqual(result["pushed"], 2)
        self.assertEqual(result["errors"], [])

        # and now they're marked synced -- a follow-up push doesn't retry them
        fake2 = FakeHttp([])
        cloud._request = fake2
        result2 = cloud.push_passport(self.vin)
        self.assertEqual(result2["pushed"], 0)
        self.assertEqual(result2["skipped"], 2)
        self.assertEqual(fake2.calls, [])

    class _FakeResponse:
        """Minimal stand-in for what urllib.request.urlopen()'s context
        manager returns -- pull_and_merge_passport/_fetch_export only ever
        call .read()."""
        def __init__(self, text):
            self._data = text.encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _patch_export_response(self, ndjson_text):
        """_fetch_export() (unlike the JSON POST calls above) goes straight
        through urllib.request.urlopen, not the cloud._request seam -- no
        existing test exercises that path, so this patches it directly for
        the duration of one call. Returns the teardown callable."""
        import ovpf_cloud
        orig = ovpf_cloud.urllib.request.urlopen
        ovpf_cloud.urllib.request.urlopen = (
            lambda req, timeout=10.0, context=None: self._FakeResponse(ndjson_text))
        def restore():
            ovpf_cloud.urllib.request.urlopen = orig
        self.addCleanup(restore)

    def test_pull_and_merge_passport_adds_remote_only_event(self):
        """The scenario this exists for: a nickname (or any other edit)
        made through the web viewer writes straight to the cloud provider
        and never reaches this device through push_passport (that's local
        -> cloud only) -- pull_and_merge_passport is the other direction."""
        import ovpf_core
        path, urn = prod.ensure_passport(self.vin)
        local_events = ovpf_core.load(path)

        remote_only = ovpf_core.envelope(
            urn, "VehicleIdentified", {"vehicle": {"nickname": "Nicky"}},
            {"type": "Owner", "name": "someone@example.com"})
        remote_events = ovpf_core.seal(local_events + [dict(remote_only)])
        import json
        remote_ndjson = "\n".join(json.dumps(e) for e in remote_events) + "\n"
        self._patch_export_response(remote_ndjson)

        result = cloud.pull_and_merge_passport(self.vin)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["conflicts"], [])

        merged = ovpf_core.load(path)
        self.assertEqual(len(merged), len(local_events) + 1)
        self.assertEqual(ovpf_core.verify_chain(merged), [])
        state = ovpf_core.reduce(merged)
        self.assertEqual(state["vehicle"].get("nickname"), "Nicky")

        # already-synced ids are marked so a follow-up push doesn't
        # try to re-upload the event that just came *from* the cloud
        synced = cloud._load_synced_ids(path)
        self.assertIn(remote_only["id"], synced)

    def test_pull_and_merge_passport_flags_real_conflict_without_overwriting_local(self):
        """Same event id, genuinely different content (not just a
        provider-added verified stamp) -- reported as a conflict, and the
        local copy is kept rather than silently replaced either way."""
        import ovpf_core
        path, urn = prod.ensure_passport(self.vin)
        local_events = ovpf_core.load(path)

        tampered = dict(local_events[0])
        tampered["data"] = {"vehicle": {"vin": "SOMETHING-ELSE"}}
        remote_ndjson = "\n".join(
            __import__("json").dumps(e)
            for e in [tampered] + local_events[1:]) + "\n"
        self._patch_export_response(remote_ndjson)

        result = cloud.pull_and_merge_passport(self.vin)
        self.assertEqual(result["added"], 0)
        self.assertIn(local_events[0]["id"], result["conflicts"])

        merged = ovpf_core.load(path)
        self.assertEqual(merged[0]["data"], local_events[0]["data"])

    def test_sync_status_reports_pending_count(self):
        prod.ensure_passport(self.vin)
        prod.record_odometer(self.vin, 1000)
        status = cloud.sync_status(self.vin)
        self.assertEqual(status["total"], 2)  # PassportOpened + Odometer
        self.assertEqual(status["pending"], 2)
        self.assertFalse(status["signed_in"])
        cloud.set_user_session("me@example.com", "tok")
        self.assertTrue(cloud.sync_status(self.vin)["signed_in"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
