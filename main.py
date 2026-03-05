import os
import logging
import sqlite3
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

load_dotenv(dotenv_path=".env")

PUBLISH_CHAT_ID = os.getenv("PUBLISH_CHAT_ID", "").strip()  # chat_id группы/канала для публикации

def get_publish_chat_id() -> int | None:
    if not PUBLISH_CHAT_ID:
        return None
    try:
        return int(PUBLISH_CHAT_ID)
    except ValueError:
        return None


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db")

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

# --------- SQLite ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        user_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recent_trips (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        data_json TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def profile_get(uid: int) -> Optional[Dict[str, str]]:
    conn = db()
    row = conn.execute("SELECT name, phone FROM profiles WHERE user_id = ?", (uid,)).fetchone()
    conn.close()
    if not row:
        return None
    return {"name": row["name"], "phone": row["phone"]}

def profile_set(uid: int, name: str, phone: str):
    conn = db()
    conn.execute(
        "INSERT INTO profiles(user_id, name, phone) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, phone=excluded.phone",
        (uid, name, phone)
    )
    conn.commit()
    conn.close()

# (последние поездки оставим пока в памяти, можно тоже в SQLite — но для твоей проблемы достаточно профиля)
RECENT_TRIPS: Dict[int, List[dict]] = {}

# -------- FSM ----------
class RegStates(StatesGroup):
    waiting_phone = State()
    confirm_name = State()
    edit_name = State()

class TripStates(StatesGroup):
    from_city = State()
    to_city = State()
    car = State()
    car_other = State()
    seats = State()
    comment = State()
    phone_choice = State()   # как было: кнопка "мой номер" или написать другой
    confirm = State()

@dataclass
class Trip:
    name: str
    from_city: str
    to_city: str
    car: str
    seats: str
    phone: str
    comment: Optional[str] = None

# -------- helpers ----------
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

def save_recent(uid: int, trip_dict: dict):
    lst = RECENT_TRIPS.get(uid, [])
    lst.insert(0, trip_dict)
    RECENT_TRIPS[uid] = lst[:10]

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
    return "\n".join(lines)

# -------- keyboards INLINE --------
def ik_main_menu():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Создать новую поездку", callback_data="menu:create")
    b.button(text="🕘 Последняя поездка", callback_data="menu:last")
    b.adjust(1)
    return b.as_markup()

def ik_start_phone():
    b = InlineKeyboardBuilder()
    b.button(text="📲 Отправить номер телефона", callback_data="reg:send_phone")
    b.adjust(1)
    return b.as_markup()

def ik_confirm_name():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Оставить так", callback_data="reg:name_ok")
    b.button(text="✏️ Изменить имя", callback_data="reg:name_edit")
    b.adjust(1)
    return b.as_markup()

def ik_cancel(back: bool = False):
    b = InlineKeyboardBuilder()
    if back:
        b.button(text="↩️ Назад", callback_data="flow:back")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

def ik_cities(back_to: str):
    b = InlineKeyboardBuilder()
    for c in CITIES:
        b.button(text=c, callback_data=f"city:{c}")
    b.button(text="↩️ Назад", callback_data=f"back:{back_to}")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(3)
    return b.as_markup()

def ik_cars():
    b = InlineKeyboardBuilder()
    for c in CARS:
        b.button(text=c, callback_data=f"car:{c}")
    b.button(text="✍️ Другое (ввести)", callback_data="car:__other__")
    b.button(text="↩️ Назад", callback_data="flow:back")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(2)
    return b.as_markup()

def ik_seats():
    b = InlineKeyboardBuilder()
    for n in ["1","2","3","4"]:
        b.button(text=n, callback_data=f"seats:{n}")
    b.button(text="↩️ Назад", callback_data="flow:back")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(4)
    return b.as_markup()

def ik_comment():
    b = InlineKeyboardBuilder()
    b.button(text="⏭ Пропустить", callback_data="comment:skip")
    b.button(text="↩️ Назад", callback_data="flow:back")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

def ik_phone_choice(saved_phone: str):
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Использовать мой номер: {saved_phone}", callback_data="phone:use_saved")
    b.button(text="↩️ Назад", callback_data="flow:back")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(1)
    return b.as_markup()

def ik_confirm():
    b = InlineKeyboardBuilder()
    b.button(text="📣 Опубликовать", callback_data="final:publish")
    b.button(text="💾 Сохранить", callback_data="final:save")
    b.button(text="↩️ Назад", callback_data="flow:back")
    b.button(text="❌ Отмена", callback_data="flow:cancel")
    b.adjust(2)
    return b.as_markup()

def ik_last_actions():
    b = InlineKeyboardBuilder()
    b.button(text="📣 Опубликовать", callback_data="last:publish")
    b.button(text="⬅️ В кабинет", callback_data="last:menu")
    b.adjust(1)
    return b.as_markup()

# request_contact — только ReplyKeyboard
def rk_request_contact():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📲 Отправить номер (контакт)", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# -------- cancel/back --------
@dp.callback_query(F.data == "flow:cancel")
async def flow_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_delete_cb(c)
    await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.callback_query(F.data == "flow:back")
async def flow_back(c: CallbackQuery, state: FSMContext):
    st = await state.get_state()
    data = await state.get_data()
    await safe_delete_cb(c)

    if st == TripStates.to_city.state:
        chosen = data.get("from_city")
        await state.set_state(TripStates.from_city)
        await c.message.answer(f"Выберите город отправки (сейчас: <b>{chosen}</b>)", reply_markup=ik_cities("menu"))
        await c.answer(); return

    if st == TripStates.car.state:
        chosen = data.get("to_city")
        await state.set_state(TripStates.to_city)
        await c.message.answer(f"Выберите город прибытия (сейчас: <b>{chosen}</b>)", reply_markup=ik_cities("from"))
        await c.answer(); return

    if st == TripStates.seats.state:
        chosen = data.get("car")
        await state.set_state(TripStates.car)
        await c.message.answer(f"Выберите авто (сейчас: <b>{chosen}</b>)", reply_markup=ik_cars())
        await c.answer(); return

    if st == TripStates.comment.state:
        chosen = data.get("seats")
        await state.set_state(TripStates.seats)
        await c.message.answer(f"Выберите места (сейчас: <b>{chosen}</b>)", reply_markup=ik_seats())
        await c.answer(); return

    if st == TripStates.phone_choice.state:
        chosen = data.get("comment")
        await state.set_state(TripStates.comment)
        await c.message.answer(f"Комментарий (сейчас: <b>{chosen if chosen else '—'}</b>)", reply_markup=ik_comment())
        await c.answer(); return

    if st == TripStates.confirm.state:
        prof = profile_get(c.from_user.id) or {}
        saved = prof.get("phone", "")
        await state.set_state(TripStates.phone_choice)
        await c.message.answer("Укажите контакты для связи:\n— нажмите кнопку (мой номер)\n— или напишите другой номер сообщением",
                               reply_markup=ik_phone_choice(saved))
        await c.answer(); return

    await state.clear()
    await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.callback_query(F.data.startswith("back:"))
async def back_special(c: CallbackQuery, state: FSMContext):
    where = c.data.split(":", 1)[1]
    await safe_delete_cb(c)
    if where == "menu":
        await state.clear()
        await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
        await c.answer(); return
    if where == "from":
        data = await state.get_data()
        chosen = data.get("from_city")
        await state.set_state(TripStates.from_city)
        await c.message.answer(f"Выберите город отправки (сейчас: <b>{chosen}</b>)", reply_markup=ik_cities("menu"))
        await c.answer(); return
    await state.clear()
    await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

# -------- /start registration (ТОЛЬКО 1 РАЗ, теперь из SQLite) --------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id

    prof = profile_get(uid)
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
        await m.answer("Пожалуйста, отправьте номер через кнопку “📲 Отправить номер (контакт)”.", reply_markup=rk_request_contact())
        return

    phone = m.contact.phone_number
    name = (m.contact.first_name or m.from_user.full_name or "Водитель").strip()

    # сохраняем в SQLite (и больше не спросит)
    profile_set(m.from_user.id, name, phone)

    await state.set_state(RegStates.confirm_name)
    await m.answer("✅ Номер сохранён.", reply_markup=ReplyKeyboardRemove())
    await m.answer(
        f"Я взял имя: <b>{name}</b>\n"
        f"Номер для связи: <b>{phone}</b>\n\n"
        "Имя оставить таким?",
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
    await c.message.answer("Напишите новое имя водителя (текстом):", reply_markup=ik_cancel())
    await c.answer()

@dp.message(RegStates.edit_name)
async def reg_name_set(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if len(name) < 2:
        await m.answer("Имя слишком короткое. Напишите ещё раз:", reply_markup=ik_cancel())
        return
    prof = profile_get(m.from_user.id)
    if not prof:
        await state.clear()
        await m.answer("Нажмите /start", reply_markup=ik_main_menu())
        return
    profile_set(m.from_user.id, name, prof["phone"])
    await state.clear()
    await m.answer("✅ Имя обновлено.\nКабинет водителя:", reply_markup=ik_main_menu())

# -------- menu --------
@dp.callback_query(F.data == "menu:create")
async def menu_create(c: CallbackQuery, state: FSMContext):
    prof = profile_get(c.from_user.id)
    if not prof:
        await state.clear()
        await safe_delete_cb(c)
        await c.message.answer("Нажмите /start чтобы зарегистрироваться.")
        await c.answer()
        return

    await safe_delete_cb(c)
    await state.update_data(name=prof["name"])
    await state.set_state(TripStates.from_city)
    await c.message.answer("Выберите город отправки из списка", reply_markup=ik_cities("menu"))
    await c.answer()

@dp.callback_query(F.data == "menu:last")
async def menu_last(c: CallbackQuery):
    await safe_delete_cb(c)
    lst = RECENT_TRIPS.get(c.from_user.id, [])
    if not lst:
        await c.message.answer("У вас ещё нет сохранённых/опубликованных поездок.", reply_markup=ik_main_menu())
        await c.answer()
        return
    tr = Trip(**lst[0])
    await c.message.answer(render_trip(tr), reply_markup=ik_last_actions())
    await c.answer()

# -------- trip flow --------
@dp.callback_query(F.data.startswith("city:"))
async def pick_city(c: CallbackQuery, state: FSMContext):
    city = c.data.split(":", 1)[1]
    st = await state.get_state()
    await safe_delete_cb(c)

    if st == TripStates.from_city.state:
        await state.update_data(from_city=city)
        await state.set_state(TripStates.to_city)
        await c.message.answer("Выберите город прибытия из списка", reply_markup=ik_cities("from"))
        await c.answer(); return

    if st == TripStates.to_city.state:
        await state.update_data(to_city=city)
        await state.set_state(TripStates.car)
        await c.message.answer("Выберите марку автомобиля из списка или пишете своё", reply_markup=ik_cars())
        await c.answer(); return

    await c.answer("Сейчас выбор города не ожидается.", show_alert=True)

@dp.callback_query(F.data.startswith("car:"))
async def pick_car(c: CallbackQuery, state: FSMContext):
    val = c.data.split(":", 1)[1]
    st = await state.get_state()
    if st != TripStates.car.state:
        await c.answer("Сейчас выбор авто не ожидается.", show_alert=True)
        return

    await safe_delete_cb(c)
    if val == "__other__":
        await state.set_state(TripStates.car_other)
        await c.message.answer("Напишите марку автомобиля (текстом):", reply_markup=ik_cancel(back=True))
        await c.answer(); return

    await state.update_data(car=val)
    await state.set_state(TripStates.seats)
    await c.message.answer("УКАЖИТЕ КОЛИЧЕСТВО ПАССАЖИРОВ", reply_markup=ik_seats())
    await c.answer()

@dp.message(TripStates.car_other)
async def car_other_text(m: Message, state: FSMContext):
    val = (m.text or "").strip()
    if len(val) < 2:
        await m.answer("Напишите марку автомобиля нормально (минимум 2 символа).", reply_markup=ik_cancel(back=True))
        return
    await state.update_data(car=val)
    await state.set_state(TripStates.seats)
    await m.answer("УКАЖИТЕ КОЛИЧЕСТВО ПАССАЖИРОВ", reply_markup=ik_seats())

@dp.callback_query(F.data.startswith("seats:"))
async def pick_seats(c: CallbackQuery, state: FSMContext):
    seats = c.data.split(":", 1)[1]
    st = await state.get_state()
    if st != TripStates.seats.state:
        await c.answer("Сейчас выбор мест не ожидается.", show_alert=True)
        return

    await safe_delete_cb(c)
    await state.update_data(seats=seats)
    await state.set_state(TripStates.comment)
    await c.message.answer("Если хотите добавить комментарии напишите снизу или нажмите кнопку пропустить😊.", reply_markup=ik_comment())
    await c.answer()

@dp.callback_query(F.data == "comment:skip")
async def comment_skip(c: CallbackQuery, state: FSMContext):
    st = await state.get_state()
    if st != TripStates.comment.state:
        await c.answer(); return
    await safe_delete_cb(c)
    await state.update_data(comment=None)

    prof = profile_get(c.from_user.id) or {}
    saved = prof.get("phone", "")
    await state.set_state(TripStates.phone_choice)
    await c.message.answer("Укажите контакты для связи:\n— нажмите кнопку (мой номер)\n— или напишите другой номер сообщением",
                           reply_markup=ik_phone_choice(saved))
    await c.answer()

@dp.message(TripStates.comment)
async def comment_text(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    await state.update_data(comment=txt if txt else None)
    prof = profile_get(m.from_user.id) or {}
    saved = prof.get("phone", "")
    await state.set_state(TripStates.phone_choice)
    await m.answer("Укажите контакты для связи:\n— нажмите кнопку (мой номер)\n— или напишите другой номер сообщением",
                   reply_markup=ik_phone_choice(saved))

@dp.callback_query(F.data == "phone:use_saved")
async def phone_use_saved(c: CallbackQuery, state: FSMContext):
    st = await state.get_state()
    if st != TripStates.phone_choice.state:
        await c.answer(); return
    prof = profile_get(c.from_user.id) or {}
    saved = prof.get("phone", "")
    if not saved:
        await c.answer("Номер не найден. Напишите номер сообщением.", show_alert=True)
        return
    await safe_delete_cb(c)
    await state.update_data(phone=saved)
    data = await state.get_data()
    tr = Trip(**data)
    await state.set_state(TripStates.confirm)
    await c.message.answer(render_trip(tr), reply_markup=ik_confirm())
    await c.answer()

@dp.message(TripStates.phone_choice)
async def phone_text_in_choice(m: Message, state: FSMContext):
    phone = normalize_phone(m.text or "")
    if not phone_valid(phone):
        prof = profile_get(m.from_user.id) or {}
        saved = prof.get("phone", "")
        await m.answer("Номер слишком короткий. Напишите ещё раз или нажмите “Использовать мой номер”.", reply_markup=ik_phone_choice(saved))
        return
    await state.update_data(phone=phone)
    data = await state.get_data()
    tr = Trip(**data)
    await state.set_state(TripStates.confirm)
    await m.answer(render_trip(tr), reply_markup=ik_confirm())

@dp.callback_query(F.data == "final:save")
async def final_save(c: CallbackQuery, state: FSMContext):
    await safe_delete_cb(c)
    data = await state.get_data()
    tr = Trip(**data)
    save_recent(c.from_user.id, asdict(tr))
    await state.clear()
    await c.message.answer("💾 Сохранено. Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.callback_query(F.data == "final:publish")
async def final_publish(c: CallbackQuery, state: FSMContext, bot: Bot):
    chat_id = get_publish_chat_id()
    if not chat_id:
        await c.answer("Не задан PUBLISH_CHAT_ID. Укажи его в .env", show_alert=True)
        return

    await safe_delete_cb(c)
    data = await state.get_data()
    tr = Trip(**data)

    # Автосохранение
    save_recent(c.from_user.id, asdict(tr))

    # Публикация в группу/канал
    await bot.send_message(chat_id, render_post(tr))

    await state.clear()
    await c.message.answer("✅ Опубликовано. Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.callback_query(F.data == "last:menu")
async def last_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_delete_cb(c)
    await c.message.answer("Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.callback_query(F.data == "last:publish")
async def last_publish(c: CallbackQuery, state: FSMContext):
    await safe_delete_cb(c)
    await c.message.answer("✅ Опубликовано из “Последняя поездка”. Кабинет водителя:", reply_markup=ik_main_menu())
    await c.answer()

@dp.message(Command("chatid"))
async def chatid(m: Message):
    await m.answer(f"chat_id: {m.chat.id}\nuser_id: {m.from_user.id}")

@dp.message()
async def fallback(m: Message, state: FSMContext):
    st = await state.get_state()
    if st == RegStates.waiting_phone.state:
        await m.answer("Нажмите кнопку “📲 Отправить номер (контакт)”.", reply_markup=rk_request_contact())
        return
    if st in {RegStates.edit_name.state, TripStates.car_other.state, TripStates.comment.state, TripStates.phone_choice.state}:
        return
    await m.answer("Нажмите /start чтобы начать заново.")

async def main():
    db_init()
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN пустой. Заполни .env (BOT_TOKEN=...)")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
