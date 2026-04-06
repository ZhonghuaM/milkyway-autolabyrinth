#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Locator, Page


def norm(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


async def locator_text(locator: Locator, limit: int = 500) -> str:
    try:
        txt = await locator.inner_text(timeout=1000)
    except Exception:
        try:
            txt = await locator.text_content(timeout=1000)
        except Exception:
            txt = ""
    return norm(txt)[:limit]


async def first_visible(locators: list[Locator], timeout: float = 3.0) -> Locator | None:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        for loc in locators:
            try:
                cand = loc.first
                if await cand.count() > 0 and await cand.is_visible():
                    return cand
            except Exception:
                pass
        await asyncio.sleep(0.1)
    return None


async def goto_labyrinth(page: Page) -> None:
    candidates = [
        page.locator('[aria-label="navigationBar.labyrinth"]').first,
        page.get_by_text(re.compile(r"^Labyrinth$", re.I)).first,
        page.get_by_text(re.compile(r"Labyrinth", re.I)).first,
    ]
    target = await first_visible(candidates, timeout=3.0)
    if target is None:
        return
    try:
        await target.click(timeout=2500)
    except Exception:
        try:
            await target.click(force=True, timeout=2500)
        except Exception:
            pass
    await page.wait_for_timeout(800)


async def open_escape_dialog(page: Page) -> None:
    btn_candidates = [
        page.get_by_role("button", name=re.compile(r"^\s*Escape\s*$", re.I)).first,
        page.get_by_role("button", name=re.compile(r"Escape\s*Labyrinth", re.I)).first,
        page.get_by_text(re.compile(r"^\s*Escape\s*$", re.I)).first,
        page.get_by_text(re.compile(r"Escape\s*Labyrinth", re.I)).first,
    ]
    btn = await first_visible(btn_candidates, timeout=3.0)
    if btn is None:
        raise RuntimeError("Escape button not found")

    try:
        await btn.click(timeout=2500)
    except Exception:
        try:
            await btn.click(force=True, timeout=2500)
        except Exception:
            box = await btn.bounding_box()
            if not box:
                raise
            await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    await page.wait_for_timeout(600)


async def dump_dialog(page: Page, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)

    dialogs = page.locator('[role="dialog"], div.MuiDialog-root, div[class*="DialogModal_"]')
    dialog_count = await dialogs.count()

    payload: dict[str, Any] = {
        "url": page.url,
        "title": await page.title(),
        "dialogs": [],
    }

    visible_dialog = None

    for i in range(dialog_count):
        dlg = dialogs.nth(i)
        try:
            if not await dlg.is_visible():
                continue
        except Exception:
            continue

        visible_dialog = dlg
        try:
            html = await dlg.evaluate("(el) => el.outerHTML")
        except Exception:
            html = ""

        entry: dict[str, Any] = {
            "index": i,
            "text": await locator_text(dlg, limit=2000),
            "html": html[:30000],
            "clickables": [],
        }

        clickables = dlg.locator('button, [role="button"], .MuiButtonBase-root, [class*="Button_"], div, span')
        count = await clickables.count()

        for j in range(count):
            node = clickables.nth(j)
            try:
                if not await node.is_visible():
                    continue
                txt = await locator_text(node, limit=120)
                if not txt:
                    continue
                box = await node.bounding_box()
                role = await node.get_attribute("role")
                cls = await node.get_attribute("class")
                tag = await node.evaluate("(el) => el.tagName.toLowerCase()")
                entry["clickables"].append({
                    "tag": tag,
                    "text": txt,
                    "role": role or "",
                    "class": norm(cls)[:300],
                    "box": box,
                })
            except Exception:
                pass

        payload["dialogs"].append(entry)

    (outdir / "escape_dialog_dump.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    await page.screenshot(path=str(outdir / "escape_full.png"), full_page=True)

    if visible_dialog is not None:
        try:
            await visible_dialog.screenshot(path=str(outdir / "escape_dialog.png"))
        except Exception:
            pass

    html = await page.content()
    (outdir / "escape_page.html").write_text(html, encoding="utf-8")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-url", required=True)
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--browser-channel", default="chrome")
    ap.add_argument("--outdir", default="escape_debug")
    args = ap.parse_args()

    outdir = Path(args.outdir)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, channel=args.browser_channel)
        context = await browser.new_context(storage_state=args.state_file, viewport={"width": 1600, "height": 1000})
        page = await context.new_page()

        await page.goto(args.game_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await goto_labyrinth(page)
        await open_escape_dialog(page)
        await dump_dialog(page, outdir)

        print(f"wrote debug files to: {outdir}")
        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
