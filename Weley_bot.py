import asyncio
import html
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "weley.db")
REFERRAL_REWARD_HALF_STARS = 5  # 2.5 Stars = 5 half-stars
WITHDRAW_OPTIONS = (15, 25, 50, 100)

# =========================
# REQUIRED CHANNELS / GROUPS
# =========================
# Replace these examples with your real required chats.
# chat_id can be a public @username or a numeric ID such as -1001234567890.
# The bot MUST be an administrator in every required chat so Telegram can
# reliably verify a user's membership with getChatMember.
REQUIRED_CHATS = [
    # {"chat_id": "@your_channel", "title": "Main Channel", "join_url": "https://t.me/your_channel"},
    # {"chat_id": -1001234567890, "title": "Main Group", "join_url": "https://t.me/+YOURINVITELINK"},
]
POLL_TIMEOUT = 30
API_TIMEOUT = 45

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in the environment or .env file.")
if not REQUIRED_CHATS:
    raise RuntimeError("Configure REQUIRED_CHATS in bot.py before starting Weley.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("weley")


class TelegramAPIError(Exception):
    def __init__(self, method: str, error_code: int, description: str):
        self.method = method
        self.error_code = error_code
        self.description = description
        super().__init__(f"{method}: {error_code} {description}")


class TelegramNetworkError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client: Optional[httpx.AsyncClient] = None

    async def start(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT))

    async def close(self):
        if self.client:
            await self.client.aclose()

    async def call(self, method: str, data: Optional[dict[str, Any]] = None) -> Any:
        assert self.client is not None
        try:
            response = await self.client.post(f"{self.base_url}/{method}", json=data or {})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramNetworkError(str(exc)) from exc

        if not payload.get("ok"):
            raise TelegramAPIError(
                method,
                int(payload.get("error_code", 0)),
                payload.get("description", "Unknown Telegram API error"),
            )
        return payload.get("result")

    async def get_me(self):
        return await self.call("getMe")

    async def delete_webhook(self):
        return await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def set_my_commands(self):
        commands = [
            {"command": "start", "description": "Start / unlock Weley"},
        ]
        return await self.call("setMyCommands", {"commands": commands})

    async def get_updates(self, offset: int, timeout: int = POLL_TIMEOUT):
        return await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
        )

    async def send_message(self, chat_id: int | str, text: str, reply_markup=None, parse_mode="HTML"):
        data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        return await self.call("sendMessage", data)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode="HTML"):
        data = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        return await self.call("editMessageText", data)

    async def answer_callback_query(self, callback_id: str, text: Optional[str] = None, show_alert=False):
        data = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            data["text"] = text
        return await self.call("answerCallbackQuery", data)

    async def get_chat_member(self, chat_id: int | str, user_id: int):
        return await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def get_chat(self, chat_id: int | str):
        return await self.call("getChat", {"chat_id": chat_id})

    async def get_available_gifts(self):
        return await self.call("getAvailableGifts")

    async def get_my_star_balance(self):
        return await self.call("getMyStarBalance")

    async def send_gift(self, user_id: int, gift_id: str, text: str):
        return await self.call(
            "sendGift",
            {"user_id": user_id, "gift_id": gift_id, "text": text},
        )


@dataclass(frozen=True)
class RequiredChat:
    chat_id: str
    title: str
    join_url: str


class Database:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self):
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    username TEXT,
                    referred_by INTEGER,
                    verified_once INTEGER NOT NULL DEFAULT 0,
                    referral_count INTEGER NOT NULL DEFAULT 0,
                    balance_half_stars INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    referred_user_id INTEGER PRIMARY KEY,
                    referrer_user_id INTEGER NOT NULL,
                    reward_half_stars INTEGER NOT NULL,
                    activated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS withdrawals (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    requested_stars INTEGER NOT NULL,
                    gift_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_users_referral_count
                    ON users(referral_count DESC);
                CREATE INDEX IF NOT EXISTS idx_withdrawals_user_status
                    ON withdrawals(user_id, status);
                """
            )

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def upsert_user(self, user: dict[str, Any]):
        now = self.now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO users (
                    user_id, first_name, last_name, username, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    username = excluded.username,
                    updated_at = excluded.updated_at
                """,
                (
                    user["id"],
                    user.get("first_name", "") or "",
                    user.get("last_name", "") or "",
                    user.get("username"),
                    now,
                    now,
                ),
            )

    def set_referrer_if_empty(self, user_id: int, referrer_id: int) -> bool:
        if user_id == referrer_id:
            return False
        with self.connect() as db:
            cur = db.execute(
                "UPDATE users SET referred_by=?, updated_at=? WHERE user_id=? AND referred_by IS NULL",
                (referrer_id, self.now(), user_id),
            )
            return cur.rowcount == 1

    def activate_user_once(self, user_id: int) -> Optional[dict[str, Any]]:
        """Mark a user as verified once and atomically award the referring user exactly once."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT verified_once, referred_by FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row or row["verified_once"]:
                db.commit()
                return None

            db.execute(
                "UPDATE users SET verified_once=1, updated_at=? WHERE user_id=?",
                (self.now(), user_id),
            )

            referrer_id = row["referred_by"]
            reward = None
            if referrer_id:
                inserted = db.execute(
                    """
                    INSERT OR IGNORE INTO referrals
                        (referred_user_id, referrer_user_id, reward_half_stars, activated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, referrer_id, REFERRAL_REWARD_HALF_STARS, self.now()),
                )
                if inserted.rowcount == 1:
                    db.execute(
                        """
                        UPDATE users
                        SET referral_count = referral_count + 1,
                            balance_half_stars = balance_half_stars + ?,
                            updated_at = ?
                        WHERE user_id=?
                        """,
                        (REFERRAL_REWARD_HALF_STARS, self.now(), referrer_id),
                    )
                    reward = db.execute(
                        "SELECT user_id, first_name, username, balance_half_stars, referral_count FROM users WHERE user_id=?",
                        (referrer_id,),
                    ).fetchone()
            db.commit()
            if reward:
                return dict(reward)
            return None

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as db:
            return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def balance_half_stars(self, user_id: int) -> int:
        row = self.get_user(user_id)
        return int(row["balance_half_stars"]) if row else 0

    def required_chats(self) -> list[RequiredChat]:
        return [
            RequiredChat(str(x["chat_id"]), str(x["title"]), str(x["join_url"]))
            for x in REQUIRED_CHATS
        ]

    def top_users(self, limit: int = 10):
        with self.connect() as db:
            return db.execute(
                """
                SELECT user_id, first_name, last_name, username, referral_count, balance_half_stars
                FROM users
                ORDER BY referral_count DESC, balance_half_stars DESC, user_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def rank_of(self, user_id: int) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT referral_count, balance_half_stars FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if not row:
                return 0
            higher = db.execute(
                """
                SELECT COUNT(*) AS n
                FROM users
                WHERE referral_count > ?
                   OR (referral_count = ? AND balance_half_stars > ?)
                   OR (referral_count = ? AND balance_half_stars = ? AND user_id < ?)
                """,
                (
                    row["referral_count"],
                    row["referral_count"],
                    row["balance_half_stars"],
                    row["referral_count"],
                    row["balance_half_stars"],
                    user_id,
                ),
            ).fetchone()["n"]
            return int(higher) + 1

    def reserve_withdrawal(self, user_id: int, stars: int, gift_id: str) -> Optional[str]:
        amount = stars * 2
        withdrawal_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT 1 FROM withdrawals WHERE user_id=? AND status='pending' LIMIT 1",
                (user_id,),
            ).fetchone()
            if pending:
                db.rollback()
                return None
            cur = db.execute(
                """
                UPDATE users
                SET balance_half_stars = balance_half_stars - ?, updated_at=?
                WHERE user_id=? AND balance_half_stars >= ?
                """,
                (amount, self.now(), user_id, amount),
            )
            if cur.rowcount != 1:
                db.rollback()
                return None
            db.execute(
                """
                INSERT INTO withdrawals(id, user_id, requested_stars, gift_id, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (withdrawal_id, user_id, stars, gift_id, self.now()),
            )
            db.commit()
        return withdrawal_id

    def complete_withdrawal(self, withdrawal_id: str):
        with self.connect() as db:
            db.execute(
                "UPDATE withdrawals SET status='completed', completed_at=?, error=NULL WHERE id=? AND status='pending'",
                (self.now(), withdrawal_id),
            )

    def fail_withdrawal(self, withdrawal_id: str, error: str, refund: bool = True):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT user_id, requested_stars, status FROM withdrawals WHERE id=?",
                (withdrawal_id,),
            ).fetchone()
            if not row or row["status"] != "pending":
                db.rollback()
                return
            if refund:
                db.execute(
                    "UPDATE users SET balance_half_stars = balance_half_stars + ?, updated_at=? WHERE user_id=?",
                    (int(row["requested_stars"]) * 2, self.now(), row["user_id"]),
                )
            db.execute(
                "UPDATE withdrawals SET status='failed', error=?, completed_at=? WHERE id=?",
                (error[:500], self.now(), withdrawal_id),
            )
            db.commit()



def escape(value: Any) -> str:
    return html.escape(str(value or ""))


def display_name(row_or_user: Any) -> str:
    first = (row_or_user["first_name"] if isinstance(row_or_user, sqlite3.Row) else row_or_user.get("first_name", "")) or ""
    last = (row_or_user["last_name"] if isinstance(row_or_user, sqlite3.Row) else row_or_user.get("last_name", "")) or ""
    username = row_or_user["username"] if isinstance(row_or_user, sqlite3.Row) else row_or_user.get("username")
    name = (f"{first} {last}").strip()
    return name or (f"@{username}" if username else "User")


def format_stars(half_stars: int) -> str:
    if half_stars % 2 == 0:
        return str(half_stars // 2)
    return f"{half_stars / 2:.1f}"


def main_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "👤 Profile", "callback_data": "profile"},
                {"text": "🔗 Refer", "callback_data": "refer"},
            ],
            [
                {"text": "💸 Withdraw", "callback_data": "withdraw"},
                {"text": "🏆 Leaderboard", "callback_data": "leaderboard"},
            ],
            [{"text": "⭐ Balance", "callback_data": "balance"}],
        ]
    }


def back_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "home"}]]}


def gate_keyboard(chats: list[RequiredChat]):
    buttons = []
    for chat in chats:
        buttons.append([{"text": f"🔗 Join {chat.title}", "url": chat.join_url}])
    buttons.append([{"text": "✅ Joined — Verify", "callback_data": "verify"}])
    return {"inline_keyboard": buttons}


class WeleyBot:
    def __init__(self):
        self.api = TelegramAPI(BOT_TOKEN)
        self.db = Database(DB_PATH)
        self.bot_id: Optional[int] = None
        self.bot_username: Optional[str] = None
        self.gift_cache: list[dict[str, Any]] = []
        self.gift_cache_until = 0.0
        self.running = True

    async def startup(self):
        await self.api.start()
        await self.api.delete_webhook()
        me = await self.api.get_me()
        self.bot_id = int(me["id"])
        self.bot_username = me["username"]
        await self.api.set_my_commands()
        log.info("Logged in as @%s (%s)", self.bot_username, self.bot_id)
        if not REQUIRED_CHATS:
            log.warning("No mandatory chats are configured in REQUIRED_CHATS.")

    async def shutdown(self):
        self.running = False
        await self.api.close()

    async def process_update(self, update: dict[str, Any]):
        try:
            if "callback_query" in update:
                await self.handle_callback(update["callback_query"])
                return
            if "message" in update:
                await self.handle_message(update["message"])
                return
        except Exception:
            log.exception("Unhandled update error")

    async def handle_message(self, message: dict[str, Any]):
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return

        user = message.get("from")
        if not user:
            return
        self.db.upsert_user(user)
        text = (message.get("text") or "").strip()

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[1].startswith("ref_"):
                raw_ref = parts[1][4:].strip()
                if raw_ref.isdigit():
                    referrer_id = int(raw_ref)
                    if self.db.get_user(referrer_id):
                        self.db.set_referrer_if_empty(user["id"], referrer_id)
            await self.show_entry_gate_or_home(message["chat"]["id"], user["id"])
            return

        await self.api.send_message(
            message["chat"]["id"],
            "Use /start to open <b>Weley</b>.",
        )

    async def is_member(self, user_id: int, required: RequiredChat):
        try:
            member = await self.api.get_chat_member(required.chat_id, user_id)
        except (TelegramAPIError, TelegramNetworkError) as exc:
            log.error("Membership check failed for %s: %s", required.chat_id, exc)
            return None
        status = member.get("status")
        if status in {"creator", "administrator", "member"}:
            return True
        if status == "restricted":
            return bool(member.get("is_member"))
        return False

    async def check_requirements(self, user_id: int):
        chats = self.db.required_chats()
        missing: list[RequiredChat] = []
        config_error: list[RequiredChat] = []
        for chat in chats:
            result = await self.is_member(user_id, chat)
            if result is None:
                config_error.append(chat)
            elif not result:
                missing.append(chat)
        return chats, missing, config_error

    async def show_gate_or_home(self, chat_id: int, user_id: int, message_id: Optional[int] = None):
        chats, missing, config_error = await self.check_requirements(user_id)
        if not chats:
            text = (
                "⚙️ <b>Weley setup is incomplete.</b>\n\n"
                "The bot owner has not configured any mandatory channel/group yet."
            )
            await self._send_or_edit(chat_id, message_id, text, main_keyboard())
            return
        if config_error:
            text = (
                "⚠️ <b>Verification setup error</b>\n\n"
                "The bot cannot verify one or more required chats. "
                "The owner must make the bot an administrator there."
            )
            await self._send_or_edit(chat_id, message_id, text, gate_keyboard(chats))
            return
        if missing:
            lines = [
                "🔒 <b>Weley Access Locked</b>",
                "\nJoin every required channel/group, then press <b>✅ Joined — Verify</b>.",
                "",
            ]
            for chat in chats:
                if chat in missing:
                    lines.append(f"❌ {escape(chat.title)}")
                else:
                    lines.append(f"✅ {escape(chat.title)}")
            await self._send_or_edit(chat_id, message_id, "\n".join(lines), gate_keyboard(chats))
            return
        reward = self.db.activate_user_once(user_id)
        if reward:
            referred = self.db.get_user(user_id)
            referred_name = display_name(referred) if referred else "a user"
            await self.api.send_message(
                reward["user_id"],
                f"🎉 <b>Congratulations!</b>\n\n"
                f"<b>{escape(referred_name)}</b> joined all required chats and became your active referral.\n"
                f"Reward: <b>{format_stars(REFERRAL_REWARD_HALF_STARS)} ⭐</b>\n"
                f"Your balance: <b>{format_stars(reward['balance_half_stars'])} ⭐</b>",
            )
        await self._send_or_edit(
            chat_id,
            message_id,
            self.welcome_text(user_id),
            main_keyboard(),
        )

    def welcome_text(self, user_id: int) -> str:
        row = self.db.get_user(user_id)
        return (
            "🎉 <b>Welcome to Weley!</b>\n\n"
            "Your account is now active.\n\n"
            "Invite friends, earn <b>2.5 ⭐</b> for every active referral, "
            "and use your reward balance for Telegram Gift withdrawals.\n\n"
            "Choose an option below 👇"
        )

    async def _send_or_edit(self, chat_id: int, message_id: Optional[int], text: str, markup):
        if message_id is None:
            await self.api.send_message(chat_id, text, markup)
            return
        try:
            await self.api.edit_message_text(chat_id, message_id, text, markup)
        except TelegramAPIError as exc:
            # Ignore the common "message is not modified" error.
            if "message is not modified" not in exc.description.lower():
                await self.api.send_message(chat_id, text, markup)

    async def handle_callback(self, cb: dict[str, Any]):
        user = cb.get("from")
        message = cb.get("message")
        if not user or not message:
            return
        user_id = int(user["id"])
        self.db.upsert_user(user)
        await self.api.answer_callback_query(cb["id"])

        data = cb.get("data", "")
        chat_id = int(message["chat"]["id"])
        message_id = int(message["message_id"])

        if data == "verify":
            await self.show_entry_gate_or_home(chat_id, user_id, message_id)
            return

        chats, missing, config_error = await self.check_requirements(user_id)
        if not chats or missing or config_error:
            await self.show_gate_or_home(chat_id, user_id, message_id)
            return

        if data == "home":
            await self._send_or_edit(chat_id, message_id, self.welcome_text(user_id), main_keyboard())
        elif data == "profile":
            await self.profile(chat_id, message_id, user_id)
        elif data == "refer":
            await self.refer(chat_id, message_id, user_id)
        elif data == "balance":
            await self.balance(chat_id, message_id, user_id)
        elif data == "leaderboard":
            await self.leaderboard(chat_id, message_id, user_id)
        elif data == "withdraw":
            await self.withdraw_menu(chat_id, message_id, user_id)
        elif data.startswith("withdraw:"):
            try:
                stars = int(data.split(":", 1)[1])
            except ValueError:
                return
            await self.do_withdraw(chat_id, message_id, user_id, stars)

    async def show_entry_gate_or_home(self, chat_id: int, user_id: int, message_id: Optional[int] = None):
        await self.show_gate_or_home(chat_id, user_id, message_id)

    async def profile(self, chat_id: int, message_id: int, user_id: int):
        row = self.db.get_user(user_id)
        name = escape(display_name(row)) if row else "User"
        username = f"@{escape(row['username'])}" if row and row["username"] else "Not set"
        rank = self.db.rank_of(user_id)
        text = (
            "👤 <b>Profile</b>\n\n"
            f"Name: <b>{name}</b>\n"
            f"Username: <b>{username}</b>\n"
            f"User ID: <code>{user_id}</code>\n\n"
            f"👥 Active referrals: <b>{row['referral_count'] if row else 0}</b>\n"
            f"⭐ Reward balance: <b>{format_stars(row['balance_half_stars'] if row else 0)} Stars</b>\n"
            f"🏆 Rank: <b>#{rank}</b>"
        )
        await self.api.edit_message_text(chat_id, message_id, text, back_keyboard())

    async def refer(self, chat_id: int, message_id: int, user_id: int):
        link = f"https://t.me/{self.bot_username}?start=ref_{user_id}"
        row = self.db.get_user(user_id)
        text = (
            "🔗 <b>Weley Referral</b>\n\n"
            "Share your personal link. A referral becomes <b>active</b> only when the new user joins "
            "all required chats and successfully verifies access in Weley.\n\n"
            f"🎁 Reward per active referral: <b>{format_stars(REFERRAL_REWARD_HALF_STARS)} ⭐</b>\n"
            f"👥 Your active referrals: <b>{row['referral_count'] if row else 0}</b>\n\n"
            f"<code>{link}</code>"
        )
        markup = {
            "inline_keyboard": [
                [{"text": "📤 Share Referral Link", "url": f"https://t.me/share/url?url={link}"}],
                [{"text": "⬅️ Back", "callback_data": "home"}],
            ]
        }
        await self.api.edit_message_text(chat_id, message_id, text, markup)

    async def balance(self, chat_id: int, message_id: int, user_id: int):
        row = self.db.get_user(user_id)
        balance = format_stars(row["balance_half_stars"] if row else 0)
        text = (
            "⭐ <b>Balance</b>\n\n"
            f"Available reward balance: <b>{balance} Telegram Stars</b>\n\n"
            "This is Weley's internal reward balance. Withdrawal sends a Telegram Gift from the bot's "
            "own Stars balance; it is not a direct transfer of the user's personal Telegram Stars."
        )
        await self.api.edit_message_text(chat_id, message_id, text, back_keyboard())

    async def leaderboard(self, chat_id: int, message_id: int, user_id: int):
        rows = self.db.top_users(10)
        rank = self.db.rank_of(user_id)
        if not rows:
            body = "No referral data yet."
        else:
            body_lines = []
            for index, row in enumerate(rows, 1):
                medal = ["🥇", "🥈", "🥉"][index - 1] if index <= 3 else f"{index}."
                body_lines.append(
                    f"{medal} {escape(display_name(row))} — "
                    f"<b>{row['referral_count']}</b> referrals"
                )
            body = "\n".join(body_lines)
        text = f"🏆 <b>Live Leaderboard</b>\n\n{body}\n\nYour current rank: <b>#{rank}</b>"
        await self.api.edit_message_text(chat_id, message_id, text, back_keyboard())

    async def get_gifts(self, force=False):
        if not force and time.time() < self.gift_cache_until:
            return self.gift_cache
        result = await self.api.get_available_gifts()
        gifts = result.get("gifts", []) if result else []
        usable = []
        for gift in gifts:
            remaining = gift.get("remaining_count")
            personal_remaining = gift.get("personal_remaining_count")
            if remaining is not None and int(remaining) <= 0:
                continue
            if personal_remaining is not None and int(personal_remaining) <= 0:
                continue
            usable.append(gift)
        self.gift_cache = usable
        self.gift_cache_until = time.time() + 60
        return self.gift_cache

    async def withdraw_menu(self, chat_id: int, message_id: int, user_id: int):
        gifts = await self.get_gifts(force=True)
        available_prices = {int(g["star_count"]): g for g in gifts}
        buttons = []
        unavailable = []
        for amount in WITHDRAW_OPTIONS:
            gift = available_prices.get(amount)
            if gift:
                buttons.append([{"text": f"🎁 {amount} Stars Gift", "callback_data": f"withdraw:{amount}"}])
            else:
                unavailable.append(amount)
        row = self.db.get_user(user_id)
        balance = row["balance_half_stars"] if row else 0
        note = ""
        if unavailable:
            note = "\n\nUnavailable right now from Telegram's live gift catalog: " + ", ".join(map(str, unavailable)) + " Stars."
        text = (
            "💸 <b>Withdraw</b>\n\n"
            f"Your balance: <b>{format_stars(balance)} ⭐</b>\n\n"
            "Choose a gift amount. Weley checks Telegram's current gift catalog before processing." + note
        )
        buttons.append([{"text": "⬅️ Back", "callback_data": "home"}])
        await self.api.edit_message_text(chat_id, message_id, text, {"inline_keyboard": buttons})

    async def do_withdraw(self, chat_id: int, message_id: int, user_id: int, stars: int):
        if stars not in WITHDRAW_OPTIONS:
            return
        gifts = await self.get_gifts(force=True)
        gift = next((g for g in gifts if int(g["star_count"]) == stars), None)
        if not gift:
            await self.api.edit_message_text(
                chat_id,
                message_id,
                f"⚠️ <b>{stars} Stars Gift</b> is not currently available in Telegram's gift catalog.\n\nPlease choose another option.",
                back_keyboard(),
            )
            return

        # Check the bot's real Stars balance before reserving the user's internal reward.
        try:
            bot_balance = await self.api.get_my_star_balance()
            if int(bot_balance.get("amount", 0)) < stars:
                await self.api.edit_message_text(
                    chat_id,
                    message_id,
                    "⚠️ <b>Withdrawal temporarily unavailable.</b>\n\nThe bot does not currently have enough Telegram Stars to send this gift. Your balance was not changed.",
                    back_keyboard(),
                )
                return
        except (TelegramAPIError, TelegramNetworkError) as exc:
            log.error("Cannot read bot Stars balance: %s", exc)
            await self.api.edit_message_text(
                chat_id,
                message_id,
                "⚠️ Telegram Stars balance could not be checked right now. Please try again later.",
                back_keyboard(),
            )
            return

        withdrawal_id = self.db.reserve_withdrawal(user_id, stars, gift["id"])
        if not withdrawal_id:
            row = self.db.get_user(user_id)
            current = format_stars(row["balance_half_stars"] if row else 0)
            await self.api.edit_message_text(
                chat_id,
                message_id,
                f"❌ <b>Withdrawal unavailable.</b>\n\nYou may already have a pending withdrawal, or your balance is below {stars} Stars.\nCurrent balance: <b>{current} ⭐</b>",
                back_keyboard(),
            )
            return

        await self.api.edit_message_text(
            chat_id,
            message_id,
            f"⏳ <b>Processing {stars} Stars Gift...</b>\n\nYour reward balance has been safely reserved.",
            back_keyboard(),
        )

        try:
            await self.api.send_gift(
                user_id,
                gift["id"],
                f"Weley referral reward — {stars} Stars gift",
            )
        except TelegramAPIError as exc:
            # Explicit Telegram rejection: refund immediately.
            self.db.fail_withdrawal(withdrawal_id, str(exc), refund=True)
            await self.api.edit_message_text(
                chat_id,
                message_id,
                "❌ <b>Gift delivery failed.</b>\n\nTelegram rejected the gift request, so your reward balance has been refunded.",
                back_keyboard(),
            )
            return
        except TelegramNetworkError as exc:
            # Ambiguous network outcome: keep it pending to avoid double-paying after a timeout.
            log.error("Ambiguous gift delivery for %s: %s", withdrawal_id, exc)
            await self.api.edit_message_text(
                chat_id,
                message_id,
                "⏳ <b>Gift delivery is being verified.</b>\n\nTelegram did not return a definitive result. Your balance remains reserved to prevent duplicate payout. An admin can review this pending withdrawal.",
                back_keyboard(),
            )
            return

        self.db.complete_withdrawal(withdrawal_id)
        await self.api.edit_message_text(
            chat_id,
            message_id,
            f"🎉 <b>Withdrawal successful!</b>\n\nYour {stars} Stars Telegram Gift has been sent successfully.\n\nWithdrawal ID: <code>{withdrawal_id[:12]}</code>",
            back_keyboard(),
        )

    async def run(self):
        await self.startup()
        offset = 0
        try:
            while self.running:
                try:
                    updates = await self.api.get_updates(offset, POLL_TIMEOUT)
                    for update in updates:
                        offset = max(offset, int(update["update_id"]) + 1)
                        await self.process_update(update)
                except TelegramNetworkError as exc:
                    log.error("Network error: %s", exc)
                    await asyncio.sleep(2)
                except TelegramAPIError as exc:
                    log.error("Telegram API error: %s", exc)
                    await asyncio.sleep(2)
        finally:
            await self.shutdown()


async def main():
    bot = WeleyBot()
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
