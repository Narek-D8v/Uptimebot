import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import signal
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from io import BytesIO, StringIO
from typing import Optional
from urllib.parse import parse_qsl, urlparse

import aiohttp
import aiosqlite
import aiosmtplib
import dns.asyncresolver
import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery, WebAppInfo
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from jsonpath_ng import parse as jp_parse

matplotlib.use('Agg')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ellis")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set!")

BOT_TOKEN_BYTES = BOT_TOKEN.encode()

DATABASE = os.environ.get("DATABASE_PATH", "uptime_bot.db")
PORT = int(os.environ.get("PORT", 8080))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}").rstrip('/')

DEFAULT_TIMEOUT = 10
MAX_FREE_MONITORS = 3
MAX_PREMIUM_MONITORS = 10
PREMIUM_PRICE_STARS = int(os.environ.get("PREMIUM_PRICE_STARS", "5"))
MIN_INTERVAL_FREE = 60
MIN_INTERVAL_PREMIUM = 30
DEFAULT_INTERVAL = 300
BODY_SIZE_LIMIT = 10 * 1024 * 1024
RATE_LIMIT_PER_SEC = 2

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

VIP_USER_ID = int(os.environ.get("VIP_USER_ID", "5457847440"))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

http_session: Optional[aiohttp.ClientSession] = None
db_conn: Optional[aiosqlite.Connection] = None
heartbeat_runner: Optional[web.AppRunner] = None
_email_task_set: set[asyncio.Task] = set()
_startup_done = False

GREETINGS = [
    "\U0001f44b Привет! Я Элис, твой страж сайтов и серверов. Всё под контролем!",
    "\U0001f319 Добро пожаловать! Я не сплю, пока твои сервисы работают.",
    "\u2728 Здравствуй! Доверь мне мониторинг – я предупрежу, если что-то случится.",
    "\U0001f6e1 Приветствую! Я твой цифровой ангел-хранитель."
]

HOST_RE = re.compile(r'^[\w.\-:]+$')
DB_VERSION = 3


class RateLimiter:
    def __init__(self, max_per_sec: int = RATE_LIMIT_PER_SEC):
        self.max_per_sec = max_per_sec
        self._records: dict[int, list[float]] = defaultdict(list)

    def is_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        window = now - 1.0
        entries = [t for t in self._records[user_id] if t > window]
        if len(entries) >= self.max_per_sec:
            return True
        entries.append(now)
        self._records[user_id] = entries
        return False


rate_limiter = RateLimiter()


class RateLimitMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        uid = getattr(getattr(event, 'from_user', None), 'id', None)
        if uid and rate_limiter.is_limited(uid):
            return
        return await handler(event, data)


dp.message.middleware(RateLimitMiddleware())
dp.callback_query.middleware(RateLimitMiddleware())


def validate_init_data(init_data: str) -> Optional[int]:
    try:
        parsed = dict(parse_qsl(init_data))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        items = sorted(parsed.items())
        data_check_string = "\n".join(f"{k}={v}" for k, v in items)
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN_BYTES, hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed_hash != received_hash:
            return None
        user_data = json.loads(parsed.get("user", "{}"))
        return user_data.get("id")
    except Exception as e:
        logger.error("InitData validation error: %s", e)
        return None


async def get_db() -> aiosqlite.Connection:
    global db_conn, _startup_done
    if db_conn is None:
        db_conn = await aiosqlite.connect(DATABASE)
        db_conn.row_factory = aiosqlite.Row
        await db_conn.execute("PRAGMA journal_mode=WAL")
        await db_conn.execute("PRAGMA busy_timeout=5000")
        if _startup_done:
            logger.warning("db_conn was None after startup – re-created")
    return db_conn


def validate_host(host: str) -> bool:
    return bool(host and len(host) <= 253 and HOST_RE.match(host))


def validate_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return result.scheme in ('http', 'https') and bool(result.netloc)
    except Exception:
        return False


async def init_db():
    global _startup_done
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            email TEXT,
            monitor_limit INTEGER DEFAULT 3,
            is_premium BOOLEAN DEFAULT 0,
            alert_repeat INTEGER DEFAULT 0,
            maintenance_from TEXT,
            maintenance_to TEXT,
            public_token TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

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
        );

        CREATE TABLE IF NOT EXISTS checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            response_time_ms REAL,
            is_up BOOLEAN,
            details TEXT,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (monitor_id) REFERENCES monitors(id)
        );

        CREATE INDEX IF NOT EXISTS idx_checks_monitor_time ON checks(monitor_id, checked_at);
        CREATE INDEX IF NOT EXISTS idx_monitors_user ON monitors(user_id);
    """)
    await db.commit()

    async with db.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
        version = row[0] if row else 0

    if version < 1:
        logger.info("Running DB migration to version 1")
        try:
            await db.executescript("""
                ALTER TABLE users ADD COLUMN email TEXT;
                ALTER TABLE users ADD COLUMN is_premium BOOLEAN DEFAULT 0;
                ALTER TABLE users ADD COLUMN alert_repeat INTEGER DEFAULT 0;
                ALTER TABLE users ADD COLUMN maintenance_from TEXT;
                ALTER TABLE users ADD COLUMN maintenance_to TEXT;
                ALTER TABLE monitors ADD COLUMN consecutive_failures INTEGER DEFAULT 0;
                ALTER TABLE monitors ADD COLUMN alert_until TIMESTAMP;
            """)
        except aiosqlite.OperationalError:
            logger.info("Some v1 columns already exist – skipping")
        await db.execute(f"PRAGMA user_version = 1")
        await db.commit()

    if version < 2:
        logger.info("Running DB migration to version 2 (public_token)")
        try:
            await db.execute("ALTER TABLE users ADD COLUMN public_token TEXT")
        except aiosqlite.OperationalError:
            logger.info("public_token column already exists – skipping")
        await db.execute("PRAGMA user_version = 2")
        await db.commit()
        logger.info("DB migration to version 2 complete")
        version = 2

    if version < 3:
        logger.info("Running DB migration to version 3 (sort_order, monitor_icon)")
        try:
            await db.execute("ALTER TABLE monitors ADD COLUMN sort_order INTEGER DEFAULT 0")
        except aiosqlite.OperationalError:
            logger.info("sort_order column already exists – skipping")
        await db.execute(f"PRAGMA user_version = {DB_VERSION}")
        await db.commit()
        logger.info("DB migration to version %d complete", DB_VERSION)

    _startup_done = True


async def ensure_vip_user():
    db = await get_db()
    await db.execute(
        "INSERT INTO users (user_id, chat_id, monitor_limit, is_premium) VALUES (?, 0, ?, 1) "
        "ON CONFLICT(user_id) DO UPDATE SET monitor_limit=?, is_premium=1",
        (VIP_USER_ID, MAX_PREMIUM_MONITORS, MAX_PREMIUM_MONITORS)
    )
    await db.commit()
    logger.info("VIP user %s premium ensured", VIP_USER_ID)


async def register_user(user_id: int, chat_id: int):
    db = await get_db()
    limit = MAX_PREMIUM_MONITORS if user_id == VIP_USER_ID else MAX_FREE_MONITORS
    is_prem = 1 if user_id == VIP_USER_ID else 0
    await db.execute(
        "INSERT INTO users (user_id, chat_id, monitor_limit, is_premium) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET chat_id=?, monitor_limit=?, is_premium=?",
        (user_id, chat_id, limit, is_prem, chat_id, limit, is_prem)
    )
    await db.commit()


async def get_user(user_id: int):
    db = await get_db()
    async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
        return await cur.fetchone()


async def set_user_email(user_id: int, email: str):
    db = await get_db()
    await db.execute("UPDATE users SET email=? WHERE user_id=?", (email, user_id))
    await db.commit()


async def set_user_alert_repeat(user_id: int, minutes: int):
    db = await get_db()
    await db.execute("UPDATE users SET alert_repeat=? WHERE user_id=?", (minutes, user_id))
    await db.commit()


async def set_user_maintenance(user_id: int, from_time: str, to_time: str):
    db = await get_db()
    await db.execute(
        "UPDATE users SET maintenance_from=?, maintenance_to=? WHERE user_id=?",
        (from_time, to_time, user_id)
    )
    await db.commit()


async def set_user_public_token(user_id: int, token: str):
    db = await get_db()
    await db.execute("UPDATE users SET public_token=? WHERE user_id=?", (token, user_id))
    await db.commit()


async def get_active_monitor_count(user_id: int) -> int:
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM monitors WHERE user_id=? AND is_paused=0", (user_id,)
    ) as cur:
        row = await cur.fetchone()
        return row["cnt"] if row else 0


async def can_add_monitor(user_id: int) -> bool:
    count = await get_active_monitor_count(user_id)
    user = await get_user(user_id)
    limit = user["monitor_limit"] if user else MAX_FREE_MONITORS
    return count < limit


async def add_monitor(user_id: int, monitor_type: str, name: str, config: dict, interval: int) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO monitors (user_id, type, name, config, interval_seconds) VALUES (?,?,?,?,?)",
        (user_id, monitor_type, name, json.dumps(config), interval)
    )
    await db.commit()
    return cur.lastrowid


async def delete_monitor(monitor_id: int, user_id: int) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id))
    await db.commit()
    return cur.rowcount > 0


async def get_monitor(monitor_id: int, user_id: int):
    db = await get_db()
    async with db.execute("SELECT * FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id)) as cur:
        return await cur.fetchone()


async def get_monitor_by_id(monitor_id: int):
    db = await get_db()
    async with db.execute("SELECT * FROM monitors WHERE id=?", (monitor_id,)) as cur:
        return await cur.fetchone()


async def get_user_monitors(user_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT id, type, name, config, interval_seconds, is_paused, is_up, last_checked "
        "FROM monitors WHERE user_id=? ORDER BY sort_order ASC, created_at DESC",
        (user_id,)
    ) as cur:
        return await cur.fetchall()


async def get_all_active_monitors():
    db = await get_db()
    async with db.execute(
        "SELECT m.id, m.user_id, m.type, m.config, m.interval_seconds, m.name, "
        "m.last_checked, m.is_up, m.alert_until, "
        "u.chat_id, u.alert_repeat, u.maintenance_from, u.maintenance_to, u.is_premium "
        "FROM monitors m JOIN users u ON m.user_id=u.user_id WHERE m.is_paused=0"
    ) as cur:
        return await cur.fetchall()


async def update_monitor_status(monitor_id: int, is_up: bool, status_text: str,
                                response_time_ms: float, details: str = "", status_code: Optional[int] = None):
    db = await get_db()
    now = datetime.now(timezone.utc)
    await db.execute(
        "UPDATE monitors SET last_status=?, is_up=?, last_checked=?, "
        "consecutive_failures=CASE WHEN ? THEN 0 ELSE consecutive_failures+1 END WHERE id=?",
        (status_text, is_up, now, is_up, monitor_id)
    )
    await db.execute(
        "INSERT INTO checks (monitor_id, response_time_ms, is_up, details) VALUES (?,?,?,?)",
        (monitor_id, response_time_ms, is_up, details)
    )
    await db.commit()


async def set_monitor_pause(monitor_id: int, user_id: int, paused: bool):
    db = await get_db()
    await db.execute("UPDATE monitors SET is_paused=? WHERE id=? AND user_id=?",
                     (int(paused), monitor_id, user_id))
    await db.commit()


async def set_monitor_interval(monitor_id: int, user_id: int, interval: int):
    db = await get_db()
    await db.execute("UPDATE monitors SET interval_seconds=? WHERE id=? AND user_id=?",
                     (interval, monitor_id, user_id))
    await db.commit()


async def update_monitor_config(monitor_id: int, user_id: int, config: dict):
    db = await get_db()
    await db.execute("UPDATE monitors SET config=? WHERE id=? AND user_id=?",
                     (json.dumps(config), monitor_id, user_id))
    await db.commit()


async def get_monitor_stats(monitor_id: int, hours: int):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) AS total, SUM(is_up) AS up_count, AVG(response_time_ms) AS avg_resp "
        "FROM checks WHERE monitor_id=? AND checked_at>=?",
        (monitor_id, since)
    ) as cur:
        row = await cur.fetchone()
    if not row or row["total"] == 0:
        return {"total": 0, "uptime": 100.0, "avg_response_time": 0}
    total = row["total"]
    up_count = row["up_count"] or 0
    avg_resp = row["avg_resp"] or 0
    uptime = (up_count / total) * 100
    return {"total": total, "uptime": round(uptime, 2), "avg_response_time": round(avg_resp, 1)}


async def get_user_overall_stats(user_id: int):
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN is_paused=0 THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN is_up=1 THEN 1 ELSE 0 END) AS up_count "
        "FROM monitors WHERE user_id=?", (user_id,)
    ) as cur:
        row = await cur.fetchone()
    total = row["total"] if row else 0
    active = row["active"] if row else 0
    up_count = row["up_count"] if row else 0
    avg_uptime = 100.0
    if active > 0:
        async with db.execute(
            "SELECT AVG(uptime) AS avg_uptime FROM ("
            "SELECT m.id, (SELECT CAST(SUM(c.is_up) AS REAL) / COUNT(*) * 100 FROM checks c "
            "WHERE c.monitor_id=m.id AND c.checked_at>=datetime('now', '-24 hours')) AS uptime "
            "FROM monitors m WHERE m.user_id=? AND m.is_paused=0)", (user_id,)
        ) as cur2:
            row2 = await cur2.fetchone()
            if row2 and row2["avg_uptime"]:
                avg_uptime = round(row2["avg_uptime"], 2)
    return {"total": total, "active": active, "up": up_count, "avg_uptime_24h": avg_uptime}


async def get_incidents(user_id: int, since: Optional[datetime] = None, until: Optional[datetime] = None):
    db = await get_db()
    query = """
        SELECT c.monitor_id, m.name AS monitor_name, m.type AS monitor_type,
               c.is_up, c.checked_at
        FROM checks c
        JOIN monitors m ON c.monitor_id = m.id
        WHERE m.user_id = ?
    """
    params: list = [user_id]
    if since:
        query += " AND c.checked_at >= ?"
        params.append(since)
    if until:
        query += " AND c.checked_at <= ?"
        params.append(until)
    query += " ORDER BY c.monitor_id, c.checked_at"
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    incidents = []
    prev = {}
    for r in rows:
        mid = r["monitor_id"]
        prev_state = prev.get(mid)
        current = {"is_up": r["is_up"], "time": r["checked_at"]}
        if prev_state is not None and prev_state["is_up"] != current["is_up"]:
            incidents.append({
                "monitor_id": mid,
                "monitor_name": r["monitor_name"],
                "monitor_type": r["monitor_type"],
                "from_status": prev_state["is_up"],
                "to_status": current["is_up"],
                "from_time": prev_state["time"],
                "to_time": current["time"],
            })
        prev[mid] = current
    return incidents


async def check_http(config: dict) -> tuple:
    url = config["url"]
    if not validate_url(url):
        return False, "Invalid URL scheme", 0, ""
    expected_status = config.get("expected_status", 200)
    method = config.get("method", "GET").upper()
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        async with http_session.request(
            method, url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True
        ) as resp:
            status = resp.status
            resp_time = (time.monotonic() - start) * 1000
            is_up = (status == expected_status)
            return is_up, str(status), resp_time, "", status
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e), None


async def check_keyword(config: dict) -> tuple:
    url = config["url"]
    if not validate_url(url):
        return False, "Invalid URL scheme", 0, ""
    keyword = config["keyword"]
    mode = config.get("mode", "present")
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        async with http_session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True
        ) as resp:
            body = await resp.content.read(BODY_SIZE_LIMIT + 1)
            if len(body) > BODY_SIZE_LIMIT:
                return False, "Response body too large", 0, ""
            body = body.decode(errors='replace')
            resp_time = (time.monotonic() - start) * 1000
            found = keyword in body
            is_up = (found if mode == "present" else not found)
            return is_up, f"Keyword {'found' if found else 'not found'}", resp_time, f"Status: {resp.status}"
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)


async def check_ping(config: dict) -> tuple:
    host = config["host"]
    if not validate_host(host):
        return False, "Invalid host", 0, ""
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        if os.name == 'nt':
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
        else:
            cmd = ["ping", "-c", "1", "-W", str(timeout), host]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 2)
        resp_time = (time.monotonic() - start) * 1000
        if proc.returncode == 0:
            return True, "Host reachable", resp_time, stdout.decode(errors='replace').strip()
        return False, "Host unreachable", resp_time, stderr.decode(errors='replace').strip() or "No response"
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
            return False, f"Expected {expected_value}, got {values}", resp_time, str(values)
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
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            data = await resp.json()
            resp_time = (time.monotonic() - start) * 1000
            expr = jp_parse(jsonpath_expr)
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


class UDPEchoClientProtocol(asyncio.DatagramProtocol):
    def __init__(self):
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
            UDPEchoClientProtocol, remote_addr=(host, port)
        )
        try:
            transport.sendto(send_data)
            await asyncio.wait_for(protocol.received_event.wait(), timeout=timeout)
            resp_time = (time.monotonic() - start) * 1000
            if protocol.response:
                decoded = protocol.response.decode(errors='ignore')
                if expected_response:
                    if expected_response in decoded:
                        return True, "UDP response matches", resp_time, decoded
                    return False, f"Unexpected response: {decoded[:50]}", resp_time, ""
                return True, "UDP response received", resp_time, decoded
            return False, "No UDP response", resp_time, ""
        finally:
            transport.close()
    except asyncio.TimeoutError:
        resp_time = (time.monotonic() - start) * 1000
        return False, "UDP timeout", resp_time, ""
    except Exception as e:
        resp_time = (time.monotonic() - start) * 1000
        return False, str(e), resp_time, str(e)


async def check_heartbeat(config: dict) -> tuple:
    last_heartbeat = config.get("last_heartbeat")
    max_interval = config.get("max_interval", 600)
    if not last_heartbeat:
        return False, "No heartbeat received", 0, ""
    try:
        last = datetime.fromisoformat(last_heartbeat)
    except Exception:
        return False, "Invalid timestamp", 0, ""
    now = datetime.now(timezone.utc)
    delta = (now - last).total_seconds()
    if delta <= max_interval:
        return True, f"Last heartbeat {delta:.0f}s ago", delta * 1000, ""
    return False, f"Heartbeat overdue: {delta:.0f}s > {max_interval}s", delta * 1000, ""


CHECK_FUNCTIONS = {
    "http": check_http,
    "keyword": check_keyword,
    "ping": check_ping,
    "port": check_port,
    "dns": check_dns,
    "api": check_api,
    "udp": check_udp,
    "heartbeat": check_heartbeat,
}


async def perform_check(monitor_id: int, monitor_type: str, config: dict) -> tuple:
    fn = CHECK_FUNCTIONS.get(monitor_type)
    if fn is None:
        return False, f"Unknown type {monitor_type}", 0, ""
    return await fn(config)


async def send_email(to_email: str, subject: str, body: str):
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD]):
        logger.warning("SMTP not configured, skipping email")
        return
    msg = EmailMessage()
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body, charset="utf-8")
    try:
        await aiosmtplib.send(msg, hostname=SMTP_HOST, port=SMTP_PORT,
                              username=SMTP_USER, password=SMTP_PASSWORD, start_tls=True)
        logger.info("Email sent to %s", to_email)
    except Exception as e:
        logger.error("Email error: %s", e)


async def scheduler_loop():
    global http_session
    http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        connector=aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300)
    )
    sem = asyncio.Semaphore(50)
    while True:
        try:
            monitors = await get_all_active_monitors()
            monitors.sort(key=lambda m: m["is_premium"], reverse=True)
            now = datetime.now(timezone.utc)
            tasks = []
            for mon in monitors:
                last_checked = mon["last_checked"]
                if last_checked:
                    last = datetime.fromisoformat(last_checked) if isinstance(last_checked, str) else last_checked
                    next_check = last + timedelta(seconds=mon["interval_seconds"])
                    if now < next_check:
                        continue
                in_maintenance = False
                maint_from = mon["maintenance_from"]
                maint_to = mon["maintenance_to"]
                if maint_from and maint_to:
                    try:
                        now_time = now.time()
                        from_t = datetime.strptime(maint_from, "%H:%M").time()
                        to_t = datetime.strptime(maint_to, "%H:%M").time()
                        if from_t <= to_t:
                            in_maintenance = from_t <= now_time <= to_t
                        else:
                            in_maintenance = now_time >= from_t or now_time <= to_t
                    except Exception as e:
                        logger.exception("Maintenance time parse error for user %s: %s", mon["user_id"], e)
                tasks.append(check_and_notify(
                    mon["id"], mon["user_id"], mon["type"], json.loads(mon["config"]),
                    mon["name"], mon["chat_id"], mon["is_up"], mon["alert_until"],
                    mon["alert_repeat"], in_maintenance, sem
                ))
            if tasks:
                await asyncio.gather(*tasks)
        except Exception as e:
            logger.error("Scheduler loop error: %s", e)
        await asyncio.sleep(15)


async def check_and_notify(mid, user_id, mtype, config, name, chat_id,
                           prev_is_up, alert_until, alert_repeat, in_maintenance, sem):
    async with sem:
        result = await perform_check(mid, mtype, config)
        is_up, status_text, resp_time = result[0], result[1], result[2]
        details = result[3] if len(result) > 3 else ""
        status_code = result[4] if len(result) > 4 else None
        await update_monitor_status(mid, is_up, status_text, resp_time, details, status_code)
        if prev_is_up is not None and prev_is_up != is_up:
            now = datetime.now(timezone.utc)
            if alert_until:
                try:
                    if now < datetime.fromisoformat(alert_until):
                        return
                except Exception:
                    pass
            if not in_maintenance:
                if is_up:
                    text = f"\u2705 <b>Восстановлен</b> {mtype.upper()} монитор\n{status_text}"
                else:
                    text = f"\U0001f534 <b>Упал</b> {mtype.upper()} монитор\n{status_text}"
                try:
                    await bot.send_message(chat_id, text)
                except Exception as e:
                    logger.error("Notify send error: %s", e)
                user = await get_user(user_id)
                email = user["email"] if user else None
                if email:
                    subject = f"{'\u2705 UP' if is_up else '\U0001f534 DOWN'}: {mtype.upper()} монитор"
                    body_lines = [
                        f"Монитор {mtype} \"{name or mid}\"",
                        f"Статус: {status_text}",
                        f"Время: {now.isoformat()}",
                        "",
                        "С уважением, Элис"
                    ]
                    try:
                        await asyncio.wait_for(send_email(email, subject, "\n".join(body_lines)), timeout=10)
                    except asyncio.TimeoutError:
                        logger.error("Email send timeout for %s", email)
            if not is_up and alert_repeat > 0:
                db = await get_db()
                await db.execute("UPDATE monitors SET alert_until=? WHERE id=?",
                                 ((now + timedelta(minutes=alert_repeat)), mid))
                await db.commit()


async def handle_root(request):
    return web.Response(text="Ellis is watching \U0001f440")


async def handle_heartbeat(request):
    path = request.match_info['path']
    token = request.headers.get("X-Heartbeat-Token", "")
    db = await get_db()
    async with db.execute(
        "SELECT id, config FROM monitors WHERE type='heartbeat' AND json_extract(config, '$.path')=?",
        (path,)
    ) as cur:
        row = await cur.fetchone()
        if not row:
            return web.Response(text="Not found", status=404)
        config = json.loads(row["config"])
        stored_token = config.get("token", "")
        if stored_token and token != stored_token:
            return web.Response(text="Forbidden", status=403)
        config["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
        await db.execute("UPDATE monitors SET config=? WHERE id=?", (json.dumps(config), row["id"]))
        await db.commit()
    return web.Response(text="OK")


async def handle_webapp_index(request):
    return web.FileResponse('./webapp/index.html')


async def api_auth(request) -> Optional[int]:
    init_data = request.headers.get("X-Telegram-InitData", "")
    if not init_data:
        return None
    return validate_init_data(init_data)


async def api_auth_required(request):
    user_id = await api_auth(request)
    if not user_id:
        raise web.HTTPForbidden(reason="Unauthorized", body=json.dumps({"error": "Unauthorized"}),
                                content_type="application/json")
    return user_id


async def handle_api_user(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    user = await get_user(user_id)
    if not user:
        return web.json_response({"error": "User not found"}, status=404)
    return web.json_response({
        "user_id": user["user_id"],
        "email": user["email"],
        "is_premium": bool(user["is_premium"]),
        "monitor_limit": user["monitor_limit"],
        "alert_repeat": user["alert_repeat"],
        "maintenance_from": user["maintenance_from"],
        "maintenance_to": user["maintenance_to"],
        "public_token": user["public_token"],
        "created_at": user["created_at"],
    })


async def handle_api_monitors_list(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    monitors = await get_user_monitors(user_id)
    result = []
    for m in monitors:
        result.append({
            "id": m["id"],
            "type": m["type"],
            "name": m["name"],
            "config": json.loads(m["config"]),
            "interval_seconds": m["interval_seconds"],
            "is_paused": bool(m["is_paused"]),
            "is_up": m["is_up"],
            "last_checked": m["last_checked"],
        })
    return web.json_response(result)


async def handle_api_monitor_detail(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    monitor_id = int(request.match_info['id'])
    mon = await get_monitor(monitor_id, user_id)
    if not mon:
        return web.json_response({"error": "Not found"}, status=404)
    db = await get_db()
    async with db.execute(
        "SELECT id, response_time_ms, is_up, details, checked_at FROM checks "
        "WHERE monitor_id=? ORDER BY checked_at DESC LIMIT 20", (monitor_id,)
    ) as cur:
        checks = await cur.fetchall()
    return web.json_response({
        "id": mon["id"],
        "type": mon["type"],
        "name": mon["name"],
        "config": json.loads(mon["config"]),
        "interval_seconds": mon["interval_seconds"],
        "is_paused": bool(mon["is_paused"]),
        "is_up": mon["is_up"],
        "last_status": mon["last_status"],
        "last_checked": mon["last_checked"],
        "consecutive_failures": mon["consecutive_failures"],
        "created_at": mon["created_at"],
        "recent_checks": [
            {"id": c["id"], "response_time_ms": c["response_time_ms"],
             "is_up": c["is_up"], "details": c["details"], "checked_at": c["checked_at"]}
            for c in checks
        ],
    })


async def handle_api_monitor_create(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    mtype = body.get("type", "")
    if mtype not in CHECK_FUNCTIONS:
        return web.json_response({"error": f"Invalid type: {mtype}"}, status=400)
    if not await can_add_monitor(user_id):
        return web.json_response({"error": "Monitor limit reached"}, status=400)
    name = body.get("name", f"Monitor {mtype}")
    config = body.get("config", {})
    interval = body.get("interval_seconds", DEFAULT_INTERVAL)
    user = await get_user(user_id)
    min_interval = MIN_INTERVAL_PREMIUM if (user and user["is_premium"]) else MIN_INTERVAL_FREE
    if interval < min_interval:
        return web.json_response({"error": f"Minimum interval is {min_interval}s"}, status=400)
    try:
        mid = await add_monitor(user_id, mtype, name, config, interval)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"id": mid, "message": "Monitor created"}, status=201)


async def handle_api_monitor_update(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    monitor_id = int(request.match_info['id'])
    mon = await get_monitor(monitor_id, user_id)
    if not mon:
        return web.json_response({"error": "Not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if "is_paused" in body:
        await set_monitor_pause(monitor_id, user_id, bool(body["is_paused"]))
    if "interval_seconds" in body:
        user = await get_user(user_id)
        min_interval = MIN_INTERVAL_PREMIUM if (user and user["is_premium"]) else MIN_INTERVAL_FREE
        if body["interval_seconds"] < min_interval:
            return web.json_response({"error": f"Minimum interval is {min_interval}s"}, status=400)
        await set_monitor_interval(monitor_id, user_id, body["interval_seconds"])
    if "config" in body:
        await update_monitor_config(monitor_id, user_id, body["config"])
    if "name" in body:
        db = await get_db()
        await db.execute("UPDATE monitors SET name=? WHERE id=? AND user_id=?",
                         (body["name"], monitor_id, user_id))
        await db.commit()
    return web.json_response({"message": "Updated"})


async def handle_api_monitor_delete(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    monitor_id = int(request.match_info['id'])
    if await delete_monitor(monitor_id, user_id):
        return web.json_response({"message": "Deleted"})
    return web.json_response({"error": "Not found"}, status=404)


async def handle_api_monitor_checks(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    monitor_id = int(request.match_info['id'])
    mon = await get_monitor(monitor_id, user_id)
    if not mon:
        return web.json_response({"error": "Not found"}, status=404)
    limit = int(request.query.get("limit", 50))
    db = await get_db()
    async with db.execute(
        "SELECT id, response_time_ms, is_up, details, checked_at FROM checks "
        "WHERE monitor_id=? ORDER BY checked_at DESC LIMIT ?", (monitor_id, limit)
    ) as cur:
        checks = await cur.fetchall()
    return web.json_response([
        {"id": c["id"], "response_time_ms": c["response_time_ms"],
         "is_up": c["is_up"], "details": c["details"], "checked_at": c["checked_at"]}
        for c in checks
    ])


async def handle_api_monitor_graph(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    monitor_id = int(request.match_info['id'])
    mon = await get_monitor(monitor_id, user_id)
    if not mon:
        return web.json_response({"error": "Not found"}, status=404)
    hours = int(request.query.get("hours", 24))
    style = request.query.get("style", "line")
    if style not in ("line", "status", "pie"):
        style = "line"
    buf = await generate_graph(monitor_id, hours, style)
    if not buf:
        return web.json_response({"error": "No data"}, status=404)
    return web.Response(body=buf.read(), content_type="image/png")


async def handle_api_incidents(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    since_str = request.query.get("since")
    until_str = request.query.get("until")
    since = datetime.fromisoformat(since_str) if since_str else None
    until = datetime.fromisoformat(until_str) if until_str else None
    incidents = await get_incidents(user_id, since, until)
    return web.json_response([
        {
            "monitor_id": inc["monitor_id"],
            "monitor_name": inc["monitor_name"],
            "monitor_type": inc["monitor_type"],
            "from_status": inc["from_status"],
            "to_status": inc["to_status"],
            "from_time": inc["from_time"].isoformat() if hasattr(inc["from_time"], 'isoformat') else inc["from_time"],
            "to_time": inc["to_time"].isoformat() if hasattr(inc["to_time"], 'isoformat') else inc["to_time"],
        }
        for inc in incidents
    ])


async def handle_api_settings(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if "email" in body:
        email = body["email"]
        if "@" not in email or "." not in email:
            return web.json_response({"error": "Invalid email"}, status=400)
        await set_user_email(user_id, email)
    if "alert_repeat" in body:
        await set_user_alert_repeat(user_id, int(body["alert_repeat"]))
    if "maintenance_from" in body and "maintenance_to" in body:
        await set_user_maintenance(user_id, body["maintenance_from"], body["maintenance_to"])
    return web.json_response({"message": "Settings updated"})


async def handle_api_public_token_generate(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    token = str(uuid.uuid4())
    await set_user_public_token(user_id, token)
    return web.json_response({"token": token, "url": f"{PUBLIC_URL}/api/v1/public/{token}"})


async def handle_api_public_page(request):
    token = request.match_info.get('token', '')
    if not token:
        return web.json_response({"error": "Token required"}, status=400)
    db = await get_db()
    async with db.execute("SELECT user_id FROM users WHERE public_token=?", (token,)) as cur:
        user = await cur.fetchone()
    if not user:
        return web.json_response({"error": "Invalid token"}, status=404)
    user_id = user["user_id"]
    monitors = await get_user_monitors(user_id)
    return web.json_response({
        "monitors": [
            {
                "id": m["id"],
                "type": m["type"],
                "name": m["name"],
                "is_up": m["is_up"],
                "last_status": None,
                "last_checked": m["last_checked"],
                "is_paused": bool(m["is_paused"]),
            }
            for m in monitors
        ]
    })


async def handle_api_export(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    monitor_id = request.query.get("monitor_id")
    hours = int(request.query.get("hours", 24))
    export_format = request.query.get("format", "json")
    if monitor_id:
        mon = await get_monitor(int(monitor_id), user_id)
        if not mon:
            return web.json_response({"error": "Not found"}, status=404)
        mids = [int(monitor_id)]
    else:
        monitors = await get_user_monitors(user_id)
        mids = [m["id"] for m in monitors]
    if not mids:
        return web.json_response({"error": "No monitors"}, status=404)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    db = await get_db()
    placeholders = ",".join("?" for _ in mids)
    async with db.execute(
        f"SELECT c.monitor_id, m.name AS monitor_name, m.type AS monitor_type, "
        f"c.response_time_ms, c.is_up, c.details, c.checked_at "
        f"FROM checks c JOIN monitors m ON c.monitor_id=m.id "
        f"WHERE c.monitor_id IN ({placeholders}) AND c.checked_at>=? "
        f"ORDER BY c.checked_at", mids + [since]
    ) as cur:
        rows = await cur.fetchall()
    data = [
        {"monitor_id": r["monitor_id"], "monitor_name": r["monitor_name"],
         "type": r["monitor_type"], "response_time_ms": r["response_time_ms"],
         "is_up": r["is_up"], "details": r["details"], "checked_at": r["checked_at"]}
        for r in rows
    ]
    if export_format == "csv":
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=["monitor_id", "monitor_name", "type",
                                                     "response_time_ms", "is_up", "details", "checked_at"])
        writer.writeheader()
        writer.writerows(data)
        return web.Response(body=output.getvalue(), content_type="text/csv",
                            headers={"Content-Disposition": "attachment; filename=export.csv"})
    return web.json_response(data)


async def handle_api_monitors_reorder(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    order = body.get("order", [])
    db = await get_db()
    for item in order:
        mid = item.get("id")
        sort = item.get("sort_order", 0)
        if mid:
            await db.execute("UPDATE monitors SET sort_order=? WHERE id=? AND user_id=?",
                             (sort, mid, user_id))
    await db.commit()
    return web.json_response({"message": "Order updated"})


async def handle_api_stats(request):
    try:
        user_id = await api_auth_required(request)
    except web.HTTPForbidden as e:
        return e
    stats = await get_user_overall_stats(user_id)
    return web.json_response(stats)


def setup_routes(app: web.Application):
    app.router.add_get('/', handle_root)
    app.router.add_post('/', handle_root)
    app.router.add_get('/webapp', handle_webapp_index)
    app.router.add_static('/webapp/', path='webapp', show_index=True)
    api = web.RouteTableDef()
    api.add_get('/api/v1/user', handle_api_user)
    api.add_get('/api/v1/monitors', handle_api_monitors_list)
    api.add_get('/api/v1/monitors/{id}', handle_api_monitor_detail)
    api.add_post('/api/v1/monitors', handle_api_monitor_create)
    api.add_patch('/api/v1/monitors/{id}', handle_api_monitor_update)
    api.add_delete('/api/v1/monitors/{id}', handle_api_monitor_delete)
    api.add_get('/api/v1/monitors/{id}/checks', handle_api_monitor_checks)
    api.add_get('/api/v1/monitors/{id}/graph', handle_api_monitor_graph)
    api.add_get('/api/v1/incidents', handle_api_incidents)
    api.add_post('/api/v1/settings', handle_api_settings)
    api.add_get('/api/v1/public/{token}', handle_api_public_page)
    api.add_post('/api/v1/public/token', handle_api_public_token_generate)
    api.add_get('/api/v1/export', handle_api_export)
    api.add_post('/api/v1/monitors/reorder', handle_api_monitors_reorder)
    api.add_get('/api/v1/stats', handle_api_stats)
    app.add_routes(api)
    app.router.add_get('/{path:.*}', handle_heartbeat)
    app.router.add_post('/{path:.*}', handle_heartbeat)


async def start_http_server():
    global heartbeat_runner
    app = web.Application()
    setup_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    heartbeat_runner = runner
    logger.info("HTTP server listening on port %d", PORT)


async def stop_http_server():
    global heartbeat_runner
    if heartbeat_runner:
        await heartbeat_runner.cleanup()
        heartbeat_runner = None
        logger.info("HTTP server stopped")


async def generate_graph(monitor_id: int, hours: int, style: str = "line") -> Optional[BytesIO]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    db = await get_db()
    async with db.execute(
        "SELECT checked_at, response_time_ms, is_up FROM checks "
        "WHERE monitor_id=? AND checked_at>=? ORDER BY checked_at",
        (monitor_id, since)
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return None
    times = [datetime.fromisoformat(r["checked_at"]) for r in rows]
    resp_times = [r["response_time_ms"] if r["response_time_ms"] else 0 for r in rows]
    is_up_vals = [r["is_up"] for r in rows]
    if style == "line":
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(times, resp_times, color='blue', marker='.', linestyle='-', linewidth=1, markersize=2)
        ax.set_ylabel('Response time (ms)')
        ax.grid(True, linestyle='--', alpha=0.6)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    elif style == "status":
        fig, ax = plt.subplots(figsize=(10, 2))
        for i in range(len(times) - 1):
            color = 'green' if is_up_vals[i] else 'red'
            ax.axvspan(times[i], times[i + 1], facecolor=color, alpha=0.3)
        ax.set_ylabel('Status')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    elif style == "pie":
        up_count = sum(is_up_vals)
        down_count = len(is_up_vals) - up_count
        fig, ax = plt.subplots()
        ax.pie([up_count, down_count], labels=['Up', 'Down'],
               colors=['#2ecc71', '#e74c3c'], autopct='%1.1f%%', startangle=90)
        ax.axis('equal')
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf


def _dashboard_button():
    return [InlineKeyboardButton(text="\U0001f5a5 Dashboard",
                                 web_app=WebAppInfo(url=f"{PUBLIC_URL}/webapp"))]


def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4cb Мои мониторы", callback_data="menu_list")],
        [InlineKeyboardButton(text="\u2795 Добавить монитор", callback_data="menu_add")],
        [InlineKeyboardButton(text="\u2699\ufe0f Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton(text="\U0001f4b3 Premium", callback_data="menu_premium")],
        [_dashboard_button()],
    ])


def settings_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4e7 Указать email", callback_data="set_email")],
        [InlineKeyboardButton(text="\U0001f514 Повтор уведомлений", callback_data="set_repeat")],
        [InlineKeyboardButton(text="\U0001f6e0 Техническое окно", callback_data="set_maintenance")],
        [InlineKeyboardButton(text="\U0001f519 Назад", callback_data="menu_main")],
    ])


def premium_kb(is_premium: bool):
    kb = InlineKeyboardBuilder()
    if not is_premium:
        kb.button(text=f"\u2b50 Купить за {PREMIUM_PRICE_STARS} Stars", callback_data="buy_premium")
    else:
        kb.button(text="\u2705 У вас Premium", callback_data="noop")
    kb.button(text="\U0001f381 Подарить Premium", callback_data="gift_premium")
    kb.button(text="\U0001f519 Назад", callback_data="menu_main")
    return kb.as_markup()


def monitor_type_kb():
    kb = InlineKeyboardBuilder()
    items = [
        ("\U0001f310 HTTP(s)", "http"),
        ("\U0001f50d Keyword", "keyword"),
        ("\U0001f4e1 Ping", "ping"),
        ("\U0001f50c Port", "port"),
        ("\U0001f493 Heartbeat", "heartbeat"),
        ("\U0001f30d DNS", "dns"),
        ("\u2699\ufe0f API", "api"),
        ("\U0001f4e6 UDP", "udp"),
    ]
    for text, data in items:
        kb.button(text=text, callback_data=f"addtype_{data}")
    kb.adjust(2)
    return kb.as_markup()


def back_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f519 Назад", callback_data="menu_main")]
    ])


def back_monitor_kb(monitor_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f519 К монитору", callback_data=f"monitor_{monitor_id}")]
    ])


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


class ChangeMonitorInterval(StatesGroup):
    waiting_for_value = State()


class GiftPremium(StatesGroup):
    waiting_for_recipient = State()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await register_user(message.from_user.id, message.chat.id)
    greeting = random.choice(GREETINGS)
    await message.answer(greeting, reply_markup=main_menu_kb())


@dp.callback_query(F.data == "menu_main")
async def main_menu_callback(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "menu_list")
async def list_monitors(callback: CallbackQuery):
    user_id = callback.from_user.id
    monitors = await get_user_monitors(user_id)
    user = await get_user(user_id)
    limit = user["monitor_limit"] if user else MAX_FREE_MONITORS
    active = len([m for m in monitors if not m["is_paused"]])
    text = f"\U0001f4ca Активно: {active}/{limit}\n\n"
    if not monitors:
        text += "У вас пока нет мониторов."
        kb = InlineKeyboardBuilder()
        kb.button(text="\u2795 Добавить", callback_data="menu_add")
        kb.button(text="\U0001f519 Назад", callback_data="menu_main")
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
        await callback.answer()
        return
    kb = InlineKeyboardBuilder()
    for m in monitors:
        mid = m["id"]
        icon = "\U0001f7e2" if m["is_up"] else "\U0001f534" if m["is_up"] is False else "\u26aa\ufe0f"
        label = f"{icon} {m['name'] or mid} [{m['type']}]"
        if m["is_paused"]:
            label += " \u23f8"
        kb.button(text=label, callback_data=f"monitor_{mid}")
    kb.button(text="\u2795 Добавить", callback_data="menu_add")
    kb.button(text="\U0001f519 Назад", callback_data="menu_main")
    kb.adjust(1)
    await callback.message.edit_text("Выберите монитор для управления:", reply_markup=kb.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("monitor_"))
async def show_monitor_card(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Монитор не найден", show_alert=True)
        return
    icon = "\U0001f7e2" if mon["is_up"] else "\U0001f534"
    text = (
        f"{icon} <b>{mon['name'] or monitor_id}</b> [{mon['type']}]\n"
        f"Статус: {mon['last_status'] or 'неизвестен'}\n"
        f"Интервал: {mon['interval_seconds']}с\n"
    )
    if mon["is_paused"]:
        text += "\u23f8 На паузе\n"
    kb = InlineKeyboardBuilder()
    kb.button(text="\u23f8 Пауза" if not mon["is_paused"] else "\u25b6\ufe0f Возобновить",
              callback_data=f"pause_{monitor_id}")
    kb.button(text="\U0001f50d Проверить", callback_data=f"check_{monitor_id}")
    kb.button(text="\U0001f4ca Статистика", callback_data=f"stats_{monitor_id}")
    kb.button(text="\U0001f4c8 Графики", callback_data=f"graph_{monitor_id}")
    kb.button(text="\u23f1 Интервал", callback_data=f"interval_{monitor_id}")
    kb.button(text="\u274c Удалить", callback_data=f"delete_{monitor_id}")
    kb.button(text="\U0001f519 К списку", callback_data="menu_list")
    kb.adjust(2)
    await callback.message.edit_text(text, reply_markup=kb.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "menu_add")
async def start_add(callback: CallbackQuery, state: FSMContext):
    if not await can_add_monitor(callback.from_user.id):
        await callback.answer("Лимит мониторов исчерпан. Получите Premium.", show_alert=True)
        return
    await state.set_state(AddMonitor.choosing_type)
    await callback.message.edit_text("Выберите тип монитора:", reply_markup=monitor_type_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("addtype_"))
async def process_type(callback: CallbackQuery, state: FSMContext):
    mtype = callback.data.split("_")[1]
    await state.update_data(type=mtype)
    await state.set_state(AddMonitor.entering_name)
    await callback.message.edit_text("Введите название (или `-` для авто):", reply_markup=back_main_kb())
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
        "heartbeat": f"Введите путь (или '-' для авто). URL: {PUBLIC_URL}/<путь>",
        "dns": "Введите домен, тип_записи [ожидаемое_значение]:",
        "api": "Введите URL JSONPath [ожидаемое_значение]:",
        "udp": "Введите хост порт [данные] [ожидаемый_ответ]:",
    }
    msg = prompts.get(data['type'], "Введите параметры:")
    if data['type'] == 'heartbeat':
        msg += "\nМожно добавить макс. интервал (сек):"
    await message.answer(msg)
    await state.set_state(AddMonitor.entering_config)


@dp.message(AddMonitor.entering_config)
async def process_config(message: Message, state: FSMContext):
    data = await state.get_data()
    mtype = data['type']
    args = message.text.strip().split()
    config = {}
    try:
        if mtype == "http":
            if len(args) < 1:
                raise ValueError("URL required")
            if not validate_url(args[0]):
                raise ValueError("Invalid URL")
            config["url"] = args[0]
            config["expected_status"] = int(args[1]) if len(args) > 1 else 200
        elif mtype == "keyword":
            if len(args) < 2:
                raise ValueError("URL and keyword required")
            if not validate_url(args[0]):
                raise ValueError("Invalid URL")
            config["url"] = args[0]
            config["keyword"] = args[1]
            config["mode"] = args[2] if len(args) > 2 and args[2] in ("present", "absent") else "present"
        elif mtype == "ping":
            if len(args) < 1:
                raise ValueError("Host required")
            if not validate_host(args[0]):
                raise ValueError("Invalid host")
            config["host"] = args[0]
        elif mtype == "port":
            if len(args) < 2:
                raise ValueError("Host and port required")
            if not validate_host(args[0]):
                raise ValueError("Invalid host")
            config["host"] = args[0]
            config["port"] = int(args[1])
        elif mtype == "heartbeat":
            path = args[0] if args[0] != "-" else str(uuid.uuid4())[:8]
            token = str(uuid.uuid4())
            config["path"] = path
            config["token"] = token
            config["max_interval"] = int(args[1]) if len(args) > 1 else 600
            config["last_heartbeat"] = None
        elif mtype == "dns":
            if len(args) < 1:
                raise ValueError("Domain required")
            config["domain"] = args[0]
            config["record_type"] = args[1] if len(args) > 1 else "A"
            config["expected_value"] = args[2] if len(args) > 2 else None
        elif mtype == "api":
            if len(args) < 1:
                raise ValueError("URL required")
            config["url"] = args[0]
            config["jsonpath"] = args[1] if len(args) > 1 else "$.status"
            config["expected_value"] = args[2] if len(args) > 2 else None
        elif mtype == "udp":
            if len(args) < 2:
                raise ValueError("Host and port required")
            config["host"] = args[0]
            config["port"] = int(args[1])
            config["send_data"] = args[2] if len(args) > 2 else ""
            config["expected_response"] = args[3] if len(args) > 3 else None
        else:
            raise ValueError(f"Unknown type {mtype}")
    except (ValueError, IndexError) as e:
        await message.answer(f"\u274c Ошибка параметров: {e}. Попробуйте снова.")
        return
    await state.update_data(config=config)
    await state.set_state(AddMonitor.entering_interval)
    user = await get_user(message.from_user.id)
    min_int = MIN_INTERVAL_PREMIUM if (user and user["is_premium"]) else MIN_INTERVAL_FREE
    await message.answer(f"Введите интервал проверки в секундах (мин. {min_int}):", reply_markup=back_main_kb())


@dp.message(AddMonitor.entering_interval)
async def process_interval(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    min_interval = MIN_INTERVAL_PREMIUM if (user and user["is_premium"]) else MIN_INTERVAL_FREE
    try:
        interval = int(message.text)
        if interval < min_interval:
            raise ValueError
    except ValueError:
        await message.answer(f"Интервал должен быть числом не менее {min_interval} секунд.")
        return
    await state.update_data(interval=interval)
    data = await state.get_data()
    text = f"<b>Подтверждение</b>\nТип: {data['type']}\nНазвание: {data.get('name', 'авто')}\n"
    if data['type'] == 'heartbeat':
        text += f"URL: {PUBLIC_URL}/{data['config']['path']}\n"
        text += f"Token: <code>{data['config']['token']}</code>\n"
        text += "Заголовок: X-Heartbeat-Token\n"
    else:
        cfg = {k: v for k, v in data['config'].items() if k != 'token'}
        text += f"Параметры: {json.dumps(cfg, indent=2)}\n"
    text += f"Интервал: {interval}с"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2705 Сохранить", callback_data="save_monitor"),
         InlineKeyboardButton(text="\u274c Отмена", callback_data="menu_main")]
    ])
    await state.set_state(AddMonitor.confirm)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "save_monitor", AddMonitor.confirm)
async def save_monitor_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    if not await can_add_monitor(user_id):
        await callback.answer("Лимит исчерпан.", show_alert=True)
        return
    mid = await add_monitor(
        user_id, data['type'],
        data.get('name', f"Monitor {data['type']}"),
        data['config'], data['interval']
    )
    await state.clear()
    await callback.message.edit_text(f"\u2705 Монитор добавлен (ID: {mid})", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("pause_"))
async def toggle_pause(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Не найден", show_alert=True)
        return
    new_paused = not mon["is_paused"]
    await set_monitor_pause(monitor_id, callback.from_user.id, new_paused)
    await callback.answer(f"{'Приостановлен' if new_paused else 'Возобновлён'}")
    await show_monitor_card(callback)


@dp.callback_query(F.data.startswith("check_"))
async def manual_check(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Не найден", show_alert=True)
        return
    result = await perform_check(monitor_id, mon["type"], json.loads(mon["config"]))
    is_up, status_text, resp_time = result[0], result[1], result[2]
    details = result[3] if len(result) > 3 else ""
    status_code = result[4] if len(result) > 4 else None
    await update_monitor_status(monitor_id, is_up, status_text, resp_time, details, status_code)
    icon = "\U0001f7e2 UP" if is_up else "\U0001f534 DOWN"
    await callback.message.answer(
        f"\U0001f504 Результат проверки\nСтатус: {icon}\n"
        f"Время ответа: {resp_time:.0f} мс\n{status_text}",
        reply_markup=back_monitor_kb(monitor_id)
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("stats_"))
async def show_stats(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Монитор не найден", show_alert=True)
        return
    stats_24h = await get_monitor_stats(monitor_id, 24)
    stats_7d = await get_monitor_stats(monitor_id, 168)
    stats_30d = await get_monitor_stats(monitor_id, 720)
    text = (
        f"\U0001f4ca <b>Статистика: {mon['name'] or monitor_id}</b>\n\n"
        f"\U0001f4c5 За 24ч:\n"
        f"  Проверок: {stats_24h['total']}\n"
        f"  Uptime: {stats_24h['uptime']}%\n"
        f"  Средн. отклик: {stats_24h['avg_response_time']} мс\n\n"
        f"\U0001f4c5 За 7д:\n"
        f"  Проверок: {stats_7d['total']}\n"
        f"  Uptime: {stats_7d['uptime']}%\n"
        f"  Средн. отклик: {stats_7d['avg_response_time']} мс\n\n"
        f"\U0001f4c5 За 30д:\n"
        f"  Проверок: {stats_30d['total']}\n"
        f"  Uptime: {stats_30d['uptime']}%\n"
        f"  Средн. отклик: {stats_30d['avg_response_time']} мс"
    )
    await callback.message.edit_text(text, reply_markup=back_monitor_kb(monitor_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("graph_"))
async def graph_menu(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\U0001f4c8 Линия (24ч)", callback_data=f"gdraw_{monitor_id}_24_line"),
         InlineKeyboardButton(text="\U0001f4ca Статус (24ч)", callback_data=f"gdraw_{monitor_id}_24_status")],
        [InlineKeyboardButton(text="\U0001f967 Круг (24ч)", callback_data=f"gdraw_{monitor_id}_24_pie")],
        [InlineKeyboardButton(text="\U0001f519 К монитору", callback_data=f"monitor_{monitor_id}")]
    ])
    await callback.message.edit_text("Выберите тип графика:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("gdraw_"))
async def draw_graph_handler(callback: CallbackQuery):
    _, mid, hours, style = callback.data.split("_")
    monitor_id = int(mid)
    buf = await generate_graph(monitor_id, int(hours), style)
    if buf:
        await callback.message.reply_photo(
            BufferedInputFile(buf.read(), filename="graph.png"),
            reply_markup=back_monitor_kb(monitor_id)
        )
    else:
        await callback.answer("Нет данных", show_alert=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("interval_"))
async def change_interval_start(callback: CallbackQuery, state: FSMContext):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Монитор не найден", show_alert=True)
        return
    await state.update_data(interval_monitor_id=monitor_id)
    user = await get_user(callback.from_user.id)
    min_int = MIN_INTERVAL_PREMIUM if (user and user["is_premium"]) else MIN_INTERVAL_FREE
    await state.set_state(ChangeMonitorInterval.waiting_for_value)
    await callback.message.edit_text(
        f"Текущий интервал: {mon['interval_seconds']}с\n"
        f"Введите новый интервал (мин. {min_int}с):",
        reply_markup=back_monitor_kb(monitor_id)
    )
    await callback.answer()


@dp.message(ChangeMonitorInterval.waiting_for_value)
async def change_interval_value(message: Message, state: FSMContext):
    data = await state.get_data()
    monitor_id = data["interval_monitor_id"]
    mon = await get_monitor(monitor_id, message.from_user.id)
    if not mon:
        await message.answer("Монитор не найден.")
        await state.clear()
        return
    user = await get_user(message.from_user.id)
    min_interval = MIN_INTERVAL_PREMIUM if (user and user["is_premium"]) else MIN_INTERVAL_FREE
    try:
        interval = int(message.text)
        if interval < min_interval:
            raise ValueError
    except ValueError:
        await message.answer(f"Интервал должен быть числом не менее {min_interval} секунд.")
        return
    await set_monitor_interval(monitor_id, message.from_user.id, interval)
    await state.clear()
    await message.answer(f"\u2705 Интервал обновлён: {interval}с", reply_markup=main_menu_kb())


@dp.callback_query(F.data.startswith("delete_"))
async def delete_confirm(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    mon = await get_monitor(monitor_id, callback.from_user.id)
    if not mon:
        await callback.answer("Монитор не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="\u2705 Да, удалить", callback_data=f"confirm_delete_{monitor_id}"),
         InlineKeyboardButton(text="\u274c Отмена", callback_data=f"monitor_{monitor_id}")]
    ])
    await callback.message.edit_text(
        f"\u274c Удалить монитор <b>{mon['name'] or monitor_id}</b>?", reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_delete_"))
async def delete_execute(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[2])
    if await delete_monitor(monitor_id, callback.from_user.id):
        await callback.answer("Монитор удалён")
        await list_monitors(callback)
    else:
        await callback.answer("Ошибка удаления", show_alert=True)


@dp.callback_query(F.data == "menu_settings")
async def show_settings(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    email = user["email"] if user and user["email"] else "не указан"
    repeat = user["alert_repeat"] if user else 0
    maint_from = user["maintenance_from"] if user else None
    maint_to = user["maintenance_to"] if user else None
    maint = f"{maint_from} \u2013 {maint_to}" if maint_from else "не задано"
    text = (
        f"\u2699\ufe0f Настройки\n\n"
        f"\U0001f4e7 Email: {email}\n"
        f"\U0001f514 Повтор: каждые {repeat} мин.\n"
        f"\U0001f6e0 Тех. окно: {maint}"
    )
    await callback.message.edit_text(text, reply_markup=settings_kb())
    await callback.answer()


@dp.callback_query(F.data == "set_email")
async def set_email_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.waiting_for_email)
    await callback.message.edit_text("\U0001f4e7 Введите ваш email:", reply_markup=back_main_kb())
    await callback.answer()


@dp.message(Settings.waiting_for_email)
async def process_email(message: Message, state: FSMContext):
    email = message.text.strip()
    if "@" not in email or "." not in email:
        await message.answer("Некорректный email. Попробуйте ещё раз:")
        return
    await set_user_email(message.from_user.id, email)
    await state.clear()
    await message.answer("\u2705 Email сохранён!", reply_markup=main_menu_kb())


@dp.callback_query(F.data == "set_repeat")
async def set_repeat_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.waiting_for_repeat)
    await callback.message.edit_text(
        "\U0001f514 Введите интервал повтора в минутах (0 = без повтора):",
        reply_markup=back_main_kb()
    )
    await callback.answer()


@dp.message(Settings.waiting_for_repeat)
async def process_repeat(message: Message, state: FSMContext):
    try:
        minutes = int(message.text)
        if minutes < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое неотрицательное число.")
        return
    await set_user_alert_repeat(message.from_user.id, minutes)
    await state.clear()
    await message.answer(f"\U0001f514 Повтор каждые {minutes} мин.", reply_markup=main_menu_kb())


@dp.callback_query(F.data == "set_maintenance")
async def set_maintenance_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Settings.waiting_for_maintenance)
    await callback.message.edit_text(
        "\U0001f6e0 Введите начало и конец техокна в формате ЧЧ:ММ ЧЧ:ММ:",
        reply_markup=back_main_kb()
    )
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
    await set_user_maintenance(message.from_user.id, parts[0], parts[1])
    await state.clear()
    await message.answer(
        f"\U0001f6e0 Тех. окно: {parts[0]} \u2013 {parts[1]}", reply_markup=main_menu_kb()
    )


@dp.callback_query(F.data == "menu_premium")
async def premium_info(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    is_prem = user["is_premium"] if user else 0
    text = "\U0001f48e Premium\n\n" + (
        "\u2705 У вас активен Premium. Спасибо!" if is_prem
        else f"\U0001f4b0 Лимит: до {MAX_PREMIUM_MONITORS} мониторов\n"
             f"\u23f0 Мин. интервал: {MIN_INTERVAL_PREMIUM}с\n"
             f"\u2b50 Цена: {PREMIUM_PRICE_STARS} Stars"
    )
    await callback.message.edit_text(text, reply_markup=premium_kb(is_prem))
    await callback.answer()


@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Premium UptimeBot",
        description=f"До {MAX_PREMIUM_MONITORS} мониторов, интервал от {MIN_INTERVAL_PREMIUM}с",
        payload="premium_upgrade",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_PRICE_STARS)],
        start_parameter="premium"
    )
    await callback.answer("\U0001f4ec Счёт отправлен")


@dp.callback_query(F.data == "gift_premium")
async def gift_premium_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GiftPremium.waiting_for_recipient)
    await callback.message.edit_text(
        "\U0001f381 Введите Telegram ID пользователя, которому хотите подарить Premium:",
        reply_markup=back_main_kb()
    )
    await callback.answer()


@dp.message(GiftPremium.waiting_for_recipient)
async def gift_premium_recipient(message: Message, state: FSMContext):
    try:
        recipient_id = int(message.text.strip())
    except ValueError:
        await message.answer("Пожалуйста, введите числовой ID пользователя.")
        return
    if recipient_id == message.from_user.id:
        await message.answer("Нельзя подарить Premium самому себе. Используйте «Купить».")
        return
    recipient = await get_user(recipient_id)
    if not recipient:
        await message.answer(
            "Пользователь с таким ID не найден в боте.\n"
            "Убедитесь, что он запустил бота командой /start."
        )
        return
    if recipient["is_premium"]:
        await message.answer("Этот пользователь уже имеет Premium.")
        await state.clear()
        return
    await state.update_data(recipient_id=recipient_id, recipient_chat_id=recipient["chat_id"])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"\u2b50 Оплатить {PREMIUM_PRICE_STARS} Stars",
                               callback_data="gift_pay")],
        [InlineKeyboardButton(text="\u274c Отмена", callback_data="menu_premium")]
    ])
    await message.answer(
        f"\U0001f381 Подарок Premium для пользователя <code>{recipient_id}</code>\n"
        f"Сумма: {PREMIUM_PRICE_STARS} Stars\n\n"
        f"Подтвердите оплату:",
        reply_markup=kb
    )


@dp.callback_query(F.data == "gift_pay")
async def gift_pay(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipient_id = data["recipient_id"]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Premium в подарок",
        description=f"Premium для пользователя {recipient_id}",
        payload=f"gift:{recipient_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Premium Gift", amount=PREMIUM_PRICE_STARS)],
        start_parameter="gift_premium"
    )
    await callback.answer("\U0001f4ec Счёт отправлен")


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("gift:"):
        recipient_id = int(payload.split(":")[1])
        recipient = await get_user(recipient_id)
        if not recipient:
            await message.answer("Ошибка: получатель не найден.")
            return
        db = await get_db()
        await db.execute("UPDATE users SET monitor_limit=?, is_premium=1 WHERE user_id=?",
                         (MAX_PREMIUM_MONITORS, recipient_id))
        await db.commit()
        await message.answer(
            f"\U0001f389 Premium успешно подарен пользователю <code>{recipient_id}</code>!",
            reply_markup=main_menu_kb()
        )
        try:
            await bot.send_message(
                recipient["chat_id"],
                f"\U0001f389 Вам подарили Premium UptimeBot!\n"
                f"Лимит увеличен до {MAX_PREMIUM_MONITORS} мониторов.",
                reply_markup=main_menu_kb()
            )
        except Exception as e:
            logger.error("Gift notification error: %s", e)
    else:
        db = await get_db()
        await db.execute("UPDATE users SET monitor_limit=?, is_premium=1 WHERE user_id=?",
                         (MAX_PREMIUM_MONITORS, message.from_user.id))
        await db.commit()
        await message.answer(
            "\U0001f389 Вы Premium! Лимит увеличен до 10, интервал от 30с.",
            reply_markup=main_menu_kb()
        )


@dp.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    await callback.answer()


async def on_startup():
    await init_db()
    await ensure_vip_user()
    await start_http_server()
    asyncio.create_task(scheduler_loop())


async def on_shutdown():
    logger.info("Shutting down...")
    if http_session and not http_session.closed:
        await http_session.close()
    await stop_http_server()
    if db_conn:
        await db_conn.close()
    _email_task_set.difference_update(t for t in _email_task_set if t.done())
    if _email_task_set:
        await asyncio.gather(*_email_task_set, return_exceptions=True)
    logger.info("Shutdown complete")


async def main():
    await on_startup()
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            logger.warning("Signal handlers not supported on this platform")
    polling_task = asyncio.create_task(dp.start_polling(bot))
    await stop_event.wait()
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    await on_shutdown()


if __name__ == "__main__":
    asyncio.run(main())
