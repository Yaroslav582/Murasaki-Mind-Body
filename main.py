import logging
import sqlite3
import os
import asyncio
import aiohttp
from datetime import datetime, timedelta
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# -----------------------------
# 🔑 НАСТРОЙКИ
# -----------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]  # ← из Groq
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DB_NAME = "murasaki.db"

# -----------------------------
# 🗃️ БАЗА ДАННЫХ
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            goal TEXT DEFAULT '',
            interests TEXT DEFAULT ''
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            completed INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

# -----------------------------
# 🤖 GROQ: ГЕНЕРАЦИЯ
# -----------------------------
async def groq_generate(prompt: str, max_tokens: int = 250):
    payload = {
        "model": "llama3-70b-8192",  # мощная, бесплатная, поддерживает русский
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_URL, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                else:
                    error = await resp.text()
                    logging.error(f"Groq error {resp.status}: {error}")
                    return "Не удалось сгенерировать. Попробуйте позже."
    except Exception as e:
        logging.error(f"Groq exception: {e}")
        return "Ошибка подключения."

# -----------------------------
# 📜 КОМАНДЫ
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        "💜 Привет! Я Murasaki — твой ИИ-коуч.\n\n"
        "Напишите:\n"
        "• 'Тренировка'\n• 'Идея'\n• 'Рецепт'"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    clean = re.sub(r'[^\w\s]', '', text.lower())
    
    # Инициализация
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
    
    if "тренировка" in clean:
        await update.message.reply_text("Создаю тренировку...")
        prompt = "Составь короткую (20-30 мин) тренировку для общего физического развития. Формат: упражнение - повторения, отдых, совет по технике. Только тренировка, на русском."
        result = await groq_generate(prompt)
        await update.message.reply_text(f"💪 **Тренировка**:\n\n{result}")
        
    elif "идея" in clean:
        await update.message.reply_text("Генерирую идею...")
        prompt = "Придумай одну креативную, реалистичную идею для личного проекта или саморазвития. 1-2 предложения, на русском, без вступлений."
        result = await groq_generate(prompt)
        await update.message.reply_text(f"💡 **Идея**:\n\n{result}")
        
    elif "рецепт" in clean:
        await update.message.reply_text("Готовлю рецепт...")
        prompt = "Придумай простой рецепт на 500 ккал: готовка до 20 минут, доступные продукты, укажи БЖУ. Только рецепт, на русском."
        result = await groq_generate(prompt)
        await update.message.reply_text(f"🥗 **Рецепт**:\n\n{result}")
        
    else:
        await update.message.reply_text(
            "💭 Попробуйте:\n"
            "• 'Тренировка'\n• 'Идея'\n• 'Рецепт'"
        )

# -----------------------------
# 🚀 ЗАПУСК
# -----------------------------
def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    print("✅ Murasaki Bot запущен на Groq!")
    app.run_polling()

if __name__ == "__main__":
    main()
