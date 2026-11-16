"""Telegram-бот: каталог, корзина и заказы (aiogram 3 + SQLite).
 
Клиент:   каталог по категориям -> карточка товара -> корзина -> оформление
          (самовывоз/доставка, адрес, телефон) -> подтверждение -> «Мои заказы».
Владелец: новый заказ приходит с кнопками статусов, клиент получает уведомления.
          /admin — сводка за сегодня и последние заказы.
"""
import asyncio
import html
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "shop.db")

# ======================= НАСТРОЙКИ ПОД КЛИЕНТА =======================
SHOP_NAME = "Кофейня «Тёплый угол»"
CURRENCY = "€"
DELIVERY_FEE = 300  # в центах: 3,00 €
FREE_DELIVERY_FROM = 2500  # бесплатная доставка от 25,00 €
MAX_QTY = 9  # максимум одной позиции за раз
MAX_CART_QTY = 20  # максимум одной позиции в корзине

WELCOME_IMAGE = Path(__file__).parent / "welcome.png"
WELCOME_TEXT = (
    f"👋 Добро пожаловать в <b>{SHOP_NAME}</b>!\n\n"
    "Выберите напитки и выпечку, оформите заказ за минуту — "
    "заберите сами или закажите доставку. Мы сообщим, когда всё будет готово."
)
CONTACTS_TEXT = (
    "📍 <b>Контакты</b>\n\n"
    "Адрес: ул. Примерная, 1\n"
    "Телефон: +49 000 0000000\n"
    "Часы работы: пн–вс, 08:00–19:00\n\n"
    f"Доставка: {DELIVERY_FEE / 100:.0f} €, бесплатно от {FREE_DELIVERY_FROM / 100:.0f} €."
)

CATEGORIES = {
    "coffee": "☕ Кофе",
    "bakery": "🥐 Выпечка",
    "dessert": "🍰 Десерты",
}
# Цены в центах
ITEMS = {
    1: {"cat": "coffee", "name": "Эспрессо", "desc": "Классический, насыщенный, 30 мл.", "price": 220},
    2: {"cat": "coffee", "name": "Капучино", "desc": "Эспрессо с нежной молочной пенкой, 200 мл.", "price": 350},
    3: {"cat": "coffee", "name": "Латте", "desc": "Мягкий кофе с большим количеством молока, 300 мл.", "price": 380},
    4: {"cat": "coffee", "name": "Флэт уайт", "desc": "Двойной эспрессо и бархатное молоко, 180 мл.", "price": 390},
    5: {"cat": "bakery", "name": "Круассан", "desc": "Масляный, свежий, хрустящий.", "price": 250},
    6: {"cat": "bakery", "name": "Булочка с корицей", "desc": "Тёплая, с ароматной корицей и глазурью.", "price": 280},
    7: {"cat": "bakery", "name": "Сэндвич с лососем", "desc": "Тосты, сливочный сыр, слабосолёный лосось.",
        "price": 520},
    8: {"cat": "dessert", "name": "Чизкейк Нью-Йорк", "desc": "Классический, на песочной основе.", "price": 450},
    9: {"cat": "dessert", "name": "Тирамису", "desc": "Маскарпоне, савоярди, кофейная пропитка.", "price": 480},
    10: {"cat": "dessert", "name": "Брауни", "desc": "Шоколадный, с хрустящей корочкой.", "price": 390},
}
# =====================================================================

STATUSES = {
    "new": "🆕 Новый",
    "accepted": "👍 Принят",
    "cooking": "👨‍🍳 Готовится",
    "ready": "✅ Готов",
    "cancelled": "❌ Отменён",
}
ADMIN_STATUS_BUTTONS = ["accepted", "cooking", "ready", "cancelled"]

router = Router()


class Checkout(StatesGroup):
    address = State()
    phone = State()
    confirm = State()


# ---------- Вспомогательное ----------
def money(cents: int) -> str:
    return f"{cents / 100:.2f}".replace(".", ",") + f" {CURRENCY}"


def esc(value) -> str:
    return html.escape(str(value or ""))


def delivery_fee(method: str, subtotal: int) -> int:
    return DELIVERY_FEE if method == "delivery" and subtotal < FREE_DELIVERY_FROM else 0


async def safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """edit_text, который не падает, если содержимое не изменилось."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------- База данных ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS cart (
    user_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    PRIMARY KEY (user_id, item_id)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    user_name TEXT,
    username TEXT,
    items TEXT NOT NULL,            -- JSON: [[название, кол-во, цена], ...]
    total INTEGER NOT NULL,         -- в центах, вместе с доставкой
    delivery_fee INTEGER NOT NULL DEFAULT 0,
    method TEXT NOT NULL,           -- pickup | delivery
    address TEXT,
    phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def q_all(sql: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def run(sql: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


async def cart_add(user_id: int, item_id: int, qty: int) -> None:
    await run(
        """INSERT INTO cart (user_id, item_id, qty) VALUES (?, ?, ?)
           ON CONFLICT(user_id, item_id) DO UPDATE SET qty = MIN(qty + excluded.qty, ?)""",
        (user_id, item_id, qty, MAX_CART_QTY),
    )


async def cart_remove(user_id: int, item_id: int) -> None:
    await run("DELETE FROM cart WHERE user_id=? AND item_id=?", (user_id, item_id))


async def cart_clear(user_id: int) -> None:
    await run("DELETE FROM cart WHERE user_id=?", (user_id,))


async def cart_lines(user_id: int):
    rows = await q_all("SELECT item_id, qty FROM cart WHERE user_id=? ORDER BY rowid", (user_id,))
    lines, subtotal = [], 0
    for r in rows:
        item = ITEMS.get(r["item_id"])
        if not item:
            continue
        lines.append((r["item_id"], item, r["qty"]))
        subtotal += item["price"] * r["qty"]
    return lines, subtotal


async def create_order(user, lines, fee: int, method: str, address: str, phone: str) -> int:
    subtotal = sum(it["price"] * qty for _, it, qty in lines)
    items = json.dumps([[it["name"], qty, it["price"]] for _, it, qty in lines], ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO orders (user_id, user_name, username, items, total, delivery_fee,
                                   method, address, phone, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user.id, user.full_name, user.username, items, subtotal + fee, fee, method,
             address, phone, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        await db.execute("DELETE FROM cart WHERE user_id=?", (user.id,))
        await db.commit()
        return cur.lastrowid


async def get_order(order_id: int) -> dict | None:
    rows = await q_all("SELECT * FROM orders WHERE id=?", (order_id,))
    return rows[0] if rows else None


# ---------- Тексты ----------
def order_text(o: dict, admin: bool = False) -> str:
    lines = [f"📦 <b>Заказ №{o['id']}</b> · {STATUSES[o['status']]}", f"🕒 {o['created_at']}", ""]
    for name, qty, price in json.loads(o["items"]):
        lines.append(f"• {esc(name)} × {qty} — {money(price * qty)}")
    lines.append("")
    if o["method"] == "delivery":
        fee = money(o["delivery_fee"]) if o["delivery_fee"] else "бесплатно"
        lines += [f"🚗 Доставка: {fee}", f"Адрес: {esc(o['address'])}"]
    else:
        lines.append("🏃 Самовывоз")
    lines += [f"📱 {esc(o['phone'])}", f"<b>Итого: {money(o['total'])}</b>"]
    if admin:
        who = esc(o["user_name"]) + (f" (@{esc(o['username'])})" if o["username"] else "")
        lines.append(f"👤 {who}")
    return "\n".join(lines)


def item_text(item: dict, qty: int) -> str:
    return (
        f"<b>{esc(item['name'])}</b>\n{esc(item['desc'])}\n\n"
        f"Цена: {money(item['price'])}\nКоличество: {qty}\n"
        f"Сумма: <b>{money(item['price'] * qty)}</b>"
    )


def summary_text(lines, subtotal: int, fee: int, method: str, address: str, phone: str) -> str:
    out = ["📋 <b>Проверьте заказ</b>", ""]
    for _, item, qty in lines:
        out.append(f"• {esc(item['name'])} × {qty} — {money(item['price'] * qty)}")
    out.append("")
    if method == "delivery":
        out += [f"🚗 Доставка: {money(fee) if fee else 'бесплатно'}", f"Адрес: {esc(address)}"]
    else:
        out.append("🏃 Самовывоз")
    out += [f"📱 {esc(phone)}", f"<b>Итого: {money(subtotal + fee)}</b>"]
    return "\n".join(out)


def client_status_text(o: dict) -> str:
    n = o["id"]
    texts = {
        "accepted": f"👍 Заказ №{n} принят, скоро начнём готовить.",
        "cooking": f"👨‍🍳 Заказ №{n} готовится.",
        "ready": (f"✅ Заказ №{n} готов и скоро будет у вас!" if o["method"] == "delivery"
                  else f"✅ Заказ №{n} готов — ждём вас!"),
        "cancelled": f"❌ Заказ №{n} отменён. Если это ошибка — свяжитесь с нами.",
    }
    return texts.get(o["status"], f"Заказ №{n}: {STATUSES[o['status']]}")


# ---------- Клавиатуры ----------
def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Каталог", callback_data="menu:catalog")
    kb.button(text="🛒 Корзина", callback_data="menu:cart")
    kb.button(text="📦 Мои заказы", callback_data="menu:orders")
    kb.button(text="📍 Контакты", callback_data="menu:contacts")
    kb.adjust(1, 2, 1)
    return kb.as_markup()


def categories_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, title in CATEGORIES.items():
        kb.button(text=title, callback_data=f"cat:{key}")
    kb.button(text="🛒 Корзина", callback_data="menu:cart")
    kb.button(text="🏠 Меню", callback_data="menu:home")
    kb.adjust(1, 1, 1, 2)
    return kb.as_markup()


def category_kb(cat: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    ids = [i for i, it in ITEMS.items() if it["cat"] == cat]
    for i in ids:
        kb.button(text=f"{ITEMS[i]['name']} — {money(ITEMS[i]['price'])}", callback_data=f"item:{i}:1")
    kb.button(text="⬅️ Каталог", callback_data="menu:catalog")
    kb.button(text="🛒 Корзина", callback_data="menu:cart")
    kb.adjust(*([1] * len(ids)), 2)
    return kb.as_markup()


def item_kb(item_id: int, qty: int) -> InlineKeyboardMarkup:
    item = ITEMS[item_id]
    kb = InlineKeyboardBuilder()
    kb.button(text="➖", callback_data=f"item:{item_id}:{qty - 1}")
    kb.button(text=f"{qty} шт", callback_data="noop")
    kb.button(text="➕", callback_data=f"item:{item_id}:{qty + 1}")
    kb.button(text=f"🛒 В корзину · {money(item['price'] * qty)}", callback_data=f"add:{item_id}:{qty}")
    kb.button(text="⬅️ Назад", callback_data=f"cat:{item['cat']}")
    kb.adjust(3, 1, 1)
    return kb.as_markup()


def admin_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for status in ADMIN_STATUS_BUTTONS:
        kb.button(text=STATUSES[status], callback_data=f"st:{order_id}:{status}")
    kb.adjust(2)
    return kb.as_markup()


async def render_cart(user_id: int):
    lines, subtotal = await cart_lines(user_id)
    kb = InlineKeyboardBuilder()
    if not lines:
        kb.button(text="🛍 В каталог", callback_data="menu:catalog")
        kb.button(text="🏠 Меню", callback_data="menu:home")
        kb.adjust(2)
        return "🛒 Корзина пуста.\n\nЗагляните в каталог — там есть из чего выбрать.", kb.as_markup()
    out = ["🛒 <b>Ваша корзина</b>", ""]
    for item_id, item, qty in lines:
        out.append(f"• {esc(item['name'])} × {qty} — {money(item['price'] * qty)}")
        kb.button(text=f"❌ {item['name']}", callback_data=f"cart:del:{item_id}")
    out += ["", f"Сумма: <b>{money(subtotal)}</b>"]
    if subtotal >= FREE_DELIVERY_FROM:
        out.append("🎉 Доставка бесплатно")
    else:
        out.append(f"До бесплатной доставки: {money(FREE_DELIVERY_FROM - subtotal)}")
    kb.button(text="✅ Оформить заказ", callback_data="checkout:start")
    kb.button(text="🗑 Очистить", callback_data="cart:clear")
    kb.button(text="⬅️ Каталог", callback_data="menu:catalog")
    kb.adjust(*([2] * ((len(lines) + 1) // 2)), 1, 2)
    return "\n".join(out), kb.as_markup()


# ---------- Старт и меню ----------
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    # Картинка — отдельным сообщением, чтобы меню можно было редактировать
    if WELCOME_IMAGE.exists():
        await m.answer_photo(FSInputFile(WELCOME_IMAGE))
    await m.answer(WELCOME_TEXT, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Оформление отменено. Корзина сохранена.", reply_markup=ReplyKeyboardRemove())
    await m.answer("Что дальше?", reply_markup=main_menu())


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "menu:home")
async def menu_home(c: CallbackQuery):
    await safe_edit(c.message, WELCOME_TEXT, main_menu())
    await c.answer()


@router.callback_query(F.data == "menu:contacts")
async def menu_contacts(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data="menu:home")
    await safe_edit(c.message, CONTACTS_TEXT, kb.as_markup())
    await c.answer()


# ---------- Каталог ----------
@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(c: CallbackQuery):
    await safe_edit(c.message, "🛍 <b>Каталог</b>\nЧто хотите заказать?", categories_kb())
    await c.answer()


@router.callback_query(F.data.startswith("cat:"))
async def show_category(c: CallbackQuery):
    cat = c.data.split(":")[1]
    if cat not in CATEGORIES:
        await c.answer()
        return
    await safe_edit(c.message, f"<b>{CATEGORIES[cat]}</b>\nВыберите позицию:", category_kb(cat))
    await c.answer()


@router.callback_query(F.data.startswith("item:"))
async def show_item(c: CallbackQuery):
    _, item_id, qty = c.data.split(":")
    item = ITEMS.get(int(item_id))
    if not item:
        await c.answer("Позиция недоступна", show_alert=True)
        return
    qty = max(1, min(MAX_QTY, int(qty)))
    await safe_edit(c.message, item_text(item, qty), item_kb(int(item_id), qty))
    await c.answer()


@router.callback_query(F.data.startswith("add:"))
async def add_to_cart(c: CallbackQuery):
    _, item_id, qty = c.data.split(":")
    item = ITEMS.get(int(item_id))
    if not item:
        await c.answer("Позиция недоступна", show_alert=True)
        return
    qty = max(1, min(MAX_QTY, int(qty)))
    await cart_add(c.from_user.id, int(item_id), qty)
    await c.answer(f"Добавлено: {item['name']} × {qty}")
    await safe_edit(c.message, f"<b>{CATEGORIES[item['cat']]}</b>\nВыберите позицию:", category_kb(item["cat"]))


# ---------- Корзина ----------
@router.callback_query(F.data == "menu:cart")
async def menu_cart(c: CallbackQuery):
    text, kb = await render_cart(c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer()


@router.callback_query(F.data.startswith("cart:del:"))
async def cart_delete(c: CallbackQuery):
    await cart_remove(c.from_user.id, int(c.data.split(":")[2]))
    text, kb = await render_cart(c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer("Удалено")


@router.callback_query(F.data == "cart:clear")
async def cart_clear_all(c: CallbackQuery):
    await cart_clear(c.from_user.id)
    text, kb = await render_cart(c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer("Корзина очищена")


# ---------- Оформление заказа ----------
def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Или напишите номер вручную",
    )


async def ask_phone(message: Message) -> None:
    await message.answer(
        "Оставьте номер телефона для связи: нажмите кнопку ниже или напишите его сообщением.\n"
        "Отмена — /cancel",
        reply_markup=phone_kb(),
    )


@router.callback_query(F.data == "checkout:start")
async def checkout_start(c: CallbackQuery, state: FSMContext):
    lines, _ = await cart_lines(c.from_user.id)
    if not lines:
        await c.answer("Корзина пуста", show_alert=True)
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🏃 Самовывоз", callback_data="ship:pickup")
    kb.button(
        text=f"🚗 Доставка (+{money(DELIVERY_FEE)}, бесплатно от {money(FREE_DELIVERY_FROM)})",
        callback_data="ship:delivery",
    )
    kb.button(text="⬅️ В корзину", callback_data="menu:cart")
    kb.adjust(1)
    await safe_edit(c.message, "Как хотите получить заказ?", kb.as_markup())
    await c.answer()


@router.callback_query(F.data.in_({"ship:pickup", "ship:delivery"}))
async def choose_shipping(c: CallbackQuery, state: FSMContext):
    method = c.data.split(":")[1]
    if method == "pickup":
        await state.update_data(method="pickup", address="")
        await state.set_state(Checkout.phone)
        await safe_edit(c.message, "🏃 Самовывоз. Адрес и часы работы — в разделе «Контакты».")
        await ask_phone(c.message)
    else:
        await state.update_data(method="delivery")
        await state.set_state(Checkout.address)
        await safe_edit(c.message, "🚗 Доставка.\nНапишите адрес: улица, дом, квартира.\nОтмена — /cancel")
    await c.answer()


@router.message(Checkout.address, F.text)
async def got_address(m: Message, state: FSMContext):
    address = m.text.strip()
    if len(address) < 5:
        await m.answer("Адрес слишком короткий. Напишите улицу, дом и квартиру.")
        return
    await state.update_data(address=address[:200])
    await state.set_state(Checkout.phone)
    await ask_phone(m)


@router.message(Checkout.phone)
async def got_phone(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else (m.text or "").strip()
    if sum(ch.isdigit() for ch in phone) < 6:
        await m.answer("Не похоже на номер телефона. Попробуйте ещё раз, например +49 170 1234567.")
        return
    await state.update_data(phone=phone[:30])
    await state.set_state(Checkout.confirm)
    await m.answer("👌 Номер получен.", reply_markup=ReplyKeyboardRemove())

    data = await state.get_data()
    lines, subtotal = await cart_lines(m.from_user.id)
    if not lines:
        await state.clear()
        await m.answer("Корзина пуста. Добавьте что-нибудь из каталога.", reply_markup=main_menu())
        return
    fee = delivery_fee(data["method"], subtotal)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить заказ", callback_data="order:confirm")
    kb.button(text="❌ Отмена", callback_data="order:cancel")
    kb.adjust(1)
    await m.answer(
        summary_text(lines, subtotal, fee, data["method"], data.get("address", ""), data["phone"]),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "order:cancel")
async def order_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "Оформление отменено. Корзина сохранена.", main_menu())
    await c.answer()


@router.callback_query(F.data == "order:confirm")
async def order_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("method") or not data.get("phone"):
        await state.clear()
        await c.answer("Сессия устарела, оформите заказ заново", show_alert=True)
        return
    lines, subtotal = await cart_lines(c.from_user.id)
    if not lines:
        await state.clear()
        await c.answer("Корзина пуста", show_alert=True)
        return
    fee = delivery_fee(data["method"], subtotal)
    order_id = await create_order(c.from_user, lines, fee, data["method"], data.get("address", ""), data["phone"])
    await state.clear()
    order = await get_order(order_id)

    await safe_edit(
        c.message,
        f"🎉 <b>Заказ №{order_id} принят!</b>\n\n{order_text(order)}\n\nМы сообщим, когда всё будет готово.",
        main_menu(),
    )
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, order_text(order, admin=True), reply_markup=admin_kb(order_id))
        except (TelegramBadRequest, TelegramForbiddenError):
            logging.warning("Не удалось отправить уведомление владельцу (ADMIN_ID=%s)", ADMIN_ID)
    await c.answer("Заказ оформлен")


# ---------- Мои заказы ----------
@router.message(Command("orders"))
async def cmd_orders(m: Message):
    rows = await q_all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 3", (m.from_user.id,))
    text = "\n\n".join(order_text(o) for o in rows) if rows else "У вас пока нет заказов."
    await m.answer(text, reply_markup=main_menu())


@router.callback_query(F.data == "menu:orders")
async def menu_orders(c: CallbackQuery):
    rows = await q_all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 3", (c.from_user.id,))
    text = "<b>Последние заказы</b>\n\n" + "\n\n".join(
        order_text(o) for o in rows) if rows else "У вас пока нет заказов."
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Каталог", callback_data="menu:catalog")
    kb.button(text="🏠 Меню", callback_data="menu:home")
    kb.adjust(2)
    await safe_edit(c.message, text, kb.as_markup())
    await c.answer()


# ---------- Владелец ----------
@router.callback_query(F.data.startswith("st:"))
async def change_status(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True)
        return
    _, order_id, status = c.data.split(":")
    if status not in STATUSES:
        await c.answer()
        return
    await run("UPDATE orders SET status=? WHERE id=?", (status, int(order_id)))
    order = await get_order(int(order_id))
    if not order:
        await c.answer("Заказ не найден", show_alert=True)
        return
    await safe_edit(c.message, order_text(order, admin=True), admin_kb(order["id"]))
    try:
        await bot.send_message(order["user_id"], client_status_text(order))
    except (TelegramBadRequest, TelegramForbiddenError):
        pass  # клиент мог заблокировать бота
    await c.answer("Статус обновлён")


@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    stats = await q_all(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS s FROM orders "
        "WHERE created_at LIKE ? AND status != 'cancelled'",
        (f"{today}%",),
    )
    recent = await q_all("SELECT * FROM orders ORDER BY id DESC LIMIT 10")
    out = [f"📊 <b>Сегодня:</b> {stats[0]['n']} заказов на {money(stats[0]['s'])}", ""]
    if recent:
        out.append("<b>Последние заказы:</b>")
        for o in recent:
            out.append(f"№{o['id']} · {o['created_at'][11:16]} · {money(o['total'])} · {STATUSES[o['status']]}")
    else:
        out.append("Заказов пока нет.")
    await m.answer("\n".join(out))


@router.message()
async def fallback(m: Message):
    await m.answer("Откройте меню: /start")


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Укажите BOT_TOKEN в файле .env")
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Меню"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="cancel", description="Отменить оформление"),
    ])
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
