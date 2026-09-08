import asyncio
import json
import unittest

from labyrinth_events import LABYRINTH_EXPLORE_ACTION_HRID, LabyrinthWebSocketWatcher


def completion(batch=False):
    action = {"actionHrid": LABYRINTH_EXPLORE_ACTION_HRID, "isDone": True}
    return (
        {"type": "actions_updated", "endCharacterActions": [action]}
        if batch else {"type": "action_completed", "endCharacterAction": action}
    )


class Emitter:
    def __init__(self, url="wss://api-test.milkywayidle.com/ws"):
        self.url = url
        self.handlers = {}

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event, *args):
        for callback in self.handlers.get(event, []):
            callback(*args)


class EventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.watcher = LabyrinthWebSocketWatcher()

    def test_completion_coalesces_until_next_run(self):
        self.watcher.feed_frame(json.dumps(completion()).encode())
        self.assertIn("action_completed", self.watcher.consume_reason())
        self.watcher.feed_frame(completion(batch=True))
        self.assertFalse(self.watcher.event.is_set())
        self.watcher.feed_frame({
            "type": "action_started", "actionHrid": LABYRINTH_EXPLORE_ACTION_HRID,
        })
        self.watcher.feed_frame(completion(batch=True))
        self.assertIn("actions_updated", self.watcher.consume_reason())

    def test_inactive_transition_and_maintenance_suppression(self):
        self.watcher.feed_frame({"type": "labyrinth_updated", "labyrinth": {"isActive": False}})
        self.assertFalse(self.watcher.event.is_set())
        self.watcher.feed_frame({"type": "labyrinth_room_progress"})
        self.watcher.feed_frame({"type": "labyrinth_updated", "labyrinth": {"isActive": False}})
        self.assertIn("isActive=false", self.watcher.consume_reason())
        self.watcher.maintenance_active = True
        self.watcher.feed_frame({"type": "labyrinth_room_progress"})
        self.watcher.feed_frame({"type": "labyrinth_updated", "labyrinth": {"isActive": False}})
        self.watcher.feed_frame(completion())
        self.assertFalse(self.watcher.event.is_set())

    def test_malformed_incomplete_and_other_action_packets(self):
        for packet in ("invalid", b"\xff", "[]", None, {"type": "actions_updated", "endCharacterActions": None}):
            self.watcher.feed_frame(packet)
        for action in (
            {"actionHrid": "/actions/example", "isDone": True},
            {"actionHrid": LABYRINTH_EXPLORE_ACTION_HRID, "isDone": False},
        ):
            self.watcher.feed_frame({"type": "action_completed", "endCharacterAction": action})
        self.assertFalse(self.watcher.event.is_set())

    async def test_wait_wakes_on_a_completion_and_times_out_without_one(self):
        task = asyncio.create_task(self.watcher.wait(600))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.watcher.feed_frame(completion())
        await asyncio.wait_for(task, timeout=1)
        self.watcher.consume_reason()
        await asyncio.wait_for(self.watcher.wait(0.01), timeout=1)
        self.assertFalse(self.watcher.event.is_set())

    def test_page_replacement_ignores_old_socket_and_reconnects(self):
        page, socket = Emitter(), Emitter()
        self.watcher.install_page(page)
        self.watcher.install_page(page)
        self.assertEqual(len(page.handlers["websocket"]), 1)
        page.emit("websocket", socket)
        self.watcher.reset_connection_state()
        socket.emit("framereceived", completion())
        socket.emit("close")
        self.assertFalse(self.watcher.event.is_set())
        new_page, new_socket = Emitter(), Emitter()
        self.watcher.install_page(new_page)
        new_page.emit("websocket", new_socket)
        new_socket.emit("framereceived", completion())
        self.assertTrue(self.watcher.event.is_set())
        self.watcher.consume_reason()
        new_socket.emit("close")
        self.assertIn("closed", self.watcher.consume_reason())

    def test_unrelated_websocket_is_not_observed(self):
        page, socket = Emitter(), Emitter("wss://example.invalid/ws")
        self.watcher.install_page(page)
        page.emit("websocket", socket)
        self.assertEqual(socket.handlers, {})
