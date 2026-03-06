import os
import time
import json
import sqlite3
import logging
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# ---------- Minimal .env loader (no python-dotenv needed) ----------
def load_env_file(path: str = ".env") -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass

load_env_file(".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()  # optional
PUBLISH_CHAT_ID = os.getenv("PUBLISH_CHAT_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

WELCOME = (
    "Привет! 🚖\n\n"
    "Я Ваш помощник в мире такси! Здесь Вы можете найти подходящего таксиста или стать им самим. "
    "Если Вы пассажир, сообщите мне о Вашем маршруте, и я помогу Вам найти лучшую поездку. "
    "Таксисты, загружайте свои объявления, чтобы пассажиры могли Вас найти! "
    "Давайте сделаем Ваши поездки удобнее и безопаснее!"
)

CITIES = [
    "ТАШКЕНТ","СИРДАРЬЯ","ДЖИЗЗАК","САМАРКАНД","ФЕРГАНА","НАМАНГАН","АНДИЖАН","КАШКАДАРЬЯ",
    "СУРХАНДАРЬЯ","БУХАРА","НАВАИ","ХОРЕЗМ","КАРАКАЛПАКИЯ"
]
CARS = ["Коболт","Джентра","Нексия","Нексия 3","Каптива","Малибу 1","Малибу 2","Трекер"]



# --- Topics mapping (RU Cyrillic -> UZ Latin key) ---
CITY_RU_TO_UZ = {
    "ТАШКЕНТ": "Toshkent",
    "СИРДАРЬЯ": "Sirdaryo",
    "ДЖИЗЗАК": "Jizzax",
    "САМАРКАНД": "Samarqand",
    "ФЕРГАНА": "Fargona",
    "НАМАНГАН": "Namangan",
    "АНДИЖАН": "Andijon",
    "КАШКАДАРЬЯ": "Qashqadaryo",
    "СУРХАНДАРЬЯ": "Surxondaryo",
    "БУХАРА": "Buxoro",
    "НАВАИ": "Navoiy",
    "ХОРЕЗМ": "Xorazm",
    "КАРАКАЛПАКИЯ": "Qoraqalpogiston",
}

def city_key_uz(city_ru: str) -> str:
    return CITY_RU_TO_UZ.get((city_ru or "").strip().upper(), (city_ru or "").strip().title())

def tagify(s: str) -> str:
    # hashtag-safe: only letters/digits/underscore
    out = []
    for ch in (s or ""):
        if ch.isalnum():
            out.append(ch)
        elif ch in ["_",]:
            out.append(ch)
    return "".join(out) or "Route"

def route_code(a: str, b: str) -> str:
    # 3-letter code from Uzbek keys
    A = city_key_uz(a)
    B = city_key_uz(b)
    A3 = tagify(A)[:3].upper()
    B3 = tagify(B)[:3].upper()
    return f"{A3}_{B3}"

PAGE_SIZE = 5

# ---------- DB ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS profiles(
        user_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS topic_bindings(
        city_key TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        thread_id INTEGER NOT NULL,
        topic_title TEXT
    )""")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trips(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        data_json TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""")
    conn.commit()
    conn.close()



def topic_set(city_key: str, chat_id: int, thread_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO topic_bindings(city_key, chat_id, thread_id) VALUES(?,?,?) "
        "ON CONFLICT(city_key) DO UPDATE SET chat_id=excluded.chat_id, thread_id=excluded.thread_id",
        (city_key, chat_id, thread_id),
    )
    conn.commit()
    conn.close()

def topic_get(city_key: str):
    conn = db()
    r = conn.execute(
        "SELECT city_key, chat_id, thread_id FROM topic_bindings WHERE city_key=?",
        (city_key,)
    ).fetchone()
    conn.close()
    if not r:
        return None
    return {"city_key": r["city_key"], "chat_id": int(r["chat_id"]), "thread_id": int(r["thread_id"])}

def topic_list():
    conn = db()
    rows = conn.execute(
        "SELECT city_key, chat_id, thread_id FROM topic_bindings ORDER BY city_key"
    ).fetchall()
    conn.close()
    return [{"city_key": r["city_key"], "chat_id": int(r["chat_id"]), "thread_id": int(r["thread_id"])} for r in rows]


def profile_get(user_id: int) -> Optional[Dict[str, str]]:
    conn = db()
    r = conn.execute("SELECT name, phone FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not r:
        return None
    return {"name": r["name"], "phone": r["phone"]}

def profile_set(user_id: int, name: str, phone: str):
    conn = db()
    conn.execute(
        "INSERT INTO profiles(user_id,name,phone) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, phone=excluded.phone",
        (user_id, name, phone),
    )
    conn.commit()
    conn.close()

def trip_insert(user_id: int, trip_dict: Dict[str, Any]) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO trips(user_id,data_json,created_at) VALUES(?,?,?)",
        (user_id, json.dumps(trip_dict, ensure_ascii=False), int(time.time())),
    )
    trip_id = cur.lastrowid

    # keep only last 50 to be safe
    conn.execute("""
        DELETE FROM trips
        WHERE user_id=?
        AND id NOT IN (
            SELECT id FROM trips WHERE user_id=? ORDER BY id DESC LIMIT 50
        )
    """, (user_id, user_id))

    conn.commit()
    conn.close()
    return int(trip_id)

def trip_get(user_id: int, trip_id: int) -> Optional[Dict[str, Any]]:
    conn = db()
    r = conn.execute(
        "SELECT data_json FROM trips WHERE user_id=? AND id=?",
        (user_id, trip_id)
    ).fetchone()
    conn.close()
    if not r:
        return None
    return json.loads(r["data_json"])

def trip_update(user_id: int, trip_id: int, trip_dict: Dict[str, Any]) -> bool:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE trips SET data_json=?, created_at=? WHERE user_id=? AND id=?",
        (json.dumps(trip_dict, ensure_ascii=False), int(time.time()), user_id, trip_id)
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok

def trip_list(user_id: int, limit: int, offset: int) -> List[Tuple[int, Dict[str, Any]]]:
    conn = db()
    rows = conn.execute(
        "SELECT id, data_json FROM trips WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append((int(r["id"]), json.loads(r["data_json"])))
    return out

def trip_count(user_id: int) -> int:
    conn = db()
    r = conn.execute("SELECT COUNT(*) AS c FROM trips WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return int(r["c"] if r else 0)

# ---------- Helpers ----------
def is_admin(user_id: int) -> bool:
    return (not ADMIN_ID) or (str(user_id) == ADMIN_ID)

def get_publish_chat_id() -> Optional[int]:
    if not PUBLISH_CHAT_ID:
        return None
    try:
        return int(PUBLISH_CHAT_ID)
    except ValueError:
        return None

def normalize_phone(s: str) -> str:
    s = (s or "").strip()
    s = s.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    return s

def phone_valid(s: str) -> bool:
    return len(normalize_phone(s)) >= 5

async def safe_delete_cb(c: CallbackQuery):
    try:
        await c.message.delete()
    except Exception:
        pass

def msg_link(chat_id: int, username: Optional[str], message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        internal = s[4:]
        return f"https://t.me/c/{internal}/{message_id}"
    return ""




async def send_to_topic(bot: Bot, from_city: str, text: str):
    """
    Отправляет в привязанную тему по from_city (topic_bindings).
    Если привязки нет — отправляет в PUBLISH_CHAT_ID (General).
    Возвращает (chat_id, username, message_id).
    """
    chat_id_default = get_publish_chat_id()
    if chat_id_default is None:
        return (None, None, None)

    raw = (from_city or "").strip()
    keys = []
    # 1) если есть city_key() (маппинг RU->UZ), пробуем его
    try:
        keys.append(city_key(raw))
    except Exception:
        pass
    # 2) пробуем как есть
    if raw:
        keys.append(raw)
        keys.append(raw.upper())
        keys.append(raw.title())

    bind = None
    for k in keys:
        if not k:
            continue
        try:
            bind = topic_get_ci(k)
        except Exception:
            # если вдруг нет topic_get_ci, пробуем topic_get
            try:
                bind = topic_get(k)
            except Exception:
                bind = None
        if bind:
            break

    chat_id = bind["chat_id"] if bind else chat_id_default
    thread_id = bind["thread_id"] if bind else None

    chat = await bot.get_chat(chat_id)
    username = getattr(chat, "username", None)

    if thread_id:
        msg = await bot.send_message(chat_id, text, message_thread_id=thread_id)
    else:
        msg = await bot.send_message(chat_id, text)

    return (chat_id, username, msg.message_id)


    key = city_key(from_city) if "city_key" in globals() else (from_city or "")
    bind = topic_get(key) if "topic_get" in globals() else None

    chat_id = bind["chat_id"] if bind else chat_id_default
    thread_id = bind["thread_id"] if bind else None

    chat = await bot.get_chat(chat_id)
    username = getattr(chat, "username", None)

    if thread_id:
        msg = await bot.send_message(chat_id, text, message_thread_id=thread_id)
    else:
        msg = await bot.send_message(chat_id, text)

    return (chat_id, username, msg.message_id)



async def publish_trip(bot: Bot, user_id: int, from_city: str, post_text: str):
    """
    Публикует по from_city в привязанную тему (topic_bindings).
    Если привязки нет — публикует в PUBLISH_CHAT_ID (General).
    Возвращает (place_text, link).
    """
    chat_id2, username2, mid2 = await send_to_topic(bot, from_city, post_text)
    if chat_id2 is None or mid2 is None:
        return ("(не задан PUBLISH_CHAT_ID)", "")

    chat = await bot.get_chat(chat_id2)
    place = f"@{chat.username}" if getattr(chat, "username", None) else (chat.title or str(chat_id2))
    link = msg_link(chat_id2, username2, mid2)
    return (place, link)

@dataclass
class Trip:
    name: str
    from_city: str
    to_city: str
    car: str
    seats: str
    phone: str
    comment: Optional[str] = None

def render_trip(tr: Trip) -> str:
    lines = [
        "Проверь введенные данные:",
        f"Имя водителя: <b>{tr.name}</b>",
        f"Город отправления: <b>{tr.from_city}</b>",
        f"Город прибытия: <b>{tr.to_city}</b>",
        f"Авто: <b>{tr.car}</b>",
        f"Номер телефона: <b>{tr.phone}</b>",
        f"Количество свободных мест: <b>{tr.seats}</b>",
    ]
    if tr.comment:
        lines.append(f"Комментарии: <b>{tr.comment}</b>")
    return "\n".join(lines)

def render_post(tr: Trip) -> str:
    lines = [
        "🚖 <b>Поездка</b>",
        f"👤 Водитель: {tr.name}",
        f"🛫 Откуда: {tr.from_city}",
        f"🛬 Куда: {tr.to_city}",
        f"🚘 Авто: {tr.car}",
        f"👥 Мест: {tr.seats}",
        f"📞 Телефон: {tr.phone}",
    ]
    if tr.comment:
        lines.append(f"💬 Комментарий: {tr.comment}")
        # hashtags
    a = city_key_uz(tr.from_city)
    b = city_key_uz(tr.to_city)
    code = route_code(tr.from_city, tr.to_city)
    lines.append(f"#{tagify(a)} #{tagify(b)} #{code}")
    return "\n".join(lines)

# ---------- States ----------
class RegStates(StatesGroup):
    waiting_phone = State()
    confirm_name = State()
    edit_name = State()

class CreateStates(StatesGroup):
    from_city = State()
    to_city = State()
    car = State()
    car_other = State()
    seats = State()
    comment = State()
    phone_choice = State()
    confirm = State()

class EditStates(StatesGroup):
    choose_field = State()
    edit_text = State()

# ---------- Keyboards ----------
def ik_main_menu():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Создать новую поездку", callback_data="menu:create")
    b.button(text="🕘 Последние поездки", callback_data="menu:lastlist:0")
    b.adjust(1)
    return b.as_markup()

def ik_start_phone():
    b = InlineKeyboardBuilder()
    b.button(text="📲 Отправить номер телефона", callback_data="reg:send_phone")
    b.adjust(1)
    return b.as_markup()

def rk_request_contact():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📲 Отправить номер (контакт)", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def ik_confirm_name():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Оставить так", callback_data="reg:name_ok")
    b.button(text="✏️ Изменить имя", callback_data="reg:name_edit")
    b.adjust(1)
    return b.as_markup()

def ik_cancel(back_cb: str = "menu:back"):
    b = InlineKeyboardBuilder()
    b.button(text="↩️ Назад", callback_data=back_cb)
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

# creation keyboards (NO conflicts)
def ik_cities_create(back_to: str):
    b = InlineKeyboardBuilder()
    for c in CITIES:
        b.button(text=c, callback_data=f"city:{c}")
    b.button(text="↩️ Назад", callback_data=f"backcreate:{back_to}")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(3)
    return b.as_markup()

def ik_cars_create():
    b = InlineKeyboardBuilder()
    for c in CARS:
        b.button(text=c, callback_data=f"car:{c}")
    b.button(text="✍️ Другое (ввести)", callback_data="car:__other__")
    b.button(text="↩️ Назад", callback_data="backcreate:car")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(2)
    return b.as_markup()

def ik_seats_create():
    b = InlineKeyboardBuilder()
    for n in ["1","2","3","4"]:
        b.button(text=n, callback_data=f"seats:{n}")
    b.button(text="↩️ Назад", callback_data="backcreate:seats")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(4)
    return b.as_markup()

def ik_comment_create():
    b = InlineKeyboardBuilder()
    b.button(text="⏭ Пропустить", callback_data="comment:skip")
    b.button(text="↩️ Назад", callback_data="backcreate:comment")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

def ik_phone_choice(saved_phone: str):
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Использовать мой номер: {saved_phone}", callback_data="phone:use_saved")
    b.button(text="↩️ Назад", callback_data="backcreate:phone")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

def ik_confirm_create():
    b = InlineKeyboardBuilder()
    b.button(text="📣 Опубликовать", callback_data="final:publish")
    b.button(text="💾 Сохранить", callback_data="final:save")
    b.button(text="↩️ Назад", callback_data="backcreate:confirm")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(2)
    return b.as_markup()

def ik_trip_view(trip_id: int):
    b = InlineKeyboardBuilder()
    b.button(text="📣 Опубликовать", callback_data=f"trip:publish:{trip_id}")
    b.button(text="✏️ Редактировать", callback_data=f"trip:edit:{trip_id}")
    b.button(text="↩️ К списку", callback_data="menu:lastlist:0")  # default to first page
    b.button(text="⬅️ В кабинет", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

def ik_trip_view_from_list(trip_id: int, offset: int):
    b = InlineKeyboardBuilder()
    b.button(text="📣 Опубликовать", callback_data=f"trip:publish:{trip_id}")
    b.button(text="✏️ Редактировать", callback_data=f"trip:edit:{trip_id}")
    b.button(text="↩️ К списку", callback_data=f"menu:lastlist:{offset}")
    b.button(text="⬅️ В кабинет", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

def ik_edit_fields(trip_id: int, offset: int):
    b = InlineKeyboardBuilder()
    b.button(text="🛫 Откуда", callback_data=f"edit:from:{trip_id}:{offset}")
    b.button(text="🛬 Куда", callback_data=f"edit:to:{trip_id}:{offset}")
    b.button(text="🚘 Авто", callback_data=f"edit:car:{trip_id}:{offset}")
    b.button(text="👥 Места", callback_data=f"edit:seats:{trip_id}:{offset}")
    b.button(text="📞 Телефон", callback_data=f"edit:phone:{trip_id}:{offset}")
    b.button(text="💬 Комментарий", callback_data=f"edit:comment:{trip_id}:{offset}")
    b.button(text="↩️ Назад", callback_data=f"trip:view:{trip_id}:{offset}")
    b.adjust(2)
    return b.as_markup()

# edit keyboards (separate callback prefixes => NO conflicts)
def ik_cities_edit(which: str, trip_id: int, offset: int):
    b = InlineKeyboardBuilder()
    for c in CITIES:
        b.button(text=c, callback_data=f"editcity:{which}:{trip_id}:{offset}:{c}")
    b.button(text="↩️ Назад", callback_data=f"trip:edit:{trip_id}:{offset}")
    b.adjust(3)
    return b.as_markup()

def ik_cars_edit(trip_id: int, offset: int):
    b = InlineKeyboardBuilder()
    for c in CARS:
        b.button(text=c, callback_data=f"editcar:{trip_id}:{offset}:{c}")
    b.button(text="✍️ Другое (ввести)", callback_data=f"editcar:{trip_id}:{offset}:__other__")
    b.button(text="↩️ Назад", callback_data=f"trip:edit:{trip_id}:{offset}")
    b.adjust(2)
    return b.as_markup()

def ik_seats_edit(trip_id: int, offset: int):
    b = InlineKeyboardBuilder()
    for n in ["1","2","3","4"]:
        b.button(text=n, callback_data=f"editseats:{trip_id}:{offset}:{n}")
    b.button(text="↩️ Назад", callback_data=f"trip:edit:{trip_id}:{offset}")
    b.adjust(4)
    return b.as_markup()

def ik_list_trips(user_id: int, offset: int):
    total = trip_count(user_id)
    items = trip_list(user_id, PAGE_SIZE, offset)
    b = InlineKeyboardBuilder()

    # buttons for items
    for idx, (tid, td) in enumerate(items, start=1):
        from_c = td.get("from_city","?")
        to_c = td.get("to_city","?")
        seats = td.get("seats","?")
        text = f"{offset + idx}) {from_c}→{to_c} | {seats} мест"
        b.button(text=text, callback_data=f"trip:view:{tid}:{offset}")

    # nav
    prev_off = max(0, offset - PAGE_SIZE)
    next_off = offset + PAGE_SIZE
    nav_row = []
    if offset > 0:
        nav_row.append(("⬅️ Назад", f"menu:lastlist:{prev_off}"))
    if next_off < total:
        nav_row.append(("➡️ Далее", f"menu:lastlist:{next_off}"))

    if nav_row:
        for (txt, cb) in nav_row:
            b.button(text=txt, callback_data=cb)

    b.button(text="⬅️ В кабинет", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

# ---------- Flow: cancel ----------
@dp.callback_query(F.data == "flow:cancel")
async def flow_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_delete_cb(c)
    await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

# ---------- /start registration ----------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    prof = profile_get(m.from_user.id)
    if prof:
        await m.answer(
            f"{prof['name']} ты находишься в кабинете водителя.\n"
            "Здесь ты можешь создавать новые поездки и управлять созданными ранее.",
            reply_markup=ik_main_menu()
        )
        return
    await state.set_state(RegStates.waiting_phone)
    await m.answer(WELCOME, reply_markup=ik_start_phone())

@dp.callback_query(F.data == "reg:send_phone")
async def reg_send_phone(c: CallbackQuery, state: FSMContext):
    await safe_delete_cb(c)
    await state.set_state(RegStates.waiting_phone)
    await c.message.answer("Нажмите кнопку ниже, чтобы отправить номер (контакт).", reply_markup=rk_request_contact())
    await c.answer()

@dp.message(RegStates.waiting_phone)
async def reg_got_phone(m: Message, state: FSMContext):
    if not m.contact or not m.contact.phone_number:
        await m.answer("Отправьте номер через кнопку “📲 Отправить номер (контакт)”.", reply_markup=rk_request_contact())
        return
    phone = m.contact.phone_number
    name = (m.contact.first_name or m.from_user.full_name or "Водитель").strip()
    profile_set(m.from_user.id, name, phone)
    await state.set_state(RegStates.confirm_name)
    await m.answer("✅ Номер сохранён.", reply_markup=ReplyKeyboardRemove())
    await m.answer(
        f"Я взял имя: <b>{name}</b>\nНомер: <b>{phone}</b>\n\nИмя оставить таким?",
        reply_markup=ik_confirm_name()
    )

@dp.callback_query(F.data == "reg:name_ok")
async def reg_name_ok(c: CallbackQuery, state: FSMContext):
    await safe_delete_cb(c)
    await state.clear()
    await c.message.answer("Готово ✅\nКабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.callback_query(F.data == "reg:name_edit")
async def reg_name_edit(c: CallbackQuery, state: FSMContext):
    await safe_delete_cb(c)
    await state.set_state(RegStates.edit_name)
    await c.message.answer("Напишите новое имя (текстом):", reply_markup=ik_cancel(back_cb="flow:cancel"))
    await c.answer()

@dp.message(RegStates.edit_name)
async def reg_name_set(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if len(name) < 2:
        await m.answer("Имя слишком короткое. Напишите ещё раз:")
        return
    prof = profile_get(m.from_user.id)
    if not prof:
        await state.clear()
        await m.answer("Нажмите /start")
        return
    profile_set(m.from_user.id, name, prof["phone"])
    await state.clear()
    await m.answer("✅ Имя обновлено.\nКабинет водителя:", reply_markup=ik_main_menu())

# ---------- Menu ----------
@dp.callback_query(F.data == "menu:create")
async def menu_create(c: CallbackQuery, state: FSMContext):
    prof = profile_get(c.from_user.id)
    if not prof:
        await flow_cancel(c, state); return
    await safe_delete_cb(c)
    await state.clear()
    await state.update_data(name=prof["name"])
    await state.set_state(CreateStates.from_city)
    await c.message.answer("Выберите город отправки из списка", reply_markup=ik_cities_create("menu"))
    await c.answer()

@dp.callback_query(F.data.startswith("menu:lastlist:"))
async def menu_lastlist(c: CallbackQuery):
    await safe_delete_cb(c)
    offset = int(c.data.split(":")[-1])
    total = trip_count(c.from_user.id)
    if total == 0:
        await c.message.answer("У вас ещё нет сохранённых поездок.", reply_markup=ik_main_menu())
        await c.answer()
        return
    await c.message.answer(f"Последние поездки (показано {PAGE_SIZE}):", reply_markup=ik_list_trips(c.from_user.id, offset))
    await c.answer()

# ---------- Trip view ----------
@dp.callback_query(F.data.startswith("trip:view:"))
async def trip_view(c: CallbackQuery):
    await safe_delete_cb(c)
    _, _, tid, offset = c.data.split(":", 3)
    trip_id = int(tid); off = int(offset)
    td = trip_get(c.from_user.id, trip_id)
    if not td:
        await c.message.answer("Эта поездка не найдена.", reply_markup=ik_main_menu())
        await c.answer()
        return
    tr = Trip(**td)
    await c.message.answer(render_trip(tr), reply_markup=ik_trip_view_from_list(trip_id, off))
    await c.answer()

# ---------- Create: back ----------
@dp.callback_query(F.data.startswith("backcreate:"))
async def backcreate(c: CallbackQuery, state: FSMContext):
    where = c.data.split(":", 1)[1]
    data = await state.get_data()
    await safe_delete_cb(c)

    if where == "menu":
        await state.clear()
        await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
        await c.answer(); return

    if where == "from":
        await state.set_state(CreateStates.from_city)
        cur = data.get("from_city")
        await c.message.answer(f"Выберите город отправки (сейчас: <b>{cur or '—'}</b>)", reply_markup=ik_cities_create("menu"))
        await c.answer(); return

    if where == "car":
        await state.set_state(CreateStates.to_city)
        cur = data.get("to_city")
        await c.message.answer(f"Выберите город прибытия (сейчас: <b>{cur or '—'}</b>)", reply_markup=ik_cities_create("from"))
        await c.answer(); return

    if where == "seats":
        await state.set_state(CreateStates.car)
        cur = data.get("car")
        await c.message.answer(f"Выберите авто (сейчас: <b>{cur or '—'}</b>)", reply_markup=ik_cars_create())
        await c.answer(); return

    if where == "comment":
        await state.set_state(CreateStates.seats)
        cur = data.get("seats")
        await c.message.answer(f"УКАЖИТЕ КОЛИЧЕСТВО ПАССАЖИРОВ (сейчас: <b>{cur or '—'}</b>)", reply_markup=ik_seats_create())
        await c.answer(); return

    if where == "phone":
        await state.set_state(CreateStates.comment)
        cur = data.get("comment")
        await c.message.answer(
            "Если хотите добавить комментарии напишите снизу или нажмите кнопку пропустить😊.\n"
            f"(сейчас: <b>{cur or '—'}</b>)",
            reply_markup=ik_comment_create()
        )
        await c.answer(); return

    if where == "confirm":
        await state.set_state(CreateStates.phone_choice)
        prof = profile_get(c.from_user.id) or {}
        saved = prof.get("phone","")
        await c.message.answer(
            "Укажите контакты для связи:\n— нажмите кнопку (мой номер)\n— или напишите другой номер сообщением",
            reply_markup=ik_phone_choice(saved)
        )
        await c.answer(); return

    await state.clear()
    await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

# ---------- Create: cities ----------
@dp.callback_query(F.data.startswith("city:"))
async def pick_city_create(c: CallbackQuery, state: FSMContext):
    city = c.data.split(":", 1)[1]
    st = await state.get_state()
    await safe_delete_cb(c)

    if st == CreateStates.from_city.state:
        await state.update_data(from_city=city)
        await state.set_state(CreateStates.to_city)
        await c.message.answer("Выберите город прибытия из списка", reply_markup=ik_cities_create("from"))
        await c.answer(); return

    if st == CreateStates.to_city.state:
        await state.update_data(to_city=city)
        await state.set_state(CreateStates.car)
        await c.message.answer("Выберите марку автомобиля из списка или пишете своё", reply_markup=ik_cars_create())
        await c.answer(); return

    await c.answer("Сейчас выбор города не ожидается.", show_alert=True)

# ---------- Create: car ----------
@dp.callback_query(F.data.startswith("car:"))
async def pick_car_create(c: CallbackQuery, state: FSMContext):
    val = c.data.split(":", 1)[1]
    st = await state.get_state()
    if st != CreateStates.car.state:
        await c.answer("Сейчас выбор авто не ожидается.", show_alert=True)
        return
    await safe_delete_cb(c)

    if val == "__other__":
        await state.set_state(CreateStates.car_other)
        await c.message.answer("Напишите марку автомобиля (текстом):", reply_markup=ik_cancel(back_cb="backcreate:seats"))
        await c.answer(); return

    await state.update_data(car=val)
    await state.set_state(CreateStates.seats)
    await c.message.answer("УКАЖИТЕ КОЛИЧЕСТВО ПАССАЖИРОВ", reply_markup=ik_seats_create())
    await c.answer()

@dp.message(CreateStates.car_other)
async def car_other_text(m: Message, state: FSMContext):
    val = (m.text or "").strip()
    if len(val) < 2:
        await m.answer("Слишком коротко. Напишите марку ещё раз:")
        return
    await state.update_data(car=val)
    await state.set_state(CreateStates.seats)
    await m.answer("УКАЖИТЕ КОЛИЧЕСТВО ПАССАЖИРОВ", reply_markup=ik_seats_create())

# ---------- Create: seats ----------
@dp.callback_query(F.data.startswith("seats:"))
async def pick_seats_create(c: CallbackQuery, state: FSMContext):
    seats = c.data.split(":", 1)[1]
    st = await state.get_state()
    if st != CreateStates.seats.state:
        await c.answer("Сейчас выбор мест не ожидается.", show_alert=True)
        return
    await safe_delete_cb(c)
    await state.update_data(seats=seats)
    await state.set_state(CreateStates.comment)
    await c.message.answer("Если хотите добавить комментарии напишите снизу или нажмите кнопку пропустить😊.", reply_markup=ik_comment_create())
    await c.answer()

# ---------- Create: comment ----------
@dp.callback_query(F.data == "comment:skip")
async def comment_skip(c: CallbackQuery, state: FSMContext):
    st = await state.get_state()
    if st != CreateStates.comment.state:
        await c.answer(); return
    await safe_delete_cb(c)
    await state.update_data(comment=None)

    prof = profile_get(c.from_user.id) or {}
    saved = prof.get("phone","")
    await state.set_state(CreateStates.phone_choice)
    await c.message.answer(
        "Укажите контакты для связи:\n— нажмите кнопку (мой номер)\n— или напишите другой номер сообщением",
        reply_markup=ik_phone_choice(saved)
    )
    await c.answer()

@dp.message(CreateStates.comment)
async def comment_text(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    await state.update_data(comment=txt if txt else None)
    prof = profile_get(m.from_user.id) or {}
    saved = prof.get("phone","")
    await state.set_state(CreateStates.phone_choice)
    await m.answer(
        "Укажите контакты для связи:\n— нажмите кнопку (мой номер)\n— или напишите другой номер сообщением",
        reply_markup=ik_phone_choice(saved)
    )

# ---------- Create: phone choice ----------
@dp.callback_query(F.data == "phone:use_saved")
async def phone_use_saved(c: CallbackQuery, state: FSMContext):
    st = await state.get_state()
    if st != CreateStates.phone_choice.state:
        await c.answer(); return
    prof = profile_get(c.from_user.id) or {}
    saved = prof.get("phone","")
    if not saved:
        await c.answer("Номер не найден. Напишите номер сообщением.", show_alert=True)
        return
    await safe_delete_cb(c)
    await state.update_data(phone=saved)
    data = await state.get_data()
    tr = Trip(**data)
    await state.set_state(CreateStates.confirm)
    await c.message.answer(render_trip(tr), reply_markup=ik_confirm_create())
    await c.answer()

@dp.message(CreateStates.phone_choice)
async def phone_text(m: Message, state: FSMContext):
    phone = normalize_phone(m.text or "")
    if not phone_valid(phone):
        prof = profile_get(m.from_user.id) or {}
        saved = prof.get("phone","")
        await m.answer("Номер слишком короткий. Напишите ещё раз или нажмите “Использовать мой номер”.", reply_markup=ik_phone_choice(saved))
        return
    await state.update_data(phone=phone)
    data = await state.get_data()
    tr = Trip(**data)
    await state.set_state(CreateStates.confirm)
    await m.answer(render_trip(tr), reply_markup=ik_confirm_create())

# ---------- Create: save/publish ----------
@dp.callback_query(F.data == "final:save")
async def final_save(c: CallbackQuery, state: FSMContext):
    await safe_delete_cb(c)
    data = await state.get_data()
    tr = Trip(**data)
    trip_insert(c.from_user.id, asdict(tr))
    await state.clear()
    await c.message.answer("💾 Сохранено. Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.callback_query(F.data == "final:publish")
async def final_publish(c: CallbackQuery, state: FSMContext, bot: Bot):
    await safe_delete_cb(c)
    data = await state.get_data()
    try:
        tr = Trip(**data)
    except Exception:
        await c.message.answer("❌ Ошибка данных поездки. Создайте заново.", reply_markup=ik_main_menu())
        await state.clear()
        await c.answer()
        return

    # автосохранение
    try:
        trip_insert(c.from_user.id, asdict(tr))
    except Exception:
        pass

    place, link = await publish_trip(bot, c.from_user.id, tr.from_city, render_post(tr))
    msg = f"✅ Опубликовано в: <b>{place}</b>"
    if link:
        msg += f"\n🔗 Ссылка: {link}"

    await state.clear()
    await c.message.answer(msg + "\n\nКабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def edit_choose(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    # edit:<field>:<trip_id>:<offset>
    if len(parts) < 4:
        await c.answer()
        return
    field = parts[1]
    trip_id = int(parts[2])
    offset = int(parts[3])

    await safe_delete_cb(c)

    td = trip_get(c.from_user.id, trip_id)
    if not td:
        await state.clear()
        await c.message.answer("Поездка не найдена.", reply_markup=ik_main_menu())
        await c.answer(); return

    if field == "from":
        await c.message.answer(
            f"Выберите город отправки (сейчас: <b>{td.get('from_city')}</b>)",
            reply_markup=ik_cities_edit("from", trip_id, offset)
        )
        await c.answer(); return

    if field == "to":
        await c.message.answer(
            f"Выберите город прибытия (сейчас: <b>{td.get('to_city')}</b>)",
            reply_markup=ik_cities_edit("to", trip_id, offset)
        )
        await c.answer(); return

    if field == "car":
        await c.message.answer(
            f"Выберите авто (сейчас: <b>{td.get('car')}</b>)",
            reply_markup=ik_cars_edit(trip_id, offset)
        )
        await c.answer(); return

    if field == "seats":
        await c.message.answer(
            f"Выберите места (сейчас: <b>{td.get('seats')}</b>)",
            reply_markup=ik_seats_edit(trip_id, offset)
        )
        await c.answer(); return

    if field in {"phone", "comment"}:
        await state.set_state(EditStates.edit_text)
        await state.update_data(edit_trip_id=trip_id, edit_offset=offset, edit_field=field)
        cur = td.get(field) or "—"
        hint = "Введите новый телефон:" if field == "phone" else "Введите новый комментарий (или '-' чтобы удалить):"
        await c.message.answer(f"{hint}\n(сейчас: <b>{cur}</b>)", reply_markup=ik_cancel(back_cb=f"trip:edit:{trip_id}:{offset}"))
        await c.answer(); return

    await c.answer("Неизвестный пункт", show_alert=True)

@dp.callback_query(F.data.startswith("editcity:"))
async def edit_pick_city(c: CallbackQuery):
    # editcity:<which>:<trip_id>:<offset>:<city>
    parts = c.data.split(":")
    if len(parts) < 5:
        await c.answer(); return
    which = parts[1]
    trip_id = int(parts[2])
    offset = int(parts[3])
    city = parts[4]

    await safe_delete_cb(c)

    td = trip_get(c.from_user.id, trip_id)
    if not td:
        await c.message.answer("Поездка не найдена.", reply_markup=ik_main_menu())
        await c.answer(); return

    if which == "from":
        td["from_city"] = city
    else:
        td["to_city"] = city

    trip_update(c.from_user.id, trip_id, td)
    tr = Trip(**td)
    await c.message.answer("✅ Обновлено.\n" + render_trip(tr), reply_markup=ik_trip_view_from_list(trip_id, offset))
    await c.answer()

@dp.callback_query(F.data.startswith("editseats:"))
async def edit_pick_seats(c: CallbackQuery):
    # editseats:<trip_id>:<offset>:<seats>
    parts = c.data.split(":")
    if len(parts) < 4:
        await c.answer(); return
    trip_id = int(parts[1])
    offset = int(parts[2])
    seats = parts[3]

    await safe_delete_cb(c)

    td = trip_get(c.from_user.id, trip_id)
    if not td:
        await c.message.answer("Поездка не найдена.", reply_markup=ik_main_menu())
        await c.answer(); return

    td["seats"] = seats
    trip_update(c.from_user.id, trip_id, td)
    tr = Trip(**td)
    await c.message.answer("✅ Обновлено.\n" + render_trip(tr), reply_markup=ik_trip_view_from_list(trip_id, offset))
    await c.answer()

@dp.callback_query(F.data.startswith("editcar:"))
async def edit_pick_car(c: CallbackQuery, state: FSMContext):
    # editcar:<trip_id>:<offset>:<car>
    parts = c.data.split(":")
    if len(parts) < 4:
        await c.answer(); return
    trip_id = int(parts[1])
    offset = int(parts[2])
    car = parts[3]

    await safe_delete_cb(c)

    td = trip_get(c.from_user.id, trip_id)
    if not td:
        await c.message.answer("Поездка не найдена.", reply_markup=ik_main_menu())
        await c.answer(); return

    if car == "__other__":
        await state.set_state(EditStates.edit_text)
        await state.update_data(edit_trip_id=trip_id, edit_offset=offset, edit_field="car")
        await c.message.answer("Введите марку авто (текстом):", reply_markup=ik_cancel(back_cb=f"trip:edit:{trip_id}:{offset}"))
        await c.answer()
        return

    td["car"] = car
    trip_update(c.from_user.id, trip_id, td)
    tr = Trip(**td)
    await c.message.answer("✅ Обновлено.\n" + render_trip(tr), reply_markup=ik_trip_view_from_list(trip_id, offset))
    await c.answer()

@dp.message(EditStates.edit_text)
async def edit_text_apply(m: Message, state: FSMContext):
    data = await state.get_data()
    trip_id = int(data.get("edit_trip_id", 0))
    offset = int(data.get("edit_offset", 0))
    field = data.get("edit_field")
    val = (m.text or "").strip()

    td = trip_get(m.from_user.id, trip_id)
    if not td:
        await state.clear()
        await m.answer("Поездка не найдена.", reply_markup=ik_main_menu())
        return

    if field == "phone":
        if not phone_valid(val):
            await m.answer("Телефон слишком короткий. Введите ещё раз:")
            return
        td["phone"] = val
    elif field == "comment":
        td["comment"] = None if val == "-" else val
    elif field == "car":
        if len(val) < 2:
            await m.answer("Слишком коротко. Введите ещё раз:")
            return
        td["car"] = val
    else:
        await state.clear()
        await m.answer("Ошибка редактирования. Нажмите /start")
        return

    trip_update(m.from_user.id, trip_id, td)
    await state.clear()
    tr = Trip(**td)
    await m.answer("✅ Сохранено.\n" + render_trip(tr), reply_markup=ik_trip_view_from_list(trip_id, offset))

# ---------- Commands ----------
@dp.message(Command("chatid"))
async def chatid(m: Message):
    thread_id = getattr(m, "message_thread_id", None)
    await m.answer(f"chat_id: {m.chat.id}\nthread_id: {thread_id}\nuser_id: {m.from_user.id}")

@dp.message(Command("wherepublish"))
async def wherepublish(m: Message):
    await m.answer(f"PUBLISH_CHAT_ID: {get_publish_chat_id() or 'не задан'}")

# ---------- Fallback ----------

@dp.message(Command("bindcity"))
async def bindcity(m: Message):
    # писать прямо внутри темы. примеры: /bindcity Самарканд   или /bindcity Ташкент
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Использование: /bindcity ГОРОД. Пиши команду внутри нужной темы (подгруппы).")
        return

    # topics: message_thread_id есть только в теме
    thread_id = getattr(m, "message_thread_id", None)
    if not thread_id:
        await m.answer("Эту команду нужно писать ВНУТРИ темы (подгруппы), чтобы я увидел thread_id.")
        return

    city_ru = parts[1].strip()
    key = city_key_uz(city_ru)

    topic_title = m.chat.title or ""
    topic_set(key, m.chat.id, int(thread_id), topic_title)

    await m.answer(f"✅ Привязал тему к городу: {city_ru} → key={key}\nchat_id={m.chat.id}\nthread_id={thread_id}")

@dp.message(Command("bindlist"))
async def bindlist(m: Message):
    rows = topic_list()
    if not rows:
        await m.answer("Привязок нет. Используй /bindcity в каждой теме.")
        return
    lines = ["Привязки тем:"]
    for r in rows:
        lines.append(f"- {r['city_key']}: chat_id={r['chat_id']} thread_id={r['thread_id']}")
    await m.answer("\n".join(lines))

@dp.message(Command("bindhere"))
async def bindhere(m: Message):
    # Привязать текущую тему по названию (например SAMARQAND/TOSHKENT)
    thread_id = getattr(m, "message_thread_id", None)
    if not thread_id:
        await m.answer("Команду нужно писать внутри ТЕМЫ (Topics), чтобы был thread_id.")
        return

    # В темах Telegram обычно название темы доступно как is_topic_message + message_thread_id,
    # а key берём из названия темы (в Desktop отображается как заголовок SAMARQAND).
    # В API точного topic title может не быть в Message, поэтому используем безопасный ключ:
    # если пользователь в теме SAMARQAND — он может просто написать /bindhere, и мы используем
    # 'Samarqand' как key по последнему известному: просим уточнить, если не можем.
    #
    # Практичный способ: используем chat title + thread_id как уникальный ключ темы,
    # но нам нужен ключ города => попросим 1 раз, если не нашли.
    #
    # Решение: если пользователь написал /bindhere, берём "Samarqand" из текущего чата темы:
    # В Telegram Desktop в теме сообщение приходит с forward_from_chat? нет.
    # Поэтому делаем простой вариант: пользователь должен закрепить ключ в теме через описание темы:
    # Но без этого не вытащить. Поэтому используем компромисс:
    # Если тема называется как город, пользователь пишет /bindhere <CityKey>. (но ты просил без)
    #
    # Реальный рабочий вариант без параметров:
    # сохраняем связку thread_id -> city_key по последнему /chatid + ручному вводу? нет.
    #
    # => Лучший вариант: /bindhere берёт город из САМОГО сообщения, если есть: "/bindhere SAMARQAND".
    # Но ты хочешь совсем без. Тогда берём город из закреплённого сообщения темы: первое слово.
    #
    await m.answer("Напиши так: /bindhere SAMARQAND (или TOSHKENT). Это 1 слово, без русского.\n"
                   "Потом дальше будет работать автоматически.")




def ik_bind_cities():
    b = InlineKeyboardBuilder()
    # Города как в боте (кириллица)
    cities_ru = [
        "ТАШКЕНТ","СИРДАРЬЯ","ДЖИЗЗАК","САМАРКАНД","ФЕРГАНА","НАМАНГАН","АНДИЖАН",
        "КАШКАДАРЬЯ","СУРХАНДАРЬЯ","БУХАРА","НАВАИ","ХОРЕЗМ","КАРАКАЛПАКИЯ"
    ]
    for c in cities_ru:
        b.button(text=c, callback_data=f"bindcitybtn:{c}")
    b.button(text="❌ Отмена", callback_data="bindcitybtn:cancel")
    b.adjust(2)
    return b.as_markup()

@dp.message(Command("bind"))
async def bind_menu(m: Message):
    # Пиши в теме (подгруппе). Бот покажет кнопки городов.
    thread_id = getattr(m, "message_thread_id", None)
    if not thread_id:
        await m.answer("Команду /bind нужно писать ВНУТРИ темы (Topics), чтобы был thread_id.")
        return
    await m.answer("Выберите город для этой темы:", reply_markup=ik_bind_cities())

@dp.callback_query(F.data.startswith("bindcitybtn:"))
async def bind_city_btn(c: CallbackQuery):
    await c.answer()

    if c.data == "bindcitybtn:cancel":
        try:
            await c.message.delete()
        except Exception:
            pass
        return

    thread_id = getattr(c.message, "message_thread_id", None)
    if not thread_id:
        await c.answer("Эта кнопка должна нажиматься внутри темы (Topics).", show_alert=True)
        return

    city_ru = c.data.split(":", 1)[1].strip()
    key = city_key(city_ru) if "city_key" in globals() else city_ru.title()

    # topic_set должен уже быть в коде (мы его добавляли)
    topic_set(key, c.message.chat.id, int(thread_id))

    # Уберём клавиатуру, чтобы не нажимали лишний раз
    try:
        await c.message.edit_text(f"✅ Привязано: {city_ru} → {key}\nchat_id={c.message.chat.id}\nthread_id={thread_id}")
    except Exception:
        pass

@dp.message()
async def fallback(m: Message):
    await m.answer("Нажмите /start")

async def main():
    db_init()
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("BOT_TOKEN пустой. Заполни .env")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
