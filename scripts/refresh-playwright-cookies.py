#!/usr/bin/env python3
"""Convert ~/.midway/cookie (Netscape format) to Playwright storage state JSON.

Run after `mwinit`, then call browser_set_storage_state to reload cookies
without restarting the MCP server.
"""
import json
import os
import sys
import time

MIDWAY_COOKIE_PATH = os.path.expanduser("~/.midway/cookie")
STORAGE_STATE_PATH = os.path.expanduser("~/.midway/playwright-storage-state.json")


def parse_netscape_cookies(cookie_file_path):
    """Parse Netscape cookie file into Playwright cookie dicts."""
    cookies = []
    with open(cookie_file_path) as f:
        for line in f:
            line = line.strip()
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#") or not line:
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            domain, _, path, secure_flag, expires, name, value = fields[:7]
            cookies.append({
                "name": name,
                "value": value.strip(),
                "domain": domain,
                "path": path,
                "expires": int(expires) if int(expires) != 0 else -1,
                "httpOnly": http_only,
                "secure": secure_flag == "TRUE",
                "sameSite": "None" if secure_flag == "TRUE" else "Lax",
            })
    return cookies


def main():
    if not os.path.exists(MIDWAY_COOKIE_PATH):
        sys.exit(f"Error: {MIDWAY_COOKIE_PATH} not found. Run `mwinit` first.")

    cookies = parse_netscape_cookies(MIDWAY_COOKIE_PATH)
    if not cookies:
        sys.exit(f"Error: No cookies parsed from {MIDWAY_COOKIE_PATH}.")

    expired = [c for c in cookies if 0 < c["expires"] < time.time()]
    if expired:
        print(f"Warning: {len(expired)} cookie(s) already expired. Run `mwinit` to refresh.")

    # Write with restrictive permissions
    fd = os.open(STORAGE_STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"cookies": cookies, "origins": []}, f, indent=2)

    print(f"Wrote {len(cookies)} cookies to {STORAGE_STATE_PATH}")


if __name__ == "__main__":
    main()
