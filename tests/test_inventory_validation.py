from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

from exceptions import RequestException
from inventory import DropsCampaign
from inventory_snapshot import build_campaigns


def _stamp(offset_minutes: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    ).isoformat()


def _benefit(benefit_id: str) -> dict[str, object]:
    return {
        "benefit": {
            "id": benefit_id,
            "name": benefit_id,
            "distributionType": "BADGE",
            "imageAssetURL": "https://cdn.example/reward.png",
        }
    }


def _drop(drop_id: str = "drop") -> dict[str, object]:
    return {
        "id": drop_id,
        "name": drop_id,
        "startAt": _stamp(-10),
        "endAt": _stamp(60),
        "benefitEdges": [_benefit(f"benefit-{drop_id}")],
        "preconditionDrops": [],
        "requiredMinutesWatched": 10,
        "self": {
            "currentMinutesWatched": 0,
            "isClaimed": False,
        },
    }


def _campaign_data() -> dict[str, Any]:
    return {
        "id": "campaign",
        "name": "Campaign",
        "status": "ACTIVE",
        "startAt": _stamp(-10),
        "endAt": _stamp(60),
        "game": {
            "id": "1",
            "displayName": "Game",
            "boxArtURL": "https://cdn.example/game-285x380.jpg",
        },
        "self": {"isAccountConnected": True},
        "allow": {"isEnabled": False, "channels": []},
        "timeBasedDrops": [_drop()],
    }


class _InventoryGui:
    def update_drop(self, _drop: object) -> None:
        return None


class _Twitch:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(enable_badges_emotes=True)
        self.gui = SimpleNamespace(inv=_InventoryGui())
        self.events: list[str] = []

    def history_event(self, kind: str, **_values: object) -> None:
        self.events.append(kind)


class InventoryValidationTests(unittest.TestCase):
    def _campaign(
        self,
        data: dict[str, Any] | None = None,
        claimed_benefits: dict[str, datetime] | None = None,
    ) -> DropsCampaign:
        return DropsCampaign(
            cast(Any, _Twitch()),
            data or _campaign_data(),
            claimed_benefits or {},
        )

    def test_one_malformed_campaign_does_not_discard_valid_campaigns(self) -> None:
        twitch = _Twitch()
        malformed = _campaign_data()
        malformed["status"] = "MYSTERY"

        campaigns = build_campaigns(
            cast(Any, twitch),
            {"campaign": _campaign_data(), "malformed": malformed},
            {},
        )

        self.assertEqual([campaign.id for campaign in campaigns], ["campaign"])
        self.assertEqual(twitch.events, ["inventory.campaigns_skipped"])

    def test_snapshot_with_no_valid_campaign_is_rejected(self) -> None:
        twitch = _Twitch()
        malformed = _campaign_data()
        malformed["status"] = "MYSTERY"

        with self.assertRaisesRegex(RequestException, "Every Twitch campaign"):
            build_campaigns(cast(Any, twitch), {"malformed": malformed}, {})

    def test_boolean_fields_reject_strings(self) -> None:
        mutations = (
            lambda data: data["self"].__setitem__(
                "isAccountConnected", "false"
            ),
            lambda data: data["allow"].__setitem__("isEnabled", "false"),
            lambda data: data["timeBasedDrops"][0]["self"].__setitem__(
                "isClaimed", "false"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = _campaign_data()
                mutate(data)
                with self.assertRaisesRegex(ValueError, "boolean"):
                    self._campaign(data)

    def test_enabled_allowlist_cannot_fail_open(self) -> None:
        for channels in ([], ["invalid"]):
            with self.subTest(channels=channels):
                data = _campaign_data()
                data["allow"] = {
                    "isEnabled": True,
                    "channels": channels,
                }
                with self.assertRaisesRegex(ValueError, "allowlist"):
                    self._campaign(data)

    def test_missing_and_cyclic_prerequisites_are_rejected(self) -> None:
        missing = _campaign_data()
        missing["timeBasedDrops"][0]["preconditionDrops"] = [
            {"id": "missing"}
        ]
        with self.assertRaisesRegex(ValueError, "missing prerequisite"):
            self._campaign(missing)

        cyclic = _campaign_data()
        cyclic["timeBasedDrops"][0]["preconditionDrops"] = [
            {"id": "drop"}
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            self._campaign(cyclic)

    def test_mixed_campaign_evaluates_eligibility_per_drop(self) -> None:
        data = _campaign_data()
        data["self"]["isAccountConnected"] = False
        direct = _drop("direct")
        benefit_edges = cast(
            list[dict[str, Any]],
            direct["benefitEdges"],
        )
        direct_benefit = cast(dict[str, Any], benefit_edges[0]["benefit"])
        direct_benefit["distributionType"] = "DIRECT_ENTITLEMENT"
        data["timeBasedDrops"].append(direct)

        campaign = self._campaign(data)
        drops = {drop.id: drop for drop in campaign.drops}

        self.assertTrue(drops["drop"].eligible)
        self.assertFalse(drops["direct"].eligible)
        self.assertFalse(drops["direct"].can_earn())

    def test_partial_benefit_evidence_does_not_mark_drop_claimed(self) -> None:
        data = _campaign_data()
        drop = data["timeBasedDrops"][0]
        drop["benefitEdges"] = [_benefit("one"), _benefit("two")]
        del drop["self"]
        awarded = datetime.now(timezone.utc)

        campaign = self._campaign(data, {"one": awarded})

        self.assertFalse(next(iter(campaign.drops)).is_claimed)

    def test_unknown_status_and_lossy_minutes_are_rejected(self) -> None:
        unknown = _campaign_data()
        unknown["status"] = "MYSTERY"
        with self.assertRaisesRegex(ValueError, "status is unknown"):
            self._campaign(unknown)

        for value in (True, 1.5, "10"):
            with self.subTest(value=value):
                data = _campaign_data()
                data["timeBasedDrops"][0]["requiredMinutesWatched"] = value
                with self.assertRaisesRegex(ValueError, "non-negative integer"):
                    self._campaign(data)

    def test_progress_overshoot_is_normalized_to_complete(self) -> None:
        data = _campaign_data()
        data["timeBasedDrops"][0]["self"]["currentMinutesWatched"] = 14

        campaign = self._campaign(data)
        drop = next(iter(campaign.drops))

        self.assertEqual((drop.current_minutes, drop.required_minutes), (10, 10))

        drop.update_minutes(16, 12)
        self.assertEqual((drop.current_minutes, drop.required_minutes), (12, 12))

    def test_authoritative_requirement_updates_without_stale_rewind(self) -> None:
        campaign = self._campaign()
        drop = next(iter(campaign.drops))

        drop.update_minutes(4, 20)
        self.assertEqual((drop.current_minutes, drop.required_minutes), (4, 20))

        drop.update_minutes(3, 5)
        self.assertEqual((drop.current_minutes, drop.required_minutes), (4, 20))


if __name__ == "__main__":
    unittest.main()
