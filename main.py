import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
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
import edge_tts
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
IMGBB_API_URL = "https://api.imgbb.com/1/upload"

# === RAILWAY VOLUME ===
# Проверяем, есть ли примонтированный volume
RAILWAY_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")

if RAILWAY_VOLUME and os.path.exists(RAILWAY_VOLUME):
    # Production (Railway с Volume)
    DATA_DIR = RAILWAY_VOLUME
    print(f"✅ Using Railway Volume: {DATA_DIR}")
else:
    # Local development
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))
    print(f"📁 Using local directory: {DATA_DIR}")

# Пути к файлам
DB_NAME = os.path.join(DATA_DIR, "sport.db")
LOG_DIR = os.path.join(DATA_DIR, "logs")
VOICE_DIR = os.path.join(DATA_DIR, "voice_temp")

# Создаём папки при импорте
for directory in [LOG_DIR, VOICE_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)
        print(f"📁 Created: {directory}")

# Админы
ADMIN_IDS = [123456789]  # ← Замени на свой ID
REQUIRED_CHANNEL = "@Murasaki_lab"

# Retry настройки
MAX_RETRIES = 3
RETRY_DELAY = 1

# Маппинг языков на голоса edge-tts
VOICE_MAP = {
    'ru': 'ru-RU-DmitryNeural',
    'en': 'en-US-ChristopherNeural',
    'ko': 'ko-KR-HyunsuNeural'
}

VOICE_MAP_FEMALE = {
    'ru': 'ru-RU-SvetlanaNeural',
    'en': 'en-US-JennyNeural',
    'ko': 'ko-KR-SunHiNeural'
}

# Системный промпт
SYSTEM_PROMPT = """Ты персональный AI-тренер и нутрициолог Murasaki Sport. 

Твоя задача:
- Составлять программы тренировок (дом/зал)
- Подбирать упражнения под инвентарь
- Давать рецепты с КБЖУ
- Консультировать по питанию
- Мотивировать и поддерживать

Стиль: дружелюбный, мотивирующий, конкретный.
Ответы: 3-5 предложений (кроме программ).
Язык: русский.

Важно: не ставь диагнозы, рекомендуй врача при необходимости."""


# ============================================================
# === ЛОГИРОВАНИЕ ===
# ============================================================

def setup_logging():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)
    
    log_format = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Файл для всех логов
    file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, 'bot.log'),
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_format)
    logger.addHandler(file_handler)
    
    # Файл для ошибок
    error_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, 'errors.log'),
        maxBytes=5*1024*1024,
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(log_format)
    logger.addHandler(error_handler)
    
    # Ежедневный лог
    daily_handler = TimedRotatingFileHandler(
        os.path.join(LOG_DIR, 'daily.log'),
        when='midnight',
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    daily_handler.setLevel(logging.INFO)
    daily_handler.setFormatter(log_format)
    logger.addHandler(daily_handler)
    
    # Консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%H:%M:%S')
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('telegram').setLevel(logging.WARNING)
    
    return logger

logger = setup_logging()


# ============================================================
# === ДЕКОРАТОРЫ И УТИЛИТЫ ===
# ============================================================

def handle_errors(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        try:
            return await func(update, context, *args, **kwargs)
        except NetworkError as e:
            logger.warning(f"Network error in {func.__name__}: {e}")
            await safe_reply(update, "⚠️ Проблемы с сетью. Попробуй ещё раз.")
        except TimedOut as e:
            logger.warning(f"Timeout in {func.__name__}: {e}")
            await safe_reply(update, "⚠️ Превышено время ожидания.")
        except TelegramError as e:
            logger.error(f"Telegram error in {func.__name__}: {e}")
            await safe_reply(update, "⚠️ Ошибка Telegram.")
        except sqlite3.Error as e:
            logger.error(f"Database error in {func.__name__}: {e}\n{traceback.format_exc()}")
            await safe_reply(update, "⚠️ Ошибка базы данных.")
            await notify_admins(context, f"🚨 DB Error:\n```\n{e}\n```")
        except Exception as e:
            logger.error(f"Unexpected error in {func.__name__}: {e}\n{traceback.format_exc()}")
            await safe_reply(update, "⚠️ Тренер ушёл на перерыв, попробуй через минуту.")
            await notify_admins(context, f"🚨 Error in {func.__name__}:\n```\n{e}\n```")
    return wrapper


async def safe_reply(update: Update, text: str):
    try:
        if update.callback_query:
            await update.callback_query.message.reply_text(text)
        elif update.message:
            await update.message.reply_text(text)
    except Exception as e:
        logger.error(f"Failed to send error message: {e}")


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, message[:4000], parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")


def db_connection():
    class DBConnection:
        def __init__(self):
            self.conn = None
            
        def __enter__(self):
            self.conn = sqlite3.connect(DB_NAME, timeout=30)
            self.conn.row_factory = sqlite3.Row
            return self.conn
            
        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.conn:
                if exc_type is None:
                    self.conn.commit()
                else:
                    self.conn.rollback()
                    logger.error(f"DB transaction rolled back: {exc_val}")
                self.conn.close()
            return False
    
    return DBConnection()


async def retry_async(func, *args, max_retries=MAX_RETRIES, delay=RETRY_DELAY, **kwargs):
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exception = e
            logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(delay * (attempt + 1))
    
    raise last_exception


# ============================================================
# === BACKUP ===
# ============================================================

async def backup_database(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Starting database backup...")
    
    try:
        if not os.path.exists(DB_NAME):
            logger.error(f"Database file {DB_NAME} not found!")
            return
        
        backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        
        with db_connection() as conn:
            backup_conn = sqlite3.connect(backup_name)
            conn.backup(backup_conn)
            backup_conn.close()
        
        stats = get_backup_stats()
        
        for admin_id in ADMIN_IDS:
            try:
                with open(backup_name, 'rb') as f:
                    await context.bot.send_document(
                        chat_id=admin_id,
                        document=f,
                        filename=backup_name,
                        caption=(
                            f"📦 **Ежедневный бэкап**\n\n"
                            f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
                            f"👥 Пользователей: {stats['users']}\n"
                            f"💎 Premium: {stats['premium']}\n"
                            f"📊 Размер: {stats['size_kb']:.1f} KB"
                        ),
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.error(f"Failed to send backup to {admin_id}: {e}")
        
        os.remove(backup_name)
        logger.info("Database backup completed")
        
    except Exception as e:
        logger.error(f"Backup failed: {e}")


def get_backup_stats() -> dict:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            users = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1")
            premium = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM workouts")
            workouts = cursor.fetchone()[0]
            cursor.execute("SELECT SUM(total_questions) FROM stats")
            questions = cursor.fetchone()[0] or 0
        
        size_kb = os.path.getsize(DB_NAME) / 1024
        return {'users': users, 'premium': premium, 'workouts': workouts, 'questions': questions, 'size_kb': size_kb}
    except:
        return {'users': 0, 'premium': 0, 'workouts': 0, 'questions': 0, 'size_kb': 0}


async def health_check(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Running health check...")
    issues = []
    
    try:
        with db_connection() as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        issues.append(f"❌ Database: {e}")
    
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            async with session.get("https://api.groq.com/openai/v1/models", headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    issues.append(f"⚠️ Groq API: status {resp.status}")
    except Exception as e:
        issues.append(f"❌ Groq API: {e}")
    
    if issues:
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(admin_id, f"🏥 **Health Check:**\n\n" + "\n".join(issues), parse_mode="Markdown")
            except:
                pass


# ============================================================
# === БАЗА ДАННЫХ ===
# ============================================================

def init_db():
    logger.info("Initializing database...")
    
    try:
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
                    experience TEXT,
                    reminder_time TEXT,
                    reminder_days TEXT,
                    voice_mode INTEGER DEFAULT 0,
                    language TEXT DEFAULT 'ru',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Миграция
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN voice_mode INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'ru'")
            except sqlite3.OperationalError:
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
                    description TEXT,
                    muscles TEXT,
                    gif_url TEXT,
                    video_url TEXT,
                    image_url TEXT
                )
            """)
            
            cursor.execute("SELECT COUNT(*) FROM exercises")
            if cursor.fetchone()[0] == 0:
                _insert_default_exercises(cursor)
            
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_referral ON users(referral_code)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_user ON chat_history(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_workouts_user ON workouts(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_progress_user ON progress(user_id)")
        
        logger.info("Database initialized successfully")
        
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


def _insert_default_exercises(cursor):
    exercises_data = [
        ("Приседания", "присед,приседы,squat", "Базовое упражнение для ног.", "квадрицепсы, ягодицы",
         "https://media.giphy.com/media/1qfKN8Dt0CRdCRxz9q/giphy.gif", "https://www.youtube.com/watch?v=aclHkVaku9U", None),
        ("Отжимания", "отжимание,push-up,pushup", "Базовое упражнение для груди.", "грудь, трицепс",
         "https://media.giphy.com/media/7YCC7NnFgkUEFOfVNy/giphy.gif", "https://www.youtube.com/watch?v=IODxDxX7oi4", None),
        ("Планка", "plank,планки", "Статика для кора.", "пресс, кор",
         "https://media.giphy.com/media/xT8qBvgKeMvMGSJNgA/giphy.gif", "https://www.youtube.com/watch?v=pSHjTRCQxIw", None),
        ("Подтягивания", "подтягивание,pull-up", "Базовое для спины.", "широчайшие, бицепс",
         "https://media.giphy.com/media/3o7TKDnKzLluH40Zzq/giphy.gif", "https://www.youtube.com/watch?v=eGo4IYlbE5g", None),
        ("Выпады", "выпад,lunges", "Упражнение для ног.", "квадрицепсы, ягодицы",
         "https://media.giphy.com/media/l0HlNQ03J5JxX6lva/giphy.gif", "https://www.youtube.com/watch?v=QOVaHwm-Q6U", None),
        ("Бёрпи", "burpee,берпи", "Кардио на всё тело.", "всё тело",
         "https://media.giphy.com/media/23hPPMRgPxbNBlPQe3/giphy.gif", "https://www.youtube.com/watch?v=TU8QYVW0gDU", None),
        ("Становая тяга", "становая,deadlift", "Базовое многосуставное.", "спина, ягодицы",
         "https://media.giphy.com/media/3oEjHGr1Fhz0kyv8Ig/giphy.gif", "https://www.youtube.com/watch?v=op9kVnSso6Q", None),
        ("Жим лёжа", "жим лежа,bench press", "Базовое для груди.", "грудь, трицепс",
         "https://media.giphy.com/media/7T5wldGkk7XgCyuNUV/giphy.gif", "https://www.youtube.com/watch?v=rT7DgCr-3pg", None),
        ("Скручивания", "скручивание,crunches,пресс", "Упражнение для пресса.", "пресс",
         "https://media.giphy.com/media/l3q2VZLzFKvFTbAlo/giphy.gif", "https://www.youtube.com/watch?v=Xyd_fa5zoEU", None),
        ("Подъём на бицепс", "бицепс,bicep curl", "Изоляция бицепса.", "бицепс",
         "https://media.giphy.com/media/xUOwGmsFStnxzIGC2s/giphy.gif", "https://www.youtube.com/watch?v=ykJmrZ5v0Oo", None),
    ]
    
    cursor.executemany("""
        INSERT INTO exercises (name, aliases, description, muscles, gif_url, video_url, image_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, exercises_data)
    logger.info(f"Inserted {len(exercises_data)} exercises")


# ============================================================
# === ПОЛЬЗОВАТЕЛИ ===
# ============================================================

def generate_referral_code(user_id: int) -> str:
    import hashlib
    return hashlib.md5(f"{user_id}{datetime.now()}".encode()).hexdigest()[:8]


def get_or_create_user(user_id: int, username: str = None):
    try:
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
                logger.info(f"New user: {user_id} (@{username})")
    except Exception as e:
        logger.error(f"Error in get_or_create_user: {e}")


def process_referral(new_user_id: int, ref_code: str) -> bool:
    try:
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
            
            cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (referrer_id,))
            current = cursor.fetchone()[0]
            
            if current:
                new_date = (datetime.strptime(current, "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d")
            else:
                new_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
            
            cursor.execute("UPDATE users SET premium_until = ?, is_premium = 1 WHERE user_id = ?", (new_date, referrer_id))
            cursor.execute("UPDATE stats SET referrals_count = referrals_count + 1 WHERE user_id = ?", (referrer_id,))
            
            logger.info(f"Referral: {new_user_id} -> {referrer_id}")
            return True
    except Exception as e:
        logger.error(f"Error in process_referral: {e}")
        return False


def reset_daily_limit(user_id: int):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            today = datetime.now().strftime("%Y-%m-%d")
            cursor.execute("SELECT last_reset FROM users WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            if result and result[0] != today:
                cursor.execute("UPDATE users SET free_questions = 5, last_reset = ? WHERE user_id = ?", (today, user_id))
    except Exception as e:
        logger.error(f"Error in reset_daily_limit: {e}")


def can_ask_question(user_id: int) -> tuple:
    reset_daily_limit(user_id)
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_premium, premium_until, free_questions FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            
            if not row:
                return False, 0
            
            is_prem, premium_until, free_q = row
            
            if is_prem and premium_until:
                if datetime.now().date() <= datetime.strptime(premium_until, "%Y-%m-%d").date():
                    return True, -1
            
            return free_q > 0, free_q
    except Exception as e:
        logger.error(f"Error in can_ask_question: {e}")
        return False, 0


def use_question(user_id: int):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET free_questions = free_questions - 1 WHERE user_id = ? AND free_questions > 0", (user_id,))
            cursor.execute("UPDATE stats SET total_questions = total_questions + 1 WHERE user_id = ?", (user_id,))
    except Exception as e:
        logger.error(f"Error in use_question: {e}")


def is_premium(user_id: int) -> bool:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if not row or not row[0]:
                return False
            return datetime.now().date() <= datetime.strptime(row[0], "%Y-%m-%d").date()
    except:
        return False


def activate_premium(user_id: int, days: int = 30):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
            current = cursor.fetchone()
            
            if current and current[0]:
                base = max(datetime.now(), datetime.strptime(current[0], "%Y-%m-%d"))
            else:
                base = datetime.now()
            
            end = (base + timedelta(days=days)).strftime("%Y-%m-%d")
            cursor.execute("UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?", (end, user_id))
            logger.info(f"Premium activated: {user_id} for {days} days")
    except Exception as e:
        logger.error(f"Error in activate_premium: {e}")


# ============================================================
# === ПРОФИЛЬ ===
# ============================================================

def get_user_profile(user_id: int) -> dict:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT height, weight, age, gender, goal, location, equipment, experience
                FROM users WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            
            if not row:
                return {}
            
            return {
                'height': row[0], 'weight': row[1], 'age': row[2], 'gender': row[3],
                'goal': row[4], 'location': row[5], 'equipment': row[6], 'experience': row[7]
            }
    except:
        return {}


def update_user_profile(user_id: int, **kwargs):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            fields = [f"{k} = ?" for k, v in kwargs.items() if v is not None]
            values = [v for v in kwargs.values() if v is not None]
            
            if fields:
                values.append(user_id)
                cursor.execute(f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?", values)
                logger.info(f"Profile updated: {user_id}")
    except Exception as e:
        logger.error(f"Error in update_user_profile: {e}")


def has_profile(user_id: int) -> bool:
    p = get_user_profile(user_id)
    return bool(p.get('height') and p.get('weight') and p.get('goal'))


# ============================================================
# === НАСТРОЙКИ (Voice Mode / Language) ===
# ============================================================

def get_user_settings(user_id: int) -> dict:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT voice_mode, language FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            
            if row:
                return {'voice_mode': bool(row[0]), 'language': row[1] or 'ru'}
            return {'voice_mode': False, 'language': 'ru'}
    except:
        return {'voice_mode': False, 'language': 'ru'}


def set_voice_mode(user_id: int, enabled: bool):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET voice_mode = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
            logger.info(f"Voice mode {'enabled' if enabled else 'disabled'}: {user_id}")
    except Exception as e:
        logger.error(f"Error in set_voice_mode: {e}")


def set_user_language(user_id: int, language: str):
    if language not in ['ru', 'en', 'ko']:
        language = 'ru'
    
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET language = ? WHERE user_id = ?", (language, user_id))
            logger.info(f"Language set to {language}: {user_id}")
    except Exception as e:
        logger.error(f"Error in set_user_language: {e}")


# ============================================================
# === ГЕНЕРАЦИЯ ГОЛОСА (edge-tts) ===
# ============================================================

async def generate_voice_response(text: str, user_id: int, lang: str = 'ru') -> str | None:
    if not os.path.exists(VOICE_DIR):
        os.makedirs(VOICE_DIR)
    
    voice = VOICE_MAP.get(lang, VOICE_MAP['ru'])
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = os.path.join(VOICE_DIR, f"voice_{user_id}_{timestamp}.ogg")
    
    try:
        if len(text) > 4000:
            text = text[:4000] + "..."
        
        clean_text = clean_text_for_voice(text)
        
        communicate = edge_tts.Communicate(clean_text, voice)
        await communicate.save(output_file)
        
        logger.info(f"Voice generated: {user_id}")
        return output_file
    except Exception as e:
        logger.error(f"Voice generation failed: {e}")
        return None


def clean_text_for_voice(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'\n+', '. ', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.replace('•', '').replace('—', '-').replace('«', '"').replace('»', '"')
    return text.strip()


def cleanup_voice_files(max_age_hours: int = 1):
    if not os.path.exists(VOICE_DIR):
        return
    
    now = datetime.now()
    deleted = 0
    
    for filename in os.listdir(VOICE_DIR):
        filepath = os.path.join(VOICE_DIR, filename)
        if os.path.isfile(filepath):
            age = now - datetime.fromtimestamp(os.path.getmtime(filepath))
            if age.total_seconds() > max_age_hours * 3600:
                try:
                    os.remove(filepath)
                    deleted += 1
                except:
                    pass
    
    if deleted:
        logger.info(f"Cleaned {deleted} voice files")


async def cleanup_voice_job(context: ContextTypes.DEFAULT_TYPE):
    cleanup_voice_files(max_age_hours=1)


# ============================================================
# === ПРОГРЕСС / НАПОМИНАНИЯ / ИСТОРИЯ ===
# ============================================================

def add_weight_record(user_id: int, weight: float):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO progress (user_id, weight) VALUES (?, ?)", (user_id, weight))
            cursor.execute("UPDATE users SET weight = ? WHERE user_id = ?", (weight, user_id))
    except Exception as e:
        logger.error(f"Error in add_weight_record: {e}")


def get_weight_history(user_id: int, limit: int = 10) -> list:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT weight, date FROM progress WHERE user_id = ? ORDER BY date DESC LIMIT ?", (user_id, limit))
            return cursor.fetchall()
    except:
        return []


def set_reminder(user_id: int, time_str: str, days: str):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET reminder_time = ?, reminder_days = ? WHERE user_id = ?", (time_str, days, user_id))
    except Exception as e:
        logger.error(f"Error in set_reminder: {e}")


def get_users_with_reminders() -> list:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, reminder_time, reminder_days FROM users WHERE reminder_time IS NOT NULL AND is_premium = 1")
            return cursor.fetchall()
    except:
        return []


def add_to_history(user_id: int, role: str, content: str):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)", (user_id, role, content[:2000]))
            cursor.execute("""
                DELETE FROM chat_history WHERE user_id = ? AND id NOT IN (
                    SELECT id FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 10
                )
            """, (user_id, user_id))
    except Exception as e:
        logger.error(f"Error in add_to_history: {e}")


def get_chat_context(user_id: int, limit: int = 5) -> list:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))
            return [{"role": r[0], "content": r[1]} for r in reversed(cursor.fetchall())]
    except:
        return []


def clear_history(user_id: int):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
    except Exception as e:
        logger.error(f"Error in clear_history: {e}")


# ============================================================
# === ПОИСК УПРАЖНЕНИЙ ===
# ============================================================

def find_exercise_in_db(query: str) -> dict | None:
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            q = query.lower().strip()
            
            cursor.execute("SELECT name, description, muscles, gif_url, video_url, image_url FROM exercises WHERE LOWER(name) = ?", (q,))
            row = cursor.fetchone()
            
            if not row:
                cursor.execute("SELECT name, description, muscles, gif_url, video_url, image_url FROM exercises WHERE LOWER(aliases) LIKE ?", (f"%{q}%",))
                row = cursor.fetchone()
            
            if not row:
                cursor.execute("SELECT name, description, muscles, gif_url, video_url, image_url FROM exercises WHERE LOWER(name) LIKE ?", (f"%{q}%",))
                row = cursor.fetchone()
            
            if row:
                return {'name': row[0], 'description': row[1], 'muscles': row[2], 'gif_url': row[3], 'video_url': row[4], 'image_url': row[5]}
    except:
        pass
    return None


async def search_exercise_gif(query: str) -> str | None:
    if not GIPHY_API_KEY:
        return None
    
    try:
        async with aiohttp.ClientSession() as session:
            params = {"api_key": GIPHY_API_KEY, "q": f"{query} exercise fitness", "limit": 3, "rating": "g"}
            async with session.get("https://api.giphy.com/v1/gifs/search", params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data["data"]:
                        return data["data"][0]["images"]["downsized_medium"]["url"]
    except Exception as e:
        logger.error(f"Giphy error: {e}")
    return None


def get_youtube_search_url(query: str) -> str:
    return f"https://www.youtube.com/results?search_query={urllib.parse.quote(query + ' техника')}"


def extract_exercise_name(text: str) -> str | None:
    patterns = [
        r"как (?:правильно )?(?:делать|выполнять) (.+?)(?:\?|$|\.)",
        r"техника (?:выполнения )?(.+?)(?:\?|$|\.)",
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


async def get_exercise_with_media(query: str) -> dict:
    exercise = find_exercise_in_db(query)
    if exercise:
        return {'found': True, 'source': 'database', **exercise}
    
    gif_url = await search_exercise_gif(query)
    return {
        'found': bool(gif_url),
        'source': 'search',
        'name': query.title(),
        'gif_url': gif_url,
        'video_url': get_youtube_search_url(query),
        'description': None,
        'muscles': None
    }


# ============================================================
# === ПРОВЕРКА ПОДПИСКИ ===
# ============================================================

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=REQUIRED_CHANNEL, user_id=user_id)
        return member.status in ['creator', 'administrator', 'member']
    except:
        return True


async def show_subscription_required(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")],
        [InlineKeyboardButton("✅ Проверить", callback_data="check_subscription")]
    ]
    
    try:
        msg = "🔒 **Подпишись на канал для доступа!**"
        if update.message:
            await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except:
        pass


# ============================================================
# === GROQ API ===
# ============================================================

async def groq_chat(user_id: int, user_message: str, use_context: bool = True) -> str:
    profile = get_user_profile(user_id)
    
    profile_text = ""
    if profile.get('height'):
        profile_text = f"\nПрофиль: {profile['height']}см, {profile['weight']}кг, цель: {profile['goal']}"
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT + profile_text}]
    
    if use_context and is_premium(user_id):
        messages.extend(get_chat_context(user_id))
    
    messages.append({"role": "user", "content": user_message})
    
    payload = {"model": "llama-3.3-70b-versatile", "messages": messages, "max_tokens": 1000, "temperature": 0.7}
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    
    async def _request():
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_URL, json=payload, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                raise aiohttp.ClientError(f"API error: {resp.status}")
    
    try:
        reply = await retry_async(_request, max_retries=3, delay=2)
        add_to_history(user_id, "user", user_message)
        add_to_history(user_id, "assistant", reply)
        return reply
    except asyncio.TimeoutError:
        return "⚠️ AI думает слишком долго. Попробуй ещё раз."
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "⚠️ Тренер ушёл на перерыв. Попробуй через минуту."


# ============================================================
# === АНАЛИЗ ФОТО ===
# ============================================================

async def upload_to_imgbb(photo_bytes: bytes) -> str | None:
    if not IMGBB_API_KEY:
        return None
    
    try:
        async with aiohttp.ClientSession() as session:
            data = {"key": IMGBB_API_KEY, "image": base64.b64encode(photo_bytes).decode()}
            async with session.post(IMGBB_API_URL, data=data, timeout=30) as resp:
                if resp.status == 200:
                    return (await resp.json())["data"]["url"]
    except Exception as e:
        logger.error(f"ImgBB error: {e}")
    return None


async def analyze_photo(user_id: int, photo_url: str, caption: str = "") -> str:
    profile = get_user_profile(user_id)
    profile_text = f" Цель: {profile['goal']}." if profile.get('goal') else ""
    
    messages = [
        {"role": "system", "content": f"Ты фитнес-тренер. Анализируй технику.{profile_text}"},
        {"role": "user", "content": [
            {"type": "text", "text": caption or "Проанализируй технику."},
            {"type": "image_url", "image_url": {"url": photo_url}}
        ]}
    ]
    
    payload = {"model": "llama-3.2-90b-vision-preview", "messages": messages, "max_tokens": 800, "temperature": 0.7}
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_URL, json=payload, headers=headers, timeout=60) as resp:
                if resp.status == 200:
                    return (await resp.json())["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Vision error: {e}")
    
    return "⚠️ Не удалось проанализировать."


# ============================================================
# === ПАРСИНГ ПРОФИЛЯ ===
# ============================================================

def parse_profile_message(text: str) -> dict:
    result = {}
    text = text.lower()
    
    numbers = re.findall(r'\d+\.?\d*', text)
    
    if len(numbers) >= 1 and 100 <= float(numbers[0]) <= 250:
        result['height'] = int(float(numbers[0]))
    if len(numbers) >= 2 and 30 <= float(numbers[1]) <= 300:
        result['weight'] = float(numbers[1])
    if len(numbers) >= 3 and 10 <= int(float(numbers[2])) <= 100:
        result['age'] = int(float(numbers[2]))
    
    if ' м ' in f' {text} ' or 'муж' in text:
        result['gender'] = 'м'
    elif ' ж ' in f' {text} ' or 'жен' in text:
        result['gender'] = 'ж'
    
    for k, v in {'похуд': 'похудеть', 'набр': 'набрать массу', 'масс': 'набрать массу', 'форм': 'поддержать форму'}.items():
        if k in text:
            result['goal'] = v
            break
    
    if 'дом' in text:
        result['location'] = 'дом'
    elif 'зал' in text:
        result['location'] = 'зал'
    
    equipment = []
    for k, v in {'гантел': 'гантели', 'штанг': 'штанга', 'турник': 'турник', 'нет': 'без инвентаря'}.items():
        if k in text:
            equipment.append(v)
    if equipment:
        result['equipment'] = ', '.join(equipment)
    
    return result


# ============================================================
# === ОТПРАВКА ОТВЕТА (ТЕКСТ/ГОЛОС) ===
# ============================================================

async def send_response(update: Update, text: str, voice_mode: bool, language: str, user_id: int, keyboard: list = None):
    if voice_mode:
        voice_sent = await send_voice_response(update, text, language, user_id)
        if not voice_sent:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)
        elif keyboard:
            await update.message.reply_text("👆", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)


async def send_voice_response(update: Update, text: str, language: str, user_id: int) -> bool:
    voice_file = None
    
    try:
        await update.message.chat.send_action("record_voice")
        voice_file = await generate_voice_response(text, user_id, language)
        
        if not voice_file or not os.path.exists(voice_file):
            return False
        
        with open(voice_file, 'rb') as audio:
            await update.message.reply_voice(voice=audio)
        
        logger.info(f"Voice sent: {user_id}")
        return True
    except Exception as e:
        logger.error(f"Voice send error: {e}")
        return False
    finally:
        if voice_file and os.path.exists(voice_file):
            try:
                os.remove(voice_file)
            except:
                pass


# ============================================================
# === МЕНЮ НАСТРОЕК ===
# ============================================================

async def send_settings_menu(message, settings: dict, edit: bool = False):
    voice_mode = settings.get('voice_mode', False)
    language = settings.get('language', 'ru')
    
    mode_text = "🎙️ Голос" if voice_mode else "📝 Текст"
    mode_emoji = "🔊" if voice_mode else "🔇"
    
    lang_flags = {'ru': '🇷🇺', 'en': '🇺🇸', 'ko': '🇰🇷'}
    lang_names = {'ru': 'Русский', 'en': 'English', 'ko': '한국어'}
    current_lang = f"{lang_flags.get(language, '🇷🇺')} {lang_names.get(language, 'Русский')}"
    
    keyboard = [
        [InlineKeyboardButton(f"{mode_emoji} Режим: {mode_text}", callback_data="toggle_voice_mode")],
        [InlineKeyboardButton(f"🌍 Язык: {current_lang}", callback_data="change_language")],
        [
            InlineKeyboardButton("🇷🇺", callback_data="set_lang_ru"),
            InlineKeyboardButton("🇺🇸", callback_data="set_lang_en"),
            InlineKeyboardButton("🇰🇷", callback_data="set_lang_ko")
        ],
        [InlineKeyboardButton("◀️ Назад", callback_data="start_bot")]
    ]
    
    text = (
        "⚙️ **Настройки**\n\n"
        f"📢 Режим: **{mode_text}**\n"
        f"🌍 Язык: **{current_lang}**\n\n"
        "💡 _Голосовой режим — ответы голосом_"
    )
    
    if edit:
        await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ============================================================
# === КОМАНДЫ ===
# ============================================================

@handle_errors
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)
    
    logger.info(f"User {user.id} started")
    
    if not await check_subscription(user.id, context) and user.id not in ADMIN_IDS:
        await show_subscription_required(update, context)
        return
    
    if context.args:
        if process_referral(user.id, context.args[0]):
            await update.message.reply_text("🎁 Бонус начислен!")
    
    settings = get_user_settings(user.id)
    mode_emoji = "🎙️" if settings['voice_mode'] else "📝"
    
    keyboard = [
        [InlineKeyboardButton("👤 Профиль", callback_data="setup_profile")],
        [InlineKeyboardButton("💪 Тренировка", callback_data="workout"),
         InlineKeyboardButton("🍽️ Рецепт", callback_data="recipe")],
        [InlineKeyboardButton("📊 Прогресс", callback_data="progress"),
         InlineKeyboardButton("🏋️ Упражнения", callback_data="exercises_menu")],
        [InlineKeyboardButton(f"⚙️ Настройки {mode_emoji}", callback_data="settings")],
        [InlineKeyboardButton("🔥 Premium", callback_data="subscribe")]
    ]
    
    await update.message.reply_text(
        f"💪 Привет, {user.first_name}!\n\n"
        f"Я **Murasaki Sport** — AI-тренер!\n\n"
        f"📢 Режим: **{'Голос 🎙️' if settings['voice_mode'] else 'Текст 📝'}**\n\n"
        "Спроси: «Как делать приседания?» 🎬",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


@handle_errors
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = get_user_settings(update.effective_user.id)
    await send_settings_menu(update.message, settings)


@handle_errors
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **Команды:**\n"
        "/start — Меню\n"
        "/settings — Настройки (голос/текст)\n"
        "/profile — Профиль\n"
        "/stats — Статистика\n"
        "/exercises — Упражнения\n"
        "/clear — Очистить историю",
        parse_mode="Markdown"
    )


@handle_errors
async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = get_user_profile(update.effective_user.id)
    
    if not has_profile(update.effective_user.id):
        keyboard = [[InlineKeyboardButton("👤 Создать", callback_data="setup_profile")]]
        await update.message.reply_text("❌ Профиль не заполнен", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    prem = "💎" if is_premium(update.effective_user.id) else "🆓"
    
    await update.message.reply_text(
        f"👤 **Профиль** {prem}\n\n"
        f"📏 Рост: {p.get('height', '—')} см\n"
        f"⚖️ Вес: {p.get('weight', '—')} кг\n"
        f"🎯 Цель: {p.get('goal', '—')}",
        parse_mode="Markdown"
    )


@handle_errors
async def exercises_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, muscles FROM exercises LIMIT 15")
            exercises = cursor.fetchall()
        
        lines = [f"• **{name}** — {muscles}" for name, muscles in exercises]
        await update.message.reply_text("🏋️ **Упражнения:**\n\n" + "\n".join(lines), parse_mode="Markdown")
    except:
        await update.message.reply_text("⚠️ Ошибка")


@handle_errors
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.free_questions, u.is_premium, s.total_questions, s.workouts_completed
                FROM users u LEFT JOIN stats s ON u.user_id = s.user_id WHERE u.user_id = ?
            """, (update.effective_user.id,))
            row = cursor.fetchone()
        
        if row:
            await update.message.reply_text(
                f"📊 **Статистика**\n\n"
                f"Статус: {'💎 Premium' if row[1] else '🆓 Free'}\n"
                f"💬 Вопросов: {row[2] or 0}\n"
                f"💪 Тренировок: {row[3] or 0}",
                parse_mode="Markdown"
            )
    except:
        pass


@handle_errors
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_history(update.effective_user.id)
    await update.message.reply_text("✅ История очищена!")


@handle_errors
async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referral_code FROM users WHERE user_id = ?", (update.effective_user.id,))
            code = cursor.fetchone()[0]
        
        await update.message.reply_text(
            f"👥 **+7 дней Premium за друга!**\n\n`https://t.me/{context.bot.username}?start={code}`",
            parse_mode="Markdown"
        )
    except:
        pass


# ============================================================
# === ОБРАБОТКА СООБЩЕНИЙ ===
# ============================================================

@handle_errors
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)
    
    if not await check_subscription(user.id, context) and user.id not in ADMIN_IDS:
        await show_subscription_required(update, context)
        return
    
    text = update.message.text.strip()
    text_lower = text.lower()
    
    logger.info(f"Message from {user.id}: {text[:50]}...")
    
    # Настройки
    settings = get_user_settings(user.id)
    voice_mode = settings.get('voice_mode', False)
    language = settings.get('language', 'ru')
    
    # === ВЕС ===
    weight_match = re.match(r'^вес\s+(\d+\.?\d*)', text_lower)
    if weight_match:
        w = float(weight_match.group(1))
        if 30 <= w <= 300:
            add_weight_record(user.id, w)
            history = get_weight_history(user.id, 2)
            
            response = f"✅ Записано: **{w} кг**"
            if len(history) >= 2:
                diff = w - history[1][0]
                if diff != 0:
                    response += f"\n{'📈' if diff > 0 else '📉'} {'+' if diff > 0 else ''}{diff:.1f} кг"
            
            await send_response(update, response, voice_mode, language, user.id)
            return
    
    # === ПРОФИЛЬ ===
    if len(re.findall(r'\d+', text)) >= 2 and any(w in text_lower for w in ['похуд', 'набр', 'дом', 'зал', 'форм']):
        data = parse_profile_message(text)
        if data.get('height') and data.get('weight'):
            update_user_profile(user.id, **data)
            await send_response(update, "✅ **Профиль сохранён!**", voice_mode, language, user.id)
            return
    
    # === УПРАЖНЕНИЕ ===
    if any(kw in text_lower for kw in ['как делать', 'как правильно', 'техника', 'покажи', 'научи']):
        ex_name = extract_exercise_name(text)
        
        if ex_name:
            if voice_mode:
                await update.message.chat.send_action("record_voice")
            else:
                await update.message.chat.send_action("typing")
            
            can_ask, _ = can_ask_question(user.id)
            if not can_ask:
                keyboard = [[InlineKeyboardButton("💎 Premium", callback_data="subscribe")]]
                await update.message.reply_text("⚠️ Лимит исчерпан!", reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            ex_data = await get_exercise_with_media(ex_name)
            ai_response = await groq_chat(user.id, f"Объясни технику '{ex_name}'. Кратко.", use_context=False)
            
            if not is_premium(user.id):
                use_question(user.id)
            
            response_text = f"💪 **{ex_data['name']}**\n\n"
            if ex_data.get('muscles'):
                response_text += f"🎯 {ex_data['muscles']}\n\n"
            response_text += ai_response
            
            keyboard = []
            if ex_data.get('video_url'):
                keyboard.append([InlineKeyboardButton("▶️ YouTube", url=ex_data['video_url'])])
            
            # GIF + ответ
            if ex_data.get('gif_url'):
                try:
                    await update.message.reply_animation(
                        animation=ex_data['gif_url'],
                        caption=f"💪 {ex_data['name']}" if voice_mode else response_text[:1024],
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard and not voice_mode else None
                    )
                    
                    if voice_mode:
                        await send_voice_response(update, ai_response, language, user.id)
                        if keyboard:
                            await update.message.reply_text("👆", reply_markup=InlineKeyboardMarkup(keyboard))
                    return
                except:
                    pass
            
            await send_response(update, response_text, voice_mode, language, user.id, keyboard)
            return
    
    # === ОБЫЧНЫЙ ВОПРОС ===
    can_ask, _ = can_ask_question(user.id)
    if not can_ask:
        keyboard = [[InlineKeyboardButton("💎 Premium", callback_data="subscribe")]]
        await update.message.reply_text("⚠️ Лимит исчерпан!", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if voice_mode:
        await update.message.chat.send_action("record_voice")
    else:
        await update.message.chat.send_action("typing")
    
    response = await groq_chat(user.id, text)
    
    if not is_premium(user.id):
        use_question(user.id)
    
    footer = ""
    if not is_premium(user.id):
        _, rem = can_ask_question(user.id)
        if rem <= 2:
            footer = f"\n\n💡 Осталось: {rem}/5"
    
    await send_response(update, response + footer, voice_mode, language, user.id)


# ============================================================
# === ОБРАБОТКА ФОТО ===
# ============================================================

@handle_errors
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username)
    
    if not is_premium(user.id) and user.id not in ADMIN_IDS:
        keyboard = [[InlineKeyboardButton("🔥 Premium", callback_data="subscribe")]]
        await update.message.reply_text("📸 Анализ фото — Premium!", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    settings = get_user_settings(user.id)
    
    if settings['voice_mode']:
        await update.message.chat.send_action("record_voice")
    else:
        await update.message.chat.send_action("typing")
    
    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    
    photo_url = await upload_to_imgbb(bytes(photo_bytes))
    if not photo_url:
        await update.message.reply_text("⚠️ Не удалось загрузить фото")
        return
    
    analysis = await analyze_photo(user.id, photo_url, update.message.caption or "")
    
    await send_response(update, f"📸 **Анализ:**\n\n{analysis}", settings['voice_mode'], settings['language'], user.id)


# ============================================================
# === CALLBACK HANDLERS ===
# ============================================================

@handle_errors
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    # === НАСТРОЙКИ ===
    if query.data == "settings":
        settings = get_user_settings(user_id)
        await send_settings_menu(query.message, settings, edit=True)
        return
    
    if query.data == "toggle_voice_mode":
        settings = get_user_settings(user_id)
        new_mode = not settings['voice_mode']
        set_voice_mode(user_id, new_mode)
        settings['voice_mode'] = new_mode
        await send_settings_menu(query.message, settings, edit=True)
        await query.answer("🎙️ Голос включён!" if new_mode else "📝 Текст включён!", show_alert=True)
        return
    
    if query.data == "change_language":
        await query.answer("Выбери язык ниже 👇")
        return
    
    if query.data.startswith("set_lang_"):
        lang = query.data.replace("set_lang_", "")
        set_user_language(user_id, lang)
        settings = get_user_settings(user_id)
        await send_settings_menu(query.message, settings, edit=True)
        await query.answer({'ru': '🇷🇺 Русский', 'en': '🇺🇸 English', 'ko': '🇰🇷 한국어'}.get(lang, lang), show_alert=True)
        return
    
    # === ПОДПИСКА ===
    if query.data == "check_subscription":
        if await check_subscription(user_id, context):
            keyboard = [[InlineKeyboardButton("💪 Начать", callback_data="start_bot")]]
            await query.message.edit_text("✅ Подписка подтверждена!", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await query.answer("❌ Подписка не найдена!", show_alert=True)
        return
    
    if query.data == "start_bot":
        settings = get_user_settings(user_id)
        mode_emoji = "🎙️" if settings['voice_mode'] else "📝"
        
        keyboard = [
            [InlineKeyboardButton("👤 Профиль", callback_data="setup_profile")],
            [InlineKeyboardButton("💪 Тренировка", callback_data="workout"),
             InlineKeyboardButton("🍽️ Рецепт", callback_data="recipe")],
            [InlineKeyboardButton(f"⚙️ Настройки {mode_emoji}", callback_data="settings")],
            [InlineKeyboardButton("🔥 Premium", callback_data="subscribe")]
        ]
        await query.message.edit_text("💪 **Готов!**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    # === ПРОФИЛЬ ===
    if query.data == "setup_profile":
        await query.message.reply_text("👤 Напиши:\n\n`175 80 25 м похудеть зал гантели`", parse_mode="Markdown")
        return
    
    # === УПРАЖНЕНИЯ ===
    if query.data == "exercises_menu":
        keyboard = [
            [InlineKeyboardButton("🦵 Ноги", callback_data="ex_legs"),
             InlineKeyboardButton("💪 Руки", callback_data="ex_arms")],
            [InlineKeyboardButton("🔙 Спина", callback_data="ex_back"),
             InlineKeyboardButton("🫁 Грудь", callback_data="ex_chest")]
        ]
        await query.message.reply_text("🏋️ **Группа:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    # === ТРЕНИРОВКА ===
    if query.data == "workout":
        if not has_profile(user_id):
            keyboard = [[InlineKeyboardButton("👤 Создать", callback_data="setup_profile")]]
            await query.message.reply_text("❌ Сначала профиль!", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        
        keyboard = [
            [InlineKeyboardButton("💪 Силовая", callback_data="workout_strength"),
             InlineKeyboardButton("🔥 Кардио", callback_data="workout_cardio")]
        ]
        await query.message.reply_text("💪 **Тип:**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    if query.data.startswith("workout_"):
        wtype = query.data.replace("workout_", "")
        profile = get_user_profile(user_id)
        
        await query.message.edit_text("💪 Составляю...")
        
        types = {'strength': 'силовую', 'cardio': 'кардио'}
        response = await groq_chat(user_id, f"Составь {types.get(wtype, '')} тренировку. Место: {profile.get('location', 'дом')}.", use_context=False)
        
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO workouts (user_id, workout_text) VALUES (?, ?)", (user_id, response))
                wid = cursor.lastrowid
            
            keyboard = [[InlineKeyboardButton("✅ Выполнено!", callback_data=f"complete_{wid}")]]
            await query.message.edit_text(f"💪 **Тренировка:**\n\n{response}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except:
            await query.message.edit_text(f"💪\n\n{response}", parse_mode="Markdown")
        return
    
    if query.data.startswith("complete_"):
        wid = int(query.data.replace("complete_", ""))
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE workouts SET completed = 1 WHERE id = ?", (wid,))
                cursor.execute("UPDATE stats SET workouts_completed = workouts_completed + 1 WHERE user_id = ?", (user_id,))
        except:
            pass
        await query.answer("🔥 Отлично!", show_alert=True)
        await query.message.reply_text("✅ Тренировка выполнена! 💪")
        return
    
    # === РЕЦЕПТ ===
    if query.data == "recipe":
        keyboard = [
            [InlineKeyboardButton("🍳 Завтрак", callback_data="recipe_breakfast"),
             InlineKeyboardButton("🥗 Обед", callback_data="recipe_lunch")]
        ]
        await query.message.reply_text("🍽️ **Что?**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    if query.data.startswith("recipe_"):
        rtype = query.data.replace("recipe_", "")
        types = {'breakfast': 'завтрак', 'lunch': 'обед'}
        
        await query.message.edit_text("🍽️ Готовлю...")
        response = await groq_chat(user_id, f"Рецепт: {types.get(rtype, 'блюдо')}. КБЖУ.", use_context=False)
        
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE stats SET recipes_generated = recipes_generated + 1 WHERE user_id = ?", (user_id,))
        except:
            pass
        
        keyboard = [[InlineKeyboardButton("🔄 Другой", callback_data="recipe")]]
        await query.message.edit_text(f"🍽️\n\n{response}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return
    
    # === ПРОГРЕСС ===
    if query.data == "progress":
        records = get_weight_history(user_id, 10)
        if not records:
            await query.message.reply_text("📊 Нет записей.\n\n`Вес 75.5`", parse_mode="Markdown")
            return
        
        lines = [f"• {datetime.fromisoformat(d).strftime('%d.%m')}: **{w}** кг" for w, d in records]
        await query.message.reply_text("📊 **Прогресс:**\n\n" + "\n".join(lines), parse_mode="Markdown")
        return
    
    # === ПОДПИСКА ===
    if query.data == "subscribe":
        keyboard = [
            [InlineKeyboardButton("💳 99₽", callback_data="pay_premium")],
            [InlineKeyboardButton("👥 Бесплатно", callback_data="referral_info")]
        ]
        await query.message.reply_text(
            "💎 **Premium**\n\n✅ Безлимит\n✅ Голос/Текст\n✅ Анализ фото\n✅ Напоминания",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return
    
    if query.data == "pay_premium":
        if not PROVIDER_TOKEN:
            await query.message.reply_text("⚠️ Платежи недоступны. /referral")
            return
        
        await context.bot.send_invoice(
            chat_id=user_id,
            title="Premium 30 дней",
            description="Безлимит + голос + фото",
            payload="premium_30days",
            provider_token=PROVIDER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice("Premium", 99 * 100)]
        )
        return
    
    if query.data == "referral_info":
        try:
            with db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT referral_code FROM users WHERE user_id = ?", (user_id,))
                code = cursor.fetchone()[0]
            await query.message.reply_text(f"👥 **+7 дней за друга!**\n\n`https://t.me/{context.bot.username}?start={code}`", parse_mode="Markdown")
        except:
            pass
        return


# ============================================================
# === ПЛАТЕЖИ ===
# ============================================================

@handle_errors
async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


@handle_errors
async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    activate_premium(update.effective_user.id)
    await update.message.reply_text("🎉 **Premium активирован!**", parse_mode="Markdown")


# ============================================================
# === НАПОМИНАНИЯ ===
# ============================================================

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    current_day = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][now.weekday()]
    
    for user_id, reminder_time, reminder_days in get_users_with_reminders():
        if reminder_time == current_time and current_day in reminder_days:
            try:
                await context.bot.send_message(user_id, "⏰ **Время тренировки!** 💪", parse_mode="Markdown")
            except:
                pass


# ============================================================
# === АДМИН ===
# ============================================================

@handle_errors
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    stats = get_backup_stats()
    await update.message.reply_text(
        f"🔧 **Админ**\n\n"
        f"👥 Юзеров: {stats['users']}\n"
        f"💎 Premium: {stats['premium']}\n"
        f"📊 БД: {stats['size_kb']:.1f} KB\n\n"
        f"/give_premium ID 30\n/backup\n/logs\n/broadcast текст",
        parse_mode="Markdown"
    )


@handle_errors
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
    except:
        await update.message.reply_text("❌ Ошибка")


@handle_errors
async def backup_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("📦 Бэкап...")
    await backup_database(context)


@handle_errors
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    error_log = os.path.join(LOG_DIR, 'errors.log')
    if os.path.exists(error_log):
        with open(error_log, 'r', encoding='utf-8') as f:
            lines = f.readlines()[-30:]
        await update.message.reply_text(f"📝 **Ошибки:**\n```\n{''.join(lines)[-3500:]}\n```", parse_mode="Markdown")
    else:
        await update.message.reply_text("📝 Ошибок нет!")


@handle_errors
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    
    if not context.args:
        await update.message.reply_text("`/broadcast текст`", parse_mode="Markdown")
        return
    
    msg = " ".join(context.args)
    
    try:
        with db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            users = cursor.fetchall()
        
        success = 0
        for (uid,) in users:
            try:
                await context.bot.send_message(uid, msg, parse_mode="Markdown")
                success += 1
                await asyncio.sleep(0.05)
            except:
                pass
        
        await update.message.reply_text(f"✅ {success}/{len(users)}")
    except:
        await update.message.reply_text("❌ Ошибка")


# ============================================================
# === ERROR HANDLER ===
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}\n{traceback.format_exc()}")
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, f"🚨 **Error:**\n```\n{context.error}\n```"[:4000], parse_mode="Markdown")
        except:
            pass


# ============================================================
# === MAIN ===
# ============================================================

def main():
    logger.info("=" * 50)
    logger.info("Starting Murasaki Sport Bot...")
    logger.info("=" * 50)
    
    try:
        init_db()
    except Exception as e:
        logger.critical(f"DB init failed: {e}")
        sys.exit(1)
    
    for d in [VOICE_DIR, LOG_DIR]:
        if not os.path.exists(d):
            os.makedirs(d)
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("exercises", exercises_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("referral", referral_command))
    
    # Админ
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("give_premium", give_premium_command))
    app.add_handler(CommandHandler("backup", backup_now_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Сообщения
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    # Платежи
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    
    # Error handler
    app.add_error_handler(error_handler)
    
    # Jobs
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(check_reminders, interval=60, first=10)
        job_queue.run_daily(backup_database, time=dtime(hour=3, minute=0))
        job_queue.run_repeating(health_check, interval=3600, first=300)
        job_queue.run_repeating(cleanup_voice_job, interval=1800, first=60)
    
    logger.info("=" * 50)
    logger.info("✅ Bot started!")
    logger.info(f"🎙️ Voice: edge-tts (RU/EN/KO)")
    logger.info(f"📁 Logs: {LOG_DIR}")
    logger.info(f"🔊 Voice temp: {VOICE_DIR}")
    logger.info("=" * 50)
    
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
