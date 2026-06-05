"""
stealth_client — a standalone, hardened Selenium reference client.
==================================================================

This package is the Selenium counterpart to the project's primary Playwright
stack (``backend/``). It is intentionally ISOLATED: it has its own
``requirements.txt`` (selenium + selenium-stealth) so those deps never get
pulled into the Flask/Playwright deployment.

It refines the original five reference snippets
(browser_setup / rotation / session_manager / humanize / main) into a coherent
package, aligned to the project's BookMyShow target:

    config      env-driven configuration + shared pools (mirrors fingerprint.py)
    fingerprint coherent UA + viewport + proxy bundle
    browser     stealth Chrome driver (selenium-stealth + manual patches)
    session     cookie save/load (IP-aware) + Akamai warm-up
    humanize    human-like mouse / scroll / typing / delays
    runner      orchestration glue (build → drive → warm → work → persist)

Entry point: ``python run.py`` (see the sibling run.py / README.md).
"""

from .browser import StealthDriver, get_stealth_driver
from .fingerprint import get_random_fingerprint
from .runner import run

__all__ = ["StealthDriver", "get_stealth_driver", "get_random_fingerprint", "run"]
