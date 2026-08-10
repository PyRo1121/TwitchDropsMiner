from __future__ import annotations

from collections import Counter, abc
from string import Formatter
from typing import Any, Mapping, TypedDict, TYPE_CHECKING, cast

from exceptions import MinerException
from utils import json_load
from constants import LANG_PATH, DEFAULT_LANG

if TYPE_CHECKING:
    from typing_extensions import NotRequired


class StatusMessages(TypedDict):
    terminated: str
    watching: str
    goes_online: str
    goes_offline: str
    claimed_drop: str
    no_channel: str
    no_campaign: str


class LoginMessages(TypedDict):
    unexpected_content: str


class ErrorMessages(TypedDict):
    no_connection: str
    site_down: str


class GUIStatus(TypedDict):
    name: str
    idle: str
    exiting: str
    terminated: str
    cleanup: str
    gathering: str
    switching: str
    fetching_inventory: str
    inventory_retry: str
    fetching_campaigns: str
    adding_campaigns: str


class GUITabs(TypedDict):
    main: str
    inventory: str
    settings: str
    help: str


class GUITray(TypedDict):
    notification_title: str
    minimize: str
    show: str
    quit: str


class GUILoginForm(TypedDict):
    name: str
    logging_in: str
    logged_in: str
    logged_out: str
    button: str


class GUIWebsocket(TypedDict):
    name: str
    websocket: str
    initializing: str
    connected: str
    disconnected: str
    connecting: str
    disconnecting: str
    reconnecting: str


class GUIProgress(TypedDict):
    name: str
    drop: str
    game: str
    campaign: str
    remaining: str
    drop_progress: str
    campaign_progress: str


class GUIChannelHeadings(TypedDict):
    channel: str
    status: str
    game: str
    viewers: str


class GUIChannels(TypedDict):
    name: str
    switch: str
    online: str
    pending: str
    offline: str
    headings: GUIChannelHeadings


class GUIInvFilter(TypedDict):
    name: str
    show: str
    not_linked: str
    upcoming: str
    expired: str
    excluded: str
    finished: str
    refresh: str


class GUIInvStatus(TypedDict):
    linked: str
    not_linked: str
    active: str
    expired: str
    upcoming: str
    claimed: str
    ready_to_claim: str


class GUIInventory(TypedDict):
    filter: GUIInvFilter
    status: GUIInvStatus
    starts: str
    ends: str
    allowed_channels: str
    all_channels: str
    and_more: str
    percent_progress: str
    minutes_progress: str


class GUISettingsGeneral(TypedDict):
    name: str
    autostart: str
    tray: str
    tray_notifications: str
    dark_mode: str
    priority_mode: str
    proxy: str


class GUISettingsAdvanced(TypedDict):
    name: str
    warning: str
    warning_text: str
    enable_badges_emotes: str
    available_drops_check: str
    experimental_dual_watch: str


class GUIPriorityModes(TypedDict):
    priority_only: str
    ending_soonest: str
    low_availability: str


class GUISettings(TypedDict):
    general: GUISettingsGeneral
    advanced: GUISettingsAdvanced
    priority_modes: GUIPriorityModes
    game_name: str
    priority: str
    exclude: str
    reload: str
    reload_text: str


class GUIHelpLinks(TypedDict):
    name: str
    inventory: str
    campaigns: str


class GUIHelpInvalidate(TypedDict):
    button: str
    text: str


class GUIHelp(TypedDict):
    links: GUIHelpLinks
    how_it_works: str
    how_it_works_text: str
    getting_started: str
    getting_started_text: str
    invalidate: GUIHelpInvalidate


class GUIText(TypedDict):
    device_authorization: str
    enter_device_code: str
    device_code: str
    brand_name: str
    brand_subtitle: str
    control_center: str
    deck_title: str
    search_placeholder: str
    overview_kicker: str
    overview_subtitle: str
    starting: str
    status_live: str
    status_error: str
    status_attention: str
    status_error_detail: str
    status_scanning_detail: str
    status_switching_detail: str
    status_idle_detail: str
    status_watching_detail: str
    status_waiting_detail: str
    websocket_waiting: str
    websocket_summary: str
    claims_automatic: str
    jump_anywhere: str
    state_readout: str
    warming_up: str
    quick_steering: str
    keep_moving: str
    overview: str
    history: str
    preferences: str
    help_about: str
    event_log_command: str
    account: str
    campaign_metric: str
    drop_metric: str
    session_watch_metric: str
    live_channels_metric: str
    campaigns_metric: str
    claimed_drops_metric: str
    view_channels: str
    browse_drops: str
    open_preferences: str
    channels_kicker: str
    channels_subtitle: str
    history_kicker: str
    history_title: str
    history_subtitle: str
    inventory_kicker: str
    inventory_title: str
    inventory_subtitle: str
    help_kicker: str
    help_title: str
    help_subtitle: str
    settings_kicker: str
    settings_subtitle: str
    live_target: str
    idle: str
    no_active_campaign: str
    no_active_drop: str
    no_active_watch: str
    game_intel_waiting: str
    game_intel_unavailable: str
    game_intel_no_match: str
    players_playing: str
    price_us: str
    free_to_play: str
    steam_listing_found: str
    steam_intel: str
    logging_level: str
    fatal_error: str
    settings_error: str
    startup_error: str
    art_placeholder: str
    watching_channel: str
    campaign_remaining: str
    drop_remaining: str
    idle_badge: str
    pending_badge: str
    live_badge: str
    offline_badge: str
    drops_badge: str
    watching_badge: str
    open: str
    open_channel: str
    select_channel: str
    monitored: str
    monitored_live: str
    filter_channels: str
    no_channels: str
    no_channels_body: str
    dismiss: str
    latest_signals: str
    event_log: str
    diagnostics: str
    session_title: str
    session_running: str
    session_stopped: str
    session_failed: str
    session_interrupted: str
    session_in_progress: str
    session_summary: str
    clear_completed: str
    no_sessions: str
    no_sessions_body: str
    image_placeholder: str
    link_campaign: str
    claimed_summary: str
    no_campaigns: str
    no_campaigns_body: str
    app_name: str
    app_description: str
    open_repository: str
    open_event_log: str
    add: str
    remove: str
    remove_selected: str
    language_restart: str
    history_retention: str
    days: str
    token_revoke_http: str
    token_revoke_error: str
    startup_saved: str
    startup_failed: str
    reload_requested: str


class GUIMessages(TypedDict):
    output: str
    status: GUIStatus
    tabs: GUITabs
    tray: GUITray
    login: GUILoginForm
    websocket: GUIWebsocket
    progress: GUIProgress
    channels: GUIChannels
    inventory: GUIInventory
    settings: GUISettings
    help: GUIHelp
    text: GUIText


class NotificationMessages(TypedDict):
    auth_required_title: str
    auth_required_message: str
    inventory_failed_title: str
    inventory_failed_message: str
    watch_unavailable_title: str
    watch_unavailable_message: str
    session_failed_title: str
    session_failed_message: str
    campaign_deadline_title: str
    campaign_deadline_message: str
    claim_unconfirmed_title: str
    claim_unconfirmed_message: str
    attention: str


class Translation(TypedDict):
    language_name: NotRequired[str]
    english_name: str
    status: StatusMessages
    login: LoginMessages
    error: ErrorMessages
    notifications: NotificationMessages
    gui: GUIMessages


default_translation: Translation = {
    "english_name": "English",
    "status": {
        "terminated": "\nApplication Terminated.\nClose the window to exit the application.",
        "watching": "Watching: {channel}",
        "goes_online": "{channel} goes ONLINE, switching...",
        "goes_offline": "{channel} goes OFFLINE, switching...",
        "claimed_drop": "Claimed drop: {drop}",
        "no_channel": "No available channels to watch. Waiting for an ONLINE channel...",
        "no_campaign": "No active campaigns to mine drops for. Waiting for an active campaign...",
    },
    "login": {
        "unexpected_content": (
            "Unexpected content type returned, usually due to being redirected. "
            "Do you need to login for internet access?"
        ),
    },
    "error": {
        "site_down": "Twitch is down, retrying in {seconds} seconds...",
        "no_connection": "Cannot connect to Twitch, retrying in {seconds} seconds... ({url})",
    },
    "notifications": {
        "auth_required_title": "Twitch authentication required",
        "auth_required_message": "Open Twitch Drops Miner and sign in to resume farming.",
        "inventory_failed_title": "Inventory refresh failed",
        "inventory_failed_message": "Twitch inventory could not be refreshed; farming state may be stale.",
        "watch_unavailable_title": "No eligible live channel",
        "watch_unavailable_message": "The miner is standing by until an eligible channel becomes available.",
        "session_failed_title": "Miner stopped unexpectedly",
        "session_failed_message": "Open the event log to inspect the failure and restart the miner.",
        "campaign_deadline_title": "Drop campaign ending soon",
        "campaign_deadline_message": "An unfinished campaign is approaching its deadline.",
        "claim_unconfirmed_title": "Drop claim needs attention",
        "claim_unconfirmed_message": "A claim could not be confirmed after retrying; it will be reconciled again.",
        "attention": "Attention: {title} — {message}",
    },
    "gui": {
        "output": "Output",
        "status": {
            "name": "Status",
            "idle": "Idle",
            "exiting": "Exiting...",
            "terminated": "Terminated",
            "cleanup": "Cleaning up channels...",
            "gathering": "Gathering channels...",
            "switching": "Switching the channel...",
            "fetching_inventory": "Fetching inventory...",
            "inventory_retry": "Inventory refresh failed; retrying in {seconds}s...",
            "fetching_campaigns": "Fetching campaigns...",
            "adding_campaigns": "Adding campaigns to inventory... {counter}",
        },
        "tabs": {
            "main": "Main",
            "inventory": "Inventory",
            "settings": "Settings",
            "help": "Help",
        },
        "tray": {
            "notification_title": "Mined Drop",
            "minimize": "Minimize to Tray",
            "show": "Show",
            "quit": "Quit",
        },
        "login": {
            "name": "Login Form",
            "logged_in": "Logged in",
            "logged_out": "Logged out",
            "logging_in": "Logging in...",
            "button": "Login",
        },
        "websocket": {
            "name": "Websocket Status",
            "websocket": "Websocket #{id}:",
            "initializing": "Initializing...",
            "connected": "Connected",
            "disconnected": "Disconnected",
            "connecting": "Connecting...",
            "disconnecting": "Disconnecting...",
            "reconnecting": "Reconnecting...",
        },
        "progress": {
            "name": "Campaign Progress",
            "drop": "Drop:",
            "game": "Game:",
            "campaign": "Campaign:",
            "remaining": "{time} remaining",
            "drop_progress": "Progress:",
            "campaign_progress": "Progress:",
        },
        "channels": {
            "name": "Channels",
            "switch": "Switch",
            "online": "ONLINE  ✔",
            "pending": "OFFLINE ⏳",
            "offline": "OFFLINE ❌",
            "headings": {
                "channel": "Channel",
                "status": "Status",
                "game": "Game",
                "viewers": "Viewers",
            },
        },
        "inventory": {
            "filter": {
                "name": "Filter",
                "show": "Show:",
                "not_linked": "Not linked",
                "upcoming": "Upcoming",
                "expired": "Expired",
                "excluded": "Excluded",
                "finished": "Finished",
                "refresh": "Refresh",
            },
            "status": {
                "linked": "Linked ✔",
                "not_linked": "Not Linked ❌",
                "active": "Active ✔",
                "upcoming": "Upcoming ⏳",
                "expired": "Expired ❌",
                "claimed": "Claimed ✔",
                "ready_to_claim": "Ready to claim ⏳",
            },
            "starts": "Starts: {time}",
            "ends": "Ends: {time}",
            "allowed_channels": "Allowed Channels:",
            "all_channels": "All",
            "and_more": "and {amount} more...",
            "percent_progress": "{percent} of {minutes} minutes",
            "minutes_progress": "{minutes} minutes",
        },
        "settings": {
            "general": {
                "name": "General",
                "autostart": "Autostart: ",
                "tray": "Autostart into tray: ",
                "tray_notifications": "Tray notifications: ",
                "dark_mode": "Dark mode: ",
                "priority_mode": "Priority mode: ",
                "proxy": "Proxy (requires restart):",
            },
            "advanced": {
                "name": "Advanced",
                "warning": "Warning!",
                "warning_text": (
                    "These options will cause the miner to misbehave.\n"
                    "If you're experiencing any issues, "
                    "make sure all of these options are disabled."
                ),
                "enable_badges_emotes": "Enable partial support for badges and emotes: ",
                "available_drops_check": "Enable extra available drops check: ",
                "experimental_dual_watch": (
                    "Experimental dual-target watching (may not earn both Drops): "
                ),
            },
            "priority_modes": {
                "priority_only": "Priority list only",
                "ending_soonest": "Ending soonest",
                "low_availability": "Low availability first",
            },
            "game_name": "Game name",
            "priority": "Priority",
            "exclude": "Exclude",
            "reload": "Reload",
            "reload_text": "Most changes require a reload to take an immediate effect: ",
        },
        "text": {
            "device_authorization": "Authorize this application using Twitch's secure device-code flow.",
            "enter_device_code": "Enter device code: {code}",
            "device_code": "Device code: {code}",
            "brand_name": "TDM",
            "brand_subtitle": "DROP DECK",
            "control_center": "CONTROL CENTER",
            "deck_title": "Drop deck",
            "search_placeholder": "Search  ·  Ctrl K",
            "overview_kicker": "OVERVIEW  /  LIVE CONTROL",
            "overview_subtitle": "The quiet control surface for everything you are farming.",
            "starting": "Starting",
            "status_live": "Live",
            "status_error": "Error",
            "status_attention": "Attention",
            "status_error_detail": "The miner needs attention. Open Event log for the exact request or login detail.",
            "status_scanning_detail": "Scanning campaign inventory and live channels. This usually clears in a few seconds.",
            "status_switching_detail": "Re-evaluating the watch queue so the next minute lands on the best eligible channel.",
            "status_idle_detail": "Nothing eligible is live right now. The miner is standing by and will resume automatically.",
            "status_watching_detail": "A live channel is selected; Twitch watch minutes are being sent automatically.",
            "status_waiting_detail": "The deck is connected and waiting for the next backend signal.",
            "websocket_waiting": "WebSocket: —",
            "websocket_summary": "WS {index}: {status} · {topics} topics",
            "claims_automatic": "Claims run automatically while the miner is live.",
            "jump_anywhere": "Ctrl K  ·  jump anywhere",
            "state_readout": "STATE READOUT",
            "warming_up": "Warming up the drop deck…",
            "quick_steering": "QUICK STEERING",
            "keep_moving": "Keep the miner moving",
            "overview": "Overview",
            "history": "History",
            "preferences": "Preferences",
            "help_about": "{help} & About",
            "event_log_command": "Event log",
            "account": "Account",
            "campaign_metric": "Campaign",
            "drop_metric": "Drop",
            "session_watch_metric": "Session watch",
            "live_channels_metric": "Live channels",
            "campaigns_metric": "Campaigns",
            "claimed_drops_metric": "Claimed drops",
            "view_channels": "View channels",
            "browse_drops": "Browse drops",
            "open_preferences": "Open preferences",
            "channels_kicker": "CHANNELS / WATCH LIST",
            "channels_subtitle": "Choose what the miner should watch next.",
            "history_kicker": "HISTORY / RUN LEDGER",
            "history_title": "Session history",
            "history_subtitle": "A local record of meaningful farming sessions and outcomes.",
            "inventory_kicker": "DROPS / REWARD TRACKER",
            "inventory_title": "Drops",
            "inventory_subtitle": "Campaigns are sorted by what can be claimed next.",
            "help_kicker": "HELP / FIELD GUIDE",
            "help_title": "{help} & About",
            "help_subtitle": "Short answers for keeping the miner on course.",
            "settings_kicker": "SETTINGS / CONTROL SURFACE",
            "settings_subtitle": "Tune the miner without leaving the deck.",
            "live_target": "LIVE TARGET  /  FARMING NOW",
            "idle": "Idle",
            "no_active_campaign": "No active campaign",
            "no_active_drop": "No active drop",
            "no_active_watch": "NO ACTIVE WATCH",
            "game_intel_waiting": "GAME INTEL  ·  waiting for a target",
            "game_intel_unavailable": "GAME INTEL  ·  Steam signal unavailable  ·  search links ready",
            "game_intel_no_match": "GAME INTEL  ·  no exact Steam match  ·  search links ready",
            "players_playing": "{players} playing",
            "price_us": "{price} US",
            "free_to_play": "Free to play",
            "steam_listing_found": "Steam listing found",
            "steam_intel": "STEAM INTEL  ·  {details}",
            "logging_level": "Logging level: {level}",
            "fatal_error": "Fatal error encountered:\n",
            "settings_error": "Settings error",
            "startup_error": "Startup error",
            "art_placeholder": "ART",
            "watching_channel": "WATCHING  /  @{name}{suffix}",
            "campaign_remaining": "Campaign remaining: {time}",
            "drop_remaining": "Drop remaining: {time}",
            "idle_badge": "IDLE",
            "pending_badge": "ONLINE?",
            "live_badge": "LIVE",
            "offline_badge": "OFFLINE",
            "drops_badge": "DROPS",
            "watching_badge": "WATCHING",
            "open": "OPEN",
            "open_channel": "Open {channel} on Twitch",
            "select_channel": "Watch {channel}",
            "monitored": "{count} monitored",
            "monitored_live": "{count} monitored  ·  {live} live",
            "filter_channels": "Filter channels  ·  name or game",
            "no_channels": "No channels yet",
            "no_channels_body": "The miner will populate monitored channels here.",
            "dismiss": "Dismiss",
            "latest_signals": "LATEST SIGNALS",
            "event_log": "Event log",
            "diagnostics": "diagnostics",
            "session_title": "Session · {time}",
            "session_running": "RUNNING",
            "session_stopped": "STOPPED",
            "session_failed": "FAILED",
            "session_interrupted": "INTERRUPTED",
            "session_in_progress": "in progress",
            "session_summary": "{start} → {end} · {duration} · {claims} claims · {syncs} inventory syncs",
            "clear_completed": "Clear completed",
            "no_sessions": "No sessions recorded",
            "no_sessions_body": "Session history will appear after the miner starts.",
            "image_placeholder": "IMAGE",
            "link_campaign": "Link this campaign on Twitch",
            "claimed_summary": "{progress} · {claimed}/{total} claimed",
            "no_campaigns": "No campaigns",
            "no_campaigns_body": "Inventory will appear after Twitch data is loaded.",
            "app_name": "Twitch Drops Miner",
            "app_description": "A background miner for Twitch timed Drops.",
            "open_repository": "Open project repository",
            "open_event_log": "Open event log",
            "add": "Add",
            "remove": "Remove",
            "remove_selected": "Remove selected",
            "language_restart": "Language (restart required)",
            "history_retention": "History retention",
            "days": "{days} days",
            "token_revoke_http": "Token revoke failed: HTTP {status}",
            "token_revoke_error": "Token revoke failed: {error}",
            "startup_saved": "Startup setting saved.",
            "startup_failed": "Startup setting failed: {error}",
            "reload_requested": "Campaign reload requested.",
        },
        "help": {
            "links": {
                "name": "Useful Links",
                "inventory": "See Twitch inventory",
                "campaigns": "See all campaigns and manage account links",
            },
            "how_it_works": "How It Works",
            "how_it_works_text": (
                "TwitchDropsMiner advances Drops by checking stream metadata; "
                "it does not download or play video or audio. "
                "When a campaign is active, it keeps your selected channel and account "
                "ready so progress can move."
            ),
            "getting_started": "Getting Started",
            "getting_started_text": (
                "1. Sign in to Twitch.\n"
                "2. Link the campaigns you want from Twitch's Drops pages.\n"
                "3. Choose a priority mode, then add games to Priority or Exclude if needed.\n"
                "4. Press Reload after changing priorities or exclusions.\n"
                "5. Return to the Overview and leave the miner running while it farms."
            ),
            "invalidate": {
                "button": "Invalidate",
                "text": "Invalidate the authentication token (log out):",
            },
        },
    },
}


_TranslationPath = tuple[str, ...]
_FormatSignature = Counter[tuple[str, str | None, str | None]]
_FORMATTER = Formatter()


def _format_signature(template: str) -> _FormatSignature:
    try:
        return Counter(
            (field_name, conversion, format_spec)
            for _, field_name, format_spec, conversion in _FORMATTER.parse(template)
            if field_name is not None
        )
    except ValueError as exc:
        raise ValueError(f"invalid format string: {exc}") from exc


def _validate_translation_catalog(
    translation: Mapping[str, Any],
    default: Mapping[str, Any] = default_translation,
    *,
    language: str,
    path: _TranslationPath = (),
) -> None:
    """Require a locale catalog to match the English schema exactly."""
    location = ".".join(path) or "<root>"
    missing = sorted(default.keys() - translation.keys())
    unknown = sorted(translation.keys() - default.keys())
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        raise ValueError(f"{language}:{location}: {'; '.join(details)}")

    for key, default_value in default.items():
        value = translation[key]
        item_path = (*path, key)
        item_location = f"{language}:{'.'.join(item_path)}"
        if isinstance(default_value, Mapping):
            if not isinstance(value, Mapping):
                raise ValueError(f"{item_location}: expected an object")
            _validate_translation_catalog(
                value,
                default_value,
                language=language,
                path=item_path,
            )
            continue
        if not isinstance(value, str):
            raise ValueError(f"{item_location}: expected a string")
        if not value.strip():
            raise ValueError(f"{item_location}: translation cannot be empty")
        try:
            default_signature = _format_signature(default_value)
            translated_signature = _format_signature(value)
        except ValueError as exc:
            raise ValueError(f"{item_location}: {exc}") from exc
        if translated_signature != default_signature:
            raise ValueError(
                f"{item_location}: placeholders do not match the English schema"
            )


class Translator:
    def __init__(self) -> None:
        self._langs: list[str] = []
        # start with (and always copy) the default translation
        self._translation: Translation = self._new_default()
        self._translation["language_name"] = DEFAULT_LANG
        # load available translation names
        for filepath in LANG_PATH.glob("*.json"):
            self._langs.append(filepath.stem)
        self._langs.sort()
        if DEFAULT_LANG in self._langs:
            self._langs.remove(DEFAULT_LANG)
        self._langs.insert(0, DEFAULT_LANG)

    def _new_default(self) -> Translation:
        return default_translation.copy()

    def _lang_name(self) -> str:
        return cast(dict[str, Any], self._translation).get("language_name", DEFAULT_LANG)

    @property
    def languages(self) -> abc.Iterable[str]:
        return iter(self._langs)

    @property
    def current(self) -> str:
        return self._lang_name()

    def set_language(self, language: str) -> None:
        if language not in self._langs:
            raise ValueError("Unrecognized language")
        elif self._lang_name() == language:
            # same language as loaded selected
            return
        elif language == DEFAULT_LANG:
            # default language selected - use the memory value
            self._translation = self._new_default()
        else:
            loaded = json_load(
                LANG_PATH.joinpath(f"{language}.json"),
                default_translation,
                merge=False,
            )
            if "language_name" in loaded:
                raise ValueError("Translations cannot define 'language_name'")
            _validate_translation_catalog(loaded, language=language)
            self._translation = loaded
        self._translation["language_name"] = language

    def __call__(self, *path: str) -> str:
        if not path:
            raise ValueError("Language path expected")
        v: Any = self._translation
        try:
            for key in path:
                v = v[key]
        except KeyError:
            # this can only really happen for the default translation
            raise MinerException(
                f"{self.current} translation is missing the '{' -> '.join(path)}' translation key"
            )
        return v


_ = Translator()
