import os
import re
import json
import time
import threading
from collections import defaultdict, deque
from flask import Flask
import telebot
from telebot import types

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.environ.get("OWNER_ID", "0").strip()

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable set nahi hai!")

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    OWNER_ID = 0

DATA_FILE = "bot_data.json"
bot = telebot.TeleBot(BOT_TOKEN)
BOT_USERNAME = bot.get_me().username

app = Flask(__name__)

@app.route("/")
def home():
    return "Group Protector Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

DEFAULT_GROUP_CFG = {
    "bio_guard": True,
    "link_guard": True,
    "spam_guard": True,
    "action_mode": "mute",     # "mute", "ban", "warn"
    "warn_limit": 3,
    "mute_minutes": 60
}

db_lock = threading.Lock()

def load_db():
    default_data = {
        "updates_url": "https://t.me/telegram",
        "support_url": "https://t.me/telegram",
        "groups": {},
        "users": []
    }
    if not os.path.exists(DATA_FILE):
        return default_data
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in default_data.items():
            if k not in data:
                data[k] = v
        return data
    except Exception:
        return default_data

def save_db(data):
    with db_lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

db = load_db()

last_alert_msg = {}
recent_msgs = defaultdict(lambda: defaultdict(deque))
SPAM_WINDOW_SEC = 180
owner_pending = {}

LINK_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|telegram\.dog/\S+|\b[a-zA-Z0-9.-]+\.(com|net|org|in|io|me|co|gg|xyz|app|link|bot)\b)",
    re.IGNORECASE
)
BOT_USERNAME_PATTERN = re.compile(r"@[a-zA-Z0-9_]{3,}bot\b", re.IGNORECASE)

def ensure_group_entry(chat_id, title=None, chat_type="supergroup"):
    key = str(chat_id)
    changed = False
    if key not in db["groups"]:
        db["groups"][key] = {
            "title": title or f"Group {chat_id}",
            "type": chat_type,
            "cfg": dict(DEFAULT_GROUP_CFG),
            "warns": {}
        }
        changed = True
    else:
        if title and db["groups"][key].get("title") != title:
            db["groups"][key]["title"] = title
            changed = True
        for ck, cv in DEFAULT_GROUP_CFG.items():
            if ck not in db["groups"][key]["cfg"]:
                db["groups"][key]["cfg"][ck] = cv
                changed = True
        if "warns" not in db["groups"][key]:
            db["groups"][key]["warns"] = {}
            changed = True
    if changed:
        save_db(db)
    return db["groups"][key]

def is_owner(user_id):
    return OWNER_ID != 0 and user_id == OWNER_ID

def is_group_admin(chat_id, user_id):
    if is_owner(user_id):
        return True
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False

def has_link_or_bot(text):
    if not text:
        return False
    return bool(LINK_PATTERN.search(text) or BOT_USERNAME_PATTERN.search(text))

def normalize_text(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())

def safe_delete(chat_id, message_id):
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def send_clean_alert(chat_id, html_text):
    prev_id = last_alert_msg.get(chat_id)
    if prev_id:
        safe_delete(chat_id, prev_id)

    try:
        sent = bot.send_message(chat_id, html_text, parse_mode="HTML", disable_web_page_preview=True)
        last_alert_msg[chat_id] = sent.message_id
    except Exception:
        pass

def format_duration(minutes):
    if minutes < 60:
        return f"{minutes}m"
    if minutes % 60 == 0 and minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"

# --- कीबोर्ड्स ---

def start_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    add_btn = types.InlineKeyboardButton(
        "➕ Add Me To Your Group ➕",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
    )
    groups_btn = types.InlineKeyboardButton(
        "📂 My Managed Groups",
        callback_data="my_groups"
    )
    updates_btn = types.InlineKeyboardButton(
        "📣 Updates ↗",
        url=db.get("updates_url", "https://t.me/telegram")
    )
    support_btn = types.InlineKeyboardButton(
        "💬 Support ↗",
        url=db.get("support_url", "https://t.me/telegram")
    )
    kb.add(add_btn)
    kb.add(groups_btn)
    kb.row(updates_btn, support_btn)
    return kb

def group_settings_text(gid_str):
    gdata = db["groups"].get(gid_str)
    if not gdata:
        return "⚠️ Group not found."
    cfg = gdata["cfg"]
    mode_txt = {
        "mute": f"🔇 Mute ({format_duration(cfg['mute_minutes'])})",
        "ban": "🔨 Ban",
        "warn": "⚠️ Warn Only"
    }.get(cfg["action_mode"], "🔇 Mute")

    return (
        "⚙️ <b>BOT SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ <b>Group:</b> {gdata.get('title')}\n"
        f"🆔 <b>ID:</b> <code>{gid_str}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🕵️ Bio Guard: <b>{'🟢 ON' if cfg['bio_guard'] else '🔴 OFF'}</b>\n"
        f"🔗 Link/Bot Guard: <b>{'🟢 ON' if cfg['link_guard'] else '🔴 OFF'}</b>\n"
        f"🚨 Spam Guard: <b>{'🟢 ON' if cfg['spam_guard'] else '🔴 OFF'}</b>\n"
        f"⚖️ Action: <b>{mode_txt}</b> • Limit: <b>{cfg['warn_limit']}</b>"
    )

def group_settings_keyboard(gid_str):
    cfg = db["groups"][gid_str]["cfg"]
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.row(
        types.InlineKeyboardButton(
            f"{'🟢' if cfg['bio_guard'] else '🔴'} Bio Guard",
            callback_data=f"tgl:{gid_str}:bio_guard"
        ),
        types.InlineKeyboardButton(
            f"{'🟢' if cfg['link_guard'] else '🔴'} Link/Bot Guard",
            callback_data=f"tgl:{gid_str}:link_guard"
        )
    )
    kb.row(
        types.InlineKeyboardButton(
            f"{'🟢' if cfg['spam_guard'] else '🔴'} Spam Guard",
            callback_data=f"tgl:{gid_str}:spam_guard"
        ),
        types.InlineKeyboardButton(
            f"⚖️ {cfg['action_mode'].upper()}",
            callback_data=f"cycle_mode:{gid_str}"
        )
    )
    kb.row(
        types.InlineKeyboardButton(
            f"⚠️ Warns: {cfg['warn_limit']}",
            callback_data=f"cycle_warn:{gid_str}"
        ),
        types.InlineKeyboardButton(
            f"🔇 Mute: {format_duration(cfg['mute_minutes'])}",
            callback_data=f"cycle_mute:{gid_str}"
        )
    )
    kb.add(types.InlineKeyboardButton("♻️ Reset Warns", callback_data=f"reset_warns:{gid_str}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="my_groups"))
    return kb

def owner_panel_text():
    return (
        "👑 <b>OWNER PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ Groups & Channels: <b>{len(db['groups'])}</b>\n"
        f"👤 Users: <b>{len(db['users'])}</b>\n"
        f"📣 Updates: <code>{db.get('updates_url')}</code>\n"
        f"💬 Support: <code>{db.get('support_url')}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

def owner_panel_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🚀 Send Promo / Broadcast", callback_data="own_promo"))
    kb.row(
        types.InlineKeyboardButton("📣 Updates Link", callback_data="own_set_updates"),
        types.InlineKeyboardButton("💬 Support Link", callback_data="own_set_support")
    )
    kb.add(types.InlineKeyboardButton("📂 Managed Groups", callback_data="my_groups"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="home"))
    return kb

def owner_cancel_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="own_back"))
    return kb

# --- प्रोटेक्शन व सजा लॉजिक ---

def punish_user(chat_id, user, short_reason, offending_msg_id=None):
    if offending_msg_id:
        safe_delete(chat_id, offending_msg_id)

    gdata = ensure_group_entry(chat_id)
    cfg = gdata["cfg"]
    uid_str = str(user.id)
    clean_name = (user.first_name or "User").replace("<", "").replace(">", "")
    mention = f'<a href="tg://user?id={user.id}">{clean_name}</a>'

    warns = gdata["warns"].get(uid_str, 0) + 1
    gdata["warns"][uid_str] = warns
    limit = cfg["warn_limit"]

    if warns >= limit:
        mode = cfg["action_mode"]
        gdata["warns"][uid_str] = 0
        save_db(db)

        if mode == "ban":
            try:
                bot.ban_chat_member(chat_id, user.id)
                send_clean_alert(chat_id, f"🔨 {mention} <b>Banned</b> • {short_reason}")
            except Exception:
                send_clean_alert(chat_id, f"⚠️ Ban failed • {mention}")
        elif mode == "mute":
            mute_mins = cfg["mute_minutes"]
            until_ts = int(time.time()) + (mute_mins * 60)
            try:
                perms = types.ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False
                )
                bot.restrict_chat_member(chat_id, user.id, until_date=until_ts, permissions=perms)
                send_clean_alert(chat_id, f"🔇 {mention} <b>Muted ({format_duration(mute_mins)})</b> • {short_reason}")
            except Exception:
                send_clean_alert(chat_id, f"⚠️ Mute failed • {mention}")
        else:
            send_clean_alert(chat_id, f"🚨 {mention} <b>{limit}/{limit} Warns</b> • {short_reason}")
    else:
        save_db(db)
        send_clean_alert(chat_id, f"⚠️ {mention} <b>Warn ({warns}/{limit})</b> • {short_reason}")

def check_bio(chat_id, user, msg_id):
    try:
        info = bot.get_chat(user.id)
        bio = getattr(info, "bio", "") or ""
        if has_link_or_bot(bio):
            punish_user(chat_id, user, "🔗 Bio link/bot", msg_id)
            return True
    except Exception:
        pass
    return False

def check_spam_across_accounts(message):
    chat_id = message.chat.id
    user = message.from_user
    norm = normalize_text(message.text or message.caption or "")
    if not norm or len(norm) < 4:
        return False

    now = time.time()
    q = recent_msgs[chat_id][norm]
    while q and (now - q[0][2]) > SPAM_WINDOW_SEC:
        q.popleft()

    q.append((user.id, message.message_id, now))
    distinct_users = {item[0] for item in q}

    if len(distinct_users) >= 2:
        entries = list(q)
        q.clear()
        for uid, mid, _ in entries:
            safe_delete(chat_id, mid)
            try:
                u_obj = bot.get_chat_member(chat_id, uid).user
            except Exception:
                u_obj = user
            punish_user(chat_id, u_obj, "🚨 Spam flood", None)
        return True

    return False

# --- चैट स्टेटस ---

@bot.my_chat_member_handler()
def handle_my_chat_member(update):
    chat = update.chat
    new_status = update.new_chat_member.status
    key = str(chat.id)

    if new_status in ["administrator", "member"]:
        ensure_group_entry(chat.id, title=chat.title, chat_type=chat.type)
    elif new_status in ["left", "kicked"] and key in db["groups"]:
        del db["groups"][key]
        save_db(db)

# --- कमांड्स ---

@bot.message_handler(commands=["start"])
def cmd_start(message):
    if message.chat.type == "private":
        uid = message.from_user.id
        owner_pending.pop(uid, None)
        if uid not in db["users"]:
            db["users"].append(uid)
            save_db(db)

        first_name = (message.from_user.first_name or "User").replace("<", "").replace(">", "")
        text = (
            f"✨ Hello, <b>{first_name}</b>\n"
            "🛡️ I keep your group clean from spam links, bot URLs, and multi-account spam.\n\n"
            "⚡ <b>What I protect:</b>\n"
            "• 🕵️ Bio links & bot URLs\n"
            "• 🔗 Links & <code>@bot</code> in chat\n"
            "• 🚨 Same message across accounts\n"
            "• ⚖️ Custom warns, mute & ban"
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=start_keyboard())
    else:
        ensure_group_entry(message.chat.id, title=message.chat.title, chat_type=message.chat.type)

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.chat.type != "private":
        return
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🔒 Owner only.")
        return
    owner_pending.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, owner_panel_text(), parse_mode="HTML", reply_markup=owner_panel_keyboard())

# --- कॉलबैक बटन ---

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    data = call.data
    uid = call.from_user.id

    if data == "noop":
        bot.answer_callback_query(call.id)
        return

    if data == "home":
        owner_pending.pop(uid, None)
        first_name = (call.from_user.first_name or "User").replace("<", "").replace(">", "")
        text = (
            f"✨ Hello, <b>{first_name}</b>\n"
            "🛡️ I keep your group clean from spam links, bot URLs, and multi-account spam."
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=start_keyboard())
        except Exception:
            pass
        return

    if data == "own_back":
        owner_pending.pop(uid, None)
        if is_owner(uid):
            try:
                bot.edit_message_text(owner_panel_text(), call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=owner_panel_keyboard())
            except Exception:
                pass
        return

    if data == "my_groups":
        allowed = [(gid_str, gdata) for gid_str, gdata in db["groups"].items() if is_group_admin(int(gid_str), uid)]
        kb = types.InlineKeyboardMarkup(row_width=1)
        if not allowed:
            kb.add(types.InlineKeyboardButton("📭 No managed groups found", callback_data="noop"))
        else:
            for gid_str, gdata in allowed:
                kb.add(types.InlineKeyboardButton(f"🛡️ {gdata.get('title', gid_str)}", callback_data=f"open_grp:{gid_str}"))
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="home"))

        try:
            bot.edit_message_text("📂 <b>Select Group:</b>", call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return

    if data.startswith("open_grp:"):
        gid_str = data.split(":", 1)[1]
        if gid_str not in db["groups"] or not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "🔒 Access denied.", show_alert=True)
            return
        bot.edit_message_text(group_settings_text(gid_str), call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=group_settings_keyboard(gid_str))
        return

    if data.startswith("tgl:"):
        _, gid_str, feat = data.split(":", 2)
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "🔒 Access denied.", show_alert=True)
            return
        cfg = db["groups"][gid_str]["cfg"]
        cfg[feat] = not cfg.get(feat, True)
        save_db(db)
        bot.answer_callback_query(call.id, "🟢 ON" if cfg[feat] else "🔴 OFF")
        bot.edit_message_text(group_settings_text(gid_str), call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=group_settings_keyboard(gid_str))
        return

    if data.startswith("cycle_mode:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "🔒 Access denied.", show_alert=True)
            return
        modes = ["mute", "ban", "warn"]
        cfg = db["groups"][gid_str]["cfg"]
        idx = modes.index(cfg["action_mode"]) if cfg["action_mode"] in modes else 0
        cfg["action_mode"] = modes[(idx + 1) % len(modes)]
        save_db(db)
        bot.answer_callback_query(call.id, f"⚖️ {cfg['action_mode'].upper()}")
        bot.edit_message_text(group_settings_text(gid_str), call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=group_settings_keyboard(gid_str))
        return

    if data.startswith("cycle_warn:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "🔒 Access denied.", show_alert=True)
            return
        limits = [2, 3, 4, 5]
        cfg = db["groups"][gid_str]["cfg"]
        idx = limits.index(cfg["warn_limit"]) if cfg["warn_limit"] in limits else 1
        cfg["warn_limit"] = limits[(idx + 1) % len(limits)]
        save_db(db)
        bot.answer_callback_query(call.id, f"⚠️ {cfg['warn_limit']}")
        bot.edit_message_text(group_settings_text(gid_str), call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=group_settings_keyboard(gid_str))
        return

    if data.startswith("cycle_mute:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "🔒 Access denied.", show_alert=True)
            return
        durations = [10, 30, 60, 360, 1440, 10080]
        cfg = db["groups"][gid_str]["cfg"]
        idx = durations.index(cfg["mute_minutes"]) if cfg["mute_minutes"] in durations else 2
        cfg["mute_minutes"] = durations[(idx + 1) % len(durations)]
        save_db(db)
        bot.answer_callback_query(call.id, f"🔇 {format_duration(cfg['mute_minutes'])}")
        bot.edit_message_text(group_settings_text(gid_str), call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=group_settings_keyboard(gid_str))
        return

    if data.startswith("reset_warns:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "🔒 Access denied.", show_alert=True)
            return
        db["groups"][gid_str]["warns"] = {}
        save_db(db)
        bot.answer_callback_query(call.id, "♻️ Warns reset!", show_alert=True)
        return

    if data.startswith("own_"):
        if not is_owner(uid):
            bot.answer_callback_query(call.id, "🔒 Owner only.", show_alert=True)
            return

        if data == "own_set_updates":
            owner_pending[uid] = {"mode": "set_updates"}
            bot.edit_message_text("📣 Send new Updates link:", call.message.chat.id, call.message.message_id, reply_markup=owner_cancel_keyboard())
        elif data == "own_set_support":
            owner_pending[uid] = {"mode": "set_support"}
            bot.edit_message_text("💬 Send new Support link:", call.message.chat.id, call.message.message_id, reply_markup=owner_cancel_keyboard())
        elif data == "own_promo":
            owner_pending[uid] = {"mode": "promo"}
            bot.edit_message_text("🚀 Send message to broadcast:", call.message.chat.id, call.message.message_id, reply_markup=owner_cancel_keyboard())
        bot.answer_callback_query(call.id)

# --- मैसेज हैंडलर ---

@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video", "document", "animation", "sticker"])
def handle_all_messages(message):
    user = message.from_user
    if not user or user.is_bot:
        return

    if message.chat.type == "private":
        if is_owner(user.id) and user.id in owner_pending:
            state = owner_pending.pop(user.id)
            mode = state["mode"]

            if mode == "set_updates" and message.text:
                db["updates_url"] = message.text.strip()
                save_db(db)
                bot.send_message(user.id, "✅ Updates link updated.", reply_markup=owner_panel_keyboard())

            elif mode == "set_support" and message.text:
                db["support_url"] = message.text.strip()
                save_db(db)
                bot.send_message(user.id, "✅ Support link updated.", reply_markup=owner_panel_keyboard())

            elif mode == "promo":
                sent = 0
                targets = set(list(db["groups"].keys()) + [str(u) for u in db["users"]])
                for t_id in targets:
                    try:
                        bot.copy_message(int(t_id), user.id, message.message_id)
                        sent += 1
                        time.sleep(0.15)
                    except Exception:
                        pass
                bot.send_message(user.id, f"🚀 Sent to {sent} chats.", reply_markup=owner_panel_keyboard())
        return

    if message.chat.type in ["group", "supergroup"]:
        gdata = ensure_group_entry(message.chat.id, title=message.chat.title, chat_type=message.chat.type)
        cfg = gdata["cfg"]

        if is_group_admin(message.chat.id, user.id):
            return

        if cfg.get("bio_guard", True) and check_bio(message.chat.id, user, message.message_id):
            return

        content = message.text or message.caption or ""
        if cfg.get("link_guard", True) and has_link_or_bot(content):
            punish_user(message.chat.id, user, "🔗 Link/bot", message.message_id)
            return

        if cfg.get("spam_guard", True) and check_spam_across_accounts(message):
            return

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("Group Protector Bot chal raha hai...")
    bot.infinity_polling(skip_pending=True, allowed_updates=["message", "callback_query", "my_chat_member"])
