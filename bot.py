import os
import time
import threading
from flask import Flask
import telebot
from telebot import types

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable set nahi hai! Render Environment me BOT_TOKEN dalein.")

bot = telebot.TeleBot(BOT_TOKEN)

app = Flask(__name__)

@app.route("/")
def home():
    return "UserTagger Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

group_states = {}

def get_state(chat_id):
    if chat_id not in group_states:
        group_states[chat_id] = {
            "running": False,
            "batch_size": 5,
            "delay": 2.0,
            "users": {},
            "thread": None
        }
    return group_states[chat_id]

def format_mention(user_id, name):
    clean_name = (name or "User").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{clean_name}</a>'

def build_panel_keyboard(chat_id):
    st = get_state(chat_id)
    kb = types.InlineKeyboardMarkup(row_width=2)

    status_btn = types.InlineKeyboardButton(
        "⏹ रोकें (Stop)" if st["running"] else "▶️ टैग शुरू करें",
        callback_data=f"toggle:{chat_id}"
    )
    batch_btn = types.InlineKeyboardButton(
        f"👥 बैच: {st['batch_size']}",
        callback_data=f"batch:{chat_id}"
    )
    delay_btn = types.InlineKeyboardButton(
        f"⏱ ब्रेक: {st['delay']}s",
        callback_data=f"delay:{chat_id}"
    )
    stats_btn = types.InlineKeyboardButton(
        f"📊 यूज़र्स: {len(st['users'])}",
        callback_data=f"stats:{chat_id}"
    )
    close_btn = types.InlineKeyboardButton(
        "✖️ बंद करें",
        callback_data=f"close:{chat_id}"
    )

    kb.add(status_btn, stats_btn)
    kb.add(batch_btn, delay_btn)
    kb.add(close_btn)
    return kb

def run_tagging(chat_id, custom_text, reply_to_message_id):
    st = get_state(chat_id)
    user_list = list(st["users"].values())

    if not user_list:
        st["running"] = False
        try:
            bot.send_message(chat_id, "⚠️ अभी ग्रुप में टैग करने के लिए कोई रिकॉर्डेड यूज़र नहीं मिला है। जैसे ही सदस्य मैसेज या जॉइन करेंगे, लिस्ट बन जाएगी।")
        except Exception:
            pass
        return

    batch_size = st["batch_size"]
    delay = st["delay"]

    for i in range(0, len(user_list), batch_size):
        if not st["running"]:
            try:
                bot.send_message(chat_id, "🛑 टैगिंग रोक दी गई है।")
            except Exception:
                pass
            return

        chunk = user_list[i:i + batch_size]
        mentions = " ".join(format_mention(u["id"], u["name"]) for u in chunk)

        text_parts = []
        if custom_text:
            text_parts.append(custom_text)
        text_parts.append(mentions)
        final_text = "\n\n".join(text_parts)

        try:
            bot.send_message(
                chat_id,
                final_text,
                parse_mode="HTML",
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True
            )
        except Exception as e:
            print("Send error:", e)
            time.sleep(3)

        time.sleep(delay)

    st["running"] = False
    try:
        bot.send_message(chat_id, "✅ सभी उपलब्ध यूज़र्स को टैग कर दिया गया है।")
    except Exception:
        pass

@bot.message_handler(commands=["start"])
def handle_start(message):
    bot.reply_to(
        message,
        "👋 नमस्ते! मैं **UserTagger Bot** हूँ।\n\n"
        "मुझे अपने ग्रुप में जोड़ें और **Admin** बनाएं:\n"
        "• `/panel` — बटन कंट्रोल पैनल खोलें\n"
        "• `/tag आपका मैसेज` — बैच में यूज़र्स टैग करें\n"
        "• `/stop` — टैगिंग रोकें",
        parse_mode="Markdown"
    )

@bot.message_handler(content_types=["new_chat_members", "text", "photo", "video", "document", "sticker"])
def collect_users(message):
    if message.chat.type not in ["group", "supergroup"]:
        return

    st = get_state(message.chat.id)

    if message.from_user and not message.from_user.is_bot:
        full_name = message.from_user.first_name or ""
        if message.from_user.last_name:
            full_name += f" {message.from_user.last_name}"
        st["users"][message.from_user.id] = {
            "id": message.from_user.id,
            "name": full_name.strip() or "User"
        }

    if message.new_chat_members:
        for member in message.new_chat_members:
            if not member.is_bot:
                name = member.first_name or ""
                if member.last_name:
                    name += f" {member.last_name}"
                st["users"][member.id] = {
                    "id": member.id,
                    "name": name.strip() or "User"
                }

    if message.text and message.text.startswith("/tag"):
        parts = message.text.split(maxsplit=1)
        custom_text = parts[1].strip() if len(parts) > 1 else ""
        reply_id = message.reply_to_message.message_id if message.reply_to_message else None

        if st["running"]:
            bot.reply_to(message, "⏳ टैगिंग पहले से चल रही है। रोकने के लिए /stop या पैनल का इस्तेमाल करें।")
            return

        st["running"] = True
        t = threading.Thread(
            target=run_tagging,
            args=(message.chat.id, custom_text, reply_id),
            daemon=True
        )
        st["thread"] = t
        t.start()

    elif message.text and message.text.startswith("/stop"):
        st["running"] = False
        bot.reply_to(message, "⏹ रोकने का सिग्नल भेज दिया गया है।")

    elif message.text and message.text.startswith("/panel"):
        bot.send_message(
            message.chat.id,
            "🎛 **टैगर कंट्रोल पैनल**",
            parse_mode="Markdown",
            reply_markup=build_panel_keyboard(message.chat.id)
        )

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    data = call.data.split(":")
    if len(data) != 2:
        return

    action, chat_id_str = data
    chat_id = int(chat_id_str)
    st = get_state(chat_id)

    try:
        member = bot.get_chat_member(chat_id, call.from_user.id)
        if member.status not in ["administrator", "creator"]:
            bot.answer_callback_query(call.id, "❌ सिर्फ एडमिन ही यह बटन इस्तेमाल कर सकते हैं।", show_alert=True)
            return
    except Exception:
        bot.answer_callback_query(call.id, "⚠️ एडमिन स्टेटस चेक नहीं हो सका।", show_alert=True)
        return

    if action == "toggle":
        if st["running"]:
            st["running"] = False
            bot.answer_callback_query(call.id, "⏹ टैगिंग रोकी जा रही है...")
        else:
            st["running"] = True
            t = threading.Thread(
                target=run_tagging,
                args=(chat_id, "", None),
                daemon=True
            )
            st["thread"] = t
            t.start()
            bot.answer_callback_query(call.id, "▶️ टैगिंग शुरू हो गई!")

    elif action == "batch":
        sizes = [3, 5, 8, 10]
        idx = sizes.index(st["batch_size"]) if st["batch_size"] in sizes else 1
        st["batch_size"] = sizes[(idx + 1) % len(sizes)]
        bot.answer_callback_query(call.id, f"बैच साइज: {st['batch_size']}")

    elif action == "delay":
        delays = [1.5, 2.0, 3.0, 5.0]
        idx = delays.index(st["delay"]) if st["delay"] in delays else 1
        st["delay"] = delays[(idx + 1) % len(delays)]
        bot.answer_callback_query(call.id, f"ब्रेक टाइम: {st['delay']}s")

    elif action == "stats":
        bot.answer_callback_query(call.id, f"कुल सेव यूज़र्स: {len(st['users'])}", show_alert=True)
        return

    elif action == "close":
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        return

    try:
        bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=call.message.message_id,
            reply_markup=build_panel_keyboard(chat_id)
        )
    except Exception:
        pass

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    print("Bot aur Flask server chal rahe hain...")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
