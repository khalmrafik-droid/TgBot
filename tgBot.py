"""
Telegram бот-таролог для BotHost.ru
API-ключи берутся из переменных окружения:
- TELEGRAM_TOKEN
- OPENROUTER_API_KEY
"""

import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, PreCheckoutQueryHandler,
    filters, ContextTypes
)

# ========== ЗАГРУЗКА КЛЮЧЕЙ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "НЕ_УСТАНОВЛЕН")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "НЕ_УСТАНОВЛЕН")

# ========== НАСТРОЙКИ ==========
PRICE_PER_READING = 50
SUBSCRIPTION_PRICE = 300
STARS_PER_READING = 25
STARS_SUBSCRIPTION = 150

SYSTEM_PROMPT = """Ты — опытный таролог, работающий с классической колодой Райдера‑Уэйта.

Правила:
1. Перед каждым ответом описывай процесс: «тасую колоду», «вытягиваю карту».
2. Всегда называй конкретные карты (например, «Восьмёрка Кубков», «Башня», «Солнце»).
3. Говори честно, но без жестокости.
4. В конце каждого ответа давай короткий совет.
5. Стиль — спокойный, чуть мистический, без пафоса."""

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect('tarot_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        free_readings_used INTEGER DEFAULT 0,
        subscription_end TEXT,
        total_readings INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount INTEGER,
        payment_type TEXT,
        date TEXT
    )''')
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('tarot_bot.db')
    c = conn.cursor()
    c.execute("SELECT free_readings_used, subscription_end, total_readings FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "free_readings_used": row[0],
            "subscription_end": row[1],
            "total_readings": row[2]
        }
    else:
        conn = sqlite3.connect('tarot_bot.db')
        c = conn.cursor()
        c.execute("INSERT INTO users (user_id, free_readings_used, subscription_end, total_readings) VALUES (?, 0, NULL, 0)", (user_id,))
        conn.commit()
        conn.close()
        return {"free_readings_used": 0, "subscription_end": None, "total_readings": 0}

def update_user_readings(user_id):
    conn = sqlite3.connect('tarot_bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET free_readings_used = free_readings_used + 1, total_readings = total_readings + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def has_active_subscription(user_id):
    conn = sqlite3.connect('tarot_bot.db')
    c = conn.cursor()
    c.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        end_date = datetime.fromisoformat(row[0])
        return end_date > datetime.now()
    return False

def activate_subscription(user_id, months=1):
    conn = sqlite3.connect('tarot_bot.db')
    c = conn.cursor()
    c.execute("SELECT subscription_end FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row and row[0]:
        current_end = datetime.fromisoformat(row[0])
        if current_end > datetime.now():
            new_end = current_end + timedelta(days=30 * months)
        else:
            new_end = datetime.now() + timedelta(days=30 * months)
    else:
        new_end = datetime.now() + timedelta(days=30 * months)
    c.execute("UPDATE users SET subscription_end = ? WHERE user_id = ?", (new_end.isoformat(), user_id))
    conn.commit()
    conn.close()

def log_payment(user_id, amount, payment_type):
    conn = sqlite3.connect('tarot_bot.db')
    c = conn.cursor()
    c.execute("INSERT INTO payments (user_id, amount, payment_type, date) VALUES (?, ?, ?, ?)",
              (user_id, amount, payment_type, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def can_do_reading(user_id):
    user = get_user(user_id)
    if has_active_subscription(user_id):
        return True, "subscription"
    if user["free_readings_used"] < 3:
        return True, "free"
    return False, "limited"

# ========== ЗАПРОС К OPENROUTER ==========
async def ask_deepseek(user_message: str, conversation_history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_message})
    
      try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://t.me/tarot_bot",
                "X-Title": "Tarot Bot"
            },
            json={
                "model": "google/gemini-2.0-flash-lite-preview-02-05:free",  # ← новая модель
                "messages": messages,
                "temperature": 0.8,
                "max_tokens": 1500
            },
            timeout=60
        )
        
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            return f"🔮 Ошибка OpenRouter: {response.status_code}\n{response.text[:200]}"
    except Exception as e:
        return f"⚠️ Ошибка: {str(e)[:100]}"

# ========== КОМАНДЫ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    remaining = 3 - user["free_readings_used"]
    subscription_active = has_active_subscription(user_id)
    status_text = "✅ *У вас активна подписка* — неограниченные расклады!" if subscription_active else f"📊 *Бесплатных раскладов осталось:* {remaining} из 3"
    await update.message.reply_text(
        f"🔮 *Привет, я твой личный таролог!*\n\n{status_text}\n\nПросто задай вопрос — и я вытяну карты.\n\nПосле 3 бесплатных раскладов:\n• 50 ₽ за расклад\n• 300 ₽ за безлимит на месяц\n\nКоманды:\n/start — это сообщение\n/status — проверить остаток\n/subscribe — купить подписку\n/clear — очистить историю",
        parse_mode="Markdown"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    remaining = 3 - user["free_readings_used"]
    subscription_active = has_active_subscription(user_id)
    if subscription_active:
        await update.message.reply_text("✅ *У вас активна подписка!*\n\nВы можете делать неограниченное количество раскладов.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"📊 *Ваш статус:*\n\nБесплатных раскладов использовано: {user['free_readings_used']} из 3\nОсталось: {remaining}\nВсего раскладов: {user['total_readings']}\n\nЧтобы купить подписку — /subscribe", parse_mode="Markdown")

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 1 расклад — 50 ₽ (Telegram Stars)", callback_data="pay_star_reading")],
        [InlineKeyboardButton("🌟 Подписка на месяц — 300 ₽ (Telegram Stars)", callback_data="pay_star_subscription")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")]
    ]
    await update.message.reply_text("🌟 *Варианты оплаты:*\n\n• 50 ₽ — 1 расклад\n• 300 ₽ — безлимит на месяц\n\nВыбери вариант:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text("✨ *История раскладов очищена.* Начинаем новый сеанс.", parse_mode="Markdown")

async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "cancel_payment":
        await query.edit_message_text("❌ Оплата отменена.")
        return
    if data == "pay_star_reading":
        title, description, amount, payload = "🔮 Расклад Таро", "Один полный расклад карт Таро", STARS_PER_READING, "reading_1"
    elif data == "pay_star_subscription":
        title, description, amount, payload = "🌟 Подписка на месяц Таро", "Безлимитные расклады на 30 дней", STARS_SUBSCRIPTION, "subscription_1month"
    else:
        return
    await context.bot.send_invoice(chat_id=update.effective_chat.id, title=title, description=description, payload=payload, provider_token="", currency="XTR", prices=[LabeledPrice("Оплата", amount)], start_parameter="tarot_payment")

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment = update.message.successful_payment
    if payment.invoice_payload.startswith("reading"):
        context.user_data["paid_reading_available"] = True
        log_payment(user_id, PRICE_PER_READING, "single")
        await update.message.reply_text("✅ *Оплата прошла успешно!*\n\nТы купил 1 расклад. Напиши свой вопрос.", parse_mode="Markdown")
    elif payment.invoice_payload.startswith("subscription"):
        activate_subscription(user_id, 1)
        log_payment(user_id, SUBSCRIPTION_PRICE, "subscription")
        await update.message.reply_text("🌟 *Подписка активирована!*\n\nТеперь у тебя безлимитные расклады на 30 дней.", parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.effective_user.id
    paid_available = context.user_data.get("paid_reading_available", False)
    can_do, reason = can_do_reading(user_id)
    
    if not can_do and not paid_available:
        await update.message.reply_text("🔮 *Лимит бесплатных раскладов исчерпан.*\n\nНапиши /subscribe — выбери вариант оплаты.", parse_mode="Markdown")
        return
    
    if "history" not in context.user_data:
        context.user_data["history"] = []
    history = context.user_data["history"]
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    answer = await ask_deepseek(user_message, history)
    
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 20:
        history.pop(0)
        history.pop(0)
    
    if paid_available:
        context.user_data["paid_reading_available"] = False
        await update.message.reply_text(f"{answer}\n\n---\n💎 *Использован оплаченный расклад.*", parse_mode="Markdown")
    elif reason == "free":
        user = get_user(user_id)
        remaining = 2 - user["free_readings_used"]
        update_user_readings(user_id)
        await update.message.reply_text(f"{answer}\n\n---\n📊 *Бесплатных раскладов осталось:* {remaining} из 3", parse_mode="Markdown")
    else:
        update_user_readings(user_id)
        await update.message.reply_text(f"{answer}\n\n---\n🌟 *Подписка активна* — задавай следующий вопрос!", parse_mode="Markdown")

# ========== ЗАПУСК БОТА ==========
def main():
    init_db()
    
    # Проверка наличия ключей
    if TELEGRAM_TOKEN == "НЕ_УСТАНОВЛЕН":
        print("❌ ОШИБКА: Переменная TELEGRAM_TOKEN не установлена!")
        print("   Добавь её в переменные окружения на BotHost.ru")
        return
    
    if OPENROUTER_API_KEY == "НЕ_УСТАНОВЛЕН":
        print("❌ ОШИБКА: Переменная OPENROUTER_API_KEY не установлена!")
        print("   Добавь её в переменные окружения на BotHost.ru")
        return
    
    # Создаём приложение
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CallbackQueryHandler(payment_callback))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🔮 Бот-таролог запущен на BotHost.ru!")
    app.run_polling()

if __name__ == "__main__":
    main()
