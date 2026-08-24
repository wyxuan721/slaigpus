#!/usr/bin/env python3
"""Attach to a browser you already opened and logged into by hand.

    # terminal 1 — open it, log in, leave it running
    slaigpus viewer intranet --cdp

    # terminal 2
    python examples/agent_attach_cdp.py

Best fit for sites behind SSO/MFA, where scripting the login is not worth it.
The tunnel is owned by `slaigpus viewer`, so this script starts nothing and
cleans up nothing — it just borrows the live session.
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

from slaigpus.automation import attach_over_cdp


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--url", default="", help="navigate before acting")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        try:
            browser = attach_over_cdp(playwright, args.cdp_port)
        except Exception as exc:  # noqa: BLE001
            print(
                f"could not attach on port {args.cdp_port}: {exc}\n"
                f"is `slaigpus viewer <site> --cdp` running?",
                file=sys.stderr,
            )
            return 1

        # connect_over_cdp gives back the *existing* context, cookies and all.
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        if args.url:
            page.goto(args.url, wait_until="domcontentloaded")

        print("title:", page.title())
        print("url:  ", page.url)

        # ---- your automation starts here -------------------------------
        # Everything the logged-in user can see, you can now read:
        #
        #   page.click("text=Reports")
        #   page.wait_for_load_state("networkidle")
        #   print(page.locator("#total").inner_text())
        # ----------------------------------------------------------------

        # Detach without closing the user's window.
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
