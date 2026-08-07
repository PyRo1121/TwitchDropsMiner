"""Offscreen render harness for the Qt UI — builds QtGUIManager with mock
backend data and screenshots every page. NOT a source file of the app.

Run from the repo root with the venv:
    QT_QPA_PLATFORM=offscreen python render_preview.py
"""
import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication
from constants import PriorityMode

app = QApplication([])

from gui_qt.manager import QtGUIManager


class MockGame:
    def __init__(self, name):
        self.name = name
        self.id = abs(hash(name)) % 1000000

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, MockGame) and other.id == self.id

    def is_special(self):
        return False


def game(name):
    return MockGame(name)


class MockSettings:
    language = "English"
    dark_mode = True
    tray = False
    tray_notifications = True
    autostart_tray = False
    proxy = ""
    enable_badges_emotes = True
    available_drops_check = False
    priority = ["Eternights", "Honkai: Star Rail"]
    exclude = ["Counter-Strike 2"]
    priority_mode = PriorityMode.ENDING_SOONEST
    logging_level = logging.ERROR


class MockTwitch:
    settings = MockSettings()

    def change_state(self, state):
        print("  [mock] change_state:", state)

    def state_change(self, state):
        return lambda: self.change_state(state)

    def close(self):
        print("  [mock] close()")


def channel(id_, name, online, game_, viewers, drops=True, acl=False, pending=False):
    return SimpleNamespace(
        id=id_, name=name, iid=str(id_), login=name.lower(),
        url=f"https://www.twitch.tv/{name.lower()}",
        online=online, offline=not online and not pending, pending_online=pending,
        game=game_ if online else None, viewers=viewers if online else None,
        drops_enabled=drops, acl_based=acl,
    )


def drop(progress, reward, minutes, campaign):
    return SimpleNamespace(
        id=f"drop-{abs(hash(reward))}",
        name=reward,
        progress=progress,
        remaining_minutes=minutes,
        current_minutes=round(progress * max(minutes, 1)),
        required_minutes=max(minutes, 1),
        rewards_text=lambda: reward,
        campaign=campaign,
        benefits=[],
        is_claimed=False,
        can_claim=False,
        can_earn=lambda: True,
        starts_at=datetime.now(timezone.utc) - timedelta(hours=1),
        ends_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )


def campaign(name_, gname, progress_, claimed, total, active=True,
             upcoming=False, finished=False, minutes=90, drops=None):
    now = datetime.now(timezone.utc)
    c = SimpleNamespace(
        id=f"campaign-{abs(hash(name_))}",
        name=name_, game=game(gname),
        progress=progress_, claimed_drops=claimed, total_drops=total,
        active=active, upcoming=upcoming, expired=not active and not upcoming,
        finished=finished, remaining_minutes=minutes, drops=drops or [],
        required_minutes=max((d.required_minutes for d in (drops or [])), default=1),
        eligible=True, linked=True,
        link_url="https://www.twitch.tv/drops/inventory",
        image_url="https://static-cdn.jtvnw.net/ttv-boxart/123-285x380.jpg",
        starts_at=now + timedelta(hours=1) if upcoming else now - timedelta(hours=1),
        ends_at=now + timedelta(hours=3),
        allowed_channels=[],
    )
    for item in c.drops:
        item.campaign = c
    return c


async def _run(twitch):
    m = QtGUIManager(twitch)
    app.processEvents()

    try:
        os.makedirs("/tmp/tdm_shots", exist_ok=True)
    except OSError:
        pass

    # --- Overview hero + status ---
    m.status.update("Watching Eternights — drops active")
    m.websockets.update(0, "connected", 199)
    camp = campaign("Eternights Drops", "Eternights", 0.62, 3, 5, minutes=92,
                    drops=[drop(0.4, "Darial Earrings", 40, None),
                           drop(0.62, "Nia Idol", 25, None)])
    d = drop(0.58, "Journal + Artbook", 41, camp)
    m.display_drop(cast(Any, d))
    m.print("Drop farming is live — watching @EternightsGame")
    m.print("Logged in, user ID 123456789")
    m.set_games(cast(Any, {game("Eternights"), game("Honkai: Star Rail"), game("Persona 5")}))
    await asyncio.sleep(0)
    app.processEvents()
    _shot(m, "/tmp/tdm_shots/01-overview.png")

    # --- Channels page ---
    chans = [
        channel(1, "EternightsGame", True, game("Eternights"), 1280),
        channel(2, "OfficialStarRail", True, game("Honkai: Star Rail"), 890, acl=True),
        channel(3, "RetroGaming", True, game("Eternights"), 320),
        channel(4, "OfflineChannel", False, None, None),
    ]
    for c in chans:
        m.channels.display(cast(Any, c), add=True)
    m.channels.set_watching(cast(Any, chans[0]))
    m._navigate("channels")
    app.processEvents()
    _shot(m, "/tmp/tdm_shots/02-channels.png")

    # --- Drops page (campaign cards) ---
    m.inv.clear()
    await m.inv.add_campaign(cast(Any,
        campaign("Eternights Drops", "Eternights", 0.62, 3, 5, minutes=40,
                 drops=[drop(0.5, "Darial Earrings", 30, None),
                        drop(1.0, "Nia Idol", 0, None)])))
    await m.inv.add_campaign(cast(Any,
        campaign("Persona 3 Reload Drops", "Persona 3 Reload", 0.15, 0, 4,
                 active=False, upcoming=True, minutes=220,
                 drops=[drop(0.15, "Aegis Costume", 200, None)])))
    m._navigate("drops")
    app.processEvents()
    _shot(m, "/tmp/tdm_shots/03-drops.png")

    # --- Settings ---
    m._navigate("settings")
    app.processEvents()
    _shot(m, "/tmp/tdm_shots/04-settings.png")

    # --- Activity ---
    for i in range(8):
        m.print(f"[12:0{i}] Drop claimed: +reward #{i} · Eternights")
    m._navigate("activity")
    app.processEvents()
    _shot(m, "/tmp/tdm_shots/05-activity.png")

    # --- Help ---
    m._navigate("help")
    app.processEvents()
    _shot(m, "/tmp/tdm_shots/06-help.png")

    # --- Login prompt ---
    m._navigate("overview")
    m.progress.display(None)
    m.login_panel.set_status("Sign in to start farming", None)
    app.processEvents()
    _shot(m, "/tmp/tdm_shots/07-login.png")

    print("Rendered 7 screenshots -> /tmp/tdm_shots/")
    m.close_window()
    app.quit()


def main():
    import asyncio
    asyncio.run(_run(MockTwitch()))


def _shot(widget, path):
    pm = widget.grab()
    ok = pm.save(path, "PNG")
    print(f"  saved {path}: {ok}")


if __name__ == "__main__":
    main()
