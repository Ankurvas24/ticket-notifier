# Selenium Stealth Client

A standalone, hardened **Selenium** reference client for the TicketAlert project.

It is the Selenium counterpart to the primary **Playwright** stack in
[`../backend/`](../backend). The backend (Flask + Playwright) is what you deploy;
this client is a clean, self-contained alternative you can run locally when you
want a real, visible Chrome window driving BookMyShow — useful for debugging
bot-checks, capturing a fresh session, or working through an OTP screen by hand.

It refines the original five reference snippets
(`browser_setup` / `rotation` / `session_manager` / `humanize` / `main`) into one
coherent package.

## Why it's isolated

Selenium and `selenium-stealth` are **not** in the project's main
`requirements.txt` on purpose — the deployed server runs Playwright and should
never pull a second browser-automation stack. This package keeps its own
`requirements.txt`, so the two never collide.

## Layout

```
selenium-client/
├── run.py                 # entry point: `python run.py`
├── requirements.txt       # selenium + selenium-stealth (isolated)
└── stealth_client/
    ├── config.py          # env-driven config + shared UA/viewport/IP-bound pools
    ├── fingerprint.py      # coherent UA + viewport + proxy bundle  (← rotation.py)
    ├── browser.py          # stealth Chrome driver + proxy auth      (← browser_setup.py)
    ├── session.py          # cookie save/load (IP-aware) + warm-up   (← session_manager.py)
    ├── humanize.py         # mouse / scroll / typing / delays        (← humanize.py)
    └── runner.py           # orchestration                           (← main.py)
```

## Quick start

```bash
cd selenium-client
pip install -r requirements.txt
python run.py
```

Then add your scraping / checkout logic where `runner.py` marks
`YOUR SCRAPING / CHECKOUT LOGIC GOES HERE`.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `HOMEPAGE` | `https://in.bookmyshow.com` | Warm-up page (earns Akamai/CF clearance). |
| `TARGET_URL` | = `HOMEPAGE` | Page to drive after warm-up. |
| `HEADLESS` | `false` | `1`/`true` → `--headless=new` (server/cron). |
| `PROXY_SERVER` | — | Single proxy `host:port`. |
| `PROXY_POOL` | — | Comma/space list of `host:port` (rotated). |
| `PROXY_USERNAME` / `PROXY_PASSWORD` | — | Proxy creds (auto-wired via a generated auth extension). |
| `SELENIUM_COOKIE_DIR` | `~/.ticketalert/selenium_cookies` | Where sessions are saved. |
| `SELENIUM_COOKIE_TTL_S` | `1200` | Saved-session TTL (seconds). |
| `WARMUP_SETTLE_S` | `5` | Seconds to let sensor scripts settle. |
| `PAGE_LOAD_TIMEOUT_S` | `45` | Per-navigation timeout. |

These mirror the backend's env vars, so the same proxy/cookie configuration
works for both clients.

## How it behaves

1. Picks a **coherent fingerprint** — the UA, viewport and `sec-ch-ua-platform`
   all agree (a mismatch is a classic bot tell).
2. Launches **stealth Chrome** (`selenium-stealth` + a manual `navigator.webdriver`
   patch). Authenticated proxies work via a throw-away auth extension.
3. **Replays a saved session** if one is still valid; otherwise **warms up** on
   the homepage to earn fresh clearance.
4. Navigates to the target, does the work, then **persists the session**.

### IP-binding rule (important)

Cloudflare/Akamai clearance (`cf_clearance`, `_abck`, `bm_sz`, …) is bound to the
exit IP that earned it. This client only replays those cookies on a **stable IP**
(a direct connection); through a rotating proxy they are stripped on load and a
fresh handshake is performed. This matches the rule enforced in the backend's
`cookie_manager.py`.

### Headless note

Many WAFs flag legacy headless Chrome, so the default is a **visible** window.
When you do need headless (servers, cron), `HEADLESS=1` uses the modern
`--headless=new` mode, which is much less detectable and still supports the
proxy-auth extension.
