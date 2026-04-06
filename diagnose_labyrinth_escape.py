#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page, Locator


def normalize_space(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


FLOOR_PATTERN = re.compile(r"\bFloor\s+(\d+)\s*\(Treasure:\s*(\d+\s*/\s*\d+)\)", re.I)
ENTRY_COUNT_PATTERNS = [
    re.compile(r"(\d+)\s*/\s*(\d+)\s*Entries", re.I),
    re.compile(r"Entries\s*[:：]?\s*(\d+)\s*/\s*(\d+)", re.I),
]


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


async def page_body_text(page: Page, limit: int = 7000) -> str:
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


async def first_visible_locator(locators: list[Locator], timeout: float = 3.0) -> Locator | None:
    end = time.time() + timeout
    while time.time() < end:
        for loc in locators:
            try:
                cand = loc.first
                if await cand.count() > 0 and await cand.is_visible():
                    return cand
            except Exception:
                pass
        await asyncio.sleep(0.1)
    return None


async def first_visible_enabled_locator(locators: list[Locator], timeout: float = 3.0) -> Locator | None:
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
        await asyncio.sleep(0.1)
    return fallback


async def dismiss_offline_progress_modal(page: Page, timeout_ms: int = 2000) -> bool:
    bg = page.locator('div[class*="OfflineProgressModal_background"]').first
    modal = page.locator('div[class*="OfflineProgressModal_modalContainer"]').first
    if not (await locator_is_visible(bg) or await locator_is_visible(modal)):
        return False

    btn_candidates = [
        modal.get_by_role("button", name=re.compile(r"close|ok|collect|claim|x|×|关闭|确定|领取", re.I)).first,
        modal.locator("button").first,
    ]
    btn = await first_visible_locator(btn_candidates, timeout=0.8)
    if btn is not None:
        try:
            await btn.click(timeout=1500)
        except Exception:
            pass
    else:
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


async def find_labyrinth_root(page: Page, timeout_sec: float = 1.0) -> Locator | None:
    candidates = [
        page.locator('div[class*="LabyrinthPanel_"]').first,
        page.locator('div[class*="RoomGrid_"]').first,
        page.locator('div[class*="ActiveRoomContainer_"]').first,
        page.locator("main").first,
    ]
    end = time.time() + timeout_sec
    while time.time() < end:
        for loc in candidates:
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    text = await locator_text(loc, limit=1800)
                    if re.search(r"Labyrinth|Enter Labyrinth|Floor\s+\d+|Escape|Entries", text, re.I):
                        return loc
            except Exception:
                pass
        await asyncio.sleep(0.1)
    return None


async def goto_labyrinth_panel(page: Page) -> None:
    root = await find_labyrinth_root(page, timeout_sec=0.8)
    if root is not None:
        return

    candidates = [
        page.locator('[aria-label="navigationBar.labyrinth"]').first,
        page.locator('div[class*="NavigationBar_navigationLink"]').filter(
            has_text=re.compile(r"^\s*Labyrinth\s*$", re.I)
        ).first,
        page.get_by_text(re.compile(r"^\s*Labyrinth\s*$", re.I)).first,
    ]
    target = await first_visible_locator(candidates, timeout=3.0)
    if target is None:
        raise RuntimeError("labyrinth navigation link not found")

    try:
        await target.click(timeout=2500)
    except Exception:
        await target.click(force=True, timeout=2500)

    end = time.time() + 8.0
    while time.time() < end:
        root = await find_labyrinth_root(page, timeout_sec=0.3)
        if root is not None:
            return
        await asyncio.sleep(0.15)

    raise RuntimeError("failed to open labyrinth panel")

async def get_current_floor(page: Page) -> int | None:
    text = await page_body_text(page, limit=7000)
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
    return title


async def read_ticket_count(page: Page) -> dict[str, Any]:
    text = await page_body_text(page, limit=5000)
    for pat in ENTRY_COUNT_PATTERNS:
        m = pat.search(text)
        if m:
            return {
                "current": int(m.group(1)),
                "max": int(m.group(2)),
                "raw_text": normalize_space(m.group(0)),
            }
    return {"current": None, "max": None, "raw_text": ""}


async def detect_labyrinth_state(page: Page) -> dict[str, Any]:
    text = await page_body_text(page, limit=7000)
    active_action = await read_active_action_text(page)
    active_is_lab = bool(re.search(r"\bLabyrinth\b", active_action, re.I))

    floor_match = FLOOR_PATTERN.search(text)
    if floor_match:
        if active_action and not active_is_lab:
            return {
                "state": "needs_escape",
                "detail": f"floor {floor_match.group(1)} visible but active action is '{active_action[:120]}'",
            }
        return {
            "state": "in_labyrinth",
            "detail": f"floor {floor_match.group(1)} treasure {floor_match.group(2)}",
        }

    if re.search(r"Claim|Collect|领取|收取", text, re.I) and re.search(r"Labyrinth", text, re.I):
        return {"state": "finished", "detail": "claim/collect visible in body text"}

    if re.search(r"Enter Labyrinth|进入迷宫|Start Now|立即开始", text, re.I):
        return {"state": "entry", "detail": "entry controls visible"}

    return {"state": "unknown", "detail": text[:200]}


async def capture_state(page: Page, outdir: Path, label: str) -> dict[str, Any]:
    body = await page_body_text(page, limit=7000)
    state = await detect_labyrinth_state(page)
    active_action = await read_active_action_text(page)
    tickets = await read_ticket_count(page)
    floor = await get_current_floor(page)

    dialogs = []
    dialog_locator = page.locator('[role="dialog"], div.MuiDialog-root, div[class*="DialogModal_"]')
    count = await dialog_locator.count()
    for i in range(count):
        dlg = dialog_locator.nth(i)
        try:
            if not await dlg.is_visible():
                continue
            dialogs.append({
                "index": i,
                "text": await locator_text(dlg, limit=1500),
                "html": await dlg.evaluate("(el) => el.outerHTML"),
            })
        except Exception:
            pass

    png_path = outdir / f"{label}.png"
    html_path = outdir / f"{label}.html"
    await page.screenshot(path=str(png_path), full_page=True)
    html_path.write_text(await page.content(), encoding="utf-8")

    return {
        "label": label,
        "url": page.url,
        "title": await page.title(),
        "state": state,
        "active_action": active_action,
        "floor": floor,
        "tickets": tickets,
        "body_excerpt": body[:2500],
        "dialogs": dialogs,
        "screenshot": png_path.name,
        "html": html_path.name,
    }


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
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            return
    except Exception as e:
        last_err = e

    try:
        await locator.evaluate("(el) => el.click()")
        return
    except Exception as e:
        last_err = e

    raise RuntimeError(f"all click methods failed: {last_err}")


async def click_escape(page: Page) -> str:
    root = page.locator('div[class*="LabyrinthPanel_"]').first
    candidates = [
        root.get_by_role("button", name=re.compile(r"^\s*Escape\s*$", re.I)).first,
        root.locator("button.Button_warning__1-AMI").filter(has_text=re.compile(r"^\s*Escape\s*$", re.I)).first,
        page.get_by_role("button", name=re.compile(r"^\s*Escape\s*$", re.I)).first,
    ]
    btn = await first_visible_enabled_locator(candidates, timeout=2.0)
    if btn is None:
        raise RuntimeError("Escape button not found")

    text = await locator_text(btn, limit=100)
    await click_locator_hard(page, btn, timeout_ms=2500)
    return text


async def click_yes_in_escape_dialog(page: Page) -> str:
    dialog = page.locator(
        'div.MuiDialog-root:has-text("Are you sure you want to escape the Labyrinth?")'
    ).last
    await dialog.wait_for(state="visible", timeout=2000)

    yes_btn = dialog.locator("button.Button_success__6d6kU").first
    await yes_btn.wait_for(state="visible", timeout=2000)
    label = await locator_text(yes_btn, limit=100)
    await click_locator_hard(page, yes_btn, timeout_ms=2500)
    return label


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-url", required=True)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--browser-channel", default="chrome")
    ap.add_argument("--outdir", default="escape_trace_debug")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    network_log: list[dict[str, Any]] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel=args.browser_channel)
        context = await browser.new_context(
            storage_state=args.state_file,
            viewport={"width": 1600, "height": 1000},
        )

        async def on_response(resp):
            try:
                url = resp.url
                if "api" in url.lower() or "graphql" in url.lower() or "labyrinth" in url.lower():
                    network_log.append({
                        "type": "response",
                        "url": url,
                        "status": resp.status,
                        "method": resp.request.method,
                    })
            except Exception:
                pass

        context.on("response", on_response)

        page = await context.new_page()

        trace_path = outdir / "escape_trace.zip"
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)

        await page.goto(args.game_url, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        await dismiss_offline_progress_modal(page)
        await goto_labyrinth_panel(page)
        root = await find_labyrinth_root(page, timeout_sec=1.0)
        if root is None:
            raise RuntimeError(f"still not on labyrinth page after navigation; title={await page.title()!r}")
        await asyncio.sleep(0.8)

        states = []

        states.append(await capture_state(page, outdir, "01_before_escape_click"))

        escape_label = await click_escape(page)
        await asyncio.sleep(0.5)
        states.append(await capture_state(page, outdir, "02_after_escape_click"))

        yes_label = await click_yes_in_escape_dialog(page)
        states.append(await capture_state(page, outdir, "03_after_yes_click_immediate"))

        await asyncio.sleep(1.0)
        states.append(await capture_state(page, outdir, "04_after_yes_1s"))

        await asyncio.sleep(2.0)
        states.append(await capture_state(page, outdir, "05_after_yes_3s"))

        await asyncio.sleep(3.0)
        states.append(await capture_state(page, outdir, "06_after_yes_6s"))

        try:
            await page.reload(wait_until="domcontentloaded", timeout=10000)
        except Exception:
            await page.goto(page.url, wait_until="domcontentloaded", timeout=10000)

        await asyncio.sleep(1.2)
        await dismiss_offline_progress_modal(page)
        try:
            await goto_labyrinth_panel(page)
        except Exception:
            pass
        states.append(await capture_state(page, outdir, "07_after_reload"))

        payload = {
            "game_url": args.game_url,
            "escape_button_label": escape_label,
            "yes_button_label": yes_label,
            "states": states,
            "network_log": network_log,
        }

        (outdir / "escape_transition.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        await context.tracing.stop(path=str(trace_path))
        print(f"wrote debug files to: {outdir}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
