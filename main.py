# main.py
# Telegram Football Sign-Up Bot
# - 15 slots + waitlist (5) + VIP auto-reservations
# - Inline buttons: Join / Leave / List
# - Admin commands: /newgame, /lock, /unlock, /reset
# - Persistence: Pickle file (no database needed)
# - Python 3.10+, python-telegram-bot==20.8

from __future__ import annotations

import os
import html
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

from dateutil import parser as dtparser
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
    ChatMemberAdministrator,
    ChatMemberOwner,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

# -----------------------------
# CONFIG
# -----------------------------
DEFAULT_CAPACITY = 15
WAITLIST_CAPACITY = 5  # <= your request

# VIPs: always included when a new game is created
# IMPORTANT: These are display names shown in Telegram.
# Make sure they match how the names appear in Telegram (e.g., "Albert Tan" if that’s the display)
VIP_NAMES = ["Albert", "Ah Soon"]

# If you later want to bind VIPs to real Telegram accounts, you can extend each row with a user_id.


# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("football-bot")


# -----------------------------
# Utilities
# -----------------------------
def is_admin(member: ChatMember) -> bool:
    return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))


async def user_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return is_admin(member)
    except Exception as e:
        logger.warning("Admin check failed: %s", e)
        return False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_keyboard(locked: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="✅ Join", callback_data="join")],
        [InlineKeyboardButton(text="🚪 Leave", callback_data="leave")],
        [InlineKeyboardButton(text="📋 List", callback_data="list")],
    ]
    if locked:
        buttons[0][0] = InlineKeyboardButton(text="🔒 Locked", callback_data="noop")
    return InlineKeyboardMarkup(buttons)


def ensure_event(chat_data: Dict[str, Any]) -> Dict[str, Any]:
    if "event" not in chat_data or not chat_data.get("event"):
        chat_data["event"] = {
            "title": None,  # e.g., "Sat 5 Oct, 7pm"
            "capacity": DEFAULT_CAPACITY,
            "locked": False,
            "players": [],   # list of dicts: {user_id, name, joined_at, is_vip}
            "waitlist": [],  # same structure
            "created_at": now_iso(),
        }
    return chat_data["event"]


def find_user(lst: List[Dict[str, Any]], user_id: int) -> int:
    for i, row in enumerate(lst):
        if row.get("user_id") == user_id:
            return i
    return -1


def format_roster(event: Dict[str, Any]) -> str:
    title = event.get("title") or "Upcoming Game"
    cap = event.get("capacity", DEFAULT_CAPACITY)
    players = event.get("players", [])
    wait = event.get("waitlist", [])
    wait_cap = WAITLIST_CAPACITY

    lines = [
        f"<b>⚽ {html.escape(title)}</b>",
        f"Slots: <b>{len(players)}/{cap}</b>  •  Waitlist: <b>{len(wait)}/{wait_cap}</b>",
        "",
        "<b>Confirmed</b>",
    ]

    if players:
        for i, p in enumerate(players, start=1):
            tag = " (VIP)" if p.get("is_vip") else ""
            lines.append(f"{i:>2}. {html.escape(p['name'])}{tag}")
    else:
        lines.append("(no one yet)")

    if wait:
        lines.extend(["", "<b>Waitlist</b>"])
        for i, p in enumerate(wait, start=1):
            lines.append(f"WL{i:>2}. {html.escape(p['name'])}")

    return "\n".join(lines)


def vip_rows(capacity: int) -> List[Dict[str, Any]]:
    """Return reserved entries for VIP_NAMES (up to capacity)."""
    rows = []
    for nm in VIP_NAMES:
        if len(rows) >= capacity:
            break
        rows.append(
            {
                # user_id None means it's a reserved slot by name; we tag as VIP.
                "user_id": None,
                "name": nm,
                "joined_at": now_iso(),
                "is_vip": True,
            }
        )
    return rows


# -----------------------------
# Command Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = ensure_event(context.chat_data)
    text = (
        "Hi! I manage sign-ups for your football matches.\n\n"
        "Use the buttons below or commands:\n"
        "- /join — claim a slot\n"
        "- /leave — give up your slot\n"
        "- /list — show roster\n\n"
        "Admins: /newgame, /lock, /unlock, /reset\n"
        f"Capacity defaults to {DEFAULT_CAPACITY}. Waitlist up to {WAITLIST_CAPACITY}."
    )
    await update.effective_message.reply_html(
        text, reply_markup=make_keyboard(event.get("locked", False))
    )


async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await user_is_admin(update, context):
        await update.effective_message.reply_text("Only admins can start a new game.")
        return

    # Usage: /newgame [title or date-text] [capacity]
    args = context.args

    event = ensure_event(context.chat_data)
    capacity = event.get("capacity", DEFAULT_CAPACITY)
    title = event.get("title") or "Next Game"

    if args:
        # Try to parse last token as capacity if it's an int
        try:
            maybe_cap = int(args[-1])
            capacity = maybe_cap
            args = args[:-1]
        except Exception:
            pass
        # Remaining args form the title if present
        if args:
            title = " ".join(args).strip().strip('"')
            try:
                dt = dtparser.parse(title, fuzzy=True)
                title = dt.strftime("%a %d %b, %I:%M %p").lstrip("0")
            except Exception:
                pass

    # Reset event and seed VIPs
    seeded = vip_rows(max(1, capacity))
    context.chat_data["event"] = {
        "title": title,
        "capacity": max(1, capacity),
        "locked": False,
        "players": seeded[:capacity],  # VIPs occupy earliest slots
        "waitlist": [],
        "created_at": now_iso(),
    }

    msg = (
        f"Created new game: <b>{html.escape(title)}</b>\n"
        f"Capacity: <b>{capacity}</b>\n"
        "VIPs auto-added. Tap <b>Join</b> to claim a spot!"
    )
    await update.effective_message.reply_html(msg, reply_markup=make_keyboard(False))


async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_join(update, context, source="cmd")


async def leave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await handle_leave(update, context, source="cmd")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = ensure_event(context.chat_data)
    roster = format_roster(event)
    await update.effective_message.reply_html(
        roster, reply_markup=make_keyboard(event.get("locked", False))
    )


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await user_is_admin(update, context):
        await update.effective_message.reply_text("Only admins can lock the list.")
        return
    event = ensure_event(context.chat_data)
    event["locked"] = True
    await update.effective_message.reply_text("Sign-ups locked. 🧱")


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await user_is_admin(update, context):
        await update.effective_message.reply_text("Only admins can unlock the list.")
        return
    event = ensure_event(context.chat_data)
    event["locked"] = False
    await update.effective_message.reply_text("Sign-ups unlocked. ✅")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await user_is_admin(update, context):
        await update.effective_message.reply_text("Only admins can reset.")
        return
    context.chat_data.pop("event", None)
    event = ensure_event(context.chat_data)
    await update.effective_message.reply_text(
        "Event reset.", reply_markup=make_keyboard(event.get("locked", False))
    )


# -----------------------------
# Callback (Buttons)
# -----------------------------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "join":
        await handle_join(update, context, source="button")
    elif data == "leave":
        await handle_leave(update, context, source="button")
    elif data == "list":
        event = ensure_event(context.chat_data)
        await query.message.reply_html(
            format_roster(event), reply_markup=make_keyboard(event.get("locked", False))
        )
    else:
        pass


# -----------------------------
# Core Join/Leave Logic
# -----------------------------
def _vip_name_set() -> set[str]:
    return {n.strip().lower() for n in VIP_NAMES if n.strip()}


def _is_vip_row(row: Dict[str, Any]) -> bool:
    return bool(row.get("is_vip"))


async def handle_join(update: Update, context: ContextTypes.DEFAULT_TYPE, source: str) -> None:
    event = ensure_event(context.chat_data)
    msg = update.effective_message
    user = update.effective_user

    if event.get("locked"):
        await msg.reply_text("Sign-ups are locked.")
        return

    uid = user.id
    display_name = user.full_name

    # Already in players?
    i = find_user(event["players"], uid)
    if i != -1:
        await msg.reply_text(f"You're already in the list at #{i+1}.")
        return

    # Already in waitlist?
    j = find_user(event["waitlist"], uid)
    if j != -1:
        await msg.reply_text(f"You're already on the waitlist at WL#{j+1}.")
        return

    capacity = event.get("capacity", DEFAULT_CAPACITY)

    entry = {"user_id": uid, "name": display_name, "joined_at": now_iso(), "is_vip": False}

    if len(event["players"]) < capacity:
        event["players"].append(entry)
        await msg.reply_text(f"Joined! You are #{len(event['players'])}.")
    else:
        # Enforce waitlist capacity
        if len(event["waitlist"]) >= WAITLIST_CAPACITY:
            await msg.reply_text("List and waitlist are full. Sorry!")
            return
        event["waitlist"].append(entry)
        await msg.reply_text(f"List is full. You are WL#{len(event['waitlist'])}.")
