"""Twitch game model used across the backend and the Qt dashboard.

Kept as its own small module so generic helpers in ``utils.py`` stay free of
domain state and callers can import ``Game`` eagerly without pulling in utils.
"""
from __future__ import annotations

from constants import JsonType
from utils import slugify


class Game:
    SPECIAL_GAME_IDS: set[int] = {509663, 509672}

    def __init__(self, data: JsonType):
        try:
            raw_id = data["id"]
        except KeyError as exc:
            raise ValueError("Game data must contain an integer id") from exc
        if type(raw_id) is int:
            game_id = raw_id
        elif (
            isinstance(raw_id, str)
            and raw_id.isascii()
            and raw_id.isdecimal()
        ):
            try:
                game_id = int(raw_id)
            except ValueError as exc:
                raise ValueError(
                    "Game data must contain a bounded integer id"
                ) from exc
        else:
            raise ValueError("Game data must contain an integer id")
        if game_id < 0:
            raise ValueError("Game id cannot be negative")
        self.id = game_id
        name = data.get("displayName") or data.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Game data must contain a name")
        self.name: str = name
        slug = data.get("slug")
        self.slug: str = slug if isinstance(slug, str) and slug else slugify(name)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Game({self.id}, {self.name})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, self.__class__):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return self.id

    def is_special(self) -> bool:
        return self.id in self.SPECIAL_GAME_IDS
