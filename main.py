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

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

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
ADMIN_IDS = [1162907446]

REQUIRED_CHANNEL = "@Murasaki_lab"

# Голоса
VOICE_MAP = {
    'ru': 'ru-RU-DmitryNeural',
    'en': 'en-US-ChristopherNeural',
    'ko': 'ko-KR-HyunsuNeural'
}

SYSTEM_PROMPT = """Ты персональный AI-тренер Murasaki Sport. 
Отвечай ТОЛЬКО на вопросы о спорте, тренировках, питании, здоровье, фитнесе.
Стиль: дружелюбный, мотивирующий. Ответы: 3-5 предложений. Язык: русский."""

# === КЛЮЧЕВЫЕ СЛОВА ДЛЯ ФИЛЬТРА ===
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
    if any(q in text_lower for q in ['как', 'что', 'какой', 'сколько', 'почему', 'можно ли']):
        if any(kw in text_lower for kw in ['есть', 'пить', 'делать', 'качать', 'тренир', 'худе', 'набира']):
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
        
        for col, default in [("voice_mode", "0"), ("language", "'ru'"), ("profile_step", "NULL")]:
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
        
        # Заполняем упражнения
        cursor.execute("SELECT COUNT(*) FROM exercises")
        if cursor.fetchone()[0] == 0:
            exercises = [
                # Ноги
                ("Приседания", "присед,squat,приседы", "ноги, ягодицы", 
                 "https://media.giphy.com/media/1qfKN8Dt0CRdCRxz9q/giphy.gif",
                 "https://www.youtube.com/watch?v=aclHkVaku9U"),
                ("Выпады", "выпад,lunges,выпады вперёд", "ноги, ягодицы",
                 "https://media.giphy.com/media/l0HlNQ03J5JxX6lva/giphy.gif",
                 "https://www.youtube.com/watch?v=QOVaHwm-Q6U"),
                ("Приседания с гантелями", "гоблет,goblet squat", "ноги, ягодицы",
                 "https://media.giphy.com/media/xUOxfaAIH6BdNd3Bv2/giphy.gif",
                 "https://www.youtube.com/watch?v=MeIiIdhvXT4"),
                
                # Грудь
                ("Отжимания", "отжимание,push-up,pushup", "грудь, трицепс",
                 "https://media.giphy.com/media/7YCC7NnFgkUEFOfVNy/giphy.gif",
                 "https://www.youtube.com/watch?v=IODxDxX7oi4"),
                ("Жим лёжа", "жим лежа,bench press,жим штанги", "грудь, трицепс",
                 "https://media.giphy.com/media/7T5wldGkk7XgCyuNUV/giphy.gif",
                 "https://www.youtube.com/watch?v=rT7DgCr-3pg"),
                ("Отжимания на брусьях", "брусья,dips", "грудь, трицепс",
                 "https://media.giphy.com/media/l2JhNkxsr2EtjfCaA/giphy.gif",
                 "https://www.youtube.com/watch?v=2z8JmcrW-As"),
                
                # Спина
                ("Подтягивания", "подтягивание,pull-up,pullup", "спина, бицепс",
                 "https://media.giphy.com/media/3o7TKDnKzLluH40Zzq/giphy.gif",
                 "https://www.youtube.com/watch?v=eGo4IYlbE5g"),
                ("Тяга в наклоне", "тяга штанги,bent over row", "спина, бицепс",
                 "https://media.giphy.com/media/3ohc11UljvpPKWeNva/giphy.gif",
                 "https://www.youtube.com/watch?v=G8l_8chR5BE"),
                ("Гиперэкстензия", "гиперэкстензии,hyperextension", "спина, поясница",
                 "https://media.giphy.com/media/xT9DPIBYf0pAviBLzO/giphy.gif",
                 "https://www.youtube.com/watch?v=ph3pddpKzzw"),
                
                # Пресс
                ("Планка", "plank,планки", "пресс, кор",
                 "https://media.giphy.com/media/xT8qBvgKeMvMGSJNgA/giphy.gif",
                 "https://www.youtube.com/watch?v=pSHjTRCQxIw"),
                ("Скручивания", "crunches,кранчи,пресс", "пресс",
                 "https://media.giphy.com/media/l3q2VZLzFKvFTbAlo/giphy.gif",
                 "https://www.youtube.com/watch?v=Xyd_fa5zoEU"),
                ("Подъём ног", "leg raise,подъём ног в висе", "пресс, кор",
                 "https://media.giphy.com/media/3oriO6qJiXajN0TyDu/giphy.gif",
                 "https://www.youtube.com/watch?v=hdng3Nm1x_E"),
                
                # Руки
                ("Подъём на бицепс", "бицепс,bicep curl,сгибание на бицепс", "руки, бицепс",
                 "https://media.giphy.com/media/xUOwGmsFStnxzIGC2s/giphy.gif",
                 "https://www.youtube.com/watch?v=ykJmrZ5v0Oo"),
                ("Французский жим", "трицепс,french press", "руки, трицепс",
                 "https://media.giphy.com/media/l0HlQoLBg7MOsqUxy/giphy.gif",
                 "https://www.youtube.com/watch?v=d_KZxkY_0cM"),
                
                # Плечи
                ("Жим гантелей", "жим гантелей стоя,shoulder press", "плечи",
                 "https://media.giphy.com/media/fxTgmTbqWFqdNdH1M5/giphy.gif",
                 "https://www.youtube.com/watch?v=qEwKCR5JCog"),
                
                # Кардио / Всё тело
                ("Бёрпи", "burpee,берпи", "всё тело, кардио",
                 "https://media.giphy.com/media/23hPPMRgPxbNBlPQe3/giphy.gif",
                 "https://www.youtube.com/watch?v=TU8QYVW0gDU"),
                ("Jumping Jacks", "джампинг джек,прыжки", "всё тело, кардио",
                 "https://media.giphy.com/media/l3q2ZBvNqKfULS7zq/giphy.gif",
                 "https://www.youtube.com/watch?v=c4DAnQ6DtF8"),
                ("Становая тяга", "становая,deadlift", "спина, ноги",
                 "https://media.giphy.com/media/3oEjHGr1Fhz0kyv8Ig/giphy.gif",
                 "https://www.youtube.com/watch?v=op9kVnSso6Q"),
            ]
            cursor.executemany(
                "INSERT INTO exercises (name, aliases, muscles, gif_url, video_url) VALUES (?, ?, ?, ?, ?)",
                exercises
            )
            logger.info(f"Inserted {len(exercises)} exercises")
    
    logger.info("Database initialized")


# ============================================================
# === ПОЛЬЗОВАТЕЛИ ===
# ============================================================

def generate_referral_code(user_id: int) -> str:
    import hashlib
    return hashlib.md5(f"{user_id}{datetime.now()}".encode()).hexdigest()[:8]


def get_or_create_user(user_id: int, username: str = None):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        
        if not cursor.fetchone():
            today = datetime.now().strftime("%Y-%m-%d")
            ref_code = generate_referral_code(user_id)
            cursor.execute("""
                INSERT INTO users (user_id, username, free_questions, last_reset, referral_code)
                VALUES (?, ?, 5, ?, ?)
            """, (user_id, username, today, ref_code))
            cursor.execute("INSERT INTO stats (user_id) VALUES (?)", (user_id,))
            logger.info(f"New user: {user_id}")


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


def is_premium(user_id: int) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or not row[0]:
            return False
        try:
            return datetime.now().date() <= datetime.strptime(row[0], "%Y-%m-%d").date()
        except:
            return False


def activate_premium(user_id: int, days: int = 30):
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
        current = cursor.fetchone()
        
        if current and current[0]:
            try:
                base = max(datetime.now(), datetime.strptime(current[0], "%Y-%m-%d"))
            except:
                base = datetime.now()
        else:
            base = datetime.now()
        
        end = (base + timedelta(days=days)).strftime("%Y-%m-%d")
        cursor.execute("UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?", (end, user_id))
        logger.info(f"Premium activated: {user_id} for {days} days")


def process_referral(new_user_id: int, ref_code: str) -> bool:
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE referral_code = ?", (ref_code,))
        result = cursor.fetchone()
        
        if not result or result[0] == new_user_id:
            return False
        
        referrer_id = result[0]
        
        cursor.execute("SELECT referred_by FROM users WHERE user_id = ?", (new_user_id,))
        already = cursor.fetchone()
        if already and already[0]:
            return False
        
        cursor.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, new_user_id))
        activate_premium(referrer_id, 7)
        cursor.execute("UPDATE stats SET referrals_count = referrals_count + 1 WHERE user_id = ?", (referrer_id,))
        return True


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
        
        # Точное совпадение
        cursor.execute("SELECT name, muscles, gif_url, video_url FROM exercises WHERE LOWER(name) = ?", (q,))
        row = cursor.fetchone()
        
        # По алиасам
        if not row:
            cursor.execute("SELECT name, muscles, gif_url, video_url FROM exercises WHERE LOWER(aliases) LIKE ?", (f"%{q}%",))
            row = cursor.fetchone()
        
        # Частичное совпадение
        if not row:
            cursor.execute("SELECT name, muscles, gif_url, video_url FROM exercises WHERE LOWER(name) LIKE ?", (f"%{q}%",))
            row = cursor.fetchone()
        
        if row:
            return {'name': row[0], 'muscles': row[1], 'gif_url': row[2], 'video_url': row[3]}
    return None


def get_exercises_by_group(group: str) -> list:
    """Получает упражнения по группе мышц"""
    group_keywords = {
        'legs': ['ноги', 'ягодиц', 'бёдр', 'квадрицепс'],
        'arms': ['руки', 'бицепс', 'трицепс', 'предплечь'],
        'back': ['спина', 'широчайш', 'поясниц'],
        'chest': ['грудь', 'груд'],
        'abs': ['пресс', 'кор', 'живот'],
        'shoulders': ['плечи', 'дельт'],
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
        r"научи (.+?)(?:\?|$|\.)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            ex = re.sub(r'\b(упражнение|правильно|мне)\b', '', match.group(1)).strip()
            if len(ex) > 2:
                return ex
    return None


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
        profile_text = f"\nПрофиль: {profile.get('height', '?')}см, {profile.get('weight', '?')}кг, цель: {profile['goal']}, место: {profile.get('location', '?')}"
    
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
        return "⚠️ Ошибка AI. Попробуй позже."


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
        'question': "📏 **Шаг 1/6: Укажи свой рост**\n\nВведи число в сантиметрах (например: 175):",
        'next': 'weight',
        'field': 'height',
        'validate': lambda x: 100 <= float(re.search(r'\d+', x).group()) <= 250 if re.search(r'\d+', x) else False,
        'parse': lambda x: int(re.search(r'\d+', x).group()),
        'error': "❌ Введи корректный рост (100-250 см)"
    },
    'weight': {
        'question': "⚖️ **Шаг 2/6: Укажи свой вес**\n\nВведи число в килограммах (например: 75):",
        'next': 'age',
        'field': 'weight',
        'validate': lambda x: 30 <= float(re.search(r'[\d.]+', x).group()) <= 300 if re.search(r'[\d.]+', x) else False,
        'parse': lambda x: float(re.search(r'[\d.]+', x).group()),
        'error': "❌ Введи корректный вес (30-300 кг)"
    },
    'age': {
        'question': "🎂 **Шаг 3/6: Укажи свой возраст**\n\nВведи число (например: 25):",
        'next': 'gender',
        'field': 'age',
        'validate': lambda x: 10 <= int(re.search(r'\d+', x).group()) <= 100 if re.search(r'\d+', x) else False,
        'parse': lambda x: int(re.search(r'\d+', x).group()),
        'error': "❌ Введи корректный возраст (10-100 лет)"
    },
    'gender': {
        'question': "👤 **Шаг 4/6: Укажи пол**",
        'next': 'goal',
        'field': 'gender',
        'buttons': [
            [InlineKeyboardButton("👨 Мужской", callback_data="profile_gender_мужской"),
             InlineKeyboardButton("👩 Женский", callback_data="profile_gender_женский")]
        ]
    },
    'goal': {
        'question': "🎯 **Шаг 5/6: Какая у тебя цель?**",
        'next': 'location',
        'field': 'goal',
        'buttons': [
            [InlineKeyboardButton("🔥 Похудеть", callback_data="profile_goal_похудеть")],
            [InlineKeyboardButton("💪 Набрать массу", callback_data="profile_goal_набрать массу")],
            [InlineKeyboardButton("✨ Поддержать форму", callback_data="profile_goal_поддержать форму")],
            [InlineKeyboardButton("🏋️ Развить силу", callback_data="profile_goal_развить силу")]
        ]
    },
    'location': {
        'question': "📍 **Шаг 6/6: Где тренируешься?**",
        'next': None,
        'field': 'location',
        'buttons': [
            [InlineKeyboardButton("🏠 Дома", callback_data="profile_location_дома")],
            [InlineKeyboardButton("🏋️ В зале", callback_data="profile_location_в зале")],
            [InlineKeyboardButton("🌳 На улице", callback_data="profile_location_на улице")]
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
    
    if 'buttons' in step:
        await update.message.reply_text("👆 Выбери один из вариантов выше")
        return True
    
    try:
        if not step['validate'](text):
            await update.message.reply_text(step['error'])
            return True
        
        value = step['parse'](text)
        update_user_profile(user_id, **{step['field']: value})
        
        next_step = step['next']
        if next_step:
            set_profile_step(user_id, next_step)
            next_step_data = PROFILE_STEPS[next_step]
            
            if 'buttons' in next_step_data:
                await update.message.reply_text(
                    next_step_data['question'],
                    reply_markup=InlineKeyboardMarkup(next_step_data['buttons']),
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(next_step_data['question'], parse_mode="Markdown")
        else:
            await finish_profile_setup(update.message, user_id)
        
        return True
        
    except Exception as e:
        logger.error(f"Profile step error: {e}")
        await update.message.reply_text(step['error'])
        return True


async def finish_profile_setup(message, user_id: int):
    set_profile_step(user_id, None)
    profile = get_user_profile(user_id)
    
    await message.reply_text(
        "✅ **Профиль создан!**\n\n"
        f"📏 Рост: **{profile.get('height')} см**\n"
        f"⚖️ Вес: **{profile.get('weight')} кг**\n"
        f"🎂 Возраст: **{profile.get('age')} лет**\n"
        f"👤 Пол: **{profile.get('gender')}**\n"
        f"🎯 Цель: **{profile.get('goal')}**\n"
        f"📍 Место: **{profile.get('location')}**\n\n"
        "Теперь я могу составлять персональные программы! 💪\n\n"
        "Попробуй:\n"
        "• Составь тренировку на сегодня\n"
        "• Как правильно делать приседания?\n"
        "• Дай рецепт на завтрак",
        parse_mode="Markdown"
    )


# ============================================================
# === КОМАНДЫ ===
# ============================================================

@handle_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)
    set_profile_step(user.id, None)
    
    if not await check_subscription(user.id, context) and user.id not in ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
            [InlineKeyboardButton("✅ Проверить", callback_data="check_subscription")]
        ]
        await update.message.reply_text("🔒 Подпишись на канал для доступа!", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if context.args:
        if process_referral(user.id, context.args[0]):
            await update.message.reply_text("🎁 Бонус начислен пригласившему!")
    
    keyboard = [
        [InlineKeyboardButton("👤 Создать профиль", callback_data="setup_profile")],
        [InlineKeyboardButton("💪 Тренировка", callback_data="workout"),
         InlineKeyboardButton("🍽️ Рецепт", callback_data="recipe")],
        [InlineKeyboardButton("🏋️ Упражнения", callback_data="exercises_menu"),
         InlineKeyboardButton("📊 Прогресс", callback_data="progress")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton("🔥 Premium", callback_data="subscribe")]
    ]
    
    profile_status = "✅ Профиль заполнен" if has_profile(user.id) else "❌ Профиль не заполнен"
    
    await update.message.reply_text(
        f"💪 Привет, {user.first_name}!\n\n"
        f"Я **Murasaki Sport** — твой AI-тренер!\n\n"
        f"📋 {profile_status}\n\n"
        "**Я могу:**\n"
        "• Составлять программы тренировок\n"
        "• Показывать технику упражнений с GIF\n"
        "• Давать рецепты с КБЖУ\n\n"
        "👇 Выбери действие или задай вопрос",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


@handle_errors
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Как пользоваться ботом:**\n\n"
        "**1. Создай профиль** — нажми кнопку\n\n"
        "**2. Задавай вопросы о спорте:**\n"
        "• Составь тренировку на ноги\n"
        "• Как правильно делать отжимания?\n"
        "• Что съесть после тренировки?\n\n"
        "**3. Записывай вес:**\n"
        "`Вес 75.5`\n\n"
        "**Команды:**\n"
        "/start — Меню\n"
        "/profile — Профиль\n"
        "/settings — Настройки\n"
        "/stats — Статистика",
        parse_mode="Markdown"
    )


@handle_errors
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_user_settings(update.effective_user.id)
    
    mode_text = "🎙️ Голос" if settings['voice_mode'] else "📝 Текст"
    lang_info = {'ru': '🇷🇺 Русский', 'en': '🇺🇸 English', 'ko': '🇰🇷 한국어'}
    
    keyboard = [
        [InlineKeyboardButton(f"{'🔊' if settings['voice_mode'] else '🔇'} Режим: {mode_text}", callback_data="toggle_voice")],
        [InlineKeyboardButton("🇷🇺", callback_data="lang_ru"),
         InlineKeyboardButton("🇺🇸", callback_data="lang_en"),
         InlineKeyboardButton("🇰🇷", callback_data="lang_ko")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
    ]
    
    await update.message.reply_text(
        f"⚙️ **Настройки**\n\n"
        f"📢 Режим: **{mode_text}**\n"
        f"🌍 Язык: **{lang_info.get(settings['language'], '🇷🇺 Русский')}**",
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
            "❌ **Профиль не заполнен**\n\nНажми кнопку ниже:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    prem = "💎 Premium" if is_premium(user_id) else "🆓 Free"
    
    await update.message.reply_text(
        f"👤 **Твой профиль** ({prem})\n\n"
        f"📏 Рост: **{p.get('height', '—')} см**\n"
        f"⚖️ Вес: **{p.get('weight', '—')} кг**\n"
        f"🎂 Возраст: **{p.get('age', '—')} лет**\n"
        f"👤 Пол: **{p.get('gender', '—')}**\n"
        f"🎯 Цель: **{p.get('goal', '—')}**\n"
        f"📍 Место: **{p.get('location', '—')}**",
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
        status = "💎 Premium" if row[1] else f"🆓 Free ({row[0]}/5)"
        await update.message.reply_text(
            f"📊 **Статистика**\n\n"
            f"Статус: {status}\n\n"
            f"💬 Вопросов: **{row[2] or 0}**\n"
            f"💪 Тренировок: **{row[3] or 0}**\n"
            f"👥 Рефералов: **{row[4] or 0}**",
            parse_mode="Markdown"
        )


@handle_errors
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
        code = cursor.fetchone()[0]
    
    ref_link = f"https://t.me/{context.bot.username}?start={code}"
    
    await update.message.reply_text(
        f"👥 **Реферальная программа**\n\n"
        f"🎁 **+7 дней Premium** за друга!\n\n"
        f"Твоя ссылка:\n`{ref_link}`",
        parse_mode="Markdown"
    )


@handle_errors
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text("✅ История очищена!")


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
    text_lower = text.lower()
    
    settings = get_user_settings(user.id)
    voice_mode = settings['voice_mode']
    language = settings['language']
    
    # === ЗАПОЛНЕНИЕ ПРОФИЛЯ ===
    profile_step = get_profile_step(user.id)
    if profile_step:
        handled = await process_profile_step(update, context, user.id, text)
        if handled:
            return
    
    # === ЗАПИСЬ ВЕСА ===
    weight_match = re.match(r'^вес\s+(\d+\.?\d*)', text_lower)
    if weight_match:
        weight = float(weight_match.group(1))
        if 30 <= weight <= 300:
            add_weight_record(user.id, weight)
            history = get_weight_history(user.id, 2)
            
            response = f"✅ **Вес записан: {weight} кг**\n"
            if len(history) >= 2:
                diff = weight - history[1][0]
                if diff > 0:
                    response += f"📈 +{diff:.1f} кг"
                elif diff < 0:
                    response += f"📉 {diff:.1f} кг — прогресс!"
            
            await send_response(update, response, voice_mode, language, user.id)
            return
    
    # === УПРАЖНЕНИЕ С GIF ===
    ex_name = extract_exercise_name(text)
    if ex_name:
        can_ask, _ = can_ask_question(user.id)
        if not can_ask:
            keyboard = [[InlineKeyboardButton("💎 Premium", callback_data="subscribe")]]
            await update.message.reply_text("⚠️ Лимит исчерпан!", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        await update.message.chat.send_action("typing")
        
        exercise = find_exercise(ex_name)
        ai_response = await groq_chat(user.id, f"Объясни технику упражнения '{ex_name}'. Исходное положение, выполнение, частые ошибки. Кратко.", use_context=False)
        
        if not is_premium(user.id):
            use_question(user.id)
        
        if exercise and exercise.get('gif_url'):
            try:
                keyboard = []
                if exercise.get('video_url'):
                    keyboard.append([InlineKeyboardButton("▶️ YouTube", url=exercise['video_url'])])
                
                await update.message.reply_animation(
                    animation=exercise['gif_url'],
                    caption=f"💪 **{exercise['name']}**\n🎯 {exercise['muscles']}\n\n{ai_response[:800]}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
                )
                return
            except Exception as e:
                logger.error(f"GIF error: {e}")
        
        await send_response(update, f"💪 **{ex_name.title()}**\n\n{ai_response}", voice_mode, language, user.id)
        return
    
    # === ФИЛЬТР СООБЩЕНИЙ ===
    if not is_fitness_question(text):
        await update.message.reply_text(
            "🏋️ Я — фитнес-тренер и отвечаю на вопросы о:\n\n"
            "• Тренировках и упражнениях\n"
            "• Питании и диетах\n"
            "• Здоровье и восстановлении\n\n"
            "**Примеры:**\n"
            "• Составь тренировку на ноги\n"
            "• Как делать приседания?\n"
            "• Что съесть после тренировки?",
            parse_mode="Markdown"
        )
        return
    
    # === ЛИМИТЫ ===
    can_ask, remaining = can_ask_question(user.id)
    if not can_ask:
        keyboard = [[InlineKeyboardButton("💎 Premium", callback_data="subscribe")]]
        await update.message.reply_text(
            "⚠️ **Лимит исчерпан!**\n\n"
            "💎 Premium — безлимит\n"
            "👥 Или /referral",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    # === AI ===
    await update.message.chat.send_action("typing")
    
    response = await groq_chat(user.id, text)
    
    if not is_premium(user.id):
        use_question(user.id)
    
    footer = ""
    if not is_premium(user.id):
        _, new_rem = can_ask_question(user.id)
        if new_rem <= 2:
            footer = f"\n\n💡 Осталось: {new_rem}/5"
    
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
    if query.data == "check_subscription":
        if await check_subscription(user_id, context):
            await query.message.edit_text("✅ Подписка подтверждена! Нажми /start")
        else:
            await query.answer("❌ Подписка не найдена!", show_alert=True)
        return
    
    if query.data == "back_to_menu":
        try:
            await query.message.delete()
        except:
            pass
        return
    
    # === ПРОФИЛЬ ===
    if query.data == "setup_profile":
        await start_profile_setup(query.message, user_id)
        return
    
    if query.data.startswith("profile_"):
        parts = query.data.split("_")
        if len(parts) >= 3:
            field = parts[1]
            value = "_".join(parts[2:])
            
            update_user_profile(user_id, **{field: value})
            
            current_step = field
            if current_step in PROFILE_STEPS:
                next_step = PROFILE_STEPS[current_step]['next']
                
                if next_step:
                    set_profile_step(user_id, next_step)
                    next_data = PROFILE_STEPS[next_step]
                    
                    if 'buttons' in next_data:
                        await query.message.edit_text(
                            next_data['question'],
                            reply_markup=InlineKeyboardMarkup(next_data['buttons']),
                            parse_mode="Markdown"
                        )
                    else:
                        await query.message.edit_text(next_data['question'], parse_mode="Markdown")
                else:
                    await finish_profile_setup(query.message, user_id)
        return
    
    # ============================================================
    # === УПРАЖНЕНИЯ ===
    # ============================================================
    
    if query.data == "exercises_menu":
        keyboard = [
            [InlineKeyboardButton("🦵 Ноги", callback_data="ex_group_legs"),
             InlineKeyboardButton("💪 Руки", callback_data="ex_group_arms")],
            [InlineKeyboardButton("🔙 Спина", callback_data="ex_group_back"),
             InlineKeyboardButton("🫁 Грудь", callback_data="ex_group_chest")],
            [InlineKeyboardButton("🎯 Пресс", callback_data="ex_group_abs"),
             InlineKeyboardButton("🫀 Кардио", callback_data="ex_group_cardio")],
            [InlineKeyboardButton("📋 Все упражнения", callback_data="ex_group_all")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        await query.message.reply_text(
            "🏋️ **Выбери группу мышц:**\n\n"
            "Или напиши: «Как делать приседания?»",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    if query.data.startswith("ex_group_"):
        group = query.data.replace("ex_group_", "")
        
        exercises = get_exercises_by_group(group)
        
        if not exercises:
            await query.answer("Упражнений пока нет", show_alert=True)
            return
        
        keyboard = []
        for ex in exercises:
            # Обрезаем имя для callback_data (макс 64 байта)
            safe_name = ex['name'][:20]
            keyboard.append([InlineKeyboardButton(f"💪 {ex['name']}", callback_data=f"show_ex_{safe_name}")])
        
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="exercises_menu")])
        
        group_names = {
            'legs': '🦵 Ноги', 'arms': '💪 Руки', 'back': '🔙 Спина',
            'chest': '🫁 Грудь', 'abs': '🎯 Пресс', 'cardio': '🫀 Кардио', 'all': '📋 Все'
        }
        
        await query.message.edit_text(
            f"**{group_names.get(group, 'Упражнения')}**\n\nВыбери упражнение:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    if query.data.startswith("show_ex_"):
        exercise_query = query.data.replace("show_ex_", "")
        
        exercise = find_exercise(exercise_query)
        
        if not exercise:
            await query.answer("Упражнение не найдено", show_alert=True)
            return
        
        await query.message.edit_text("⏳ Загружаю...")
        
        ai_response = await groq_chat(
            user_id, 
            f"Объясни технику упражнения '{exercise['name']}'. Исходное положение, выполнение, частые ошибки. Кратко.",
            use_context=False
        )
        
        text = f"💪 **{exercise['name']}**\n\n"
        if exercise.get('muscles'):
            text += f"🎯 Мышцы: {exercise['muscles']}\n\n"
        text += ai_response
        
        keyboard = []
        if exercise.get('video_url'):
            keyboard.append([InlineKeyboardButton("▶️ Видео на YouTube", url=exercise['video_url'])])
        keyboard.append([InlineKeyboardButton("◀️ К списку", callback_data="exercises_menu")])
        
        if exercise.get('gif_url'):
            try:
                await query.message.delete()
                await context.bot.send_animation(
                    chat_id=user_id,
                    animation=exercise['gif_url'],
                    caption=text[:1024],
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return
            except Exception as e:
                logger.error(f"GIF error: {e}")
        
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    # === НАСТРОЙКИ ===
    if query.data == "settings":
        settings = get_user_settings(user_id)
        mode_text = "🎙️ Голос" if settings['voice_mode'] else "📝 Текст"
        
        keyboard = [
            [InlineKeyboardButton(f"{'🔊' if settings['voice_mode'] else '🔇'} Режим: {mode_text}", callback_data="toggle_voice")],
            [InlineKeyboardButton("🇷🇺", callback_data="lang_ru"),
             InlineKeyboardButton("🇺🇸", callback_data="lang_en"),
             InlineKeyboardButton("🇰🇷", callback_data="lang_ko")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        await query.message.edit_text(
            f"⚙️ **Настройки**\n\n📢 Режим: **{mode_text}**",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    if query.data == "toggle_voice":
        settings = get_user_settings(user_id)
        new_mode = not settings['voice_mode']
        set_voice_mode(user_id, new_mode)
        await query.answer("🎙️ Голос!" if new_mode else "📝 Текст!", show_alert=True)
        
        mode_text = "🎙️ Голос" if new_mode else "📝 Текст"
        keyboard = [
            [InlineKeyboardButton(f"{'🔊' if new_mode else '🔇'} Режим: {mode_text}", callback_data="toggle_voice")],
            [InlineKeyboardButton("🇷🇺", callback_data="lang_ru"),
             InlineKeyboardButton("🇺🇸", callback_data="lang_en"),
             InlineKeyboardButton("🇰🇷", callback_data="lang_ko")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back_to_menu")]
        ]
        await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if query.data.startswith("lang_"):
        lang = query.data.replace("lang_", "")
        set_user_language(user_id, lang)
        lang_names = {'ru': '🇷🇺 Русский', 'en': '🇺🇸 English', 'ko': '🇰🇷 한국어'}
        await query.answer(f"Язык: {lang_names.get(lang)}", show_alert=True)
        return
    
    # === ПРОГРЕСС ===
    if query.data == "progress":
        records = get_weight_history(user_id, 10)
        
        if not records:
            await query.message.reply_text(
                "📊 **Прогресс**\n\nЗаписей нет.\n\nНапиши: `Вес 75.5`",
                parse_mode="Markdown"
            )
            return
        
        lines = []
        for w, d in records:
            try:
                dt = datetime.fromisoformat(d).strftime("%d.%m")
            except:
                dt = d[:10]
            lines.append(f"• {dt}: **{w} кг**")
        
        change = ""
        if len(records) >= 2:
            diff = records[0][0] - records[-1][0]
            if diff > 0:
                change = f"\n\n📈 +{diff:.1f} кг"
            elif diff < 0:
                change = f"\n\n📉 {diff:.1f} кг — прогресс!"
        
        await query.message.reply_text(
            "📊 **Прогресс:**\n\n" + "\n".join(lines) + change,
            parse_mode="Markdown"
        )
        return
    
    # === ТРЕНИРОВКА ===
    if query.data == "workout":
        if not has_profile(user_id):
            keyboard = [[InlineKeyboardButton("👤 Создать профиль", callback_data="setup_profile")]]
            await query.message.reply_text("❌ Сначала создай профиль!", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        keyboard = [
            [InlineKeyboardButton("💪 Силовая", callback_data="w_strength"),
             InlineKeyboardButton("🔥 Кардио", callback_data="w_cardio")],
            [InlineKeyboardButton("🧘 Растяжка", callback_data="w_stretch"),
             InlineKeyboardButton("⚡ HIIT", callback_data="w_hiit")]
        ]
        await query.message.reply_text("💪 **Выбери тип:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    if query.data.startswith("w_"):
        wtype = query.data.replace("w_", "")
        profile = get_user_profile(user_id)
        types_ru = {'strength': 'силовую', 'cardio': 'кардио', 'stretch': 'на растяжку', 'hiit': 'HIIT'}
        
        await query.message.edit_text("💪 Составляю тренировку...")
        
        response = await groq_chat(
            user_id, 
            f"Составь {types_ru.get(wtype, 'силовую')} тренировку. "
            f"Место: {profile.get('location', 'дом')}, цель: {profile.get('goal', 'форма')}. "
            f"Разминка, упражнения с подходами, заминка.",
            use_context=False
        )
        
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO workouts (user_id, workout_text) VALUES (?, ?)", (user_id, response))
            wid = cursor.lastrowid
        
        keyboard = [[InlineKeyboardButton("✅ Выполнено!", callback_data=f"done_{wid}")]]
        await query.message.edit_text(
            f"💪 **Тренировка:**\n\n{response}",
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
        
        await query.answer("🔥 Отлично!", show_alert=True)
        await query.message.reply_text("✅ Тренировка выполнена! 💪")
        return
    
    # === РЕЦЕПТ ===
    if query.data == "recipe":
        keyboard = [
            [InlineKeyboardButton("🍳 Завтрак", callback_data="r_breakfast"),
             InlineKeyboardButton("🥗 Обед", callback_data="r_lunch")],
            [InlineKeyboardButton("🍲 Ужин", callback_data="r_dinner"),
             InlineKeyboardButton("💪 Белковое", callback_data="r_protein")]
        ]
        await query.message.reply_text("🍽️ **Что приготовить?**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    if query.data.startswith("r_"):
        rtype = query.data.replace("r_", "")
        types_ru = {'breakfast': 'завтрак', 'lunch': 'обед', 'dinner': 'ужин', 'protein': 'высокобелковое блюдо'}
        
        await query.message.edit_text("🍽️ Готовлю рецепт...")
        
        profile = get_user_profile(user_id)
        goal = f" Цель: {profile.get('goal')}." if profile.get('goal') else ""
        
        response = await groq_chat(
            user_id, 
            f"Дай рецепт: {types_ru.get(rtype, 'блюдо')}.{goal} Ингредиенты, приготовление, КБЖУ.",
            use_context=False
        )
        
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE stats SET recipes_generated = recipes_generated + 1 WHERE user_id = ?", (user_id,))
        
        keyboard = [[InlineKeyboardButton("🔄 Другой", callback_data="recipe")]]
        await query.message.edit_text(
            f"🍽️ **Рецепт:**\n\n{response}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    # === ПОДПИСКА ===
    if query.data == "subscribe":
        keyboard = [
            [InlineKeyboardButton("💳 Оплатить 99₽", callback_data="pay")],
            [InlineKeyboardButton("👥 Бесплатно (друзья)", callback_data="ref_info")]
        ]
        await query.message.reply_text(
            "💎 **Premium (99₽/мес)**\n\n"
            "✅ Безлимитные вопросы\n"
            "✅ Голосовые ответы\n"
            "✅ Память диалога",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    if query.data == "pay":
        if not PROVIDER_TOKEN:
            await query.message.reply_text("⚠️ Платежи недоступны. /referral")
            return
        await context.bot.send_invoice(
            chat_id=user_id, title="Premium 30 дней", description="Безлимит",
            payload="premium", provider_token=PROVIDER_TOKEN, currency="RUB",
            prices=[LabeledPrice("Premium", 9900)]
        )
        return
    
    if query.data == "ref_info":
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
            code = cursor.fetchone()[0]
        await query.message.reply_text(
            f"👥 **+7 дней за друга!**\n\n`https://t.me/{context.bot.username}?start={code}`",
            parse_mode="Markdown"
        )
        return


# ============================================================
# === АДМИН ===
# ============================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text(f"❌ Нет доступа.\n\nТвой ID: `{user_id}`", parse_mode="Markdown")
        return
    
    with db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
        premium = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM exercises")
        exercises = cursor.fetchone()[0]
    
    await update.message.reply_text(
        f"🔧 **Админ**\n\n"
        f"👥 Юзеров: {users}\n"
        f"💎 Premium: {premium}\n"
        f"🏋️ Упражнений: {exercises}\n\n"
        f"`/give_premium ID 30`",
        parse_mode="Markdown"
    )


async def give_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("`/give_premium ID [дни]`", parse_mode="Markdown")
        return
    
    try:
        target = int(context.args[0])
        days = int(context.args[1]) if len(context.args) > 1 else 30
        activate_premium(target, days)
        await update.message.reply_text(f"✅ Premium {days} дней для {target}")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ============================================================
# === ПЛАТЕЖИ ===
# ============================================================

@handle_errors
async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


@handle_errors
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activate_premium(update.effective_user.id)
    await update.message.reply_text("🎉 Premium активирован на 30 дней!")


# ============================================================
# === MAIN ===
# ============================================================

def main():
    logger.info("=" * 50)
    logger.info("Starting Murasaki Sport Bot...")
    logger.info(f"Database: {DB_NAME}")
    logger.info(f"Admin IDs: {ADMIN_IDS}")
    
    if ADMIN_IDS == [123456789]:
        logger.warning("⚠️ ADMIN_IDS не настроен!")
    
    logger.info("=" * 50)
    
    init_db()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("give_premium", give_premium_command))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Платежи
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    
    logger.info("✅ Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
