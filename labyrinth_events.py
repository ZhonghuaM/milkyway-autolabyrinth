"""Passive labyrinth notifications from the existing browser game connection."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakSet

LABYRINTH_EXPLORE_ACTION_HRID = "/actions/labyrinth/explore"


class LabyrinthWebSocketWatcher:
    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.reason = ""
        self.maintenance_active = False
        self._active: bool | None = None
        self._completion_seen = False
        self._generation = 0
        self._pages: WeakSet = WeakSet()

    def install_page(self, page: Any) -> None:
        if page in self._pages:
            return
        self._pages.add(page)
        generation = self._generation

        def on_websocket(ws: Any) -> None:
            host = (urlsplit(ws.url).hostname or "").lower()
            if not any(host == domain or host.endswith("." + domain)
                       for domain in ("milkywayidle.com", "milkywayidlecn.com")):
                return

            def on_frame(payload: Any) -> None:
                if generation == self._generation:
                    self.feed_frame(payload)

            def on_close(*_: Any) -> None:
                if generation == self._generation and not self.maintenance_active:
                    self._signal("game websocket closed")

            ws.on("framereceived", on_frame)
            ws.on("close", on_close)

        page.on("websocket", on_websocket)

    def reset_connection_state(self) -> None:
        # Frames still arriving from a discarded page must not wake the new one.
        self._generation += 1
        self._pages.clear()
        self._active = None
        self._completion_seen = False
        self.consume_reason()

    def consume_reason(self) -> str:
        reason = self.reason
        self.event.clear()
        self.reason = ""
        return reason

    def _signal(self, reason: str) -> None:
        if not self.event.is_set():
            self.reason = reason
            self.event.set()

    async def wait(self, timeout_sec: float) -> None:
        try:
            await asyncio.wait_for(self.event.wait(), timeout=max(0.01, timeout_sec))
        except asyncio.TimeoutError:
            pass

    def feed_frame(self, payload: Any) -> None:
        try:
            packet = payload if isinstance(payload, dict) else json.loads(payload)
        except (TypeError, ValueError, UnicodeDecodeError):
            return
        if not isinstance(packet, dict):
            return
        message_type = packet.get("type")
        if message_type in {"init_character_data", "labyrinth_updated"}:
            labyrinth = packet.get("labyrinth")
            if message_type == "init_character_data":
                labyrinth = packet.get("characterLabyrinth", labyrinth)
            if isinstance(labyrinth, dict) and isinstance(labyrinth.get("isActive"), bool):
                was_active = self._active
                self._active = labyrinth["isActive"]
                if self._active and was_active is not True:
                    self._completion_seen = False
                if (message_type == "labyrinth_updated" and was_active is True
                        and not self._active and not self.maintenance_active):
                    self._signal("labyrinth_updated isActive=false")
            return
        if message_type == "labyrinth_room_progress":
            self._active = True
            return
        if message_type == "action_started":
            if packet.get("actionHrid") == LABYRINTH_EXPLORE_ACTION_HRID:
                self._active = True
                self._completion_seen = False
            return
        if message_type == "action_completed":
            actions = [packet.get("endCharacterAction")]
        elif message_type == "actions_updated":
            actions = packet.get("endCharacterActions")
        else:
            return
        if not isinstance(actions, list):
            return
        completed = any(
            isinstance(action, dict)
            and action.get("actionHrid") == LABYRINTH_EXPLORE_ACTION_HRID
            and action.get("isDone") is True
            for action in actions
        )
        if completed and not self._completion_seen:
            self._completion_seen = True
            if not self.maintenance_active:
                self._signal(f"labyrinth explore {message_type} isDone=true")
