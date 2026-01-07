import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import os
import sys
import asyncio
import aiohttp
import base64
import urllib.parse
from datetime import datetime, timedelta, time as dtime
import re
import traceback
from functools import wraps
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, ContextTypes, filters
)
from telegram.error import TelegramError, NetworkError, TimedOut

# ============================================================
# === НАСТРОЙКИ ===
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")
GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY", "")
PROVIDER_TOKEN = os.environ.get("PROVIDER_TOKEN", "")
CRYPTO_BOT_TOKEN = os.environ.get("CRYPTO_BOT_TOKEN", "")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
CRYPTO_BOT_API = "https://pay.crypt.bot/api"

# Railway Volume
RAILWAY_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
if RAILWAY_VOLUME and os.path.exists(RAILWAY_VOLUME):
    DATA_DIR = RAILWAY_VOLUME
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__)) or "."

DB_NAME = os.path.join(DATA_DIR, "sport.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")
VOICE_DIR = os.path.join(DATA_DIR, "voice_temp")

for d in [LOG_DIR, VOICE_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

# ⚠️ ЗАМЕНИ НА СВОЙ TELEGRAM ID!
ADMIN_IDS = [123456789]

REQUIRED_CHANNEL = "@Murasaki_lab"

# Голоса
VOICE_MAP = {
    'ru': 'ru-RU-DmitryNeural',
    'en': 'en-US-ChristopherNeural',
    'ko': 'ko-KR-HyunsuNeural'
}

# Цены
PREMIUM_PRICE_RUB = 99
PREMIUM_PRICE_STARS = 50
PREMIUM_PRICE_USDT = 1.5

# Реферальные бонусы
REFERRER_BONUS_DAYS = 7  # Дней тому кто пригласил
REFERRED_BONUS_DAYS = 3  # Дней тому кого пригласили

SYSTEM_PROMPT = """Ты персональный AI-тренер Murasaki Sport.
Отвечай ТОЛЬКО на вопросы о спорте, тренировках, питании, здоровье, фитнесе.
Стиль: дружелюбный, мотивирующий. Ответы: 3-5 предложений. Язык: русский."""

FITNESS_KEYWORDS = [
    'тренировк', 'упражнен', 'качать', 'накачать', 'спорт', 'фитнес',
    'присед', 'отжим', 'подтягив', 'планка', 'бег', 'кардио', 'силов',
    'мышц', 'бицепс', 'трицепс', 'пресс', 'спина', 'ноги', 'руки', 'плечи',
    'грудь', 'ягодиц', 'растяж', 'разминк', 'заминк', 'жим', 'тяга',
    'гантел', 'штанг', 'турник', 'брусья', 'гиря', 'тренажер',
    'питани', 'диет', 'калор', 'ккал', 'белок', 'белки', 'углевод', 'жиры',
    'кбжу', 'рецепт', 'еда', 'продукт', 'витамин', 'протеин', 'завтрак',
    'обед', 'ужин', 'перекус', 'похуд', 'набрать', 'сброс', 'вес', 'масса',
    'здоров', 'сон', 'восстановлен', 'боль', 'травм', 'растян', 'суста',
    'спин', 'осанк', 'гибкост', 'выносливост', 'сила', 'энерги',
    'как делать', 'как правильно', 'техника', 'покажи', 'научи', 'помоги',
    'посоветуй', 'подскажи', 'составь', 'программ', 'план',
    'похудеть', 'накачаться', 'подтянуться', 'форма', 'рельеф', 'сушка'
]

# ============================================================
# === ЛОГИРОВАНИЕ ===
# ============================================================
def setup_logging():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers = []

    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, 'bot.log'),
        maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    logger.addHandler(file_handler)

    error_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, 'errors.log'),
        maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    logger.addHandler(error_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(console)

    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)

    return logger

logger = setup_logging()

# ============================================================
# === УТИЛИТЫ ===
# ============================================================
def handle_errors(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}\n{traceback.format_exc()}")
            try:
                if update.message:
                    await update.message.reply_text(f"⚠️ Ошибка: {str(e)[:100]}")
                elif update.callback_query:
                    await update.callback_query.message.reply_text(f"⚠️ Ошибка: {str(e)[:100]}")
            except:
                pass
    return wrapper

def db_connection():
    class DBConnection:
        def __init__(self):
            self.conn = None
        def __enter__(self):
            self.conn = sqlite3.connect(DB_NAME, timeout=30)
            self.conn.row_factory = sqlite3.Row
            return self.conn
        def __exit__(self, *args):
            if self.conn:
                self.conn.commit()
                self.conn.close()
            return False
    return DBConnection()

def is_fitness_question(text: str) -> bool:
    text_lower = text.lower()
    for keyword in FITNESS_KEYWORDS:
        if keyword in text_lower:
            return True
    return False

# ============================================================
# === БАЗА ДАННЫХ ===
# ============================================================
def init_db():
    logger.info(f"Initializing database: {DB_NAME}")

    with db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_premium INTEGER DEFAULT 0,
                premium_until TEXT,
                free_questions INTEGER DEFAULT 5,
                last_reset TEXT,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                referral_used INTEGER DEFAULT 0,
                height INTEGER,
                weight REAL,
                age INTEGER,
                gender TEXT,
                goal TEXT,
                location TEXT,
                equipment TEXT,
                voice_mode INTEGER DEFAULT 0,
                language TEXT DEFAULT 'ru',
                profile_step TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Добавляем колонку referral_used если её нет
        for col, default in [("voice_mode", "0"), ("language", "'ru'"), ("profile_step", "NULL"), ("referral_used", "0")]:
            try:
                cursor.execute(f"ALTER TABLE users ADD COLUMN {col} DEFAULT {default}")
            except:
                pass
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                workout_text TEXT NOT NULL,
                completed INTEGER DEFAULT 0,
                date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                weight REAL,
                date TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                user_id INTEGER PRIMARY KEY,
                total_questions INTEGER DEFAULT 0,
                workouts_completed INTEGER DEFAULT 0,
                recipes_generated INTEGER DEFAULT 0,
                referrals_count INTEGER DEFAULT 0
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exercises (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                aliases TEXT,
                muscles TEXT,
                gif_url TEXT,
                video_url TEXT
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL,
                currency TEXT,
                method TEXT,
                status TEXT DEFAULT 'pending',
                invoice_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("SELECT COUNT(*) FROM exercises")
        if cursor.fetchone()[0] == 0:
            exercises = [
                ("Приседания", "присед,squat,приседы", "ноги, ягодицы", 
                 "https://media.giphy.com/media/1qfKN8Dt0CRdCRxz9q/giphy.gif",
                 "https://www.youtube.com/watch?v=aclHkVaku9U"),
                ("Выпады", "выпад,lunges", "ноги, ягодицы",
                 "https://media.giphy.com/media/l0HlNQ03J5JxX6lva/giphy.gif",
                 "https://www.youtube.com/watch?v=QOVaHwm-Q6U"),
                ("Отжимания", "отжимание,push-up,pushup", "грудь, трицепс",
                 "https://media.giphy.com/media/7YCC7NnFgkUEFOfVNy/giphy.gif",
                 "https://www.youtube.com/watch?v=IODxDxX7oi4"),
                ("Жим лёжа", "жим лежа,bench press", "грудь, трицепс",
                 "https://media.giphy.com/media/7T5wldGkk7XgCyuNUV/giphy.gif",
                 "https://www.youtube.com/watch?v=rT7DgCr-3pg"),
                ("Подтягивания", "подтягивание,pull-up", "спина, бицепс",
                 "https://media.giphy.com/media/3o7TKDnKzLluH40Zzq/giphy.gif",
                 "https://www.youtube.com/watch?v=eGo4IYlbE5g"),
                ("Тяга в наклоне", "тяга штанги,row", "спина, бицепс",
                 "https://media.giphy.com/media/3ohc11UljvpPKWeNva/giphy.gif",
                 "https://www.youtube.com/watch?v=G8l_8chR5BE"),
                ("Планка", "plank,планки", "пресс, кор",
                 "https://media.giphy.com/media/xT8qBvgKeMvMGSJNgA/giphy.gif",
                 "https://www.youtube.com/watch?v=pSHjTRCQxIw"),
                ("Скручивания", "crunches,пресс", "пресс",
                 "https://media.giphy.com/media/l3q2VZLzFKvFTbAlo/giphy.gif",
                 "https://www.youtube.com/watch?v=Xyd_fa5zoEU"),
                ("Подъём на бицепс", "бицепс,curl", "руки, бицепс",
                 "https://media.giphy.com/media/xUOwGmsFStnxzIGC2s/giphy.gif",
                 "https://www.youtube.com/watch?v=ykJmrZ5v0Oo"),
                ("Бёрпи", "burpee,берпи", "всё тело, кардио",
                 "https://media.giphy.com/media/23hPPMRgPxbNBlPQe3/giphy.gif",
                 "https://www.youtube.com/watch?v=TU8QYVW0gDU"),
                ("Становая тяга", "становая,deadlift", "спина, ноги",
                 "https://media.giphy.com/media/3oEjHGr1Fhz0kyv8Ig/giphy.gif",
                 "https://www.youtube.com/watch?v=op9kVnSso6Q"),
            ]
            cursor.executemany(
                "INSERT INTO exercises (name, aliases, muscles, gif_url, video_url) VALUES (?, ?, ?, ?, ?)",
                exercises
            )

    logger.info("Database initialized")

# ============================================================
# === ПОЛЬЗОВАТЕЛИ ===
# ============================================================
def generate_referral_code(user_id: int) -> str:
    import hashlib
    return hashlib.md5(f"{user_id}{datetime.now()}".encode()).hexdigest()[:8]

def get_or_create_user(user_id: int, username: str = None) -> bool:
    """Возвращает True если пользователь новый"""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))

        if not cursor.fetchone():
            today = datetime.now().strftime("%Y-%m-%d")
            ref_code = generate_referral_code(user_id)
            cursor.execute("""
                INSERT INTO users (user_id, username, free_questions, last_reset, referral_code, referral_used)
                VALUES (?, ?, 5, ?, ?, 0)
            """, (user_id, username, today, ref_code))
            cursor.execute("INSERT INTO stats (user_id) VALUES (?)", (user_id,))
            logger.info(f"New user: {user_id}")
            return True
        return False

def get_user_profile(user_id: int) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT height, weight, age, gender, goal, location, equipment FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return {'height': row[0], 'weight': row[1], 'age': row[2], 'gender': row[3],
                    'goal': row[4], 'location': row[5], 'equipment': row[6]}
    return {}

def update_user_profile(user_id: int, **kwargs):
    with db_connection() as conn:
        cursor = conn.cursor()
        fields = [f"{k} = ?" for k, v in kwargs.items() if v is not None]
        values = [v for v in kwargs.values() if v is not None]
        if fields:
            values.append(user_id)
            cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", values)
            logger.info(f"Profile updated: {user_id} -> {kwargs}")

def has_profile(user_id: int) -> bool:
    p = get_user_profile(user_id)
    return bool(p.get('height') and p.get('weight') and p.get('goal'))

def get_profile_step(user_id: int) -> str | None:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT profile_step FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def set_profile_step(user_id: int, step: str | None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET profile_step = ? WHERE user_id = ?", (step, user_id))

def reset_daily_limit(user_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("SELECT last_reset FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row[0] != today:
            cursor.execute("UPDATE users SET free_questions = 5, last_reset = ? WHERE user_id = ?", (today, user_id))

def can_ask_question(user_id: int) -> tuple:
    reset_daily_limit(user_id)
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_premium, premium_until, free_questions FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return False, 0

        is_prem, prem_until, free_q = row
        if is_prem and prem_until:
            try:
                if datetime.now().date() <= datetime.strptime(prem_until, "%Y-%m-%d").date():
                    return True, -1
            except:
                pass
        return (free_q or 0) > 0, free_q or 0

def use_question(user_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET free_questions = free_questions - 1 WHERE user_id = ? AND free_questions > 0", (user_id,))
        cursor.execute("UPDATE stats SET total_questions = total_questions + 1 WHERE user_id = ?", (user_id,))

def get_premium_status(user_id: int) -> dict:
    """Возвращает статус премиума и оставшиеся дни"""
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if not row or not row[1]:
            return {'is_premium': False, 'days_left': 0, 'until_date': None}
        
        try:
            end_date = datetime.strptime(row[1], "%Y-%m-%d").date()
            today = datetime.now().date()
            
            if today <= end_date:
                days_left = (end_date - today).days + 1
                return {
                    'is_premium': True, 
                    'days_left': days_left, 
                    'until_date': row[1]
                }
        except:
            pass
        
        return {'is_premium': False, 'days_left': 0, 'until_date': None}

def is_premium(user_id: int) -> bool:
    return get_premium_status(user_id)['is_premium']

def activate_premium(user_id: int, days: int = 30):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
        current = cursor.fetchone()

        base = datetime.now()
        if current and current[0]:
            try:
                existing_date = datetime.strptime(current[0], "%Y-%m-%d")
                if existing_date > base:
                    base = existing_date
            except:
                pass
        
        end = (base + timedelta(days=days)).strftime("%Y-%m-%d")
        cursor.execute("UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?", (end, user_id))
        logger.info(f"Premium activated: {user_id} for {days} days until {end}")

def process_referral(new_user_id: int, ref_code: str) -> tuple:
    """
    Обрабатывает реферальный код.
    Возвращает (success, referrer_id) или (False, None)
    """
    with db_connection() as conn:
        cursor = conn.cursor()
        
        # Проверяем, не использовал ли уже этот пользователь реферал
        cursor.execute("SELECT referral_used, referred_by FROM users WHERE user_id = ?", (new_user_id,))
        user_row = cursor.fetchone()
        
        if user_row and (user_row[0] == 1 or user_row[1]):
            logger.info(f"User {new_user_id} already used referral")
            return False, None
        
        # Ищем владельца реферального кода
        cursor.execute("SELECT user_id FROM users WHERE referral_code = ?", (ref_code,))
        result = cursor.fetchone()

        if not result:
            logger.info(f"Referral code {ref_code} not found")
            return False, None
        
        referrer_id = result[0]
        
        # Нельзя реферить самого себя
        if referrer_id == new_user_id:
            logger.info(f"User {new_user_id} tried to use own referral")
            return False, None
        
        # Помечаем что реферал использован
        cursor.execute("""
            UPDATE users 
            SET referred_by = ?, referral_used = 1 
            WHERE user_id = ?
        """, (referrer_id, new_user_id))
        
        # Начисляем бонусы
        # Тому кто пригласил - 7 дней
        activate_premium(referrer_id, REFERRER_BONUS_DAYS)
        
        # Тому кого пригласили - 3 дня
        activate_premium(new_user_id, REFERRED_BONUS_DAYS)
        
        # Обновляем статистику рефералов
        cursor.execute("""
            UPDATE stats 
            SET referrals_count = referrals_count + 1 
            WHERE user_id = ?
        """, (referrer_id,))
        
        logger.info(f"Referral success: {referrer_id} invited {new_user_id}")
        return True, referrer_id

# ============================================================
# === НАСТРОЙКИ ГОЛОСА ===
# ============================================================
def get_user_settings(user_id: int) -> dict:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT voice_mode, language FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return {'voice_mode': bool(row[0]) if row else False, 'language': row[1] if row else 'ru'}

def set_voice_mode(user_id: int, enabled: bool):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET voice_mode = ? WHERE user_id = ?", (1 if enabled else 0, user_id))

def set_user_language(user_id: int, language: str):
    if language not in ['ru', 'en', 'ko']:
        language = 'ru'
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET language = ? WHERE user_id = ?", (language, user_id))

# ============================================================
# === ГЕНЕРАЦИЯ ГОЛОСА ===
# ============================================================
async def generate_voice_response(text: str, user_id: int, lang: str = 'ru') -> str | None:
    try:
        import edge_tts
    except ImportError:
        return None

    if not os.path.exists(VOICE_DIR):
        os.makedirs(VOICE_DIR)

    voice = VOICE_MAP.get(lang, 'ru-RU-DmitryNeural')
    output_file = os.path.join(VOICE_DIR, f"voice_{user_id}_{datetime.now().strftime('%H%M%S')}.ogg")

    try:
        clean_text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        clean_text = re.sub(r'[*`#]', '', clean_text)
        clean_text = re.sub(r'\n+', '. ', clean_text)[:3000]
        
        communicate = edge_tts.Communicate(clean_text, voice)
        await communicate.save(output_file)
        return output_file
    except Exception as e:
        logger.error(f"Voice error: {e}")
        return None

# ============================================================
# === ПРОГРЕСС / ИСТОРИЯ ===
# ============================================================
def add_weight_record(user_id: int, weight: float):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO progress (user_id, weight) VALUES (?, ?)", (user_id, weight))
        cursor.execute("UPDATE users SET weight = ? WHERE user_id = ?", (weight, user_id))

def get_weight_history(user_id: int, limit: int = 10) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT weight, date FROM progress WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit))
        return cursor.fetchall()

def add_to_history(user_id: int, role: str, content: str):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content[:2000]))
        cursor.execute("DELETE FROM chat_history WHERE user_id = ? AND id NOT IN (SELECT id FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 10)", (user_id, user_id))

def get_chat_context(user_id: int) -> list:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user_id,))
        return [{"role": r[0], "content": r[1]} for r in reversed(cursor.fetchall())]

def clear_history(user_id: int):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))

# ============================================================
# === УПРАЖНЕНИЯ ===
# ============================================================
def find_exercise(query: str) -> dict | None:
    with db_connection() as conn:
        cursor = conn.cursor()
        q = query.lower().strip()

        cursor.execute("SELECT name, muscles, gif_url, video_url FROM exercises WHERE LOWER(name) = ?", (q,))
        row = cursor.fetchone()
        
        if not row:
            cursor.execute("SELECT name, muscles, gif_url, video_url FROM exercises WHERE LOWER(aliases) LIKE ?", (f"%{q}%",))
            row = cursor.fetchone()
        
        if not row:
            cursor.execute("SELECT name, muscles, gif_url, video_url FROM exercises WHERE LOWER(name) LIKE ?", (f"%{q}%",))
            row = cursor.fetchone()
        
        if row:
            return {'name': row[0], 'muscles': row[1], 'gif_url': row[2], 'video_url': row[3]}
    return None

def get_exercises_by_group(group: str) -> list:
    group_keywords = {
        'legs': ['ноги', 'ягодиц'],
        'arms': ['руки', 'бицепс', 'трицепс'],
        'back': ['спина'],
        'chest': ['грудь'],
        'abs': ['пресс', 'кор'],
        'cardio': ['кардио', 'всё тело'],
        'all': []
    }

    keywords = group_keywords.get(group, [])

    with db_connection() as conn:
        cursor = conn.cursor()
        
        if group == 'all' or not keywords:
            cursor.execute("SELECT name, muscles, gif_url, video_url FROM exercises ORDER BY name LIMIT 15")
        else:
            conditions = " OR ".join([f"LOWER(muscles) LIKE '%{kw}%'" for kw in keywords])
            cursor.execute(f"SELECT name, muscles, gif_url, video_url FROM exercises WHERE {conditions} LIMIT 10")
        
        return [{'name': r[0], 'muscles': r[1], 'gif_url': r[2], 'video_url': r[3]} for r in cursor.fetchall()]

def extract_exercise_name(text: str) -> str | None:
    patterns = [
        r"как (?:правильно )?(?:делать|выполнять) (.+?)(?:\?|$|\.)",
        r"техника (.+?)(?:\?|$|\.)",
        r"покажи (.+?)(?:\?|$|\.)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            ex = re.sub(r'\b(упражнение|правильно|мне)\b', '', match.group(1)).strip()
            if len(ex) > 2:
                return ex
    return None

# ============================================================
# === CRYPTO BOT ПЛАТЕЖИ ===
# ============================================================
async def create_crypto_invoice(user_id: int, amount: float, currency: str = "USDT") -> dict | None:
    if not CRYPTO_BOT_TOKEN:
        return None

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
            payload = {
                "asset": currency,
                "amount": str(amount),
                "description": f"Premium 30 дней (user {user_id})",
                "hidden_message": "Спасибо за покупку! 💪",
                "paid_btn_name": "callback",
                "paid_btn_url": f"https://t.me/your_bot?start=paid_{user_id}",
                "payload": str(user_id),
                "expires_in": 3600
            }
            
            async with session.post(
                f"{CRYPTO_BOT_API}/createInvoice",
                headers=headers,
                json=payload
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        invoice = data["result"]
                        
                        with db_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute("""
                                INSERT INTO payments (user_id, amount, currency, method, invoice_id)
                                VALUES (?, ?, ?, ?, ?)
                            """, (user_id, amount, currency, "crypto", invoice["invoice_id"]))
                        
                        return {
                            "invoice_id": invoice["invoice_id"],
                            "pay_url": invoice["pay_url"],
                            "amount": amount,
                            "currency": currency
                        }
                
                logger.error(f"CryptoBot error: {await resp.text()}")
    except Exception as e:
        logger.error(f"CryptoBot error: {e}")

    return None

async def check_crypto_payment(invoice_id: str) -> bool:
    if not CRYPTO_BOT_TOKEN:
        return False

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
            
            async with session.get(
                f"{CRYPTO_BOT_API}/getInvoices",
                headers=headers,
                params={"invoice_ids": invoice_id}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok") and data["result"]["items"]:
                        invoice = data["result"]["items"][0]
                        return invoice["status"] == "paid"
    except Exception as e:
        logger.error(f"Check payment error: {e}")

    return False

# ============================================================
# === ПОДПИСКА НА КАНАЛ ===
# ============================================================
async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ['creator', 'administrator', 'member']
    except:
        return True

# ============================================================
# === GROQ API ===
# ============================================================
async def groq_chat(user_id: int, message: str, use_context: bool = True) -> str:
    profile = get_user_profile(user_id)

    profile_text = ""
    if profile.get('goal'):
        profile_text = f"\nПрофиль: {profile.get('height', '?')}см, {profile.get('weight', '?')}кг, цель: {profile['goal']}"

    messages = [{"role": "system", "content": SYSTEM_PROMPT + profile_text}]

    if use_context and is_premium(user_id):
        messages.extend(get_chat_context(user_id))

    messages.append({"role": "user", "content": message})

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                GROQ_URL,
                json={"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 800, "temperature": 0.7},
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                timeout=30
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    reply = data["choices"][0]["message"]["content"].strip()
                    add_to_history(user_id, "user", message)
                    add_to_history(user_id, "assistant", reply)
                    return reply
                return "⚠️ AI временно недоступен."
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "⚠️ Ошибка AI."

# ============================================================
# === ОТПРАВКА ОТВЕТА ===
# ============================================================
async def send_response(update: Update, text: str, voice_mode: bool, language: str, user_id: int, keyboard=None):
    if voice_mode:
        voice_file = await generate_voice_response(text, user_id, language)
        if voice_file and os.path.exists(voice_file):
            try:
                with open(voice_file, 'rb') as f:
                    await update.message.reply_voice(voice=f)
                os.remove(voice_file)
                return
            except:
                pass

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

# ============================================================
# === ПОШАГОВОЕ ЗАПОЛНЕНИЕ ПРОФИЛЯ ===
# ============================================================
PROFILE_STEPS = {
    'height': {
        'question': "📏 Шаг 1/6: Укажи свой рост\n\nВведи число в сантиметрах (например: 175):",
        'next': 'weight',
        'field': 'height',
        'validate': lambda x: bool(re.search(r'\d+', x)) and 100 <= int(re.search(r'\d+', x).group()) <= 250,
        'parse': lambda x: int(re.search(r'\d+', x).group()),
        'error': "❌ Введи рост от 100 до 250 см"
    },
    'weight': {
        'question': "⚖️ Шаг 2/6: Укажи свой вес\n\nВведи число в килограммах (например: 75):",
        'next': 'age',
        'field': 'weight',
        'validate': lambda x: bool(re.search(r'[\d.]+', x)) and 30 <= float(re.search(r'[\d.]+', x).group()) <= 300,
        'parse': lambda x: float(re.search(r'[\d.]+', x).group()),
        'error': "❌ Введи вес от 30 до 300 кг"
    },
    'age': {
        'question': "🎂 Шаг 3/6: Укажи возраст\n\nВведи число (например: 25):",
        'next': 'gender',
        'field': 'age',
        'validate': lambda x: bool(re.search(r'\d+', x)) and 10 <= int(re.search(r'\d+', x).group()) <= 100,
        'parse': lambda x: int(re.search(r'\d+', x).group()),
        'error': "❌ Введи возраст от 10 до 100"
    },
    'gender': {
        'question': "👤 Шаг 4/6: Укажи пол",
        'next': 'goal',
        'field': 'gender',
        'is_button': True,
        'buttons': [
            [InlineKeyboardButton("👨 Мужской", callback_data="pf_gender_мужской"),
             InlineKeyboardButton("👩 Женский", callback_data="pf_gender_женский")]
        ]
    },
    'goal': {
        'question': "🎯 Шаг 5/6: Какая у тебя цель?",
        'next': 'location',
        'field': 'goal',
        'is_button': True,
        'buttons': [
            [InlineKeyboardButton("🔥 Похудеть", callback_data="pf_goal_похудеть")],
            [InlineKeyboardButton("💪 Набрать массу", callback_data="pf_goal_набрать массу")],
            [InlineKeyboardButton("✨ Поддержать форму", callback_data="pf_goal_поддержать форму")],
            [InlineKeyboardButton("🏋️ Развить силу", callback_data="pf_goal_развить силу")]
        ]
    },
    'location': {
        'question': "📍 Шаг 6/6: Где тренируешься?",
        'next': None,
        'field': 'location',
        'is_button': True,
        'buttons': [
            [InlineKeyboardButton("🏠 Дома", callback_data="pf_location_дома")],
            [InlineKeyboardButton("🏋️ В зале", callback_data="pf_location_в зале")],
            [InlineKeyboardButton("🌳 На улице", callback_data="pf_location_на улице")]
        ]
    }
}

async def start_profile_setup(message, user_id: int):
    set_profile_step(user_id, 'height')
    step = PROFILE_STEPS['height']

    try:
        await message.edit_text(step['question'], parse_mode="Markdown")
    except:
        await message.reply_text(step['question'], parse_mode="Markdown")

async def process_profile_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str):
    current_step = get_profile_step(user_id)

    if not current_step or current_step not in PROFILE_STEPS:
        return False

    step = PROFILE_STEPS[current_step]

    if step.get('is_button'):
        await update.message.reply_text(
            "☝️ Пожалуйста, выбери вариант из кнопок выше",
            reply_markup=InlineKeyboardMarkup(step['buttons'])
        )
        return True

    try:
        if not step['validate'](text):
            await update.message.reply_text(step['error'])
            return True
        
        value = step['parse'](text)
        update_user_profile(user_id, **{step['field']: value})
        
        await go_to_next_step(update.message, user_id, step['next'])
        return True
        
    except Exception as e:
        logger.error(f"Profile step error: {e}")
        await update.message.reply_text(step['error'])
        return True

async def go_to_next_step(message, user_id: int, next_step: str | None):
    if next_step:
        set_profile_step(user_id, next_step)
        next_data = PROFILE_STEPS[next_step]

        if next_data.get('is_button'):
            await message.reply_text(
                next_data['question'],
                reply_markup=InlineKeyboardMarkup(next_data['buttons']),
                parse_mode="Markdown"
            )
        else:
            await message.reply_text(next_data['question'], parse_mode="Markdown")
    else:
        await finish_profile_setup(message, user_id)

async def finish_profile_setup(message, user_id: int):
    set_profile_step(user_id, None)
    profile = get_user_profile(user_id)
    premium_status = get_premium_status(user_id)

    premium_text = ""
    if premium_status['is_premium']:
        premium_text = f"\n💎 Premium: **{premium_status['days_left']} дней**"
    else:
        premium_text = "\n🆓 Статус: **Бесплатный**"

    await message.reply_text(
        "✅ **Профиль создан!**\n\n"
        f"📏 Рост: **{profile.get('height')} см**\n"
        f"⚖️ Вес: **{profile.get('weight')} кг**\n"
        f"🎂 Возраст: **{profile.get('age')} лет**\n"
        f"👤 Пол: **{profile.get('gender')}**\n"
        f"🎯 Цель: **{profile.get('goal')}**\n"
        f"📍 Место: **{profile.get('location')}**"
        f"{premium_text}\n\n"
        "Теперь спроси что-нибудь! 💪",
        parse_mode="Markdown"
    )

# ============================================================
# === КОМАНДЫ ===
# ============================================================
@handle_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_new_user = get_or_create_user(user.id, user.username)
    set_profile_step(user.id, None)

    if not await check_subscription(user.id, context) and user.id not in ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
            [InlineKeyboardButton("✅ Проверить", callback_data="check_sub")]
        ]
        await update.message.reply_text("🔒 Подпишись на канал!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Обработка реферальной ссылки
    referral_message = ""
    if context.args and is_new_user:
        ref_code = context.args[0]
        # Исключаем служебные параметры
        if not ref_code.startswith("paid_"):
            success, referrer_id = process_referral(user.id, ref_code)
            if success:
                referral_message = (
                    f"🎁 **Реферальный бонус активирован!**\n"
                    f"Тебе начислено **{REFERRED_BONUS_DAYS} дня Premium!**\n\n"
                )
                # Уведомляем пригласившего
                try:
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"🎉 **Твой друг присоединился!**\n\n"
                             f"Тебе начислено **+{REFERRER_BONUS_DAYS} дней Premium!** 💎",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify referrer {referrer_id}: {e}")

    keyboard = [
        [InlineKeyboardButton("👤 Создать профиль", callback_data="setup_profile")],
        [InlineKeyboardButton("💪 Тренировка", callback_data="workout"),
         InlineKeyboardButton("🍽️ Рецепт", callback_data="recipe")],
        [InlineKeyboardButton("🏋️ Упражнения", callback_data="exercises_menu"),
         InlineKeyboardButton("📊 Прогресс", callback_data="progress")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("🔥 Premium", callback_data="subscribe")]
    ]

    # Статус профиля
    profile_status = "✅ Профиль заполнен" if has_profile(user.id) else "❌ Профиль не заполнен"
    
    # Статус подписки
    premium_status = get_premium_status(user.id)
    if premium_status['is_premium']:
        sub_status = f"💎 Premium: {premium_status['days_left']} дней"
    else:
        can_ask, remaining = can_ask_question(user.id)
        sub_status = f"🆓 Бесплатно: {remaining}/5 вопросов"

    await update.message.reply_text(
        f"{referral_message}"
        f"💪 Привет, {user.first_name}!\n\n"
        f"Я **Murasaki Sport** — AI-тренер!\n\n"
        f"📋 {profile_status}\n"
        f"📌 {sub_status}\n\n"
        "Выбери действие или задай вопрос 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

@handle_errors
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Как пользоваться:**\n\n"
        "1. Создай профиль\n"
        "2. Задавай вопросы о спорте\n"
        "3. Записывай вес: `Вес 75.5`\n\n"
        "**Команды:**\n"
        "/start — Главное меню\n"
        "/profile — Твой профиль\n"
        "/subscribe — Подписка Premium\n"
        "/referral — Пригласить друга\n"
        "/settings — Настройки\n"
        "/stats — Статистика",
        parse_mode="Markdown"
    )

@handle_errors
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_user_settings(update.effective_user.id)
    mode = "🎙️ Голос" if settings['voice_mode'] else "📝 Текст"

    keyboard = [
        [InlineKeyboardButton(f"{'🔊' if settings['voice_mode'] else '🔇'} {mode}", callback_data="toggle_voice")],
        [InlineKeyboardButton("🇷🇺", callback_data="lang_ru"),
         InlineKeyboardButton("🇺🇸", callback_data="lang_en"),
         InlineKeyboardButton("🇰🇷", callback_data="lang_ko")]
    ]

    await update.message.reply_text(
        f"⚙️ **Настройки**\n\nРежим: **{mode}**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

@handle_errors
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    p = get_user_profile(user_id)

    if not has_profile(user_id):
        keyboard = [[InlineKeyboardButton("👤 Создать профиль", callback_data="setup_profile")]]
        await update.message.reply_text(
            "❌ **Профиль не заполнен**\n\n"
            "Создай профиль, чтобы получать персонализированные рекомендации!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # Статус подписки
    premium_status = get_premium_status(user_id)
    if premium_status['is_premium']:
        sub_text = f"💎 **Premium активен**\n📅 Осталось: **{premium_status['days_left']} дней**\n📆 До: {premium_status['until_date']}"
    else:
        can_ask, remaining = can_ask_question(user_id)
        sub_text = f"🆓 **Бесплатный план**\n💬 Вопросов сегодня: **{remaining}/5**"

    keyboard = [
        [InlineKeyboardButton("✏️ Изменить профиль", callback_data="setup_profile")],
        [InlineKeyboardButton("💎 Premium" if not premium_status['is_premium'] else "📊 Статистика", 
                              callback_data="subscribe" if not premium_status['is_premium'] else "show_stats")]
    ]

    await update.message.reply_text(
        f"👤 **Твой профиль**\n\n"
        f"📏 Рост: **{p.get('height', '—')} см**\n"
        f"⚖️ Вес: **{p.get('weight', '—')} кг**\n"
        f"🎂 Возраст: **{p.get('age', '—')} лет**\n"
        f"👤 Пол: **{p.get('gender', '—')}**\n"
        f"🎯 Цель: **{p.get('goal', '—')}**\n"
        f"📍 Место: **{p.get('location', '—')}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{sub_text}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

@handle_errors
async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /subscribe - показывает статус подписки или предлагает купить"""
    user_id = update.effective_user.id
    premium_status = get_premium_status(user_id)

    if premium_status['is_premium']:
        # У пользователя есть подписка - показываем статус
        keyboard = [
            [InlineKeyboardButton("👥 Пригласить друга (+7 дней)", callback_data="ref_info")],
            [InlineKeyboardButton("📊 Статистика", callback_data="show_stats")]
        ]
        
        await update.message.reply_text(
            f"💎 **Premium активен!**\n\n"
            f"📅 Осталось: **{premium_status['days_left']} дней**\n"
            f"📆 Действует до: **{premium_status['until_date']}**\n\n"
            f"✅ Безлимитные вопросы\n"
            f"✅ Голосовые ответы\n"
            f"✅ Память диалога\n\n"
            f"💡 Пригласи друга и получи **+{REFERRER_BONUS_DAYS} дней бесплатно!**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    else:
        # Нет подписки - предлагаем купить
        keyboard = [
            [InlineKeyboardButton(f"💳 {PREMIUM_PRICE_RUB}₽ (Карта)", callback_data="pay_card")],
            [InlineKeyboardButton(f"⭐ {PREMIUM_PRICE_STARS} Stars", callback_data="pay_stars")],
            [InlineKeyboardButton(f"💎 {PREMIUM_PRICE_USDT} USDT (Крипта)", callback_data="pay_crypto")],
            [InlineKeyboardButton(f"👥 Бесплатно (+{REFERRED_BONUS_DAYS} дня за регистрацию)", callback_data="ref_info")]
        ]
        
        can_ask, remaining = can_ask_question(user_id)
        
        await update.message.reply_text(
            f"🆓 **У тебя бесплатный план**\n\n"
            f"💬 Осталось вопросов сегодня: **{remaining}/5**\n\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"💎 **Premium 30 дней:**\n\n"
            f"✅ Безлимитные вопросы\n"
            f"✅ Голосовые ответы\n"
            f"✅ Память диалога\n"
            f"✅ Приоритетная поддержка\n\n"
            f"Выбери способ оплаты:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

@handle_errors
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.free_questions, u.is_premium, s.total_questions, s.workouts_completed, s.referrals_count
            FROM users u LEFT JOIN stats s ON u.user_id = s.user_id WHERE u.user_id = ?
        """, (user_id,))
        row = cursor.fetchone()

    if row:
        premium_status = get_premium_status(user_id)
        if premium_status['is_premium']:
            status = f"💎 Premium ({premium_status['days_left']} дней)"
        else:
            status = f"🆓 Бесплатно ({row[0]}/5)"
        
        await update.message.reply_text(
            f"📊 **Твоя статистика**\n\n"
            f"📌 Статус: {status}\n\n"
            f"💬 Вопросов задано: **{row[2] or 0}**\n"
            f"💪 Тренировок выполнено: **{row[3] or 0}**\n"
            f"👥 Друзей приглашено: **{row[4] or 0}**",
            parse_mode="Markdown"
        )

@handle_errors
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        code = row[0] if row else None
        
        cursor.execute("SELECT referrals_count FROM stats WHERE user_id = ?", (user_id,))
        stats_row = cursor.fetchone()
        referrals_count = stats_row[0] if stats_row else 0

    if not code:
        await update.message.reply_text("⚠️ Ошибка получения реферального кода")
        return

    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={code}"

    await update.message.reply_text(
        f"👥 **Пригласи друга — получи Premium!**\n\n"
        f"🎁 **Ты получишь:** +{REFERRER_BONUS_DAYS} дней Premium\n"
        f"🎁 **Друг получит:** +{REFERRED_BONUS_DAYS} дня Premium\n\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"🔗 **Твоя ссылка:**\n`{ref_link}`\n\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"📊 Приглашено друзей: **{referrals_count}**\n"
        f"🎁 Получено дней: **{referrals_count * REFERRER_BONUS_DAYS}**",
        parse_mode="Markdown"
    )

@handle_errors
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text("✅ История диалога очищена!")

# ============================================================
# === ОБРАБОТКА СООБЩЕНИЙ ===
# ============================================================
@handle_errors
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)

    if not await check_subscription(user.id, context) and user.id not in ADMIN_IDS:
        return

    text = update.message.text.strip()

    settings = get_user_settings(user.id)
    voice_mode = settings['voice_mode']
    language = settings['language']

    # Профиль
    profile_step = get_profile_step(user.id)
    if profile_step:
        if await process_profile_step(update, context, user.id, text):
            return

    # Вес
    weight_match = re.match(r'^вес\s+(\d+\.?\d*)', text.lower())
    if weight_match:
        weight = float(weight_match.group(1))
        if 30 <= weight <= 300:
            add_weight_record(user.id, weight)
            history = get_weight_history(user.id, 2)
            
            response = f"✅ **{weight} кг**"
            if len(history) >= 2:
                diff = weight - history[1][0]
                response += f" ({'📈' if diff > 0 else '📉'} {diff:+.1f})"
            
            await send_response(update, response, voice_mode, language, user.id)
            return

    # Упражнение
    ex_name = extract_exercise_name(text)
    if ex_name:
        can_ask, _ = can_ask_question(user.id)
        if not can_ask:
            keyboard = [[InlineKeyboardButton("💎 Premium", callback_data="subscribe")]]
            await update.message.reply_text("⚠️ Лимит вопросов исчерпан!", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        await update.message.chat.send_action("typing")
        
        exercise = find_exercise(ex_name)
        ai_response = await groq_chat(user.id, f"Техника '{ex_name}'. Кратко.", use_context=False)
        
        if not is_premium(user.id):
            use_question(user.id)
        
        if exercise and exercise.get('gif_url'):
            try:
                keyboard = [[InlineKeyboardButton("▶️ YouTube", url=exercise['video_url'])]] if exercise.get('video_url') else []
                await update.message.reply_animation(
                    animation=exercise['gif_url'],
                    caption=f"💪 **{exercise['name']}**\n🎯 {exercise['muscles']}\n\n{ai_response[:700]}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
                )
                return
            except:
                pass
        
        await send_response(update, f"💪 **{ex_name.title()}**\n\n{ai_response}", voice_mode, language, user.id)
        return

    # Фильтр
    if not is_fitness_question(text):
        await update.message.reply_text(
            "🏋️ Я отвечаю только на вопросы о спорте и питании.\n\n"
            "Примеры:\n• Составь тренировку\n• Как делать приседания?"
        )
        return

    # Лимиты
    can_ask, remaining = can_ask_question(user.id)
    if not can_ask:
        keyboard = [[InlineKeyboardButton("💎 Premium", callback_data="subscribe")]]
        await update.message.reply_text(
            "⚠️ **Лимит вопросов исчерпан!**\n\n"
            "Бесплатно: 5 вопросов в день\n\n"
            "💎 Premium — безлимитный доступ",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    await update.message.chat.send_action("typing")
    response = await groq_chat(user.id, text)

    if not is_premium(user.id):
        use_question(user.id)

    footer = ""
    if not is_premium(user.id):
        _, rem = can_ask_question(user.id)
        if rem <= 2:
            footer = f"\n\n💡 Осталось вопросов: {rem}/5"

    await send_response(update, response + footer, voice_mode, language, user.id)

# ============================================================
# === CALLBACK HANDLERS ===
# ============================================================
@handle_errors
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # === ПОДПИСКА ===
    if query.data == "check_sub":
        if await check_subscription(user_id, context):
            await query.message.edit_text("✅ Подписка подтверждена! Напиши /start")
        else:
            await query.answer("❌ Подписка не найдена!", show_alert=True)
        return

    if query.data == "back":
        try:
            await query.message.delete()
        except:
            pass
        return

    # === СТАТИСТИКА ===
    if query.data == "show_stats":
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.total_questions, s.workouts_completed, s.referrals_count
                FROM stats s WHERE s.user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
        
        if row:
            await query.message.reply_text(
                f"📊 **Статистика**\n\n"
                f"💬 Вопросов: **{row[0] or 0}**\n"
                f"💪 Тренировок: **{row[1] or 0}**\n"
                f"👥 Рефералов: **{row[2] or 0}**",
                parse_mode="Markdown"
            )
        return

    # === ПРОФИЛЬ ===
    if query.data == "setup_profile":
        await start_profile_setup(query.message, user_id)
        return

    # Обработка кнопок профиля: pf_field_value
    if query.data.startswith("pf_"):
        parts = query.data.split("_", 2)
        if len(parts) >= 3:
            field = parts[1]
            value = parts[2]
            
            update_user_profile(user_id, **{field: value})
            
            current_step = get_profile_step(user_id)
            if current_step and current_step in PROFILE_STEPS:
                next_step = PROFILE_STEPS[current_step]['next']
                
                if next_step:
                    set_profile_step(user_id, next_step)
                    next_data = PROFILE_STEPS[next_step]
                    
                    if next_data.get('is_button'):
                        await query.message.edit_text(
                            next_data['question'],
                            reply_markup=InlineKeyboardMarkup(next_data['buttons']),
                            parse_mode="Markdown"
                        )
                    else:
                        await query.message.edit_text(next_data['question'], parse_mode="Markdown")
                else:
                    set_profile_step(user_id, None)
                    profile = get_user_profile(user_id)
                    premium_status = get_premium_status(user_id)
                    
                    premium_text = ""
                    if premium_status['is_premium']:
                        premium_text = f"\n💎 Premium: **{premium_status['days_left']} дней**"
                    else:
                        premium_text = "\n🆓 Статус: **Бесплатный**"
                    
                    await query.message.edit_text(
                        "✅ **Профиль создан!**\n\n"
                        f"📏 Рост: **{profile.get('height')} см**\n"
                        f"⚖️ Вес: **{profile.get('weight')} кг**\n"
                        f"🎂 Возраст: **{profile.get('age')} лет**\n"
                        f"👤 Пол: **{profile.get('gender')}**\n"
                        f"🎯 Цель: **{profile.get('goal')}**\n"
                        f"📍 Место: **{profile.get('location')}**"
                        f"{premium_text}\n\n"
                        "Теперь спроси что-нибудь! 💪",
                        parse_mode="Markdown"
                    )
        return

    # === УПРАЖНЕНИЯ ===
    if query.data == "exercises_menu":
        keyboard = [
            [InlineKeyboardButton("🦵 Ноги", callback_data="ex_legs"),
             InlineKeyboardButton("💪 Руки", callback_data="ex_arms")],
            [InlineKeyboardButton("🔙 Спина", callback_data="ex_back"),
             InlineKeyboardButton("🫁 Грудь", callback_data="ex_chest")],
            [InlineKeyboardButton("🎯 Пресс", callback_data="ex_abs"),
             InlineKeyboardButton("📋 Все", callback_data="ex_all")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back")]
        ]
        await query.message.reply_text("🏋️ **Выбери группу мышц:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if query.data.startswith("ex_") and not query.data.startswith("ex_show_"):
        group = query.data.replace("ex_", "")
        exercises = get_exercises_by_group(group)
        
        if not exercises:
            await query.answer("Упражнения не найдены", show_alert=True)
            return
        
        keyboard = [[InlineKeyboardButton(f"💪 {ex['name']}", callback_data=f"ex_show_{ex['name'][:15]}")] for ex in exercises]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="exercises_menu")])
        
        await query.message.edit_text("Выбери упражнение:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if query.data.startswith("ex_show_"):
        name = query.data.replace("ex_show_", "")
        exercise = find_exercise(name)
        
        if not exercise:
            await query.answer("Упражнение не найдено", show_alert=True)
            return
        
        await query.message.edit_text("⏳ Загружаю...")
        
        ai = await groq_chat(user_id, f"Техника '{exercise['name']}'. Кратко.", use_context=False)
        
        keyboard = []
        if exercise.get('video_url'):
            keyboard.append([InlineKeyboardButton("▶️ YouTube", url=exercise['video_url'])])
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="exercises_menu")])
        
        if exercise.get('gif_url'):
            try:
                await query.message.delete()
                await context.bot.send_animation(
                    chat_id=user_id,
                    animation=exercise['gif_url'],
                    caption=f"💪 **{exercise['name']}**\n🎯 {exercise['muscles']}\n\n{ai[:800]}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            except:
                pass
        
        await query.message.edit_text(
            f"💪 **{exercise['name']}**\n\n{ai}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # === НАСТРОЙКИ ===
    if query.data == "settings":
        settings = get_user_settings(user_id)
        mode = "🎙️ Голос" if settings['voice_mode'] else "📝 Текст"
        
        keyboard = [
            [InlineKeyboardButton(f"{'🔊' if settings['voice_mode'] else '🔇'} {mode}", callback_data="toggle_voice")],
            [InlineKeyboardButton("🇷🇺", callback_data="lang_ru"),
             InlineKeyboardButton("🇺🇸", callback_data="lang_en"),
             InlineKeyboardButton("🇰🇷", callback_data="lang_ko")]
        ]
        await query.message.edit_text(f"⚙️ **Настройки**\n\nРежим: {mode}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if query.data == "toggle_voice":
        settings = get_user_settings(user_id)
        new = not settings['voice_mode']
        set_voice_mode(user_id, new)
        await query.answer("🎙️ Голосовые ответы включены!" if new else "📝 Текстовые ответы включены!", show_alert=True)
        
        mode = "🎙️ Голос" if new else "📝 Текст"
        keyboard = [
            [InlineKeyboardButton(f"{'🔊' if new else '🔇'} {mode}", callback_data="toggle_voice")],
            [InlineKeyboardButton("🇷🇺", callback_data="lang_ru"),
             InlineKeyboardButton("🇺🇸", callback_data="lang_en"),
             InlineKeyboardButton("🇰🇷", callback_data="lang_ko")]
        ]
        await query.message.edit_reply_markup(InlineKeyboardMarkup(keyboard))
        return

    if query.data.startswith("lang_"):
        lang = query.data.replace("lang_", "")
        set_user_language(user_id, lang)
        lang_names = {'ru': '🇷🇺 Русский', 'en': '🇺🇸 English', 'ko': '🇰🇷 한국어'}
        await query.answer(f"Язык изменён: {lang_names.get(lang)}", show_alert=True)
        return

    # === ПРОГРЕСС ===
    if query.data == "progress":
        records = get_weight_history(user_id, 10)
        
        if not records:
            await query.message.reply_text(
                "📊 **Записей пока нет**\n\n"
                "Чтобы записать вес, напиши:\n`Вес 75.5`",
                parse_mode="Markdown"
            )
            return
        
        lines = []
        for w, d in records:
            try:
                if 'T' in d or '-' in d:
                    date_str = datetime.fromisoformat(d.replace('Z', '')).strftime('%d.%m')
                else:
                    date_str = d[:10]
            except:
                date_str = d[:10]
            lines.append(f"• {date_str}: **{w}** кг")
        
        await query.message.reply_text(
            "📊 **История веса:**\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )
        return

    # === ТРЕНИРОВКА ===
    if query.data == "workout":
        if not has_profile(user_id):
            keyboard = [[InlineKeyboardButton("👤 Создать профиль", callback_data="setup_profile")]]
            await query.message.reply_text(
                "❌ **Сначала создай профиль!**\n\n"
                "Это поможет подобрать тренировку под тебя.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return
        
        keyboard = [
            [InlineKeyboardButton("💪 Силовая", callback_data="w_strength"),
             InlineKeyboardButton("🔥 Кардио", callback_data="w_cardio")],
            [InlineKeyboardButton("🧘 Растяжка", callback_data="w_stretch")]
        ]
        await query.message.reply_text(
            "💪 **Выбери тип тренировки:**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if query.data.startswith("w_"):
        wtype = query.data.replace("w_", "")
        profile = get_user_profile(user_id)
        types = {'strength': 'силовую', 'cardio': 'кардио', 'stretch': 'растяжку'}
        
        await query.message.edit_text("💪 Составляю тренировку...")
        
        response = await groq_chat(
            user_id, 
            f"Составь {types.get(wtype)} тренировку. Место: {profile.get('location', 'дом')}. Цель: {profile.get('goal', 'фитнес')}.",
            use_context=False
        )
        
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO workouts (user_id, workout_text) VALUES (?, ?)", (user_id, response))
            wid = cursor.lastrowid
        
        keyboard = [[InlineKeyboardButton("✅ Выполнено!", callback_data=f"done_{wid}")]]
        await query.message.edit_text(
            f"💪 **Твоя тренировка:**\n\n{response}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if query.data.startswith("done_"):
        wid = int(query.data.replace("done_", ""))
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE workouts SET completed = 1 WHERE id = ?", (wid,))
            cursor.execute("UPDATE stats SET workouts_completed = workouts_completed + 1 WHERE user_id = ?", (user_id,))
        await query.answer("🔥 Отлично! Тренировка записана!", show_alert=True)
        await query.message.reply_text("✅ **Тренировка выполнена!** 💪\n\nТак держать!")
        return

    # === РЕЦЕПТ ===
    if query.data == "recipe":
        keyboard = [
            [InlineKeyboardButton("🍳 Завтрак", callback_data="r_breakfast"),
             InlineKeyboardButton("🥗 Обед", callback_data="r_lunch")],
            [InlineKeyboardButton("🍲 Ужин", callback_data="r_dinner")]
        ]
        await query.message.reply_text(
            "🍽️ **Выбери приём пищи:**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if query.data.startswith("r_"):
        rtype = query.data.replace("r_", "")
        types = {'breakfast': 'завтрак', 'lunch': 'обед', 'dinner': 'ужин'}
        profile = get_user_profile(user_id)
        
        await query.message.edit_text("🍽️ Подбираю рецепт...")
        
        goal_text = f"Цель: {profile.get('goal', 'здоровое питание')}." if profile.get('goal') else ""
        response = await groq_chat(
            user_id, 
            f"Рецепт на {types.get(rtype)}. {goal_text} С КБЖУ.",
            use_context=False
        )
        
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE stats SET recipes_generated = recipes_generated + 1 WHERE user_id = ?", (user_id,))
        
        keyboard = [[InlineKeyboardButton("🔄 Другой рецепт", callback_data="recipe")]]
        await query.message.edit_text(
            f"🍽️ **Рецепт:**\n\n{response}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # ============================================================
    # === ОПЛАТА ===
    # ============================================================

    if query.data == "subscribe":
        premium_status = get_premium_status(user_id)
        
        if premium_status['is_premium']:
            # Уже есть премиум - показываем статус
            keyboard = [
                [InlineKeyboardButton("👥 Пригласить друга (+7 дней)", callback_data="ref_info")],
                [InlineKeyboardButton("📊 Статистика", callback_data="show_stats")]
            ]
            await query.message.reply_text(
                f"💎 **Premium уже активен!**\n\n"
                f"📅 Осталось: **{premium_status['days_left']} дней**\n"
                f"📆 До: **{premium_status['until_date']}**\n\n"
                f"💡 Пригласи друга и получи ещё **+{REFERRER_BONUS_DAYS} дней!**",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            return
        
        # Нет премиума - предлагаем купить
        keyboard = [
            [InlineKeyboardButton(f"💳 {PREMIUM_PRICE_RUB}₽ (Карта)", callback_data="pay_card")],
            [InlineKeyboardButton(f"⭐ {PREMIUM_PRICE_STARS} Stars", callback_data="pay_stars")],
            [InlineKeyboardButton(f"💎 {PREMIUM_PRICE_USDT} USDT (Крипта)", callback_data="pay_crypto")],
            [InlineKeyboardButton(f"👥 Бесплатно (пригласи друга)", callback_data="ref_info")]
        ]
        await query.message.reply_text(
            "💎 **Premium 30 дней**\n\n"
            "✅ Безлимитные вопросы\n"
            "✅ Голосовые ответы\n"
            "✅ Память диалога\n\n"
            "Выбери способ оплаты:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    # Оплата картой (ЮKassa/Stripe)
    if query.data == "pay_card":
        if not PROVIDER_TOKEN:
            await query.message.reply_text(
                "⚠️ Оплата картой временно недоступна.\n\n"
                "Используй Stars ⭐ или крипту 💎"
            )
            return
        
        await context.bot.send_invoice(
            chat_id=user_id,
            title="Premium 30 дней",
            description="Безлимит + голос + память",
            payload="premium_card",
            provider_token=PROVIDER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice("Premium", PREMIUM_PRICE_RUB * 100)]
        )
        return

    # Оплата Stars
    if query.data == "pay_stars":
        try:
            await context.bot.send_invoice(
                chat_id=user_id,
                title="Premium 30 дней",
                description="Безлимит + голос + память",
                payload="premium_stars",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice("Premium", PREMIUM_PRICE_STARS)]
            )
        except Exception as e:
            logger.error(f"Stars error: {e}")
            await query.message.reply_text("⚠️ Stars временно недоступны. Попробуй другой способ.")
        return

    # Оплата криптой
    if query.data == "pay_crypto":
        if not CRYPTO_BOT_TOKEN:
            await query.message.reply_text(
                "⚠️ Оплата криптой временно недоступна.\n\n"
                "Используй Stars ⭐ или карту 💳"
            )
            return
        
        await query.message.edit_text("⏳ Создаю счёт...")
        
        invoice = await create_crypto_invoice(user_id, PREMIUM_PRICE_USDT, "USDT")
        
        if invoice:
            keyboard = [
                [InlineKeyboardButton("💎 Оплатить", url=invoice['pay_url'])],
                [InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_crypto_{invoice['invoice_id']}")],
                [InlineKeyboardButton("◀️ Назад", callback_data="subscribe")]
            ]
            await query.message.edit_text(
                f"💎 **Оплата криптой**\n\n"
                f"Сумма: **{invoice['amount']} {invoice['currency']}**\n\n"
                f"1️⃣ Нажми «Оплатить»\n"
                f"2️⃣ Оплати в CryptoBot\n"
                f"3️⃣ Нажми «Я оплатил»\n\n"
                f"⏱ Счёт действует 1 час",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            await query.message.edit_text("⚠️ Ошибка создания счёта. Попробуй позже.")
        return

    # Проверка крипто-платежа
    if query.data.startswith("check_crypto_"):
        invoice_id = query.data.replace("check_crypto_", "")
        
        is_paid = await check_crypto_payment(invoice_id)
        
        if is_paid:
            activate_premium(user_id)
            
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE payments SET status = 'paid' WHERE invoice_id = ?", (invoice_id,))
            
            await query.message.edit_text(
                "🎉 **Premium активирован!**\n\n"
                "30 дней безлимитного доступа! 💪\n\n"
                "Теперь тебе доступно:\n"
                "✅ Безлимитные вопросы\n"
                "✅ Голосовые ответы\n"
                "✅ Память диалога",
                parse_mode="Markdown"
            )
        else:
            await query.answer(
                "❌ Оплата не найдена.\n\nЕсли ты оплатил, подожди минуту и попробуй снова.",
                show_alert=True
            )
        return

    # Реферальная информация
    if query.data == "ref_info":
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            code = row[0] if row else None
            
            cursor.execute("SELECT referrals_count FROM stats WHERE user_id = ?", (user_id,))
            stats_row = cursor.fetchone()
            referrals_count = stats_row[0] if stats_row else 0
        
        if not code:
            await query.answer("Ошибка", show_alert=True)
            return
        
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={code}"
        
        await query.message.reply_text(
            f"👥 **Пригласи друга — получи Premium!**\n\n"
            f"🎁 **Ты получишь:** +{REFERRER_BONUS_DAYS} дней Premium\n"
            f"🎁 **Друг получит:** +{REFERRED_BONUS_DAYS} дня Premium\n\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"🔗 **Твоя ссылка:**\n`{ref_link}`\n\n"
            f"_(нажми чтобы скопировать)_\n\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"📊 Приглашено: **{referrals_count}** друзей\n"
            f"🎁 Получено: **{referrals_count * REFERRER_BONUS_DAYS}** дней",
            parse_mode="Markdown"
        )
        return

# ============================================================
# === ПЛАТЕЖИ ===
# ============================================================
@handle_errors
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

@handle_errors
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload

    activate_premium(user_id)

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO payments (user_id, amount, currency, method, status)
            VALUES (?, ?, ?, ?, 'paid')
        """, (
            user_id,
            update.message.successful_payment.total_amount / 100,
            update.message.successful_payment.currency,
            "stars" if "stars" in payload else "card"
        ))

    logger.info(f"Payment received: {user_id}, {payload}")

    await update.message.reply_text(
        "🎉 **Premium активирован!**\n\n"
        "30 дней безлимитного доступа! 💪\n\n"
        "✅ Безлимитные вопросы\n"
        "✅ Голосовые ответы\n"
        "✅ Память диалога",
        parse_mode="Markdown"
    )

# ============================================================
# === АДМИН ===
# ============================================================
@handle_errors
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text(
            f"❌ Нет доступа.\n\nТвой ID: `{user_id}`\n\n"
            f"Добавь его в ADMIN_IDS в коде.",
            parse_mode="Markdown"
        )
        return

    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
        premium = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM payments WHERE status = 'paid'")
        payments = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(referrals_count) FROM stats")
        referrals = cursor.fetchone()[0] or 0

    await update.message.reply_text(
        f"🔧 **Админ-панель**\n\n"
        f"👥 Всего пользователей: **{users}**\n"
        f"💎 С Premium: **{premium}**\n"
        f"💳 Платежей: **{payments}**\n"
        f"👥 Рефералов: **{referrals}**\n\n"
        f"**Команды:**\n"
        f"`/give_premium ID 30` — выдать Premium\n"
        f"`/broadcast текст` — рассылка",
        parse_mode="Markdown"
    )

@handle_errors
async def give_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if len(context.args) < 1:
        await update.message.reply_text(
            "Использование:\n`/give_premium USER_ID [дни]`\n\n"
            "Пример: `/give_premium 123456789 30`",
            parse_mode="Markdown"
        )
        return

    try:
        target = int(context.args[0])
        days = int(context.args[1]) if len(context.args) > 1 else 30
        activate_premium(target, days)
        await update.message.reply_text(f"✅ Выдано **{days} дней** Premium для `{target}`", parse_mode="Markdown")
        
        # Уведомляем пользователя
        try:
            await context.bot.send_message(
                chat_id=target,
                text=f"🎁 **Тебе выдан Premium!**\n\n+{days} дней безлимита! 💎",
                parse_mode="Markdown"
            )
        except:
            pass
            
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. ID должен быть числом.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

@handle_errors
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    if not context.args:
        await update.message.reply_text(
            "Использование:\n`/broadcast текст сообщения`",
            parse_mode="Markdown"
        )
        return
    
    text = " ".join(context.args)
    
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        users = [row[0] for row in cursor.fetchall()]
    
    sent = 0
    failed = 0
    
    await update.message.reply_text(f"📤 Начинаю рассылку для {len(users)} пользователей...")
    
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)  # Анти-флуд
        except Exception as e:
            failed += 1
            logger.warning(f"Broadcast failed for {uid}: {e}")
    
    await update.message.reply_text(
        f"✅ **Рассылка завершена**\n\n"
        f"📤 Отправлено: {sent}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="Markdown"
    )

# ============================================================
# === MAIN ===
# ============================================================
def main():
    logger.info("=" * 50)
    logger.info("Starting Murasaki Sport Bot...")
    logger.info(f"Admin IDs: {ADMIN_IDS}")

    if ADMIN_IDS == [123456789]:
        logger.warning("⚠️ ADMIN_IDS не настроен! Замени 123456789 на свой Telegram ID")

    logger.info("=" * 50)

    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("give_premium", give_premium_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    # Callback-и
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Платежи
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    logger.info("✅ Bot started!")
    logger.info("💳 Payments: Card + Stars + Crypto")
    logger.info(f"👥 Referral: +{REFERRER_BONUS_DAYS} days for inviter, +{REFERRED_BONUS_DAYS} days for invited")
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
