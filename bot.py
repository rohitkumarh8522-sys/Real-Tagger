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

# Render Web Service के लिए Flask सर्वर
app = Flask(__name__)

@app.route("/")
def home():
    return "Group Protector Bot UI is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# डिफ़ॉल्ट डेटा संरचना
DEFAULT_GROUP_CFG = {
    "bio_guard": True,
    "link_guard": True,
    "spam_guard": True,
    "action_mode": "mute",     # "mute", "ban", या "warn"
    "warn_limit": 3,           # 3 वॉर्निंग के बाद Mute/Ban
    "mute_minutes": 60         # डिफ़ॉल्ट 60 मिनट (1 घंटा) म्यूट
}

db_lock = threading.Lock()

def load_db():
    default_data = {
        "updates_url": "https://t.me/telegram",
        "support_url": "https://t.me/telegram",
        "groups": {},          # str(chat_id) -> {"title": ..., "type": ..., "cfg": {...}, "warns": {str(uid): count}}
        "users": []            # ब्रॉडकास्ट के लिए प्राइवेट यूज़र IDs
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

# क्रॉस-अकाउंट स्पैम ट्रैकिंग: chat_id -> normalized_text -> deque([(user_id, message_id, timestamp)])
recent_msgs = defaultdict(lambda: defaultdict(deque))
SPAM_WINDOW_SEC = 180

# ओनर के लिए पेंडिंग इनपुट स्टेट (लिंक बदलने या प्रमोशन मैसेज भेजने के लिए)
owner_pending = {}  # user_id -> {"mode": "set_updates" | "set_support" | "promo"}

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
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def format_duration(minutes):
    if minutes < 60:
        return f"{minutes}m"
    if minutes % 60 == 0 and minutes < 1440:
        return f"{minutes // 60}h"
    days = minutes // 1440
    return f"{days}d"

# --- UI कीबोर्ड्स ---

def start_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    add_btn = types.InlineKeyboardButton(
        "+ Add Me To Your Group +",
        url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
    )
    groups_btn = types.InlineKeyboardButton(
        "📁 My Managed Groups",
        callback_data="my_groups"
    )
    updates_btn = types.InlineKeyboardButton(
        "📢 Updates",
        url=db.get("updates_url", "https://t.me/telegram")
    )
    support_btn = types.InlineKeyboardButton(
        "💬 Support",
        url=db.get("support_url", "https://t.me/telegram")
    )
    kb.add(add_btn)
    kb.add(groups_btn)
    kb.row(updates_btn, support_btn)
    return kb

def managed_groups_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not db["groups"]:
        kb.add(types.InlineKeyboardButton("⚠️ कोई ग्रुप/चैनल नहीं मिला", callback_data="noop"))
    else:
        for gid_str, gdata in db["groups"].items():
            gtitle = gdata.get("title", f"Group {gid_str}")
            kb.add(types.InlineKeyboardButton(f"🛡️ {gtitle}", callback_data=f"open_grp:{gid_str}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back to Home", callback_data="home"))
    return kb

def group_settings_text(gid_str):
    gdata = db["groups"].get(gid_str)
    if not gdata:
        return "⚠️ ग्रुप डेटा नहीं मिला।"
    cfg = gdata["cfg"]
    mode_label = {
        "mute": f"Mute ({format_duration(cfg['mute_minutes'])})",
        "ban": "Ban",
        "warn": "Warn Only"
    }.get(cfg["action_mode"], "Mute")

    return (
        "⚙️ <b>BOT SETTINGS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Group:</b> {gdata.get('title')}\n"
        f"<b>ID:</b> <code>{gid_str}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Bio Link Guard:</b> {'ON ✅' if cfg['bio_guard'] else 'OFF ❌'}\n"
        f"• <b>Link & Bot Guard:</b> {'ON ✅' if cfg['link_guard'] else 'OFF ❌'}\n"
        f"• <b>Multi-Account Spam:</b> {'ON ✅' if cfg['spam_guard'] else 'OFF ❌'}\n"
        f"• <b>Punishment Mode:</b> {mode_label}\n"
        f"• <b>Warn Limit:</b> {cfg['warn_limit']} warnings\n"
        f"• <b>Mute Duration:</b> {format_duration(cfg['mute_minutes'])}\n\n"
        "<i>नीचे दिए गए बटन से सेटिंग्स बदलें:</i>"
    )

def group_settings_keyboard(gid_str):
    gdata = db["groups"][gid_str]
    cfg = gdata["cfg"]
    kb = types.InlineKeyboardMarkup(row_width=2)

    kb.row(
        types.InlineKeyboardButton(
            f"{'🟢' if cfg['bio_guard'] else '🔴'} Bio Link Guard",
            callback_data=f"tgl:{gid_str}:bio_guard"
        ),
        types.InlineKeyboardButton(
            f"{'🟢' if cfg['link_guard'] else '🔴'} Link/Bot Guard",
            callback_data=f"tgl:{gid_str}:link_guard"
        )
    )
    kb.row(
        types.InlineKeyboardButton(
            f"{'🟢' if cfg['spam_guard'] else '🔴'} Multi-Account Spam",
            callback_data=f"tgl:{gid_str}:spam_guard"
        )
    )
    kb.row(
        types.InlineKeyboardButton(
            f"⚖️ Mode: {cfg['action_mode'].upper()}",
            callback_data=f"cycle_mode:{gid_str}"
        ),
        types.InlineKeyboardButton(
            f"⚠️ Warns: {cfg['warn_limit']}",
            callback_data=f"cycle_warn:{gid_str}"
        )
    )
    kb.row(
        types.InlineKeyboardButton(
            f"🔇 Mute Time: {format_duration(cfg['mute_minutes'])}",
            callback_data=f"cycle_mute:{gid_str}"
        ),
        types.InlineKeyboardButton(
            "🔄 Reset Warns",
            callback_data=f"reset_warns:{gid_str}"
        )
    )
    kb.add(types.InlineKeyboardButton("⬅️ Back to Groups", callback_data="my_groups"))
    return kb

def owner_panel_text():
    total_groups = len(db["groups"])
    total_users = len(db["users"])
    return (
        "👑 <b>OWNER ADMIN PANEL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Total Managed Groups/Channels:</b> {total_groups}\n"
        f"• <b>Total Bot Users:</b> {total_users}\n"
        f"• <b>Updates Link:</b> {db.get('updates_url')}\n"
        f"• <b>Support Link:</b> {db.get('support_url')}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>नीचे से प्रमोशन मैसेज भेजें या बटन लिंक बदलें:</i>"
    )

def owner_panel_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📣 Send Promotion / Broadcast", callback_data="own_promo"))
    kb.row(
        types.InlineKeyboardButton("🔗 Change Updates Link", callback_data="own_set_updates"),
        types.InlineKeyboardButton("🔗 Change Support Link", callback_data="own_set_support")
    )
    kb.add(types.InlineKeyboardButton("📋 View Groups List", callback_data="my_groups"))
    return kb

# --- प्रोटेक्शन व सजा लॉजिक ---

def punish_user(chat_id, user, reason, offending_msg_id=None):
    if offending_msg_id:
        safe_delete(chat_id, offending_msg_id)

    gdata = ensure_group_entry(chat_id)
    cfg = gdata["cfg"]
    uid_str = str(user.id)
    clean_name = (user.first_name or "User").replace("<", "").replace(">", "")
    mention = f'<a href="tg://user?id={user.id}">{clean_name}</a>'

    current_warns = gdata["warns"].get(uid_str, 0) + 1
    gdata["warns"][uid_str] = current_warns
    limit = cfg["warn_limit"]

    if current_warns >= limit:
        mode = cfg["action_mode"]
        gdata["warns"][uid_str] = 0
        save_db(db)

        if mode == "ban":
            try:
                bot.ban_chat_member(chat_id, user.id)
                bot.send_message(
                    chat_id,
                    f"🔨 {mention} को <b>{limit}/{limit}</b> वॉर्निंग के बाद <b>BAN</b> कर दिया गया।\n<b>कारण:</b> {reason}",
                    parse_mode="HTML"
                )
            except Exception as e:
                bot.send_message(chat_id, f"⚠️ Ban करने में त्रुटि: <code>{e}</code>", parse_mode="HTML")

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
                bot.send_message(
                    chat_id,
                    f"🔇 {mention} को <b>{limit}/{limit}</b> वॉर्निंग के बाद <b>{format_duration(mute_mins)}</b> के लिए <b>MUTE</b> कर दिया गया।\n<b>कारण:</b> {reason}",
                    parse_mode="HTML"
                )
            except Exception as e:
                bot.send_message(chat_id, f"⚠️ Mute करने में त्रुटि: <code>{e}</code>", parse_mode="HTML")

        else:
            save_db(db)
            bot.send_message(
                chat_id,
                f"⚠️ {mention} को <b>{limit}/{limit}</b> वॉर्निंग मिली है।\n<b>कारण:</b> {reason}",
                parse_mode="HTML"
            )
    else:
        save_db(db)
        bot.send_message(
            chat_id,
            f"⚠️ <b>चेतावनी ({current_warns}/{limit})</b> — {mention}\n"
            f"<b>कारण:</b> {reason}\n"
            f"<i>{limit}वीं बार उल्लंघन पर {cfg['action_mode'].upper()} कार्रवाई होगी।</i>",
            parse_mode="HTML"
        )

def check_bio(chat_id, user, msg_id):
    try:
        info = bot.get_chat(user.id)
        bio = getattr(info, "bio", "") or ""
        if has_link_or_bot(bio):
            punish_user(chat_id, user, "प्रोफ़ाइल Bio में लिंक या बॉट URL/यूज़रनेम", msg_id)
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
            punish_user(chat_id, u_obj, "अलग-अलग अकाउंट से एक ही मैसेज (स्पैम)", None)
        return True

    return False

# --- बॉट स्टेटस अपडेट (ग्रुप/चैनल में जुड़ने या एडमिन बनने पर) ---

@bot.my_chat_member_handler()
def handle_my_chat_member(update):
    chat = update.chat
    new_status = update.new_chat_member.status
    key = str(chat.id)

    if new_status in ["administrator", "member"]:
        ensure_group_entry(chat.id, title=chat.title, chat_type=chat.type)
    elif new_status in ["left", "kicked", "restricted"]:
        if new_status in ["left", "kicked"] and key in db["groups"]:
            del db["groups"][key]
            save_db(db)

# --- कमांड्स (/start, /admin, /settings) ---

@bot.message_handler(commands=["start"])
def cmd_start(message):
    if message.chat.type == "private":
        uid = message.from_user.id
        if uid not in db["users"]:
            db["users"].append(uid)
            save_db(db)

        first_name = (message.from_user.first_name or "User").replace("<", "").replace(">", "")
        text = (
            f"Hello, <b>{first_name}</b> 🌿\n"
            "I am a <b>Bio Link Guard & Group Security Bot</b>. I keep your group clean from "
            "spam links, bot URLs, and multi-account spam.\n\n"
            "⚡ <b>What I can do:</b>\n"
            "• Detects links & bot URLs in member bios\n"
            "• Blocks links & bot usernames in group chats\n"
            "• Detects same message spam across different accounts\n"
            "• Configurable Warnings, Mute duration & Ban\n\n"
            "Add me to your group and I'll protect it."
        )
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=start_keyboard())
    else:
        ensure_group_entry(message.chat.id, title=message.chat.title, chat_type=message.chat.type)
        bot.reply_to(message, "🛡️ Group Protector सक्रिय है। सेटिंग्स के लिए प्राइवेट चैट में <b>My Managed Groups</b> खोलें।", parse_mode="HTML")

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.chat.type != "private":
        return
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "❌ यह पैनल केवल बॉट ओनर के लिए है।")
        return
    bot.send_message(message.chat.id, owner_panel_text(), parse_mode="HTML", reply_markup=owner_panel_keyboard())

# --- कॉलबैक बटन हैंडलिंग ---

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    data = call.data
    uid = call.from_user.id

    if data == "noop":
        bot.answer_callback_query(call.id)
        return

    if data == "home":
        first_name = (call.from_user.first_name or "User").replace("<", "").replace(">", "")
        text = (
            f"Hello, <b>{first_name}</b> 🌿\n"
            "I am a <b>Bio Link Guard & Group Security Bot</b>. I keep your group clean from "
            "spam links, bot URLs, and multi-account spam.\n\n"
            "Add me to your group and I'll protect it."
        )
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=start_keyboard())
        except Exception:
            pass
        return

    if data == "my_groups":
        # केवल वे ग्रुप्स दिखाएं जहाँ यूज़र एडमिन/ओनर है (या बॉट ओनर सभी देख सके)
        allowed_items = []
        for gid_str, gdata in db["groups"].items():
            gid = int(gid_str)
            if is_group_admin(gid, uid):
                allowed_items.append((gid_str, gdata))

        kb = types.InlineKeyboardMarkup(row_width=1)
        if not allowed_items:
            kb.add(types.InlineKeyboardButton("⚠️ कोई मैनेज करने योग्य ग्रुप नहीं मिला", callback_data="noop"))
        else:
            for gid_str, gdata in allowed_items:
                kb.add(types.InlineKeyboardButton(f"🛡️ {gdata.get('title', gid_str)}", callback_data=f"open_grp:{gid_str}"))
        kb.add(types.InlineKeyboardButton("⬅️ Back to Home", callback_data="home"))

        try:
            bot.edit_message_text(
                "📂 <b>Select a Group to Manage:</b>",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=kb
            )
        except Exception:
            pass
        return

    if data.startswith("open_grp:"):
        gid_str = data.split(":", 1)[1]
        if gid_str not in db["groups"]:
            bot.answer_callback_query(call.id, "ग्रुप नहीं मिला।", show_alert=True)
            return
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "❌ आप इस ग्रुप के एडमिन नहीं हैं।", show_alert=True)
            return

        bot.edit_message_text(
            group_settings_text(gid_str),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=group_settings_keyboard(gid_str)
        )
        return

    if data.startswith("tgl:"):
        _, gid_str, feat = data.split(":", 2)
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "❌ अनुमति नहीं है।", show_alert=True)
            return
        cfg = db["groups"][gid_str]["cfg"]
        cfg[feat] = not cfg.get(feat, True)
        save_db(db)
        bot.answer_callback_query(call.id, f"{feat}: {'ON' if cfg[feat] else 'OFF'}")
        bot.edit_message_text(
            group_settings_text(gid_str),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=group_settings_keyboard(gid_str)
        )
        return

    if data.startswith("cycle_mode:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "❌ अनुमति नहीं है।", show_alert=True)
            return
        modes = ["mute", "ban", "warn"]
        cfg = db["groups"][gid_str]["cfg"]
        idx = modes.index(cfg["action_mode"]) if cfg["action_mode"] in modes else 0
        cfg["action_mode"] = modes[(idx + 1) % len(modes)]
        save_db(db)
        bot.answer_callback_query(call.id, f"Mode: {cfg['action_mode'].upper()}")
        bot.edit_message_text(
            group_settings_text(gid_str),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=group_settings_keyboard(gid_str)
        )
        return

    if data.startswith("cycle_warn:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "❌ अनुमति नहीं है।", show_alert=True)
            return
        limits = [2, 3, 4, 5]
        cfg = db["groups"][gid_str]["cfg"]
        idx = limits.index(cfg["warn_limit"]) if cfg["warn_limit"] in limits else 1
        cfg["warn_limit"] = limits[(idx + 1) % len(limits)]
        save_db(db)
        bot.answer_callback_query(call.id, f"Warn Limit: {cfg['warn_limit']}")
        bot.edit_message_text(
            group_settings_text(gid_str),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=group_settings_keyboard(gid_str)
        )
        return

    if data.startswith("cycle_mute:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "❌ अनुमति नहीं है।", show_alert=True)
            return
        durations = [10, 30, 60, 360, 1440, 10080]
        cfg = db["groups"][gid_str]["cfg"]
        idx = durations.index(cfg["mute_minutes"]) if cfg["mute_minutes"] in durations else 2
        cfg["mute_minutes"] = durations[(idx + 1) % len(durations)]
        save_db(db)
        bot.answer_callback_query(call.id, f"Mute Time: {format_duration(cfg['mute_minutes'])}")
        bot.edit_message_text(
            group_settings_text(gid_str),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=group_settings_keyboard(gid_str)
        )
        return

    if data.startswith("reset_warns:"):
        gid_str = data.split(":", 1)[1]
        if not is_group_admin(int(gid_str), uid):
            bot.answer_callback_query(call.id, "❌ अनुमति नहीं है।", show_alert=True)
            return
        db["groups"][gid_str]["warns"] = {}
        save_db(db)
        bot.answer_callback_query(call.id, "✅ सभी वॉर्निंग्स रीसेट हो गईं!", show_alert=True)
        return

    # --- ओनर पैनल कॉलबैक्स ---
    if data.startswith("own_"):
        if not is_owner(uid):
            bot.answer_callback_query(call.id, "❌ सिर्फ ओनर ही यह कर सकता है।", show_alert=True)
            return

        if data == "own_set_updates":
            owner_pending[uid] = {"mode": "set_updates"}
            bot.send_message(uid, "🔗 नया <b>Updates</b> लिंक भेजें (जैसे <code>https://t.me/yourchannel</code>):", parse_mode="HTML")
        elif data == "own_set_support":
            owner_pending[uid] = {"mode": "set_support"}
            bot.send_message(uid, "🔗 नया <b>Support</b> लिंक भेजें (जैसे <code>https://t.me/yoursupport</code>):", parse_mode="HTML")
        elif data == "own_promo":
            owner_pending[uid] = {"mode": "promo"}
            bot.send_message(uid, "📣 वह प्रमोशन/अनाउंसमेंट मैसेज लिखें जिसे सभी ग्रुप्स और यूज़र्स को भेजना है:")
        bot.answer_callback_query(call.id)

# --- प्राइवेट मैसेज (ओनर इनपुट) व ग्रुप मैसेज मॉनिटरिंग ---

@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video", "document", "animation", "sticker"])
def handle_all_messages(message):
    user = message.from_user
    if not user or user.is_bot:
        return

    # प्राइवेट चैट में ओनर के पेंडिंग इनपुट हैंडल करना
    if message.chat.type == "private":
        if is_owner(user.id) and user.id in owner_pending and message.text:
            state = owner_pending.pop(user.id)
            mode = state["mode"]
            val = message.text.strip()

            if mode == "set_updates":
                db["updates_url"] = val
                save_db(db)
                bot.reply_to(message, "✅ Updates लिंक अपडेट हो गया!", reply_markup=owner_panel_keyboard())
            elif mode == "set_support":
                db["support_url"] = val
                save_db(db)
                bot.reply_to(message, "✅ Support लिंक अपडेट हो गया!", reply_markup=owner_panel_keyboard())
            elif mode == "promo":
                sent_count = 0
                targets = list(db["groups"].keys()) + [str(u) for u in db["users"]]
                for t_id in set(targets):
                    try:
                        bot.send_message(int(t_id), f"📣 <b>Promotion</b>\n\n{val}", parse_mode="HTML")
                        sent_count += 1
                        time.sleep(0.15)
                    except Exception:
                        pass
                bot.reply_to(message, f"✅ प्रमोशन मैसेज <b>{sent_count}</b> चैट्स पर भेजा गया।", parse_mode="HTML", reply_markup=owner_panel_keyboard())
        return

    # ग्रुप/सुपरग्रुप प्रोटेक्शन
    if message.chat.type in ["group", "supergroup"]:
        gdata = ensure_group_entry(message.chat.id, title=message.chat.title, chat_type=message.chat.type)
        cfg = gdata["cfg"]

        if is_group_admin(message.chat.id, user.id):
            return

        if cfg.get("bio_guard", True):
            if check_bio(message.chat.id, user, message.message_id):
                return

        content = message.text or message.caption or ""
        if cfg.get("link_guard", True) and has_link_or_bot(content):
            punish_user(message.chat.id, user, "ग्रुप में लिंक या बॉट यूज़रनेम भेजना", message.message_id)
            return

        if cfg.get("spam_guard", True):
            if check_spam_across_accounts(message):
                return

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("Group Protector UI Bot chal raha hai...")
    bot.infinity_polling(skip_pending=True, allowed_updates=["message", "callback_query", "my_chat_member"])
