"""Deterministic tests for timezone validation and calendar-day boundaries.

These exercise :mod:`app.timezone` directly -- no network, no model -- and cover
multiple IANA zones plus a daylight-saving boundary where a day is 23 or 25
hours rather than 24.

The HTTP tests below exercise the real ASGI stack through TestClient: the
``POST /auth/settings/timezone`` route, its validation, and persistence to the
owner's user row.

Run with:  python -m tests.test_timezone
"""
from __future__ import annotations

import unittest
import uuid

import tests.support as support

import app.main as main_module

from app.timezone import (
    UTC_FALLBACK,
    local_date_key,
    local_day_boundaries,
    validate_timezone,
)


class TestValidateTimezone(unittest.TestCase):
    def test_known_iana_zones_pass(self):
        for zone in ("UTC", "America/New_York", "Europe/London", "Asia/Tokyo", "Australia/Sydney"):
            self.assertEqual(validate_timezone(zone), zone)

    def test_unknown_zone_degrades_to_utc(self):
        self.assertEqual(validate_timezone("Not/AZone"), UTC_FALLBACK)
        self.assertEqual(validate_timezone("America/Fake"), UTC_FALLBACK)

    def test_empty_and_none_degrade_to_utc(self):
        self.assertEqual(validate_timezone(""), UTC_FALLBACK)
        self.assertEqual(validate_timezone("   "), UTC_FALLBACK)
        self.assertEqual(validate_timezone(None), UTC_FALLBACK)


class TestLocalDateKey(unittest.TestCase):
    def test_utc_matches_the_utc_date(self):
        # 2026-10-24 00:30 UTC -> 2026-10-24
        self.assertEqual(local_date_key("UTC", 1792801800), "2026-10-24")

    def test_ahead_of_utc_maps_to_the_next_local_date(self):
        # 2026-10-24 00:30 UTC is already 2026-10-24 09:30 in Asia/Tokyo (+9).
        self.assertEqual(local_date_key("Asia/Tokyo", 1792801800), "2026-10-24")

    def test_behind_utc_maps_to_the_previous_local_date(self):
        # 2026-10-24 00:30 UTC is 2026-10-23 20:30 in America/New_York (-4).
        self.assertEqual(local_date_key("America/New_York", 1792801800), "2026-10-23")

    def test_unrecognised_zone_uses_utc(self):
        self.assertEqual(local_date_key("Not/AZone", 1792801800), "2026-10-24")


class TestLocalDayBoundaries(unittest.TestCase):
    def test_utc_day_is_exactly_24_hours(self):
        start, end = local_day_boundaries("UTC", 1792801800)  # 2026-10-24 00:30 UTC
        self.assertEqual(start, 1792800000.0)  # 2026-10-24 00:00:00 UTC
        self.assertEqual(end, 1792886400.0)  # 2026-10-25 00:00:00 UTC
        self.assertEqual(end - start, 24 * 3600)

    def test_new_york_ordinary_day_is_24_hours(self):
        # 2026-10-24 is well clear of any DST transition.
        start, end = local_day_boundaries("America/New_York", 1792801800)
        self.assertEqual(end - start, 24 * 3600)

    def test_spring_forward_day_is_23_hours(self):
        # America/New_York springs forward 2026-03-08 at 02:00 local -> that day
        # runs 00:00 to 01:00 (EST) then 03:00 to 24:00 (EDT): 23 hours.
        when = 1772971200  # 2026-03-08 12:00 UTC (= 08:00 EDT)
        start, end = local_day_boundaries("America/New_York", when)
        self.assertEqual(end - start, 23 * 3600)

    def test_fall_back_day_is_25_hours(self):
        # America/New_York falls back 2026-11-01 at 02:00 local -> that day runs
        # 25 hours (01:00-02:00 occurs twice).
        when = 1793534400  # 2026-11-01 12:00 UTC (= 07:00 EST after the fallback)
        start, end = local_day_boundaries("America/New_York", when)
        self.assertEqual(end - start, 25 * 3600)

    def test_unrecognised_zone_uses_utc_boundaries(self):
        start, end = local_day_boundaries("Not/AZone", 1792801800)
        self.assertEqual(end - start, 24 * 3600)


class TestTimezoneRoute(unittest.TestCase):
    """The ``POST /auth/settings/timezone`` route over the real ASGI stack."""

    def setUp(self):
        main_module.settings.rag_internal_token = support.INTERNAL
        self.alice = support.sign_in(f"sub-{uuid.uuid4().hex}", "alice@example.com")
        self.client, _ = support.make_client(self.alice["id"])

    def test_sets_a_valid_timezone(self):
        resp = self.client.post(
            "/auth/settings/timezone",
            json={"timezone": "America/New_York"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["timezone"], "America/New_York")
        row = main_module.db.get_user(self.alice["id"])
        self.assertEqual(row["timezone"], "America/New_York")

    def test_invalid_timezone_degrades_to_utc(self):
        resp = self.client.post(
            "/auth/settings/timezone",
            json={"timezone": "Not/AZone"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["timezone"], UTC_FALLBACK)
        row = main_module.db.get_user(self.alice["id"])
        self.assertEqual(row["timezone"], UTC_FALLBACK)

    def test_empty_timezone_degrades_to_utc(self):
        resp = self.client.post("/auth/settings/timezone", json={"timezone": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["timezone"], UTC_FALLBACK)

    def test_another_user_cannot_set_this_users_timezone(self):
        bob = support.sign_in(f"bob-{uuid.uuid4().hex}", "bob@example.com")
        bob_client, _ = support.make_client(bob["id"])
        resp = bob_client.post(
            "/auth/settings/timezone",
            json={"timezone": "Asia/Tokyo"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["timezone"], "Asia/Tokyo")
        # Alice's row is untouched.
        self.assertEqual(main_module.db.get_user(self.alice["id"])["timezone"], UTC_FALLBACK)


if __name__ == "__main__":
    unittest.main()
