#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import csv
import gc
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Route,
    async_playwright,
)

LOG = logging.getLogger("milkyway_autolabyrinth")


# ---------- Data models ----------

@dataclass(slots=True)
class AccountConfig:
    name: str
    game_url: str
    state_file: str
    action_bias_ms: int = 0


@dataclass(slots=True)
class RunSettings:
    loops: int = 0                # 0 = forever
    delay_sec: float = 20.0
    refresh_every: int = 40
    recycle_context_every: int = 200
    low_ticket_threshold: int = 0
    navigation_timeout_ms: int = 15000
    results_csv: str = "auto_labyrinth_results.csv"
    debug_dir: str = "autolabyrinth_debug"
    log_every: int = 10
    browser_channel: str | None = None
    headless: bool = True
    save_failure_screenshots: bool = False
    block_assets: bool = True


@dataclass(slots=True)
class TicketState:
    current: int | None = None
    max: int | None = None
    raw_text: str = ""


@dataclass(slots=True)
class LabyrinthState:
    state: str = "unknown"  # entry | in_labyrinth | finished | unknown
    detail: str = ""


@dataclass(slots=True)
class WorkerSummary:
    account: str
    total: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(slots=True)
class CycleResult:
    ok: bool
    mode: str
    error: str = ""
    detail: str = ""
    tickets_before: int | None = None
    tickets_after: int | None = None
    tickets_max: int | None = None
    floor: int | None = None


# ---------- Patterns ----------

ENTRY_COUNT_PATTERNS = [
    re.compile(r"(\d+)\s*/\s*(\d+)\s*Entries", re.I),
    re.compile(r"Entries\s*[:：]?\s*(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"入场券\s*[:：]?\s*(\d+)\s*/\s*(\d+)", re.I),
    re.compile(r"Tickets?\s*[:：]?\s*(\d+)\s*/\s*(\d+)", re.I),
]

FLOOR_PATTERN = re.compile(r"\bFloor\s+(\d+)\s*\(Treasure:\s*(\d+\s*/\s*\d+)\)", re.I)
ENTRY_DIALOG_PATTERN = re.compile(r"maximum allowed supplies.*crates|without the maximum allowed supplies and crates", re.I)
AUTH_SIGNALS = [
    re.compile(r"\blogin\b", re.I),
    re.compile(r"\bsign in\b", re.I),
    re.compile(r"\blog in\b", re.I),
    re.compile(r"connect wallet", re.I),
    re.compile(r"continue with google|continue with discord|walletconnect", re.I),
]


# ---------- Utilities ----------

class CsvSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._header_written = self.path.exists() and self.path.stat().st_size > 0

    async def write_row(self, row: dict[str, Any]) -> None:
        async with self._lock:
            fieldnames = list(row.keys())
            with self.path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if not self._header_written:
                    writer.writeheader()
                    self._header_written = True
                writer.writerow(row)


def normalize_space(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def load_accounts(accounts_path: str | Path) -> list[AccountConfig]:
    data = json.loads(Path(accounts_path).read_text(encoding="utf-8"))
    raw = data.get("accounts", []) if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError("accounts file must contain a non-empty list of accounts")
    accounts: list[AccountConfig] = []
    for item in raw:
        accounts.append(
            AccountConfig(
                name=item["name"],
                game_url=item["game_url"],
                state_file=item["state_file"],
                action_bias_ms=int(item.get("action_bias_ms", 0)),
            )
        )
    return accounts


def filter_accounts(accounts: list[AccountConfig], wanted_name: str | None) -> list[AccountConfig]:
    if not wanted_name:
        return accounts
    filtered = [a for a in accounts if a.name == wanted_name]
    if not filtered:
        raise ValueError(f"account '{wanted_name}' not found in accounts file")
    return filtered


async def locator_text(locator: Locator | None, limit: int = 4000) -> str:
    if locator is None:
        return ""
    try:
        txt = await locator.inner_text(timeout=1200)
    except Exception:
        try:
            txt = await locator.text_content(timeout=1200)
        except Exception:
            txt = ""
    return normalize_space(txt)[:limit]


async def page_body_text(page: Page, limit: int = 5000) -> str:
    try:
        txt = await page.locator("body").inner_text(timeout=1500)
    except Exception:
        txt = ""
    return normalize_space(txt)[:limit]


async def locator_is_visible(locator: Locator | None) -> bool:
    if locator is None:
        return False
    try:
        return await locator.is_visible()
    except Exception:
        return False


async def locator_is_enabled(locator: Locator | None) -> bool:
    if locator is None:
        return False
    try:
        return await locator.is_enabled()
    except Exception:
        return False


async def first_visible_locator(locators: Iterable[Locator], timeout: float = 4.0) -> Locator | None:
    end = time.time() + timeout
    while time.time() < end:
        for loc in locators:
            try:
                cand = loc.first
                if await cand.count() > 0 and await cand.is_visible():
                    return cand
            except Exception:
                pass
        await asyncio.sleep(0.12)
    return None


async def first_visible_enabled_locator(locators: Iterable[Locator], timeout: float = 4.0) -> Locator | None:
    end = time.time() + timeout
    fallback: Locator | None = None
    while time.time() < end:
        for loc in locators:
            try:
                cand = loc.first
                if await cand.count() == 0 or not await cand.is_visible():
                    continue
                if await locator_is_enabled(cand):
                    return cand
                if fallback is None:
                    fallback = cand
            except Exception:
                pass
        await asyncio.sleep(0.12)
    return fallback


def as_click_target(locator: Locator) -> Locator:
    return locator.locator(
        "xpath=ancestor-or-self::*[self::button or self::a or @role='button' or contains(@class, 'NavigationBar_navigationLink')][1]"
    ).first


def button_candidates(scope: Page | Locator, names: list[str]) -> list[Locator]:
    out: list[Locator] = []
    for name in names:
        out.extend([
            scope.get_by_role("button", name=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)).first,
            scope.get_by_text(re.compile(rf"^\s*{re.escape(name)}\s*$", re.I)).first,
        ])
    return out


def nav_candidates(page: Page, aria_label: str, names: list[str]) -> list[Locator]:
    cands: list[Locator] = []
    icon = page.locator(f'[aria-label="{aria_label}"]').first
    cands.extend([icon, as_click_target(icon)])
    nav = page.locator('div[class*="NavigationBar_navigationLink"]')
    for name in names:
        text_loc = nav.filter(has_text=re.compile(rf"\b{re.escape(name)}\b", re.I)).first
        cands.extend([text_loc, as_click_target(text_loc)])
    return cands


async def block_static_assets(route: Route) -> None:
    if route.request.resource_type in {"image", "font", "media"}:
        await route.abort()
    else:
        await route.continue_()


async def take_failure_screenshot(page: Page | None, path: Path) -> None:
    if page is None:
        return
    try:
        if not page.is_closed():
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


async def safe_close_context(context: BrowserContext | None, reason: str = "cleanup") -> None:
    if context is None:
        return
    try:
        await context.close(reason=reason)
    except Exception as e:
        msg = str(e)
        if "Connection closed" not in msg and "Target page, context or browser has been closed" not in msg:
            LOG.warning("context close warning: %s", e)


async def safe_close_browser(browser: Browser | None, reason: str = "cleanup") -> None:
    if browser is None:
        return
    try:
        if browser.is_connected():
            await browser.close(reason=reason)
    except Exception as e:
        msg = str(e)
        if "Connection closed" not in msg and "Target page, context or browser has been closed" not in msg:
            LOG.warning("browser close warning: %s", e)


# ---------- Page state ----------

async def dismiss_offline_progress_modal(page: Page, timeout_ms: int = 2500) -> bool:
    bg = page.locator('div[class*="OfflineProgressModal_background"]').first
    modal = page.locator('div[class*="OfflineProgressModal_modalContainer"]').first
    if not (await locator_is_visible(bg) or await locator_is_visible(modal)):
        return False

    btn = await first_visible_locator(
        [
            *button_candidates(modal, ["Close", "OK", "Collect", "Claim", "关闭", "确定", "领取"]),
            modal.locator("button").first,
        ],
        timeout=0.8,
    )
    if btn is not None:
        try:
            await btn.click(timeout=2000)
        except Exception:
            pass
    else:
        try:
            await bg.click(timeout=1000)
        except Exception:
            pass
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass

    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        if not (await locator_is_visible(bg) or await locator_is_visible(modal)):
            return True
        await asyncio.sleep(0.1)
    return False


async def maybe_click_with_intercept_retry(page: Page, locator: Locator, timeout_ms: int = 5000) -> None:
    try:
        await locator.click(timeout=timeout_ms)
    except Exception as e:
        if "intercepts pointer events" in str(e):
            await dismiss_offline_progress_modal(page, timeout_ms=2500)
            await locator.click(timeout=timeout_ms)
        else:
            raise


async def ensure_probably_logged_in(page: Page) -> None:
    shell_markers = [
        page.locator('div[class*="NavigationBar_navigationLink"]').first,
        page.locator('[aria-label^="navigationBar."]').first,
        page.locator('main').first,
        page.locator('div[class*="LabyrinthPanel_"]').first,
        page.locator('div[class*="SettingsPanel_"]').first,
    ]
    for marker in shell_markers:
        if await locator_is_visible(marker):
            return

    end = time.time() + 8.0
    body = ""
    while time.time() < end:
        await asyncio.sleep(0.25)
        for marker in shell_markers:
            if await locator_is_visible(marker):
                return
        body = await page_body_text(page, limit=2500)
        if body:
            break

    auth_hits = sum(1 for pat in AUTH_SIGNALS if pat.search(body))
    if auth_hits >= 2:
        raise RuntimeError(
            "saved state appears invalid; page still looks like login/auth screen"
        )


async def goto_game_page(page: Page, game_url: str, *, just_reloaded: bool = False) -> None:
    await page.goto(game_url, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    if just_reloaded:
        await dismiss_offline_progress_modal(page, timeout_ms=2500)
    await ensure_probably_logged_in(page)


async def find_labyrinth_root(page: Page, timeout_sec: float = 0.8) -> Locator | None:
    candidates = [
        page.locator('div[class*="LabyrinthPanel_"]').first,
        page.locator('div[class*="RoomGrid_"]').first,
        page.locator('div[class*="ActiveRoomContainer_"]').first,
        page.locator("main").first,
    ]
    return await first_visible_locator(candidates, timeout=timeout_sec)


async def find_settings_root(page: Page, timeout_sec: float = 0.8) -> Locator | None:
    candidates = [
        page.locator('div[class*="SettingsPanel_"]').first,
        page.locator('section[class*="SettingsPanel_"]').first,
        page.locator("main").first,
    ]
    return await first_visible_locator(candidates, timeout=timeout_sec)


async def wait_for_labyrinth_panel(page: Page, timeout_ms: int = 10000) -> bool:
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        root = await find_labyrinth_root(page, timeout_sec=0.25)
        if root is not None:
            text = await locator_text(root, limit=1800)
            if re.search(r"Labyrinth|Enter Labyrinth|Floor\s+\d+|Escape|Entries", text, re.I):
                return True
        await asyncio.sleep(0.15)
    return False


async def wait_for_settings_panel(page: Page, timeout_ms: int = 10000) -> bool:
    end = time.time() + timeout_ms / 1000
    while time.time() < end:
        root = await find_settings_root(page, timeout_sec=0.25)
        if root is not None:
            text = await locator_text(root, limit=1800)
            if re.search(r"Settings|Refill Entries|Refill Labyrinth Entries", text, re.I):
                return True
        await asyncio.sleep(0.15)
    return False


async def goto_labyrinth_panel(page: Page, timeout_ms: int = 15000) -> None:
    if await wait_for_labyrinth_panel(page, timeout_ms=1200):
        return
    target = await first_visible_locator(nav_candidates(page, "navigationBar.labyrinth", ["Labyrinth", "迷宫"]), timeout=4.0)
    if target is None:
        raise RuntimeError("labyrinth navigation link not found")
    await maybe_click_with_intercept_retry(page, target)
    if not await wait_for_labyrinth_panel(page, timeout_ms=timeout_ms):
        raise RuntimeError("failed to open labyrinth panel")


async def goto_settings_panel(page: Page, timeout_ms: int = 15000) -> None:
    if await wait_for_settings_panel(page, timeout_ms=1200):
        return
    target = await first_visible_locator(nav_candidates(page, "navigationBar.settings", ["Settings", "设置"]), timeout=4.0)
    if target is None:
        raise RuntimeError("settings navigation link not found")
    await maybe_click_with_intercept_retry(page, target)
    if not await wait_for_settings_panel(page, timeout_ms=timeout_ms):
        raise RuntimeError("failed to open settings panel")


async def read_ticket_count(page: Page) -> TicketState:
    root = await find_labyrinth_root(page, timeout_sec=0.5)
    texts: list[str] = []
    if root is not None:
        texts.append(await locator_text(root, limit=4000))
    texts.append(await page_body_text(page, limit=5000))

    for text in texts:
        for pat in ENTRY_COUNT_PATTERNS:
            m = pat.search(text)
            if m:
                return TicketState(current=int(m.group(1)), max=int(m.group(2)), raw_text=normalize_space(m.group(0)))
    return TicketState(raw_text=texts[0] if texts else "")


async def get_current_floor(page: Page) -> int | None:
    text = await page_body_text(page, limit=6000)
    m = FLOOR_PATTERN.search(text)
    if m:
        return int(m.group(1))
    return None


async def read_active_action_text(page: Page) -> str:
    text = await page_body_text(page, limit=2500)
    m = re.search(r"Active Characters:\s*\d+\s+(.+?)\s+Stop\b", text, re.I)
    if m:
        return normalize_space(m.group(1))
    title = normalize_space(await page.title())
    if title:
        return title
    return ""


async def detect_labyrinth_state(page: Page) -> LabyrinthState:
    text = await page_body_text(page, limit=6000)
    active_action = await read_active_action_text(page)
    active_is_lab = bool(re.search(r"\bLabyrinth\b", active_action, re.I))

    floor_match = FLOOR_PATTERN.search(text)
    exit_btn = await first_visible_locator(button_candidates(page, ["Escape", "Escape Labyrinth", "结束迷宫", "逃离迷宫"]), timeout=0.35)

    # When the floor cap is reached, the page can still show the last labyrinth floor,
    # but the active action bar has already switched back to another task. In that case
    # we should escape instead of idling forever on the old floor screen.
    if floor_match:
        if active_action and not active_is_lab:
            return LabyrinthState(
                "needs_escape",
                f"floor {floor_match.group(1)} visible but active action is '{active_action[:80]}'",
            )
        return LabyrinthState("in_labyrinth", f"floor {floor_match.group(1)} treasure {floor_match.group(2)}")

    if exit_btn is not None:
        if active_action and not active_is_lab:
            return LabyrinthState("needs_escape", f"escape visible and active action is '{active_action[:80]}'")
        return LabyrinthState("in_labyrinth", "escape button visible")

    claim_btn = await first_visible_locator(button_candidates(page, ["Claim", "Collect", "领取", "收取"]), timeout=0.35)
    if claim_btn is not None:
        return LabyrinthState("finished", "claim/collect visible")

    enter_btn = await first_visible_locator(button_candidates(page, ["Enter Labyrinth", "进入迷宫"]), timeout=0.35)
    if enter_btn is not None:
        return LabyrinthState("entry", "enter labyrinth button visible")

    start_btn = await first_visible_locator(button_candidates(page, ["Start", "Start Now", "开始", "立即开始"]), timeout=0.35)
    if start_btn is not None:
        return LabyrinthState("entry", "start button visible")

    if re.search(r"The Labyrinth consists of floors|Entries regenerate|Enter Labyrinth", text, re.I):
        return LabyrinthState("entry", "entry text visible")

    if re.search(r"Claim|Collect|领取|收取", text, re.I) and re.search(r"Labyrinth", text, re.I):
        return LabyrinthState("finished", "result text visible")

    return LabyrinthState("unknown", text[:200])


# ---------- Actions ----------

async def collect_labyrinth_result(page: Page) -> tuple[bool, str]:
    clicked = False
    for _ in range(4):
        btn = await first_visible_enabled_locator(
            [
                *button_candidates(page, ["Claim", "Collect", "领取", "收取"]),
                *button_candidates(page, ["OK", "Confirm", "Continue", "确定", "确认"]),
            ],
            timeout=0.8,
        )
        if btn is None:
            break
        await maybe_click_with_intercept_retry(page, btn)
        clicked = True
        await page.wait_for_timeout(250)
    return clicked, ("result claimed" if clicked else "no result button found")


async def replenish_tickets(page: Page, before_current: int | None, timeout_ms: int) -> tuple[bool, TicketState, str]:
    await goto_settings_panel(page, timeout_ms=timeout_ms)
    scope = await find_settings_root(page, timeout_sec=0.8) or page

    refill_btn = await first_visible_enabled_locator(
        button_candidates(scope, ["Refill Entries", "Refill Labyrinth Entries", "补充入场券", "补充门票"]),
        timeout=2.5,
    )
    if refill_btn is None:
        disabled_btn = await first_visible_locator(
            button_candidates(scope, ["Refill Entries", "Refill Labyrinth Entries", "补充入场券", "补充门票"]),
            timeout=1.0,
        )
        await goto_labyrinth_panel(page, timeout_ms=timeout_ms)
        if disabled_btn is not None:
            return False, await read_ticket_count(page), "refill button disabled or cooldown active"
        return False, await read_ticket_count(page), "refill button not found"

    await maybe_click_with_intercept_retry(page, refill_btn)
    await page.wait_for_timeout(400)
    await goto_labyrinth_panel(page, timeout_ms=timeout_ms)

    end = time.time() + 6.0
    last = TicketState()
    while time.time() < end:
        last = await read_ticket_count(page)
        if last.current is not None:
            if before_current is None:
                return True, last, f"entries now {last.current}/{last.max}"
            if last.current > before_current:
                return True, last, f"entries increased from {before_current} to {last.current}"
        await asyncio.sleep(0.25)

    return False, last, f"entries did not increase after refill; latest='{last.raw_text}'"


async def maybe_accept_entry_dialog(page: Page) -> tuple[bool, str]:
    dialogs = [
        page.locator('[role="dialog"]').last,
        page.locator('div.MuiDialog-root').last,
        page.locator('div[class*="DialogModal_"]').last,
    ]
    dialog = await first_visible_locator(dialogs, timeout=0.8)
    if dialog is None:
        return False, "no entry confirm dialog"

    text = await locator_text(dialog, limit=1600)
    if not ENTRY_DIALOG_PATTERN.search(text):
        return False, f"dialog visible but not entry confirm: {text[:120]}"

    yes_btn = await first_visible_enabled_locator(
        button_candidates(dialog, ["Yes", "OK", "Continue", "确定", "确认"]),
        timeout=2.0,
    )
    if yes_btn is None:
        return False, "entry confirm dialog visible but yes button not found"

    await maybe_click_with_intercept_retry(page, yes_btn)
    await page.wait_for_timeout(350)
    return True, "entry confirm dialog accepted"


async def enter_labyrinth(page: Page, timeout_ms: int) -> tuple[bool, str, int | None]:
    root = await find_labyrinth_root(page, timeout_sec=0.8) or page

    enter_btn = await first_visible_enabled_locator(
        [
            *button_candidates(root, ["Enter Labyrinth", "进入迷宫"]),
            *button_candidates(page, ["Enter Labyrinth", "进入迷宫"]),
        ],
        timeout=2.0,
    )
    if enter_btn is None:
        return False, "enter labyrinth button not found", None

    await maybe_click_with_intercept_retry(page, enter_btn)
    await page.wait_for_timeout(300)

    accepted, dialog_detail = await maybe_accept_entry_dialog(page)
    if accepted:
        LOG.info("labyrinth entry dialog: %s", dialog_detail)
        await page.wait_for_timeout(300)

    start_btn = await first_visible_enabled_locator(
        [
            *button_candidates(page, ["Start", "Start Now", "开始", "立即开始"]),
            *button_candidates(root, ["Start", "Start Now", "开始", "立即开始"]),
        ],
        timeout=3.0,
    )
    if start_btn is None:
        state = await detect_labyrinth_state(page)
        return False, f"start button not found after enter; state={state.state} ({state.detail})", None

    await maybe_click_with_intercept_retry(page, start_btn)
    await page.wait_for_timeout(500)

    end = time.time() + timeout_ms / 1000
    last_state = LabyrinthState()
    while time.time() < end:
        last_state = await detect_labyrinth_state(page)
        floor = await get_current_floor(page)
        if last_state.state == "in_labyrinth":
            return True, (f"entered labyrinth on floor {floor}" if floor is not None else "entered labyrinth"), floor
        await asyncio.sleep(0.25)

    return False, f"labyrinth state after enter attempt: {last_state.state} ({last_state.detail})", None

async def click_locator_hard(page: Page, locator: Locator, timeout_ms: int = 2500) -> None:
    last_err = None

    try:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass

    try:
        await locator.click(timeout=timeout_ms)
        return
    except Exception as e:
        last_err = e

    try:
        await locator.click(force=True, timeout=timeout_ms)
        return
    except Exception as e:
        last_err = e

    try:
        box = await locator.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.up()
            return
    except Exception as e:
        last_err = e

    try:
        await locator.evaluate("(el) => el.click()")
        return
    except Exception as e:
        last_err = e

    raise RuntimeError(f"all click methods failed: {last_err}")

async def maybe_confirm_escape_dialog(page: Page) -> tuple[bool, str]:
    # 只认真正可见的 escape dialog
    dialog = page.locator(
        'div.MuiDialog-root:has-text("Are you sure you want to escape the Labyrinth?")'
    ).last

    try:
        await dialog.wait_for(state="visible", timeout=1500)
    except Exception:
        return False, "no visible escape confirm dialog"

    # 优先直接点成功按钮（Yes）
    yes_btn = dialog.locator("button.Button_success__6d6kU").first

    try:
        await yes_btn.wait_for(state="visible", timeout=1500)
    except Exception:
        return False, "escape dialog visible but success button not visible"

    # 多层兜底：普通 click -> force click -> 鼠标点中心 -> JS click
    last_err = None

    try:
        await yes_btn.click(timeout=2000)
    except Exception as e:
        last_err = e
        try:
            await yes_btn.click(force=True, timeout=2000)
        except Exception as e2:
            last_err = e2
            try:
                box = await yes_btn.bounding_box()
                if box:
                    await page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                else:
                    raise RuntimeError("no bounding box for Yes button")
            except Exception as e3:
                last_err = e3
                try:
                    await yes_btn.evaluate("(el) => el.click()")
                except Exception as e4:
                    last_err = e4
                    return False, f"failed to click escape Yes button: {last_err}"

    # 必须验证 dialog 真正关闭
    end = time.time() + 4.0
    while time.time() < end:
        try:
            if not await dialog.is_visible():
                return True, "escape confirm dialog accepted and closed"
        except Exception:
            return True, "escape confirm dialog accepted and detached"
        await asyncio.sleep(0.15)

    return False, "clicked Yes but escape dialog is still visible"

ESCAPE_DIALOG_TEXT_RE = re.compile(
    r"(escape the labyrinth|torches remaining|really sure)",
    re.I,
)

async def find_visible_escape_dialog(page):
    candidates = [
        page.locator('div.MuiDialog-root').filter(has_text=ESCAPE_DIALOG_TEXT_RE),
        page.locator('div[class*="DialogModal_"]').filter(has_text=ESCAPE_DIALOG_TEXT_RE),
    ]

    end = time.time() + 2.0
    while time.time() < end:
        for group in candidates:
            try:
                n = await group.count()
            except Exception:
                n = 0
            for i in range(n - 1, -1, -1):
                dlg = group.nth(i)
                try:
                    if await dlg.is_visible():
                        return dlg
                except Exception:
                    pass
        await asyncio.sleep(0.1)

    return None


async def click_yes_in_dialog(page, dialog):
    yes_candidates = [
        dialog.locator("button.Button_success__6d6kU").last,
        dialog.get_by_role("button", name=re.compile(r"^\s*Yes\s*$", re.I)).last,
        dialog.locator("button").filter(has_text=re.compile(r"^\s*Yes\s*$", re.I)).last,
    ]

    for btn in yes_candidates:
        try:
            if await btn.count() > 0 and await btn.is_visible():
                try:
                    await btn.click(timeout=1500)
                except Exception:
                    await btn.click(force=True, timeout=1500)
                return True
        except Exception:
            pass

    return False


async def accept_escape_dialog_chain(page, max_dialogs=3):
    notes = []

    for idx in range(1, max_dialogs + 1):
        dialog = await find_visible_escape_dialog(page)
        if dialog is None:
            break

        try:
            text = await dialog.inner_text()
        except Exception:
            text = ""

        text_one_line = re.sub(r"\s+", " ", text).strip()

        ok = await click_yes_in_dialog(page, dialog)
        if not ok:
            notes.append(f"dialog {idx}: yes button not found")
            return False, notes

        if "torches remaining" in text_one_line.lower():
            notes.append(f"dialog {idx}: accepted torch warning")
        elif "escape the labyrinth" in text_one_line.lower():
            notes.append(f"dialog {idx}: accepted escape confirm")
        else:
            notes.append(f"dialog {idx}: accepted dialog: {text_one_line[:120]}")

        await asyncio.sleep(0.35)

    leftover = await find_visible_escape_dialog(page)
    if leftover is not None:
        notes.append("escape dialogs still visible after chain")
        return False, notes

    return True, notes



async def escape_labyrinth(page: Page, timeout_ms: int = 15000) -> tuple[bool, str]:
    notes = []

    state = await detect_labyrinth_state(page)
    if state.state not in {"in_labyrinth", "needs_escape"}:
        return True, f"not in labyrinth ({state.state})"

    escape_btn = page.get_by_role("button", name=re.compile(r"^\s*Escape\s*$", re.I)).last
    try:
        await escape_btn.click(timeout=2000)
    except Exception:
        await escape_btn.click(force=True, timeout=2000)

    notes.append("clicked Escape")

    ok, chain_notes = await accept_escape_dialog_chain(page, max_dialogs=3)
    notes.extend(chain_notes)
    if not ok:
        return False, "; ".join(notes)

    end = time.time() + timeout_ms / 1000

    # 先给前端一点时间自己切回入口
    while time.time() < end:
        state = await detect_labyrinth_state(page)
        if state.state in {"entry", "finished", "unknown"}:
            notes.append(f"final_state={state.state}")
            return True, "; ".join(notes)
        await asyncio.sleep(0.25)

    # 再做一次 reload 兜底
    try:
        await page.reload(wait_until="domcontentloaded", timeout=10000)
    except Exception:
        try:
            await page.goto(page.url, wait_until="domcontentloaded", timeout=10000)
        except Exception as e:
            notes.append(f"reload_failed={e}")
            return False, "; ".join(notes)

    await page.wait_for_timeout(1200)

    try:
        await dismiss_offline_progress_modal(page, timeout_ms=1500)
    except Exception:
        pass

    try:
        await goto_labyrinth_panel(page, timeout_ms=8000)
    except Exception:
        pass

    state = await detect_labyrinth_state(page)
    if state.state in {"entry", "finished", "unknown"}:
        notes.append(f"final_state={state.state}")
        return True, "; ".join(notes)

    notes.append(f"final_state={state.state} ({state.detail})")
    return False, "; ".join(notes)


# ---------- Context / worker ----------

async def new_context_for_account(browser: Browser, account: AccountConfig, settings: RunSettings) -> tuple[BrowserContext, Page]:
    context = await browser.new_context(
        storage_state=account.state_file,
        service_workers="block",
        viewport={"width": 1280, "height": 900},
    )
    if settings.block_assets:
        await context.route("**/*", block_static_assets)
    page = await context.new_page()
    await goto_game_page(page, account.game_url, just_reloaded=True)
    return context, page


async def run_one_cycle(page: Page, settings: RunSettings) -> CycleResult:
    await goto_labyrinth_panel(page, timeout_ms=settings.navigation_timeout_ms)

    state_before = await detect_labyrinth_state(page)
    tickets_before = await read_ticket_count(page)
    detail_parts: list[str] = [f"state_before={state_before.state}:{state_before.detail}"]

    if state_before.state == "finished":
        claimed, claim_detail = await collect_labyrinth_result(page)
        detail_parts.append(claim_detail)
        state_before = await detect_labyrinth_state(page)
        tickets_before = await read_ticket_count(page)
        if claimed:
            return CycleResult(
                ok=True,
                mode="claimed_result",
                detail="; ".join(detail_parts),
                tickets_before=tickets_before.current,
                tickets_after=tickets_before.current,
                tickets_max=tickets_before.max,
                floor=None,
            )

    if state_before.state == "needs_escape":
        escaped, escape_detail = await escape_labyrinth(page, timeout_ms=settings.navigation_timeout_ms)
        detail_parts.append(escape_detail)
        tickets_after = await read_ticket_count(page)
        return CycleResult(
            ok=escaped,
            mode="escaped_finished_run" if escaped else "escape_failed",
            error="" if escaped else escape_detail,
            detail="; ".join(detail_parts),
            tickets_before=tickets_before.current,
            tickets_after=tickets_after.current,
            tickets_max=tickets_after.max or tickets_before.max,
            floor=None,
        )

    if state_before.state == "in_labyrinth":
        floor = await get_current_floor(page)
        return CycleResult(
            ok=True,
            mode="already_in_labyrinth",
            detail="; ".join(detail_parts),
            tickets_before=tickets_before.current,
            tickets_after=tickets_before.current,
            tickets_max=tickets_before.max,
            floor=floor,
        )

    if tickets_before.current is not None and tickets_before.current <= settings.low_ticket_threshold:
        rep_ok, rep_tickets, rep_detail = await replenish_tickets(
            page,
            before_current=tickets_before.current,
            timeout_ms=settings.navigation_timeout_ms,
        )
        detail_parts.append(rep_detail)
        tickets_before = rep_tickets if rep_tickets.current is not None else tickets_before
        if not rep_ok and tickets_before.current == 0:
            return CycleResult(
                ok=True,
                mode="waiting_ticket_cooldown",
                detail="; ".join(detail_parts),
                tickets_before=tickets_before.current,
                tickets_after=tickets_before.current,
                tickets_max=tickets_before.max,
                floor=None,
            )

    if tickets_before.current == 0:
        return CycleResult(
            ok=True,
            mode="no_ticket",
            detail="; ".join(detail_parts),
            tickets_before=tickets_before.current,
            tickets_after=tickets_before.current,
            tickets_max=tickets_before.max,
            floor=None,
        )

    entered, enter_detail, floor = await enter_labyrinth(page, timeout_ms=settings.navigation_timeout_ms)
    detail_parts.append(enter_detail)
    tickets_after = await read_ticket_count(page)

    if not entered:
        return CycleResult(
            ok=False,
            mode="enter_failed",
            error=enter_detail,
            detail="; ".join(detail_parts),
            tickets_before=tickets_before.current,
            tickets_after=tickets_after.current,
            tickets_max=tickets_after.max or tickets_before.max,
            floor=floor,
        )

    return CycleResult(
        ok=True,
        mode="entered_labyrinth",
        detail="; ".join(detail_parts),
        tickets_before=tickets_before.current,
        tickets_after=tickets_after.current,
        tickets_max=tickets_after.max or tickets_before.max,
        floor=floor,
    )


async def account_worker(playwright: Playwright, account: AccountConfig, settings: RunSettings, sink: CsvSink) -> WorkerSummary:
    debug_dir = Path(settings.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict[str, Any] = {"headless": settings.headless}
    if settings.browser_channel:
        launch_kwargs["channel"] = settings.browser_channel
    browser = await playwright.chromium.launch(**launch_kwargs)

    context: BrowserContext | None = None
    page: Page | None = None
    summary = WorkerSummary(account=account.name)

    run_started_at: float | None = None
    run_started_by_script = False
    last_logged_floor: int | None = None

    async def reset_context() -> None:
        nonlocal context, page
        await safe_close_context(context, reason="reset")
        context, page = await new_context_for_account(browser, account, settings)

    try:
        await reset_context()
        iteration = 1

        while settings.loops <= 0 or iteration <= settings.loops:
            t0 = time.perf_counter()
            try:
                assert page is not None

                if iteration > 1 and settings.refresh_every > 0 and iteration % settings.refresh_every == 0:
                    await goto_game_page(page, account.game_url, just_reloaded=True)

                if iteration > 1 and settings.recycle_context_every > 0 and iteration % settings.recycle_context_every == 0:
                    await reset_context()
                    assert page is not None

                if account.action_bias_ms > 0:
                    await page.wait_for_timeout(account.action_bias_ms)

                pre_state = await detect_labyrinth_state(page)
                pre_floor = await get_current_floor(page)

                # If we attach mid-run, log once but do not claim full round timing.
                if pre_state.state == "in_labyrinth" and run_started_at is None:
                    run_started_at = time.time()
                    run_started_by_script = False
                    last_logged_floor = pre_floor
                    LOG.info(
                        "[%s] attached to active labyrinth%s",
                        account.name,
                        f" on floor {pre_floor}" if pre_floor is not None else "",
                    )

                result = await run_one_cycle(page, settings)
                post_state = await detect_labyrinth_state(page)
                post_floor = await get_current_floor(page)

                summary.total += 1
                if result.ok:
                    summary.ok += 1
                    if result.mode in {"already_in_labyrinth", "waiting_ticket_cooldown", "no_ticket", "claimed_result", "escaped_finished_run"}:
                        summary.skipped += 1
                else:
                    summary.failed += 1

                # Start timing only when the script itself started a run.
                if result.ok and result.mode == "entered_labyrinth":
                    run_started_at = time.time()
                    run_started_by_script = True
                    last_logged_floor = result.floor
                    LOG.info(
                        "[%s] labyrinth started%s",
                        account.name,
                        f" on floor {result.floor}" if result.floor is not None else "",
                    )

                # Floor-only logging for active runs.
                if post_state.state == "in_labyrinth":
                    current_floor = post_floor
                    if current_floor is not None and last_logged_floor is not None and current_floor != last_logged_floor:
                        last_logged_floor = current_floor
                        if run_started_by_script and run_started_at is not None:
                            LOG.info(
                                "[%s] floor %s (elapsed %s)",
                                account.name,
                                current_floor,
                                format_duration(time.time() - run_started_at),
                            )
                        else:
                            LOG.info("[%s] floor %s", account.name, current_floor)
                    elif current_floor is not None and last_logged_floor is None:
                        last_logged_floor = current_floor

                # Finish logging only if the script started the run.
                if run_started_at is not None and run_started_by_script and post_state.state == "finished":
                    LOG.info(
                        "[%s] labyrinth round finished in %s",
                        account.name,
                        format_duration(time.time() - run_started_at),
                    )
                    run_started_at = None
                    run_started_by_script = False
                    last_logged_floor = None

                if run_started_at is not None and run_started_by_script and (
                    (pre_state.state in {"in_labyrinth", "needs_escape"} and post_state.state == "entry")
                    or result.mode == "escaped_finished_run"
                ):
                    LOG.info(
                        "[%s] labyrinth round finished in %s",
                        account.name,
                        format_duration(time.time() - run_started_at),
                    )
                    run_started_at = None
                    run_started_by_script = False
                    last_logged_floor = None

                row = {
                    "account": account.name,
                    "iteration": iteration,
                    "ok": result.ok,
                    "mode": result.mode,
                    "error": result.error,
                    "tickets_before": result.tickets_before,
                    "tickets_after": result.tickets_after,
                    "tickets_max": result.tickets_max,
                    "floor": post_floor,
                    "round_elapsed": format_duration(time.time() - run_started_at) if (run_started_at and run_started_by_script) else "",
                    "elapsed_sec": round(time.perf_counter() - t0, 2),
                    "detail": result.detail,
                }
                await sink.write_row(row)

                if not result.ok:
                    LOG.warning("[%s] iteration %s failed: %s", account.name, iteration, result.error)
                    if settings.save_failure_screenshots:
                        await take_failure_screenshot(page, debug_dir / f"{account.name}_fail_{iteration}.png")
                    await reset_context()
                    run_started_at = None
                    run_started_by_script = False
                    last_logged_floor = None
                elif settings.log_every > 0 and iteration % settings.log_every == 0:
                    LOG.info(
                        "[%s] iter=%s mode=%s tickets=%s/%s floor=%s",
                        account.name,
                        iteration,
                        result.mode,
                        result.tickets_after,
                        result.tickets_max,
                        post_floor,
                    )

            except Exception as e:
                summary.total += 1
                summary.failed += 1
                err = str(e)
                LOG.warning("[%s] iteration %s exception: %s", account.name, iteration, err)
                if settings.save_failure_screenshots:
                    await take_failure_screenshot(page, debug_dir / f"{account.name}_exception_{iteration}.png")
                await sink.write_row(
                    {
                        "account": account.name,
                        "iteration": iteration,
                        "ok": False,
                        "mode": "exception",
                        "error": err,
                        "tickets_before": "",
                        "tickets_after": "",
                        "tickets_max": "",
                        "floor": "",
                        "round_elapsed": "",
                        "elapsed_sec": round(time.perf_counter() - t0, 2),
                        "detail": "",
                    }
                )
                await reset_context()
                run_started_at = None
                run_started_by_script = False
                last_logged_floor = None

            iteration += 1
            await asyncio.sleep(settings.delay_sec)

        return summary

    finally:
        await safe_close_context(context, reason="worker finished")
        await safe_close_browser(browser, reason="worker finished")
        gc.collect()


# ---------- Probe ----------

async def build_probe_snapshot(page: Page, panel_name: str) -> dict[str, Any]:
    try:
        snapshot = await page.evaluate(
            r'''
            (panelName) => {
              const isVisible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return !!(rect.width || rect.height) && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const compact = (s, n = 220) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
              const textOf = (el) => compact(el.innerText || el.textContent || '', 220);

              const visibleButtons = [];
              for (const el of document.querySelectorAll('button, [role="button"], a, div[class*="NavigationBar_navigationLink"], [aria-label]')) {
                if (!isVisible(el)) continue;
                const text = textOf(el);
                const ariaLabel = el.getAttribute('aria-label') || '';
                if (!text && !ariaLabel) continue;
                visibleButtons.push({
                  tag: el.tagName.toLowerCase(),
                  text,
                  ariaLabel,
                  className: compact(typeof el.className === 'string' ? el.className : '', 180),
                  disabled: !!el.disabled,
                });
                if (visibleButtons.length >= 80) break;
              }

              return {
                panelName,
                url: location.href,
                title: document.title,
                bodyText: compact(document.body ? document.body.innerText : '', 5000),
                visibleButtons,
              };
            }
            ''',
            panel_name,
        )
    except Exception as e:
        snapshot = {
            "panelName": panel_name,
            "url": page.url,
            "title": "",
            "bodyText": f"probe evaluate failed: {e}",
            "visibleButtons": [],
        }
    snapshot["ticket_state"] = {
        "current": (await read_ticket_count(page)).current,
        "max": (await read_ticket_count(page)).max,
        "raw_text": (await read_ticket_count(page)).raw_text,
    }
    state = await detect_labyrinth_state(page)
    snapshot["labyrinth_state"] = {"state": state.state, "detail": state.detail}
    return snapshot


async def probe_account(playwright: Playwright, account: AccountConfig, settings: RunSettings) -> Path:
    debug_dir = Path(settings.debug_dir)
    debug_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict[str, Any] = {"headless": settings.headless}
    if settings.browser_channel:
        launch_kwargs["channel"] = settings.browser_channel
    browser = await playwright.chromium.launch(**launch_kwargs)

    context: BrowserContext | None = None
    page: Page | None = None
    output_path = debug_dir / f"{account.name}_probe.json"

    try:
        context, page = await new_context_for_account(browser, account, settings)
        initial = await build_probe_snapshot(page, "initial")
        await take_failure_screenshot(page, debug_dir / f"{account.name}_probe_initial.png")

        await goto_labyrinth_panel(page, timeout_ms=settings.navigation_timeout_ms)
        labyrinth = await build_probe_snapshot(page, "labyrinth")
        await take_failure_screenshot(page, debug_dir / f"{account.name}_probe_labyrinth.png")

        await goto_settings_panel(page, timeout_ms=settings.navigation_timeout_ms)
        settings_snapshot = await build_probe_snapshot(page, "settings")
        await take_failure_screenshot(page, debug_dir / f"{account.name}_probe_settings.png")

        payload = {
            "account": account.name,
            "game_url": account.game_url,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "initial": initial,
            "labyrinth": labyrinth,
            "settings": settings_snapshot,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("[%s] probe written to %s", account.name, output_path)
        return output_path
    finally:
        await safe_close_context(context, reason="probe finished")
        await safe_close_browser(browser, reason="probe finished")
        gc.collect()


# ---------- Entrypoints ----------

async def run_parallel(accounts: list[AccountConfig], settings: RunSettings) -> list[WorkerSummary]:
    sink = CsvSink(settings.results_csv)
    p = await async_playwright().start()
    try:
        results = await asyncio.gather(
            *[account_worker(p, account, settings, sink) for account in accounts],
            return_exceptions=True,
        )
        summaries: list[WorkerSummary] = []
        for account, res in zip(accounts, results):
            if isinstance(res, Exception):
                LOG.error("[%s] worker crashed: %s", account.name, res)
                await sink.write_row(
                    {
                        "account": account.name,
                        "iteration": "",
                        "ok": False,
                        "mode": "worker_crashed",
                        "error": str(res),
                        "tickets_before": "",
                        "tickets_after": "",
                        "tickets_max": "",
                        "floor": "",
                        "round_elapsed": "",
                        "elapsed_sec": "",
                        "detail": "",
                    }
                )
                summaries.append(WorkerSummary(account=account.name, total=0, ok=0, skipped=0, failed=1))
            else:
                summaries.append(res)
        return summaries
    finally:
        try:
            await p.stop()
        except Exception:
            pass
        gc.collect()


async def run_probe(accounts: list[AccountConfig], settings: RunSettings) -> list[Path]:
    p = await async_playwright().start()
    try:
        outputs: list[Path] = []
        for account in accounts:
            outputs.append(await probe_account(p, account, settings))
        return outputs
    finally:
        try:
            await p.stop()
        except Exception:
            pass
        gc.collect()


async def save_state_from_existing_chrome(cdp_url: str, output_path: str) -> None:
    p = await async_playwright().start()
    try:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError(f"No browser context found at {cdp_url}")
        context = browser.contexts[0]
        await context.storage_state(path=output_path, indexed_db=True)
        LOG.info("saved storage state to %s", output_path)
    finally:
        await p.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Milky Way Idle auto-labyrinth runner")
    sub = parser.add_subparsers(dest="command", required=True)

    save_state_p = sub.add_parser("save-state", help="save auth state from an already-open Chromium browser via CDP")
    save_state_p.add_argument("--cdp-url", required=True)
    save_state_p.add_argument("--output", required=True)

    run_p = sub.add_parser("run", help="run labyrinth automation")
    run_p.add_argument("--accounts", required=True)
    run_p.add_argument("--account", default=None)
    run_p.add_argument("--loops", type=int, default=0, help="0 means run forever")
    run_p.add_argument("--delay-sec", type=float, default=20.0)
    run_p.add_argument("--refresh-every", type=int, default=40)
    run_p.add_argument("--recycle-context-every", type=int, default=200)
    run_p.add_argument("--low-ticket-threshold", type=int, default=0)
    run_p.add_argument("--results-csv", default="auto_labyrinth_results.csv")
    run_p.add_argument("--debug-dir", default="autolabyrinth_debug")
    run_p.add_argument("--log-every", type=int, default=10)
    run_p.add_argument("--browser-channel", default=None)
    run_p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    run_p.add_argument("--save-failure-screenshots", action=argparse.BooleanOptionalAction, default=False)
    run_p.add_argument("--block-assets", action=argparse.BooleanOptionalAction, default=True)
    run_p.add_argument("--verbose", action="store_true")

    probe_p = sub.add_parser("probe", help="dump simple DOM hints and screenshots")
    probe_p.add_argument("--accounts", required=True)
    probe_p.add_argument("--account", default=None)
    probe_p.add_argument("--debug-dir", default="autolabyrinth_debug")
    probe_p.add_argument("--browser-channel", default=None)
    probe_p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=False)
    probe_p.add_argument("--block-assets", action=argparse.BooleanOptionalAction, default=True)
    probe_p.add_argument("--verbose", action="store_true")

    return parser


async def async_main(args: argparse.Namespace) -> int:
    if args.command == "save-state":
        await save_state_from_existing_chrome(args.cdp_url, args.output)
        return 0

    accounts = filter_accounts(load_accounts(args.accounts), getattr(args, "account", None))
    settings = RunSettings(
        loops=getattr(args, "loops", 0),
        delay_sec=getattr(args, "delay_sec", 20.0),
        refresh_every=getattr(args, "refresh_every", 40),
        recycle_context_every=getattr(args, "recycle_context_every", 200),
        low_ticket_threshold=getattr(args, "low_ticket_threshold", 0),
        results_csv=getattr(args, "results_csv", "auto_labyrinth_results.csv"),
        debug_dir=getattr(args, "debug_dir", "autolabyrinth_debug"),
        log_every=getattr(args, "log_every", 10),
        browser_channel=getattr(args, "browser_channel", None),
        headless=getattr(args, "headless", True),
        save_failure_screenshots=getattr(args, "save_failure_screenshots", False),
        block_assets=getattr(args, "block_assets", True),
    )

    if args.command == "probe":
        outputs = await run_probe(accounts, settings)
        for path in outputs:
            LOG.info("probe artifact: %s", path)
        return 0

    summaries = await run_parallel(accounts, settings)
    LOG.info("run finished")
    for s in summaries:
        LOG.info("[%s] total=%s ok=%s skipped=%s failed=%s", s.account, s.total, s.ok, s.skipped, s.failed)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        LOG.warning("interrupted by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
