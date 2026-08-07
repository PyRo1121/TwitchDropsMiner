from __future__ import annotations

import re
import html
import asyncio
import logging
from base64 import b64encode

from typing import Any, SupportsInt, TYPE_CHECKING

import aiohttp
from yarl import URL

from game import Game
from utils import extract_available_drops, json_minify, isonow, require_int
from exceptions import ExitRequest, MinerException, ReloadRequest
from constants import CALL, GQL_QUERIES, ONLINE_DELAY, URLType

if TYPE_CHECKING:
    from twitch import Twitch
    from constants import JsonType, GQLPersistedQuery


logger = logging.getLogger("TwitchDrops")


class Stream:
    def __init__(
        self,
        channel: Channel,
        *,
        id: SupportsInt,
        game: JsonType | None,
        viewers: int,
        title: str,
    ):
        self.channel: Channel = channel
        self.broadcast_id = require_int(id, f"Invalid broadcast id: {id!r}")
        self.viewers = require_int(
            viewers, "Stream data must contain an integer viewer count"
        )
        self.drops_enabled: bool = not channel._twitch.settings.available_drops_check
        if game is not None and not isinstance(game, dict):
            raise ValueError("Stream game data is invalid")
        self.game: Game | None = Game(game) if isinstance(game, dict) else None
        if not isinstance(title, str):
            raise ValueError("Stream data must contain a title")
        self.title: str = title

    def _watch_payload(self) -> list[JsonType]:
        """Build a fresh viewer-presence event for this broadcast.

        Twitch's player sends a current timestamp for each minute-watched event.
        Keeping this payload cached makes every heartbeat look like the original
        event and is especially harmful when two broadcasts are farmed together.
        """
        return [
            {
                "event": "minute-watched",
                "properties": {
                    "broadcast_id": str(self.broadcast_id),
                    "channel_id": str(self.channel.id),
                    "channel": self.channel._login,
                    "client_time": isonow(),
                    "game": self.game.name if self.game is not None else "",
                    "game_id": str(self.game.id) if self.game is not None else "",
                    "hidden": False,
                    "is_live": True,
                    "live": True,
                    "logged_in": True,
                    "minutes_logged": 1,
                    "muted": False,
                    "user_id": self.channel._twitch._auth_state.user_id,
                }
            }
        ]

    @property
    def spade_payload(self) -> JsonType:
        return {
            "data": b64encode(json_minify(self._watch_payload()).encode("utf8")).decode("utf8")
        }


    @classmethod
    def from_get_stream(cls, channel: Channel, channel_data: JsonType) -> Stream:
        stream = channel_data["stream"]
        settings = channel_data["broadcastSettings"]
        return cls(
            channel,
            id=stream["id"],
            game=settings["game"],
            viewers=stream["viewersCount"],
            title=settings["title"],
        )

    @classmethod
    def from_directory(
        cls, channel: Channel, channel_data: JsonType, *, drops_enabled: bool = False
    ) -> Stream:
        self = cls(
            channel,
            id=channel_data["id"],
            game=channel_data["game"],  # has to be there since we searched with it
            viewers=channel_data["viewersCount"],
            title=channel_data["title"],
        )
        self.drops_enabled = drops_enabled
        return self


class Channel:
    __slots__ = (
        "_twitch", "_gui_channels", "id", "_login", "_display_name", "_spade_url",
        "_stream", "_pending_stream_up", "acl_based"
    )

    def __init__(
        self,
        twitch: Twitch,
        *,
        id: SupportsInt,
        login: str,
        display_name: str | None = None,
        acl_based: bool = False,
    ):
        self._twitch: Twitch = twitch
        self._gui_channels: Any = twitch.gui.channels
        self.id = require_int(id, f"Invalid channel id: {id!r}")
        if not isinstance(login, str) or not login:
            raise ValueError("Channel data must contain a login")
        self._login = login
        self._display_name: str | None = display_name if isinstance(display_name, str) else None
        self._spade_url: URLType | None = None
        self._stream: Stream | None = None
        self._pending_stream_up: asyncio.Task[Any] | None = None
        # ACL-based channels are:
        # • considered first when switching channels
        # • if we're watching a non-based channel, a based channel going up triggers a switch
        # • not cleaned up unless they're streaming a game we haven't selected
        self.acl_based: bool = acl_based

    @classmethod
    def from_acl(cls, twitch: Twitch, data: JsonType) -> Channel:
        return cls(
            twitch,
            id=data["id"],
            login=data["name"],
            display_name=data.get("displayName"),
            acl_based=True,
        )

    @classmethod
    def from_directory(
        cls, twitch: Twitch, data: JsonType, *, drops_enabled: bool = False
    ) -> Channel:
        channel = data["broadcaster"]
        self = cls(
            twitch, id=channel["id"], login=channel["login"], display_name=channel["displayName"]
        )
        self._stream = Stream.from_directory(self, data, drops_enabled=drops_enabled)
        return self

    def __repr__(self) -> str:
        if self._display_name is not None:
            name = f"{self._display_name}({self._login})"
        else:
            name = self._login
        return f"Channel({name}, {self.id})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, self.__class__):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return self.id

    @property
    def stream_gql(self) -> GQLPersistedQuery:
        return GQL_QUERIES["GetStreamInfo"].with_variables({"channel": self._login})

    @property
    def name(self) -> str:
        if self._display_name is not None:
            return self._display_name
        return self._login

    @property
    def url(self) -> URLType:
        return URLType(f"{self._twitch._client_type.CLIENT_URL}/{self._login}")

    @property
    def iid(self) -> str:
        """
        Returns a string to be used as ID/key of the columns inside channel list.
        """
        return str(self.id)

    @property
    def online(self) -> bool:
        """
        Returns True if the streamer is online and is currently streaming, False otherwise.
        """
        return self._stream is not None

    @property
    def offline(self) -> bool:
        """
        Returns True if the streamer is offline and isn't about to come online, False otherwise.
        """
        return self._stream is None and self._pending_stream_up is None

    @property
    def pending_online(self) -> bool:
        """
        Returns True if the streamer is about to go online (most likely), False otherwise.
        This is because 'stream-up' event is received way before
        stream information becomes available.
        """
        return self._stream is None and self._pending_stream_up is not None

    @property
    def game(self) -> Game | None:
        if self._stream is not None:
            return self._stream.game
        return None

    @property
    def viewers(self) -> int | None:
        if self._stream is not None:
            return self._stream.viewers
        return None

    @viewers.setter
    def viewers(self, value: int):
        if self._stream is not None:
            self._stream.viewers = value

    @property
    def drops_enabled(self) -> bool:
        if self._stream is not None:
            return self._stream.drops_enabled
        return False

    def display(self, *, add: bool = False):
        self._gui_channels.display(self, add=add)

    def remove(self):
        if self._pending_stream_up is not None:
            self._pending_stream_up.cancel()
            self._pending_stream_up = None
        self._gui_channels.remove(self)

    async def get_spade_url(self) -> URLType:
        """
        To get this monstrous thing, you have to walk a chain of requests.
        Streamer page (HTML) --parse-> Streamer Settings (JavaScript) --parse-> Spade URL

        For mobile view, spade_url is available immediately from the page, skipping step #2.
        """
        SETTINGS_PATTERN: str = r'src="(https://[\w.-]+/config/settings\.[0-9a-f]{32}\.js)"'
        SPADE_PATTERN: str = r'"spade_?url"\s*:\s*"(https://[^"]+)"'
        async with self._twitch.request("GET", self.url) as response1:
            streamer_html: str = await response1.text(encoding="utf8")
        match = re.search(SPADE_PATTERN, streamer_html, re.I)
        if not match:
            match = re.search(SETTINGS_PATTERN, streamer_html, re.I)
            if not match:
                raise MinerException("Error while spade_url extraction: step #1")
            streamer_settings = match.group(1)
            async with self._twitch.request("GET", streamer_settings) as response2:
                settings_js: str = await response2.text(encoding="utf8")
            match = re.search(SPADE_PATTERN, settings_js, re.I)
            if not match:
                raise MinerException("Error while spade_url extraction: step #2")
        spade_url = URL(html.unescape(match.group(1)).replace("\\/", "/"))
        if spade_url.scheme != "https" or spade_url.host != "spade.twitch.tv":
            raise MinerException("Unexpected Spade endpoint")
        return URLType(str(spade_url))

    def _check_drops_enabled(self, available_drops: list[JsonType]) -> bool:
        for campaign_data in available_drops:
            if not isinstance(campaign_data, dict):
                continue
            campaign_id = campaign_data.get("id")
            if not isinstance(campaign_id, str):
                continue
            campaign = self._twitch._campaigns.get(campaign_id)
            if campaign is not None and campaign.can_earn(self, ignore_channel_status=True):
                return True
        return False

    def external_update(self, channel_data: JsonType, available_drops: list[JsonType]):
        """
        Update stream information based on data provided externally.

        Used for bulk-updates of channel statuses during reload.
        """
        if not channel_data["stream"]:
            self._stream = None
            return
        stream = Stream.from_get_stream(self, channel_data)
        if not stream.drops_enabled:
            stream.drops_enabled = self._check_drops_enabled(available_drops)
        self._stream = stream

    async def get_stream(self) -> Stream | None:
        try:
            response: JsonType = await self._twitch.gql_request(self.stream_gql)
        except MinerException as exc:
            raise MinerException(f"Channel: {self._login}") from exc
        try:
            data = response["data"]
            channel_data = data["user"]
        except (KeyError, TypeError) as exc:
            raise MinerException(f"Channel: {self._login} returned malformed stream data") from exc
        if channel_data is None:
            return None
        if not isinstance(channel_data, dict):
            raise MinerException(f"Channel: {self._login} returned malformed stream data")
        # fill in display name
        if self._display_name is None:
            display_name = channel_data.get("displayName")
            if isinstance(display_name, str) and display_name:
                self._display_name = display_name
        stream_data = channel_data.get("stream")
        if stream_data is None:
            return None
        if not isinstance(stream_data, dict):
            raise MinerException(f"Channel: {self._login} returned malformed stream data")
        try:
            stream = Stream.from_get_stream(self, channel_data)
        except (KeyError, TypeError, ValueError) as exc:
            raise MinerException(f"Channel: {self._login} returned malformed stream data") from exc
        if not stream.drops_enabled:
            try:
                available_drops_campaigns: JsonType = await self._twitch.gql_request(
                    GQL_QUERIES["AvailableDrops"].with_variables({"channelID": str(self.id)})
                )
            except MinerException:
                logger.log(CALL, f"AvailableDrops GQL call failed for channel: {self._login}")
            else:
                available_drops = extract_available_drops(available_drops_campaigns)
                stream.drops_enabled = self._check_drops_enabled(available_drops)
        return stream

    async def update_stream(self) -> bool:
        """
        Fetches the current channel stream, and if one exists,
        updates it's game, title, tags and viewers. Updates channel status in general.
        """
        old_stream = self._stream
        self._stream = await self.get_stream()
        self._twitch.on_channel_update(self, old_stream, self._stream)
        return self._stream is not None

    async def _online_delay(self):
        """
        The 'stream-up' event is sent before the stream actually goes online,
        so just wait a bit and check if it's actually online by then.
        """
        try:
            await asyncio.sleep(ONLINE_DELAY.total_seconds())
            await self.update_stream()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Delayed online check failed for channel %s", self._login)
        finally:
            self._pending_stream_up = None  # for 'display' to work properly

    def check_online(self):
        """
        Sets up a task that will wait ONLINE_DELAY duration,
        and then check for the stream being ONLINE OR OFFLINE.

        If the channel is OFFLINE, it sets the channel's status to PENDING_ONLINE,
        where after ONLINE_DELAY, it's going to be set to ONLINE.
        If the channel is ONLINE already, after ONLINE_DELAY,
        it's status is going to be double-checked to ensure it's actually ONLINE.

        This is called externally, if we receive an event about the status possibly being ONLINE
        or having to be updated.
        """
        if self._pending_stream_up is None:
            self._pending_stream_up = asyncio.create_task(self._online_delay())
            self.display()

    def set_offline(self):
        """
        Sets the channel status to OFFLINE. Cancels PENDING_ONLINE if applicable.

        This is called externally, if we receive an event indicating the channel is now OFFLINE.
        """
        needs_display: bool = False
        if self._pending_stream_up is not None:
            self._pending_stream_up.cancel()
            self._pending_stream_up = None
            needs_display = True
        if self.online:
            old_stream = self._stream
            self._stream = None
            self._twitch.on_channel_update(self, old_stream, self._stream)
            needs_display = False  # calling on_channel_update always does a display at the end
        if needs_display:
            self.display()

    async def send_watch(self) -> bool:
        stream = self._stream
        if stream is None:
            return False
        try:
            if self._spade_url is None:
                self._spade_url = await self.get_spade_url()
            stream = self._stream
            if stream is None:
                return False
            async with self._twitch.request(
                "POST", self._spade_url, data=stream.spade_payload
            ) as response:
                if response.status in (401, 403, 404, 410):
                    self._spade_url = None
                if response.status != 204:
                    logger.warning(
                        "Spade watch event rejected for %s: HTTP %s",
                        self._login,
                        response.status,
                    )
                return response.status == 204
        except (ExitRequest, ReloadRequest):
            raise
        except (MinerException, aiohttp.ClientError):
            logger.warning("Spade watch event failed for %s", self._login)
            return False
