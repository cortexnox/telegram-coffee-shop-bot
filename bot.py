"""
Telegram bot: catalog, shopping cart and orders (aiogram 3 + SQLite).

Customer:  category catalog -> product card -> shopping cart -> checkout
           (pickup/delivery, address, phone) -> confirmation -> "My Orders".
Owner:     receives new orders with status buttons, customer gets notifications.
           /admin — today's summary and recent orders.
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

# ======================= SHOP CONFIGURATION =======================
SHOP_NAME = "The Cozy Corner Coffee Shop"
CURRENCY = "€"
DELIVERY_FEE = 300  # In cents: €3.00
FREE_DELIVERY_FROM = 2500  # Free delivery on orders of €25.00 or more
MAX_QTY = 9  # Maximum quantity per product selection
MAX_CART_QTY = 20  # Maximum quantity of one product in the cart

WELCOME_IMAGE = Path(__file__).parent / "welcome.png"
WELCOME_TEXT = (
    f"👋 Welcome to <b>{SHOP_NAME}</b>!\n\n"
    "Choose your favorite drinks and pastries and place your order in just a minute — "
    "pick it up yourself or have it delivered. We'll let you know when everything is ready."
)
CONTACTS_TEXT = (
    "📍 <b>Contact Information</b>\n\n"
    "Address: 1 Example Street\n"
    "Phone: +49 000 0000000\n"
    "Opening hours: Mon–Sun, 08:00–19:00\n\n"
    f"Delivery fee: {DELIVERY_FEE / 100:.0f} €, free for orders over "
    f"{FREE_DELIVERY_FROM / 100:.0f} €."
)

CATEGORIES = {
    "coffee": "☕ Coffee",
    "bakery": "🥐 Bakery",
    "dessert": "🍰 Desserts",
}

# Prices are stored in cents
ITEMS = {
    1: {"cat": "coffee", "name": "Espresso", "desc": "Classic, rich espresso, 30 ml.", "price": 220},
    2: {"cat": "coffee", "name": "Cappuccino", "desc": "Espresso topped with silky milk foam, 200 ml.", "price": 350},
    3: {"cat": "coffee", "name": "Latte", "desc": "Smooth coffee with plenty of milk, 300 ml.", "price": 380},
    4: {"cat": "coffee", "name": "Flat White", "desc": "Double espresso with velvety milk, 180 ml.", "price": 390},
    5: {"cat": "bakery", "name": "Croissant", "desc": "Buttery, fresh and crispy.", "price": 250},
    6: {"cat": "bakery", "name": "Cinnamon Roll", "desc": "Warm pastry with aromatic cinnamon and icing.", "price": 280},
    7: {"cat": "bakery", "name": "Salmon Sandwich", "desc": "Toasted bread, cream cheese and lightly salted salmon.", "price": 520},
    8: {"cat": "dessert", "name": "New York Cheesecake", "desc": "Classic cheesecake with a biscuit base.", "price": 450},
    9: {"cat": "dessert", "name": "Tiramisu", "desc": "Mascarpone, ladyfingers and coffee-soaked layers.", "price": 480},
    10: {"cat": "dessert", "name": "Brownie", "desc": "Rich chocolate brownie with a crispy crust.", "price": 390},
}
# ==================================================================

STATUSES = {
    "new": "🆕 New",
    "accepted": "👍 Accepted",
    "cooking": "👨‍🍳 Preparing",
    "ready": "✅ Ready",
    "cancelled": "❌ Cancelled",
}
ADMIN_STATUS_BUTTONS = ["accepted", "cooking", "ready", "cancelled"]

router = Router()


class Checkout(StatesGroup):
    address = State()
    phone = State()
    confirm = State()


# ---------- Helpers ----------
def money(cents: int) -> str:
    return f"{cents / 100:.2f}".replace(".", ",") + f" {CURRENCY}"


def esc(value) -> str:
    return html.escape(str(value or ""))


def delivery_fee(method: str, subtotal: int) -> int:
    return DELIVERY_FEE if method == "delivery" and subtotal < FREE_DELIVERY_FROM else 0


async def safe_edit(message: Message, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    """Edit a message without raising an error if its content has not changed."""
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ---------- Database ----------
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
    items TEXT NOT NULL,
    total INTEGER NOT NULL,
    delivery_fee INTEGER NOT NULL DEFAULT 0,
    method TEXT NOT NULL,
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


# ---------- Text templates ----------
def order_text(o: dict, admin: bool = False) -> str:
    lines = [f"📦 <b>Order #{o['id']}</b> · {STATUSES[o['status']]}", f"🕒 {o['created_at']}", ""]
    for name, qty, price in json.loads(o["items"]):
        lines.append(f"• {esc(name)} × {qty} — {money(price * qty)}")
    lines.append("")
    if o["method"] == "delivery":
        fee = money(o["delivery_fee"]) if o["delivery_fee"] else "Free"
        lines += [f"🚗 Delivery: {fee}", f"Address: {esc(o['address'])}"]
    else:
        lines.append("🏃 Pickup")
    lines += [f"📱 {esc(o['phone'])}", f"<b>Total: {money(o['total'])}</b>"]
    if admin:
        who = esc(o["user_name"]) + (f" (@{esc(o['username'])})" if o["username"] else "")
        lines.append(f"👤 Customer: {who}")
    return "\n".join(lines)


def item_text(item: dict, qty: int) -> str:
    return (
        f"<b>{esc(item['name'])}</b>\n{esc(item['desc'])}\n\n"
        f"Price: {money(item['price'])}\nQuantity: {qty}\n"
        f"Subtotal: <b>{money(item['price'] * qty)}</b>"
    )


def summary_text(lines, subtotal: int, fee: int, method: str, address: str, phone: str) -> str:
    out = ["📋 <b>Please review your order</b>", ""]
    for _, item, qty in lines:
        out.append(f"• {esc(item['name'])} × {qty} — {money(item['price'] * qty)}")
    out.append("")
    if method == "delivery":
        out += [f"🚗 Delivery: {money(fee) if fee else 'Free'}", f"Address: {esc(address)}"]
    else:
        out.append("🏃 Pickup")
    out += [f"📱 {esc(phone)}", f"<b>Total: {money(subtotal + fee)}</b>"]
    return "\n".join(out)


def client_status_text(o: dict) -> str:
    n = o["id"]
    texts = {
        "accepted": f"👍 Your order #{n} has been accepted. We'll start preparing it soon.",
        "cooking": f"👨‍🍳 Your order #{n} is being prepared.",
        "ready": (
            f"✅ Your order #{n} is ready and will be delivered soon!"
            if o["method"] == "delivery"
            else f"✅ Your order #{n} is ready — we're waiting for you!"
        ),
        "cancelled": f"❌ Your order #{n} has been cancelled. If you think this is a mistake, please contact us.",
    }
    return texts.get(o["status"], f"Order #{n}: {STATUSES[o['status']]}")


# ---------- Keyboards ----------
def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Catalog", callback_data="menu:catalog")
    kb.button(text="🛒 Shopping Cart", callback_data="menu:cart")
    kb.button(text="📦 My Orders", callback_data="menu:orders")
    kb.button(text="📍 Contact Information", callback_data="menu:contacts")
    kb.adjust(1, 2, 1)
    return kb.as_markup()


def categories_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, title in CATEGORIES.items():
        kb.button(text=title, callback_data=f"cat:{key}")
    kb.button(text="🛒 Shopping Cart", callback_data="menu:cart")
    kb.button(text="🏠 Main Menu", callback_data="menu:home")
    kb.adjust(1, 1, 1, 2)
    return kb.as_markup()


def category_kb(cat: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    ids = [i for i, it in ITEMS.items() if it["cat"] == cat]
    for i in ids:
        kb.button(text=f"{ITEMS[i]['name']} — {money(ITEMS[i]['price'])}", callback_data=f"item:{i}:1")
    kb.button(text="⬅️ Back to Catalog", callback_data="menu:catalog")
    kb.button(text="🛒 Shopping Cart", callback_data="menu:cart")
    kb.adjust(*([1] * len(ids)), 2)
    return kb.as_markup()


def item_kb(item_id: int, qty: int) -> InlineKeyboardMarkup:
    item = ITEMS[item_id]
    kb = InlineKeyboardBuilder()
    kb.button(text="➖", callback_data=f"item:{item_id}:{qty - 1}")
    kb.button(text=f"{qty} pcs", callback_data="noop")
    kb.button(text="➕", callback_data=f"item:{item_id}:{qty + 1}")
    kb.button(text=f"🛒 Add to Cart · {money(item['price'] * qty)}", callback_data=f"add:{item_id}:{qty}")
    kb.button(text="⬅️ Back", callback_data=f"cat:{item['cat']}")
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
        kb.button(text="🛍 Browse Catalog", callback_data="menu:catalog")
        kb.button(text="🏠 Main Menu", callback_data="menu:home")
        kb.adjust(2)
        return "🛒 Your shopping cart is empty.\n\nTake a look at our catalog — there's plenty to choose from!", kb.as_markup()
    out = ["🛒 <b>Your Shopping Cart</b>", ""]
    for item_id, item, qty in lines:
        out.append(f"• {esc(item['name'])} × {qty} — {money(item['price'] * qty)}")
        kb.button(text=f"❌ {item['name']}", callback_data=f"cart:del:{item_id}")
    out += ["", f"Subtotal: <b>{money(subtotal)}</b>"]
    if subtotal >= FREE_DELIVERY_FROM:
        out.append("🎉 Free delivery!")
    else:
        out.append(f"Spend {money(FREE_DELIVERY_FROM - subtotal)} more to get free delivery.")
    kb.button(text="✅ Proceed to Checkout", callback_data="checkout:start")
    kb.button(text="🗑 Clear Cart", callback_data="cart:clear")
    kb.button(text="⬅️ Back to Catalog", callback_data="menu:catalog")
    kb.adjust(*([2] * ((len(lines) + 1) // 2)), 1, 2)
    return "\n".join(out), kb.as_markup()


# ---------- Start and Main Menu ----------
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    if WELCOME_IMAGE.exists():
        await m.answer_photo(FSInputFile(WELCOME_IMAGE))
    await m.answer(WELCOME_TEXT, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Checkout cancelled. Your shopping cart has been saved.", reply_markup=ReplyKeyboardRemove())
    await m.answer("What would you like to do next?", reply_markup=main_menu())


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
    kb.button(text="⬅️ Back to Menu", callback_data="menu:home")
    await safe_edit(c.message, CONTACTS_TEXT, kb.as_markup())
    await c.answer()


# ---------- Catalog ----------
@router.callback_query(F.data == "menu:catalog")
async def menu_catalog(c: CallbackQuery):
    await safe_edit(c.message, "🛍 <b>Catalog</b>\nWhat would you like to order?", categories_kb())
    await c.answer()


@router.callback_query(F.data.startswith("cat:"))
async def show_category(c: CallbackQuery):
    cat = c.data.split(":")[1]
    if cat not in CATEGORIES:
        await c.answer()
        return
    await safe_edit(c.message, f"<b>{CATEGORIES[cat]}</b>\nChoose a product:", category_kb(cat))
    await c.answer()


@router.callback_query(F.data.startswith("item:"))
async def show_item(c: CallbackQuery):
    _, item_id, qty = c.data.split(":")
    item = ITEMS.get(int(item_id))
    if not item:
        await c.answer("This product is unavailable.", show_alert=True)
        return
    qty = max(1, min(MAX_QTY, int(qty)))
    await safe_edit(c.message, item_text(item, qty), item_kb(int(item_id), qty))
    await c.answer()


@router.callback_query(F.data.startswith("add:"))
async def add_to_cart(c: CallbackQuery):
    _, item_id, qty = c.data.split(":")
    item = ITEMS.get(int(item_id))
    if not item:
        await c.answer("This product is unavailable.", show_alert=True)
        return
    qty = max(1, min(MAX_QTY, int(qty)))
    await cart_add(c.from_user.id, int(item_id), qty)
    await c.answer(f"Added: {item['name']} × {qty}")
    await safe_edit(c.message, f"<b>{CATEGORIES[item['cat']]}</b>\nChoose a product:", category_kb(item["cat"]))


# ---------- Shopping Cart ----------
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
    await c.answer("Product removed.")


@router.callback_query(F.data == "cart:clear")
async def cart_clear_all(c: CallbackQuery):
    await cart_clear(c.from_user.id)
    text, kb = await render_cart(c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer("Shopping cart cleared.")


# ---------- Checkout ----------
def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Share My Phone Number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Or enter your phone number manually",
    )


async def ask_phone(message: Message) -> None:
    await message.answer(
        "Please provide your phone number so we can contact you. "
        "Tap the button below or type your number in a message.\n"
        "To cancel, use /cancel",
        reply_markup=phone_kb(),
    )


@router.callback_query(F.data == "checkout:start")
async def checkout_start(c: CallbackQuery, state: FSMContext):
    lines, _ = await cart_lines(c.from_user.id)
    if not lines:
        await c.answer("Your shopping cart is empty.", show_alert=True)
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🏃 Pickup", callback_data="ship:pickup")
    kb.button(
        text=f"🚗 Delivery (+{money(DELIVERY_FEE)}, free from {money(FREE_DELIVERY_FROM)})",
        callback_data="ship:delivery",
    )
    kb.button(text="⬅️ Back to Cart", callback_data="menu:cart")
    kb.adjust(1)
    await safe_edit(c.message, "How would you like to receive your order?", kb.as_markup())
    await c.answer()


@router.callback_query(F.data.in_({"ship:pickup", "ship:delivery"}))
async def choose_shipping(c: CallbackQuery, state: FSMContext):
    method = c.data.split(":")[1]
    if method == "pickup":
        await state.update_data(method="pickup", address="")
        await state.set_state(Checkout.phone)
        await safe_edit(c.message, "🏃 Pickup selected. You can find our address and opening hours in the Contact Information section.")
        await ask_phone(c.message)
    else:
        await state.update_data(method="delivery")
        await state.set_state(Checkout.address)
        await safe_edit(c.message, "🚗 Delivery selected.\nPlease enter your address: street, house number and apartment.\nTo cancel, use /cancel")
    await c.answer()


@router.message(Checkout.address, F.text)
async def got_address(m: Message, state: FSMContext):
    address = m.text.strip()
    if len(address) < 5:
        await m.answer("The address is too short. Please enter your street, house number and apartment.")
        return
    await state.update_data(address=address[:200])
    await state.set_state(Checkout.phone)
    await ask_phone(m)


@router.message(Checkout.phone)
async def got_phone(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else (m.text or "").strip()
    if sum(ch.isdigit() for ch in phone) < 6:
        await m.answer("This doesn't look like a valid phone number. Please try again, e.g. +49 170 1234567.")
        return
    await state.update_data(phone=phone[:30])
    await state.set_state(Checkout.confirm)
    await m.answer("👌 Phone number received.", reply_markup=ReplyKeyboardRemove())

    data = await state.get_data()
    lines, subtotal = await cart_lines(m.from_user.id)
    if not lines:
        await state.clear()
        await m.answer("Your shopping cart is empty. Add something from the catalog first.", reply_markup=main_menu())
        return
    fee = delivery_fee(data["method"], subtotal)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Confirm Order", callback_data="order:confirm")
    kb.button(text="❌ Cancel", callback_data="order:cancel")
    kb.adjust(1)
    await m.answer(
        summary_text(lines, subtotal, fee, data["method"], data.get("address", ""), data["phone"]),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "order:cancel")
async def order_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(c.message, "Checkout cancelled. Your shopping cart has been saved.", main_menu())
    await c.answer()


@router.callback_query(F.data == "order:confirm")
async def order_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("method") or not data.get("phone"):
        await state.clear()
        await c.answer("Your session has expired. Please start checkout again.", show_alert=True)
        return
    lines, subtotal = await cart_lines(c.from_user.id)
    if not lines:
        await state.clear()
        await c.answer("Your shopping cart is empty.", show_alert=True)
        return
    fee = delivery_fee(data["method"], subtotal)
    order_id = await create_order(c.from_user, lines, fee, data["method"], data.get("address", ""), data["phone"])
    await state.clear()
    order = await get_order(order_id)

    await safe_edit(
        c.message,
        f"🎉 <b>Order #{order_id} has been placed!</b>\n\n{order_text(order)}\n\nWe'll notify you when everything is ready.",
        main_menu(),
    )
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, order_text(order, admin=True), reply_markup=admin_kb(order_id))
        except (TelegramBadRequest, TelegramForbiddenError):
            logging.warning("Failed to send order notification to the owner (ADMIN_ID=%s)", ADMIN_ID)
    await c.answer("Order placed successfully!")


# ---------- My Orders ----------
@router.message(Command("orders"))
async def cmd_orders(m: Message):
    rows = await q_all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 3", (m.from_user.id,))
    text = "\n\n".join(order_text(o) for o in rows) if rows else "You haven't placed any orders yet."
    await m.answer(text, reply_markup=main_menu())


@router.callback_query(F.data == "menu:orders")
async def menu_orders(c: CallbackQuery):
    rows = await q_all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 3", (c.from_user.id,))
    text = "<b>Recent Orders</b>\n\n" + "\n\n".join(
        order_text(o) for o in rows) if rows else "You haven't placed any orders yet."
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 Catalog", callback_data="menu:catalog")
    kb.button(text="🏠 Main Menu", callback_data="menu:home")
    kb.adjust(2)
    await safe_edit(c.message, text, kb.as_markup())
    await c.answer()


# ---------- Owner / Admin ----------
@router.callback_query(F.data.startswith("st:"))
async def change_status(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Access denied.", show_alert=True)
        return
    _, order_id, status = c.data.split(":")
    if status not in STATUSES:
        await c.answer()
        return
    await run("UPDATE orders SET status=? WHERE id=?", (status, int(order_id)))
    order = await get_order(int(order_id))
    if not order:
        await c.answer("Order not found.", show_alert=True)
        return
    await safe_edit(c.message, order_text(order, admin=True), admin_kb(order["id"]))
    try:
        await bot.send_message(order["user_id"], client_status_text(order))
    except (TelegramBadRequest, TelegramForbiddenError):
        pass  # The customer may have blocked the bot
    await c.answer("Order status updated.")


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
    out = [f"📊 <b>Today:</b> {stats[0]['n']} orders totaling {money(stats[0]['s'])}", ""]
    if recent:
        out.append("<b>Recent Orders:</b>")
        for o in recent:
            out.append(f"#{o['id']} · {o['created_at'][11:16]} · {money(o['total'])} · {STATUSES[o['status']]}")
    else:
        out.append("No orders yet.")
    await m.answer("\n".join(out))


@router.message()
async def fallback(m: Message):
    await m.answer("Open the menu using /start")


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Please specify BOT_TOKEN in your .env file.")
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Open main menu"),
        BotCommand(command="orders", description="View my orders"),
        BotCommand(command="cancel", description="Cancel checkout"),
    ])
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())