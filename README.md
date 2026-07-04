# 🌙 Ellis

> **A beautiful Telegram uptime monitoring bot that quietly watches over your infrastructure.**

Ellis is a Telegram-first uptime monitoring bot designed to keep an eye on your websites, servers, APIs, DNS records, ports, and more—all without requiring a separate dashboard.

She lives directly in your Telegram chat, sending instant alerts whenever something goes down and letting you know the moment it's back online.

Whether you're managing a personal project, a homelab, or production infrastructure, Ellis helps you stay informed with a clean interface and reliable monitoring.

---

## ✨ Features

### 🔎 8 Monitoring Types

- 🌐 HTTP(S)
- 🔍 Keyword
- 📡 Ping (ICMP)
- 🔌 TCP Port
- 💓 Heartbeat
- 🌍 DNS
- ⚙️ API (JSONPath)
- 📦 UDP

### 📱 Telegram-first

- Beautiful inline interface
- Interactive setup wizard
- No web dashboard
- No browser required

### 📊 Monitoring

- 24h / 7d / 30d uptime graphs
- Response time history
- Uptime percentage
- Manual health checks
- Pause & Resume
- Maintenance windows

### 🔔 Notifications

- Instant DOWN alerts
- Instant recovery notifications
- Optional repeated reminders
- Quiet monitoring with minimal spam

### ⭐ Premium

- **Free:** 3 monitors
- **Premium:** 10 monitors
- One-time purchase with **Telegram Stars**

---

## 📸 Preview

> Screenshots coming soon.

---

## 🚀 Installation

### Requirements

- Python 3.11+
- Telegram Bot Token
- Public IP *(only required for Heartbeat/UDP monitoring)*

### Clone

```bash
git clone https://github.com/yourusername/ellis.git
cd ellis
```

### Install

```bash
pip install -r requirements.txt
```

### Configure

```python
BOT_TOKEN = "YOUR_TOKEN"
```

### Run

```bash
python main.py
```

Open Telegram and send:

```
/start
```

---

## 🛠 Commands

| Command | Description |
|---------|-------------|
| `/start` | Open the main menu |
| `/list` | Show all monitors |
| `/add` | Add a new monitor |
| `/pause` | Pause monitoring |
| `/resume` | Resume monitoring |
| `/check` | Run an immediate check |
| `/stats` | Show statistics |
| `/graph` | Generate uptime graph |
| `/export` | Export monitors |
| `/settings` | Open settings |
| `/premium` | Premium information |

---

## 🏗 Architecture

```
Telegram
      │
      ▼
 Aiogram 3.x
      │
 ┌────┴────────────┐
 │                 │
Scheduler      Heartbeat Server
 │                 │
 └──────┬──────────┘
        │
 ┌─────────────────────────────┐
 │ HTTP │ API │ DNS │ Ping     │
 │ TCP  │ UDP │ Keyword │ HB   │
 └─────────────────────────────┘
              │
              ▼
         SQLite Database
```

---

## 📦 Tech Stack

- Python 3.11+
- Aiogram 3
- aiohttp
- aiosqlite
- dnspython
- jsonpath-ng
- matplotlib

---

## 📄 License

MIT License.

---

<div align="center">

### 🌙 Ellis

*Quietly watching over your infrastructure.*

</div>
