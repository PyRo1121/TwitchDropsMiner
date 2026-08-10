from __future__ import annotations

import re
import math
from enum import Enum
from itertools import chain
from typing import TYPE_CHECKING
from datetime import datetime, timedelta, timezone

from channel import Channel
from drop_claim_service import DropClaimService, DropClaimState
from game import Game
from utils import timestamp
from constants import URLType

if TYPE_CHECKING:
    from collections import abc

    from twitch import Twitch
    from constants import JsonType


DIMS_PATTERN = re.compile(r'-\d+x\d+(?=\.(?:jpg|png|gif)$)', re.I)


def remove_dimensions(url: URLType) -> URLType:
    return URLType(DIMS_PATTERN.sub("", url))


def _strict_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


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
        if (
            not isinstance(benefit_id, str)
            or not benefit_id
            or not isinstance(name, str)
            or not name
            or not isinstance(image_url, str)
            or not image_url
        ):
            raise ValueError("Drop benefit data is incomplete")
        self.id: str = benefit_id
        self.name: str = name
        self.type: BenefitType = (
            BenefitType(distribution_type)
            if isinstance(distribution_type, str)
            and distribution_type in BenefitType.__members__
            else BenefitType.UNKNOWN
        )
        self.image_url: URLType = URLType(image_url)


class BaseDrop:
    def __init__(self, campaign: DropsCampaign, data: JsonType):
        self._twitch: Twitch = campaign._twitch
        drop_id = data.get("id")
        name = data.get("name")
        start_at = data.get("startAt")
        end_at = data.get("endAt")
        if (
            not isinstance(drop_id, str)
            or not drop_id
            or not isinstance(name, str)
            or not name
            or not isinstance(start_at, str)
            or not isinstance(end_at, str)
        ):
            raise ValueError("Drop data is incomplete")
        self.id: str = drop_id
        self.name: str = name
        self.campaign: DropsCampaign = campaign
        benefit_edges = data.get("benefitEdges", [])
        if benefit_edges is None:
            benefit_edges = []
        if not isinstance(benefit_edges, list):
            raise ValueError("Drop benefitEdges must be a list")
        self.benefits: list[Benefit] = []
        benefit_ids: set[str] = set()
        for benefit_data in benefit_edges:
            if not isinstance(benefit_data, dict):
                raise ValueError("Drop benefitEdges contains invalid data")
            benefit = Benefit(benefit_data)
            if benefit.id in benefit_ids:
                raise ValueError("Drop contains a duplicate benefit ID")
            benefit_ids.add(benefit.id)
            self.benefits.append(benefit)
        self.starts_at: datetime = timestamp(start_at)
        self.ends_at: datetime = timestamp(end_at)
        if self.starts_at >= self.ends_at:
            raise ValueError("Drop start time must precede its end time")

        self_data = data.get("self")
        self._claim_state_authoritative = self_data is not None
        if self_data is None:
            claim_id = None
            is_claimed = False
        else:
            if not isinstance(self_data, dict):
                raise ValueError("Drop self data must be an object")
            claim_id = self_data.get("dropInstanceID")
            if claim_id is not None and not isinstance(claim_id, str):
                raise ValueError("Drop instance ID must be a string")
            is_claimed = _strict_bool(
                self_data.get("isClaimed", False),
                "Drop isClaimed",
            )
        self._claim_state = DropClaimState.from_payload(
            is_claimed=is_claimed,
            claim_id=claim_id,
        )

        precondition_data = data.get("preconditionDrops", [])
        if precondition_data is None:
            precondition_data = []
        if not isinstance(precondition_data, list):
            raise ValueError("Drop preconditionDrops must be a list")
        self.precondition_drops: list[str] = []
        for item in precondition_data:
            if not isinstance(item, dict):
                raise ValueError("Drop preconditionDrops contains invalid data")
            precondition_id = item.get("id")
            if not isinstance(precondition_id, str) or not precondition_id:
                raise ValueError("Drop precondition ID must be a string")
            if precondition_id in self.precondition_drops:
                raise ValueError("Drop contains a duplicate precondition ID")
            self.precondition_drops.append(precondition_id)

    def _state_suffix(self) -> str:
        if self.is_claimed:
            return ", claimed=True"
        if self.can_earn():
            return ", can_earn=True"
        return ""

    def __repr__(self) -> str:
        return f"Drop({self.rewards_text()}{self._state_suffix()})"

    def _preconditions(self) -> list[TimedDrop]:
        return [self.campaign.timed_drops[pid] for pid in self.precondition_drops]

    @property
    def preconditions_met(self) -> bool:
        return all(precondition.is_claimed for precondition in self._preconditions())

    def _on_state_changed(self) -> None:
        raise NotImplementedError

    def _benefits_are_eligible(self) -> bool:
        has_badge_or_emote = any(
            benefit.type.is_badge_or_emote() for benefit in self.benefits
        )
        has_direct_entitlement = any(
            not benefit.type.is_badge_or_emote() for benefit in self.benefits
        )
        return (
            has_direct_entitlement and self.campaign.linked
            or has_badge_or_emote
            and self._twitch.settings.enable_badges_emotes
        )

    @property
    def eligible(self) -> bool:
        if self.benefits:
            return self._benefits_are_eligible()
        return self.campaign._prerequisite_is_eligible(self.id)

    def _base_earn_conditions(self) -> bool:
        # define when a drop can be earned or not
        return (
            self.eligible
            and self.preconditions_met  # preconditions are met
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
    def claim_id(self) -> str | None:
        return self._claim_state.claim_id

    @claim_id.setter
    def claim_id(self, value: str | None) -> None:
        # A failed synthetic claim is cleared by watch reconciliation before a
        # later authoritative inventory refresh supplies another instance ID.
        if value is None:
            self._claim_state = self._claim_state.clear_instance()
        else:
            self.update_claim(value)

    @property
    def is_claimed(self) -> bool:
        return self._claim_state.is_claimed

    @property
    def can_claim(self) -> bool:
        # https://help.twitch.tv/s/article/mission-based-drops?language=en_US#claiming
        # "If you are unable to claim the Drop in time, you will be able to claim it
        # from the Drops Inventory page until 24 hours after the Drops campaign has ended."
        return (
            self._claim_state.is_ready
            and datetime.now(timezone.utc) < self.campaign.ends_at + timedelta(hours=24)
        )

    def update_claim(self, claim_id: str) -> None:
        self._claim_state = self._claim_state.ready(claim_id)

    async def generate_claim(self) -> None:
        await self.campaign._claim_service.generate_claim(self)

    def rewards_text(self, delim: str = ", ") -> str:
        return delim.join(benefit.name for benefit in self.benefits)

    async def claim(self) -> bool:
        return await self.campaign._claim_service.claim(self)

    def _apply_claim_result(self, result: bool) -> None:
        if result:
            self._claim_state = self._claim_state.claimed()

    def _present_claim_result(self) -> None:
        self._on_state_changed()


class TimedDrop(BaseDrop):
    def __init__(self, campaign: DropsCampaign, data: JsonType):
        super().__init__(campaign, data)
        try:
            required_minutes = _nonnegative_int(
                data["requiredMinutesWatched"],
                "Timed Drop requiredMinutesWatched",
            )
        except KeyError as exc:
            raise ValueError("Timed Drop minute data is invalid") from exc
        self.required_minutes = required_minutes

        if self.is_claimed:
            # Claimed state is authoritative. Twitch can omit or return stale
            # progress for claimed drops, so that irrelevant field is not parsed.
            self.real_current_minutes = required_minutes
            return

        self_data = data.get("self")
        raw_minutes = (
            self_data.get("currentMinutesWatched", 0)
            if isinstance(self_data, dict)
            else 0
        )
        current_minutes = _nonnegative_int(
            raw_minutes,
            "Timed Drop currentMinutesWatched",
        )
        # Twitch occasionally reports completed progress beyond the advertised
        # requirement. Treat that as complete while preserving the domain
        # invariant that progress never exceeds its requirement.
        self.real_current_minutes = min(current_minutes, required_minutes)

    def __repr__(self) -> str:
        if 0 < self.current_minutes < self.required_minutes:
            minutes = f", {self.current_minutes}/{self.required_minutes}"
        else:
            minutes = ""
        return f"Drop({self.rewards_text()}{minutes}{self._state_suffix()})"

    @property
    def current_minutes(self) -> int:
        return self.real_current_minutes

    @property
    def remaining_minutes(self) -> int:
        return self.required_minutes - self.current_minutes

    @property
    def total_required_minutes(self) -> int:
        return self.required_minutes + max(
            (precondition.total_required_minutes for precondition in self._preconditions()),
            default=0,
        )

    @property
    def total_remaining_minutes(self) -> int:
        return self.remaining_minutes + max(
            (precondition.total_remaining_minutes for precondition in self._preconditions()),
            default=0,
        )

    @property
    def progress(self) -> float:
        if self.required_minutes <= 0:
            return 1.0
        if self.current_minutes <= 0:
            return 0.0
        if self.current_minutes >= self.required_minutes:
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

    def _apply_claim_result(self, result: bool) -> None:
        super()._apply_claim_result(result)
        if result:
            self.real_current_minutes = self.required_minutes

    def _apply_claim_evidence(self) -> None:
        self._claim_state = self._claim_state.claimed()
        self.real_current_minutes = self.required_minutes

    def display(self, *, countdown: bool = True, subone: bool = False):
        self._twitch.gui.display_drop(self, countdown=countdown, subone=subone)

    def update_minutes(
        self,
        new_minutes: int,
        required_minutes: int | None = None,
    ) -> None:
        """Apply a newer authoritative progress/requirement pair."""
        current = _nonnegative_int(new_minutes, "Drop current progress")
        required = (
            self.required_minutes
            if required_minutes is None
            else _nonnegative_int(required_minutes, "Drop required progress")
        )
        current = min(current, required)
        if current < self.real_current_minutes:
            return

        requirement_changed = required != self.required_minutes
        self.required_minutes = required
        delta = current - self.real_current_minutes
        if delta > 0:
            self.campaign._bump_all_minutes(delta)
        elif requirement_changed:
            self._on_state_changed()


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
            or not campaign_id
            or not isinstance(name, str)
            or not name
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
        known_statuses = {
            "ACTIVE",
            "UPCOMING",
            "EXPIRED",
            "EXPIRED_MANUALLY",
            "DISABLED",
        }
        if status not in known_statuses:
            raise ValueError(f"Campaign status is unknown: {status}")

        self.id: str = campaign_id
        self.name: str = name
        self.game: Game = Game(game_data)
        self_data = data.get("self")
        if self_data is None:
            self_data = {}
        if not isinstance(self_data, dict):
            raise ValueError("Campaign self data is invalid")
        self.linked = _strict_bool(
            self_data.get("isAccountConnected", False),
            "Campaign isAccountConnected",
        )
        account_link_url = data.get("accountLinkURL")
        self.link_url: str = (
            account_link_url if isinstance(account_link_url, str) else ""
        )
        # Campaign artwork comes from the game object. Remove Twitch's size suffix.
        self.image_url: URLType = remove_dimensions(URLType(box_art_url))
        self.starts_at: datetime = timestamp(start_at)
        self.ends_at: datetime = timestamp(end_at)
        if self.starts_at >= self.ends_at:
            raise ValueError("Campaign start time must precede its end time")
        self._valid = status in {"ACTIVE", "UPCOMING"}
        self._claim_service = DropClaimService(twitch)

        allowed = data.get("allow")
        if allowed is None:
            allowed = {}
        if not isinstance(allowed, dict):
            raise ValueError("Campaign allow data is invalid")
        allowed_data = allowed.get("channels", [])
        if allowed_data is None:
            allowed_data = []
        if not isinstance(allowed_data, list):
            raise ValueError("Campaign allow channels must be a list")
        allowlist_enabled = _strict_bool(
            allowed.get("isEnabled", False),
            "Campaign allow isEnabled",
        )
        self.allowed_channels: list[Channel] = []
        if allowlist_enabled:
            if not allowed_data:
                raise ValueError("Enabled campaign allowlist is empty")
            for channel_data in allowed_data:
                if not isinstance(channel_data, dict):
                    raise ValueError("Campaign allowlist contains invalid data")
                try:
                    channel = Channel.from_acl(twitch, channel_data)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "Campaign allowlist contains an invalid channel"
                    ) from exc
                self.allowed_channels.append(channel)

        time_based_drops = data.get("timeBasedDrops", [])
        if time_based_drops is None:
            time_based_drops = []
        if not isinstance(time_based_drops, list):
            raise ValueError("Campaign timeBasedDrops must be a list")
        self.timed_drops: dict[str, TimedDrop] = {}
        for drop_data in time_based_drops:
            if not isinstance(drop_data, dict):
                raise ValueError("Campaign contains a malformed timed drop")
            drop_id = drop_data.get("id")
            if not isinstance(drop_id, str) or not drop_id:
                raise ValueError("Campaign timed drop is missing an ID")
            if drop_id in self.timed_drops:
                raise ValueError(f"Campaign contains duplicate drop ID: {drop_id}")
            self.timed_drops[drop_id] = TimedDrop(self, drop_data)
        self._validate_preconditions()
        if claimed_benefits:
            resolve_claim_evidence((self,), claimed_benefits)

    def _validate_preconditions(self) -> None:
        states: dict[str, int] = {}

        def visit(drop_id: str) -> None:
            state = states.get(drop_id, 0)
            if state == 1:
                raise ValueError("Campaign drop prerequisites contain a cycle")
            if state == 2:
                return
            states[drop_id] = 1
            drop = self.timed_drops[drop_id]
            for precondition_id in drop.precondition_drops:
                if precondition_id not in self.timed_drops:
                    raise ValueError(
                        "Campaign drop references a missing prerequisite"
                    )
                visit(precondition_id)
            states[drop_id] = 2

        for drop_id in self.timed_drops:
            visit(drop_id)

    def _precondition_ids(self, drop: TimedDrop) -> set[str]:
        preconditions: set[str] = set()
        pending = list(drop.precondition_drops)
        while pending:
            precondition_id = pending.pop()
            if precondition_id in preconditions:
                continue
            preconditions.add(precondition_id)
            pending.extend(self.timed_drops[precondition_id].precondition_drops)
        return preconditions

    def _prerequisite_is_eligible(self, drop_id: str) -> bool:
        return any(
            candidate._benefits_are_eligible()
            and drop_id in self._precondition_ids(candidate)
            for candidate in self.drops
            if candidate.benefits
        )

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
        return any(drop.eligible for drop in self.drops)

    @property
    def finished(self) -> bool:
        return all(d.is_claimed or d.required_minutes <= 0 for d in self.drops)

    @property
    def claimed_drops(self) -> int:
        return sum(d.is_claimed for d in self.drops)

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

    def _bump_all_minutes(self, delta: int) -> None:
        for drop in self.drops:
            drop._update_real_minutes(delta)

    def _base_can_earn(
        self, channel: Channel | None = None, ignore_channel_status: bool = False
    ) -> bool:
        return (
            self.active  # campaign is active (and valid)
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

    def preconditions_chain(self) -> set[str]:
        return set(
            chain.from_iterable(
                self._precondition_ids(drop)
                for drop in self.drops
                if not drop.is_claimed
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


def resolve_claim_evidence(
    campaigns: abc.Iterable[DropsCampaign],
    claimed_benefits: dict[str, datetime],
) -> None:
    """Resolve account-wide benefit awards against one staged inventory.

    Twitch's claimed-benefit feed is account-wide and benefit IDs can be reused.
    An award is assigned only when its campaign/drop identity is unique across
    every valid campaign whose claim grace window contains the award.
    """
    staged_campaigns = tuple(campaigns)
    owners: dict[str, list[tuple[DropsCampaign, TimedDrop]]] = {}
    for campaign in staged_campaigns:
        for drop in campaign.drops:
            for benefit in drop.benefits:
                owners.setdefault(benefit.id, []).append((campaign, drop))

    candidates: dict[str, set[tuple[str, str]]] = {}
    for benefit_id, awarded_at in claimed_benefits.items():
        candidates[benefit_id] = {
            (campaign.id, drop.id)
            for campaign, drop in owners.get(benefit_id, ())
            if (
                drop.starts_at
                <= awarded_at
                < campaign.ends_at + timedelta(hours=24)
            )
        }

    for campaign in staged_campaigns:
        for drop in campaign.drops:
            if drop._claim_state_authoritative or not drop.benefits:
                continue
            identity = (campaign.id, drop.id)
            if all(
                candidates.get(benefit.id) == {identity}
                for benefit in drop.benefits
            ):
                drop._apply_claim_evidence()
