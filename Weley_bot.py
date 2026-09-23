import asyncio
import html
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = "8226258742:AAHh02o_I1aZ-3vReUdEeM7WA1KBI13BLHE"
ADMIN_ID = 6330924087
BOT_USERNAME = "weley_bot"

REQUIRED_GROUP_ID = -1003006178111
REQUIRED_GROUP_JOIN_URL = "https://t.me/solveearn_community"
REQUIRED_GROUP_NAME = "@solveearn_community"

DB_PATH = "weley.db"

JOIN_REWARD = 100
REFERRAL_REWARD = 45
POINT_USD_REFERENCE = 0.001
STARS_PER_POINT = 1
STAR_PACKAGES = [100, 250, 500, 1000, 1500, 2000]
CUSTOM_MIN_STARS = 101

PVP_WIN_REWARD = 50
PVP_PLAY_REWARD = 10
PVP_SEARCH_SECONDS = 10

GAMES = {
    "basketball": ("🏀 Basketball", "🏀"),
    "football": ("⚽ Football", "⚽"),
    "darts": ("🎯 Darts", "🎯"),
    "bowling": ("🎳 Bowling", "🎳"),
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("weley")

DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("PRAGMA foreign_keys=ON")

BOT_STOPPED = False
PVP_QUEUES = {key: [] for key in GAMES}
PVP_LOCK = asyncio.Lock()
DB_LOCK = asyncio.Lock()

# =========================
# HELPERS / DB
# =========================

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db(sql, params=()):
    cur = DB.execute(sql, params)
    DB.commit()
    return cur


def one(sql, params=()):
    return DB.execute(sql, params).fetchone()


def rows(sql, params=()):
    return DB.execute(sql, params).fetchall()


def ensure_column(table, column, definition):
    existing = {r[1] for r in DB.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        DB.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            referrer_id INTEGER,
            referral_count INTEGER NOT NULL DEFAULT 0,
            join_rewarded INTEGER NOT NULL DEFAULT 0,
            referral_rewarded INTEGER NOT NULL DEFAULT 0,
            banned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stars INTEGER NOT NULL,
            points INTEGER NOT NULL,
            payload TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            charge_id TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount INTEGER NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pvp_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_key TEXT NOT NULL,
            user1_id INTEGER NOT NULL,
            user2_id INTEGER NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        CREATE INDEX IF NOT EXISTS idx_purchases_status ON purchases(status);
        CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id);
        CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
        """
    )

    # Safe migration for older Weley databases.
    migrations = {
        "users": {
            "username": "TEXT",
            "first_name": "TEXT",
            "points": "INTEGER NOT NULL DEFAULT 0",
            "referrer_id": "INTEGER",
            "referral_count": "INTEGER NOT NULL DEFAULT 0",
            "join_rewarded": "INTEGER NOT NULL DEFAULT 0",
            "referral_rewarded": "INTEGER NOT NULL DEFAULT 0",
            "banned": "INTEGER NOT NULL DEFAULT 0",
            "created_at": "TEXT",
            "last_seen": "TEXT",
        },
        "purchases": {
            "user_id": "INTEGER",
            "stars": "INTEGER",
            "points": "INTEGER",
            "payload": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "charge_id": "TEXT",
            "created_at": "TEXT",
            "completed_at": "TEXT",
        },
    }
    for table, cols in migrations.items():
        for col, definition in cols.items():
            ensure_column(table, col, definition)

    stamp = now()
    DB.execute(
        "UPDATE users SET created_at=COALESCE(created_at, ?), last_seen=COALESCE(last_seen, ?) WHERE created_at IS NULL OR last_seen IS NULL",
        (stamp, stamp),
    )
    DB.commit()

    global BOT_STOPPED
    setting = one("SELECT value FROM settings WHERE key='bot_stopped'")
    BOT_STOPPED = setting and setting["value"] == "1"


def set_bot_stopped(value):
    global BOT_STOPPED
    BOT_STOPPED = bool(value)
    db(
        "INSERT INTO settings(key,value) VALUES('bot_stopped',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("1" if BOT_STOPPED else "0",),
    )


def ensure_user(tg_user, referrer_id=None):
    existing = one("SELECT * FROM users WHERE user_id=?", (tg_user.id,))
    stamp = now()

    if existing:
        db(
            "UPDATE users SET username=?, first_name=?, last_seen=? WHERE user_id=?",
            (tg_user.username, tg_user.first_name, stamp, tg_user.id),
        )
        return

    if referrer_id == tg_user.id:
        referrer_id = None

    db(
        """INSERT INTO users(user_id,username,first_name,referrer_id,created_at,last_seen)
        VALUES(?,?,?,?,?,?)""",
        (tg_user.id, tg_user.username, tg_user.first_name, referrer_id, stamp, stamp),
    )


def user_row(user_id):
    return one("SELECT * FROM users WHERE user_id=?", (user_id,))


def admin(user_id):
    return user_id == ADMIN_ID


def add_points(user_id, amount, kind, note):
    if amount == 0:
        return
    db("UPDATE users SET points=points+? WHERE user_id=?", (amount, user_id))
    db(
        "INSERT INTO ledger(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
        (user_id, kind, amount, note, now()),
    )


def set_points(user_id, amount, note):
    amount = max(0, int(amount))
    current = user_row(user_id)
    old = current["points"] if current else 0
    db("UPDATE users SET points=? WHERE user_id=?", (amount, user_id))
    db(
        "INSERT INTO ledger(user_id,kind,amount,note,created_at) VALUES(?,?,?,?,?)",
        (user_id, "admin_set", amount - old, note, now()),
    )


def fmt_name(row):
    if row["username"]:
        return "@" + html.escape(row["username"])
    return f"<code>{row['user_id']}</code>"


async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def joined_required_group(bot, user_id):
    try:
        member = await bot.get_chat_member(REQUIRED_GROUP_ID, user_id)
    except TelegramError as exc:
        log.warning("getChatMember failed: %s", exc)
        return False

    if member.status in {"creator", "administrator", "member"}:
        return True
    if member.status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def activate_user(user_id):
    async with DB_LOCK:
        u = user_row(user_id)
        if not u or u["join_rewarded"]:
            return 0, None

        add_points(user_id, JOIN_REWARD, "join_reward", "First verified join")
        db("UPDATE users SET join_rewarded=1 WHERE user_id=?", (user_id,))

        referral = None
        ref_id = u["referrer_id"]
        if ref_id and not u["referral_rewarded"]:
            ref = user_row(ref_id)
            if ref and not ref["banned"] and ref_id != user_id:
                add_points(ref_id, REFERRAL_REWARD, "referral_reward", f"Qualified referral {user_id}")
                db("UPDATE users SET referral_count=referral_count+1 WHERE user_id=?", (ref_id,))
                db("UPDATE users SET referral_rewarded=1 WHERE user_id=?", (user_id,))
                referral = (ref_id, REFERRAL_REWARD)
        return JOIN_REWARD, referral


# =========================
# KEYBOARDS
# =========================

def home_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Profile", callback_data="profile"), InlineKeyboardButton("🟣 Balance", callback_data="balance")],
        [InlineKeyboardButton("🔗 Refer", callback_data="refer"), InlineKeyboardButton("🎁 Rewards", callback_data="rewards")],
        [InlineKeyboardButton("⭐ Deposit Stars", callback_data="deposit"), InlineKeyboardButton("🧮 Converter", callback_data="converter")],
        [InlineKeyboardButton("🎮 PvP Arena", callback_data="pvp"), InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
    ])


def join_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Join now", url=REQUIRED_GROUP_JOIN_URL)],
        [InlineKeyboardButton("✅ I've Joined — Verify", callback_data="verify")],
    ])


def deposit_kb():
    kb = []
    row = []
    for stars in STAR_PACKAGES:
        row.append(InlineKeyboardButton(f"⭐ {stars}", callback_data=f"buy:{stars}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("✏️ Custom 101+ Stars", callback_data="custom")])
    kb.append([InlineKeyboardButton("🧾 My Purchases", callback_data="purchases")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(kb)


def pvp_kb():
    kb = [[InlineKeyboardButton(label, callback_data=f"game:{key}")] for key, (label, _) in GAMES.items()]
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="home")])
    return InlineKeyboardMarkup(kb)


def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Payment Requests", callback_data="a_requests"), InlineKeyboardButton("🔎 Search User", callback_data="a_search")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="a_broadcast"), InlineKeyboardButton("📊 Stats", callback_data="a_stats")],
        [InlineKeyboardButton("⏯ Maintenance", callback_data="a_toggle"), InlineKeyboardButton("🔄 Refresh", callback_data="a_refresh")],
        [InlineKeyboardButton("✖️ Close", callback_data="a_close")],
    ])


def user_admin_kb(user_id, banned):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ +100 WP", callback_data=f"u_add:{user_id}:100"), InlineKeyboardButton("➖ -100 WP", callback_data=f"u_add:{user_id}:-100")],
        [InlineKeyboardButton("✏️ Set WP", callback_data=f"u_set:{user_id}"), InlineKeyboardButton("📜 History", callback_data=f"u_history:{user_id}")],
        [InlineKeyboardButton("🚫 Ban" if not banned else "✅ Unban", callback_data=f"u_toggleban:{user_id}")],
        [InlineKeyboardButton("⬅️ Admin", callback_data="a_refresh")],
    ])


# =========================
# START / ADMIN
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    referrer = None
    if context.args:
        raw = context.args[0].strip()
        if raw.startswith("ref_"):
            raw = raw[4:]
        if raw.isdigit():
            referrer = int(raw)

    ensure_user(user, referrer)
    row = user_row(user.id)

    if row["banned"] and not admin(user.id):
        await update.message.reply_text("🚫 <b>Access restricted</b>\n\nYour Weley account is currently restricted.", parse_mode=ParseMode.HTML)
        return

    if BOT_STOPPED and not admin(user.id):
        await update.message.reply_text("🛠 <b>Weley is temporarily paused</b>\n\nPlease try again later.", parse_mode=ParseMode.HTML)
        return

    if not await joined_required_group(context.bot, user.id):
        await update.message.reply_text(
            f"🔐 <b>One step to unlock Weley</b>\n\nJoin <b>{html.escape(REQUIRED_GROUP_NAME)}</b> and then verify your membership.",
            reply_markup=join_kb(),
            parse_mode=ParseMode.HTML,
        )
        return

    reward, referral = await activate_user(user.id)
    text = (
        "🌟 <b>Welcome to Weley</b>\n\n"
        "Build Points, invite friends, buy Points with Telegram Stars, and play free PvP matches.\n\n"
        f"👋 Verified join: <b>+{JOIN_REWARD} WP</b>\n"
        f"🔗 Qualified referral: <b>+{REFERRAL_REWARD} WP</b>\n\n"
        "Choose an option below 👇"
    )
    if reward:
        text += f"\n\n🎉 <b>+{reward} WP</b> added to your balance."
    await update.message.reply_text(text, reply_markup=home_kb(), parse_mode=ParseMode.HTML)

    if referral:
        try:
            await context.bot.send_message(
                referral[0],
                f"🎉 <b>Referral activated!</b>\n\n<b>+{referral[1]} WP</b> added.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin(update.effective_user.id):
        return
    context.user_data.clear()
    await update.message.reply_text("🛠 <b>Weley Admin Panel</b>", reply_markup=admin_kb(), parse_mode=ParseMode.HTML)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ Current input mode cleared.", reply_markup=home_kb())


# =========================
# USER CALLBACKS
# =========================

async def user_callback(query, context, data):
    user = query.from_user
    row = user_row(user.id)

    if data == "home":
        await safe_edit(query, "🌟 <b>Weley</b>\n\nChoose what you want to do 👇", home_kb())
        return

    if data == "verify":
        if not await joined_required_group(context.bot, user.id):
            await safe_edit(query, "❌ <b>Not verified yet</b>\n\nJoin the required group, then try again.", join_kb())
            return
        reward, referral = await activate_user(user.id)
        msg = "✅ <b>Membership confirmed</b>\n\nYour Weley account is active."
        if reward:
            msg += f"\n\n🎉 <b>+{reward} WP</b> added."
        await safe_edit(query, msg, home_kb())
        if referral:
            try:
                await context.bot.send_message(referral[0], f"🎉 <b>New qualified referral!</b>\n\n+{referral[1]} WP added.", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
        return

    if data == "profile":
        await safe_edit(
            query,
            "👤 <b>Profile</b>\n\n"
            f"🪪 ID: <code>{row['user_id']}</code>\n"
            f"👤 Username: {fmt_name(row)}\n"
            f"🟣 Points: <b>{row['points']:,} WP</b>\n"
            f"👥 Qualified referrals: <b>{row['referral_count']}</b>\n"
            f"✅ Activated: <b>{'Yes' if row['join_rewarded'] else 'No'}</b>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]]),
        )
        return

    if data == "balance":
        ref = row["points"] * POINT_USD_REFERENCE
        await safe_edit(
            query,
            "🟣 <b>Balance</b>\n\n"
            f"🟣 Weley Points: <b>{row['points']:,} WP</b>\n"
            f"💵 Reference value: <b>${ref:,.3f}</b>\n\n"
            "ℹ️ The USD number is reference-only in this version and is not a cash-withdrawal balance.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Deposit Stars", callback_data="deposit")],
                [InlineKeyboardButton("⬅️ Back", callback_data="home")],
            ]),
        )
        return

    if data == "refer":
        me = await context.bot.get_me()
        bot_name = me.username or BOT_USERNAME
        link = f"https://t.me/{bot_name}?start=ref_{user.id}"
        share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('Join Weley and start earning Points!', safe='')}"
        await safe_edit(
            query,
            "🔗 <b>Your Referral Link</b>\n\n"
            f"<code>{html.escape(link)}</code>\n\n"
            f"🎁 <b>+{REFERRAL_REWARD} WP</b> for every qualified referral.\n"
            "A referral qualifies only after the invited user joins the required group and verifies.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 Share Link", url=share_url)],
                [InlineKeyboardButton("⬅️ Back", callback_data="home")],
            ]),
        )
        return

    if data == "rewards":
        await safe_edit(
            query,
            "🎁 <b>Weley Rewards</b>\n\n"
            f"👋 First verified join: <b>+{JOIN_REWARD} WP</b>\n"
            f"🔗 Qualified referral: <b>+{REFERRAL_REWARD} WP</b>\n"
            f"🏆 PvP win: <b>+{PVP_WIN_REWARD} WP</b>\n"
            f"🎮 PvP participation: <b>+{PVP_PLAY_REWARD} WP</b>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]]),
        )
        return

    if data == "converter":
        await safe_edit(
            query,
            "🧮 <b>Points Converter</b>\n\n"
            "Reference rate:\n"
            "• <b>250 WP = $0.25</b>\n"
            "• <b>1 WP = $0.001</b>\n\n"
            "⚠️ This is only a display/reference rate in this version.\n"
            "There is no cash or USDT withdrawal feature here.",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]]),
        )
        return

    if data == "deposit":
        await safe_edit(
            query,
            "⭐ <b>Deposit Stars</b>\n\n"
            f"Rate: <b>1 Star = {STARS_PER_POINT} WP</b>\n"
            "Choose a package below. Payments use Telegram Stars (XTR).",
            deposit_kb(),
        )
        return

    if data == "custom":
        context.user_data.clear()
        context.user_data["custom_stars"] = True
        await safe_edit(query, "✏️ <b>Custom deposit</b>\n\nSend a whole number of Stars greater than 100.\nExample: <code>350</code>\n\nUse /cancel to stop.")
        return

    if data.startswith("buy:"):
        try:
            stars = int(data.split(":", 1)[1])
        except ValueError:
            await query.answer("Invalid package", show_alert=True)
            return
        if stars not in STAR_PACKAGES:
            await query.answer("Package unavailable", show_alert=True)
            return
        await issue_invoice(query.message.chat_id, stars, context)
        return

    if data == "purchases":
        purchases = rows("SELECT * FROM purchases WHERE user_id=? ORDER BY id DESC LIMIT 8", (user.id,))
        if not purchases:
            text = "🧾 <b>My Purchases</b>\n\nNo purchases yet."
        else:
            lines = ["🧾 <b>My Purchases</b>\n"]
            for p in purchases:
                stamp = html.escape(p["created_at"].replace("T", " ")[:19])
                lines.append(f"#{p['id']} • ⭐ {p['stars']} → {p['points']} WP • <b>{p['status']}</b> • <code>{stamp} UTC</code>")
            text = "\n".join(lines)
        await safe_edit(query, text, InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Deposit Stars", callback_data="deposit")],
            [InlineKeyboardButton("⬅️ Back", callback_data="home")],
        ]))
        return

    if data == "pvp":
        await safe_edit(
            query,
            "🎮 <b>PvP Arena</b>\n\nChoose a free mini-game.\n\nNo Star betting and no cash wagering are used.",
            pvp_kb(),
        )
        return

    if data.startswith("game:"):
        await pvp_join(query, context, data.split(":", 1)[1])
        return

    if data == "leaderboard":
        top = rows("SELECT username, first_name, points FROM users WHERE banned=0 ORDER BY points DESC, user_id ASC LIMIT 10")
        lines = ["🏆 <b>Top Weley Players</b>\n"]
        if not top:
            lines.append("No players yet.")
        else:
            for i, player in enumerate(top, 1):
                name = "@" + html.escape(player["username"]) if player["username"] else html.escape(player["first_name"] or "User")
                lines.append(f"{i}. {name} — <b>{player['points']:,} WP</b>")
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home")]]))
        return


# =========================
# PAYMENTS
# =========================

async def issue_invoice(chat_id, stars, context):
    if stars < 100:
        await context.bot.send_message(chat_id, "❌ Minimum deposit is 100 Stars.")
        return
    points = stars * STARS_PER_POINT
    payload = f"wp:{chat_id}:{stars}:{uuid.uuid4().hex}"
    db(
        "INSERT INTO purchases(user_id,stars,points,payload,status,created_at) VALUES(?,?,?,?,?,?)",
        (chat_id, stars, points, payload, "pending", now()),
    )
    try:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=f"{points} Weley Points",
            description=f"Digital Weley Points package — {stars} Telegram Stars.",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(f"{points} Weley Points", stars)],
            provider_token="",
            start_parameter=f"wp-{uuid.uuid4().hex[:10]}",
        )
    except TelegramError as exc:
        db("UPDATE purchases SET status='failed' WHERE payload=?", (payload,))
        log.exception("Invoice failed: %s", exc)
        await context.bot.send_message(chat_id, "❌ Could not create the Stars invoice. Please try again.")


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    p = one("SELECT * FROM purchases WHERE payload=? AND status='pending'", (q.invoice_payload,))
    if not p:
        await q.answer(ok=False, error_message="Invoice is expired or unavailable.")
        return
    if q.from_user.id != p["user_id"] or q.total_amount != p["stars"]:
        await q.answer(ok=False, error_message="Invoice validation failed.")
        return
    await q.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    p = one("SELECT * FROM purchases WHERE payload=?", (payment.invoice_payload,))
    if not p:
        await update.message.reply_text("⚠️ Payment received but purchase record was not found. Contact admin.")
        return

    # Atomic claim prevents duplicate credit if Telegram retries an update.
    cur = db(
        "UPDATE purchases SET status='processing', charge_id=?, completed_at=? WHERE id=? AND status='pending'",
        (payment.telegram_payment_charge_id, now(), p["id"]),
    )
    if cur.rowcount != 1:
        return

    add_points(p["user_id"], p["points"], "stars_purchase", f"Purchase #{p['id']}")
    db("UPDATE purchases SET status='completed' WHERE id=?", (p["id"],))

    await update.message.reply_text(
        "✅ <b>Payment confirmed</b>\n\n"
        f"⭐ Paid: <b>{p['stars']} Stars</b>\n"
        f"🟣 Added: <b>+{p['points']} WP</b>\n\n"
        "Your Points are ready.",
        reply_markup=home_kb(),
        parse_mode=ParseMode.HTML,
    )


# =========================
# PVP
# =========================

async def pvp_join(query, context, game_key):
    if game_key not in GAMES:
        await query.answer("Game unavailable", show_alert=True)
        return

    uid = query.from_user.id
    async with PVP_LOCK:
        for queue in PVP_QUEUES.values():
            queue[:] = [item for item in queue if item["user_id"] != uid]

        if PVP_QUEUES[game_key]:
            opponent = PVP_QUEUES[game_key].pop(0)
        else:
            opponent = None
            PVP_QUEUES[game_key].append({"user_id": uid, "chat_id": query.message.chat_id, "joined_at": time.monotonic()})

    if opponent:
        await safe_edit(query, "⚔️ <b>Opponent found!</b>\n\nStarting the match…")
        await run_pvp(context, game_key, opponent["user_id"], uid)
        return

    await safe_edit(
        query,
        f"🔎 <b>Searching for opponent…</b>\n\n{GAMES[game_key][0]}\n⏱ {PVP_SEARCH_SECONDS}s\n\nFree match • no Star wager",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Leave Queue", callback_data=f"leaveq:{game_key}")]]),
    )
    asyncio.create_task(pvp_timeout(context, game_key, uid, query.message.chat_id))


async def leave_queue(query, game_key):
    uid = query.from_user.id
    async with PVP_LOCK:
        PVP_QUEUES[game_key][:] = [x for x in PVP_QUEUES[game_key] if x["user_id"] != uid]
    await safe_edit(query, "✅ <b>Queue cancelled.</b>", pvp_kb())


async def pvp_timeout(context, game_key, user_id, chat_id):
    await asyncio.sleep(PVP_SEARCH_SECONDS)
    async with PVP_LOCK:
        item = next((x for x in PVP_QUEUES[game_key] if x["user_id"] == user_id), None)
        if not item:
            return
        PVP_QUEUES[game_key].remove(item)
    try:
        await context.bot.send_message(chat_id, "⌛ <b>No opponent found.</b>\n\nNo Points were charged.", reply_markup=pvp_kb(), parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


async def run_pvp(context, game_key, user1, user2):
    name, emoji = GAMES[game_key]
    try:
        await context.bot.send_message(user1, f"🎮 <b>{name}</b>\n\nMatch starting…", parse_mode=ParseMode.HTML)
        await context.bot.send_message(user2, f"🎮 <b>{name}</b>\n\nMatch starting…", parse_mode=ParseMode.HTML)
        await asyncio.sleep(1)
        m1 = await context.bot.send_dice(user1, emoji=emoji)
        m2 = await context.bot.send_dice(user2, emoji=emoji)
        await asyncio.sleep(4)

        v1, v2 = m1.dice.value, m2.dice.value
        if v1 == v2:
            result = "draw"
            add_points(user1, PVP_PLAY_REWARD, "pvp_draw", name)
            add_points(user2, PVP_PLAY_REWARD, "pvp_draw", name)
            t1 = t2 = f"🤝 <b>Draw!</b>\n\n+{PVP_PLAY_REWARD} WP"
        else:
            winner = user1 if v1 > v2 else user2
            loser = user2 if winner == user1 else user1
            result = str(winner)
            add_points(winner, PVP_WIN_REWARD, "pvp_win", name)
            add_points(loser, PVP_PLAY_REWARD, "pvp_play", name)
            t1 = f"🏆 <b>You won!</b>\n\n+{PVP_WIN_REWARD} WP" if winner == user1 else f"💪 <b>Good game!</b>\n\n+{PVP_PLAY_REWARD} WP"
            t2 = f"🏆 <b>You won!</b>\n\n+{PVP_WIN_REWARD} WP" if winner == user2 else f"💪 <b>Good game!</b>\n\n+{PVP_PLAY_REWARD} WP"

        db(
            "INSERT INTO pvp_matches(game_key,user1_id,user2_id,result,created_at) VALUES(?,?,?,?,?)",
            (game_key, user1, user2, result, now()),
        )
        await context.bot.send_message(user1, t1, parse_mode=ParseMode.HTML)
        await context.bot.send_message(user2, t2, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        log.exception("PvP error: %s", exc)
        try:
            await context.bot.send_message(user1, "⚠️ Match interrupted. No wager was used; please try again.")
            await context.bot.send_message(user2, "⚠️ Match interrupted. No wager was used; please try again.")
        except TelegramError:
            pass


# =========================
# ADMIN PANEL
# =========================

async def render_admin_home(query):
    status = "STOPPED" if BOT_STOPPED else "RUNNING"
    await safe_edit(query, f"🛠 <b>Weley Admin Panel</b>\n\nStatus: <b>{status}</b>", admin_kb())


async def admin_callback(query, context, data):
    if not admin(query.from_user.id):
        await query.answer("Admin only", show_alert=True)
        return

    if data == "a_close":
        context.user_data.clear()
        await safe_edit(query, "✅ Admin panel closed.")
        return

    if data in {"a_refresh", "a_back"}:
        context.user_data.clear()
        await render_admin_home(query)
        return

    if data == "a_search":
        context.user_data.clear()
        context.user_data["admin_state"] = "search"
        await safe_edit(query, "🔎 <b>Search User</b>\n\nSend <code>@username</code> or numeric Telegram ID.\n\nUse /cancel to stop.")
        return

    if data == "a_broadcast":
        context.user_data.clear()
        context.user_data["admin_state"] = "broadcast"
        await safe_edit(query, "📢 <b>Broadcast</b>\n\nSend the message you want to broadcast.\n\nUse /cancel to stop.")
        return

    if data == "a_toggle":
        set_bot_stopped(not BOT_STOPPED)
        await render_admin_home(query)
        return

    if data == "a_stats":
        total = one("SELECT COUNT(*) c FROM users")["c"]
        active = one("SELECT COUNT(*) c FROM users WHERE banned=0 AND join_rewarded=1")["c"]
        banned = one("SELECT COUNT(*) c FROM users WHERE banned=1")["c"]
        points = one("SELECT COALESCE(SUM(points),0) s FROM users")["s"]
        paid = one("SELECT COALESCE(SUM(stars),0) s FROM purchases WHERE status='completed'")["s"]
        purchase_count = one("SELECT COUNT(*) c FROM purchases WHERE status='completed'")["c"]
        await safe_edit(
            query,
            "📊 <b>Weley Stats</b>\n\n"
            f"👥 Users: <b>{total}</b>\n"
            f"✅ Active: <b>{active}</b>\n"
            f"🚫 Banned: <b>{banned}</b>\n"
            f"🟣 Points in circulation: <b>{points:,} WP</b>\n"
            f"⭐ Stars paid: <b>{paid:,}</b>\n"
            f"🧾 Completed purchases: <b>{purchase_count}</b>\n"
            f"🛠 Status: <b>{'STOPPED' if BOT_STOPPED else 'RUNNING'}</b>",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Admin", callback_data="a_refresh")]]),
        )
        return

    if data == "a_requests":
        pending = rows("SELECT * FROM purchases WHERE status IN ('pending','processing') ORDER BY id DESC LIMIT 10")
        recent = rows("SELECT * FROM purchases WHERE status='completed' ORDER BY id DESC LIMIT 5")
        lines = ["📥 <b>Payment Requests</b>\n"]
        if pending:
            lines.append("<b>Pending / Processing</b>")
            for p in pending:
                stamp = html.escape(p["created_at"].replace("T", " ")[:19])
                lines.append(f"#{p['id']} • <code>{p['user_id']}</code> • ⭐ {p['stars']} → {p['points']} WP • <b>{p['status']}</b> • {stamp} UTC")
        else:
            lines.append("No pending requests.")
        lines.append("\n<b>Recent Completed</b>")
        if recent:
            for p in recent:
                lines.append(f"#{p['id']} • <code>{p['user_id']}</code> • ⭐ {p['stars']} → {p['points']} WP • ✅ completed")
        else:
            lines.append("None yet.")
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="a_requests")], [InlineKeyboardButton("⬅️ Admin", callback_data="a_refresh")]]))
        return

    if data.startswith("u_add:"):
        _, uid, delta = data.split(":")
        uid = int(uid)
        delta = int(delta)
        target = user_row(uid)
        if not target:
            await query.answer("User not found", show_alert=True)
            return
        new_value = max(0, target["points"] + delta)
        set_points(uid, new_value, "Admin quick adjustment")
        await show_admin_user(query, uid)
        return

    if data.startswith("u_set:"):
        uid = int(data.split(":", 1)[1])
        if not user_row(uid):
            await query.answer("User not found", show_alert=True)
            return
        context.user_data.clear()
        context.user_data["admin_state"] = "set_balance"
        context.user_data["selected_user"] = uid
        await safe_edit(query, f"✏️ <b>Set WP Balance</b>\n\nSend the new whole-number balance for <code>{uid}</code>.\n\nUse /cancel to stop.")
        return

    if data.startswith("u_toggleban:"):
        uid = int(data.split(":", 1)[1])
        target = user_row(uid)
        if not target:
            await query.answer("User not found", show_alert=True)
            return
        new_state = 0 if target["banned"] else 1
        db("UPDATE users SET banned=? WHERE user_id=?", (new_state, uid))
        await show_admin_user(query, uid)
        return

    if data.startswith("u_history:"):
        uid = int(data.split(":", 1)[1])
        target = user_row(uid)
        if not target:
            await query.answer("User not found", show_alert=True)
            return
        history = rows("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 12", (uid,))
        lines = [f"📜 <b>History — {fmt_name(target)}</b>\n"]
        if not history:
            lines.append("No balance transactions yet.")
        else:
            for h in history:
                stamp = html.escape(h["created_at"].replace("T", " ")[:19])
                lines.append(f"• <b>{h['amount']:+,} WP</b> • {html.escape(h['kind'])} • {html.escape(h['note'] or '')} • {stamp} UTC")
        await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ User", callback_data=f"u_view:{uid}")]]))
        return

    if data.startswith("u_view:"):
        await show_admin_user(query, int(data.split(":", 1)[1]))
        return


async def show_admin_user(query, uid):
    row = user_row(uid)
    if not row:
        await safe_edit(query, "❌ User not found.", admin_kb())
        return
    await safe_edit(
        query,
        "🔎 <b>User Details</b>\n\n"
        f"👤 {fmt_name(row)}\n"
        f"🪪 ID: <code>{row['user_id']}</code>\n"
        f"🟣 Balance: <b>{row['points']:,} WP</b>\n"
        f"👥 Referrals: <b>{row['referral_count']}</b>\n"
        f"🔗 Referrer: <code>{row['referrer_id'] or '-'}</code>\n"
        f"✅ Activated: <b>{'Yes' if row['join_rewarded'] else 'No'}</b>\n"
        f"🚫 Banned: <b>{'Yes' if row['banned'] else 'No'}</b>\n"
        f"📅 Created: <code>{html.escape(row['created_at'] or '-')}</code>",
        user_admin_kb(uid, bool(row["banned"])),
    )


async def admin_text(update, context):
    user = update.effective_user
    if not user or not update.message or not admin(user.id):
        return False

    state = context.user_data.get("admin_state")
    if not state:
        return False

    text = update.message.text.strip()

    if state == "search":
        context.user_data.clear()
        if text.isdigit():
            row = user_row(int(text))
        else:
            username = text.lstrip("@").lower()
            row = one("SELECT * FROM users WHERE LOWER(username)=? LIMIT 1", (username,))
        if not row:
            await update.message.reply_text("❌ User not found in Weley.", reply_markup=admin_kb())
            return True
        context.user_data["selected_user"] = row["user_id"]
        await update.message.reply_text(
            "🔎 <b>User found</b>",
            reply_markup=user_admin_kb(row["user_id"], bool(row["banned"])),
            parse_mode=ParseMode.HTML,
        )
        # Send details separately so the admin gets a normal readable message.
        await update.message.reply_text(
            "👤 <b>Details</b>\n\n"
            f"{fmt_name(row)}\n"
            f"🪪 <code>{row['user_id']}</code>\n"
            f"🟣 <b>{row['points']:,} WP</b>\n"
            f"👥 Referrals: <b>{row['referral_count']}</b>\n"
            f"🔗 Referrer: <code>{row['referrer_id'] or '-'}</code>\n"
            f"✅ Active: <b>{'Yes' if row['join_rewarded'] else 'No'}</b>\n"
            f"🚫 Banned: <b>{'Yes' if row['banned'] else 'No'}</b>",
            parse_mode=ParseMode.HTML,
        )
        return True

    if state == "set_balance":
        uid = context.user_data.get("selected_user")
        context.user_data.clear()
        try:
            amount = int(text)
        except ValueError:
            await update.message.reply_text("❌ Send a whole number only.", reply_markup=admin_kb())
            return True
        if amount < 0:
            await update.message.reply_text("❌ Balance cannot be negative.", reply_markup=admin_kb())
            return True
        if not uid or not user_row(uid):
            await update.message.reply_text("❌ User no longer exists.", reply_markup=admin_kb())
            return True
        set_points(uid, amount, "Admin custom balance")
        await update.message.reply_text(f"✅ Balance updated to <b>{amount:,} WP</b>.", reply_markup=admin_kb(), parse_mode=ParseMode.HTML)
        return True

    if state == "broadcast":
        context.user_data.clear()
        targets = rows("SELECT user_id FROM users WHERE banned=0 ORDER BY user_id")
        sent = failed = 0
        for target in targets:
            try:
                await context.bot.send_message(target["user_id"], text)
                sent += 1
            except (TelegramError, Forbidden, BadRequest):
                failed += 1
            await asyncio.sleep(0.05)
        await update.message.reply_text(f"📢 <b>Broadcast complete</b>\n\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>", reply_markup=admin_kb(), parse_mode=ParseMode.HTML)
        return True

    return False


# =========================
# GENERAL TEXT
# =========================

async def text_router(update, context):
    user = update.effective_user
    if not user or not update.message:
        return
    ensure_user(user)

    # Admin workflows first.
    if admin(user.id) and await admin_text(update, context):
        return

    if context.user_data.get("custom_stars"):
        raw = update.message.text.strip()
        try:
            stars = int(raw)
        except ValueError:
            await update.message.reply_text("❌ Enter a whole-number Stars amount.")
            return
        if stars < CUSTOM_MIN_STARS:
            await update.message.reply_text(f"❌ Custom amount must be at least {CUSTOM_MIN_STARS} Stars.")
            return
        context.user_data.pop("custom_stars", None)
        await issue_invoice(update.effective_chat.id, stars, context)
        return

    row = user_row(user.id)
    if row["banned"] and not admin(user.id):
        await update.message.reply_text("🚫 Your Weley account is restricted.")
        return
    if BOT_STOPPED and not admin(user.id):
        await update.message.reply_text("🛠 Weley is temporarily paused.")
        return
    await update.message.reply_text("Use the buttons below 👇", reply_markup=home_kb())


# =========================
# CALLBACK DISPATCHER
# =========================

async def callback_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    ensure_user(user)
    data = q.data or ""

    # Admin callbacks have priority and are admin-only.
    if data.startswith(("a_", "u_")):
        if not admin(user.id):
            await q.answer("Admin only", show_alert=True)
            return
        await admin_callback(q, context, data)
        return

    # Queue leave works even during maintenance so a stale queue can be cleared.
    if data.startswith("leaveq:"):
        await leave_queue(q, data.split(":", 1)[1])
        return

    row = user_row(user.id)
    if row["banned"] and not admin(user.id):
        await safe_edit(q, "🚫 <b>Access restricted</b>")
        return

    if BOT_STOPPED and user.id != ADMIN_ID and data != "verify":
        await safe_edit(q, "🛠 <b>Weley is temporarily paused</b>")
        return

    await user_callback(q, context, data)


# =========================
# MAIN
# =========================

def main():
    init_db()
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_NEW_BOT_TOKEN_HERE":
        raise RuntimeError("Put your new BotFather token into BOT_TOKEN near the top of this file.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(callback_dispatch))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Weley started | admin=%s | required_group=%s", ADMIN_ID, REQUIRED_GROUP_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
