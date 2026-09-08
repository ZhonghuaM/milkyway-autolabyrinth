import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import milkyway_autolabyrinth as runner
from labyrinth_events import LabyrinthWebSocketWatcher


class CycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_escape_and_reenter_in_one_pass(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner, "goto_labyrinth_panel", new=AsyncMock()))
            stack.enter_context(patch.object(runner, "detect_labyrinth_state", new=AsyncMock(
                side_effect=[runner.LabyrinthState("needs_escape"), runner.LabyrinthState("entry")],
            )))
            stack.enter_context(patch.object(runner, "read_ticket_count", new=AsyncMock(
                return_value=runner.TicketState(2, 5),
            )))
            escape = stack.enter_context(patch.object(runner, "escape_labyrinth", new=AsyncMock(
                return_value=(True, "escaped"),
            )))
            enter = stack.enter_context(patch.object(runner, "enter_labyrinth", new=AsyncMock(
                return_value=(True, "started", 1),
            )))
            result = await runner.run_one_cycle(Mock(), runner.RunSettings())
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "entered_labyrinth")
        escape.assert_awaited_once()
        enter.assert_awaited_once()

    async def test_claim_and_reenter_in_one_pass(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner, "goto_labyrinth_panel", new=AsyncMock()))
            stack.enter_context(patch.object(runner, "detect_labyrinth_state", new=AsyncMock(
                side_effect=[runner.LabyrinthState("finished"), runner.LabyrinthState("entry")],
            )))
            stack.enter_context(patch.object(runner, "read_ticket_count", new=AsyncMock(
                return_value=runner.TicketState(2, 5),
            )))
            stack.enter_context(patch.object(runner, "collect_labyrinth_result", new=AsyncMock(
                return_value=(True, "claimed"),
            )))
            enter = stack.enter_context(patch.object(runner, "enter_labyrinth", new=AsyncMock(
                return_value=(True, "started", 1),
            )))
            result = await runner.run_one_cycle(Mock(), runner.RunSettings())
        self.assertEqual(result.mode, "entered_labyrinth")
        enter.assert_awaited_once()

    async def test_unknown_state_never_attempts_entry(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner, "goto_labyrinth_panel", new=AsyncMock()))
            stack.enter_context(patch.object(runner, "detect_labyrinth_state", new=AsyncMock(
                return_value=runner.LabyrinthState("unknown"),
            )))
            stack.enter_context(patch.object(runner, "read_ticket_count", new=AsyncMock(
                return_value=runner.TicketState(),
            )))
            enter = stack.enter_context(patch.object(runner, "enter_labyrinth", new=AsyncMock()))
            result = await runner.run_one_cycle(Mock(), runner.RunSettings())
        self.assertFalse(result.ok)
        enter.assert_not_awaited()


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def mocks(self, stack, initialization_errors=0):
        page = Mock()
        page.is_closed.return_value = False
        context = Mock()
        browser = SimpleNamespace(new_context=AsyncMock(), close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        watcher = LabyrinthWebSocketWatcher()
        stack.enter_context(patch.object(runner, "LabyrinthWebSocketWatcher", return_value=watcher))
        init = stack.enter_context(patch.object(runner, "new_context_for_account", new=AsyncMock(
            side_effect=[TimeoutError("slow startup")] * initialization_errors + [(context, page)],
        )))
        stack.enter_context(patch.object(runner, "safe_close_context", new=AsyncMock()))
        stack.enter_context(patch.object(runner, "safe_close_browser", new=AsyncMock()))
        stack.enter_context(patch.object(runner, "detect_labyrinth_state", new=AsyncMock(
            return_value=runner.LabyrinthState("in_labyrinth"),
        )))
        stack.enter_context(patch.object(runner, "get_current_floor", new=AsyncMock(return_value=1)))
        cycle = stack.enter_context(patch.object(runner, "run_one_cycle", new=AsyncMock(
            return_value=runner.CycleResult(ok=True, mode="already_in_labyrinth", floor=1),
        )))
        return playwright, watcher, init, cycle

    async def test_worker_runs_again_when_completion_wakes_wait(self):
        with tempfile.TemporaryDirectory() as debug, ExitStack() as stack:
            playwright, watcher, init, cycle = self.mocks(stack)
            async def finish_round(timeout_sec):
                self.assertEqual(timeout_sec, 600)
                self.assertFalse(watcher.maintenance_active)
                watcher.feed_frame({
                    "type": "action_completed",
                    "endCharacterAction": {
                        "actionHrid": "/actions/labyrinth/explore", "isDone": True,
                    },
                })
                await LabyrinthWebSocketWatcher.wait(watcher, timeout_sec)
            wait = stack.enter_context(patch.object(watcher, "wait", new=AsyncMock(side_effect=finish_round)))
            summary = await runner.account_worker(
                playwright, runner.AccountConfig("example", "https://test.milkywayidle.com/game", "unused.json"),
                runner.RunSettings(loops=2, debug_dir=debug), SimpleNamespace(write_row=AsyncMock()),
            )
        self.assertEqual(summary.ok, 2)
        self.assertEqual(cycle.await_count, 2)
        self.assertEqual(init.await_count, 1)
        wait.assert_awaited_once()
        self.assertFalse(watcher.event.is_set())

    async def test_initialization_timeout_retries_without_killing_worker(self):
        with tempfile.TemporaryDirectory() as debug, ExitStack() as stack:
            playwright, watcher, init, cycle = self.mocks(stack, initialization_errors=1)
            sleep = stack.enter_context(patch.object(runner.asyncio, "sleep", new=AsyncMock()))
            summary = await runner.account_worker(
                playwright, runner.AccountConfig("example", "https://test.milkywayidle.com/game", "unused.json"),
                runner.RunSettings(loops=2, debug_dir=debug), SimpleNamespace(write_row=AsyncMock()),
            )
        self.assertEqual(init.await_count, 2)
        self.assertEqual(cycle.await_count, 1)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.ok, 1)
        sleep.assert_awaited_once_with(20.0)
        self.assertFalse(watcher.maintenance_active)
