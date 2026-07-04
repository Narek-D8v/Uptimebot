# 🌙 Ellis — Uptime Monitoring Telegram Bot

**Ellis** is a gentle yet vigilant uptime monitoring bot that lives in your Telegram chat. She keeps an eye on your websites, servers, APIs, and network services — and immediately lets you know if something goes wrong (or comes back to life).

Named after a soft-spoken guardian, Ellis watches over your infrastructure while you sleep, work, or just need some peace of mind.

---

## ✨ Features

- **8 monitor types:**
  - 🌐 **HTTP(S)** — checks a URL for an expected HTTP status code.
  - 🔍 **Keyword** — verifies the presence (or absence) of a string in the response body.
  - 📡 **Ping** — ICMP echo check to ensure a host is reachable.
  - 🔌 **Port** — checks if a TCP port is open (SMTP, FTP, databases, custom services…).
  - 💓 **Heartbeat** — listens for incoming requests from your cron jobs or external services (built‑in HTTP server).
  - 🌍 **DNS** — resolves domain names and verifies expected DNS records (A, MX, TXT, etc.).
  - ⚙️ **API** — validates JSON responses with JSONPath expressions and expected values.
  - 📦 **UDP** — sends and receives UDP datagrams (perfect for SNMP, game servers, etc.).

- **Intuitive Telegram interface** — everything via inline keyboards and a step‑by‑step setup wizard. No external dashboards required.
- **Uptime graphs** (24h / 7d / 30d) with response‑time plots and coloured status bands.
- **Smart notifications** — alerts when a monitor changes state (DOWN → UP / UP → DOWN) with optional repeated reminders while the issue persists.
- **Maintenance windows** — suppress alerts during planned downtimes (e.g., nightly backups).
- **Pause / resume** monitors with a single tap.
- **Statistics** — uptime percentage, average response time, and total checks for any monitor.
- **Free & Premium** — 3 monitors free forever; upgrade to 10 monitors with a one‑time payment of 5 Telegram Stars.
- **Export** — download your monitor list as JSON.

---

### Prerequisites
- Python 3.11+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) A server with a public IP if you want to use the **Heartbeat** or **UDP** monitors from the outside world.

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/ellis-bot.git
cd ellis-bot
2. Install dependencies
bash
pip install -r requirements.txt
3. Configure the bot
Edit main.py and replace the placeholder token with your own:

python
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
You can also change HEARTBEAT_PORT (default 8080) if needed.

4. Run Ellis
bash
python main.py
Ellis will start the Telegram polling and the built‑in heartbeat server (if configured). You’ll see logs in the console.

5. Start a chat
Open Telegram, find your bot, and send /start. Ellis will guide you from there.

###🧭 Usage
Main Menu
📋 Мои мониторы — list, pause, resume, check, or delete your monitors.

➕ Добавить монитор — step‑by‑step addition of any monitor type.

⚙️ Настройки — configure repeat alerts and maintenance windows.

💳 Premium — upgrade your monitor limit to 10.

###Commands
Command	Description
/start	Welcome message and main menu
/list	Show all your monitors
/add <type> <params>	Quick add (bypass wizard)
/pause <id>	Pause a monitor
/resume <id>	Resume a monitor
/check <id>	Run an immediate check
/delete <id>	Delete a monitor
/stats <id>	Show uptime stats for the last 24h
/graph <id>	Generate an uptime graph (24h)
/export	Export your monitors as JSON
/settings	Open settings
/premium	Information about premium
Adding a monitor (example – HTTP)
Press ➕ Добавить монитор → 🌐 HTTP(s)

Enter a name (or - for auto).

Enter the URL and optionally the expected status code (e.g., https://example.com 200).

Choose the check interval (minimum 60 seconds).

Confirm — Ellis will immediately test the URL and start monitoring.

Heartbeat monitors provide a unique URL like http://<your-server-ip>:8080/hb_abc123. Configure your cron job to curl that URL every N minutes, and Ellis will alert if it stops.

###⚙️ Configuration
All sensitive settings are in the top of main.py:

python
BOT_TOKEN = "YOUR_TOKEN"
DATABASE = "uptime_bot.db"
HEARTBEAT_PORT = 8080
DEFAULT_INTERVAL = 300  # 5 minutes
DEFAULT_TIMEOUT = 10    # seconds
MAX_FREE_MONITORS = 3
MAX_PREMIUM_MONITORS = 10
PREMIUM_PRICE_STARS = 5
You can adjust the monitoring concurrency by changing the semaphore inside scheduler_loop() (default 50 simultaneous checks).

###🗄️ Database
Ellis uses SQLite (with WAL mode) – no external database required. The file uptime_bot.db is created automatically on first run and contains three tables: users, monitors, and checks.

For 100+ users with many monitors, SQLite handles the load easily. If you ever need to scale beyond that, the architecture can be migrated to PostgreSQL by swapping the aiosqlite calls.

###🏗️ Architecture
Telegram Bot: aiogram 3.x with finite state machine (FSM) for dialogs.

HTTP Client: aiohttp ClientSession with connection pooling (kept alive for performance).

Background Scheduler: an infinite asyncio loop that respects each monitor’s interval, maintenance windows, and repeat‑alert logic.

Heartbeat Server: aiohttp web server on a separate port, only handles incoming heartbeat pings.

Monitoring Backends:

HTTP/Keyword/API → aiohttp

Ping → system ping subprocess (cross‑platform)

Port → asyncio.open_connection

DNS → dnspython async resolver

UDP → asyncio.DatagramProtocol

Heartbeat → timestamp comparison

Payments: Telegram Stars (native) – no external payment provider.

###📦 Dependencies
aiogram>=3.7

aiohttp

aiosqlite

dnspython

matplotlib

jsonpath-ng

All listed in requirements.txt.

###🤝 Contributing
Pull requests are welcome! For major changes, please open an issue first to discuss what you would like to change.

If you add a new monitor type, please:

Add the check function in the check_*.py style.

Extend the perform_check dispatcher.

Update the monitor_types_keyboard and the process_config handler.

###📜 License
MIT

###🌟 Acknowledgements
Inspired by UptimeRobot – the original uptime guardian.

Built with love for the Telegram community.

Special thanks to the open‑source libraries that make Ellis possible.

Ellis — quietly watching over your uptime, with a gentle touch and a vigilant eye.
