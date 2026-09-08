#!/usr/bin/env python3
"""Local regression tests for labyrinth navigation recovery; no network or account state."""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from milkyway_autolabyrinth import (
    ensure_probably_logged_in,
    goto_labyrinth_panel,
    wait_for_labyrinth_panel,
)


async def run_tests() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                """
                <style>.hidden { display: none }</style>
                <div class="NavigationBar_navigationBar_test">
                  <div class="NavigationBar_navigationLink_test hidden" role="button">
                    <svg aria-label="navigationBar.labyrinth"></svg><span>Labyrinth</span>
                  </div>
                  <div class="NavigationBar_navigationLink_test" role="button"
                       onclick="document.querySelector('main').innerHTML =
                         '<div class=LabyrinthPanel_test>Labyrinth Floor 3 Entries</div>'">
                    <svg aria-label="navigationBar.labyrinth"></svg><span>Labyrinth</span>
                  </div>
                </div>
                <main></main>
                """
            )
            await goto_labyrinth_panel(page, timeout_ms=4000)
            assert await wait_for_labyrinth_panel(page, timeout_ms=500)

            collapsed_page = await browser.new_page()
            await collapsed_page.set_content(
                """
                <button class="NavigationBar_navToggleButton_test"
                        onclick="document.querySelector('#links').style.display = 'block'">
                  Menu
                </button>
                <div class="NavigationBar_navigationBar_test">
                  <div id="links" style="display: none">
                    <div class="NavigationBar_navigationLink_test" role="button"
                         onclick="document.querySelector('main').innerHTML =
                           '<div class=LabyrinthPanel_test>Labyrinth Floor 4 Entries</div>'">
                      <svg aria-label="navigationBar.labyrinth"></svg><span>Labyrinth</span>
                    </div>
                  </div>
                </div>
                <main></main>
                """
            )
            await goto_labyrinth_panel(collapsed_page, timeout_ms=4000)
            assert await wait_for_labyrinth_panel(collapsed_page, timeout_ms=500)

            blank_page = await browser.new_page()
            await blank_page.set_content("<main></main>")
            try:
                await ensure_probably_logged_in(blank_page, timeout_ms=300)
            except RuntimeError as exc:
                message = str(exc)
                assert "game shell did not become ready" in message
                assert "nav_links=0" in message
            else:
                raise AssertionError("blank <main> was incorrectly accepted as a ready game shell")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(run_tests())
    print("labyrinth navigation recovery tests: PASS")
