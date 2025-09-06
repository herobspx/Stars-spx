import os
import asyncio
import time
import sqlite3
from contextlib import closing
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ContentType
)
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

DB_PATH = "subs.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with closing(db()) as conn, conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            tg_username TEXT,
            status TEXT DEFAULT 'pending',
            current_plan_id TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS plans(
            id TEXT PRIMARY KEY,
            name TEXT,
            days INTEGER,
            price REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_id TEXT,
            receipt_file_id TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            admin_id INTEGER,
            decided_at INTEGER
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS subscriptions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_id TEXT,
            start_at INTEGER,
            end_at INTEGER,
            status TEXT DEFAULT 'active'
        )""")

        # إضافة الخطط إذا لم توجد
        c.execute("SELECT COUNT(*) AS cnt FROM plans")
        if c.fetchone()["cnt"] == 0:
            c.executemany(
                "INSERT INTO plans(id,name,days,price) VALUES(?,?,?,?)",
                [
                    ("month", "شهر", 30, 200.0),
                    ("2weeks", "أسبوعين", 14, 140.0),
                ]
            )

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def plans_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="شهر • 200﷼", callback_data="plan:month")
    kb.button(text="أسبوعين • 140﷼", callback_data="plan:2weeks")
    kb.adjust(1)
    return kb.as_markup()

def pay_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="أرسِل الإيصال", callback_data="send_receipt")
    kb.button(text="تغيير الخطة", callback_data="change_plan")
    kb.adjust(1)
    return kb.as_markup()

def admin_decision_kb(payment_id: int, user_id: int, plan_id: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ اعتماد", callback_data=f"adm_approve:{payment_id}:{user_id}:{plan_id}")
    kb.button(text="❌ رفض", callback_data=f"adm_reject:{payment_id}")
    kb.adjust(2)
    return kb.as_markup()

async def send_plans(message: Message):
    await message.answer(
        "اختر خطة الاشتراك المناسبة لك 👇",
        reply_markup=plans_keyboard()
    )

@dp.message(CommandStart())
async def start(message: Message):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO users(user_id, tg_username) VALUES(?, ?)",
            (message.from_user.id, message.from_user.username or "")
        )
    await message.answer("أهلًا في بوت الاشتراك لقناة توصيات SPX Options 📈")
    await send_plans(message)

@dp.callback_query(F.data.startswith("change_plan"))
async def change_plan(cb: CallbackQuery):
    await cb.message.edit_text("اختر خطة الاشتراك المناسبة لك 👇", reply_markup=plans_keyboard())
    await cb.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def choose_plan(cb: CallbackQuery):
    plan_id = cb.data.split(":")[1]
    with closing(db()) as conn, conn:
        conn.execute("UPDATE users SET current_plan_id=? WHERE user_id=?", (plan_id, cb.from_user.id))
        plan = conn.execute("SELECT name, days, price FROM plans WHERE id=?", (plan_id,)).fetchone()

    text = (
        f"خطة مختارة: *{plan['name']}* ({plan['days']} يوم) — السعر: {plan['price']}﷼\n\n"
        "طرق الدفع المتاحة:\n"
        "- تحويل بنكي\n"
        "  البنك: البنك العربي\n"
        "  الآيبان: SA1630100991104930184574\n"
        "  اسم الحساب: بدر محمد الجعيد\n\n"
        "بعد الدفع اضغط على (أرسِل الإيصال) ثم ارفع صورة/ملف إيصال الدفع."
    )
    await cb.message.edit_text(text, reply_markup=pay_keyboard(), parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "send_receipt")
async def ask_receipt(cb: CallbackQuery):
    await cb.message.answer("من فضلك أرسل *صورة أو ملف* لإيصال الدفع الآن.", parse_mode="Markdown")
    await cb.answer()

@dp.message(F.content_type.in_({ContentType.PHOTO, ContentType.DOCUMENT}))
async def handle_receipt(message: Message):
    with closing(db()) as conn:
        user = conn.execute("SELECT current_plan_id FROM users WHERE user_id=?", (message.from_user.id,)).fetchone()
    if not user or not user["current_plan_id"]:
        return await message.answer("اختر خطة أولًا عبر /start")

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id

    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO payments(user_id, plan_id, receipt_file_id, amount, status) VALUES(?,?,?,?,?)",
            (message.from_user.id, user["current_plan_id"], file_id, 0.0, "pending")
        )
        pay_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        plan = conn.execute("SELECT name, days, price FROM plans WHERE id=?", (user["current_plan_id"],)).fetchone()

    await message.answer("تم استلام الإيصال ✅ بانتظار مراجعة المشرف.")
    for admin in ADMIN_IDS:
        await bot.send_message(
            admin,
            (
                f"طلب اشتراك جديد 🚀\n"
                f"المستخدم: {message.from_user.full_name} (@{message.from_user.username}) [{message.from_user.id}]\n"
                f"الخطة: {plan['name']} — {plan['price']}﷼ / {plan['days']} يوم\n"
                f"رقم الدفع: {pay_id}"
            ),
            reply_markup=admin_decision_kb(pay_id, message.from_user.id, user["current_plan_id"])
        )
        if message.photo:
            await bot.send_photo(admin, photo=file_id, caption=f"إيصال الدفع #{pay_id}")
        elif message.document:
            await bot.send_document(admin, document=file_id, caption=f"إيصال الدفع #{pay_id}")

@dp.callback_query(F.data.startswith("adm_approve:"))
async def admin_approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("غير مصرح.", show_alert=True)

    _, payment_id, user_id, plan_id = cb.data.split(":")
    payment_id, user_id = int(payment_id), int(user_id)

    with closing(db()) as conn, conn:
        plan = conn.execute("SELECT name, days FROM plans WHERE id=?", (plan_id,)).fetchone()
        now = int(time.time())
        end_at = now + plan["days"] * 86400

        conn.execute(
            "INSERT INTO subscriptions(user_id, plan_id, start_at, end_at, status) VALUES(?,?,?,?,?)",
            (user_id, plan_id, now, end_at, "active")
        )
        conn.execute(
            "UPDATE payments SET status='approved', admin_id=?, decided_at=? WHERE id=?",
            (cb.from_user.id, now, payment_id)
        )
        conn.execute("UPDATE users SET status='active' WHERE user_id=?", (user_id,))

    expire_at = int(time.time()) + 5 * 60
    invite = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        expire_date=expire_at,
        member_limit=1,
        creates_join_request=False
    )

    try:
        await bot.send_message(
            user_id,
            (
                "تم اعتماد اشتراكك ✅\n"
                f"رابط الانضمام للقناة (صالح لمدة 5 دقائق ومرّة واحدة فقط):\n{invite.invite_link}\n\n"
                "إذا انتهت صلاحية الرابط قبل الدخول، راسل البوت لإعادة الإرسال."
            )
        )
    except Exception:
        pass

    await cb.message.edit_text(f"✅ تم اعتماد الدفع #{payment_id} للمستخدم {user_id}.")
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_reject:"))
async def admin_reject(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("غير مصرح.", show_alert=True)
    _, payment_id = cb.data.split(":")
    payment_id = int(payment_id)
    with closing(db()) as conn, conn:
        conn.execute(
            "UPDATE payments SET status='rejected', admin_id=?, decided_at=? WHERE id=?",
            (cb.from_user.id, int(time.time()), payment_id)
        )
    await cb.message.edit_text(f"❌ تم رفض الدفع #{payment_id}.")
    await cb.answer()

@dp.message(Command("extend"))
async def extend_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid, days = message.text.split()
        uid, days = int(uid), int(days)
    except Exception:
        return await message.reply("الاستخدام: /extend <user_id> <days>")
    with closing(db()) as conn, conn:
        sub = conn.execute(
            "SELECT id, end_at FROM subscriptions WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (uid,)
        ).fetchone()
        if not sub:
            return await message.reply("لا يوجد اشتراك نشط.")
        new_end = sub["end_at"] + days * 86400
        conn.execute("UPDATE subscriptions SET end_at=? WHERE id=?", (new_end, sub["id"]))
    await message.reply(f"تم التمديد حتى: {time.strftime('%Y-%m-%d %H:%M', time.localtime(new_end))}")

@dp.message(Command("end"))
async def end_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    try:
        _, uid = message.text.split()
        uid = int(uid)
    except Exception:
        return await message.reply("الاستخدام: /end <user_id>")
    await remove_from_channel(uid)
    await message.reply("تم إنهاء الاشتراك وإزالة العضو.")

async def remove_from_channel(user_id: int):
    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
    except Exception:
        pass
    with closing(db()) as conn, conn:
        conn.execute("UPDATE users SET status='expired' WHERE user_id=?", (user_id,))
        conn.execute("UPDATE subscriptions SET status='expired' WHERE user_id=? AND status='active'", (user_id,))

async def check_expirations():
    now = int(time.time())
    with closing(db()) as conn:
        expiring = conn.execute(
            "SELECT user_id FROM subscriptions WHERE end_at<=? AND status='active'",
            (now,)
        ).fetchall()
    for row in expiring:
        await remove_from_channel(row["user_id"])

async def on_startup():
    init_db()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_expirations, "interval", minutes=5)
    scheduler.start()

def main():
    asyncio.run(_main())

async def _main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    main()
