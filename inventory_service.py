from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING, Any

from constants import GQL_BATCH_SIZE, GQL_QUERIES
from exceptions import ExitRequest, MinerException, RequestException
from inventory import DropsCampaign, TimedDrop
from translate import _
from utils import (
    cancel_tasks,
    chunk,
    merge_primary_json,
    open_dump,
    timestamp,
)

if TYPE_CHECKING:
    from constants import JsonType
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


class InventoryService:
    """Fetch, validate, stage, and atomically install inventory snapshots."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch

    @staticmethod
    def _merge_data(primary_data: JsonType, secondary_data: JsonType) -> JsonType:
        try:
            return merge_primary_json(primary_data, secondary_data)
        except TypeError as exc:
            raise MinerException(str(exc)) from exc

    async def fetch_campaigns(
        self, campaigns_chunk: list[tuple[str, JsonType]]
    ) -> dict[str, JsonType]:
        campaign_ids: dict[str, JsonType] = dict(campaigns_chunk)
        auth_state = await self._twitch.get_auth()
        response_list: list[JsonType] = await self._twitch.transport.gql_request(
            [
                GQL_QUERIES["CampaignDetails"].with_variables(
                    {"channelLogin": str(auth_state.user_id), "dropID": cid}
                )
                for cid in campaign_ids
            ]
        )
        fetched_data: dict[str, JsonType] = {}
        for response_json in response_list:
            try:
                campaign_data = response_json["data"]["user"]["dropCampaign"]
                campaign_id = campaign_data["id"]
            except (KeyError, TypeError):
                logger.warning("Campaign detail response did not contain a campaign")
                continue
            if (
                isinstance(campaign_data, dict)
                and isinstance(campaign_id, str)
                and campaign_id in campaign_ids
            ):
                fetched_data[campaign_id] = campaign_data
        return self._merge_data(campaign_ids, fetched_data)

    def _prepare_inventory(
        self,
        campaigns: list[DropsCampaign],
    ) -> tuple[
        dict[str, TimedDrop],
        dict[str, DropsCampaign],
        deque[datetime],
    ]:
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

    async def _install_inventory(
        self,
        campaigns: list[DropsCampaign],
        status_update: Callable[[str], Any],
    ) -> None:
        drops, campaigns_by_id, maintenance_triggers = self._prepare_inventory(
            campaigns
        )
        status_update(
            _("gui", "status", "adding_campaigns").format(
                counter=f"(0/{len(campaigns)})"
            )
        )

        # Quiesce tasks that can mutate the old snapshot, then commit the core
        # data before starting any optional presentation or image work.
        await self._twitch.watch_service.stop_watching_and_wait()
        await self._twitch.websocket.cancel_topic_tasks()
        if self._twitch.gui.close_requested:
            raise ExitRequest()

        now_timestamp = monotonic()
        watch_claim_cooldowns = {
            drop_id: blocked_until
            for drop_id, blocked_until in getattr(
                self._twitch,
                "_watch_claim_cooldowns",
                {},
            ).items()
            if blocked_until > now_timestamp
            and drop_id in drops
            and not drops[drop_id].is_claimed
        }

        self._twitch._inventory_generation += 1
        self._twitch._drops = drops
        self._twitch.inventory = list(campaigns)
        self._twitch._campaigns = campaigns_by_id
        self._twitch._mnt_triggers = maintenance_triggers
        self._twitch._watch_claim_cooldowns = watch_claim_cooldowns

        try:
            await self._twitch.gui.inv.replace_campaigns(campaigns)
        except ExitRequest:
            raise
        except Exception as exc:
            logger.exception("Inventory presentation replacement failed")
            self._twitch.history_event(
                "inventory.presentation_failed",
                severity="warning",
                data={"error_type": type(exc).__name__},
            )
            # Do not leave cards bound to retired drop objects. An empty
            # presentation is safer than rolling back the committed core state.
            try:
                await self._twitch.gui.inv.replace_campaigns(())
            except Exception:
                logger.exception("Unable to clear stale inventory presentation")

        status_update(
            _("gui", "status", "adding_campaigns").format(
                counter=f"({len(campaigns)}/{len(campaigns)})"
            )
        )

        # NOTE: maintenance task is restarted at the end of each inventory fetch
        if self._twitch._mnt_task is not None and not self._twitch._mnt_task.done():
            await cancel_tasks([self._twitch._mnt_task])
        self._twitch._mnt_task = asyncio.create_task(
            self._twitch.watch_service._maintenance_task()
        )

    def _build_campaigns(
        self,
        inventory_data: dict[str, JsonType],
        claimed_benefits: dict[str, datetime],
    ) -> list[DropsCampaign]:
        campaigns: list[DropsCampaign] = []
        skipped_ids: list[str] = []
        for campaign_id, campaign_data in inventory_data.items():
            try:
                campaign = DropsCampaign(self._twitch, campaign_data, claimed_benefits)
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
            self._twitch.history_event(
                "inventory.campaigns_skipped",
                severity="warning",
                data={"count": len(skipped_ids), "total": len(inventory_data)},
            )
        if inventory_data and not campaigns:
            raise RequestException("Every Twitch campaign in the snapshot was malformed")
        campaigns.sort(key=lambda c: c.active, reverse=True)
        campaigns.sort(key=lambda c: c.upcoming and c.starts_at or c.ends_at)
        campaigns.sort(key=lambda c: c.eligible, reverse=True)
        return campaigns

    def _dump_inventory(
        self, inventory_data: dict[str, JsonType], inventory: JsonType
    ) -> None:
        # dump the campaigns data to the dump file
        with open_dump("a") as file:
            # we need to pre-process the inventory dump a little
            dump_data: JsonType = deepcopy(inventory_data)
            for campaign_data in dump_data.values():
                if not isinstance(campaign_data, dict):
                    continue
                # replace ACL lists with a simple text description
                allow = campaign_data.get("allow")
                if (
                    isinstance(allow, dict)
                    and allow.get("isEnabled", True)
                    and isinstance(allow.get("channels"), list)
                    and allow["channels"]
                ):
                    # simply count the channels included in the ACL
                    allow["channels"] = f"{len(allow['channels'])} channels"
                # replace drop instance IDs, so they don't include user IDs
                drops = campaign_data.get("timeBasedDrops")
                if not isinstance(drops, list):
                    continue
                for drop_data in drops:
                    if not isinstance(drop_data, dict):
                        continue
                    self_data = drop_data.get("self")
                    if isinstance(self_data, dict) and self_data.get("dropInstanceID"):
                        self_data["dropInstanceID"] = "..."
            json.dump(dump_data, file, indent=4, sort_keys=True)
            file.write("\n\n")  # add 2x new line spacer
            json.dump(inventory["gameEventDrops"], file, indent=4, sort_keys=True, default=str)

    async def _fetch_campaign_details(
        self,
        inventory_data: dict[str, JsonType],
        available_campaigns: dict[str, JsonType],
        status_update: Callable[[str], Any],
    ) -> dict[str, JsonType]:
        # fetch detailed data for each campaign, in chunks
        status_update(_("gui", "status", "fetching_campaigns"))
        fetch_campaigns_tasks: list[asyncio.Task[Any]] = [
            asyncio.create_task(self.fetch_campaigns(campaigns_chunk))
            for campaigns_chunk in chunk(available_campaigns.items(), GQL_BATCH_SIZE)
        ]
        try:
            for coro in asyncio.as_completed(fetch_campaigns_tasks):
                chunk_campaigns_data = await coro
                # Merge inventory and campaign data together.
                inventory_data = self._merge_data(inventory_data, chunk_campaigns_data)
        finally:
            await cancel_tasks(fetch_campaigns_tasks)
        return inventory_data

    @staticmethod
    def _parse_available_campaigns(response: JsonType) -> dict[str, JsonType]:
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

    @staticmethod
    def _parse_inventory_snapshot(
        inventory: JsonType,
    ) -> tuple[dict[str, JsonType], dict[str, datetime]]:
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

        # This contains claimed benefit edge IDs, not drop IDs.
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

    async def fetch_inventory(self) -> None:
        status_update = self._twitch.gui.status.update
        status_update(_("gui", "status", "fetching_inventory"))
        # fetch in-progress campaigns (inventory)
        response = await self._twitch.transport.gql_request(GQL_QUERIES["Inventory"])
        try:
            inventory = response["data"]["currentUser"]["inventory"]
        except (KeyError, TypeError) as exc:
            raise RequestException("Twitch inventory response was malformed") from exc
        if not isinstance(inventory, dict):
            raise RequestException("Twitch inventory response was malformed")
        inventory_data, claimed_benefits = self._parse_inventory_snapshot(inventory)
        # fetch general available campaigns data (campaigns)
        response = await self._twitch.transport.gql_request(GQL_QUERIES["Campaigns"])
        available_campaigns = self._parse_available_campaigns(response)
        inventory_data = await self._fetch_campaign_details(
            inventory_data, available_campaigns, status_update
        )
        # Campaigns for removed/unavailable games cannot be represented and are
        # intentionally omitted. Every remaining campaign must validate fully.
        for campaign_id in list(inventory_data.keys()):
            campaign_data = inventory_data[campaign_id]
            if not isinstance(campaign_data, dict):
                raise RequestException("Twitch campaign details were malformed")
            if campaign_data.get("game") is None:
                del inventory_data[campaign_id]

        if self._twitch.settings.dump:
            self._dump_inventory(inventory_data, inventory)

        campaigns = self._build_campaigns(inventory_data, claimed_benefits)
        await self._install_inventory(campaigns, status_update)
