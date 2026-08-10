"""Owns the serialized Twitch Drop claim lifecycle.

Inventory entities own claim state; this service owns authentication, network I/O,
serialization, and user-visible claim outcomes.  Keeping those concerns here makes
a claim transition one operation instead of a collection of side effects spread
through ``BaseDrop``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from constants import GQL_QUERIES
from exceptions import GQLException, RequestException
from translate import _

if TYPE_CHECKING:
    from inventory import BaseDrop
    from twitch import Twitch


logger = logging.getLogger("TwitchDrops")


class ClaimStatus(Enum):
    """Mutually exclusive states for a drop's claim lifecycle."""

    UNAVAILABLE = "unavailable"
    READY = "ready"
    CLAIMED = "claimed"


@dataclass(frozen=True, slots=True)
class DropClaimState:
    """Validated claim state stored by an inventory drop.

    ``READY`` always carries a non-empty instance ID. ``UNAVAILABLE`` never
    carries one. Twitch may report an already-claimed drop without an instance
    ID, so ``CLAIMED`` intentionally permits either shape.
    """

    status: ClaimStatus
    claim_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ClaimStatus):
            raise ValueError("Drop claim status is invalid")
        if self.claim_id is not None and (
            not isinstance(self.claim_id, str) or not self.claim_id
        ):
            raise ValueError("Drop instance ID must be a non-empty string")
        if self.status is ClaimStatus.READY and self.claim_id is None:
            raise ValueError("A ready Drop claim requires an instance ID")
        if self.status is ClaimStatus.UNAVAILABLE and self.claim_id is not None:
            raise ValueError("An unavailable Drop claim cannot have an instance ID")

    @classmethod
    def from_payload(
        cls,
        *,
        is_claimed: bool,
        claim_id: str | None,
    ) -> DropClaimState:
        if is_claimed:
            return cls(ClaimStatus.CLAIMED, claim_id)
        if claim_id is not None:
            return cls(ClaimStatus.READY, claim_id)
        return cls(ClaimStatus.UNAVAILABLE)

    @property
    def is_claimed(self) -> bool:
        return self.status is ClaimStatus.CLAIMED

    @property
    def is_ready(self) -> bool:
        return self.status is ClaimStatus.READY

    def ready(self, claim_id: str) -> DropClaimState:
        if self.is_claimed:
            return DropClaimState(ClaimStatus.CLAIMED, claim_id)
        return DropClaimState(ClaimStatus.READY, claim_id)

    def clear_instance(self) -> DropClaimState:
        if self.is_claimed:
            return DropClaimState(ClaimStatus.CLAIMED)
        return DropClaimState(ClaimStatus.UNAVAILABLE)

    def claimed(self) -> DropClaimState:
        return DropClaimState(ClaimStatus.CLAIMED, self.claim_id)


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """Immutable network request captured from a ready claim state."""

    campaign_id: str
    drop_id: str
    claim_id: str

    def __post_init__(self) -> None:
        for field, value in (
            ("campaign_id", self.campaign_id),
            ("drop_id", self.drop_id),
            ("claim_id", self.claim_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"Claim {field} must be a non-empty string")


class DropClaimService:
    """Serializes and publishes claim transitions for one campaign generation."""

    def __init__(self, twitch: Twitch) -> None:
        self._twitch = twitch
        self._locks: dict[str, asyncio.Lock] = {}

    async def generate_claim(self, drop: BaseDrop) -> None:
        """Create a validated synthetic instance ID for a completed drop."""
        auth_state = await self._twitch.get_auth()
        user_id = auth_state.user_id
        if type(user_id) is not int or user_id < 1:
            raise RequestException("Cannot generate a Drop claim without a user ID")
        drop.update_claim(f"{user_id}#{drop.campaign.id}#{drop.id}")

    async def claim(self, drop: BaseDrop) -> bool:
        """Attempt one serialized claim and commit its resulting state."""
        lock = self._locks.setdefault(drop.id, asyncio.Lock())
        async with lock:
            if drop.is_claimed:
                drop._apply_claim_result(True)
                drop._present_claim_result()
                return True

            request = self._request_for(drop)
            claim_attempted = request is not None
            result = request is not None and await self._submit(request)
            drop._apply_claim_result(result)
            self._publish_result(drop, result, claim_attempted=claim_attempted)
            drop._present_claim_result()
            return result

    @staticmethod
    def _request_for(drop: BaseDrop) -> ClaimRequest | None:
        if not drop.can_claim:
            return None
        claim_id = drop.claim_id
        if claim_id is None:
            # ``can_claim`` and claim state are required to agree.
            raise RuntimeError("Ready Drop claim lost its instance ID")
        return ClaimRequest(drop.campaign.id, drop.id, claim_id)

    async def _submit(self, request: ClaimRequest) -> bool:
        try:
            response = await self._twitch.transport.gql_request(
                GQL_QUERIES["ClaimDrop"].with_variables(
                    {"input": {"dropInstanceID": request.claim_id}}
                )
            )
        except (GQLException, RequestException):
            # Inventory/event reconciliation remains the source of truth after
            # an unconfirmed request.
            return False

        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            logger.warning(
                "Drop claim returned malformed GraphQL data: %s",
                request.drop_id,
            )
            return False
        if data.get("errors"):
            return False
        claim_result = data.get("claimDropRewards")
        if not isinstance(claim_result, dict):
            return False
        status = claim_result.get("status")
        return status in {
            "ELIGIBLE_FOR_ALL",
            "DROP_INSTANCE_ALREADY_CLAIMED",
        }

    def _publish_result(
        self,
        drop: BaseDrop,
        result: bool,
        *,
        claim_attempted: bool,
    ) -> None:
        if result:
            if not claim_attempted:
                return
            self._twitch.history_event(
                "claim.succeeded",
                data={
                    "campaign_id": drop.campaign.id,
                    "drop_id": drop.id,
                    "game": drop.campaign.game.name,
                    "reward": drop.rewards_text(),
                },
            )
            claim_text = (
                f"{drop.campaign.game.name}\n"
                f"{drop.rewards_text()} "
                f"({drop.campaign.claimed_drops}/{drop.campaign.total_drops})"
            )
            self._twitch.print(
                _("status", "claimed_drop").format(
                    drop=claim_text.replace("\n", " ")
                )
            )
            self._twitch.gui.tray.notify(
                claim_text,
                _("gui", "tray", "notification_title"),
            )
            return

        if claim_attempted:
            self._twitch.history_event(
                "claim.unconfirmed",
                severity="warning",
                data={
                    "campaign_id": drop.campaign.id,
                    "drop_id": drop.id,
                    "game": drop.campaign.game.name,
                    "reward": drop.rewards_text(),
                },
            )
        logger.error("Drop claim has potentially failed! Drop ID: %s", drop.id)
