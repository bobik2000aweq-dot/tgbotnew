import asyncio
import os
import logging
import re
import json
import uuid
from datetime import datetime, timedelta
from aiohttp import web
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import FSInputFile
from aiogram.client.session.aiohttp import AiohttpSession
import asyncpg
import sys
from openai import AsyncOpenAI

_ai_client = None

def _get_ai_client():
    global _ai_client
    if _ai_client is None:
        _ai_client = AsyncOpenAI(
            api_key=os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY") or "dummy",
            base_url=os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL") or None,
        )
    return _ai_client


logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")
HUNTME_API_KEY = os.environ.get("HUNTME_API_KEY")
HUNTME_API_BASE_URL = os.environ.get("HUNTME_API_BASE_URL", "https://apihmscout.com/api/employee-api-key").rstrip("/")
HUNTME_OPERATOR_OFFICE_ID = os.environ.get("HUNTME_OPERATOR_OFFICE_ID")
HUNTME_TIMEOUT_SECONDS = int(os.environ.get("HUNTME_TIMEOUT_SECONDS", "20"))
ADMIN_IDS = [8123065501, 8288307098, 7387962932]
DATABASE_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")


def is_admin(user) -> bool:
    return user.id in ADMIN_IDS

COOLDOWN_HOURS = 24
MAX_TEXT_LENGTH = 3800

_replit_host = os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
_railway_host = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
WEBHOOK_HOST = _replit_host or _railway_host or None
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

_bot_session = AiohttpSession()
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=_bot_session)
dp = Dispatcher()

db_pool = None

user_application_type = {}
admin_chat_state = {}
operator_in_chat = {}
user_iq_state = {}
user_age_state = {}
admin_interview_input = {}
pending_operator_apps = {}
user_edit_state = {}
pending_admin_confirm = {}
pending_chat_requests = {}
interview_slots_empty_notified = None

SQUADS = {
    "k": ("kolla", "https://t.me/kollasquad"),
}

IQ_QUESTIONS = [
    {
        "q": "❓ <b>Вопрос 1/1</b>\n\nСколько дней в неделе?",
        "options": [("A) 5", "iq_A"), ("B) 6", "iq_B"), ("C) 7", "iq_C"), ("D) 8", "iq_D")],
        "answer": "C"
    },
]

TEXT_TASK_QUESTION = (
    "✍️ <b>Тестовое задание</b>\n\n"
    "Напиши своими словами (2-5 предложений):\n\n"
    "<b>Почему ты хочешь вступить в нашу команду и что ты умеешь?</b>\n\n"
    "⚠️ Пиши от себя — ответы написанные ИИ (ChatGPT и т.п.) автоматически отклоняются."
)


OPERATOR_FIELDS = [
    ("имя", "Имя"),
    ("возраст", "Возраст (и дата рождения)"),
    ("знание английского", "Знание английского языка"),
    ("модель процессора", "Модель процессора"),
    ("модель видеокарты", "Модель видеокарты"),
    ("скорость интернета", "Скорость Интернета"),
    ("где работал", "Где работал/ла"),
    ("номер телефона", "Номер телефона (с кодом страны)"),
    ("телеграмм", "Телеграмм"),
    ("кто привёл", "Кто привёл / откуда узнали о нас"),
]


def validate_operator_form(text: str) -> list[str]:
    lines_lower = text.lower().splitlines()
    missing = []
    for keyword, label in OPERATOR_FIELDS:
        found = False
        for line in lines_lower:
            if keyword in line:
                parts = line.split(":", 1)
                if len(parts) == 2 and parts[1].strip():
                    found = True
                    break
        if not found:
            missing.append(label)

    if not re.search(r"(?<!\d)\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}(?!\d)", text):
        missing.append("Дата рождения в формате ДД.ММ.ГГГГ")

    return missing


def extract_age_from_application(text: str):
    import re
    today = datetime.now()
    for line in text.splitlines():
        if "возраст" in line.lower():
            # 1. Явный возраст: число 5-80, не часть даты (не окружено цифрами)
            explicit = re.search(r'(?<!\d)(\d{1,2})(?!\d)(?!\s*[.\-/]\s*\d)', line)
            if explicit:
                age = int(explicit.group(1))
                if 5 <= age <= 80:
                    return age
            # 2. Полная дата dd.mm.yyyy или dd/mm/yyyy
            full_date = re.search(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})', line)
            if full_date:
                try:
                    d, m, y = int(full_date.group(1)), int(full_date.group(2)), int(full_date.group(3))
                    bday = datetime(y, m, d)
                    age = (today - bday).days // 365
                    return age
                except Exception:
                    pass
            # 3. Только год рождения
            year_match = re.search(r'\b(19\d{2}|20\d{2})\b', line)
            if year_match:
                birth_year = int(year_match.group())
                return today.year - birth_year
    return None


async def check_ai_generated(text: str) -> tuple[bool, str]:
    try:
        response = await _get_ai_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты эксперт по определению текстов, написанных ИИ (ChatGPT, Claude и т.п.). "
                        "Анализируй текст соискателя и определи: написан ли он ИИ или живым человеком. "
                        "Признаки ИИ-текста: идеальная структура, шаблонные фразы, отсутствие личного опыта, "
                        "слишком формальный язык, общие слова без конкретики. "
                        "Отвечай строго в JSON формате: {\"is_ai\": true/false, \"reason\": \"краткое объяснение на русском\"}"
                    )
                },
                {
                    "role": "user",
                    "content": f"Текст соискателя:\n\n{text}"
                }
            ],
            response_format={"type": "json_object"},
            max_tokens=200,
        )
        result = json.loads(response.choices[0].message.content)
        return result.get("is_ai", False), result.get("reason", "")
    except Exception as e:
        logger.error(f"Ошибка AI проверки: {e}")
        return False, ""


def iq_keyboard(question_index: int):
    opts = IQ_QUESTIONS[question_index]["options"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=opts[0][0], callback_data=opts[0][1]),
         InlineKeyboardButton(text=opts[1][0], callback_data=opts[1][1])],
        [InlineKeyboardButton(text=opts[2][0], callback_data=opts[2][1]),
         InlineKeyboardButton(text=opts[3][0], callback_data=opts[3][1])],
    ])


async def get_db():
    return db_pool


async def db_save_application(app_type: str, user_id: int, full_name: str, username: str, app_text: str, iq_score: int = None) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO applications (app_type, user_id, full_name, username, app_text, iq_score) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            app_type, user_id, full_name, username, app_text, iq_score
        )
        return row["id"]


async def db_get_applications(app_type: str):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, full_name, username, user_id, app_text, submitted_at, iq_score, replied_by, replied_at FROM applications WHERE app_type=$1 AND replied_by IS NULL ORDER BY submitted_at DESC",
            app_type
        )
    return rows


async def db_mark_replied(app_id: int, admin_name: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE applications SET replied_by=$1, replied_at=NOW() WHERE id=$2",
            admin_name, app_id
        )


async def db_get_application_by_id(app_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, full_name, username, user_id, app_text, submitted_at, app_type FROM applications WHERE id=$1",
            app_id
        )


async def db_delete_applications_by_user(target_user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM applications WHERE user_id=$1", target_user_id)


async def db_delete_application_by_id(app_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE interview_slots SET reminder_sent=TRUE WHERE booked_app_id=$1",
            app_id
        )
        await conn.execute("DELETE FROM applications WHERE id=$1", app_id)


async def db_update_application_text(user_id: int, app_type: str, new_text: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE applications SET app_text=$1 WHERE user_id=$2 AND app_type=$3",
            new_text, user_id, app_type
        )


async def db_get_application_by_user(user_id: int, app_type: str):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM applications WHERE user_id=$1 AND app_type=$2 ORDER BY submitted_at DESC LIMIT 1",
            user_id, app_type
        )


async def db_set_admin_confirmed(app_id: int, confirmed: bool):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE applications SET admin_confirmed=$1 WHERE id=$2",
            confirmed, app_id
        )


async def db_get_unconfirmed_operator_apps() -> list:
    async with db_pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM applications
            WHERE app_type='operator'
              AND admin_confirmed IS NULL
              AND submitted_at < NOW() - INTERVAL '24 hours'
        """)


async def db_set_cooldown(user_id: int, app_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO cooldowns (user_id, app_type, last_application) VALUES ($1, $2, NOW()) ON CONFLICT (user_id, app_type) DO UPDATE SET last_application=NOW()",
            user_id, app_type
        )


async def db_get_cooldown(user_id: int, app_type: str):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT last_application FROM cooldowns WHERE user_id=$1 AND app_type=$2", user_id, app_type)
    return row["last_application"] if row else None


async def db_is_greeted(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM greeted_users WHERE user_id=$1", user_id)
    return row is not None


async def db_mark_greeted(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO greeted_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
            user_id
        )


async def db_add_interview_slots(slots: list):
    async with db_pool.acquire() as conn:
        normalized_slots = [
            (slot[0], slot[1], slot[2] if len(slot) > 2 else None, "manual")
            for slot in slots
        ]
        await conn.executemany(
            "INSERT INTO interview_slots (slot_text, slot_dt, office_id, source) VALUES ($1, $2, $3, $4)",
            normalized_slots
        )


async def db_get_free_slots():
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT DISTINCT ON (slot_dt) id, slot_text, slot_dt
            FROM interview_slots s
            WHERE s.is_booked = FALSE
              AND s.slot_dt IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM interview_slots booked
                  WHERE booked.slot_dt = s.slot_dt AND booked.is_booked = TRUE
              )
            ORDER BY slot_dt ASC, id ASC
            """
        )


async def db_get_free_slots_for_date(date_str: str):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT DISTINCT ON (slot_dt) id, slot_text, slot_dt
            FROM interview_slots s
            WHERE s.is_booked = FALSE
              AND s.slot_dt IS NOT NULL
              AND TO_CHAR(s.slot_dt, 'DD.MM') = $1
              AND NOT EXISTS (
                  SELECT 1 FROM interview_slots booked
                  WHERE booked.slot_dt = s.slot_dt AND booked.is_booked = TRUE
              )
            ORDER BY slot_dt ASC, id ASC
            """,
            date_str,
        )


async def db_get_booked_slot_for_app(app_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT slot_text FROM interview_slots WHERE booked_app_id=$1 AND is_booked=TRUE",
            app_id
        )
    return row["slot_text"] if row else None


async def db_book_slot(slot_id: int, user_id: int, app_id: int) -> bool:
    """Бронирует время атомарно, не позволяя занять его дважды."""
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            slot_dt = await conn.fetchval(
                "SELECT slot_dt FROM interview_slots WHERE id=$1 FOR UPDATE",
                slot_id,
            )
            if not slot_dt:
                return False
            # Блокируем все строки того же времени: это защищает от дублей по офисам
            # и от двух одновременных нажатий на одну дату.
            await conn.fetch(
                "SELECT id FROM interview_slots WHERE slot_dt=$1 FOR UPDATE",
                slot_dt,
            )
            already_booked = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM interview_slots WHERE slot_dt=$1 AND is_booked=TRUE)",
                slot_dt,
            )
            if already_booked:
                return False
            result = await conn.execute(
                "UPDATE interview_slots SET is_booked=TRUE, booked_by_user_id=$2, booked_app_id=$3 WHERE id=$1 AND is_booked=FALSE",
                slot_id, user_id, app_id,
            )
            return result == "UPDATE 1"


async def db_release_slot(slot_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE interview_slots SET is_booked=FALSE, booked_by_user_id=NULL, booked_app_id=NULL, reminder_sent=FALSE WHERE id=$1",
            slot_id,
        )


async def db_clear_slots():
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interview_slots")


async def db_get_slot_text(slot_id: int) -> str | None:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT slot_text FROM interview_slots WHERE id=$1", slot_id)
        return row["slot_text"] if row else None


async def db_get_slot_by_id(slot_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, slot_text, slot_dt, office_id, crm_status FROM interview_slots WHERE id=$1",
            slot_id,
        )


async def db_mark_crm_result(slot_id: int, success: bool, status: int, payload):
    """Persist CRM delivery state so failures are retried instead of lost."""
    try:
        details = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        details = str(payload)
    details = details[:4000]
    if success:
        await db_pool.execute(
            """
            UPDATE interview_slots
            SET crm_status='sent',
                crm_last_error=NULL,
                crm_attempts=crm_attempts + 1,
                crm_next_attempt_at=NULL,
                crm_sent_at=NOW()
            WHERE id=$1
            """,
            slot_id,
        )
    else:
        await db_pool.execute(
            """
            UPDATE interview_slots
            SET crm_status='failed',
                crm_last_error=$2,
                crm_attempts=crm_attempts + 1,
                crm_next_attempt_at=NOW() + INTERVAL '5 minutes'
            WHERE id=$1
            """,
            slot_id,
            f"HTTP {status}; {details}",
        )


async def db_get_crm_retry_slots():
    return await db_pool.fetch(
        """
        SELECT
            s.id AS slot_id,
            s.slot_dt,
            s.office_id,
            a.id AS app_id,
            a.full_name,
            a.app_text,
            s.crm_attempts
        FROM interview_slots s
        JOIN applications a ON a.id = s.booked_app_id
        WHERE s.is_booked=TRUE
          AND COALESCE(s.crm_status, 'pending') <> 'sent'
          AND (s.crm_next_attempt_at IS NULL OR s.crm_next_attempt_at <= NOW())
        ORDER BY s.id
        LIMIT 20
        """
    )


async def db_get_slot_by_app_id(app_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM interview_slots WHERE booked_app_id=$1 AND is_booked=TRUE ORDER BY id DESC LIMIT 1",
            app_id
        )


async def db_get_slots_for_reminder() -> list:
    async with db_pool.acquire() as conn:
        return await conn.fetch("""
            SELECT s.id, s.slot_text, s.booked_by_user_id,
                   a.full_name, a.username
            FROM interview_slots s
            LEFT JOIN applications a ON a.id = s.booked_app_id
            WHERE s.is_booked = TRUE
              AND s.reminder_sent = FALSE
              AND s.slot_dt IS NOT NULL
              AND (s.slot_dt AT TIME ZONE 'Europe/Moscow')
                  BETWEEN NOW() + INTERVAL '1 hour 50 minutes'
                      AND NOW() + INTERVAL '2 hours 10 minutes'
        """)


async def db_mark_reminder_sent(slot_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE interview_slots SET reminder_sent=TRUE WHERE id=$1", slot_id
        )


async def db_delete_slot(slot_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM interview_slots WHERE id=$1", slot_id)


async def db_delete_slots_for_date(date_str: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM interview_slots WHERE slot_text LIKE $1",
            f"{date_str}%"
        )


async def db_get_booked_slots():
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT slot_text, booked_by_user_id, booked_app_id FROM interview_slots WHERE is_booked=TRUE ORDER BY slot_dt ASC"
        )


async def db_get_all_slots_for_date(date_str: str):
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT id, slot_text, is_booked, booked_by_user_id, booked_app_id FROM interview_slots WHERE slot_text LIKE $1 ORDER BY slot_dt ASC",
            f"{date_str}%"
        )


def extract_scout_username(app_text: str) -> str | None:
    import re
    SKIP = {"-", "—", "нет", "никто", ".", "не знаю", "сам", "никого", "nobody", "no", "none"}
    lines = app_text.splitlines()
    for i, line in enumerate(lines):
        low = line.lower()
        if "привёл" in low or "привел" in low or "привела" in low or "откуда" in low:
            # Collect value: everything after the first colon on this line,
            # plus the next line if current value is empty (URL on next line)
            if ":" in line:
                after = line.split(":", 1)[1].strip()
                # https:// split workaround — reconstruct
                if after.startswith("//"):
                    after = "https:" + after
                value = after
            else:
                value = ""
            # If nothing after colon, check next line
            if not value and i + 1 < len(lines):
                value = lines[i + 1].strip()

            if not value or value.lower() in SKIP:
                return "@werolk"

            # 1. Invite link t.me/+...
            m = re.search(r'(t\.me/\+[\w-]+)', value, re.IGNORECASE)
            if m:
                return m.group(1)

            # 2. Profile link t.me/username
            m = re.search(r't\.me/@?([\w]{3,32})', value, re.IGNORECASE)
            if m:
                return "@" + m.group(1).lower()

            # 3. @username anywhere in value
            m = re.search(r'@([\w]{3,32})', value)
            if m:
                return "@" + m.group(1).lower()

            # 4. Bare word that looks like a username (no spaces, 3–32 chars)
            clean = value.strip("@ \t")
            if clean and " " not in clean and 2 < len(clean) <= 32:
                return "@" + clean.lower()

            # 5. Return whatever they wrote (up to 40 chars) as the key
            return value[:40].strip()
    return "@werolk"


def extract_form_field(app_text: str, *keywords) -> str | None:
    for line in app_text.splitlines():
        low = line.lower()
        if any(kw in low for kw in keywords):
            if ":" in line:
                value = line.split(":", 1)[1].strip()
                if value and value not in ("-", "—", "нет", "."):
                    return value
    return None


def _normalize_phone(value: str) -> str:
    phone = re.sub(r"[^\d+]", "", value or "")
    if phone.startswith("8") and len(phone) == 11:
        phone = "+7" + phone[1:]
    return phone


def _normalize_telegram(value: str) -> str | None:
    telegram = (value or "").strip()
    telegram = re.sub(r"^https?://t\.me/", "", telegram, flags=re.IGNORECASE)
    telegram = telegram.lstrip("@").split("/", 1)[0]
    if re.fullmatch(r"[a-zA-Z0-9_]{5,}", telegram):
        return telegram
    return None


def _huntme_birth_date(app_text: str) -> str | None:
    match = re.search(r"(?<!\d)(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4})(?!\d)", app_text)
    if not match:
        return None
    raw = match.group(1).replace("/", ".").replace("-", ".")
    try:
        return datetime.strptime(raw, "%d.%m.%Y").strftime("%d.%m.%Y")
    except ValueError:
        return None


async def _huntme_json_request(method: str, path: str, *, json_body=None, headers=None):
    if not HUNTME_API_KEY:
        return 0, {"message": "HUNTME_API_KEY не задан"}
    request_headers = {"Authorization": f"Bearer {HUNTME_API_KEY}", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    timeout = aiohttp.ClientTimeout(total=HUNTME_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, f"{HUNTME_API_BASE_URL}/{path.lstrip('/')}", json=json_body, headers=request_headers) as response:
            raw = await response.text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            return response.status, payload


async def _huntme_create_operator_request(app, slot_id: int, slot) -> tuple[int, object]:
    office_id_value = slot["office_id"] if slot and "office_id" in slot.keys() else None
    office_id_value = office_id_value or HUNTME_OPERATOR_OFFICE_ID
    if not office_id_value:
        return 0, {"message": "не задан HUNTME_OPERATOR_OFFICE_ID"}
    try:
        office_id = int(office_id_value)
    except (TypeError, ValueError):
        return 0, {"message": "HUNTME_OPERATOR_OFFICE_ID должен быть числом"}
    app_text = app["app_text"] or ""
    name = extract_form_field(app_text, "имя") or app["full_name"] or ""
    birth_date = _huntme_birth_date(app_text)
    phone_value = extract_form_field(app_text, "телефон", "номер телефона", "номер")
    number = _normalize_phone(phone_value or "")
    telegram = _normalize_telegram(extract_form_field(app_text, "телеграм", "telegram") or "")
    slot_dt = slot["slot_dt"] if slot else None
    missing = []
    if len(name.strip()) < 3: missing.append("имя")
    if not birth_date: missing.append("дата рождения в формате ДД.ММ.ГГГГ")
    if not number or not (10 <= len(re.sub(r"\D", "", number)) <= 15): missing.append("номер телефона")
    if missing: return 422, {"message": "В анкете не хватает: " + ", ".join(missing)}
    if not slot_dt: return 422, {"message": "у локального слота нет даты"}
    payload = {
        "category": 0,
        "office_id": office_id,
        "interview_appointment_date": f"{slot_dt:%d.%m.%Y %H:%M}",
        "name": name.strip()[:255],
        "birth_date": birth_date,
        "number": number,
    }
    if telegram: payload["telegram"] = telegram
    headers = {"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"hlustyak-operator:{app['id']}:{slot_id}"))}
    return await _huntme_json_request("POST", "/request-call/operator", json_body=payload, headers=headers)


async def _submit_operator_request_to_crm(app, slot_id: int, slot):
    try:
        status, payload = await _huntme_create_operator_request(app, slot_id, slot)
        success = status in (200, 201, 202)
        await db_mark_crm_result(slot_id, success, status, payload)
        if success:
            logger.info(f"Заявка #{app['id']} отправлена в CRM после бронирования слота #{slot_id}: HTTP {status}; ответ={payload}")
        else:
            logger.error(
                f"Не удалось отправить заявку #{app['id']} в CRM: HTTP {status}; "
                f"попытка сохранена для повтора; ответ={payload}"
            )
        return success
    except Exception as exc:
        logger.exception(f"Ошибка отправки заявки #{app['id']} в CRM: {exc}")
        try:
            await db_mark_crm_result(slot_id, False, 0, {"message": str(exc)})
        except Exception:
            logger.exception(f"Не удалось сохранить ошибку CRM для слота #{slot_id}")
        return False


async def crm_retry_loop():
    """Retry every failed CRM delivery, using the same idempotency key."""
    while True:
        try:
            for row in await db_get_crm_retry_slots():
                app = {
                    "id": row["app_id"],
                    "full_name": row["full_name"],
                    "app_text": row["app_text"],
                }
                slot = {
                    "id": row["slot_id"],
                    "slot_dt": row["slot_dt"],
                    "office_id": row["office_id"],
                }
                await _submit_operator_request_to_crm(app, row["slot_id"], slot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"Ошибка цикла повторной отправки CRM: {exc}")
        await asyncio.sleep(60)


async def db_get_scout_referrals() -> dict:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, full_name, username, app_text FROM applications WHERE app_type='operator' ORDER BY submitted_at DESC"
        )
    result = {}
    for row in rows:
        text = row["app_text"] or ""
        scout = extract_scout_username(text)
        if not scout:
            continue
        if scout not in result:
            result[scout] = []
        result[scout].append({
            "full_name": row["full_name"],
            "username": row["username"],
            "app_id": row["id"],
            "form_name": extract_form_field(text, "имя"),
            "form_dob": extract_form_field(text, "возраст", "дата рождения", "дата рожд"),
        })
    return result


async def db_get_slot_dates_summary():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT TO_CHAR(slot_dt, 'DD.MM') AS date_part, COUNT(*) FILTER (WHERE is_booked = FALSE) AS free, COUNT(*) FILTER (WHERE is_booked = TRUE) AS booked, MIN(slot_dt) AS first_slot
            FROM interview_slots WHERE slot_dt IS NOT NULL
            GROUP BY TO_CHAR(slot_dt, 'DD.MM') ORDER BY first_slot ASC
            """
        )
    return {row["date_part"]: {"free": row["free"], "booked": row["booked"]} for row in rows}


async def db_get_free_slot_dates_summary():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT TO_CHAR(slot_dt, 'DD.MM') AS date_part, COUNT(*) AS total
            FROM interview_slots WHERE is_booked = FALSE AND slot_dt IS NOT NULL
            GROUP BY TO_CHAR(slot_dt, 'DD.MM') ORDER BY MIN(slot_dt) ASC
            """
        )
    return {row["date_part"]: row["total"] for row in rows}


async def send_welcome(message: types.Message):
    name = message.from_user.first_name or "Пользователь"
    caption = (
        f"🖤 <b>{name}</b>, добро пожаловать в <b>hlustyak Agency</b>.\n\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Доступные вакансии:</b>\n\n"
        f"🖥 <b>Оператор</b> — модерирование чата у стримерш на зарубежных платформах и работа с OBS.\n"
        f"<i>Всему необходимому обучим.</i>\n\n"
        f"🔍 <b>Скаут</b> — поиск и привлечение новых кандидатов на различные вакансии.\n"
        f"<i>Полностью обучаем и закрепляем за вами личного ментора-наставника.</i>\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"⬇️ Выбери роль и подай заявку"
    )
    try:
        photo = FSInputFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.png"))
        await message.answer_photo(photo, caption=caption, reply_markup=main_keyboard())
        return
    except Exception as e:
        logger.warning(f"send_welcome answer_photo failed: {e}")
    try:
        await message.answer(caption, reply_markup=main_keyboard())
    except Exception as e:
        logger.error(f"send_welcome answer failed: {e}")


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖤 Заявка — Оператор", callback_data="operator")],
        [InlineKeyboardButton(text="🌑 Заявка — Скаут", callback_data="scout")],
        [InlineKeyboardButton(text="✒️ Изменить анкету оператора", callback_data="edit_operator_app")],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗡 Отменить заявку", callback_data="cancel")]
    ])


async def admin_applications_keyboard():
    op_apps, sc_apps = await asyncio.gather(
        db_get_applications("operator"),
        db_get_applications("scout"),
    )
    operator_count = len(op_apps)
    scout_count = len(sc_apps)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📩 Заявки операторов ({operator_count})", callback_data="view_operator")],
        [InlineKeyboardButton(text=f"🕵️ Заявки скаутов ({scout_count})", callback_data="view_scout")]
    ])


def admin_sticky_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📩 Операторы"), KeyboardButton(text="🕵️ Скауты")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📅 Собеседования")],
            [KeyboardButton(text="🔗 Кто привёл"), KeyboardButton(text="💬 Чаты")],
            [KeyboardButton(text="🧪 Тест заявки")],
        ],
        resize_keyboard=True,
        persistent=True
    )


def parse_interview_slots(text: str) -> list:
    import re
    from datetime import datetime as _dt
    slots = []
    today = _dt.now()
    pattern = re.compile(
        r'(\d{1,2})'               # day
        r'(?:[./]\d{1,2})?'        # optional .MM / /MM
        r'\s*(?:[сСcC]\s*)?'       # optional "с"
        r'(\d{1,2})\s*[-–]\s*(\d{1,2})'  # from-to hours
    )
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = pattern.match(line)
        if not m:
            continue
        day = int(m.group(1))
        hour_from = int(m.group(2))
        hour_to = int(m.group(3))
        month = today.month
        year = today.year
        if day < today.day:
            month += 1
            if month > 12:
                month = 1
                year += 1
        for h in range(hour_from, hour_to + 1):
            try:
                dt = _dt(year, month, day, h, 0)
                slot_text = dt.strftime("%d.%m %H:%M")
                slots.append((slot_text, dt))
            except ValueError:
                continue
    return slots


async def _db_stats_counts():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE app_type='operator') AS op_total,
                COUNT(*) FILTER (WHERE app_type='scout')    AS sc_total,
                COUNT(*) FILTER (WHERE app_type='operator' AND replied_by IS NOT NULL) AS op_replied,
                COUNT(*) FILTER (WHERE app_type='scout'    AND replied_by IS NOT NULL) AS sc_replied
            FROM applications
        """)
    return row


async def admin_stats_text():
    (op_apps, sc_apps), counts = await asyncio.gather(
        asyncio.gather(db_get_applications("operator"), db_get_applications("scout")),
        _db_stats_counts(),
    )
    sep = "─" * 22
    return (
        f"📊 <b>СТАТИСТИКА ЗАЯВОК</b>\n"
        f"{sep}\n"
        f"📩 <b>Операторы</b>\n"
        f"   Всего подано:      <b>{counts['op_total']}</b>\n"
        f"   Ожидают ответа:  <b>{len(op_apps)}</b>\n"
        f"   Отвечено:            <b>{counts['op_replied']}</b>\n\n"
        f"🕵️ <b>Скауты</b>\n"
        f"   Всего подано:      <b>{counts['sc_total']}</b>\n"
        f"   Ожидают ответа:  <b>{len(sc_apps)}</b>\n"
        f"   Отвечено:            <b>{counts['sc_replied']}</b>"
    )


def admin_notify_keyboard(app_type: str, app_id: int):
    rows = []
    if app_type == "scout":
        rows.append([
            InlineKeyboardButton(text="🔵 Kolla", callback_data=f"sq_ap:k:{app_id}"),
        ])
        rows.append([
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_app:{app_id}"),
        ])
    if app_type == "operator":
        rows.append([
            InlineKeyboardButton(text="📅 Собеседование", callback_data=f"app_interview:{app_id}"),
            InlineKeyboardButton(text="💬 Чат", callback_data=f"open_chat:{app_id}"),
        ])
    rows.append([InlineKeyboardButton(text="🗑 Удалить заявку", callback_data=f"del_app:{app_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_confirm_keyboard(app_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Записать", callback_data=f"confirm_op:{app_id}"),
            InlineKeyboardButton(text="❌ Нет дат", callback_data=f"no_dates_op:{app_id}"),
        ]
    ])


def reply_keyboard(app_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить ответ", callback_data=f"cancel_reply:{app_id}")]
    ])

def nav_keyboard(app_type: str, idx: int, total: int, app_id: int):
    prefix = "pg_op" if app_type == "operator" else "pg_sc"
    rows = []
    nav_row = [
        InlineKeyboardButton(text="◀" if idx > 0 else "·", callback_data=f"{prefix}:{idx-1}" if idx > 0 else "noop"),
        InlineKeyboardButton(text=f"📄 {idx+1} / {total}", callback_data="noop"),
        InlineKeyboardButton(text="▶" if idx < total-1 else "·", callback_data=f"{prefix}:{idx+1}" if idx < total-1 else "noop"),
    ]
    rows.append(nav_row)
    if app_type == "scout":
        rows.append([
            InlineKeyboardButton(text="🔵 Kolla", callback_data=f"sq_ap:k:{app_id}"),
        ])
        rows.append([
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_app:{app_id}"),
        ])
    if app_type == "operator":
        rows.append([
            InlineKeyboardButton(text="📅 Собеседование", callback_data=f"app_interview:{app_id}"),
            InlineKeyboardButton(text="💬 Чат", callback_data=f"open_chat:{app_id}"),
        ])
    rows.append([InlineKeyboardButton(text="🗑 Удалить заявку", callback_data=f"del_app:{app_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def format_app_entry(app, idx: int, total: int, emoji: str, label: str, interview_slot: str = None) -> str:
    time_str = app["submitted_at"].strftime("%d.%m.%Y в %H:%M")
    iq_str = "  ✅ Задание пройдено" if app.get("iq_score") else ""
    if interview_slot:
        interview_str = f"\n📅 <b>Собеседование:</b> {interview_slot}"
    else:
        interview_str = ""
    if app.get("replied_by"):
        replied_time = app["replied_at"].strftime("%d.%m в %H:%M") if app.get("replied_at") else ""
        status_line = f"✅ <b>Отвечено</b> · {app['replied_by']} · {replied_time}"
    else:
        status_line = "⏳ <b>Ожидает ответа</b>"
    sep = "─" * 22
    entry = (
        f"{emoji} <b>ЗАЯВКА {idx+1}/{total} — {label.upper()}</b>\n"
        f"{sep}\n"
        f"{status_line}\n\n"
        f"👤 {app['full_name']}\n"
        f"🔗 {app['username']}  ·  🆔 <code>{app['user_id']}</code>\n"
        f"🕐 {time_str}{iq_str}"
        f"{interview_str}\n"
        f"{sep}\n"
        f"📋 <b>АНКЕТА</b>\n\n"
        f"{app['app_text']}"
    )
    if len(entry) > 4000:
        entry = entry[:4000] + "...\n[текст обрезан]"
    return entry


async def safe_send(admin_id: int, text: str, reply_markup=None):
    try:
        await bot.send_message(admin_id, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        logger.warning(f"Админ {admin_id} заблокировал бота — пропускаем.")
    except TelegramBadRequest as e:
        logger.warning(f"Ошибка отправки админу {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при отправке админу {admin_id}: {e}")


async def notify_admins_about_application_decision(app, admin_name: str, actor_id: int, decision: str):
    """Отправляет другим админам единый пинг о решении по заявке."""
    action = "одобрил" if decision == "approved" else "отклонил"
    emoji = "✅" if decision == "approved" else "❌"
    app_type = "оператора" if app["app_type"] == "operator" else "скаута"
    full_name = app["full_name"] or "Без имени"
    username = app["username"] or "без username"
    text = (
        f"{emoji} <b>{admin_name} {action} заявку #{app['id']}</b>\n"
        f"Тип: {app_type}\n"
        f"Кандидат: {full_name}\n"
        f"Контакт: {username}"
    )
    for other_admin_id in ADMIN_IDS:
        if other_admin_id != actor_id:
            await safe_send(other_admin_id, text)

@dp.errors()
async def global_error_handler(event: types.ErrorEvent) -> bool:
    import traceback
    logger.error(
        f"Unhandled exception in handler for update {event.update}: "
        f"{''.join(traceback.format_exception(type(event.exception), event.exception, event.exception.__traceback__))}"
    )
    return True


@dp.message(Command("start"))
async def start(message: types.Message):
    logger.info(f"/start от {message.from_user.id} (@{message.from_user.username})")
    if is_admin(message.from_user):
        sep = "─" * 22
        await message.answer(
            f"👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА</b>\n"
            f"{sep}\n"
            f"📩 <b>Операторы</b> — просмотр заявок операторов\n"
            f"🕵️ <b>Скауты</b> — просмотр заявок скаутов\n"
            f"📅 <b>Собеседования</b> — управление слотами\n"
            f"📊 <b>Статистика</b> — сводка по заявкам\n"
            f"🔗 <b>Кто привёл</b> — рефералы скаутов",
            reply_markup=admin_sticky_keyboard()
        )
        return
    try:
        await db_mark_greeted(message.from_user.id)
    except Exception as e:
        logger.error(f"db_mark_greeted failed: {e}")
    await send_welcome(message)


@dp.message(lambda m: m.text == "📩 Операторы")
async def admin_btn_operator(message: types.Message):
    if not is_admin(message.from_user):
        return
    apps = await db_get_applications("operator")
    if not apps:
        await message.answer("📭 Новых заявок на оператора нет.", reply_markup=admin_sticky_keyboard())
        return
    total = len(apps)
    app = apps[0]
    interview_slot = await db_get_booked_slot_for_app(app["id"])
    text = format_app_entry(app, 0, total, "📩", "оператора", interview_slot)
    kb = nav_keyboard("operator", 0, total, app["id"])
    await message.answer(text, reply_markup=kb)


@dp.message(lambda m: m.text in ("🔍 Скауты", "🕵️ Скауты"))
async def admin_btn_scout(message: types.Message):
    if not is_admin(message.from_user):
        return
    apps = await db_get_applications("scout")
    if not apps:
        await message.answer("📭 Новых заявок на скаута нет.", reply_markup=admin_sticky_keyboard())
        return
    total = len(apps)
    app = apps[0]
    interview_slot = await db_get_booked_slot_for_app(app["id"])
    text = format_app_entry(app, 0, total, "🔍", "скаута", interview_slot)
    kb = nav_keyboard("scout", 0, total, app["id"])
    await message.answer(text, reply_markup=kb)


@dp.message(lambda m: m.text == "📊 Статистика")
async def admin_btn_stats(message: types.Message):
    if not is_admin(message.from_user):
        return
    await message.answer(await admin_stats_text(), reply_markup=admin_sticky_keyboard())


@dp.message(lambda m: m.text == "💬 Чаты")
async def admin_btn_chats(message: types.Message):
    if not is_admin(message.from_user):
        return
    if not pending_chat_requests:
        await message.answer("💬 <b>Чаты</b>\n\nПока нет ни одного запроса на чат.")
        return
    sep = "─" * 22
    rows = []
    for req_user_id, req in pending_chat_requests.items():
        rows.append([InlineKeyboardButton(
            text=f"💬 {req['full_name']} ({req['username']})",
            callback_data=f"reply_chat_req:{req_user_id}:{req['app_id']}"
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    count = len(pending_chat_requests)
    await message.answer(
        f"💬 <b>ВХОДЯЩИЕ ЧАТЫ</b>\n"
        f"{sep}\n"
        f"Ожидают ответа: <b>{count}</b>\n\n"
        f"Нажми на пользователя, чтобы открыть чат:",
        reply_markup=kb
    )


@dp.message(lambda m: m.text == "🧪 Тест заявки")
async def admin_btn_test_app(message: types.Message):
    if not is_admin(message.from_user):
        return
    await send_welcome(message)


@dp.message(lambda m: m.text and m.text.startswith("🔗"))
async def admin_btn_referrals(message: types.Message):
    if not is_admin(message.from_user):
        return
    referrals = await db_get_scout_referrals()
    if not referrals:
        await message.answer("📭 Пока ни один оператор не указал скаута в заявке.")
        return
    sorted_scouts = sorted(referrals.items(), key=lambda x: len(x[1]), reverse=True)
    rows = []
    for scout, operators in sorted_scouts:
        count = len(operators)
        rows.append([InlineKeyboardButton(
            text=f"🕵️ {scout} — {count} {'оператор' if count == 1 else 'оператора' if 2 <= count <= 4 else 'операторов'}",
            callback_data=f"scout_ref:{scout[1:][:30]}"
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        f"🔗 <b>Кто привёл операторов</b>\n\n"
        f"Всего скаутов с рефералами: <b>{len(sorted_scouts)}</b>\n\n"
        f"Нажмите на скаута, чтобы увидеть его операторов:",
        reply_markup=kb
    )


async def show_admin_date_slots(message, date_str: str):
    slots = await db_get_all_slots_for_date(date_str)
    if not slots:
        await message.answer(f"На {date_str} слотов нет.")
        return
    rows = []
    for s in slots:
        time_part = s["slot_text"].split(" ", 1)[1] if " " in s["slot_text"] else s["slot_text"]
        if s["is_booked"]:
            label = f"✅ {time_part} — ID {s['booked_by_user_id']} (#{s['booked_app_id']})"
        else:
            label = f"🟢 {time_part} — свободно"
        rows.append([
            InlineKeyboardButton(text=label, callback_data="noop"),
            InlineKeyboardButton(text="🗑", callback_data=f"admin_del_slot:{s['id']}:{date_str}"),
        ])
    rows.append([InlineKeyboardButton(text=f"🗑 Удалить весь день {date_str}", callback_data=f"admin_del_day:{date_str}")])
    rows.append([InlineKeyboardButton(text="◀ Назад к датам", callback_data="admin_interviews_back")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(f"📅 <b>Слоты на {date_str}:</b>", reply_markup=kb)


async def show_admin_interviews(target):
    dates = await db_get_slot_dates_summary()
    sep = "─" * 22
    rows = []
    if not dates:
        text = (f"📅 <b>СОБЕСЕДОВАНИЯ</b>\n{sep}\n\n" "Свободных слотов пока нет.\n" "Добавьте даты вручную через кнопку ниже.")
    else:
        total_free = sum(v["free"] for v in dates.values())
        total_booked = sum(v["booked"] for v in dates.values())
        for date_part, counts in sorted(dates.items()):
            free_part = f"  🟢 {counts['free']} св." if counts["free"] else ""
            booked_part = f"  ✅ {counts['booked']} зап." if counts["booked"] else ""
            label = f"📅 {date_part}{free_part}{booked_part}"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"admin_idate:{date_part}")])
        text=(f"📅 <b>СОБЕСЕДОВАНИЯ</b>\n" f"{sep}\n" f"🟢 Свободных слотов: <b>{total_free}</b>\n" f"✅ Занятых слотов: <b>{total_booked}</b>\n" f"{sep}\n" "Нажмите на дату для просмотра слотов:")
    rows.append([InlineKeyboardButton(text="➕ Добавить даты вручную", callback_data="interview_add")])
    if dates: rows.append([InlineKeyboardButton(text="🗑 Очистить все слоты", callback_data="interview_clear")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    if hasattr(target,"answer"): await target.answer(text,reply_markup=kb)
    else: await target.message.answer(text,reply_markup=kb)

@dp.message(lambda m: m.text == "📅 Собеседования")
async def admin_btn_interviews(message: types.Message):
    if not is_admin(message.from_user):
        return
    await show_admin_interviews(message)


@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    await message.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "Этот бот принимает заявки на вступление в команду.\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/cancel — отменить текущую заявку\n"
        "/help — показать эту справку\n\n"
        "<b>Как подать заявку:</b>\n"
        "1. Нажмите на кнопку нужной вакансии\n"
        "2. Заполните все поля из шаблона\n"
        "3. Отправьте заявку одним сообщением\n\n"
        "⏳ После подачи заявки повторная отправка возможна через 24 часа."
    )


@dp.message(Command("myid"))
async def myid_cmd(message: types.Message):
    u = message.from_user
    is_admin(u)
    await message.answer(f"🆔 Ваш Telegram ID: <code>{u.id}</code>")


@dp.message(Command("cancel"))
async def cancel_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_application_type:
        del user_application_type[user_id]
        user_iq_state.pop(user_id, None)
        pending_operator_apps.pop(user_id, None)
        await message.answer("❌ <b>Заявка отменена.</b>\n\nВы можете начать заново — /start")
    elif user_id in user_iq_state:
        del user_iq_state[user_id]
        await message.answer("❌ <b>Тест отменён.</b>\n\nВы можете начать заново — /start")
    elif user_id in admin_chat_state:
        state = admin_chat_state.pop(user_id)
        op_id = state["user_id"]
        if op_id in operator_in_chat:
            operator_in_chat[op_id].discard(user_id)
            if not operator_in_chat[op_id]:
                del operator_in_chat[op_id]
        await message.answer("❌ Чат закрыт.")
    elif user_id in user_edit_state:
        user_edit_state.pop(user_id)
        user_application_type.pop(user_id, None)
        await message.answer("🗡 Редактирование анкеты отменено.")
    else:
        await message.answer("У вас нет активной заявки. Нажмите /start чтобы начать.")


@dp.message(Command("applications"))
async def applications_cmd(message: types.Message):
    if not is_admin(message.from_user):
        return
    await message.answer(
        "📋 <b>Просмотр заявок</b>\n\nВыберите тип заявок:",
        reply_markup=await admin_applications_keyboard()
    )


@dp.callback_query()
async def callbacks(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    _cb_user = callback.from_user
    is_admin(_cb_user)

    if callback.data == "cancel":
        if user_id in user_application_type:
            del user_application_type[user_id]
        user_age_state.pop(user_id, None)
        user_iq_state.pop(user_id, None)
        pending_operator_apps.pop(user_id, None)
        try:
            await callback.answer("Заявка отменена.", show_alert=False)
        except Exception:
            pass
        await callback.message.answer(
            "❌ <b>Заявка отменена.</b>\n\nВы можете начать заново — /start"
        )
        return

    if callback.data.startswith("cancel_reply:"):
        if user_id in admin_chat_state:
            state = admin_chat_state.pop(user_id)
            op_id = state["user_id"]
            if op_id in operator_in_chat:
                operator_in_chat[op_id].discard(user_id)
                if not operator_in_chat[op_id]:
                    del operator_in_chat[op_id]
        try:
            await callback.answer("Чат закрыт.", show_alert=False)
        except Exception:
            pass
        await callback.message.answer("❌ Чат закрыт.")
        return

    if callback.data == "interview_add":
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        admin_interview_input[user_id] = True
        try:
            await callback.answer()
        except Exception:
            pass
        await callback.message.answer(
            "📅 <b>Добавление дат собеседований</b>\n\n"
            "Введите даты в формате:\n"
            "<code>26 с 11-18</code>\n\n"
            "Можно несколько строк сразу:\n"
            "<code>26 с 11-18\n27 с 10-15</code>\n\n"
            "Бот автоматически создаст слоты по часам.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="interview_add_cancel")]
            ])
        )
        return

    if callback.data == "interview_add_cancel":
        admin_interview_input.pop(user_id, None)
        try:
            await callback.answer("Отменено.")
        except Exception:
            pass
        return

    if callback.data == "interview_clear":
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            await callback.answer()
        except Exception:
            pass
        await db_clear_slots()
        await callback.message.answer("🗑 Все слоты удалены.")
        return

    if callback.data.startswith("admin_idate:"):
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        date_str = callback.data.split(":", 1)[1]
        try:
            await callback.answer()
        except Exception:
            pass
        await show_admin_date_slots(callback.message, date_str)
        return

    if callback.data.startswith("admin_del_slot:"):
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        _, slot_id_str, date_str = callback.data.split(":", 2)
        await db_delete_slot(int(slot_id_str))
        try:
            await callback.answer("Слот удалён.", show_alert=False)
        except Exception:
            pass
        slots = await db_get_all_slots_for_date(date_str)
        if not slots:
            await callback.message.answer(f"На {date_str} больше нет слотов.")
            await show_admin_interviews(callback)
        else:
            await show_admin_date_slots(callback.message, date_str)
        return

    if callback.data.startswith("admin_del_day:"):
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        date_str = callback.data.split(":", 1)[1]
        await db_delete_slots_for_date(date_str)
        try:
            await callback.answer(f"День {date_str} удалён.", show_alert=False)
        except Exception:
            pass
        await show_admin_interviews(callback)
        return

    if callback.data == "admin_interviews_back":
        if not is_admin(_cb_user):
            return
        try:
            await callback.answer()
        except Exception:
            pass
        await show_admin_interviews(callback)
        return

    if callback.data.startswith("select_interview:"):
        app_id = int(callback.data.split(":")[1])
        _app_check = await db_get_application_by_id(app_id)
        if not _app_check or _app_check["app_type"] != "operator":
            try:
                await callback.answer("Запись на собеседование только для операторов.", show_alert=True)
            except Exception:
                pass
            return
        dates = await db_get_free_slot_dates_summary()
        if not dates:
            try:
                await callback.answer("Слотов для записи пока нет. Попробуйте позже.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await callback.answer()
        except Exception:
            pass
        rows = []
        for date_part, total in sorted(dates.items()):
            rows.append([InlineKeyboardButton(
                text=f"📅 {date_part} — {total} вар." if total > 1 else f"📅 {date_part}",
                callback_data=f"idate:{date_part}:{app_id}"
            )])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await callback.message.answer(
            "📅 <b>Выберите удобную дату для собеседования:</b>",
            reply_markup=kb
        )
        return

    if callback.data.startswith("idate:"):
        parts = callback.data.split(":")
        date_str = parts[1]
        app_id = int(parts[2])
        all_slots = await db_get_free_slots_for_date(date_str)
        if not all_slots:
            try:
                await callback.answer("На эту дату слотов нет.", show_alert=True)
            except Exception:
                pass
            return
        try:
            await callback.answer()
        except Exception:
            pass
        rows = []
        for slot in all_slots:
            time_part = slot["slot_text"].split(" ", 1)[1] if " " in slot["slot_text"] else slot["slot_text"]
            rows.append([InlineKeyboardButton(
                text=f"🕐 {time_part}",
                callback_data=f"book_slot:{slot['id']}:{app_id}"
            )])
        rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"select_interview:{app_id}")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await callback.message.answer(
            f"🕐 <b>Выберите время на {date_str}:</b>",
            reply_markup=kb
        )
        return

    if callback.data.startswith("book_slot:"):
        parts = callback.data.split(":")
        slot_id = int(parts[1])
        app_id = int(parts[2])
        _bk_check = await db_get_application_by_id(app_id)
        if not _bk_check or _bk_check["app_type"] != "operator":
            try:
                await callback.answer("Запись на собеседование только для операторов.", show_alert=True)
            except Exception:
                pass
            return
        slot_record = await db_get_slot_by_id(slot_id)
        if not slot_record:
            try:
                await callback.answer("Ошибка при записи, попробуйте ещё раз.", show_alert=True)
            except Exception:
                pass
            return
        booked = await db_book_slot(slot_id, user_id, app_id)
        if not booked:
            try:
                await callback.answer("Этот слот уже занят. Выберите другое время.", show_alert=True)
            except Exception:
                pass
            return
        slot_text = slot_record["slot_text"]
        # Сначала гарантированно отправляем заявку в CRM, затем подтверждаем запись пользователю.
        # Бронь уже сохранена локально и не откатывается при ошибке CRM.
        await _submit_operator_request_to_crm(_bk_check, slot_id, slot_record)
        if not is_admin(_cb_user):
            await db_set_cooldown(user_id, "operator")
        try:
            await callback.answer(f"✅ Записан на {slot_text}", show_alert=True)
        except Exception:
            pass
        await callback.message.answer(
            f"🕯 <b>Собеседование подтверждено</b>\n\n"
            f"📅 <b>{slot_text}</b>\n\n"
            f"🕒 Часовой пояс: UTC+3 (Минск, Москва, Стамбул).\n"
            f"Проверь соответствие своему местному времени.\n\n"
            f"💻 Формат: онлайн, через Zoom.\n"
            f"🔗 Скачать Zoom: https://zoom.us/download\n\n"
            f"Ссылку на конференцию пришлют за 5 минут до начала — будь на связи.\n\n"
            f"HR-специалисты:\n"
            f"@hr_helper12\n"
            f"@hr_helper13\n\n"
            f"Если планы изменятся — сообщи менеджеру заранее.\n"
            f"Напомним за 2 часа до встречи."
        )
        pending = pending_operator_apps.pop(user_id, None)
        sep = "─" * 22
        if pending and pending["app_id"] == app_id:
            base_msg = pending["admin_message"]
        else:
            app_rec = await db_get_application_by_id(app_id)
            if app_rec:
                iq_line = "  ✅ Задание пройдено" if app_rec.get("iq_score") else ""
                submitted = app_rec["submitted_at"].strftime("%d.%m.%Y в %H:%M") if app_rec.get("submitted_at") else "—"
                base_msg = (
                    f"📩 <b>НОВАЯ ЗАЯВКА — ОПЕРАТОР</b>\n"
                    f"{sep}\n"
                    f"👤 {app_rec['full_name']}\n"
                    f"🔗 {app_rec['username']}  ·  🆔 <code>{app_rec['user_id']}</code>\n"
                    f"🕐 {submitted}{iq_line}\n"
                    f"{sep}\n"
                    f"📋 <b>АНКЕТА</b>\n\n"
                    f"{app_rec['app_text']}"
                    f"\n\n👁 @werolk @whyisrey"
                )
            else:
                user_info = callback.from_user
                full_name = f"{user_info.first_name or ''} {user_info.last_name or ''}".strip() or "Без имени"
                username_str = f"@{user_info.username}" if user_info.username else "без username"
                base_msg = (
                    f"📩 <b>НОВАЯ ЗАЯВКА — ОПЕРАТОР</b>\n"
                    f"{sep}\n"
                    f"👤 {full_name}\n"
                    f"🔗 {username_str}  ·  🆔 <code>{user_id}</code>\n"
                    f"\n\n👁 @werolk @whyisrey"
                )
        full_admin_msg = base_msg + f"\n{sep}\n📅 <b>Собеседование: {slot_text}</b>"
        for admin_id in ADMIN_IDS:
            await safe_send(admin_id, full_admin_msg, reply_markup=admin_notify_keyboard("operator", app_id))
        return

    if callback.data.startswith("scout_ref:"):
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        scout_username = "@" + callback.data.split(":", 1)[1]
        try:
            await callback.answer()
        except Exception:
            pass
        referrals = await db_get_scout_referrals()
        operators = referrals.get(scout_username, [])
        if not operators:
            await callback.message.answer(f"Операторов от {scout_username} не найдено.")
            return
        count = len(operators)
        lines = [
            f"🔗 <b>Операторы привлечённые {scout_username}</b>",
            f"Всего: <b>{count}</b>\n",
        ]
        for i, op in enumerate(operators, 1):
            tg_username = op["username"] if op["username"] else "без username"
            name = op["form_name"] or op["full_name"] or "—"
            dob = op["form_dob"] or "—"
            lines.append(
                f"<b>{i}.</b> {name}\n"
                f"   👤 {tg_username}\n"
                f"   🎂 {dob}"
            )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀ Назад", callback_data="referrals_back")]
        ])
        await callback.message.answer("\n\n".join(lines), reply_markup=kb)
        return

    if callback.data == "referrals_back":
        if not is_admin(_cb_user):
            return
        try:
            await callback.answer()
        except Exception:
            pass
        referrals = await db_get_scout_referrals()
        if not referrals:
            await callback.message.answer("📭 Пока ни один оператор не указал скаута в заявке.")
            return
        sorted_scouts = sorted(referrals.items(), key=lambda x: len(x[1]), reverse=True)
        rows = []
        for scout, operators in sorted_scouts:
            count = len(operators)
            rows.append([InlineKeyboardButton(
                text=f"🕵️ {scout} — {count} {'оператор' if count == 1 else 'оператора' if 2 <= count <= 4 else 'операторов'}",
                callback_data=f"scout_ref:{scout[1:][:30]}"
            )])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await callback.message.answer(
            f"🔗 <b>Кто привёл операторов</b>\n\n"
            f"Всего скаутов с рефералами: <b>{len(sorted_scouts)}</b>\n\n"
            f"Нажмите на скаута, чтобы увидеть его операторов:",
            reply_markup=kb
        )
        return

    if callback.data.startswith("open_chat:") or callback.data.startswith("reply_app:"):
        if not is_admin(_cb_user):
            try:
                await callback.answer("У вас нет доступа.", show_alert=True)
            except Exception:
                pass
            return
        try:
            app_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        app = await db_get_application_by_id(app_id)
        if not app:
            try:
                await callback.answer("Заявка не найдена.", show_alert=True)
            except Exception:
                pass
            return
        op_user_id = app["user_id"]
        admin_chat_state[user_id] = {"user_id": op_user_id, "app_id": app_id}
        if op_user_id not in operator_in_chat:
            operator_in_chat[op_user_id] = set()
        operator_in_chat[op_user_id].add(user_id)
        admin_name = f"@{callback.from_user.username}" if callback.from_user.username else (callback.from_user.first_name or "Админ")
        try:
            await callback.answer()
        except Exception:
            pass
        await callback.message.answer(
            f"💬 <b>Чат открыт</b>\n\n"
            f"С кем: <b>{app['full_name']}</b> ({app['username']})\n"
            f"Пишите — сообщения будут уходить напрямую пользователю.\n"
            f"Для закрытия чата нажмите кнопку ниже или /cancel",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔴 Закрыть чат", callback_data=f"close_chat:{app_id}")]
            ])
        )
        try:
            await bot.send_message(
                op_user_id,
                f"🖤 <b>Администрация на связи.</b>\n\nПишите — ответы будут поступать сюда."
            )
        except Exception:
            pass
        return

    if callback.data.startswith("close_chat:"):
        if not is_admin(_cb_user):
            return
        try:
            app_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            return
        state = admin_chat_state.pop(user_id, None)
        if state:
            op_id = state["user_id"]
            if op_id in operator_in_chat:
                operator_in_chat[op_id].discard(user_id)
                if not operator_in_chat[op_id]:
                    del operator_in_chat[op_id]
                    try:
                        await bot.send_message(op_id, "🌑 Чат с администрацией закрыт.")
                    except Exception:
                        pass
        try:
            await callback.answer("Чат закрыт.", show_alert=False)
        except Exception:
            pass
        await callback.message.answer("🔴 Чат закрыт.")
        return

    if callback.data == "edit_invalid_operator":
        user_application_type[user_id] = "operator"
        try:
            await callback.answer()
        except Exception:
            pass
        await callback.message.answer(
            "✏️ <b>Исправь заявку оператора</b>\n\n"
            "Отправь все поля одним сообщением — пустые поля снова будут показаны отдельно:\n\n"
            "Имя:\n"
            "Возраст (и дата рождения):\n"
            "Знание английского языка:\n"
            "Модель процессора:\n"
            "Модель видеокарты:\n"
            "Скорость Интернета:\n"
            "Где работал/ла:\n"
            "Номер телефона (с кодом страны):\n"
            "Телеграмм:\n"
            "Кто привёл / откуда узнали о нас:",
            reply_markup=cancel_keyboard()
        )
        return

    if callback.data == "edit_operator_app":
        existing = await db_get_application_by_user(user_id, "operator")
        try:
            await callback.answer()
        except Exception:
            pass
        if not existing:
            await callback.message.answer(
                "🌑 Активная заявка на оператора не найдена.\n\n"
                "Подай заявку через кнопку ниже.",
                reply_markup=main_keyboard()
            )
            return
        user_edit_state[user_id] = True
        user_application_type[user_id] = "operator"
        form_text = (
            "✒️ <b>Редактирование анкеты оператора</b>\n\n"
            "▸ Заполни все поля и отправь <b>одним сообщением</b>:\n\n"
            "Имя:\n"
            "Возраст (и дата рождения):\n"
            "Знание английского языка:\n"
            "Модель процессора:\n"
            "Модель видеокарты:\n"
            "Скорость Интернета:\n"
            "Где работал/ла:\n"
            "Номер телефона (с кодом страны):\n"
            "Телеграмм:\n"
            "Кто привёл / откуда узнали о нас (укажи юзернейм):"
        )
        await callback.message.answer(form_text, reply_markup=cancel_keyboard())
        return

    if callback.data in ("view_operator", "view_scout"):
        if not is_admin(_cb_user):
            try:
                await callback.answer("У вас нет доступа.", show_alert=True)
            except Exception:
                pass
            return
        app_type = "operator" if callback.data == "view_operator" else "scout"
        apps = await db_get_applications(app_type)
        emoji = "📩" if app_type == "operator" else "🕵️"
        label = "оператора" if app_type == "operator" else "скаута"
        try:
            await callback.answer()
        except Exception:
            pass
        if not apps:
            await callback.message.answer(f"📭 Заявок на {label} пока нет.")
            return
        total = len(apps)
        app = apps[0]
        interview_slot = await db_get_booked_slot_for_app(app["id"])
        text = format_app_entry(app, 0, total, emoji, label, interview_slot)
        kb = nav_keyboard(app_type, 0, total, app["id"])
        await callback.message.answer(text, reply_markup=kb)
        return

    if callback.data.startswith("app_interview:"):
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            app_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        try:
            await callback.answer()
        except Exception:
            pass
        slot = await db_get_slot_by_app_id(app_id)
        if not slot:
            await callback.message.answer(f"📅 <b>Собеседование (заявка #{app_id})</b>\n\n❌ Не назначено")
            return
        from datetime import timedelta as _td2
        if slot["slot_dt"] and not slot["reminder_sent"]:
            remind_dt = slot["slot_dt"] - _td2(hours=2)
            remind_str = remind_dt.strftime("%d.%m в %H:%M") + " МСК"
            reminder_status = f"❌ Не отправлено\n🕐 Запланировано на: <b>{remind_str}</b>"
        elif slot["reminder_sent"]:
            reminder_status = "✅ Отправлено"
        else:
            reminder_status = "❌ Не отправлено"
        await callback.message.answer(
            f"📅 <b>Собеседование (заявка #{app_id})</b>\n\n"
            f"Дата: <b>{slot['slot_text']}</b>\n\n"
            f"Напоминание: {reminder_status}"
        )
        return

    if callback.data.startswith("del_app:"):
        if not is_admin(_cb_user):
            try:
                await callback.answer("У вас нет доступа.", show_alert=True)
            except Exception:
                pass
            return
        try:
            app_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        try:
            await callback.answer()
        except Exception:
            pass
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_confirm:{app_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"del_cancel:{app_id}"),
            ]
        ])
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    if callback.data.startswith("del_confirm:"):
        if not is_admin(_cb_user):
            return
        try:
            app_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        app = await db_get_application_by_id(app_id)
        await db_delete_application_by_id(app_id)
        admin = callback.from_user
        admin_name = f"@{admin.username}" if admin.username else (admin.first_name or "Админ")
        try:
            await callback.answer("🗑 Заявка удалена.", show_alert=False)
            await callback.message.edit_text(f"🗑 <b>Заявка удалена</b> администратором {admin_name}.", reply_markup=None)
        except Exception:
            pass
        if app:
            app_type_label = "оператора" if app["app_type"] == "operator" else "скаута"
            notify_text = (
                f"🗑 <b>Заявка удалена</b>\n\n"
                f"Удалил: {admin_name}\n"
                f"Тип: заявка на {app_type_label}\n"
                f"Соискатель: {app['full_name']} ({app['username']})\n"
                f"🆔 <code>{app['user_id']}</code>"
            )
            for other_admin_id in ADMIN_IDS:
                if other_admin_id != user_id:
                    await safe_send(other_admin_id, notify_text)
        return

    if callback.data.startswith("del_cancel:"):
        if not is_admin(_cb_user):
            return
        try:
            app_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            return
        app = await db_get_application_by_id(app_id)
        if not app:
            try:
                await callback.answer("Заявка уже удалена.", show_alert=True)
            except Exception:
                pass
            return
        app_type = app["app_type"]
        apps = await db_get_applications(app_type)
        if not apps:
            try:
                await callback.answer()
                await callback.message.edit_text(f"📭 Заявок больше нет.", reply_markup=None)
            except Exception:
                pass
            return
        idx = next((i for i, a in enumerate(apps) if a["id"] == app_id), 0)
        emoji = "📩" if app_type == "operator" else "🔍"
        label = "оператора" if app_type == "operator" else "скаута"
        interview_slot = await db_get_booked_slot_for_app(apps[idx]["id"])
        text = format_app_entry(apps[idx], idx, len(apps), emoji, label, interview_slot)
        kb = nav_keyboard(app_type, idx, len(apps), apps[idx]["id"])
        try:
            await callback.answer()
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        return

    if callback.data.startswith("sq_ap:"):
        if not is_admin(_cb_user):
            try:
                await callback.answer("У вас нет доступа.", show_alert=True)
            except Exception:
                pass
            return
        try:
            parts = callback.data.split(":")
            squad_key = parts[1]
            app_id = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        if squad_key not in SQUADS:
            try:
                await callback.answer("Этот сквад больше недоступен.", show_alert=True)
            except Exception:
                pass
            return
        squad_name, squad_link = SQUADS[squad_key]
        app = await db_get_application_by_id(app_id)
        if not app:
            try:
                await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
            except Exception:
                pass
            return
        admin = callback.from_user
        admin_name = f"@{admin.username}" if admin.username else (admin.first_name or "Админ")
        user_message = (
            f"⚡ Твоя заявка одобрена.\n\n"
            f"Ссылка на сквад: {squad_link}\n\n"
            f"Изучи материал в группе и приступай. По вопросам — к ментору или в чат."
        )
        try:
            await bot.send_message(app["user_id"], user_message, parse_mode=None)
        except TelegramForbiddenError:
            await db_delete_applications_by_user(app["user_id"])
            try:
                await callback.answer("❌ Пользователь заблокировал бота — заявка удалена.", show_alert=True)
                await callback.message.edit_text("🗑 Заявка удалена: пользователь заблокировал бота.", reply_markup=None)
            except Exception:
                pass
            return
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю: {e}")
            await callback.answer(f"❌ Не удалось отправить сообщение пользователю: {e}", show_alert=True)
            return
        await db_mark_replied(app_id, admin_name)
        try:
            await callback.answer(f"✅ Одобрено в {squad_name}!", show_alert=False)
        except Exception:
            pass
        await notify_admins_about_application_decision(
            app, admin_name, user_id, "approved"
        )
        try:
            await callback.message.edit_text(
                callback.message.text + f"\n\n✅ <b>Одобрено в {squad_name}</b> · {admin_name}",
                reply_markup=None
            )
        except Exception:
            pass
        return

    if callback.data.startswith("chat_with_admin:"):
        try:
            parts = callback.data.split(":")
            app_id = int(parts[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        if user_id in operator_in_chat:
            await callback.answer("Чат уже открыт — просто напиши сообщение.", show_alert=True)
            return
        if user_id in pending_chat_requests:
            await callback.answer("Запрос уже отправлен — ожидай ответа администратора.", show_alert=True)
            return
        full_name = f"{callback.from_user.first_name or ''} {callback.from_user.last_name or ''}".strip() or "Без имени"
        username_str = f"@{callback.from_user.username}" if callback.from_user.username else "без username"
        pending_chat_requests[user_id] = {
            "full_name": full_name,
            "username": username_str,
            "app_id": app_id,
        }
        sep = "─" * 22
        notify_text = (
            f"💬 <b>Новый запрос на чат</b>\n"
            f"{sep}\n"
            f"👤 {full_name} ({username_str})\n"
            f"Заявка #{app_id}\n\n"
            f"Нажми кнопку ниже или зайди в <b>💬 Чаты</b>, чтобы ответить."
        )
        for adm_id in ADMIN_IDS:
            await safe_send(
                adm_id,
                notify_text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"💬 Ответить {full_name}", callback_data=f"reply_chat_req:{user_id}:{app_id}")]
                ])
            )
        try:
            await callback.answer()
        except Exception:
            pass
        await callback.message.answer(
            "🖤 <b>Запрос отправлен.</b>\n\nАдминистратор скоро ответит — жди сообщения прямо здесь."
        )
        return

    if callback.data.startswith("reply_chat_req:"):
        if not is_admin(_cb_user):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        try:
            parts = callback.data.split(":")
            target_user_id = int(parts[1])
            app_id = int(parts[2])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        req = pending_chat_requests.get(target_user_id)
        if not req:
            await callback.answer("Запрос уже закрыт или обработан другим администратором.", show_alert=True)
            return
        pending_chat_requests.pop(target_user_id, None)
        if target_user_id not in operator_in_chat:
            operator_in_chat[target_user_id] = set()
        operator_in_chat[target_user_id].add(user_id)
        admin_chat_state[user_id] = {"user_id": target_user_id, "app_id": app_id}
        admin_name = f"@{_cb_user.username}" if _cb_user.username else (_cb_user.first_name or "Админ")
        try:
            await bot.send_message(
                target_user_id,
                f"🖤 <b>Администратор {admin_name} на связи.</b>\n\nПиши — ответы придут сюда, в бот."
            )
        except Exception:
            pass
        try:
            await callback.answer()
        except Exception:
            pass
        await callback.message.answer(
            f"💬 <b>Чат открыт</b>\n\n"
            f"С кем: <b>{req['full_name']}</b> ({req['username']})\n"
            f"Пишите — сообщения уходят напрямую пользователю.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔴 Закрыть чат", callback_data=f"close_chat:{app_id}")]
            ])
        )
        return

    if callback.data.startswith("reject_app:"):
        if not is_admin(_cb_user):
            try:
                await callback.answer("У вас нет доступа.", show_alert=True)
            except Exception:
                pass
            return
        try:
            app_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            await callback.answer("Ошибка.", show_alert=True)
            return
        app = await db_get_application_by_id(app_id)
        if not app:
            try:
                await callback.answer("Заявка не найдена или уже обработана.", show_alert=True)
            except Exception:
                pass
            return
        admin = callback.from_user
        admin_name = f"@{admin.username}" if admin.username else (admin.first_name or "Админ")
        reject_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать администратору", callback_data=f"chat_with_admin:{app_id}:{admin.id}")]
        ])
        try:
            await bot.send_message(
                app["user_id"],
                "🌑 <b>Твоя заявка на скаута отклонена.</b>\n\n"
                "Если хочешь уточнить причину — нажми кнопку ниже и напиши прямо здесь, в боте.",
                reply_markup=reject_kb
            )
        except TelegramForbiddenError:
            await db_delete_applications_by_user(app["user_id"])
            try:
                await callback.answer("❌ Пользователь заблокировал бота — заявка удалена.", show_alert=True)
                await callback.message.edit_text("🗑 Заявка удалена: пользователь заблокировал бота.", reply_markup=None)
            except Exception:
                pass
            return
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю: {e}")
            await callback.answer(f"❌ Не удалось отправить сообщение: {e}", show_alert=True)
            return
        await db_mark_replied(app_id, admin_name)
        try:
            await callback.answer("❌ Заявка отклонена.", show_alert=False)
        except Exception:
            pass
        await notify_admins_about_application_decision(
            app, admin_name, user_id, "rejected"
        )
        try:
            await callback.message.edit_text(
                callback.message.text + f"\n\n❌ <b>Отклонено</b> · {admin_name}",
                reply_markup=None
            )
        except Exception:
            pass
        return

    if callback.data == "noop":
        try:
            await callback.answer()
        except Exception:
            pass
        return

    if callback.data.startswith("pg_op:") or callback.data.startswith("pg_sc:"):
        try:
            await callback.answer()
        except Exception:
            pass
        parts = callback.data.split(":")
        is_op = parts[0] == "pg_op"
        idx = int(parts[1])
        app_type = "operator" if is_op else "scout"
        emoji = "📩" if is_op else "🔍"
        label = "оператора" if is_op else "скаута"
        apps = await db_get_applications(app_type)
        total = len(apps)
        if not apps:
            try:
                await callback.message.edit_text(f"📭 Новых заявок на {label} нет.")
            except Exception:
                pass
            return
        idx = min(idx, total - 1)
        idx = max(idx, 0)
        app = apps[idx]
        interview_slot = await db_get_booked_slot_for_app(app["id"])
        text = format_app_entry(app, idx, total, emoji, label, interview_slot)
        kb = nav_keyboard(app_type, idx, total, app["id"])
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass
        return

    if callback.data.startswith("iq_"):
        state = user_iq_state.get(user_id)
        if not state:
            try:
                await callback.answer("Начните заявку заново через /start", show_alert=True)
            except Exception:
                pass
            return
        chosen = callback.data[3:]
        correct = IQ_QUESTIONS[state["question"]]["answer"]
        try:
            await callback.answer()
        except Exception:
            pass
        if chosen != correct:
            app_type = state["app_type"]
            user_iq_state.pop(user_id, None)
            retry_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Попробовать ещё раз",
                    callback_data=app_type
                )]
            ])
            await callback.message.answer(
                "💀 <b>Неверный ответ.</b>\n\n"
                "Проверка не пройдена. Попробуй ещё раз — может, повезёт.",
                reply_markup=retry_kb
            )
            return
        state["score"] += 1
        state["question"] += 1
        if state["question"] < len(IQ_QUESTIONS):
            await callback.message.answer(
                IQ_QUESTIONS[state["question"]]["q"],
                reply_markup=iq_keyboard(state["question"])
            )
        else:
            total = len(IQ_QUESTIONS)
            score = state["score"]
            iq_points = round(70 + (score / total) * 60)
            state["iq_score"] = iq_points
            app_type = state["app_type"]
            user_iq_state.pop(user_id, None)
            await callback.message.answer("⚡ <b>Задание выполнено.</b>")
            if app_type == "operator":
                user_age_state[user_id] = {"iq_score": iq_points}
                await callback.message.answer(
                    "🌑 <b>Укажи дату рождения</b>\n\n"
                    "Например: <code>15.03.2004</code>",
                    reply_markup=cancel_keyboard()
                )
                return
            user_application_type[user_id] = app_type
            await callback.message.answer("🌑 Заполни анкету ниже:")
            form_text = (
                    "🌑 <b>Заявка на скаута</b>\n\n"
                    "⛓ <b>Пробное задание:</b>\n"
                    "Представь: я — девушка с Bimbo/DW, просто ищу общение. "
                    "Твоя задача — убедить меня рассмотреть работу моделью: зарплата от 150к₽, график 5/2 или 4/3, стримы от 5 часов. "
                    "Суть работы — стримить без 18+ для иностранной аудитории, общаться со зрителями. "
                    "Что напишешь?\n\n"
                    "▸ <b>Анкета — заполни всё одним сообщением:</b>\n\n"
                    "1. Имя:\n"
                    "2. Дата рождения (дд.мм.гггг):\n"
                    "3. Telegram:\n"
                    "4. Номер телефона:\n"
                    "5. Ответ на задание:"
                )
            await callback.message.answer(form_text, reply_markup=cancel_keyboard())
        return

    logger.info(f"Нажата кнопка '{callback.data}' пользователем {user_id}")

    if not is_admin(_cb_user):
        last_time = await db_get_cooldown(user_id, callback.data)
        if last_time:
            last_time = last_time.replace(tzinfo=None)
            if datetime.now() - last_time < timedelta(hours=COOLDOWN_HOURS):
                remaining = timedelta(hours=COOLDOWN_HOURS) - (datetime.now() - last_time)
                hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                minutes = remainder // 60
                try:
                    await callback.answer(
                        f"⏳ Заявка уже подана. Повторная — через {hours} ч. {minutes} мин.",
                        show_alert=True
                    )
                except Exception:
                    pass
                return

    user_iq_state[user_id] = {"app_type": callback.data, "question": 0, "score": 0, "iq_score": 70}
    try:
        await callback.answer()
    except Exception:
        pass

    await callback.message.answer(
        "🔮 <b>Проверочное задание</b>\n\n"
        "Ответь на вопрос, прежде чем продолжить:\n\n"
        + IQ_QUESTIONS[0]["q"],
        reply_markup=iq_keyboard(0)
    )


@dp.message(Command("reply"))
async def reply_user(message: types.Message):
    if not is_admin(message.from_user):
        return
    try:
        parts = message.text.split(" ", 2)
        if len(parts) < 3:
            raise ValueError("Недостаточно аргументов")
        _, user_id_str, text = parts
        target_user_id = int(user_id_str)
        admin = message.from_user
        admin_name = f"@{admin.username}" if admin.username else (admin.first_name or "Админ")
        await bot.send_message(target_user_id, f"📬 <b>Ответ от администрации</b> ({admin_name}):\n\n{text}")
        await message.answer("✅ Ответ отправлен")
        for other_admin_id in ADMIN_IDS:
            if other_admin_id != admin.id:
                await safe_send(
                    other_admin_id,
                    f"💬 <b>{admin_name} ответил пользователю</b> <code>{target_user_id}</code>:\n\n{text}"
                )
    except TelegramForbiddenError:
        await db_delete_applications_by_user(target_user_id)
        await message.answer("❌ Пользователь заблокировал бота — все его заявки удалены.")
    except (TelegramBadRequest, ValueError):
        await message.answer("❌ Используй: /reply user_id текст")
    except Exception as e:
        logger.error(f"Ошибка в /reply: {e}")
        await message.answer("❌ Не удалось отправить ответ.")


@dp.message()
async def handle_message(message: types.Message):
    user = message.from_user

    if user.id in admin_chat_state and is_admin(user):
        if not message.text:
            await message.answer("⚠️ Отправьте сообщение текстом.")
            return
        state = admin_chat_state[user.id]
        target_user_id = state["user_id"]
        app_id = state["app_id"]
        admin_name = f"@{user.username}" if user.username else (user.first_name or "Админ")
        try:
            await bot.send_message(
                target_user_id,
                f"📬 <b>Сообщение от администрации</b> ({admin_name}):\n\n{message.text}"
            )
            await db_mark_replied(app_id, admin_name)
            await message.answer(
                "✅ Сообщение отправлено.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔴 Закрыть чат", callback_data=f"close_chat:{app_id}")]
                ])
            )
            for other_admin_id in ADMIN_IDS:
                if other_admin_id != user.id:
                    await safe_send(
                        other_admin_id,
                        f"💬 <b>{admin_name}</b> → пользователю <code>{target_user_id}</code> (заявка #{app_id}):\n\n{message.text}"
                    )
        except TelegramForbiddenError:
            admin_chat_state.pop(user.id, None)
            if target_user_id in operator_in_chat:
                operator_in_chat[target_user_id].discard(user.id)
                if not operator_in_chat[target_user_id]:
                    del operator_in_chat[target_user_id]
            await db_delete_applications_by_user(target_user_id)
            await message.answer("❌ Пользователь заблокировал бота — все его заявки удалены.")
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения: {e}")
            await message.answer("❌ Не удалось отправить сообщение.")
        return

    if user.id in operator_in_chat and not is_admin(user):
        if not message.text:
            return
        admin_ids_in_chat = list(operator_in_chat[user.id])
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Без имени"
        username_str = f"@{user.username}" if user.username else "без username"
        for adm_id in admin_ids_in_chat:
            await safe_send(
                adm_id,
                f"💬 <b>Сообщение от пользователя</b>\n"
                f"👤 {full_name} ({username_str}) <code>{user.id}</code>:\n\n{message.text}"
            )
        await message.answer("⚡ Сообщение доставлено.")
        return

    if user.id in admin_interview_input and is_admin(user):
        if not message.text:
            await message.answer("⚠️ Отправьте даты текстом.")
            return
        admin_interview_input.pop(user.id, None)
        slots = parse_interview_slots(message.text)
        if not slots:
            await message.answer(
                "⚠️ Не удалось распознать даты.\n\n"
                "Используйте формат: <code>26 с 11-18</code>"
            )
            return
        await db_add_interview_slots(slots)
        slot_list = "\n".join(f"  • {s[0]}" for s in slots)
        await message.answer(
            f"✅ <b>Добавлено {len(slots)} слотов:</b>\n\n{slot_list}"
        )
        return

    if user.id in user_age_state:
        if not message.text:
            await message.answer("⚠️ Пожалуйста, введите возраст текстом.")
            return
        age_text = message.text.strip()
        age = None
        import re
        from datetime import datetime as _dt
        full_date = re.search(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})', age_text)
        if full_date:
            try:
                d, m, y = int(full_date.group(1)), int(full_date.group(2)), int(full_date.group(3))
                bday = _dt(y, m, d)
                age = (_dt.now() - bday).days // 365
            except Exception:
                pass
        if age is None:
            year_only = re.search(r'\b(19\d{2}|20\d{2})\b', age_text)
            if year_only:
                age = _dt.now().year - int(year_only.group())
        if age is None:
            explicit = re.search(r'\b(\d{1,2})\b', age_text)
            if explicit:
                candidate = int(explicit.group(1))
                if 5 <= candidate <= 80:
                    age = candidate
        if age is None:
            await message.answer("⚠️ Возраст не распознан. Введи ещё раз — например: <code>20</code> или <code>15.03.2004</code>")
            return
        if age < 18 and not is_admin(user):
            user_age_state.pop(user.id, None)
            scout_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌑 Заявка — Скаут", callback_data="scout")]
            ])
            await message.answer(
                f"🌑 <b>Оператором — только с 18 лет ({age} лет).</b>\n\n"
                "Но ты можешь подать заявку на <b>скаута</b> — без возрастных ограничений.",
                reply_markup=scout_kb
            )
            return
        iq_score = user_age_state.pop(user.id)["iq_score"]
        user_application_type[user.id] = "operator"
        await message.answer("🌑 Заполни анкету ниже:")
        form_text = (
            "🖤 <b>Заявка на оператора</b>\n\n"
            "▸ Заполни все поля и отправь <b>одним сообщением</b>:\n\n"
            "Имя:\n"
            "Возраст (и дата рождения):\n"
            "Знание английского языка:\n"
            "Модель процессора:\n"
            "Модель видеокарты:\n"
            "Скорость Интернета:\n"
            "Где работал/ла:\n"
            "Номер телефона (с кодом страны):\n"
            "Телеграмм:\n"
            "Кто привёл / откуда узнали о нас (укажи юзернейм):"
        )
        await message.answer(form_text, reply_markup=cancel_keyboard())
        return

    app_type = user_application_type.get(user.id)

    if not app_type:
        if not await db_is_greeted(user.id):
            await db_mark_greeted(user.id)
            await send_welcome(message)
        else:
            await message.answer(
                "Пожалуйста, выберите тип заявки через /start",
                reply_markup=main_keyboard()
            )
        return

    if not message.text:
        await message.answer(
            "⚠️ Пожалуйста, отправьте заявку текстом, а не фото/стикером/голосом."
        )
        return

    if app_type == "operator":
        type_label = "оставил заявку на оператора"
        type_emoji = "📩"
        mention = "\n\n👁 @werolk @whyisrey"
    else:
        type_label = "оставил заявку на скаута"
        type_emoji = "🕵️"
        mention = ""

    now = datetime.now().strftime("%d.%m.%Y в %H:%M")
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Без имени"
    username_str = f"@{user.username}" if user.username else "без username"

    app_text = message.text
    if len(app_text) > MAX_TEXT_LENGTH:
        app_text = app_text[:MAX_TEXT_LENGTH] + "...\n[текст обрезан]"

    if app_type == "operator" and not is_admin(user):
        missing_fields = validate_operator_form(app_text)
        if missing_fields:
            fields_list = "\n".join(f"• {f}" for f in missing_fields)
            await message.answer(
                f"⚠️ <b>Анкета не заполнена до конца.</b>\n\n"
                f"Отсутствуют поля:\n{fields_list}\n\n"
                f"Дополни и отправь заново одним сообщением.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Изменить заявку", callback_data="edit_invalid_operator")],
                    [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
                ])
            )
            return

    if app_type == "scout" and not is_admin(user):
        wait_msg = await message.answer("🌑 Анализирую анкету...")
        is_ai, _ = await check_ai_generated(app_text)
        try:
            await bot.delete_message(message.chat.id, wait_msg.message_id)
        except Exception:
            pass
        if is_ai:
            retry_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать ещё раз", callback_data=app_type)]
            ])
            user_application_type.pop(user.id, None)
            await message.answer(
                "💀 <b>Анкета отклонена.</b>\n\n"
                "Текст похож на написанный ИИ (ChatGPT, Claude и др.).\n"
                "Нужны твои слова — живые, без шаблонов.",
                reply_markup=retry_kb
            )
            return

    logger.info(f"Заявка от {user.id} (@{user.username})")

    is_edit = user_edit_state.pop(user.id, False)
    del user_application_type[user.id]

    if is_edit:
        await db_update_application_text(user.id, app_type, app_text)
        existing = await db_get_application_by_user(user.id, app_type)
        app_id = existing["id"] if existing else None
        sep = "─" * 22
        admin_message = (
            f"✏️ <b>ОБНОВЛЕНИЕ АНКЕТЫ — ОПЕРАТОР</b>\n"
            f"{sep}\n"
            f"👤 {full_name}\n"
            f"🔗 {username_str}  ·  🆔 <code>{user.id}</code>\n"
            f"🕐 {now}\n"
            f"{sep}\n"
            f"📋 <b>НОВЫЙ ТЕКСТ АНКЕТЫ</b>\n\n"
            f"{app_text}\n\n"
            f"👁 @werolk @whyisrey"
        )
        for admin_id in ADMIN_IDS:
            kb = admin_notify_keyboard(app_type, app_id) if app_id else None
            await safe_send(admin_id, admin_message, reply_markup=kb)
        await message.answer("⚡ <b>Анкета обновлена.</b>\n\nИзменения сохранены.")
        return

    iq_state = user_iq_state.pop(user.id, None)
    iq_score = iq_state["iq_score"] if iq_state else None

    app_id = await db_save_application(app_type, user.id, full_name, username_str, app_text, iq_score)

    iq_line = "  ✅ Задание пройдено" if iq_score is not None else ""
    sep = "─" * 22
    type_header = "ОПЕРАТОР" if app_type == "operator" else "СКАУТ"
    test_tag = "🧪 <b>[ТЕСТ — ЗАЯВКА ОТ АДМИНА]</b>\n" if is_admin(user) else ""
    admin_message = (
        f"{test_tag}{type_emoji} <b>НОВАЯ ЗАЯВКА — {type_header}</b>\n"
        f"{sep}\n"
        f"👤 {full_name}\n"
        f"🔗 {username_str}  ·  🆔 <code>{user.id}</code>\n"
        f"🕐 {now}{iq_line}\n"
        f"{sep}\n"
        f"📋 <b>АНКЕТА</b>\n\n"
        f"{app_text}"
        f"{mention}"
    )

    if app_type == "operator":
        slots_summary = await db_get_free_slot_dates_summary()
        if slots_summary:
            pending_operator_apps[user.id] = {
                "app_id": app_id,
                "admin_message": admin_message,
            }
            interview_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🗓 Выбрать время собеседования",
                    callback_data=f"select_interview:{app_id}"
                )]
            ])
            await message.answer(
                "⚡ <b>Заявка принята.</b>\n\n"
                "Выбери удобное время для собеседования.\n"
                "После выбора заявка уйдёт на рассмотрение.",
                reply_markup=interview_kb
            )
        else:
            await message.answer(
                "🌑 <b>Свободных дат пока нет.</b>\n\n"
                "Собеседования временно недоступны. Попробуй позже."
            )
    else:
        for admin_id in ADMIN_IDS:
            await safe_send(admin_id, admin_message, reply_markup=admin_notify_keyboard(app_type, app_id))
        if not is_admin(user):
            await db_set_cooldown(user.id, app_type)
        await message.answer(
            "⚡ <b>Заявка принята.</b>\n\n"
            "Скоро с тобой свяжутся. Держи связь."
        )


async def health_handler(request):
    return web.Response(
        text='{"status":"ok"}',
        content_type="application/json"
    )


async def notify_admins_if_no_interview_slots():
    """Уведомляет админов, когда свободных дат нет, без повторного спама."""
    global interview_slots_empty_notified
    if db_pool is None:
        return
    try:
        free_dates = await db_get_free_slot_dates_summary()
    except Exception as exc:
        logger.warning(f"Не удалось проверить наличие свободных дат для уведомления: {exc}")
        return

    has_free_slots = bool(free_dates)
    if has_free_slots:
        interview_slots_empty_notified = False
        return
    if interview_slots_empty_notified is True:
        return

    interview_slots_empty_notified = True
    text = (
        "🚨 <b>Нет дат для собеседований</b>\n\n"
        "Свободных слотов сейчас нет. Добавьте даты вручную "
        "в разделе «📅 Собеседования»."
    )
    for admin_id in ADMIN_IDS:
        await safe_send(admin_id, text)


async def cleanup_slots_loop():
    from datetime import timezone, timedelta as _td
    MSK = timezone(_td(hours=3))
    while True:
        now_msk = datetime.now(MSK)
        next_run = now_msk.replace(hour=6, minute=0, second=0, microsecond=0)
        if now_msk >= next_run:
            next_run += _td(days=1)
        wait_sec = (next_run - now_msk).total_seconds()
        logger.info(f"Очистка слотов запланирована через {int(wait_sec // 3600)}ч {int((wait_sec % 3600) // 60)}м (в 06:00 МСК)")
        await asyncio.sleep(wait_sec)
        try:
            async with db_pool.acquire() as conn:
                deleted = await conn.fetchval(
                    """
                    WITH del AS (
                        DELETE FROM interview_slots
                        WHERE (slot_dt AT TIME ZONE 'Europe/Moscow')::date
                              <= (NOW() AT TIME ZONE 'Europe/Moscow')::date
                        RETURNING id
                    ) SELECT COUNT(*) FROM del
                    """
                )
            logger.info(f"Очистка слотов: удалено {deleted} устаревших записей")
        except Exception as e:
            logger.error(f"Ошибка при очистке слотов: {e}")


async def reminder_loop():
    await asyncio.sleep(30)
    while True:
        try:
            slots = await db_get_slots_for_reminder()
            for slot in slots:
                try:
                    await bot.send_message(
                        slot["booked_by_user_id"],
                        f"🕯 <b>Напоминание о собеседовании</b>\n\n"
                        f"До начала осталось 2 часа.\n\n"
                        f"📅 <b>{slot['slot_text']}</b>\n\n"
                        f"Ссылку на Zoom интервьюер пришлёт за 5 минут до начала.\n"
                        f"Будь на связи.\n\n"
                        f"HR-специалисты:\n"
                        f"@hr_helper12\n"
                        f"@hr_helper13"
                    )
                except Exception as e:
                    logger.warning(f"Не удалось отправить напоминание пользователю {slot['booked_by_user_id']}: {e}")
                await db_mark_reminder_sent(slot["id"])
                uname = f"@{slot['username']}" if slot['username'] else "без username"
                name = slot['full_name'] or "Без имени"
                sep = "─" * 22
                for admin_id in ADMIN_IDS:
                    await safe_send(
                        admin_id,
                        f"⏰ <b>НАПОМИНАНИЕ ОТПРАВЛЕНО</b>\n"
                        f"{sep}\n"
                        f"👤 {name}\n"
                        f"🔗 {uname}  ·  🆔 <code>{slot['booked_by_user_id']}</code>\n"
                        f"📅 <b>{slot['slot_text']}</b>"
                    )
        except Exception as e:
            logger.error(f"Ошибка в reminder_loop: {e}")
        await asyncio.sleep(300)


async def keep_alive_loop():
    await asyncio.sleep(60)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                host = os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
                if host:
                    async with session.get(f"https://{host}/"):
                        pass
            except Exception:
                pass
            await asyncio.sleep(600)


async def _init_db(db_url: str):
    """Подключиться к БД с retry, создать таблицы."""
    global db_pool
    for attempt in range(1, 7):
        try:
            db_pool = await asyncpg.create_pool(
                db_url,
                min_size=1,
                max_size=5,
                command_timeout=60,
                max_inactive_connection_lifetime=1800,
            )
            logger.info("Пул БД создан успешно")
            break
        except Exception as e:
            logger.error(f"Попытка {attempt}/6 подключения к БД: {e}")
            if attempt == 6:
                raise
            await asyncio.sleep(5 * attempt)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS applications (
                id SERIAL PRIMARY KEY,
                app_type TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                full_name TEXT,
                username TEXT,
                app_text TEXT,
                iq_score INT,
                submitted_at TIMESTAMPTZ DEFAULT NOW(),
                replied_by TEXT,
                replied_at TIMESTAMPTZ,
                admin_confirmed BOOLEAN DEFAULT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS greeted_users (
                user_id BIGINT PRIMARY KEY
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cooldowns (
                user_id BIGINT NOT NULL,
                app_type TEXT NOT NULL,
                last_application TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (user_id, app_type)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS interview_slots (
                id SERIAL PRIMARY KEY,
                slot_text TEXT NOT NULL,
                slot_dt TIMESTAMP,
                office_id BIGINT,
                source TEXT NOT NULL DEFAULT 'manual',
                is_booked BOOLEAN DEFAULT FALSE,
                booked_by_user_id BIGINT,
                booked_app_id INT,
                reminder_sent BOOLEAN DEFAULT FALSE,
            crm_status TEXT DEFAULT 'pending',
            crm_last_error TEXT,
            crm_attempts INT NOT NULL DEFAULT 0,
            crm_next_attempt_at TIMESTAMPTZ,
            crm_sent_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS office_id BIGINT
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual'
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS crm_status TEXT DEFAULT 'pending'
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS crm_last_error TEXT
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS crm_attempts INT NOT NULL DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS crm_next_attempt_at TIMESTAMPTZ
        """)
        await conn.execute("""
            ALTER TABLE interview_slots
            ADD COLUMN IF NOT EXISTS crm_sent_at TIMESTAMPTZ
        """)
        await conn.execute("""
            ALTER TABLE applications
            ADD COLUMN IF NOT EXISTS admin_confirmed BOOLEAN DEFAULT NULL
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interview_slots_free_dt
            ON interview_slots (slot_dt) WHERE is_booked = FALSE
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interview_slots_reminder
            ON interview_slots (slot_dt)
            WHERE is_booked = TRUE AND reminder_sent = FALSE
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_applications_user_type_submitted
            ON applications (user_id, app_type, submitted_at DESC)
        """)
    logger.info("Таблицы БД готовы")


async def _webhook_background_init(db_url: str):
    """Фоновая инициализация после старта сервера (production)."""
    try:
        await _init_db(db_url)
    except Exception as e:
        logger.critical(f"КРИТИЧНО: БД недоступна после всех попыток: {e}")
        return
    try:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logger.info(f"Webhook установлен: {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Ошибка установки webhook: {e}")
    await notify_admins_if_no_interview_slots()
    asyncio.create_task(keep_alive_loop())
    asyncio.create_task(cleanup_slots_loop())
    asyncio.create_task(reminder_loop())
    asyncio.create_task(crm_retry_loop())
    logger.info(
        "HuntMe config: base=%s, api_key=%s, office_id=%s",
        HUNTME_API_BASE_URL,
        "set" if HUNTME_API_KEY else "MISSING",
        HUNTME_OPERATOR_OFFICE_ID or "MISSING",
    )
    logger.info("Фоновая инициализация завершена")


async def main():
    global db_pool
    db_url = DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL / NEON_DATABASE_URL не задан")
    logger.info(f"Подключение к БД: {db_url[:35]}...")

    if WEBHOOK_URL:
        # Production: сначала поднимаем сервер (health check), потом БД в фоне
        logger.info("Режим: Webhook (production)")
        app = web.Application(client_max_size=2 * 1024 * 1024)
        app.router.add_get("/", health_handler)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT, backlog=256)
        await site.start()
        logger.info(f"Сервер запущен на порту {PORT} — health check пройдёт")

        asyncio.create_task(_webhook_background_init(db_url))
        await asyncio.Event().wait()
    else:
        # Dev: обычный порядок с polling
        logger.info("Режим: Polling (dev)")
        await _init_db(db_url)
        await notify_admins_if_no_interview_slots()
        asyncio.create_task(crm_retry_loop())
        logger.info(
            "HuntMe config: base=%s, api_key=%s, office_id=%s",
            HUNTME_API_BASE_URL,
            "set" if HUNTME_API_KEY else "MISSING",
            HUNTME_OPERATOR_OFFICE_ID or "MISSING",
        )
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            dev_domain = os.environ.get("REPLIT_DEV_DOMAIN", "")
            if dev_domain and dev_domain in webhook_info.url:
                logger.info("Dev webhook активен — переключаюсь на polling")
                await bot.delete_webhook(drop_pending_updates=True)
                await dp.start_polling(bot)
            else:
                logger.info(f"Production webhook активен ({webhook_info.url}) — dev ожидает")
                await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
