from __future__ import annotations

import re
import math
import logging
from enum import Enum
from itertools import chain
from typing import TYPE_CHECKING
from functools import cached_property
from datetime import datetime, timedelta, timezone

from translate import _
from channel import Channel
from game import Game
from utils import timestamp
from exceptions import GQLException, RequestException
from constants import GQL_QUERIES, URLType

if TYPE_CHECKING:
    from collections import abc

    from twitch import Twitch
    from constants import JsonType


logger = logging.getLogger("TwitchDrops")
DIMS_PATTERN = re.compile(r'-\d+x\d+(?=\.(?:jpg|png|gif)$)', re.I)


def remove_dimensions(url: URLType) -> URLType:
    return URLType(DIMS_PATTERN.sub('', url))


class BenefitType(Enum):
    UNKNOWN = "UNKNOWN"
    BADGE = "BADGE"
    EMOTE = "EMOTE"
    DIRECT_ENTITLEMENT = "DIRECT_ENTITLEMENT"

    def is_badge_or_emote(self) -> bool:
        return self in (BenefitType.BADGE, BenefitType.EMOTE)


class Benefit:
    __slots__ = ("id", "name", "type", "image_url")

    def __init__(self, data: JsonType):
        benefit_data = data.get("benefit")
        if not isinstance(benefit_data, dict):
            raise ValueError("Drop benefit data is missing")
        benefit_id = benefit_data.get("id")
        name = benefit_data.get("name")
        distribution_type = benefit_data.get("distributionType")
        image_url = benefit_data.get("imageAssetURL")
        if not isinstance(benefit_id, str) or not isinstance(name, str) or not isinstance(image_url, str):
            raise ValueError("Drop benefit data is incomplete")
        self.id: str = benefit_id
        self.name: str = name
        self.type: BenefitType = (
            BenefitType(distribution_type)
            if isinstance(distribution_type, str)
            and distribution_type in BenefitType.__members__.keys()
            else BenefitType.UNKNOWN
        )
        self.image_url: URLType = URLType(image_url)


class BaseDrop:
    def __init__(
        self, campaign: DropsCampaign, data: JsonType, claimed_benefits: dict[str, datetime]
    ):
        self._twitch: Twitch = campaign._twitch
        drop_id = data.get("id")
        name = data.get("name")
        start_at = data.get("startAt")
        end_at = data.get("endAt")
        if (
            not isinstance(drop_id, str)
            or not isinstance(name, str)
            or not isinstance(start_at, str)
            or not isinstance(end_at, str)
        ):
            raise ValueError("Drop data is incomplete")
        self.id: str = drop_id
        self.name: str = name
        self.campaign: DropsCampaign = campaign
        benefit_edges = data.get("benefitEdges") or []
        if not isinstance(benefit_edges, list):
            raise ValueError("Drop benefitEdges must be a list")
        self.benefits: list[Benefit] = [
            Benefit(benefit_data)
            for benefit_data in benefit_edges
            if isinstance(benefit_data, dict)
        ]
        self.starts_at: datetime = timestamp(start_at)
        self.ends_at: datetime = timestamp(end_at)
        self.claim_id: str | None = None
        self.is_claimed: bool = False
        self_data = data.get("self")
        if isinstance(self_data, dict):
            claim_id = self_data.get("dropInstanceID")
            self.claim_id = claim_id if isinstance(claim_id, str) else None
            self.is_claimed = bool(self_data.get("isClaimed", False))
        elif (
            # If there's no self edge available, we can use claimed_benefits to determine
            # (with pretty good certainty) if this drop has been claimed or not.
            # To do this, we check if the benefitEdges appear in claimed_benefits, and then
            # deref their "lastAwardedAt" timestamps into a list to check against.
            # If the benefits were claimed while the drop was active,
            # the drop has been claimed too.
            (
                dts := [
                    claimed_benefits[bid]
                    for benefit in self.benefits
                    if (bid := benefit.id) in claimed_benefits
                ]
            )
            and all(self.starts_at <= dt < self.ends_at for dt in dts)
        ):
            self.is_claimed = True
        precondition_data = data.get("preconditionDrops") or []
        if not isinstance(precondition_data, list):
            raise ValueError("Drop preconditionDrops must be a list")
        self.precondition_drops: list[str] = []
        for item in precondition_data:
            if not isinstance(item, dict):
                continue
            precondition_id = item.get("id")
            if isinstance(precondition_id, str):
                self.precondition_drops.append(precondition_id)

    def __repr__(self) -> str:
        if self.is_claimed:
            additional = ", claimed=True"
        elif self.can_earn():
            additional = ", can_earn=True"
        else:
            additional = ''
        return f"Drop({self.rewards_text()}{additional})"

    @property
    def preconditions_met(self) -> bool:
        campaign = self.campaign
        return all(
            (precondition := campaign.timed_drops.get(pid)) is not None
            and precondition.is_claimed
            for pid in self.precondition_drops
        )

    def _on_state_changed(self) -> None:
        raise NotImplementedError

    def _base_earn_conditions(self) -> bool:
        # define when a drop can be earned or not
        return (
            self.preconditions_met  # preconditions are met
            and not self.is_claimed  # isn't already claimed
            # has at least one benefit, or participates in a preconditions chain
            and (bool(self.benefits) or self.id in self.campaign.preconditions_chain())
        )

    def _base_can_earn(self) -> bool:
        # cross-participates in can_earn and can_earn_within handling, where a timeframe is added
        return (
            self._base_earn_conditions()
            # is within the timeframe
            and self.starts_at <= datetime.now(timezone.utc) < self.ends_at
        )

    def _can_earn_within(self, stamp: datetime) -> bool:
        # NOTE: This does not check the campaign's eligibility or active status
        return (
            self._base_earn_conditions()
            and self.ends_at > datetime.now(timezone.utc)
            and self.starts_at < stamp
        )

    def can_earn(
        self, channel: Channel | None = None, ignore_channel_status: bool = False
    ) -> bool:
        return (
            self._base_can_earn() and self.campaign._base_can_earn(channel, ignore_channel_status)
        )

    @property
    def can_claim(self) -> bool:
        # https://help.twitch.tv/s/article/mission-based-drops?language=en_US#claiming
        # "If you are unable to claim the Drop in time, you will be able to claim it
        # from the Drops Inventory page until 24 hours after the Drops campaign has ended."
        return (
            self.claim_id is not None
            and not self.is_claimed
            and datetime.now(timezone.utc) < self.campaign.ends_at + timedelta(hours=24)
        )

    def update_claim(self, claim_id: str):
        self.claim_id = claim_id

    async def generate_claim(self) -> None:
        # claim IDs now appear to be constructed from other IDs we have access to
        # Format: UserID#CampaignID#DropID
        # NOTE: This marks a drop as a ready-to-claim, so we may want to later ensure
        # its mining progress is finished first
        auth_state = await self.campaign._twitch.get_auth()
        self.claim_id = f"{auth_state.user_id}#{self.campaign.id}#{self.id}"

    def rewards_text(self, delim: str = ", ") -> str:
        return delim.join(benefit.name for benefit in self.benefits)

    async def claim(self) -> bool:
        result = await self._claim()
        if result:
            self.is_claimed = result
            claim_text = (
                f"{self.campaign.game.name}\n"
                f"{self.rewards_text()} "
                f"({self.campaign.claimed_drops}/{self.campaign.total_drops})"
            )
            # two different claim texts, becase a new line after the game name
            # looks ugly in the output window - replace it with a space
            self._twitch.print(
                _("status", "claimed_drop").format(drop=claim_text.replace('\n', ' '))
            )
            self._twitch.gui.tray.notify(claim_text, _("gui", "tray", "notification_title"))
        else:
            logger.error(f"Drop claim has potentially failed! Drop ID: {self.id}")
        return result

    async def _claim(self) -> bool:
        """
        Returns True if the claim succeeded, False otherwise.
        """
        if self.is_claimed:
            return True
        if not self.can_claim:
            return False
        try:
            response = await self._twitch.gql_request(
                GQL_QUERIES["ClaimDrop"].with_variables(
                    {"input": {"dropInstanceID": self.claim_id}}
                )
            )
        except (GQLException, RequestException):
            # Regardless of the error, assume claiming potentially failed and
            # let the next inventory/event reconciliation retry it.
            return False
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            logger.warning("Drop claim returned malformed GraphQL data: %s", self.id)
            return False
        if data.get("errors"):
            return False
        claim_result = data.get("claimDropRewards")
        if not isinstance(claim_result, dict):
            return False
        return claim_result.get("status") in (
            "ELIGIBLE_FOR_ALL",
            "DROP_INSTANCE_ALREADY_CLAIMED",
        )


class TimedDrop(BaseDrop):
    def __init__(
        self, campaign: DropsCampaign, data: JsonType, claimed_benefits: dict[str, datetime]
    ):
        super().__init__(campaign, data, claimed_benefits)
        self_data = data.get("self")
        raw_minutes = self_data.get("currentMinutesWatched", 0) if isinstance(self_data, dict) else 0
        try:
            self.required_minutes = max(0, int(data["requiredMinutesWatched"]))
            self.real_current_minutes = min(
                max(0, int(raw_minutes or 0)), self.required_minutes
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Timed Drop minute data is invalid") from exc
        if self.is_claimed:
            # claimed drops may report inconsistent current minutes, so we need to overwrite them
            self.real_current_minutes = self.required_minutes

    def __repr__(self) -> str:
        if self.is_claimed:
            additional = ", claimed=True"
        elif self.can_earn():
            additional = ", can_earn=True"
        else:
            additional = ''
        if 0 < self.current_minutes < self.required_minutes:
            minutes = f", {self.current_minutes}/{self.required_minutes}"
        else:
            minutes = ''
        return f"Drop({self.rewards_text()}{minutes}{additional})"

    @property
    def current_minutes(self) -> int:
        return self.real_current_minutes

    @property
    def remaining_minutes(self) -> int:
        return self.required_minutes - self.current_minutes

    @property
    def total_required_minutes(self) -> int:
        return self.required_minutes + max(
            (
                precondition.total_required_minutes
                for pid in self.precondition_drops
                if (precondition := self.campaign.timed_drops.get(pid)) is not None
            ),
            default=0,
        )

    @property
    def total_remaining_minutes(self) -> int:
        return self.remaining_minutes + max(
            (
                precondition.total_remaining_minutes
                for pid in self.precondition_drops
                if (precondition := self.campaign.timed_drops.get(pid)) is not None
            ),
            default=0,
        )

    @property
    def progress(self) -> float:
        if self.current_minutes <= 0 or self.required_minutes <= 0:
            return 0.0
        elif self.current_minutes >= self.required_minutes:
            return 1.0
        return self.current_minutes / self.required_minutes

    @property
    def availability(self) -> float:
        now = datetime.now(timezone.utc)
        if self.required_minutes > 0 and self.total_remaining_minutes > 0 and now < self.ends_at:
            return ((self.ends_at - now).total_seconds() / 60) / self.total_remaining_minutes
        return math.inf

    def _base_earn_conditions(self) -> bool:
        return super()._base_earn_conditions() and self.required_minutes > 0

    def _on_state_changed(self) -> None:
        self._twitch.gui.inv.update_drop(self)

    def _update_real_minutes(self, delta: int) -> None:
        if delta == 0 or self.real_current_minutes + delta < 0:
            return
        if self.real_current_minutes + delta < self.required_minutes:
            self.real_current_minutes += delta
        else:
            self.real_current_minutes = self.required_minutes
        self._on_state_changed()

    async def claim(self) -> bool:
        result = await super().claim()
        if result:
            self.real_current_minutes = self.required_minutes
        self._on_state_changed()
        return result

    def display(self, *, countdown: bool = True, subone: bool = False):
        self._twitch.gui.display_drop(self, countdown=countdown, subone=subone)

    def update_minutes(self, new_minutes: int):
        """Apply only newer authoritative progress; stale snapshots cannot rewind it."""
        if new_minutes <= self.real_current_minutes:
            return
        delta = min(new_minutes - self.real_current_minutes, self.required_minutes - self.real_current_minutes)
        if delta > 0:
            self.campaign._update_real_minutes(delta)


class DropsCampaign:
    def __init__(self, twitch: Twitch, data: JsonType, claimed_benefits: dict[str, datetime]):
        self._twitch: Twitch = twitch
        campaign_id = data.get("id")
        name = data.get("name")
        game_data = data.get("game")
        start_at = data.get("startAt")
        end_at = data.get("endAt")
        status = data.get("status")
        if (
            not isinstance(campaign_id, str)
            or not isinstance(name, str)
            or not isinstance(start_at, str)
            or not isinstance(end_at, str)
            or not isinstance(status, str)
        ):
            raise ValueError("Campaign data is incomplete")
        if not isinstance(game_data, dict):
            raise ValueError("Campaign game data is missing")
        box_art_url = game_data.get("boxArtURL")
        if not isinstance(box_art_url, str):
            raise ValueError("Campaign game artwork is missing")
        self.id: str = campaign_id
        self.name: str = name
        self.game: Game = Game(game_data)
        self_data = data.get("self") or {}
        self.linked: bool = (
            bool(self_data.get("isAccountConnected", False))
            if isinstance(self_data, dict)
            else False
        )
        account_link_url = data.get("accountLinkURL")
        self.link_url: str = account_link_url if isinstance(account_link_url, str) else ""
        # Campaign artwork comes from the game object. Remove Twitch's size suffix.
        self.image_url: URLType = remove_dimensions(URLType(box_art_url))
        self.starts_at: datetime = timestamp(start_at)
        self.ends_at: datetime = timestamp(end_at)
        self._valid: bool = status not in {"EXPIRED", "EXPIRED_MANUALLY", "DISABLED"}
        allowed = data.get("allow") or {}
        if not isinstance(allowed, dict):
            raise ValueError("Campaign allow data is invalid")
        allowed_data = allowed.get("channels") or []
        if not isinstance(allowed_data, list):
            raise ValueError("Campaign allow channels must be a list")
        allowlist_enabled = bool(allowed.get("isEnabled", True))
        self.allowed_channels: list[Channel] = []
        if allowlist_enabled:
            for channel_data in allowed_data:
                if not isinstance(channel_data, dict):
                    continue
                try:
                    self.allowed_channels.append(Channel.from_acl(twitch, channel_data))
                except (KeyError, TypeError, ValueError):
                    logger.warning("Ignoring malformed campaign allowlist channel")
        time_based_drops = data.get("timeBasedDrops") or []
        if not isinstance(time_based_drops, list):
            raise ValueError("Campaign timeBasedDrops must be a list")
        self.timed_drops: dict[str, TimedDrop] = {}
        for drop_data in time_based_drops:
            if not isinstance(drop_data, dict):
                continue
            drop_id = drop_data.get("id")
            if isinstance(drop_id, str):
                self.timed_drops[drop_id] = TimedDrop(self, drop_data, claimed_benefits)

    def __repr__(self) -> str:
        return f"Campaign({self.game!s}, {self.name}, {self.claimed_drops}/{self.total_drops})"

    @property
    def drops(self) -> abc.Iterable[TimedDrop]:
        return self.timed_drops.values()

    @property
    def time_triggers(self) -> set[datetime]:
        return set(
            chain(
                (self.starts_at, self.ends_at),
                *((d.starts_at, d.ends_at) for d in self.timed_drops.values()),
            )
        )

    @property
    def active(self) -> bool:
        return self._valid and self.starts_at <= datetime.now(timezone.utc) < self.ends_at

    @property
    def upcoming(self) -> bool:
        return self._valid and datetime.now(timezone.utc) < self.starts_at

    @property
    def expired(self) -> bool:
        return not self._valid or self.ends_at <= datetime.now(timezone.utc)

    @property
    def total_drops(self) -> int:
        return len(self.timed_drops)

    @property
    def eligible(self) -> bool:
        if self.has_badge_or_emote:
            return self._twitch.settings.enable_badges_emotes
        return self.linked

    @cached_property
    def has_badge_or_emote(self) -> bool:
        return any(
            benefit.type.is_badge_or_emote() for drop in self.drops for benefit in drop.benefits
        )

    @property
    def finished(self) -> bool:
        return all(d.is_claimed or d.required_minutes <= 0 for d in self.drops)

    @property
    def claimed_drops(self) -> int:
        return sum(d.is_claimed for d in self.drops)

    @property
    def remaining_drops(self) -> int:
        return sum(not d.is_claimed for d in self.drops)

    @property
    def required_minutes(self) -> int:
        return max((d.total_required_minutes for d in self.drops), default=0)

    @property
    def remaining_minutes(self) -> int:
        return max((d.total_remaining_minutes for d in self.drops), default=0)

    @property
    def progress(self) -> float:
        if self.total_drops == 0:
            return 0.0
        return sum(d.progress for d in self.drops) / self.total_drops

    @property
    def availability(self) -> float:
        return min((d.availability for d in self.drops), default=math.inf)

    @property
    def first_drop(self) -> TimedDrop | None:
        drops: list[TimedDrop] = sorted(
            (drop for drop in self.drops if drop.can_earn()),
            key=lambda d: d.remaining_minutes,
        )
        return drops[0] if drops else None

    def _update_real_minutes(self, delta: int) -> None:
        for drop in self.drops:
            drop._update_real_minutes(delta)

    def _base_can_earn(
        self, channel: Channel | None = None, ignore_channel_status: bool = False
    ) -> bool:
        return (
            self.eligible  # account is eligible
            and self.active  # campaign is active (and valid)
            and (
                channel is None or (  # channel isn't specified,
                    # or there's no ACL, or the channel is in the ACL
                    (not self.allowed_channels or channel in self.allowed_channels)
                    # and the channel is live and playing the campaign's game,
                    # or this campaign can be earned anywhere (special game)
                    and (
                        ignore_channel_status
                        or channel.game is not None and channel.game == self.game
                        or self.game.is_special()
                    )
                )
            )
        )

    def get_drop(self, drop_id: str) -> TimedDrop | None:
        return self.timed_drops.get(drop_id)

    def preconditions_chain(self) -> set[str]:
        return set(
            chain.from_iterable(
                drop.precondition_drops for drop in self.drops if not drop.is_claimed
            )
        )

    def can_earn(
        self, channel: Channel | None = None, ignore_channel_status: bool = False
    ) -> bool:
        # True if any of the containing drops can be earned
        return (
            self._base_can_earn(channel, ignore_channel_status)
            and any(drop._base_can_earn() for drop in self.drops)
        )

    def can_earn_within(self, stamp: datetime) -> bool:
        # Same as can_earn, but doesn't check the channel
        # and uses a future timestamp to see if we can earn this campaign later
        return (
            self.eligible
            and self._valid
            and self.ends_at > datetime.now(timezone.utc)
            and self.starts_at < stamp
            and any(drop._can_earn_within(stamp) for drop in self.drops)
        )
