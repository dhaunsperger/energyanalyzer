"""Headful, stealth-patched browser with the Playwright Inspector attached.

`playwright codegen` cannot apply playwright-stealth: it launches its own
browser and gives you no hook to install init scripts before the first
navigation, which is exactly when the evasions have to be in place. This is the
equivalent -- a stealth-patched context, opened headful, paused on the
Inspector so you get the same Record button and selector picker.

Usage (needs WSLg or an X server; DISPLAY must be set)::

    .venv/bin/python scripts/stealth_codegen.py ambit
    .venv/bin/python scripts/stealth_codegen.py https://example.com/plans

Then in the Inspector window: click **Record**, drive the funnel by hand, and
copy the generated calls into that REP's ``render``/``harvester`` in
``fetchers/rep_discovery.py``. Close the browser to end the session.

Note the recorder emits plain Playwright calls -- it has no idea stealth is
active -- so code copied out of it only behaves the same if the runtime applies
stealth too. Wire that in ``fetch_rendered_html`` before relying on it.

Kept deliberately manual and one-shot: this is for re-deriving a nav flow after
a site changes, roughly once a year, not something the refresh calls.
"""

from __future__ import annotations

import sys

SITES = {
    "ambit": "https://www.ambitenergy.com/",
    "txu": "https://www.txu.com/",
    "direct_energy": "https://shop.directenergy.com/",
    "champion": "https://championenergyservices.com/",
    "octopus": "https://octopusenergy.com/texas",
}


def main() -> int:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    target = sys.argv[1] if len(sys.argv) > 1 else "ambit"
    url = SITES.get(target, target)
    if not url.startswith("http"):
        print(f"Unknown site {target!r}. Known: {', '.join(sorted(SITES))}, or pass a URL.")
        return 2

    print(f"Opening {url} headful with stealth. Click Record in the Inspector.")
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # Opens the Inspector and blocks until you resume or close it.
        page.pause()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
