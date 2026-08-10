from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from exceptions import MinerException, RequestException
from inventory import DropsCampaign, TimedDrop
from utils import merge_primary_json, timestamp

if TYPE_CHECKING:
    from constants import JsonType
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


def merge_campaign_data(primary_data: JsonType, secondary_data: JsonType) -> JsonType:
    """Merge complementary campaign payloads without allowing type conflicts."""
    try:
        return merge_primary_json(primary_data, secondary_data)
    except TypeError as exc:
        raise MinerException(str(exc)) from exc


def parse_available_campaigns(response: JsonType) -> dict[str, JsonType]:
    """Validate the campaign-directory response and index applicable campaigns."""
    try:
        raw_available = response["data"]["currentUser"]["dropCampaigns"]
    except (KeyError, TypeError) as exc:
        raise RequestException("Twitch campaign response was malformed") from exc
    if raw_available is None:
        available_list: list[Any] = []
    elif isinstance(raw_available, list):
        available_list = raw_available
    else:
        raise RequestException("Twitch campaign list was malformed")

    applicable_statuses = {"ACTIVE", "UPCOMING"}
    available_campaigns: dict[str, JsonType] = {}
    for campaign_data in available_list:
        if not isinstance(campaign_data, dict):
            raise RequestException("Twitch campaign list contained invalid data")
        campaign_id = campaign_data.get("id")
        status = campaign_data.get("status")
        if not isinstance(campaign_id, str) or not isinstance(status, str):
            raise RequestException("Twitch campaign list contained invalid data")
        if campaign_id in available_campaigns:
            raise RequestException(
                f"Twitch campaign list contains duplicate ID: {campaign_id}"
            )
        if status in applicable_statuses:
            available_campaigns[campaign_id] = campaign_data
    return available_campaigns


def parse_inventory_snapshot(
    inventory: JsonType,
) -> tuple[dict[str, JsonType], dict[str, datetime]]:
    """Validate an account inventory response before any state is mutated."""
    if not isinstance(inventory, dict):
        raise RequestException("Twitch inventory response was malformed")

    raw_ongoing = inventory.get("dropCampaignsInProgress")
    if raw_ongoing is None:
        ongoing_campaigns: list[Any] = []
    elif isinstance(raw_ongoing, list):
        ongoing_campaigns = raw_ongoing
    else:
        raise RequestException("Twitch in-progress campaign list was malformed")

    raw_game_events = inventory.get("gameEventDrops")
    if raw_game_events is None:
        game_events: list[Any] = []
    elif isinstance(raw_game_events, list):
        game_events = raw_game_events
    else:
        raise RequestException("Twitch claimed-benefit list was malformed")

    # These are claimed benefit edge IDs, not drop IDs.
    claimed_benefits: dict[str, datetime] = {}
    for benefit_data in game_events:
        if not isinstance(benefit_data, dict):
            raise RequestException("Twitch claimed-benefit list contained invalid data")
        benefit_id = benefit_data.get("id")
        awarded_at = benefit_data.get("lastAwardedAt")
        if not isinstance(benefit_id, str) or not isinstance(awarded_at, str):
            raise RequestException("Twitch claimed-benefit list contained invalid data")
        try:
            claimed_benefits[benefit_id] = timestamp(awarded_at)
        except ValueError as exc:
            raise RequestException(
                "Twitch claimed-benefit timestamp was malformed"
            ) from exc

    inventory_data: dict[str, JsonType] = {}
    for campaign_data in ongoing_campaigns:
        if not isinstance(campaign_data, dict):
            raise RequestException(
                "Twitch in-progress campaign list contained invalid data"
            )
        campaign_id = campaign_data.get("id")
        if not isinstance(campaign_id, str) or not campaign_id:
            raise RequestException(
                "Twitch in-progress campaign list contained invalid data"
            )
        if campaign_id in inventory_data:
            raise RequestException(
                f"Twitch inventory contains duplicate campaign ID: {campaign_id}"
            )
        inventory_data[campaign_id] = campaign_data
    return inventory_data, claimed_benefits


def build_campaigns(
    twitch: Twitch,
    inventory_data: dict[str, JsonType],
    claimed_benefits: dict[str, datetime],
) -> list[DropsCampaign]:
    """Construct validated domain campaigns while isolating malformed entries."""
    campaigns: list[DropsCampaign] = []
    skipped_ids: list[str] = []
    for campaign_id, campaign_data in inventory_data.items():
        try:
            campaign = DropsCampaign(twitch, campaign_data, claimed_benefits)
            if campaign.id != campaign_id:
                raise ValueError("campaign ID mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            skipped_ids.append(campaign_id)
            logger.warning(
                "Skipping malformed Twitch campaign %s (%s)",
                campaign_id,
                type(exc).__name__,
            )
            continue
        campaigns.append(campaign)
    if skipped_ids:
        twitch.history_event(
            "inventory.campaigns_skipped",
            severity="warning",
            data={"count": len(skipped_ids), "total": len(inventory_data)},
        )
    if inventory_data and not campaigns:
        raise RequestException("Every Twitch campaign in the snapshot was malformed")
    campaigns.sort(key=lambda campaign: campaign.active, reverse=True)
    campaigns.sort(
        key=lambda campaign: campaign.upcoming
        and campaign.starts_at
        or campaign.ends_at
    )
    campaigns.sort(key=lambda campaign: campaign.eligible, reverse=True)
    return campaigns


def prepare_inventory(
    campaigns: list[DropsCampaign],
) -> tuple[
    dict[str, TimedDrop],
    dict[str, DropsCampaign],
    deque[datetime],
]:
    """Build all indexes and maintenance deadlines before an atomic install."""
    drops: dict[str, TimedDrop] = {}
    campaigns_by_id: dict[str, DropsCampaign] = {}
    switch_triggers: set[datetime] = set()
    next_hour = datetime.now(timezone.utc) + timedelta(hours=1)

    for campaign in campaigns:
        if campaign.id in campaigns_by_id:
            raise RequestException(
                f"Twitch inventory contains duplicate campaign ID: {campaign.id}"
            )
        campaigns_by_id[campaign.id] = campaign
        for drop in campaign.drops:
            if drop.id in drops:
                raise RequestException(
                    f"Twitch inventory contains duplicate drop ID: {drop.id}"
                )
            drops[drop.id] = drop
        if campaign.can_earn_within(next_hour):
            switch_triggers.update(campaign.time_triggers)

    now = datetime.now(timezone.utc)
    maintenance_triggers = deque(
        trigger for trigger in sorted(switch_triggers) if trigger > now
    )
    return drops, campaigns_by_id, maintenance_triggers
