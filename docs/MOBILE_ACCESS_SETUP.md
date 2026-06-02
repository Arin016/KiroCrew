# Mobile Dashboard Access (AEA + Tunnels)

Access the KiroClaw dashboard from your phone using AEA (Amazon Employee Access) and Amazon Tunnels.

## Prerequisites

- KiroClaw running on a Cloud Desktop (see [REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md))
- [AWS Tunnels CLI](https://code.amazon.com/packages/PdebieTunnels)
- AEA installed on your phone ([iOS](https://apps.apple.com/app/amazon-employee-access/id1446115066) / [Android](https://play.google.com/store/apps/details?id=com.amazon.aea))

## Automated Setup (Recommended)

KiroClaw v2.6+ includes built-in tunnel management via the `tunnel/` package. The gateway automatically spawns and manages a tunnel when enabled.

### 1. Install Tunnels CLI

```bash
toolbox registry add s3://tunnels-toolbox-prod/tools.json
toolbox install tunnels
```

### 2. Enable tunnel in config

```json
{
  "tunnel": {
    "enabled": true,
    "name_mode": "username",
    "name_override": ""
  }
}
```

| Field | Values | Default | Description |
|-------|--------|---------|-------------|
| `enabled` | bool | `false` | Spawn tunnel on gateway startup |
| `name_mode` | `username` / `hash` | `username` | `username` returns just "kiroclaw" (Tunnels CLI auto-prefixes `<user>-`); `hash` returns "kiroclaw-<hostHash>" |
| `name_override` | string | `""` | Takes precedence when non-empty, regardless of `name_mode` |

### 3. Restart the gateway

```bash
kiroclaw gateway
```

The `TunnelManager` handles the full lifecycle:
- Spawns the tunnel process on startup
- Detects Midway authentication state (prompts `mwinit` if expired)
- Exponential backoff on connection failures
- Dynamically adds/removes the tunnel CORS origin
- Presigned links automatically use the tunnel URL when connected
- Double auth: Midway OIDC (tunnel layer) + dashboard token (app layer)

### 4. Check tunnel status

```bash
curl http://localhost:7777/api/tunnel/status
```

The dashboard also shows a TunnelStatus pill in the top bar (green = connected, yellow = connecting, red = disconnected).

## Manual Setup (Deprecated)

For environments where automatic tunnel management is not desired, you can still manage the tunnel manually.

### 1. Create a named tunnel

```bash
tunnel create 7777 --name kiroclaw
# → https://<alias>-kiroclaw.w.tunnels.<alias>.people.aws.dev
```

Named tunnels produce a stable URL that doesn't change between restarts. The tunnel proxies HTTPS traffic to your local KiroClaw dashboard port (7777).

### 2. Configure dashboard URL

Set `dashboard.url` in `~/.kiroclaw/config.json` to the tunnel URL:

```json
{
  "dashboard": {
    "url": "https://<alias>-kiroclaw.w.tunnels.<alias>.people.aws.dev"
  }
}
```

This tells the Slack bot to generate presigned links using the tunnel URL instead of `localhost`.

### 3. Keep the tunnel running

The tunnel CLI must stay connected for mobile access to work. Run it in a tmux session or as a background process:

```bash
tmux new -s tunnel
tunnel create 7777 --name kiroclaw
# Ctrl+B, D to detach
```

## Usage

1. Type `/kiroclaw dashboard` (or `/kiroclaw dashboard 6h` for a longer session) in your KiroClaw Slack DM
2. The bot DMs you a presigned link: `https://<tunnel-url>/?token=...`
3. Tap the link on your phone — it opens in AEA browser
4. Midway authentication flows through the tunnel's OIDC cookie

## Session Duration

| Layer | Duration | Notes |
|-------|----------|-------|
| AEA cookie | 2 hours | Refreshed automatically on every AEA unlock (screen lock/unlock cycle) |
| Midway session cookie | ~20 hours | Gates auto-refresh of AEA cookie |
| **`/kiroclaw dashboard` token** | **1 hour (default)** | **Practical limit** — configurable: `/kiroclaw dashboard 6h`, max `/kiroclaw dashboard 20h` |
| Presigned link click window | 5 minutes | Must click before it expires |

The AEA cookie has a 2-hour TTL, but in practice you'll never hit it — every time you lock and unlock your phone, AEA re-authenticates and gets a fresh cookie. Normal mobile usage (switching apps, checking notifications) naturally keeps the cookie alive.

The **dashboard presigned token** is the real session limit. Use `/kiroclaw dashboard 6h` or `/kiroclaw dashboard 20h` for longer sessions. When the token expires, generate a new link with `/kiroclaw dashboard`.

## Dependencies

The automated tunnel integration ([CR-275510342](https://code.amazon.com/reviews/CR-275510342)) requires:

1. **Tunnels CLI** — `toolbox install tunnels`
2. **Active Midway session** — `mwinit -o` (TunnelManager detects expired sessions and warns)

Earlier manual-mode prerequisites (both merged to `beta-braveheart`):

1. **[CR-269276380](https://code.amazon.com/reviews/CR-269276380)** — fixes `send_dashboard_link()` to preserve the HTTPS scheme and omit default ports from presigned URLs.
2. **[CR-269744499](https://code.amazon.com/reviews/CR-269744499)** — adds mobile responsive layout to the dashboard UI (viewport meta, touch-friendly controls, session toggle).

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `/kiroclaw dashboard` link still shows `localhost` | Verify `dashboard.url` is set in config.json and restart the gateway |
| Link opens but shows "token expired" | Click within 5 minutes of generating. Use `/kiroclaw dashboard` again for a fresh link |
| Tunnel disconnects | Reconnect with `tunnel create 7777 --name kiroclaw` — same URL is reused |
| Dashboard loads but layout is broken | Ensure you're on latest `beta-braveheart` — mobile responsive layout was added in [CR-269744499](https://code.amazon.com/reviews/CR-269744499) |
| AEA can't reach the URL | Verify the tunnel CLI is running and connected on your Cloud Desktop |

## References

- [Midway Sentinel wiki](https://w.amazon.com/bin/view/Users/scatcher/skippy/articles/MidwaySentinel/) — AEA cookie, SSH cert, and session cookie lifetimes
- [token_auth.py](https://code.amazon.com/packages/KiroClaw/blobs/beta-braveheart/--/src/kiro_claw/dashboard/token_auth.py) — dashboard token TTL and link window constants
- [AWS Tunnels README](https://code.amazon.com/packages/PdebieTunnels/blobs/main/--/README.md) — install, usage, architecture
- [Tunnels Slack channel](https://amazon.enterprise.slack.com/archives/C0AQVTNJW4Q) — #tunnels-interest
- [REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md) — Cloud Desktop setup guide
- [AEA](https://aea.amazon.com) — Amazon Employee Access mobile browser
