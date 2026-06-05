"""
humanize.py — human-like interaction helpers (the refined humanize.py).
=======================================================================

Refinements over the original snippet: an off-centre landing point (dead-centre
clicks are a bot tell), a natural scroll sequence, and character-by-character
typing with randomised key cadence — mirroring the Playwright stack's
``humanizer.js`` so both clients behave alike.
"""

from __future__ import annotations

import random
import time

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys  # noqa: F401  (handy for callers)


def random_delay(min_sec: float = 1.5, max_sec: float = 4.0) -> None:
    """Pause for a random human-reaction-time interval."""
    time.sleep(random.uniform(min_sec, max_sec))


def human_like_move(driver, element) -> None:
    """
    Move the cursor onto ``element`` landing slightly off-centre, then dwell —
    avoiding the dead-centre, zero-dwell signature of an automated click.
    """
    try:
        action = ActionChains(driver)
        # Land off-centre within the element's bounds.
        w = element.size.get("width", 10)
        h = element.size.get("height", 10)
        dx = int(w * random.uniform(-0.25, 0.25))
        dy = int(h * random.uniform(-0.25, 0.25))
        action.move_to_element_with_offset(element, dx, dy)
        action.pause(random.uniform(0.12, 0.35))
        action.perform()
    except Exception:
        # Fall back to a plain hover if offset moves aren't supported.
        try:
            ActionChains(driver).move_to_element(element).perform()
        except Exception:
            pass


def human_scroll(driver, distance: int = 400) -> None:
    """Scroll down in a few uneven steps, occasionally drifting back up."""
    steps = random.randint(3, 6)
    for _ in range(steps):
        driver.execute_script(
            "window.scrollBy(0, arguments[0]);",
            distance // steps + random.randint(-20, 20),
        )
        time.sleep(random.uniform(0.05, 0.18))
    if random.random() > 0.5:
        driver.execute_script("window.scrollBy(0, arguments[0]);", -random.randint(40, 120))
        time.sleep(random.uniform(0.2, 0.5))


def human_click(driver, element) -> None:
    """Hover naturally onto the element, brief pause, then click."""
    human_like_move(driver, element)
    time.sleep(random.uniform(0.1, 0.3))
    element.click()


def human_type(element, text: str) -> None:
    """Type ``text`` one character at a time with randomised inter-key delays."""
    try:
        element.click()
    except Exception:
        pass
    time.sleep(random.uniform(0.08, 0.2))
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.04, 0.13))
