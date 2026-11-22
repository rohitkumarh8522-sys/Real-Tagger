import os
import re
import time
import threading
from collections import defaultdict, deque
from flask import Flask
import telebot
from telebot import types

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable set nahi hai!")

bot = telebot.TeleBot(BOT_TOKEN)

# Render Web Service को चालू रखने के लिए Flask सर्वर
app = Flask(__name__)

@app.route("/")
def home():
    return "Advance Group Protector Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

MAX_WARNINGS = 3          # 3वीं बार उल्लंघन पर Ban
SPAM_WINDOW_SEC = 180     # 3 मिनट के भीतर एक जैसा मैसेज आए तो स्पैम

# chat_id -> {user_id: warning_count}
warnings_db = defaultdict(lambda: defaultdict(int))

# chat_id -> {normalized_text: deque([(user_id, message_id, timestamp), ...])}
recent_msgs_db = defaultdict(lambda: defaultdict(deque))

# लिंक और बॉट यूज़रनेम पहचानने के पैटर्न
LINK_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|telegram\.dog/\S+|\b[a-zA-Z0-9.-]+\.(com|net|org|in|io|me|co|gg|xyz|app|link|bot)\b)",
    re.IGNORECASE
)
BOT_USERNAME_PATTERN = re.compile(r"@[a-zA-Z0-9_]{3,}bot\b", re.IGNORECASE)

def is_admin(chat_id, user_id):
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
    clean = re.sub(r"\s+", " ", (text or "").strip().lower())
    return clean

def safe_delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def apply_strike(chat_id, user, reason, offending_message_id=None):
    if offending_message_id:
        safe_delete_message(chat_id, offending_message_id)

    user_id = user.id
    mention = f'<a href="tg://user?id={user_id}">{(user.first_name or "User").replace("<", "").replace(">", "")}</a>'

    warnings_db[chat_id][user_id] += 1
    strikes = warnings_db[chat_id][user_id]

    if strikes >= MAX_WARNINGS:
        try:
            bot.ban_chat_member(chat_id, user_id)
            warnings_db[chat_id][user_id] = 0
            bot.send_message(
                chat_id,
                f"🔨 {mention} को <b>{MAX_WARNINGS}/{MAX_WARNINGS}</b> उल्लंघन के बाद <b>BAN</b> कर दिया गया है।\n<b>कारण:</b> {reason}",
                parse_mode="HTML"
            )
        except Exception as e:
            bot.send_message(
                chat_id,
                f"⚠️ {mention} को Ban करने में समस्या आई (कृपया बॉट को Ban परमिशन दें): <code>{e}</code>",
                parse_mode="HTML"
            )
    else:
        bot.send_message(
            chat_id,
            f"⚠️ <b>चेतावनी ({strikes}/{MAX_WARNINGS})</b> — {mention}\n"
            f"<b>कारण:</b> {reason}\n"
            f"<i>3वीं बार उल्लंघन होने पर सीधे Ban कर दिया जाएगा।</i>",
            parse_mode="HTML"
        )

def check_bio_violation(chat_id, user, message_id):
    try:
        chat_info = bot.get_chat(user.id)
        bio = getattr(chat_info, "bio", "") or ""
        if has_link_or_bot(bio):
            apply_strike(
                chat_id,
                user,
                "प्रोफ़ाइल Bio में लिंक या बॉट URL/यूज़रनेम पाया गया",
                message_id
            )
            return True
    except Exception:
        pass
    return False

def check_cross_account_spam(message):
    chat_id = message.chat.id
    user = message.from_user
    norm = normalize_text(message.text or message.caption or "")

    if not norm or len(norm) < 4:
        return False

    now = time.time()
    q = recent_msgs_db[chat_id][norm]

    # पुराने मैसेज हटाएं
    while q and (now - q[0][2]) > SPAM_WINDOW_SEC:
        q.popleft()

    q.append((user.id, message.message_id, now))

    # अलग-अलग अकाउंट से एक ही मैसेज की जांच
    distinct_users = {item[0] for item in q}
    if len(distinct_users) >= 2:
        entries = list(q)
        q.clear()
        for uid, mid, _ in entries:
            safe_delete_message(chat_id, mid)
            try:
                u_obj = bot.get_chat_member(chat_id, uid).user
            except Exception:
                u_obj = user
            apply_strike(
                chat_id,
                u_obj,
                "अलग-अलग अकाउंट से एक ही स्पैम मैसेज भेजना",
                None
            )
        return True

    return False

def build_panel_keyboard(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📊 कुल वॉर्निंग्स", callback_data=f"stats:{chat_id}"),
        types.InlineKeyboardButton("🔄 वॉर्निंग रीसेट", callback_data=f"reset:{chat_id}"),
        types.InlineKeyboardButton("✖️ बंद करें", callback_data=f"close:{chat_id}")
    )
    return kb

@bot.message_handler(commands=["start"])
def handle_start(message):
    bot.reply_to(
        message,
        "🛡️ <b>Advance Group Protector Bot</b> सक्रिय है!\n\n"
        "• बायो में लिंक या बॉट URL पर 3 वॉर्निंग के बाद <b>Ban</b>\n"
        "• ग्रुप में लिंक या बॉट यूज़रनेम पर 3 वॉर्निंग के बाद <b>Ban</b>\n"
        "• अलग-अलग अकाउंट से एक ही मैसेज पर 3 वॉर्निंग के बाद <b>Ban</b>\n\n"
        "ग्रुप में बॉट को <b>Admin</b> (Delete Messages + Ban Users परमिशन) बनाएं और <code>/panel</code> भेजें।",
        parse_mode="HTML"
    )

@bot.message_handler(commands=["panel"])
def handle_panel(message):
    if message.chat.type == "private":
        bot.reply_to(message, "कृपया यह कमांड किसी ग्रुप में भेजें।")
        return
    if not is_admin(message.chat.id, message.from_user.id):
        bot.reply_to(message, "❌ सिर्फ ग्रुप एडमिन ही कंट्रोल पैनल खोल सकते हैं।")
        return

    bot.send_message(
        message.chat.id,
        "🛡️ <b>Group Protector कंट्रोल पैनल</b>",
        parse_mode="HTML",
        reply_markup=build_panel_keyboard(message.chat.id)
    )

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    parts = call.data.split(":")
    if len(parts) != 2:
        return
    action, chat_id_str = parts
    chat_id = int(chat_id_str)

    if not is_admin(chat_id, call.from_user.id):
        bot.answer_callback_query(call.id, "❌ सिर्फ एडमिन ही यह बटन दबा सकते हैं।", show_alert=True)
        return

    if action == "stats":
        active_warns = sum(1 for c in warnings_db[chat_id].values() if c > 0)
        bot.answer_callback_query(
            call.id,
            f"अधिकतम वॉर्निंग: {MAX_WARNINGS}\nवॉर्निंग वाले यूज़र्स: {active_warns}",
            show_alert=True
        )
    elif action == "reset":
        warnings_db[chat_id].clear()
        recent_msgs_db[chat_id].clear()
        bot.answer_callback_query(call.id, "✅ सभी वॉर्निंग्स रीसेट कर दी गईं!", show_alert=True)
    elif action == "close":
        safe_delete_message(chat_id, call.message.message_id)

@bot.message_handler(func=lambda m: True, content_types=["text", "photo", "video", "document", "animation", "sticker"])
def monitor_group_messages(message):
    if message.chat.type not in ["group", "supergroup"]:
        return

    user = message.from_user
    if not user or user.is_bot:
        return

    # ग्रुप एडमिन को छूट
    if is_admin(message.chat.id, user.id):
        return

    # 1. बायो में लिंक या बॉट URL/यूज़रनेम चेक
    if check_bio_violation(message.chat.id, user, message.message_id):
        return

    content = message.text or message.caption or ""

    # 2. मैसेज में लिंक या बॉट यूज़रनेम चेक
    if has_link_or_bot(content):
        apply_strike(
            message.chat.id,
            user,
            "ग्रुप में लिंक या बॉट यूज़रनेम भेजना",
            message.message_id
        )
        return

    # 3. अलग-अलग अकाउंट से एक ही मैसेज (स्पैम) चेक
    if check_cross_account_spam(message):
        return

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("Advance Group Protector Bot chal raha hai...")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
