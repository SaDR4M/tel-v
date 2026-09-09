# RickPanel Telegram Bot

A Telegram bot with 4 menu buttons:

1. **Price List / Purchase** — browse plans, "pay" manually, upload receipt → admin approves → account auto-created on the panel
2. **My Subscription** — live status/usage pulled from the panel API
3. **How to Use** — static setup guide for common VPN clients
4. **Support** — forwards user messages to your admin chat; configured admins can reply to the forwarded request and the bot delivers that reply to the user

Built with **aiogram 3** + **SQLite** (no external DB needed). Payments are handled manually: the user sends a receipt screenshot, an admin taps Approve/Reject, and on approval the bot calls the panel API to create the account.

## 1. Configure

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | What it is |
|---|---|
| `BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `ADMIN_CHAT_ID` | Chat/group ID where receipts get posted for approval (add the bot there and use [@userinfobot](https://t.me/userinfobot) or a getUpdates call to find the ID) |
| `ADMIN_USER_IDS` | Comma-separated Telegram user IDs allowed to approve/reject orders and reply to support requests |
| `PANEL_BASE_URL` | e.g. `https://api.rickpanel.io` |
| `PANEL_ADMIN_USERNAME` / `PANEL_ADMIN_PASSWORD` | Panel admin credentials (used to fetch an API token) |
| `SUPPORT_CONTACT` | Shown in the Support menu, e.g. `@yourhandle` |

Edit **`config/plans.json`** to set your actual prices, data caps, and durations — no code changes needed.

Also edit the payment instructions placeholder in `handlers/purchase.py` (`cb_buy` function) — put your real wallet address / payment details there.

## 2. Run locally (for testing)

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

## 3. Hosting options (you weren't sure — here's the rundown)

You need a small **always-on Linux server** since this bot uses long-polling (no public URL/webhook required, so no domain or SSL needed).

| Option | Cost | Notes |
|---|---|---|
| **Hetzner Cloud CX22** | ~€4/mo | Cheapest reliable EU VPS, plenty for this bot |
| **Contabo VPS S** | ~€5/mo | More RAM/disk for the price, slightly less polished network |
| **DigitalOcean Droplet** | $6/mo | Good docs, easy to use if new to VPS |
| **Oracle Cloud Free Tier** | Free | Free ARM VM forever, but setup is fiddlier |
| **A Raspberry Pi at home** | Free (hardware) | Fine for low volume, but ties uptime to your home internet |

Any of these works — the bot itself is lightweight (no GPU, minimal RAM, <100MB disk).

### Deploying on a VPS (any provider)

```bash
# On the server
sudo apt update && sudo apt install -y python3-venv git
git clone <your-repo-or-scp-the-folder-over>
cd telegram_vpn_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env   # fill in your values
```

### Keep it running with systemd (recommended)

Create `/etc/systemd/system/vpnbot.service`:

```ini
[Unit]
Description=Telegram VPN Bot
After=network.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/telegram_vpn_bot
ExecStart=/home/youruser/telegram_vpn_bot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vpnbot
sudo systemctl status vpnbot   # check it's running
journalctl -u vpnbot -f        # watch live logs
```

## 4. If your panel's API differs from Marzban's schema

Everything panel-specific lives in **`panel_api.py`** — that's the only file you should need to touch if endpoint paths, auth flow, or payload shape differ from what's here. It currently assumes:

- `POST /api/admin/token` (form-encoded username/password) → `{"access_token": "..."}`
- `POST /api/user` to create
- `GET /api/user/{username}` to fetch
- `PUT /api/user/{username}` to modify

## Project structure

```
telegram_vpn_bot/
├── main.py                 # entry point, wires up routers
├── database.py             # SQLite: user <-> panel account mapping, orders
├── panel_api.py            # panel API client — edit here if your API differs
├── config/
│   ├── settings.py         # loads .env + plans.json
│   └── plans.json          # your pricing/data plans — edit freely
└── handlers/
    ├── menu.py             # /start, main menu
    ├── purchase.py         # plan selection + receipt upload
    ├── admin_review.py     # admin approve/reject buttons
    ├── mysub.py            # "My Subscription" status lookup
    ├── docs_support.py     # docs + support message forwarding
    └── keyboards.py        # all inline keyboards
```
