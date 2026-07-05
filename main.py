python
import asyncio
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

import aiohttp
import aiosqlite
import aiosmtplib
from email.message import EmailMessage
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BufferedInputFile, LabeledPrice, PreCheckoutQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ---------- КОНФИГУРАЦИЯ ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

DATABASE = "uptime_bot.db"
PORT = int(os.environ.get("PORT", 8080))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}").rstrip('/')

DEFAULT_TIMEOUT = 10
MAX_FREE_MONITORS = 3
MAX_PREMIUM_MONITORS = 10
PREMIUM_PRICE_STARS = 5
MIN_INTERVAL_FREE = 60
MIN_INTERVAL_PREMIUM = 30
DEFAULT_INTERVAL = 300

# SMTP
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM")

VIP_USER_ID = 5457847440  # пользователь с автоматическим премиумом

# ---------- ИНИЦИАЛИЗАЦИЯ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
http_session: Optional[aiohttp.ClientSession] = None

GREETINGS = [
    "👋 Привет! Я Элис, твой страж сайтов и серверов. Всё под контролем!",
    "🌙 Добро пожаловать! Я не сплю, пока твои сервисы работают.",
    "✨ Здравствуй! Доверь мне мониторинг – я предупрежу, если что-то случится.",
    "🛡 Приветствую! Я твой цифровой ангел-хранитель."
]

# ---------- БАЗА ДАННЫХ ----------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                email TEXT,
                monitor_limit INTEGER DEFAULT 3,
                is_premium BOOLEAN DEFAULT 0,
                alert_repeat INTEGER DEFAULT 0,
                maintenance_from TEXT,
                maintenance_to TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('http','keyword','ping','port','heartbeat','dns','api','udp')),
                name TEXT,
                config TEXT NOT NULL DEFAULT '{}',
                interval_seconds INTEGER NOT NULL DEFAULT 300,
                is_paused BOOLEAN DEFAULT 0,
                last_status TEXT,
                is_up BOOLEAN,
                last_checked TIMESTAMP,
                consecutive_failures INTEGER DEFAULT 0,
                alert_until TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                status_code INTEGER,
                response_time_ms REAL,
                is_up BOOLEAN,
                details TEXT,
                checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (monitor_id) REFERENCES monitors(id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_checks_monitor_time ON checks(monitor_id, checked_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_monitors_user ON monitors(user_id)")
        await db.commit()

async def ensure_vip_user():
    """Гарантирует премиум для VIP-пользователя даже после первого запуска."""
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "INSERT INTO users (user_id, chat_id, monitor_limit, is_premium) VALUES (?, 0, ?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET monitor_limit=?, is_premium=1",
            (VIP_USER_ID, MAX_PREMIUM_MONITORS, MAX_PREMIUM_MONITORS)
        )
        await db.commit()
    logging.info(f"VIP user {VIP_USER_ID} premium ensured")

async def register_user(user_id: int, chat_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        limit = MAX_PREMIUM_MONITORS if user_id == VIP_USER_ID else MAX_FREE_MONITORS
        is_prem = 1 if user_id == VIP_USER_ID else 0
        await db.execute(
            "INSERT INTO users (user_id, chat_id, monitor_limit, is_premium) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET chat_id=?, monitor_limit=?, is_premium=?",
            (user_id, chat_id, limit, is_prem, chat_id, limit, is_prem)
        )
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
            return await cur.fetchone()

async def set_user_email(user_id: int, email: str):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE users SET email=? WHERE user_id=?", (email, user_id))
        await db.commit()

async def set_user_setting(user_id: int, setting: str, value):
    async with aiosqlite.connect(DATABASE) as db:
        if setting == "alert_repeat":
            await db.execute("UPDATE users SET alert_repeat=? WHERE user_id=?", (int(value), user_id))
        elif setting in ("maintenance_from", "maintenance_to"):
            await db.execute(f"UPDATE users SET {setting}=? WHERE user_id=?", (value, user_id))
        await db.commit()

async def get_active_monitor_count(user_id: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute("SELECT COUNT(*) FROM monitors WHERE user_id=? AND is_paused=0", (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def can_add_monitor(user_id: int) -> bool:
    count = await get_active_monitor_count(user_id)
    user = await get_user(user_id)
    limit = user[3] if user else MAX_FREE_MONITORS
    return count < limit

async def add_monitor(user_id: int, monitor_type: str, name: str, config: dict, interval: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute(
            "INSERT INTO monitors (user_id, type, name, config, interval_seconds) VALUES (?,?,?,?,?)",
            (user_id, monitor_type, name, json.dumps(config), interval)
        )
        await db.commit()
        return cur.lastrowid

async def delete_monitor(monitor_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE) as db:
        cur = await db.execute("DELETE FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id))
        await db.commit()
        return cur.rowcount > 0

async def get_monitor(monitor_id: int, user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute("SELECT * FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id)) as cur:
            return await cur.fetchone()

async def get_user_monitors(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT id, type, name, config, interval_seconds, is_paused, is_up, last_checked FROM monitors WHERE user_id=?",
            (user_id,)
        ) as cur:
            return await cur.fetchall()

async def get_all_active_monitors():
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT m.id, m.user_id, m.type, m.config, m.interval_seconds, m.last_checked, m.is_up, m.alert_until, u.chat_id, u.alert_repeat, u.maintenance_from, u.maintenance_to, u.is_premium "
            "FROM monitors m JOIN users u ON m.user_id=u.user_id WHERE m.is_paused=0"
        ) as cur:
            return await cur.fetchall()

async def update_monitor_status(monitor_id: int, is_up: bool, status_text: str, response_time_ms: float, details: str = ""):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "UPDATE monitors SET last_status=?, is_up=?, last_checked=?, consecutive_failures=CASE WHEN ? THEN 0 ELSE consecutive_failures+1 END WHERE id=?",
            (status_text, is_up, datetime.utcnow(), is_up, monitor_id)
        )
        await db.execute(
            "INSERT INTO checks (monitor_id, status_code, response_time_ms, is_up, details) VALUES (?,?,?,?,?)",
            (monitor_id, 0 if not isinstance(status_text, int) else status_text, response_time_ms, is_up, details)
        )
        await db.commit()

async def set_monitor_pause(monitor_id: int, user_id: int, paused: bool):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE monitors SET is_paused=? WHERE id=? AND user_id=?", (int(paused), monitor_id, user_id))
        await db.commit()

async def get_monitor_stats(monitor_id: int, hours: int):
    since = datetime.utcnow() - timedelta(hours=hours)
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(is_up), AVG(response_time_ms) FROM checks WHERE monitor_id=? AND checked_at>=?",
            (monitor_id, since)
        ) as cur:
            total, up_count, avg_resp = await cur.fetchone()
        if total == 0:
            return {"total": 0, "uptime": 100.0, "avg_response_time": 0}
        uptime = (up_count / total) * 100 if total else 100.0
        return {"total": total, "uptime": round(uptime, 2), "avg_response_time": round(avg_resp, 1) if avg_resp else 0}

# ---------- ПРОВЕРКИ ----------
async def check_http(config: dict) -> tuple:
    url = config["url"]
    expected_status = config.get("expected_status", 200)
    method = config.get("method", "GET").upper()
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        async with http_session.request(method, url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
            status = resp.status
            resp_time = (time.monotonic() - start) * 1000
            is_up = (status == expected_status)
            return is_up, str(status), resp_time, ""
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)

async def check_keyword(config: dict) -> tuple:
    url = config["url"]
    keyword = config["keyword"]
    mode = config.get("mode", "present")
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
            body = await resp.text()
            resp_time = (time.monotonic() - start) * 1000
            found = keyword in body
            is_up = (found if mode == "present" else not found)
            return is_up, f"Keyword {'found' if found else 'not found'}", resp_time, f"Status: {resp.status}"
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)

async def check_ping(config: dict) -> tuple:
    host = config["host"]
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        if os.name == 'nt':
            cmd = ["ping", "-n", "1", "-w", str(timeout*1000), host]
        else:
            cmd = ["ping", "-c", "1", "-W", str(timeout), host]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout+2)
        resp_time = (time.monotonic() - start) * 1000
        if proc.returncode == 0:
            return True, "Host reachable", resp_time, stdout.decode().strip()
        else:
            return False, "Host unreachable", resp_time, stderr.decode().strip() or "No response"
    except asyncio.TimeoutError:
        resp_time = (time.monotonic() - start) * 1000
        return False, "Ping timeout", resp_time, ""
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)

async def check_port(config: dict) -> tuple:
    host = config["host"]
    port = config["port"]
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        resp_time = (time.monotonic() - start) * 1000
        return True, f"Port {port} open", resp_time, ""
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)

async def check_dns(config: dict) -> tuple:
    import dns.asyncresolver
    domain = config["domain"]
    record_type = config["record_type"]
    expected_value = config.get("expected_value")
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        resolver = dns.asyncresolver.Resolver()
        answers = await asyncio.wait_for(resolver.resolve(domain, record_type), timeout=timeout)
        values = [str(r) for r in answers]
        resp_time = (time.monotonic() - start) * 1000
        if not values:
            return False, "No records", resp_time, ""
        if expected_value:
            if expected_value in values:
                return True, f"Found {expected_value}", resp_time, str(values)
            else:
                return False, f"Expected {expected_value}, got {values}", resp_time, str(values)
        else:
            return True, f"Resolved: {values[0]}", resp_time, str(values)
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)

async def check_api(config: dict) -> tuple:
    url = config["url"]
    jsonpath_expr = config["jsonpath"]
    expected_value = config.get("expected_value")
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        from jsonpath_ng import parse
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            data = await resp.json()
            resp_time = (time.monotonic() - start) * 1000
            expr = parse(jsonpath_expr)
            matches = [match.value for match in expr.find(data)]
            if not matches:
                return False, "JSONPath no match", resp_time, ""
            actual = str(matches[0])
            if expected_value is not None and actual != str(expected_value):
                return False, f"Expected {expected_value}, got {actual}", resp_time, actual
            return True, f"OK: {actual}", resp_time, actual
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)

async def check_udp(config: dict) -> tuple:
    host = config["host"]
    port = config["port"]
    send_data = config.get("send_data", "").encode()
    expected_response = config.get("expected_response")
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: UDPEchoClientProtocol(expected_response),
            remote_addr=(host, port)
        )
        try:
            protocol.send_data = send_data
            transport.sendto(send_data)
            await asyncio.wait_for(protocol.received_event.wait(), timeout=timeout)
            resp_time = (time.monotonic() - start) * 1000
            if protocol.response:
                if expected_response:
                    if expected_response in protocol.response.decode(errors='ignore'):
                        return True, "UDP response matches", resp_time, protocol.response.decode(errors='ignore')
                    else:
                        return False, f"Unexpected response: {protocol.response[:50]}", resp_time, ""
                return True, "UDP response received", resp_time, protocol.response.decode(errors='ignore')
            else:
                return False, "No UDP response", resp_time, ""
        finally:
            transport.close()
    except asyncio.TimeoutError:
        resp_time = (time.monotonic() - start) * 1000
        return False, "UDP timeout", resp_time, ""
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)

class UDPEchoClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, expected=None):
        self.transport = None
        self.response = None
        self.received_event = asyncio.Event()
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, addr):
        self.response = data
        self.received_event.set()
    def error_received(self, exc):
        self.received_event.set()
    def connection_lost(self, exc):
        self.received_event.set()

async def check_heartbeat(config: dict) -> tuple:
    last_heartbeat = config.get("last_heartbeat")
    max_interval = config.get("max_interval", 600)
    if not last_heartbeat:
        return False, "No heartbeat received", 0, ""
    try:
        last = datetime.fromisoformat(last_heartbeat)
    except:
        return False, "Invalid timestamp", 0, ""
    now = datetime.utcnow()
    delta = (now - last).total_seconds()
    if delta <= max_interval:
        return True, f"Last heartbeat {delta:.0f}s ago", delta * 1000, ""
    else:
        return False, f"Heartbeat overdue: {delta:.0f}s > {max_interval}s", delta * 1000, ""

async def perform_check(monitor_id: int, monitor_type: str, config: dict) -> tuple:
    if monitor_type == "http":
        return await check_http(config)
    elif monitor_type == "keyword":
        return await check_keyword(config)
    elif monitor_type == "ping":
        return await check_ping(config)
    elif monitor_type == "port":
        return await check_port(config)
    elif monitor_type == "dns":
        return await check_dns(config)
    elif monitor_type == "api":
        return await check_api(config)
    elif monitor_type == "udp":
        return await check_udp(config)
    elif monitor_type == "heartbeat":
        return await check_heartbeat(config)
    else:
        return False, f"Unknown type {monitor_type}", 0, ""

# ---------- EMAIL ----------
async def send_email(to_email: str, subject: str, body: str):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD]):
        logging.warning("SMTP not configured")
        return
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")
    try:
        await aiosmtplib.send(msg, hostname=SMTP_HOST, port=SMTP_PORT,
                              username=SMTP_USER, password=SMTP_PASSWORD,
                              start_tls=True)
        logging.info(f"Email sent to {to_email}")
    except Exception as e:
        logging.error(f"Email error: {e}")

# ---------- ПЛАНИРОВЩИК ----------
async def scheduler_loop():
    global http_session
    http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
                                        connector=aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300))
    sem = asyncio.Semaphore(50)
    while True:
        try:
            monitors = await get_all_active_monitors()
            # сортировка: премиум мониторы первыми
            monitors.sort(key=lambda m: m[12], reverse=True)  # is_premium в индексе 12
            now = datetime.utcnow()
            tasks = []
            for mon in monitors:
                (mid, user_id, mtype, config_json, interval, last_checked,
                 prev_is_up, alert_until, chat_id, alert_repeat,
                 maint_from, maint_to, is_premium) = mon
                if last_checked:
                    last = datetime.fromisoformat(last_checked) if isinstance(last_checked, str) else last_checked
                    next_check = last + timedelta(seconds=interval)
                    if now < next_check:
                        continue
                in_maintenance = False
                if maint_from and maint_to:
                    try:
                        now_time = now.time()
                        from_t = datetime.strptime(maint_from, "%H:%M").time()
                        to_t = datetime.strptime(maint_to, "%H:%M").time()
                        if from_t <= to_t:
                            in_maintenance = from_t <= now_time <= to_t
                        else:
                            in_maintenance = now_time >= from_t or now_time <= to_t
                    except:
                        pass
                tasks.append(check_and_notify(mid, user_id, mtype, json.loads(config_json), chat_id,
                                              prev_is_up, alert_until, alert_repeat, in_maintenance, sem))
            if tasks:
                await asyncio.gather(*tasks)
        except Exception as e:
            logging.error(f"Scheduler error: {e}")
        await asyncio.sleep(15)

async def check_and_notify(mid, user_id, mtype, config, chat_id, prev_is_up, alert_until, alert_repeat, in_maintenance, sem):
    async with sem:
        is_up, status_text, resp_time, details = await perform_check(mid, mtype, config)
        await update_monitor_status(mid, is_up, status_text, resp_time, details)
        if prev_is_up is not None and prev_is_up != is_up:
            now = datetime.utcnow()
            if alert_until and now < datetime.fromisoformat(alert_until):
                return
            if not in_maintenance:
                if is_up:
                    text = f"✅ <b>Восстановлен</b> {mtype.upper()} монитор\n{status_text}"
                else:
                    text = f"🔴 <b>Упал</b> {mtype.upper()} монитор\n{status_text}"
                try:
                    await bot.send_message(chat_id, text)
                except Exception as e:
                    logging.error(f"Notify error: {e}")
                # Email уведомление
                user = await get_user(user_id)
                if user and user[2]:
                    subject = f"{'✅ UP' if is_up else '🔴 DOWN'}: {mtype.upper()} монитор"
                    body = f"Монитор {mtype} \"{config.get('name', mid)}\"\n"
                    body += f"Статус: {status_text}\n"
                    body += f"Время: {now.isoformat()}\n"
                    body += "\nС уважением, Элис"
                    asyncio.create_task(send_email(user[2], subject, body))
            if not is_up and alert_repeat > 0:
                async with aiosqlite.connect(DATABASE) as db:
                    await db.execute("UPDATE monitors SET alert_until=? WHERE id=?",
                                     (now + timedelta(minutes=alert_repeat), mid))
                    await db.commit()

# ---------- HEARTBEAT СЕРВЕР ----------
async def handle_root(request):
    return web.Response(text="Ellis is watching 👀")

async def handle_heartbeat(request):
    path = request.match_info['path']
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT id, config FROM monitors WHERE type='heartbeat' AND json_extract(config, '$.path')=?",
            (path,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return web.Response(text="Not found", status=404)
            config = json.loads(row[1])
            config['last_heartbeat'] = datetime.utcnow().isoformat()
            await db.execute("UPDATE monitors SET config=? WHERE id=?", (json.dumps(config), row[0]))
            await db.commit()
    return web.Response(text="OK")

async def start_heartbeat_server():
    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_post('/', handle_root)
    app.router.add_get('/{path:.*}', handle_heartbeat)
    app.router.add_post('/{path:.*}', handle_heartbeat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Heartbeat server on port {PORT}")

# ---------- ГРАФИКИ ----------
async def generate_graph(monitor_id: int, hours: int, style: str = "line") -> Optional[BytesIO]:
    since = datetime.utcnow() - timedelta(hours=hours)
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT checked_at, response_time_ms, is_up FROM checks WHERE monitor_id=? AND checked_at>=? ORDER BY checked_at",
            (monitor_id, since)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return None
    times = [datetime.fromisoformat(r[0]) for r in rows]
    resp_times = [r[1] if r[1] else 0 for r in rows]
    is_up = [r[2] for r in rows]

    if style == "line":
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(times, resp_times, color='blue', marker='.', linestyle='-', linewidth=1, markersize=2)
        ax.set_ylabel('Response time (ms)')
        ax.grid(True, linestyle='--', alpha=0.6)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    elif style == "status":
        fig, ax = plt.subplots(figsize=(10, 2))
        for i in range(len(times) - 1):
            color = 'green' if is_up[i] else 'red'
            ax.axvspan(times[i], times[i+1], facecolor=color, alpha=0.3)
        ax.set_ylabel('Status')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    elif style == "pie":
        up_count = sum(is_up)
        down_count = len(is_up) - up_count
        fig, ax = plt.subplots()
        ax.pie([up_count, down_count], labels=['Up', 'Down'], colors=['#2ecc71', '#e74c3c'], autopct='%1.1f%%', startangle=90)
        ax.axis('equal')
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf

# ---------- КЛАВИАТУРЫ ----------
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои мониторы", callback_data="menu_list")],
        [InlineKeyboardButton(text="➕ Добавить монитор", callback_data="menu_add")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton(text="💳 Premium", callback_data="menu_premium")],
    ])

def settings_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📧 Указать email", callback_data="set_email")],
        [InlineKeyboardButton(text="⏰ Повтор уведомлений", callback_data="set_repeat")],
        [InlineKeyboardButton(text="🛠 Техническое окно", callback_data="set_maintenance")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main")],
    ])

def premium_menu(is_premium: bool):
    kb = InlineKeyboardBuilder()
    if not is_premium:
        kb.button(text=f"⭐ Купить за {PREMIUM_PRICE_STARS} Stars", callback_data="buy_premium")
    else:
        kb.button(text="✅ У вас Premium", callback_data="noop")
    kb.button(text="🔙 Назад", callback_data="menu_main")
    return kb.as_markup()

def monitor_types_keyboard():
    kb = InlineKeyboardBuilder()
    types = [("🌐 HTTP(s)", "http"), ("🔍 Keyword", "keyword"), ("📡 Ping", "ping"),
             ("🔌 Port", "port"), ("💓 Heartbeat", "heartbeat"), ("🌍 DNS", "dns"),
             ("⚙️ API", "api"), ("📦 UDP", "udp")]
    for text, data in types:
        kb.button(text=text, callback_data=f"addtype_{data}")
    kb.adjust(2)
    return kb.as_markup()

def back_to_main():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main")]])

# ---------- FSM ----------
class AddMonitor(StatesGroup):
    choosing_type = State()
    entering_name = State()
    entering_config = State()
    entering_interval = State()
    confirm = State()

class Settings(StatesGroup):
    waiting_for_email = State()
    waiting_for_repeat = State()
    waiting_for_maintenance = State()

# ---------- ОБРАБОТЧИКИ ----------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await register_user(message.from_user.id, message.chat.id)
    greeting = random.choice(GREETINGS)
    await message.answer(greeting, reply_markup=main_menu())

@dp.callback_query(F.data == "menu_main")
async def back_to_main_callback(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "menu_list")
async def list_monitors(callback: CallbackQuery):
    user_id = callback.from_user.id
    monitors = await get_user_monitors(user_id)
    user = await get_user(user_id)
    limit = user[3] if user else MAX_FREE_MONITORS
    active = len([m for m in monitors if not m[5]])
    text = f"📊 Активно: {active}/{limit}\n"
    if not monitors:
        text += "У вас нет мониторов. Добавьте первый!"
        await callback.message.edit_text(text, reply_markup=main_menu())
        await callback.answer()
        return
    for m in monitors:
        mid, mtype, name, _, interval, paused, is_up, last_checked = m
        icon = "🟢" if is_up else "🔴" if is_up is False else "⚪️"
        paused_str = " (пауза)" if paused else ""
        last = last_checked[:19] if last_checked else "никогда"
        text += f"{icon} <b>{name or mid}</b> [{mtype}]{paused_str}\n"
        text += f"⏱ {interval}с | {last}\n"
    await callback.message.edit_text(text, reply_markup=main_menu())
    await callback.answer()

# Добавление монитора
@dp.callback_query(F.data == "menu_add")
async def start_add(callback: CallbackQuery, state: FSMContext):
    if not await can_add_monitor(callback.from_user.id):
        await callback.answer("Лимит мониторов исчерпан. Получите Premium.", show_alert=True)
        return
    await state.set_state(AddMonitor.choosing_type)
    await callback.message.edit_text("Выберите тип монитора:", reply_markup=monitor_types_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("addtype_"))
async def process_type(callback: CallbackQuery, state: FSMContext):
    mtype = callback.data.split("_")[1]
    await state.update_data(type=mtype)
    await state.set_state(AddMonitor.entering_name)
    await callback.message.edit_text("Введите название (или `-` для авто):", reply_markup=back_to_main())
    await callback.answer()

@dp.message(AddMonitor.entering_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if name == "-":
        name = None
    await state.update_data(name=name)
    data = await state.get_data()
    prompts = {
        "http": "Введите URL и ожидаемый статус (по умолчанию 200) через пробел:",
        "keyword": "Введите URL ключевое_слово [present/absent]:",
        "ping": "Введите хост или IP:",
        "port": "Введите хост порт:",
        "heartbeat": f"Введите путь (или '-' для авто). URL: {PUBLIC_URL}/<путь>\nМожно добавить макс. интервал (сек):",
        "dns": "Введите домен, тип_записи [ожидаемое_значение]:",
        "api": "Введите URL JSONPath [ожидаемое_значение]:",
        "udp": "Введите хост порт [данные] [ожидаемый_ответ]:",
    }
    await message.answer(prompts[data['type']])
    await state.set_state(AddMonitor.entering_config)

@dp.message(AddMonitor.entering_config)
async def process_config(message: Message, state: FSMContext):
    data = await state.get_data()
    mtype = data['type']
    args = message.text.strip().split()
    config = {}
    try:
        if mtype == "http":
            config["url"] = args[0]
            config["expected_status"] = int(args[1]) if len(args) > 1 else 200
        elif mtype == "keyword":
            config["url"] = args[0]
            config["keyword"] = args[1]
            config["mode"] = args[2] if len(args) > 2 and args[2] in ("present","absent") else "present"
        elif mtype == "ping":
            config["host"] = args[0]
        elif mtype == "port":
            config["host"] = args[0]
            config["port"] = int(args[1])
        elif mtype == "heartbeat":
            path = args[0] if args[0] != "-" else str(uuid.uuid4())[:8]
            config["path"] = path
            config["max_interval"] = int(args[1]) if len(args) > 1 else 600
            config["last_heartbeat"] = None
        elif mtype == "dns":
            config["domain"] = args[0]
            config["record_type"] = args[1] if len(args) > 1 else "A"
            config["expected_value"] = args[2] if len(args) > 2 else None
        elif mtype == "api":
            config["url"] = args[0]
            config["jsonpath"] = args[1] if len(args) > 1 else "$.status"
            config["expected_value"] = args[2] if len(args) > 2 else None
        elif mtype == "udp":
            config["host"] = args[0]
            config["port"] = int(args[1])
            config["send_data"] = args[2] if len(args) > 2 else ""
            config["expected_response"] = args[3] if len(args) > 3 else None
        else:
            raise ValueError
    except Exception as e:
        await message.answer(f"❌ Ошибка параметров: {e}. Попробуйте снова.")
        return
    await state.update_data(config=config)
    await state.set_state(AddMonitor.entering_interval)
    min_int = MIN_INTERVAL_PREMIUM if (await get_user(message.from_user.id))[4] else MIN_INTERVAL_FREE
    await message.answer(f"Введите интервал проверки в секундах (мин. {min_int}):", reply_markup=back_to_main())

@dp.message(AddMonitor.entering_interval)
async def process_interval(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    min_interval = MIN_INTERVAL_PREMIUM if user[4] else MIN_INTERVAL_FREE
    try:
        interval = int(message.text)
        if interval < min_interval:
            raise ValueError
    except:
        await message.answer(f"Интервал должен быть числом не менее {min_interval} секунд.")
        return
    await state.update_data(interval=interval)
    data = await state.get_data()
    text = f"<b>Подтверждение</b>\nТип: {data['type']}\nНазвание: {data.get('name', 'авто')}\n"
    if data['type'] == 'heartbeat':
        text += f"Heartbeat URL: {PUBLIC_URL}/{data['config']['path']}\n"
    else:
        text += f"Параметры: {json.dumps(data['config'], indent=2)}\n"
    text += f"Интервал: {interval}с"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="save_monitor"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")]
    ])
    await state.set_state(AddMonitor.confirm)
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "save_monitor", AddMonitor.confirm)
async def save_monitor(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    if not await can_add_monitor(user_id):
        await callback.answer("Лимит исчерпан.", show_alert=True)
        return
    mid = await add_monitor(user_id, data['type'], data.get('name', f"Monitor {data['type']}"), data['config'], data['interval'])
    await state.clear()
    await callback.message.edit_text(f"✅ Монитор добавлен (ID: {mid})", reply_markup=main_menu())
    await callback.answer()

# Пауза/возобновление, проверка, удаление, графики
@dp.callback_query(F.data.startswith("pause_"))
async def toggle_pause(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Не найден", show_alert=True)
        return
    new_paused = not mon[6]
    await set_monitor_pause(monitor_id, callback.from_user.id, new_paused)
    await callback.answer(f"{'Приостановлен' if new_paused else 'Возобновлён'}")
    await list_monitors(callback)

@dp.callback_query(F.data.startswith("check_"))
async def manual_check(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Не найден", show_alert=True)
        return
    is_up, status_text, resp_time, _ = await perform_check(monitor_id, mon[2], json.loads(mon[4]))
    await update_monitor_status(monitor_id, is_up, status_text, resp_time, "")
    await callback.message.answer(
        f"🔄 Результат проверки\nСтатус: {'🟢 UP' if is_up else '🔴 DOWN'}\n"
        f"Время ответа: {resp_time:.0f} мс\n{status_text}"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_monitor_cmd(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    if await delete_monitor(monitor_id, callback.from_user.id):
        await callback.answer("Удалён")
        await list_monitors(callback)
    else:
        await callback.answer("Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("graph_"))
async def graph_menu(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Линия (24ч)", callback_data=f"gdraw_{monitor_id}_24_line"),
         InlineKeyboardButton(text="📊 Статус (24ч)", callback_data=f"gdraw_{monitor_id}_24_status")],
        [InlineKeyboardButton(text="🥧 Круг (24ч)", callback_data=f"gdraw_{monitor_id}_24_pie")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_list")]
    ])
    await callback.message.edit_text("Выберите тип графика:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("gdraw_"))
async def draw_graph(callback: CallbackQuery):
    _, mid, hours, style = callback.data.split("_")
    buf = await generate_graph(int(mid), int(hours), style)
    if buf:
        await callback.message.reply_photo(BufferedInputFile(buf.read(), filename="graph.png"))
    else:
        await callback.answer("Нет данных", show_alert=True)
    await callback.answer()

# Настройки
@dp.callback_query(F.data == "menu_settings")
async def show_settings(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    email = user[2] if user else "не указан"
    repeat = user[5] if user else 0
    maint = f"{user[6]} – {user[7]}" if user and user[6] else "не задано"
    text = f"⚙️ Настройки\n\n📧 Email: {email}\n⏰ Повтор: каждые {repeat} мин.\n🛠 Тех. окно: {maint}"
    await callback.message.edit_text(text, reply_markup=settings_menu())
    await callback.answer()

@dp.callback_query(F.data == "set_email")
async def set_email_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.waiting_for_email)
    await callback.message.edit_text("📧 Введите ваш email:", reply_markup=back_to_main())
    await callback.answer()

@dp.message(Settings.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if "@" not in email or "." not in email:
        await message.answer("Некорректный email. Попробуйте ещё раз:")
        return
    await set_user_email(message.from_user.id, email)
    await state.clear()
    await message.answer("✅ Email сохранён!", reply_markup=main_menu())

@dp.callback_query(F.data == "set_repeat")
async def set_repeat_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.waiting_for_repeat)
    await callback.message.edit_text("⏰ Введите интервал повтора в минутах (0 = без повтора):", reply_markup=back_to_main())
    await callback.answer()

@dp.message(Settings.waiting_for_repeat)
async def process_repeat(message: Message, state: FSMContext):
    try:
        minutes = int(message.text)
        if minutes < 0:
            raise ValueError
    except:
        await message.answer("Введите целое неотрицательное число.")
        return
    await set_user_setting(message.from_user.id, "alert_repeat", minutes)
    await state.clear()
    await message.answer(f"🔔 Повтор каждые {minutes} мин.", reply_markup=main_menu())

@dp.callback_query(F.data == "set_maintenance")
async def set_maintenance_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.waiting_for_maintenance)
    await callback.message.edit_text("🛠 Введите начало и конец техокна в формате ЧЧ:ММ ЧЧ:ММ:", reply_markup=back_to_main())
    await callback.answer()

@dp.message(Settings.waiting_for_maintenance)
async def process_maintenance(message: Message, state: FSMContext):
    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("Нужно два времени через пробел.")
        return
    try:
        datetime.strptime(parts[0], "%H:%M")
        datetime.strptime(parts[1], "%H:%M")
    except ValueError:
        await message.answer("Неверный формат времени.")
        return
    await set_user_setting(message.from_user.id, "maintenance_from", parts[0])
    await set_user_setting(message.from_user.id, "maintenance_to", parts[1])
    await state.clear()
    await message.answer(f"🛠 Тех. окно: {parts[0]} – {parts[1]}", reply_markup=main_menu())

# Premium
@dp.callback_query(F.data == "menu_premium")
async def premium_info(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    is_prem = user[4] if user else 0
    text = "💎 Premium\n" + ("У вас активен Premium. Спасибо!" if is_prem else f"Лимит {MAX_PREMIUM_MONITORS}, мин. интервал {MIN_INTERVAL_PREMIUM}с")
    await callback.message.edit_text(text, reply_markup=premium_menu(is_prem))
    await callback.answer()

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Premium UptimeBot",
        description="До 10 мониторов",
        payload="premium_upgrade",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_PRICE_STARS)],
        start_parameter="premium"
    )
    await callback.answer("Счёт отправлен")

@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE users SET monitor_limit=?, is_premium=1 WHERE user_id=?",
                         (MAX_PREMIUM_MONITORS, message.from_user.id))
        await db.commit()
    await message.answer("🎉 Вы Premium! Лимит увеличен до 10, интервал от 30с.", reply_markup=main_menu())

@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()

# ---------- ЗАПУСК ----------
async def on_startup():
    await init_db()
    asyncio.create_task(start_heartbeat_server())
    asyncio.create_task(scheduler_loop())

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
