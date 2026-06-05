"""
runner.py — orchestration glue (the refined main.py).
=====================================================

Wires the pieces together with the same flow as the backend:

    pick a coherent fingerprint
        → launch a stealth driver on the chosen proxy / UA / viewport
        → replay a saved session if one is valid, else warm up on the homepage
        → navigate to the target and do the work
        → persist the session for next time

Everything runs inside a ``with StealthDriver(...)`` block so the browser is
closed even if the work raises.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from . import config, session, humanize
from .browser import StealthDriver
from .fingerprint import get_random_fingerprint

logger = logging.getLogger("stealth_client.runner")


def run() -> None:
    config.setup_logging()
    fp = get_random_fingerprint()
    host = (urlparse(config.TARGET_URL).hostname or "unknown").lower()
    key = session.session_key(fp["proxy"], host)

    # A rotating proxy hands out a different exit IP each session, so IP-bound
    # clearance can only be safely replayed on a direct (stable-IP) connection.
    same_ip = fp["proxy"] is None
    logger.info(f"target={config.TARGET_URL} proxy={fp['proxy'] or 'direct'} same_ip={same_ip}")

    with StealthDriver(proxy=fp["proxy"], user_agent=fp["user_agent"],
                       viewport=fp["viewport"]) as driver:
        try:
            driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT_S)
        except Exception:
            pass

        # 1. Try replaying a saved session; otherwise warm up to earn a fresh one.
        if not session.load_cookies(driver, config.HOMEPAGE, key, same_ip=same_ip):
            session.warm_up_session(driver, config.HOMEPAGE)

        # 2. Navigate to the target.
        logger.info(f"navigating → {config.TARGET_URL}")
        driver.get(config.TARGET_URL)
        humanize.random_delay()

        # 3. ───────────────────────────────────────────────────────────────────
        #    YOUR SCRAPING / CHECKOUT LOGIC GOES HERE.
        #    e.g. find a "Book Now" button, humanize.human_click(driver, btn), …
        logger.info(f"loaded page title: {driver.title!r}")
        # ────────────────────────────────────────────────────────────────────────

        # 4. Persist the session (clearance + auth) for the next run.
        session.save_cookies(driver, key, proxy=fp["proxy"])

    logger.info("done — browser closed cleanly")


if __name__ == "__main__":
    run()
