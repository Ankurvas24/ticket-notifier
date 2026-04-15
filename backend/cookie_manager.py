import os
import json
import logging

logger = logging.getLogger("ticketalert.cookie_manager")

# Path to the user-provided cookies file
COOKIES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bms_cookies.json")

async def inject_cookies_if_exist(context, session_id: str = "scraper"):
    """
    Look for bms_cookies.json in the project root. If it exists,
    read the cookies (exported from Cookie Editor extension) and 
    inject them into the given Playwright browser context.
    
    Returns True if cookies were injected, False otherwise.
    """
    # 1. Check if the file exists
    if not os.path.isfile(COOKIES_FILE):
        return False
        
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            raw_cookies = json.load(f)
            
        if not raw_cookies:
            logger.info(f"[{session_id}] bms_cookies.json is empty.")
            return False
            
        # 2. Translate Cookie-Editor JSON into Playwright's format
        playwright_cookies = []
        for c in raw_cookies:
            # Playwright requires strictly: name, value, url/domain, path, expires, httpOnly, secure, sameSite
            # It explicitly REJECTS properties like 'hostOnly', 'session', 'storeId', 'id'
            
            name = c.get("name")
            value = c.get("value")
            
            if not name or value is None:
                continue
                
            entry = {
                "name": name,
                "value": value
            }
            
            if "domain" in c:
                entry["domain"] = c["domain"]
            elif "url" in c:
                entry["url"] = c["url"]
                
            if "path" in c:
                entry["path"] = c["path"]
                
            if "expires" in c:
                # Some Chrome exporters export 'expirationDate' instead of 'expires'
                entry["expires"] = float(c["expires"])
            elif "expirationDate" in c:
                entry["expires"] = float(c["expirationDate"])
                
            if "httpOnly" in c:
                entry["httpOnly"] = bool(c["httpOnly"])
                
            if "secure" in c:
                entry["secure"] = bool(c["secure"])
                
            if "sameSite" in c:
                same_site = (c["sameSite"] or "unspecified").lower()
                if same_site in ["strict", "lax", "none"]:
                    entry["sameSite"] = same_site.capitalize() if same_site != "none" else "None"
                    
            playwright_cookies.append(entry)
            
        # 3. Inject into context
        if playwright_cookies:
            await context.add_cookies(playwright_cookies)
            
            # Log key bots cookies if present
            key_names = [c["name"] for c in playwright_cookies if any(k in c["name"].lower() for k in ["_abck", "bm_sz", "sess", "auth", "_cfuvid"])]
            
            logger.info(f"[{session_id}] 💉 INJECTED {len(playwright_cookies)} custom cookies from bms_cookies.json! Key tokens: {key_names}")
            return True
            
        return False
        
    except Exception as e:
        logger.error(f"[{session_id}] Failed to load bms_cookies.json: {e}")
        return False
