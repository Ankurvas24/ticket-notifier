"""
browser.py — hardened stealth Chrome driver (the refined browser_setup.py).
===========================================================================

Refinements over the original snippet:
  • Headless is configurable (``HEADLESS`` env). When enabled it uses the modern
    ``--headless=new`` mode, which is far less detectable than legacy headless
    and — unlike legacy — supports extensions, so proxy-auth still works.
  • Authenticated proxies actually work. Plain ``--proxy-server`` can't carry a
    username/password; we generate a tiny throw-away Chrome extension that
    answers the proxy auth challenge (a well-known technique). Falls back to a
    bare ``--proxy-server`` when no credentials are set.
  • Ships as a context manager (``with StealthDriver(...) as driver:``) so the
    browser is always closed, even on exceptions.
  • UA / viewport / platform come from a single coherent fingerprint.
"""

from __future__ import annotations

import logging
import os
import tempfile
import zipfile
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from . import config
from .fingerprint import get_random_fingerprint, has_proxy_auth, platform_for

logger = logging.getLogger("stealth_client.browser")


def _build_proxy_auth_extension(host: str, port: str, user: str, password: str) -> str:
    """
    Build a throw-away Chrome extension (.zip) that supplies proxy credentials,
    so an authenticated residential proxy works without selenium-wire.
    Returns the path to the generated .zip (caller may delete it after launch).
    """
    manifest = """
{
  "version": "1.0.0",
  "manifest_version": 2,
  "name": "ProxyAuth",
  "permissions": [
    "proxy", "tabs", "unlimitedStorage", "storage",
    "<all_urls>", "webRequest", "webRequestBlocking"
  ],
  "background": { "scripts": ["background.js"] },
  "minimum_chrome_version": "76.0.0"
}
"""
    background = f"""
var config = {{
  mode: "fixed_servers",
  rules: {{
    singleProxy: {{ scheme: "http", host: "{host}", port: parseInt({port}) }},
    bypassList: ["localhost"]
  }}
}};
chrome.proxy.settings.set({{ value: config, scope: "regular" }}, function() {{}});
function callbackFn(details) {{
  return {{ authCredentials: {{ username: "{user}", password: "{password}" }} }};
}}
chrome.webRequest.onAuthRequired.addListener(
  callbackFn, {{ urls: ["<all_urls>"] }}, ["blocking"]
);
"""
    fd, path = tempfile.mkstemp(suffix="_proxy_auth.zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", manifest)
        zf.writestr("background.js", background)
    return path


def get_stealth_driver(proxy: Optional[str] = None,
                       user_agent: Optional[str] = None,
                       viewport: Optional[tuple] = None,
                       headless: Optional[bool] = None) -> webdriver.Chrome:
    """
    Launch a hardened Chrome instance.

    Args mirror the original snippet but any omitted value is filled from a
    coherent random fingerprint. ``proxy`` is a ``host:port`` string; if proxy
    credentials are configured (PROXY_USERNAME/PASSWORD) an auth extension is
    attached automatically.
    """
    fp = get_random_fingerprint()
    proxy = proxy if proxy is not None else fp["proxy"]
    user_agent = user_agent or fp["user_agent"]
    viewport = viewport or fp["viewport"]
    headless = config.HEADLESS if headless is None else headless
    width, height = viewport

    options = Options()

    if headless:
        options.add_argument("--headless=new")  # modern headless; supports extensions

    # Standard hardening flags
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument(f"--window-size={width},{height}")
    options.add_argument(f"--user-agent={user_agent}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Proxy wiring
    proxy_ext_path = None
    if proxy:
        host, _, port = proxy.partition(":")
        if has_proxy_auth():
            proxy_ext_path = _build_proxy_auth_extension(
                host, port or "80", config.PROXY_USERNAME, config.PROXY_PASSWORD
            )
            options.add_extension(proxy_ext_path)
            logger.info(f"Proxy (authenticated) via extension → {proxy}")
        else:
            options.add_argument(f"--proxy-server=http://{proxy}")
            options.add_argument("--disable-extensions")
            logger.info(f"Proxy (no auth) → {proxy}")
    else:
        options.add_argument("--disable-extensions")

    driver = webdriver.Chrome(options=options)  # Selenium Manager resolves chromedriver

    # The temp extension is loaded into the running browser; the file on disk is
    # no longer needed and can be cleaned up.
    if proxy_ext_path:
        try:
            os.remove(proxy_ext_path)
        except OSError:
            pass

    _apply_stealth(driver, user_agent)
    logger.info(f"Driver up — UA={user_agent[:42]}… viewport={width}x{height} headless={headless}")
    return driver


def _apply_stealth(driver: webdriver.Chrome, user_agent: str) -> None:
    """Apply selenium-stealth patches (if available) + a manual webdriver patch."""
    try:
        from selenium_stealth import stealth
        stealth(
            driver,
            languages=config.LANGUAGES,
            vendor="Google Inc.",
            platform=platform_for(user_agent),
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
    except Exception as e:  # selenium-stealth missing or version mismatch
        logger.warning(f"selenium-stealth not applied ({e}); using manual patch only")

    # Always layer the manual navigator.webdriver removal on top.
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        })
    except Exception as e:
        logger.warning(f"CDP webdriver patch failed: {e}")


class StealthDriver:
    """
    Context manager around :func:`get_stealth_driver` so the browser is always
    closed. Exposes the live driver via ``.driver`` and proxies attribute access.

    Usage::

        with StealthDriver() as driver:
            driver.get("https://in.bookmyshow.com")
    """

    def __init__(self, proxy=None, user_agent=None, viewport=None, headless=None):
        self._kwargs = dict(proxy=proxy, user_agent=user_agent,
                            viewport=viewport, headless=headless)
        self.driver: Optional[webdriver.Chrome] = None

    def __enter__(self) -> webdriver.Chrome:
        self.driver = get_stealth_driver(**self._kwargs)
        return self.driver

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        return False  # never swallow exceptions
