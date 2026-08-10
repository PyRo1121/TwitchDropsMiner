from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import floor
from typing import TYPE_CHECKING, Any

from constants import (
    GQL_BATCH_SIZE,
    GQL_QUERIES,
    INVENTORY_RETRY_BASE,
    INVENTORY_RETRY_MAX,
    MAX_INT,
    PriorityMode,
    State,
)
from exceptions import (
    ExitRequest,
    LoginException,
    RequestException,
    RequestInvalid,
)
from inventory import DropsCampaign
from inventory_snapshot import (
    build_campaigns,
    merge_campaign_data,
    parse_available_campaigns,
    parse_inventory_snapshot,
    prepare_inventory,
)
from translate import _
from utils import cancel_tasks, chunk, open_dump

if TYPE_CHECKING:
    from constants import JsonType
    from twitch import Twitch

logger = logging.getLogger("TwitchDrops")


class InventoryService:
    """Fetch, validate, stage, and atomically install inventory snapshots."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._retry_attempt = 0
        self._retry_task: asyncio.Task[None] | None = None
        self._deadline_alerts: set[str] = set()

    def start_session(self) -> None:
        self._deadline_alerts.clear()

    async def close(self) -> None:
        await self._cancel_retry_task()
        self._retry_attempt = 0

    async def _cancel_retry_task(self) -> None:
        retry_task = self._retry_task
        self._retry_task = None
        if retry_task is None or retry_task is asyncio.current_task():
            return
        await cancel_tasks((retry_task,))

    async def _retry_after(self, generation: int, delay: float) -> None:
        current_task = asyncio.current_task()
        try:
            await self._twitch.transport.wait_for_delay(delay)
        except ExitRequest:
            return
        finally:
            if self._retry_task is current_task:
                self._retry_task = None
        if (
            self._twitch._state_generation == generation
            and self._twitch._state is State.INVENTORY_FETCH
        ):
            self._twitch.change_state(State.INVENTORY_FETCH)

    def _record_campaign_deadlines(self) -> None:
        now = datetime.now(timezone.utc)
        for campaign in self._twitch.inventory:
            remaining = (campaign.ends_at - now).total_seconds()
            if (
                campaign.finished
                or not campaign.active
                or not 0 < remaining <= 3600
                or campaign.id in self._deadline_alerts
            ):
                continue
            self._deadline_alerts.add(campaign.id)
            self._twitch.history_event(
                "campaign.deadline",
                severity="warning",
                data={
                    "campaign_id": campaign.id,
                    "campaign": campaign.name,
                    "game": campaign.game.name,
                    "remaining_minutes": max(1, floor(remaining / 60)),
                },
            )

    async def sync_state(self) -> None:
        state_generation = self._twitch._state_generation
        self._twitch.gui.tray.change_icon("maint")
        # Preserve the last-good snapshot until fetch_inventory commits.
        await self._twitch.websocket.start()
        try:
            await self.fetch_inventory()
        except (ExitRequest, LoginException, RequestInvalid):
            raise
        except RequestException as exc:
            self._retry_attempt += 1
            delay = min(
                INVENTORY_RETRY_BASE
                * (2 ** min(self._retry_attempt - 1, 10)),
                INVENTORY_RETRY_MAX,
            )
            if self._retry_attempt == 1:
                self._twitch.history_event(
                    "inventory.sync_failed",
                    severity="warning",
                    data={"error_type": type(exc).__name__},
                )
            self._twitch.gui.status.update(
                _("gui", "status", "inventory_retry").format(
                    seconds=max(1, round(delay))
                )
            )
            await self._cancel_retry_task()
            self._retry_task = asyncio.create_task(
                self._retry_after(state_generation, delay)
            )
            return
        except Exception as exc:
            self._twitch.history_event(
                "inventory.sync_failed",
                severity="warning",
                data={"error_type": type(exc).__name__},
            )
            raise

        await self._cancel_retry_task()
        retry_attempts = self._retry_attempt
        self._retry_attempt = 0
        if retry_attempts:
            self._twitch.history_event(
                "inventory.sync_recovered",
                data={"attempts": retry_attempts},
            )
        self._twitch.history_event(
            "inventory.synced",
            data={
                "campaigns": len(self._twitch.inventory),
                "drops": len(self._twitch._drops),
            },
        )
        self._record_campaign_deadlines()
        self._twitch.gui.set_games(
            {campaign.game for campaign in self._twitch.inventory}
        )
        self._twitch.save()
        if self._twitch._state_generation == state_generation:
            self._twitch.change_state(State.GAMES_UPDATE)

    async def update_wanted_games(self) -> None:
        for campaign in self._twitch.inventory:
            if campaign.upcoming:
                continue
            for drop in campaign.drops:
                if drop.can_claim and await drop.claim():
                    self._twitch.watch_service.progress.mark_completed_drop(drop.id)

        exclude = self._twitch.settings.exclude
        priority = self._twitch.settings.priority
        priority_mode = self._twitch.settings.priority_mode
        priority_only = priority_mode is PriorityMode.PRIORITY_ONLY
        next_hour = datetime.now(timezone.utc) + timedelta(hours=1)
        campaigns = list(self._twitch.inventory)
        if not priority_only:
            if priority_mode is PriorityMode.ENDING_SOONEST:
                campaigns.sort(key=lambda campaign: campaign.ends_at)
            elif priority_mode is PriorityMode.LOW_AVBL_FIRST:
                campaigns.sort(key=lambda campaign: campaign.availability)
        campaigns.sort(
            key=lambda campaign: (
                priority.index(campaign.game.name)
                if campaign.game.name in priority
                else MAX_INT
            )
        )

        wanted_games = []
        for campaign in campaigns:
            game = campaign.game
            if (
                game not in wanted_games
                and game.name not in exclude
                and (not priority_only or game.name in priority)
                and campaign.can_earn_within(next_hour)
            ):
                wanted_games.append(game)
        self._twitch.wanted_games[:] = wanted_games

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
        return merge_campaign_data(campaign_ids, fetched_data)

    async def _install_inventory(
        self,
        campaigns: list[DropsCampaign],
        status_update: Callable[[str], Any],
    ) -> None:
        drops, campaigns_by_id, maintenance_triggers = prepare_inventory(campaigns)
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

        self._twitch.watch_service.progress.retain_claim_cooldowns(drops)
        self._twitch._inventory_generation += 1
        self._twitch._drops = drops
        self._twitch.inventory = list(campaigns)
        self._twitch._campaigns = campaigns_by_id
        self._twitch._mnt_triggers = maintenance_triggers

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
                inventory_data = merge_campaign_data(inventory_data, chunk_campaigns_data)
        finally:
            await cancel_tasks(fetch_campaigns_tasks)
        return inventory_data

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
        inventory_data, claimed_benefits = parse_inventory_snapshot(inventory)
        # fetch general available campaigns data (campaigns)
        response = await self._twitch.transport.gql_request(GQL_QUERIES["Campaigns"])
        available_campaigns = parse_available_campaigns(response)
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

        campaigns = build_campaigns(self._twitch, inventory_data, claimed_benefits)
        await self._install_inventory(campaigns, status_update)
