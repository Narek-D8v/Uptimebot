🌙 Ellis — Uptime Monitoring Telegram Bot

Ellis is a gentle yet vigilant uptime monitoring bot that lives inside your Telegram chat.

She quietly watches over your websites, servers, APIs, and network services, notifying you immediately whenever something goes wrong—or when everything is back online.

Named after a soft-spoken guardian, Ellis keeps your infrastructure safe while you sleep, work, or simply enjoy peace of mind.

✨ Features
🔎 8 Monitoring Types
🌐 HTTP(S) — Monitor a URL and verify its HTTP status code.
🔍 Keyword — Check whether a specific string exists (or does not exist) in the response body.
📡 Ping — ICMP reachability monitoring.
🔌 TCP Port — Verify that a TCP port is open and accepting connections.
💓 Heartbeat — Receive periodic requests from cron jobs or external services via the built-in HTTP server.
🌍 DNS — Validate DNS records (A, AAAA, MX, TXT, CNAME, etc.).
⚙️ API — Validate JSON responses using JSONPath expressions.
📦 UDP — Send and receive UDP packets (useful for SNMP, game servers, and custom protocols).
📱 Telegram-first Experience
Beautiful inline keyboard interface
Step-by-step setup wizard
No web dashboard required
Everything happens directly inside Telegram
📊 Statistics & Graphs
24-hour, 7-day and 30-day uptime graphs
Response time charts
Colored status timeline
Uptime percentage
Average response time
Total number of checks
🔔 Smart Notifications
Instant alerts when a monitor changes status
Optional repeated reminders while a service remains offline
Recovery notifications when a service comes back online
⚙️ Monitor Management
Pause or resume monitors with one tap
Maintenance windows
Manual health checks
JSON export/import support
⭐ Premium
Free: up to 3 monitors
Premium: up to 10 monitors
One-time payment of 5 Telegram Stars
🚀 Installation
Prerequisites
Python 3.11+
Telegram Bot Token from @BotFather
(Optional) A server with a public IP address if you plan to use Heartbeat or UDP monitors.
1. Clone the repository
git clone https://github.com/yourusername/ellis-bot.git
cd ellis-bot
2. Install dependencies
pip install -r requirements.txt
3. Configure the bot

Edit main.py and set your bot token:

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

You may also change the heartbeat server port if necessary:

HEARTBEAT_PORT = 8080
4. Start Ellis
python main.py

Ellis will start:

Telegram polling
Background monitoring scheduler
Built-in Heartbeat HTTP server (if enabled)

Logs will appear in the console.

5. Start chatting

Open Telegram, find your bot, and send:

/start

Ellis will guide you through the setup process.

🧭 Usage
Main Menu
📋 My Monitors

View, check, pause, resume or delete your monitors.

➕ Add Monitor

Create a new monitor using the interactive wizard.

⚙️ Settings

Configure:

notification reminders
maintenance windows
monitoring preferences
⭐ Premium

Upgrade your monitor limit.

⌨️ Commands
Command	Description
/start	Open the main menu
/list	Show all monitors
/add <type> <params>	Quick monitor creation
/pause <id>	Pause monitoring
/resume <id>	Resume monitoring
/check <id>	Run an immediate check
/delete <id>	Delete a monitor
/stats <id>	Show statistics
/graph <id>	Generate uptime graph
/export	Export monitors as JSON
/settings	Open settings
/premium	Premium information
🌐 Example: Adding an HTTP Monitor
Press ➕ Add Monitor
Select 🌐 HTTP(S)
Enter a monitor name (or - for automatic naming)
Enter the URL and optional expected status code

Example:

https://example.com 200
Choose the monitoring interval
Confirm

Ellis immediately performs the first health check and begins monitoring.

💓 Heartbeat Monitoring

Heartbeat monitors generate a unique endpoint, for example:

http://your-server-ip:8080/hb_abc123

Configure your cron job or external service to periodically send a request:

curl http://your-server-ip:8080/hb_abc123

If Ellis stops receiving requests within the expected interval, you'll receive an alert.

⚙️ Configuration

Most configuration values are located near the top of main.py.

BOT_TOKEN = "YOUR_TOKEN"

DATABASE = "uptime_bot.db"

HEARTBEAT_PORT = 8080

DEFAULT_INTERVAL = 300      # seconds
DEFAULT_TIMEOUT = 10        # seconds

MAX_FREE_MONITORS = 3
MAX_PREMIUM_MONITORS = 10

PREMIUM_PRICE_STARS = 5

Monitoring concurrency can be adjusted by modifying the semaphore inside scheduler_loop().

Default:

50 simultaneous checks
🗄️ Database

Ellis uses SQLite in WAL mode, requiring no external database.

The database is automatically created on first launch.

Tables:

users
monitors
checks

SQLite comfortably handles hundreds of users and thousands of checks.

If needed, the storage layer can later be migrated to PostgreSQL with minimal changes.

🏗️ Architecture
Telegram
     │
     ▼
Aiogram 3.x Bot
     │
     ├── FSM Dialogs
     ├── Inline UI
     ├── Scheduler
     │
     ├── HTTP(S)
     ├── Keyword
     ├── API
     ├── Ping
     ├── TCP Port
     ├── DNS
     ├── UDP
     └── Heartbeat
     │
SQLite Database
Core Components
Telegram Bot — Aiogram 3.x
FSM — Interactive setup dialogs
HTTP Client — aiohttp
Scheduler — Async monitoring loop
Heartbeat Server — Built-in aiohttp.web
Database — SQLite + WAL
Charts — Matplotlib
Payments — Telegram Stars
📦 Dependencies
aiogram>=3.7
aiohttp
aiosqlite
dnspython
jsonpath-ng
matplotlib

See requirements.txt for the complete dependency list.

🤝 Contributing

Pull requests are always welcome.

For significant changes, please open an issue first to discuss your proposal.

When adding a new monitor type, remember to:

implement the new check_* function;
register it inside perform_check();
update the monitor selection keyboard;
extend the setup wizard;
document the new feature.
📄 License

Licensed under the MIT License.

❤️ Acknowledgements

Inspired by UptimeRobot.

Built with ❤️ for the Telegram community.

Special thanks to all open-source projects that make Ellis possible.

🌙 Ellis

Quietly watching over your infrastructure — so you don't have to.
