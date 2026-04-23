import os
import json
import logging
import sqlite3
from datetime import datetime, timedelta
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, PreCheckoutQueryHandler,
    filters, ContextTypes
)

# ========== НАСТРОЙКИ ==========
TELEGRAM_TOKEN = "8734467499:AAH6fiS95Vi4XCvodNwH8nWI-e0FKB8Hupk"

# API ключ Gemini (получил на aistudio.google.com)
GEMINI_API_KEY = "AIzaSyDIM7bUaWR5OI9hnZRnPlyeB9m-4G1BfG0"

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Цены (в рублях и в Telegram Stars)
PRICE_PER_READING = 50
SUBSCRIPTION_PRICE = 300
STARS_PER_READING = 25
STARS_SUBSCRIPTION = 150

# Системный промпт для таролога (теперь передаётся в Gemini)
TAROT_SYSTEM_PROMPT = """
Ты — опытный таролог, работающий с классической колодой Райдера‑Уэйта.

Правила:
1. Перед каждым ответом описывай процесс: «тасую колоду», «вытягиваю карту».
2. Всегда называй конкретные карты (например, «Восьмёрка Кубков», «Башня», «Солнце»).
3. Говори честно, но без жестокости.
4. В конце каждого ответа давай короткий совет.
5. Если вопрос неясный — вытягивай карту «Уточнение».
6. Стиль — спокойный, чуть мистический, без пафоса.
7. Отвечай на русском языке.
"""

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ========== РАБОТА С БАЗОЙ ДАННЫХ ==========
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

# ========== ФУНКЦИЯ ЗАПРОСА К GEMINI ==========
async def ask_gemini(user_message: str, conversation_history: list) -> str:
    """Отправляет запрос к Google Gemini API с историей диалога"""
    
    # Формируем полный промпт: системный + история + новый вопрос
    full_prompt = TAROT_SYSTEM_PROMPT + "\n\n"
    
    # Добавляем историю диалога (последние 10 сообщений)
    if conversation_history:
        for msg in conversation_history[-10:]:  # берём последние 10 сообщений для контекста
            role = "Пользователь" if msg["role"] == "user" else "Таролог"
            full_prompt += f"{role}: {msg['content']}\n"
    
    full_prompt += f"\nПользователь: {user_message}\n\nТаролог:"
    
    try:
        # Используем бесплатную модель Gemini 1.5 Flash
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Генерируем ответ
        response = model.generate_content(
            full_prompt,
            generation_config={
                "temperature": 0.8,
                "max_output_tokens": 1500,
                "top_p": 0.95
            }
        )
        
        if response.text:
            return response.text
        else:
            return "🔮 *Карты молчат...* Попробуй задать вопрос иначе."
    
    except Exception as e:
        logging.error(f"Gemini API error: {e}")
        return "⚠️ Связь с оракулом временно прервалась. Попробуй через минуту."

# ========== КЛАВИАТУРЫ ДЛЯ ОПЛАТЫ ==========
def get_payment_keyboard():
    keyboard = [
        [InlineKeyboardButton("💰 1 расклад — 50 ₽ (Telegram Stars)", callback_data="pay_star_reading")],
        [InlineKeyboardButton("🌟 Подписка на месяц — 300 ₽ (Telegram Stars)", callback_data="pay_star_subscription")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_payment")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== КОМАНДЫ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    remaining = 3 - user["free_readings_used"]
    subscription_active = has_active_subscription(user_id)
    
    status_text = ""
    if subscription_active:
        status_text = "✅ *У вас активна подписка* — неограниченные расклады!"
    else:
        status_text = f"📊 *Бесплатных раскладов осталось:* {remaining} из 3"
    
    await update.message.reply_text(
        f"🔮 *Привет, я твой личный таролог!*\n\n"
        f"{status_text}\n\n"
        f"Просто задай вопрос — и я вытяну карты.\n\n"
        f"После 3 бесплатных раскладов:\n"
        f"• 50 ₽ за расклад\n"
        f"• 300 ₽ за безлимит на месяц\n\n"
        f"Команды:\n"
        f"/start — это сообщение\n"
        f"/status — проверить остаток\n"
        f"/subscribe — купить подписку\n"
        f"/clear — очистить историю",
        parse_mode="Markdown"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    remaining = 3 - user["free_readings_used"]
    subscription_active = has_active_subscription(user_id)
    
    if subscription_active:
        await update.message.reply_text(
            "✅ *У вас активна подписка!*\n\n"
            "Вы можете делать неограниченное количество раскладов.\n"
            "Просто задавай вопросы — я отвечу.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"📊 *Ваш статус:*\n\n"
            f"Бесплатных раскладов использовано: {user['free_readings_used']} из 3\n"
            f"Осталось: {remaining}\n"
            f"Всего раскладов: {user['total_readings']}\n\n"
            f"Чтобы купить подписку — /subscribe",
            parse_mode="Markdown"
        )

async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 *Варианты оплаты:*\n\n"
        "• 50 ₽ — 1 расклад (через Telegram Stars)\n"
        "• 300 ₽ — безлимит на месяц (через Telegram Stars)\n\n"
        "Telegram Stars — внутренняя валюта Telegram.\n\n"
        "Выбери вариант:",
        parse_mode="Markdown",
        reply_markup=get_payment_keyboard()
    )

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["history"] = []
    await update.message.reply_text(
        "✨ *История раскладов очищена.* Начинаем новый сеанс.",
        parse_mode="Markdown"
    )

# ========== ОБРАБОТКА ПЛАТЕЖЕЙ ==========
async def payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    data = query.data
    
    if data == "cancel_payment":
        await query.edit_message_text("❌ Оплата отменена. Если передумаешь — /subscribe")
        return
    
    if data == "pay_star_reading":
        title = "🔮 Расклад Таро"
        description = "Один полный расклад карт Таро с ответом на твой вопрос"
        amount = STARS_PER_READING
        payload = "reading_1"
    elif data == "pay_star_subscription":
        title = "🌟 Подписка на месяц Таро"
        description = "Безлимитные расклады на 30 дней"
        amount = STARS_SUBSCRIPTION
        payload = "subscription_1month"
    else:
        return
    
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice("Оплата", amount)],
        start_parameter="tarot_payment"
    )

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payment = update.message.successful_payment
    
    if payment.invoice_payload.startswith("reading"):
        context.user_data["paid_reading_available"] = True
        log_payment(user_id, PRICE_PER_READING, "single")
        await update.message.reply_text(
            "✅ *Оплата прошла успешно!*\n\n"
            "Ты купил 1 расклад. Напиши свой вопрос — и я сразу вытяну карты.",
            parse_mode="Markdown"
        )
    
    elif payment.invoice_payload.startswith("subscription"):
        activate_subscription(user_id, 1)
        log_payment(user_id, SUBSCRIPTION_PRICE, "subscription")
        await update.message.reply_text(
            "🌟 *Подписка активирована!*\n\n"
            "Теперь у тебя безлимитные расклады на 30 дней.\n"
            "Задавай любые вопросы — я всегда рядом.",
            parse_mode="Markdown"
        )

# ========== ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.effective_user.id
    
    paid_available = context.user_data.get("paid_reading_available", False)
    can_do, reason = can_do_reading(user_id)
    
    if not can_do and not paid_available:
        await update.message.reply_text(
            "🔮 *Лимит бесплатных раскладов исчерпан.*\n\n"
            "Ты использовал все 3 бесплатных расклада.\n\n"
            "Чтобы продолжить:\n"
            "• 50 ₽ за расклад\n"
            "• 300 ₽ за безлимит на месяц\n\n"
            "Напиши /subscribe — выбери вариант оплаты.",
            parse_mode="Markdown"
        )
        return
    
    if "history" not in context.user_data:
        context.user_data["history"] = []
    history = context.user_data["history"]
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    # Используем Gemini вместо DeepSeek
    answer = await ask_gemini(user_message, history)
    
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": answer})
    if len(history) > 20:
        history.pop(0)
        history.pop(0)
    
    if paid_available:
        context.user_data["paid_reading_available"] = False
        await update.message.reply_text(
            f"{answer}\n\n---\n💎 *Использован оплаченный расклад.*\nКупить ещё — /subscribe",
            parse_mode="Markdown"
        )
    elif reason == "free":
        user = get_user(user_id)
        remaining = 2 - user["free_readings_used"]
        update_user_readings(user_id)
        await update.message.reply_text(
            f"{answer}\n\n---\n📊 *Бесплатных раскладов осталось:* {remaining} из 3\nКупить подписку — /subscribe",
            parse_mode="Markdown"
        )
    else:
        update_user_readings(user_id)
        await update.message.reply_text(
            f"{answer}\n\n---\n🌟 *Подписка активна* — задавай следующий вопрос!",
            parse_mode="Markdown"
        )

# ========== ЗАПУСК ==========
def main():
    init_db()
    
    if TELEGRAM_TOKEN == "7123456789:AAEпримерныйтокен_который_дал_BotFather":
        print("❌ ОШИБКА: Замени TELEGRAM_TOKEN на реальный токен от @BotFather")
        return
    
    if GEMINI_API_KEY == "AIzaSyD3fG5hJkL9mNpQrStUvWxYz1234567890":
        print("❌ ОШИБКА: Замени GEMINI_API_KEY на реальный ключ с aistudio.google.com")
        return
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CallbackQueryHandler(payment_callback))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🔮 Бот-таролог с Gemini API запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
