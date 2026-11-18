import os
import json
import logging
import sqlite3
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "/data/not_bot.sqlite3")
PORT = int(os.environ.get("PORT", "8080"))

if not WEBAPP_URL.startswith("https://"):
    logging.warning("WEBAPP_URL is not configured as an HTTPS Railway public URL.")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("not-bot")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db_lock = asyncio.Lock()

DAILY_REWARDS = [130, 133, 154, 160, 175, 190, 220]
COLLECT_REWARD = 390
REFERRAL_REWARD = 100
MIN_EXCHANGE = 5000
USD_PER_500 = 1.30
MIN_WITHDRAW_USD = 1.0

def utcnow():
    return datetime.now(timezone.utc)

def iso_now():
    return utcnow().isoformat()

def setup_db():
    db.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT NOT NULL DEFAULT '',
        balance INTEGER NOT NULL DEFAULT 0,
        last_collect TEXT,
        last_daily TEXT,
        daily_streak INTEGER NOT NULL DEFAULT 0,
        referred_by INTEGER,
        ton_wallet TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS exchanges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        coins INTEGER NOT NULL,
        usd REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount_usd REAL NOT NULL,
        wallet TEXT NOT NULL,
        network TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    );
    """)
    # Safe migration for databases created by earlier versions.
    columns = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    if "ton_wallet" not in columns:
        db.execute("ALTER TABLE users ADD COLUMN ton_wallet TEXT")
    db.commit()

def get_user(tg_user):
    db.execute("""
        INSERT OR IGNORE INTO users(user_id, username, created_at)
        VALUES (?, ?, ?)
    """, (tg_user.id, tg_user.username or "", iso_now()))
    db.execute(
        "UPDATE users SET username=? WHERE user_id=?",
        (tg_user.username or "", tg_user.id)
    )
    db.commit()
    return db.execute("SELECT * FROM users WHERE user_id=?", (tg_user.id,)).fetchone()

def log_transaction(user_id, kind, amount, currency="NOT", note=""):
    db.execute("""
        INSERT INTO transactions(user_id, kind, amount, currency, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, kind, amount, currency, note, iso_now()))

def add_coins(user_id, amount, kind, note=""):
    db.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (amount, user_id)
    )
    log_transaction(user_id, kind, amount, "NOT", note)

def seconds_left(last_value, hours):
    if not last_value:
        return 0
    last = datetime.fromisoformat(last_value)
    remaining = timedelta(hours=hours) - (utcnow() - last)
    return max(0, int(remaining.total_seconds()))

def fmt_duration(seconds):
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"

def main_keyboard(user_id):
    rows = [
        [InlineKeyboardButton("💰 جمع العملات", callback_data="collect"),
         InlineKeyboardButton("🎁 المكافأة اليومية", callback_data="daily")],
        [InlineKeyboardButton("👥 الفريق والدعوات", callback_data="team")],
        [InlineKeyboardButton("💵 تبديل العملات", callback_data="exchange")],
        [InlineKeyboardButton("💳 طلب سحب", callback_data="withdraw")],
        [InlineKeyboardButton("👛 ربط محفظة TON", callback_data="ton_wallet")],
        [InlineKeyboardButton("👤 حسابي", callback_data="account")],
        [InlineKeyboardButton("⭐ شراء خطة (تحت الصيانة)", callback_data="plans")]
    ]
    return InlineKeyboardMarkup(rows)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    row = get_user(tg_user)

    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != tg_user.id and row["referred_by"] is None:
                referrer = db.execute(
                    "SELECT user_id FROM users WHERE user_id=?", (referrer_id,)
                ).fetchone()
                if referrer:
                    db.execute(
                        "UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL",
                        (referrer_id, tg_user.id)
                    )
                    if db.execute("SELECT changes()").fetchone()[0] == 1:
                        add_coins(referrer_id, REFERRAL_REWARD, "referral",
                                  f"دعوة المستخدم {tg_user.id}")
                    db.commit()
        except (ValueError, TypeError):
            pass

    text = (
        "💎 أهلاً بك في NOT\n\n"
        f"رصيدك الحالي: {row['balance']} NOT\n"
        "اجمع العملات واستفد من المكافآت اليومية ودعوة الأصدقاء."
    )
    await update.message.reply_text(text, reply_markup=main_keyboard(tg_user.id))

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    row = get_user(user)
    uid = user.id
    action = query.data

    if action == "collect":
        wait = seconds_left(row["last_collect"], 24)
        if wait:
            await query.message.reply_text(
                f"⏳ يمكنك الجمع مجدداً بعد {fmt_duration(wait)}",
                reply_markup=main_keyboard(uid)
            )
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            fresh = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            if seconds_left(fresh["last_collect"], 24):
                db.rollback()
                await query.message.reply_text("⏳ الجمع غير متاح الآن.")
                return
            add_coins(uid, COLLECT_REWARD, "collect", "جمع كل 24 ساعة")
            db.execute("UPDATE users SET last_collect=? WHERE user_id=?", (iso_now(), uid))
            db.commit()
        except Exception:
            db.rollback()
            raise
        await query.message.reply_text("✅ تمت إضافة 390 NOT إلى رصيدك.",
                                       reply_markup=main_keyboard(uid))

    elif action == "daily":
        db.execute("BEGIN IMMEDIATE")
        try:
            fresh = db.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            wait = seconds_left(fresh["last_daily"], 24)
            if wait:
                db.rollback()
                await query.message.reply_text(
                    f"⏳ المكافأة القادمة بعد {fmt_duration(wait)}",
                    reply_markup=main_keyboard(uid)
                )
                return
            last = datetime.fromisoformat(fresh["last_daily"]) if fresh["last_daily"] else None
            streak = fresh["daily_streak"]
            if last and utcnow() - last >= timedelta(hours=48):
                streak = 0
            day = streak % 7 + 1
            reward = DAILY_REWARDS[day - 1]
            add_coins(uid, reward, "daily", f"مكافأة اليوم {day}")
            db.execute(
                "UPDATE users SET last_daily=?, daily_streak=? WHERE user_id=?",
                (iso_now(), day, uid)
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        await query.message.reply_text(
            f"🎁 مكافأة اليوم {day}\n✅ حصلت على {reward} NOT",
            reply_markup=main_keyboard(uid)
        )

    elif action == "team":
        me = await context.bot.get_me()
        link = f"https://t.me/{me.username}?start={uid}"
        count = db.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by=?", (uid,)
        ).fetchone()[0]
        total = db.execute("""
            SELECT COALESCE(SUM(amount),0) FROM transactions
            WHERE user_id=? AND kind='referral'
        """, (uid,)).fetchone()[0]
        await query.message.reply_text(
            f"👥 فريقك\nعدد المدعوين: {count}\n"
            f"أرباح الدعوات: {int(total)} NOT\n"
            f"مكافأة كل دعوة: 100 NOT\n\nرابطك:\n{link}",
            reply_markup=main_keyboard(uid)
        )

    elif action == "account":
        row = get_user(user)
        await query.message.reply_text(
            f"👤 حساب NOT\n🆔 {uid}\n💰 الرصيد: {row['balance']} NOT\n"
            f"👛 محفظة TON: {row['ton_wallet'] or 'غير مربوطة'}",
            reply_markup=main_keyboard(uid)
        )

    elif action == "ton_wallet":
        if not WEBAPP_URL:
            await query.message.reply_text(
                "ربط TON غير جاهز بعد. أضف WEBAPP_URL في Railway بعد تفعيل النطاق العام.",
                reply_markup=main_keyboard(uid)
            )
            return
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "👛 فتح وربط محفظة TON",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/")
            )
        ]])
        await query.message.reply_text(
            "افتح الصفحة واضغط Connect Wallet لربط محفظة TON.\n"
            "لن نطلب العبارة السرية أو المفتاح الخاص، ولن ننفذ أي معاملة.",
            reply_markup=keyboard
        )

    elif action == "exchange":
        context.user_data.clear()
        context.user_data["state"] = "exchange"
        await query.message.reply_text(
            "💵 تبديل العملات\n500 NOT = 1.30 USD\n"
            "الحد الأدنى 5000 NOT.\nأرسل عدد العملات:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("إلغاء", callback_data="cancel")
            ]])
        )

    elif action == "withdraw":
        context.user_data.clear()
        context.user_data["state"] = "withdraw_amount"
        await query.message.reply_text(
            "أرسل مبلغ السحب بالدولار (الحد الأدنى 1 USD).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("إلغاء", callback_data="cancel")
            ]])
        )

    elif action == "plans":
        await query.message.reply_text(
            "🚧 قسم شراء الخطط تحت الصيانة حالياً.",
            reply_markup=main_keyboard(uid)
        )

    elif action == "cancel":
        context.user_data.clear()
        await query.message.reply_text("تم إلغاء العملية.", reply_markup=main_keyboard(uid))

async def handle_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    try:
        payload = json.loads(message.web_app_data.data)
        if payload.get("type") != "ton_wallet_connected":
            raise ValueError("Unknown payload")
        address = payload.get("address", "")
        # TON raw addresses are typically 48 chars; friendly addresses may use URL-safe Base64.
        if not isinstance(address, str) or not (20 <= len(address) <= 100):
            raise ValueError("Invalid address")
        db.execute("UPDATE users SET ton_wallet=? WHERE user_id=?", (address, user.id))
        log_transaction(user.id, "ton_wallet_link", 0, "TON", "ربط محفظة TON")
        db.commit()
        await message.reply_text(
            f"✅ تم ربط عنوان محفظة TON بحسابك:\n{address}",
            reply_markup=main_keyboard(user.id)
        )
    except Exception:
        await message.reply_text("تعذر حفظ عنوان المحفظة. حاول الربط مرة أخرى.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    row = get_user(user)
    state = context.user_data.get("state")
    value = update.effective_message.text.strip()

    if state == "exchange":
        if not value.isdigit():
            await update.message.reply_text("أرسل كمية صحيحة بالأرقام.")
            return
        coins = int(value)
        if coins < MIN_EXCHANGE:
            await update.message.reply_text("الحد الأدنى للتبديل 5000 NOT.")
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            fresh = db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
            if coins > fresh["balance"]:
                db.rollback()
                await update.message.reply_text("رصيدك غير كافٍ.")
                return
            usd = round(coins / 500 * USD_PER_500, 2)
            db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (coins, uid))
            log_transaction(uid, "exchange", -coins, "NOT", "طلب تبديل قيد المراجعة")
            db.execute(
                "INSERT INTO exchanges(user_id,coins,usd,created_at) VALUES(?,?,?,?)",
                (uid, coins, usd, iso_now())
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        context.user_data.clear()
        await update.message.reply_text(
            f"تم تسجيل طلب التبديل رقمياً.\n{coins} NOT = {usd:.2f} USD\n"
            "الحالة: قيد المراجعة. هذا ليس تحويلاً مالياً منفذاً.",
            reply_markup=main_keyboard(uid)
        )

    elif state == "withdraw_amount":
        try:
            amount = round(float(value), 2)
            if amount < MIN_WITHDRAW_USD or amount > 1_000_000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("أدخل مبلغاً صحيحاً لا يقل عن 1 دولار.")
            return
        context.user_data["withdraw_amount"] = amount
        context.user_data["state"] = "withdraw_wallet"
        await update.message.reply_text("أرسل عنوان محفظة TON التي تريد استقبال السحب عليها:")

    elif state == "withdraw_wallet":
        if not (20 <= len(value) <= 100):
            await update.message.reply_text("صيغة العنوان غير مقبولة. أعد إرسال العنوان.")
            return
        amount = context.user_data["withdraw_amount"]
        with db:
            db.execute("""
                INSERT INTO withdrawals(user_id,amount_usd,wallet,network,created_at)
                VALUES(?,?,?,?,?)
            """, (uid, amount, value, "TON", iso_now()))
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم تسجيل طلب السحب بقيمة {amount:.2f} USD.\n"
            "الحالة: قيد المراجعة. لم يتم إرسال أي أموال.",
            reply_markup=main_keyboard(uid)
        )

    else:
        await update.message.reply_text("اختر خدمة من القائمة:", reply_markup=main_keyboard(uid))

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = db.execute("""
        SELECT kind, amount, currency, created_at
        FROM transactions WHERE user_id=?
        ORDER BY id DESC LIMIT 10
    """, (uid,)).fetchall()
    if not rows:
        await update.message.reply_text("لا توجد عمليات مسجلة.")
        return
    lines = ["📜 آخر عملياتك:"]
    for item in rows:
        lines.append(f"• {item['kind']}: {item['amount']} {item['currency']} — {item['created_at'][:10]}")
    await update.message.reply_text("\n".join(lines))

def build_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_web_app_data))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application

bot_app = build_bot()

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_db()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    log.info("NOT Telegram bot started.")
    try:
        yield
    finally:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
        db.close()

web = FastAPI(title="NOT Bot", lifespan=lifespan)
web.mount("/static", StaticFiles(directory="web"), name="static")

@web.get("/")
async def homepage():
    return FileResponse("web/index.html")

@web.get("/tonconnect-manifest.json")
async def ton_manifest(request: Request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "url": base,
        "name": "NOT",
        "iconUrl": base + "/static/not-icon.png"
    })

@web.get("/health")
async def health():
    return {"status": "ok", "service": "NOT"}

if __name__ == "__main__":
    uvicorn.run(web, host="0.0.0.0", port=PORT)
