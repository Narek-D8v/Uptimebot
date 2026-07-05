import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

import aiohttp
import aiosqlite
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile, LabeledPrice,
    PreCheckoutQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web

# ------------------------------
# Конфигурация
# ------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана!")

DATABASE = "uptime_bot.db"
PORT = int(os.environ.get("PORT", 8080))          # Порт, который требует Render
HEARTBEAT_HOST = "0.0.0.0"
DEFAULT_INTERVAL = 300                              # 5 минут
DEFAULT_TIMEOUT = 10                                # таймаут запроса
CHECK_HISTORY_HOURS = 24
MAX_FREE_MONITORS = 3
MAX_PREMIUM_MONITORS = 10
PREMIUM_PRICE_STARS = 5

# Публичный URL сервиса (если запущен на Render, иначе localhost)
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PORT}")
# Убираем trailing slash
PUBLIC_URL = PUBLIC_URL.rstrip('/')

# ------------------------------
# Инициализация
# ------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# Глобальный aiohttp сессия для HTTP/API/Keyword проверок (переиспользование)
http_session: Optional[aiohttp.ClientSession] = None

# ------------------------------
# База данных
# ------------------------------
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                monitor_limit INTEGER DEFAULT 3,
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

async def register_user(user_id: int, chat_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, chat_id) VALUES (?, ?)",
            (user_id, chat_id)
        )
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

async def set_user_limit(user_id: int, limit: int):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE users SET monitor_limit = ? WHERE user_id = ?", (limit, user_id))
        await db.commit()

async def get_active_monitor_count(user_id: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM monitors WHERE user_id = ? AND is_paused = 0",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

async def can_add_monitor(user_id: int) -> bool:
    count = await get_active_monitor_count(user_id)
    limit = MAX_FREE_MONITORS
    user = await get_user(user_id)
    if user:
        limit = user[2]  # monitor_limit
    return count < limit

async def add_monitor(user_id: int, monitor_type: str, name: str, config: dict, interval: int) -> int:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "INSERT INTO monitors (user_id, type, name, config, interval_seconds) VALUES (?, ?, ?, ?, ?)",
            (user_id, monitor_type, name, json.dumps(config), interval)
        )
        await db.commit()
        return cursor.lastrowid

async def delete_monitor(monitor_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute(
            "DELETE FROM monitors WHERE id = ? AND user_id = ?",
            (monitor_id, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0

async def get_monitor(monitor_id: int, user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT * FROM monitors WHERE id = ? AND user_id = ?",
            (monitor_id, user_id)
        ) as cur:
            return await cur.fetchone()

async def get_user_monitors(user_id: int):
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT id, type, name, config, interval_seconds, is_paused, is_up, last_checked FROM monitors WHERE user_id = ?",
            (user_id,)
        ) as cur:
            return await cur.fetchall()

async def get_all_active_monitors():
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT m.id, m.user_id, m.type, m.config, m.interval_seconds, m.last_checked, m.is_up, m.alert_until, u.chat_id, u.alert_repeat, u.maintenance_from, u.maintenance_to "
            "FROM monitors m JOIN users u ON m.user_id = u.user_id WHERE m.is_paused = 0"
        ) as cur:
            return await cur.fetchall()

async def update_monitor_status(monitor_id: int, is_up: bool, status_text: str, response_time_ms: float, details: str = ""):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute(
            "UPDATE monitors SET last_status = ?, is_up = ?, last_checked = ?, consecutive_failures = CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END WHERE id = ?",
            (status_text, is_up, datetime.utcnow(), is_up, monitor_id)
        )
        await db.execute(
            "INSERT INTO checks (monitor_id, status_code, response_time_ms, is_up, details) VALUES (?, ?, ?, ?, ?)",
            (monitor_id, 0 if not isinstance(status_text, int) else status_text, response_time_ms, is_up, details)
        )
        await db.commit()

async def set_monitor_pause(monitor_id: int, user_id: int, paused: bool):
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE monitors SET is_paused = ? WHERE id = ? AND user_id = ?", (int(paused), monitor_id, user_id))
        await db.commit()

async def get_monitor_stats(monitor_id: int, hours: int):
    since = datetime.utcnow() - timedelta(hours=hours)
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT COUNT(*), SUM(is_up), AVG(response_time_ms) FROM checks WHERE monitor_id = ? AND checked_at >= ?",
            (monitor_id, since)
        ) as cur:
            total, up_count, avg_resp = await cur.fetchone()
        if total == 0:
            return {"total": 0, "uptime": 100.0, "avg_response_time": 0}
        uptime = (up_count / total) * 100 if total else 100.0
        return {
            "total": total,
            "uptime": round(uptime, 2),
            "avg_response_time": round(avg_resp, 1) if avg_resp else 0
        }

# ------------------------------
# Проверки всех типов
# ------------------------------
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
    mode = config.get("mode", "present")  # present/absent
    timeout = config.get("timeout", DEFAULT_TIMEOUT)
    start = time.monotonic()
    try:
        async with http_session.get(url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as resp:
            body = await resp.text()
            resp_time = (time.monotonic() - start) * 1000
            found = keyword in body
            is_up = (found if mode == "present" else not found)
            status_text = f"Keyword {'found' if found else 'not found'}"
            return is_up, status_text, resp_time, f"Status: {resp.status}"
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
        self.expected = expected

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

# ------------------------------
# Планировщик
# ------------------------------
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
            now = datetime.utcnow()
            tasks = []
            for mon in monitors:
                (mid, user_id, mtype, config_json, interval, last_checked,
                 prev_is_up, alert_until, chat_id, alert_repeat, maint_from, maint_to) = mon
                if last_checked:
                    last = datetime.fromisoformat(last_checked) if isinstance(last_checked, str) else last_checked
                    next_check = last + timedelta(seconds=interval)
                    if now < next_check:
                        continue
                in_maintenance = False
                if maint_from and maint_to:
                    try:
                        now_time = now.time()
                        from_time = datetime.strptime(maint_from, "%H:%M").time()
                        to_time = datetime.strptime(maint_to, "%H:%M").time()
                        if from_time <= to_time:
                            in_maintenance = from_time <= now_time <= to_time
                        else:
                            in_maintenance = now_time >= from_time or now_time <= to_time
                    except:
                        pass
                tasks.append(check_and_notify(mid, mtype, json.loads(config_json), chat_id,
                                              prev_is_up, alert_until, alert_repeat, in_maintenance, sem))
            if tasks:
                await asyncio.gather(*tasks)
        except Exception as e:
            logging.error(f"Scheduler error: {e}")
        await asyncio.sleep(15)

async def check_and_notify(mid, mtype, config, chat_id, prev_is_up, alert_until, alert_repeat, in_maintenance, sem):
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
            if not is_up and alert_repeat > 0:
                async with aiosqlite.connect(DATABASE) as db:
                    await db.execute("UPDATE monitors SET alert_until = ? WHERE id = ?",
                                     (datetime.utcnow() + timedelta(minutes=alert_repeat), mid))
                    await db.commit()

# ------------------------------
# Heartbeat веб-сервер
# ------------------------------
async def heartbeat_handler(request):
    path = request.match_info['path']
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT id, config FROM monitors WHERE type='heartbeat' AND json_extract(config, '$.path') = ?",
            (path,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return web.Response(text="Not found", status=404)
            config = json.loads(row[1])
            config['last_heartbeat'] = datetime.utcnow().isoformat()
            await db.execute("UPDATE monitors SET config = ? WHERE id = ?", (json.dumps(config), row[0]))
            await db.commit()
    return web.Response(text="OK")
    
async def handle_root(request):
    return web.Response(text="Ellis is watching 👀")
    
async def start_heartbeat_server():
    app = web.Application()
    app.router.add_get('/{path:.*}', heartbeat_handler)
    app.router.add_get('/', handle_root)
    app.router.add_post('/{path:.*}', heartbeat_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HEARTBEAT_HOST, PORT)
    await site.start()
    logging.info(f"Heartbeat server started on port {PORT}")

# ------------------------------
# Графики
# ------------------------------
async def generate_uptime_graph(monitor_id: int, hours: int) -> Optional[BytesIO]:
    since = datetime.utcnow() - timedelta(hours=hours)
    async with aiosqlite.connect(DATABASE) as db:
        async with db.execute(
            "SELECT checked_at, response_time_ms, is_up FROM checks WHERE monitor_id = ? AND checked_at >= ? ORDER BY checked_at",
            (monitor_id, since)
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        return None
    times = [datetime.fromisoformat(r[0]) for r in rows]
    response_times = [r[1] if r[1] else 0 for r in rows]
    is_up = [r[2] for r in rows]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    ax1.plot(times, response_times, color='blue', marker='.', linestyle='-', linewidth=1, markersize=2)
    ax1.set_ylabel('Response time (ms)')
    ax1.grid(True, linestyle='--', alpha=0.6)

    for i in range(len(times) - 1):
        color = 'green' if is_up[i] else 'red'
        ax2.axvspan(times[i], times[i+1], facecolor=color, alpha=0.3)
    ax2.set_ylabel('Status')
    ax2.set_xlabel('Time')
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf

# ------------------------------
# Клавиатуры
# ------------------------------
def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои мониторы", callback_data="menu_list")],
        [InlineKeyboardButton(text="➕ Добавить монитор", callback_data="menu_add")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton(text="💳 Premium", callback_data="menu_premium")],
    ])

def monitor_types_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🌐 HTTP(s)", callback_data="addtype_http")
    kb.button(text="🔍 Keyword", callback_data="addtype_keyword")
    kb.button(text="📡 Ping", callback_data="addtype_ping")
    kb.button(text="🔌 Port", callback_data="addtype_port")
    kb.button(text="💓 Heartbeat", callback_data="addtype_heartbeat")
    kb.button(text="🌍 DNS", callback_data="addtype_dns")
    kb.button(text="⚙️ API", callback_data="addtype_api")
    kb.button(text="📦 UDP", callback_data="addtype_udp")
    kb.adjust(2)
    return kb.as_markup()

def back_button(callback_data: str = "menu_list"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=callback_data)]])

def monitor_card_keyboard(monitor_id: int, is_paused: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸ Пауза" if not is_paused else "▶️ Возобновить", callback_data=f"pause_{monitor_id}")
    kb.button(text="🔄 Проверить", callback_data=f"check_{monitor_id}")
    kb.button(text="📈 График", callback_data=f"graph_{monitor_id}")
    kb.button(text="❌ Удалить", callback_data=f"delete_{monitor_id}")
    kb.adjust(2)
    return kb.as_markup()

# ------------------------------
# FSM для добавления
# ------------------------------
class AddMonitor(StatesGroup):
    choosing_type = State()
    entering_name = State()
    entering_config = State()
    entering_interval = State()
    confirm = State()

# ------------------------------
# Обработчики команд
# ------------------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await register_user(message.from_user.id, message.chat.id)
    await message.answer(
        "👋 Привет! Я бот мониторинга сайтов и сервисов.\n"
        "Выберите действие:",
        reply_markup=main_menu_keyboard()
    )

@dp.callback_query(F.data == "menu_list")
async def show_list(callback: CallbackQuery):
    user_id = callback.from_user.id
    monitors = await get_user_monitors(user_id)
    count = len([m for m in monitors if not m[5]])  # не на паузе
    limit = MAX_FREE_MONITORS
    user = await get_user(user_id)
    if user:
        limit = user[2]
    text = f"📊 Активно: {count}/{limit}\n\n"
    if not monitors:
        text += "У вас нет мониторов."
        await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
        return
    for m in monitors:
        mid, mtype, name, config_json, interval, paused, is_up, last_checked = m
        icon = "🟢" if is_up else "🔴" if is_up is False else "⚪️"
        paused_str = " (пауза)" if paused else ""
        last = last_checked[:19] if last_checked else "никогда"
        text += f"{icon} <b>{name or mid}</b> [{mtype}]{paused_str}\n"
        text += f"   Последняя проверка: {last}\n"
        text += f"   Интервал: {interval}с\n\n"
    await callback.message.edit_text(text, reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "menu_add")
async def start_add(callback: CallbackQuery, state: FSMContext):
    if not await can_add_monitor(callback.from_user.id):
        await callback.answer("Лимит мониторов исчерпан. Перейдите на Premium.", show_alert=True)
        return
    await state.set_state(AddMonitor.choosing_type)
    await callback.message.edit_text("Выберите тип монитора:", reply_markup=monitor_types_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("addtype_"))
async def process_type(callback: CallbackQuery, state: FSMContext):
    mtype = callback.data.split("_")[1]
    await state.update_data(type=mtype)
    await state.set_state(AddMonitor.entering_name)
    await callback.message.edit_text("Введите название монитора (или отправьте `-` для автоматического):", reply_markup=back_button())
    await callback.answer()

@dp.message(AddMonitor.entering_name)
async def process_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if name == "-":
        name = None
    await state.update_data(name=name)
    await state.set_state(AddMonitor.entering_config)
    data = await state.get_data()
    mtype = data["type"]
    prompts = {
        "http": "Введите URL (https://example.com) и ожидаемый статус (200) через пробел, или только URL.",
        "keyword": "Введите URL и ключевое слово через пробел (режим present/absent опционально).",
        "ping": "Введите хост или IP.",
        "port": "Введите хост и порт через пробел (например, smtp.example.com 587).",
        "heartbeat": f"Отправьте путь (латиница, цифры, дефис) или '-' для авто-генерации.\nHeartbeat URL будет: {PUBLIC_URL}/<путь>",
        "dns": "Введите домен, тип записи (A, AAAA, MX...) и опционально ожидаемое значение через пробел.",
        "api": "Введите URL, JSONPath и ожидаемое значение через пробел.",
        "udp": "Введите хост, порт и опционально отправляемые данные и ожидаемый ответ через пробел.",
    }
    await message.answer(prompts.get(mtype, "Введите параметры конфигурации."))

@dp.message(AddMonitor.entering_config)
async def process_config(message: Message, state: FSMContext):
    data = await state.get_data()
    mtype = data["type"]
    args = message.text.strip().split()
    config = {}
    try:
        if mtype == "http":
            url = args[0]
            config["url"] = url
            config["expected_status"] = int(args[1]) if len(args) > 1 else 200
        elif mtype == "keyword":
            url = args[0]
            if len(args) < 2:
                raise ValueError("Не указано ключевое слово")
            config["url"] = url
            config["keyword"] = args[1] if len(args) >= 2 else ""
            config["mode"] = args[2] if len(args) >= 3 and args[2] in ("present", "absent") else "present"
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
            raise ValueError("Неизвестный тип")
    except Exception as e:
        await message.answer(f"❌ Ошибка в параметрах: {e}\nПопробуйте снова.")
        return
    await state.update_data(config=config)
    await state.set_state(AddMonitor.entering_interval)
    await message.answer("Введите интервал проверки в секундах (минимум 60):", reply_markup=back_button())

@dp.message(AddMonitor.entering_interval)
async def process_interval(message: Message, state: FSMContext):
    try:
        interval = int(message.text)
        if interval < 60:
            raise ValueError
    except:
        await message.answer("Интервал должен быть числом не менее 60 секунд.")
        return
    await state.update_data(interval=interval)
    data = await state.get_data()
    mtype = data["type"]
    name = data.get("name", "Без имени")
    config = data["config"]
    text = f"<b>Подтверждение:</b>\nТип: {mtype}\nНазвание: {name}\n"
    if mtype == "heartbeat":
        text += f"Путь: {config['path']}\nМакс. интервал: {config['max_interval']}с\n"
        text += f"URL heartbeat: {PUBLIC_URL}/{config['path']}\n"
    else:
        text += f"Параметры: {json.dumps(config, indent=2)}\n"
    text += f"Интервал: {interval}с"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить", callback_data="save_monitor"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="menu_list")]
    ])
    await state.set_state(AddMonitor.confirm)
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "save_monitor", AddMonitor.confirm)
async def save_monitor(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    if not await can_add_monitor(user_id):
        await callback.answer("Лимит мониторов исчерпан.", show_alert=True)
        return
    mtype = data["type"]
    name = data.get("name", f"Monitor {mtype}")
    config = data["config"]
    interval = data["interval"]
    monitor_id = await add_monitor(user_id, mtype, name, config, interval)
    await state.clear()
    await callback.message.edit_text(f"✅ Монитор <b>{name}</b> (ID: {monitor_id}) добавлен.", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("pause_"))
async def toggle_pause(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    mon = await get_monitor(monitor_id, user_id)
    if not mon:
        await callback.answer("Монитор не найден", show_alert=True)
        return
    new_paused = not mon[6]
    await set_monitor_pause(monitor_id, user_id, new_paused)
    await callback.answer(f"Монитор {'приостановлен' if new_paused else 'возобновлён'}")
    await show_list(callback)

@dp.callback_query(F.data.startswith("check_"))
async def manual_check(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    mon = await get_monitor(monitor_id, user_id)
    if not mon:
        await callback.answer("Монитор не найден", show_alert=True)
        return
    mtype = mon[2]
    config = json.loads(mon[4])
    is_up, status_text, resp_time, details = await perform_check(monitor_id, mtype, config)
    await update_monitor_status(monitor_id, is_up, status_text, resp_time, details)
    await callback.message.answer(
        f"🔄 Результат проверки монитора {mon[3] or monitor_id}:\n"
        f"Статус: {'🟢 UP' if is_up else '🔴 DOWN'}\n"
        f"Время ответа: {resp_time:.0f} мс\n"
        f"Детали: {status_text}"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("delete_"))
async def delete_monitor_callback(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    if await delete_monitor(monitor_id, user_id):
        await callback.answer("Монитор удалён")
        await show_list(callback)
    else:
        await callback.answer("Ошибка удаления", show_alert=True)

@dp.callback_query(F.data.startswith("graph_"))
async def show_graph_menu(callback: CallbackQuery):
    monitor_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="24 часа", callback_data=f"graphdraw_{monitor_id}_24"),
         InlineKeyboardButton(text="7 дней", callback_data=f"graphdraw_{monitor_id}_168"),
         InlineKeyboardButton(text="30 дней", callback_data=f"graphdraw_{monitor_id}_720")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_list")]
    ])
    await callback.message.edit_text("Выберите период для графика:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("graphdraw_"))
async def draw_graph(callback: CallbackQuery):
    _, monitor_id_str, hours_str = callback.data.split("_")
    monitor_id = int(monitor_id_str)
    hours = int(hours_str)
    buf = await generate_uptime_graph(monitor_id, hours)
    if buf:
        await callback.message.reply_photo(
            photo=BufferedInputFile(buf.read(), filename="graph.png"),
            caption=f"📈 Доступность за {hours} часов"
        )
    else:
        await callback.answer("Недостаточно данных для графика", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data == "menu_settings")
async def settings_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    if not user:
        return
    limit = user[2]
    text = f"⚙️ Настройки\nЛимит мониторов: {limit}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ Повтор уведомлений (мин)", callback_data="set_repeat")],
        [InlineKeyboardButton(text="🛠 Техническое окно", callback_data="set_maintenance")],
        [InlineKeyboardButton(text="💳 Premium", callback_data="menu_premium")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_list")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "menu_premium")
async def premium_info(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = await get_user(user_id)
    limit = user[2] if user else MAX_FREE_MONITORS
    if limit >= MAX_PREMIUM_MONITORS:
        await callback.message.edit_text("У вас уже Premium (10 мониторов).", reply_markup=main_menu_keyboard())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⭐ Купить за {PREMIUM_PRICE_STARS} Stars", callback_data="buy_premium")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_settings")]
    ])
    await callback.message.edit_text(
        f"Премиум даёт до {MAX_PREMIUM_MONITORS} мониторов. Стоимость: {PREMIUM_PRICE_STARS} Telegram Stars.",
        reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_premium")
async def initiate_payment(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Premium UptimeBot",
        description=f"Расширение лимита мониторов до {MAX_PREMIUM_MONITORS} (навсегда)",
        payload="premium_upgrade",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Premium подписка", amount=PREMIUM_PRICE_STARS)],
        start_parameter="uptime_premium"
    )
    await callback.answer("Счёт отправлен. Оплатите в следующем сообщении.")

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    await set_user_limit(message.from_user.id, MAX_PREMIUM_MONITORS)
    await message.answer("🎉 Спасибо! Лимит мониторов увеличен до 10.")

# ------------------------------
# Запуск
# ------------------------------
async def on_startup():
    await init_db()
    # Запускаем heartbeat-сервер на порту из переменной окружения
    asyncio.create_task(start_heartbeat_server())
    # Запускаем планировщик
    asyncio.create_task(scheduler_loop())

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
