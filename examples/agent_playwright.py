#!/usr/bin/env python3
"""Unattended agent: tunnel up, drive the site headless, tunnel down.

    python examples/agent_playwright.py intranet

Use this shape for scheduled jobs.  If the site needs an interactive login,
run it once with --headed, log in, quit — the profile keeps the session, and
subsequent headless runs reuse it.
"""

from __future__ import annotations

import argparse
import sys

from slaigpus.automation import SiteSession


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("site", help="site name from your slaigpus config")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    parser.add_argument("--shot", default="", help="save a screenshot here")
    args = parser.parse_args()

    with SiteSession(args.site, headless=not args.headed) as session:
        page = session.page
        page.goto(session.site.url, wait_until="domcontentloaded")

        # ---- your automation starts here -------------------------------
        print("title:", page.title())
        print("url:  ", page.url)

        # A couple of patterns you will probably want:
        #
        #   page.fill("#username", "alice")
        #   page.click("button[type=submit]")
        #   page.wait_for_selector(".dashboard")
        #
        #   rows = page.locator("table tbody tr")
        #   for i in range(rows.count()):
        #       print(rows.nth(i).inner_text())
        #
        #   # API calls reuse the browser's cookies:
        #   resp = page.request.get(session.site.url + "/api/items")
        #   print(resp.json())
        # ----------------------------------------------------------------

        if args.shot:
            page.screenshot(path=args.shot, full_page=True)
            print("screenshot:", args.shot)

    return 0


if __name__ == "__main__":
    sys.exit(main())
