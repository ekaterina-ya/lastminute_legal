# ===============================================================
# БЛОК 1: ИМПОРТЫ И НАСТРОЙКА
# ===============================================================
import os
import logging
import sqlite3
import pytz
import asyncio
import re
from datetime import datetime, time, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
import backend_logic as backend
import aggregator
from pypdf import PdfReader

# --- Настройка логирования ---
LOGS_DIR = os.getenv('LOGS_DIR', 'user_logs')
if not os.path.exists(LOGS_DIR):
    os.makedirs(LOGS_DIR)

# Отдельный логгер для инцидентов безопасности
security_logger = logging.getLogger('security_logger')
security_logger.setLevel(logging.WARNING)
security_handler = logging.FileHandler("security.log", encoding='utf-8')
security_formatter = logging.Formatter('%(asctime)s - %(message)s')
security_handler.setFormatter(security_formatter)
security_logger.addHandler(security_handler)

def setup_user_logger(user_id):
    """Настраивает отдельный логгер для каждого пользователя."""
    logger = logging.getLogger(str(user_id))
    logger.setLevel(logging.INFO)
    if logger.hasHandlers():
        logger.handlers.clear()
    handler = logging.FileHandler(os.path.join(LOGS_DIR, f"{user_id}.log"), encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

# Общее логирование работы бота
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Конфигурация из переменных окружения ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')
DB_PATH = os.getenv('DATABASE_PATH', 'data/user_data.db')
CHANNEL_URL = os.getenv('TELEGRAM_CHANNEL_URL')
ADMIN_CONTACT_URL = os.getenv('ADMIN_CONTACT_URL')

# --- Настройки бота ---
DAILY_LIMIT = 10
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
CONSECUTIVE_BLOCK_LIMIT = 7
TOTAL_BLOCK_LIMIT = 15
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# --- Состояния для ConversationHandler (опрос обратной связи) ---
(RATING, USAGE, PROFILE, ELABORATE, FEEDBACK_TEXT) = range(5)

# ===============================================================
# БЛОК 2: РАБОТА С БАЗОЙ ДАННЫХ (ЛИМИТЫ И БЛОКИРОВКИ)
# ===============================================================

def init_db():
    """Инициализирует базу данных SQLite с полями для лимитов и блокировок."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            requests_count INTEGER DEFAULT 0,
            last_request_date TEXT,
            consecutive_blocks INTEGER DEFAULT 0,
            total_blocks INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0
        )
    ''')
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN consecutive_blocks INTEGER DEFAULT 0')
        cursor.execute('ALTER TABLE users ADD COLUMN total_blocks INTEGER DEFAULT 0')
        cursor.execute('ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass # Колонки уже существуют
    conn.commit()
    conn.close()

def is_user_blocked(user_id: int) -> bool:
    """Проверяет, заблокирован ли пользователь."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] == 1 if result else False

def handle_safety_violation(user_id: int, username: str) -> bool:
    """
    Обрабатывает нарушение безопасности, обновляет счетчики и блокирует пользователя.
    Возвращает True, если пользователь был только что заблокирован.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT consecutive_blocks, total_blocks FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    
    if not result:
        cursor.execute("INSERT INTO users (user_id, consecutive_blocks, total_blocks, requests_count, last_request_date) VALUES (?, 1, 1, 0, ?)", 
                       (user_id, datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d')))
        conn.commit()
        conn.close()
        return False

    consecutive, total = result
    new_consecutive, new_total = consecutive + 1, total + 1
    security_logger.warning(f"Нарушение безопасности от ID: {user_id} (@{username}). Статус: {new_consecutive} подряд, {new_total} всего.")

    if new_consecutive >= CONSECUTIVE_BLOCK_LIMIT or new_total >= TOTAL_BLOCK_LIMIT:
        cursor.execute("UPDATE users SET is_blocked = 1, consecutive_blocks = ?, total_blocks = ? WHERE user_id = ?", (new_consecutive, new_total, user_id))
        security_logger.critical(f"!!! ПОЛЬЗОВАТЕЛЬ ЗАБЛОКИРОВАН !!! ID: {user_id} (@{username}). Причина: {new_consecutive} подряд / {new_total} всего.")
        if ADMIN_USER_ID:
            asyncio.create_task(Bot(TELEGRAM_BOT_TOKEN).send_message(ADMIN_USER_ID, f"Пользователь {user_id} (@{username}) заблокирован."))
        conn.commit()
        conn.close()
        return True
    else:
        cursor.execute("UPDATE users SET consecutive_blocks = ?, total_blocks = ? WHERE user_id = ?", (new_consecutive, new_total, user_id))
        conn.commit()
        conn.close()
        return False

def reset_consecutive_blocks(user_id: int):
    """Сбрасывает счетчик последовательных нарушений при успешном запросе."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET consecutive_blocks = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def check_and_update_limit(user_id: int) -> tuple[bool, int]:
    """Проверяет и списывает лимит запросов. Возвращает (доступен_ли_запрос, оставшиеся_запросы)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today_str = datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d')
    cursor.execute("SELECT requests_count, last_request_date FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    
    if result:
        count, last_date = result[0], result[1]
        if last_date == today_str:
            if count >= DAILY_LIMIT:
                conn.close()
                return False, 0
            cursor.execute("UPDATE users SET requests_count = requests_count + 1 WHERE user_id = ?", (user_id,))
            remaining = DAILY_LIMIT - (count + 1)
        else:
            cursor.execute("UPDATE users SET requests_count = 1, last_request_date = ? WHERE user_id = ?", (today_str, user_id))
            remaining = DAILY_LIMIT - 1
    else:
        cursor.execute("INSERT INTO users (user_id, requests_count, last_request_date) VALUES (?, 1, ?)", (user_id, today_str))
        remaining = DAILY_LIMIT - 1
        
    conn.commit()
    conn.close()
    return True, remaining

def get_remaining_requests(user_id: int) -> int:
    """Получает количество оставшихся запросов без изменения счетчика."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today_str = datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d')
    cursor.execute("SELECT requests_count, last_request_date FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    if result:
        count, last_date = result[0], result[1]
        if last_date == today_str:
            return DAILY_LIMIT - count
    return DAILY_LIMIT

def unblock_user_in_db(user_id: int):
    """Снимает блокировку с пользователя и сбрасывает счетчики нарушений."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Устанавливаем is_blocked=0 и обнуляем счетчики, чтобы у пользователя был чистый старт
    cursor.execute("UPDATE users SET is_blocked = 0, consecutive_blocks = 0, total_blocks = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    logger.info(f"Администратор снял блокировку с пользователя {user_id}.")
    
# ===============================================================
# БЛОК 3: КОМАНДЫ И ОСНОВНЫЕ ОБРАБОТЧИКИ
# ===============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start."""
    main_text = (
        """Привет!
        
Этот бот может проверить ваш рекламный креатив на соответствие ФЗ «О рекламе» с учетом  актуальной практики ФАС России.
        
<b>Перед началом работы важно учитывать следующее:</b>
        1. Бот не связан с Федеральной антимонопольной службой, но использует предоставляемые ею открытые данные.
        2. Если вы связаны обязательствами по соблюдению конфиденциальности, использование бота может являться их нарушением.
        3. Бот анализирует <b>исключительно содержание</b> материала. Он не учитывает фактические обстоятельства его распространения (каналы размещения, лицензирование вашей деятельности и прочее), поэтому заключение бота не является полной юридической консультацией.
        
Об ограничениях и их причинах вы можете подробнее узнать здесь ⤵️"""
    )
    keyboard = [
        [InlineKeyboardButton("ℹ️ Больше об ограничениях", callback_data="learn_more")],
        [InlineKeyboardButton("🔍 Попробуйте поиск по практике ФАС", url="https://search.delay-rag.ru")],
        [InlineKeyboardButton("✅ Соглашаюсь и хочу загрузить креатив", callback_data="agree_and_upload")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(main_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий на основные inline-кнопки."""
    query = update.callback_query
    if query.data in ("agree_and_upload", "check_another"):
        lock = context.user_data.get("analysis_lock")
        if lock and lock.locked():
            await query.answer("Идёт анализ, дождитесь результата", show_alert=False)
            return
    await query.answer()
    if query.data == "agree_and_upload":
        await agree_and_upload(query, context)
    elif query.data == "learn_more":
        await learn_more(query, context)
    elif query.data == "check_another":
        await check_another(query, context)

async def agree_and_upload(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь согласился, просим загрузить креатив."""
    user_id = query.from_user.id
    if is_user_blocked(user_id): return
    remaining = get_remaining_requests(user_id)
    if remaining <= 0:
        await query.message.reply_text("Лимит на сегодня исчерпан. Спасибо за доверие, буду рад помочь завтра!")
        return

    context.user_data['awaiting_creative'] = True
    upload_text = (
        f"""-------------
Отлично! Остаток проверок на сегодня: <b>{remaining}</b>.
        
Отправьте мне:
        • Изображение в формате .jpg или .png или PDF-файл объёмом до 5 страниц. Максимальный размер файла — <b>до 20 МБ</b>.
        • Текст вашего креатива (например, слоган или текст рассылки), вставив его в строку ввода. Не добавляйте комментариев или инструкций (например, «проверь этот слоган») – <b>только сам текст</b>.
        
Вы можете отправить как что-то одно (только файл или только текст), так и файл с текстом. Пожалуйста, не загружайте контент, нарушающий нормы этики и морали – нейросеть не допустит его к проверке, а ваш доступ к боту будет заблокирован. 

Если вас интересует подбор релевантной практики ФАС под ваши креативы или возникающие в работе вопросы, то вы также можете воспользоваться <b><a href="https://search.delay-rag.ru">сервисом</a></b>, предназначенным специально для удобного поиска по решениям ФАС.
        """
    )
    await query.edit_message_text(text=upload_text, parse_mode=ParseMode.HTML)

async def learn_more(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = query.from_user
    user_logger = setup_user_logger(user.id)
    user_logger.info(f"Пользователь {user.id} нажал кнопку 'Подробнее о правилах'.")

    text_part1 = (
        """ <b>Спасибо за ваш интерес к нашему проекту!</b> 
Этот бот проверяет рекламные креативы на соответствие ФЗ «О рекламе», опираясь на почти 2 тысячи решений ФАС, вынесенных за прошедшие 6 лет. Он работает по принципу Retrieval‑Augmented Generation (RAG): сначала ищет похожие кейсы, затем формирует ответ, обращаясь к нейросети Gemini 3.1 Pro (или Gemini 2.5 Pro, если 3.1 вдруг срабатывает с ошибками. Это более старая модель, но бот больше полугода работал, используя её, и показывая высокое качество).

<i>По каким критериям отбирались дела, как это было осуществлено технически, как структурирована база знаний, какие есть планы по ее дальнейшему развитию, и ДА КТО ТАКОЙ ЭТОТ ВАШ РАГ – об этом можно прочесть в <a href="https://t.me/delay_RAG">канале проекта</a>.</i> 

Если вас интересует подбор релевантной практики ФАС под ваши креативы или возникающие в работе вопросы, то вы можете воспользоваться <b><a href="https://search.delay-rag.ru">сервисом</a></b>, предназначенным специально для удобного поиска по решениям ФАС.

<b>Какие задачи решает бот:</b>
        1. Проводит предварительную обработку вашего креатива: максимально подробно описывает изображения и подчищает тексты от «шумных» сведений, затрудняющих поиск по базе знаний;
        2. Выявляет до <b>5</b> самых вероятных рисков нарушения ФЗ «О рекламе», на которые в своей практике в реальности обращает внимание ФАС;
        3. Оценивает их по светофорной шкале «высокий — средний — низкий» и объясняет, в чем состоят риски;
        4. Приводит, при наличии, кейсы из практики ФАС по рекламе, чем-то схожей с вашим креативом;
        5. Даёт конкретные советы, как доработать креатив. 

<b>Что бот не умеет:</b>
        1. Отвечать на уточняющие вопросы. Любой загруженный материал и введенный текст бот рассматривает как рекламный креатив и будет подвергать его проверке на соответствие ФЗ «О рекламе».
        2. Оценивать риски, относимые к каналам распространения. Самый правильный по содержанию креатив, размещенный в интернете без erid или отправленный рассылкой без согласия получателя, <s>обречен</s> может принести вам весточку от ФАС. Если у вас есть какие-либо сомнения, лучше обратиться за консультацией к юристу.
        3. Оценивать вероятные размеры штрафов и перспективы оспаривания решения ФАС в суде – база знаний состоит только из решений ФАС, и только в части, касающейся квалификации наличия/отсутствия нарушений. 

<b>В чем бот может ошибаться:</b>
        1. Оценка риска может оказаться несколько чрезмерной. Действительно высокорискованные моменты бот точно не пропустит, но к рискам, помеченным как «средним» и «низким» в некоторых случаях следует отнестись критично;
        2. Известные и существующие похожие кейсы могут быть не упомянуты в заключении из-за технических особенностей реализации процесса retrieval-augmentation, или из-за того, что кейс пока не включен в базу знаний;
        3. Иногда бот некорректно оформляет ссылки на дела на сайте ФАС или может сказать, что caseID не найден — обычно при повторной проверке креатива этот момент налаживается. Если отладка не произошла, но вам принципиально узнать, какие кейсы цитировал бот, вы можете связаться с автором проекта через <a href="https://t.me/delay_RAG">Telegram-канал</a> или воспользоваться <a href="https://search.delay-rag.ru">сервисом поиска практики ФАС</a>.
        4. Иногда бот может допускать ошибки при предварительной обработке креатива (то есть при описании изображения). Если вы явно видите по приведенным цитатам, что этого не было в вашем креативе, можно попробовать отправить креатив на повторную проверку.
    
    """ 
    )

    text_part2 = (
        """<b>О конфиденциальности:</b> поскольку автор проекта является юристом, не могу не предупредить :) 
Поскольку проект полностью некоммерческий (скорее имеет исследовательско-экспериментальный характер), а его развитие требует накопления и разметки данных (это основа улучшения работы любых ИИ-продуктов), то условной «платой» за использование бота является то, что мы сохраняем на своем сервере загруженные пользователями материалы и в дальнейшем анализируем по ним ответы нейросети. Это помогает оценить точность данных нейросетью ответов и улучшать промпты и логику работы бота. Авторы проекта не намереваются использовать загруженные материалы каким-либо иным образом: ни передавать их кому-либо, ни тем более публиковать самостоятельно. 
Но даже такой подход может быть формальным нарушением ваших обязательств о соблюдении режима конфиденциальности если, например, вы дизайнер, работающий по заказу предпринимателя, и в вашем договоре есть такие условия. 
Кроме того, креатив передается для предобработки «в Google», точнее в нейросеть — но риск утечек инпутов из Google, который мог бы каким-либо образом навредить малому бизнесу в России, мы предлагаем считать крайне низким.
Поэтому для полной правомерности использования бота мы рекомендуем пользователям-исполнителям по каким-либо договорам, предусматривающим конфиденциальность креативов, предварительно <b>согласовывать с заказчиком</b> возможность использования бота. 

И напоследок немного о <b>пользовательских ограничениях</b>. На данный момент действуют следующие лимиты:
        1. 10 запросов в день (в 24 часа) — счетчик обнуляется в 00:00 по Москве;
        2. Размер загружаемого файла — 20 МБ;
        3. Форматы загружаемых файлов — JPG, PNG, PDF. В PDF-файле должно быть не более 5 страниц; 
        4. Файлы в интерфейсе Telegram можно загружать как файлы (но тогда не получится загрузить сделанное на iPhone фото — их стандартный формат HEIC) или как изображения (тогда фото с iPhone пройдет — Telegram сам их конвертирует в нужный формат);
        5. Лимит знаков загружаемых текстов соответствует установленному Telegram лимиту для 1 сообщения. 

В боте установлена защита от непристойного контента, нарушающего нормы морали и этики. 7 загрузок такого контента подряд или 15 загрузок в общей сложности влекут <b>блокировку</b> и невозможность использовать бот. Нейросеть может ошибаться на этапе фильтрации и быть слишком строга, мы рекомендуем попробовать загрузить тот же креатив еще раз позднее. В случае блокировки у вас всегда будет возможность связаться с автором канала, если вы считаете, что она необоснована.

Мы хотели создать доступный инструмент, который сделает деятельность рекламщиков, юристов и предпринимателей более эффективной, поэтому очень ценим обратную связь, конструктивную критику и предложения о сотрудничестве.
И присоединяйтесь к <a href="https://t.me/delay_RAG">каналу</a> автора бота! В нем много о RAG-технологии, вайб-кодинге и продвинутом использовании нейросетей в работе юриста."""
    )

    keyboard = [[InlineKeyboardButton("✅ Понятно, хочу загрузить креатив", callback_data="agree_and_upload")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=text_part1,
        parse_mode=ParseMode.HTML,
        reply_markup=None
    )

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text_part2,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

async def check_another(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопки "проверить еще". Теперь он явно приглашает к загрузке."""
    user_id = query.from_user.id
    remaining = get_remaining_requests(user_id)
    
    if remaining <= 0:
        await query.message.reply_text("Лимит на сегодня исчерпан. Спасибо за доверие, буду рад помочь завтра!")
        return

    context.user_data['awaiting_creative'] = True

    upload_text = f"Остаток проверок на сегодня: <b>{remaining}</b>.\n\n Отправьте мне изображение, PDF или текст вашего креатива."

    await query.message.reply_text(text=upload_text, parse_mode=ParseMode.HTML)
    await query.answer()



async def handle_creative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный обработчик креативов с новой логикой обработки ошибок."""
    user = update.message.from_user
    user_logger = setup_user_logger(user.id)

    lock = context.user_data.setdefault("analysis_lock", asyncio.Lock())
    if lock.locked():
        user_logger.info("Запрос получен во время обработки предыдущего. Игнорируется.")
        await update.message.reply_text("Ваш предыдущий креатив ещё анализируется, дождитесь результата.")
        return

    if not context.user_data.get('awaiting_creative', False):
        user_logger.warning("Попытка отправить креатив без получения разрешения (из главного меню).")
        await update.message.reply_text("Пожалуйста, нажмите на одну из кнопок, чтобы продолжить.")
        return

    # Предварительная проверка лимита, без списания
    if get_remaining_requests(user.id) <= 0:
        await update.message.reply_text("Лимит на сегодня исчерпан. Спасибо за доверие, буду рад помочь завтра!")
        user_logger.warning("Попытка запроса при исчерпанном лимите.")
        return

    async with lock:
        context.user_data['awaiting_creative'] = False

        error_text = f"Проверьте ваш файл: поддерживаются только файлы формата .jpg, .png и .pdf. не более {MAX_FILE_SIZE_MB} МБ."
        error_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Попробовать еще раз", callback_data="check_another")]
        ])

        temp_file_path = None
        try:
            if is_user_blocked(user.id):
                user_logger.warning("Попытка запроса от заблокированного пользователя.")
                return

            user_logger.info(f"--- Новый запрос от пользователя {user.first_name} (@{user.username}) ---")

            await update.message.reply_text("Креатив принят в работу, подготовка ответа может занять до 5 минут ⏳")

            text_content = update.message.text or update.message.caption or ""
            file_bytes, file_name = None, None

            if update.message.photo:
                photo = update.message.photo[-1]
                if photo.file_size > MAX_FILE_SIZE_BYTES:
                    await update.message.reply_text(error_text, reply_markup=error_keyboard)
                    return

                file_id = photo.file_id
                file_name = f"{user.id}_{datetime.now().timestamp()}.jpg"
                new_file = await context.bot.get_file(file_id)
                file_bytes = bytes(await new_file.download_as_bytearray())

            elif update.message.document:
                doc = update.message.document

                if doc.file_size > MAX_FILE_SIZE_BYTES or doc.mime_type not in ['application/pdf', 'image/jpeg', 'image/png']:
                    await update.message.reply_text(error_text, reply_markup=error_keyboard)
                    return

                file_id = doc.file_id
                file_name = doc.file_name
                new_file = await context.bot.get_file(file_id)
                if doc.mime_type == 'application/pdf':
                    temp_file_path = os.path.join(LOGS_DIR, f"{user.id}_{int(datetime.now().timestamp())}.pdf")
                    await new_file.download_to_drive(temp_file_path)

                    try:
                        reader = PdfReader(temp_file_path)
                        if len(reader.pages) > 5:
                            await update.message.reply_text(
                                "⚠️ В вашем PDF-файле больше 5 страниц.\n"
                                "Пожалуйста, загрузите документ объемом до 5 страниц или отправьте его по частям в нескольких запросах к боту.",
                                reply_markup=error_keyboard
                            )
                            os.remove(temp_file_path)
                            return
                    except Exception as e:
                        user_logger.warning(f"Ошибка при чтении PDF: {e}")
                        await update.message.reply_text(
                            "⚠️ Не удалось прочитать PDF-файл. Возможно, он поврежден или зашифрован.",
                            reply_markup=error_keyboard
                        )
                        if os.path.exists(temp_file_path):
                            os.remove(temp_file_path)
                        return
                else:
                    file_bytes = bytes(await new_file.download_as_bytearray())

            if not file_bytes and not text_content and not temp_file_path:
                return

            # --- ШАГ 1: Первая попытка с основной моделью ---
            analysis_result = await backend.analyze_creative_flow(
                file_bytes=file_bytes, text_content=text_content, file_path=temp_file_path,
                original_filename=file_name, user_id=user.id, user_logger=user_logger, model_to_use='primary'
            )

            error_type = analysis_result.get("error_type")

            # --- ШАГ 2: Если техническая ошибка, пробуем fallback-модель ---
            if error_type == "technical":
                primary_error_message = analysis_result.get("message", "Неизвестная ошибка")
                logger.error(f"Техническая ошибка (primary) для user {user.id}: {primary_error_message}")
                if ADMIN_USER_ID:
                    await context.bot.send_message(ADMIN_USER_ID, f"Авария в бэкенде (Gemini 3.1 Pro) у пользователя {user.id} (@{user.username})!\nОшибка: {primary_error_message}\n\nЗапускаю fallback (Gemini 2.5 Pro)...")

                await update.message.reply_text(
                    "Приносим извинения, нейросеть Gemini 3.1 Pro не сработала из-за проблем на стороне Google. Мы подготовим заключение с нейросетью Gemini 2.5 Pro. Gemini 3.1 Pro скорее всего скоро починят, можете попробовать еще раз позднее."
                )
                analysis_result = await backend.analyze_creative_flow(
                    file_bytes=file_bytes, text_content=text_content, file_path=temp_file_path,
                    original_filename=file_name, user_id=user.id, user_logger=user_logger, model_to_use='fallback'
                )
                error_type = analysis_result.get("error_type")

            # --- ШАГ 3: Финальная обработка результата (от первой или второй попытки) ---
            if error_type == "safety":
                was_just_blocked = handle_safety_violation(user.id, user.username)
                if was_just_blocked:
                    block_text = "Ваш доступ к боту был заблокирован. Нейросеть может ошибаться в своей оценке загруженного вами контента. Если вы считаете, что произошла ошибка, свяжитесь с администратором."
                    keyboard = []
                    if ADMIN_CONTACT_URL:
                        keyboard.append([InlineKeyboardButton("Связаться с администратором", url=ADMIN_CONTACT_URL)])
                    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
                    await update.message.reply_text(block_text, reply_markup=reply_markup)
                else:
                    warning_text = "Нейросеть считает, что вы направили недопустимый запрос. Она может ошибаться и при повторном рассмотрении предоставить заключение. Попробуйте еще раз позднее"
                    keyboard = [
                        [InlineKeyboardButton("✅ Проверить креатив ещё раз", callback_data="check_another")],
                        [InlineKeyboardButton("🔍 Попробуйте поиск по практике ФАС", url="https://search.delay-rag.ru")],
                        [InlineKeyboardButton("👩🏻‍💻 Узнать больше о проекте", url=CHANNEL_URL)]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await update.message.reply_text(warning_text, reply_markup=reply_markup)

            elif error_type == "technical":
                final_error_message = analysis_result.get("message", "Неизвестная ошибка")
                logger.critical(f"ОБЕ МОДЕЛИ НЕ СРАБОТАЛИ для user {user.id}: {final_error_message}")
                if ADMIN_USER_ID:
                    await context.bot.send_message(ADMIN_USER_ID, f"КРИТИЧЕСКАЯ АВАРИЯ! Fallback-модель тоже не сработала у пользователя {user.id} (@{user.username})!\nОшибка: {final_error_message}")

                keyboard = [
                    [InlineKeyboardButton("✅ Попробовать ещё раз", callback_data="check_another")],
                    [InlineKeyboardButton("🔍 Попробуйте поиск по практике ФАС", url="https://search.delay-rag.ru")],
                    [InlineKeyboardButton("👩🏻‍💻 Узнать больше о проекте", url=CHANNEL_URL)]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "Приносим извинения, произошел серьезный сбой на стороне Google. Пожалуйста, попробуйте загрузить креатив позднее.",
                    reply_markup=reply_markup
                )

            else:
                # Успешное выполнение
                check_and_update_limit(user.id)
                reset_consecutive_blocks(user.id)

                model_used = analysis_result.get("model_used", "unknown")
                if model_used == 'fallback' and ADMIN_USER_ID:
                    await context.bot.send_message(ADMIN_USER_ID, f"✅ Заключение для пользователя {user.id} (@{user.username}) успешно подготовлено с помощью Gemini 2.5 Pro (fallback) после сбоя Gemini 3.1 Pro.")

                final_output = analysis_result.get('final_output', "Произошла внутренняя ошибка.")
                user_logger.info(f"[ФИНАЛЬНЫЙ ОТВЕТ ({model_used})]: {final_output}")
                full_message = final_output

                keyboard = [
                    [InlineKeyboardButton("✅ Проверить еще один креатив", callback_data="check_another")],
                    [InlineKeyboardButton("✍️ Дать обратную связь", callback_data="give_feedback")],
                    [InlineKeyboardButton("🔍 Попробуйте поиск по практике ФАС", url="https://search.delay-rag.ru")],
                    [InlineKeyboardButton("👩🏻‍💻 Узнать больше о проекте", url=CHANNEL_URL)]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                TELEGRAM_MAX_LENGTH = 4000

                if len(full_message) <= TELEGRAM_MAX_LENGTH:
                    try:
                        await update.message.reply_text(
                            full_message,
                            reply_markup=reply_markup,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True
                        )
                    except Exception as html_err:
                        logger.warning(f"HTML parse error, sending without formatting: {html_err}")
                        plain_text = re.sub(r'<[^>]+>', '', full_message)
                        await update.message.reply_text(
                            plain_text,
                            reply_markup=reply_markup,
                            disable_web_page_preview=True
                        )
                else:
                    parts = []
                    current_part = ""
                    for line in full_message.splitlines(True):
                        if len(current_part) + len(line) > TELEGRAM_MAX_LENGTH:
                            parts.append(current_part)
                            current_part = line
                        else:
                            current_part += line
                    parts.append(current_part)

                    for part in parts[:-1]:
                        if part.strip():
                            try:
                                await context.bot.send_message(
                                    chat_id=update.effective_chat.id,
                                    text=part,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True
                                )
                            except Exception as html_err:
                                logger.warning(f"HTML parse error in message part, sending without formatting: {html_err}")
                                plain_part = re.sub(r'<[^>]+>', '', part)
                                await context.bot.send_message(
                                    chat_id=update.effective_chat.id,
                                    text=plain_part,
                                    disable_web_page_preview=True
                                )

                    if parts[-1].strip():
                        try:
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=parts[-1],
                                reply_markup=reply_markup,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True
                            )
                        except Exception as html_err:
                            logger.warning(f"HTML parse error in last message part, sending without formatting: {html_err}")
                            plain_last = re.sub(r'<[^>]+>', '', parts[-1])
                            await context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=plain_last,
                                reply_markup=reply_markup,
                                disable_web_page_preview=True
                            )

        except Exception as e:
            logger.error(f"Критическая ошибка в handle_creative для user {user.id}: {e}", exc_info=True)
            user_logger.error(f"КРИТИЧЕСКАЯ ОШИБКА В handle_creative: {e}", exc_info=True)
            await update.message.reply_text("Приносим извинения, произошла внутренняя ошибка. Пожалуйста, попробуйте позже: для перезапуска бота введите команду /start")
            if ADMIN_USER_ID:
                await context.bot.send_message(ADMIN_USER_ID, f"Авария у пользователя {user.id} (@{user.username})!\nОшибка: {e}")

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /unblock, доступный только администратору."""
    admin_id = os.getenv('ADMIN_USER_ID')
    user_id_to_check = str(update.effective_user.id)

    # Проверка: команду может использовать только администратор
    if not admin_id or user_id_to_check != admin_id:
        logger.warning(f"Попытка несанкционированного доступа к команде /unblock от пользователя {user_id_to_check}")
        return

    # Проверяем, что команда передана с аргументом (ID пользователя)
    if not context.args:
        await update.message.reply_text("Ошибка: укажите ID пользователя для разблокировки.\nПример: `/unblock 123456789`")
        return

    try:
        user_id_to_unblock = int(context.args[0])
        unblock_user_in_db(user_id_to_unblock)
        await update.message.reply_text(f"✅ Пользователь с ID {user_id_to_unblock} успешно разблокирован.")
    except (ValueError, IndexError):
        await update.message.reply_text("Ошибка: ID пользователя должен быть числом.")
    except Exception as e:
        await update.message.reply_text(f"Произошла ошибка при разблокировке: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик необработанных исключений в хэндлерах."""
    logger.error("Необработанное исключение при обработке апдейта", exc_info=context.error)
    if ADMIN_USER_ID:
        try:
            await context.bot.send_message(
                ADMIN_USER_ID,
                f"⚠️ Необработанное исключение в боте:\n<code>{context.error}</code>",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

# ===============================================================
# БЛОК 4: ПЛАНИРОВЩИК
# ===============================================================
async def run_daily_scheduler():
    """Асинхронный планировщик, который запускает агрегатор раз в сутки."""
    MOSCOW_TZ = pytz.timezone('Europe/Moscow')
    SCHEDULE_TIME = time(20, 00) # 20:00 по Москве

    print("Планировщик отчетов запущен.")
    while True:
        try:
            now_moscow = datetime.now(MOSCOW_TZ)
            
            # Определяем следующую дату запуска
            target_datetime = now_moscow.replace(hour=SCHEDULE_TIME.hour, minute=SCHEDULE_TIME.minute, second=0, microsecond=0)
            if now_moscow > target_datetime:
                # Если 20:00 сегодня уже прошло, планируем на завтра
                target_datetime += timedelta(days=1)
            
            sleep_seconds = (target_datetime - now_moscow).total_seconds()
            
            print(f"Следующий запуск агрегатора запланирован на {target_datetime.strftime('%Y-%m-%d %H:%M:%S')}. Сон на {int(sleep_seconds)} секунд.")
            await asyncio.sleep(sleep_seconds)

            # Время пришло! Запускаем агрегатор.
            print("Время пришло! Запускаю агрегацию логов...")
            aggregator.run_aggregation_logic()
            print("Агрегация логов завершена.")
            
            # Небольшая пауза, чтобы избежать повторного запуска в ту же секунду
            await asyncio.sleep(60)

        except Exception as e:
            print(f"Критическая ошибка в планировщике: {e}")
            # Ждем 5 минут перед повторной попыткой, чтобы не спамить лог ошибками
            await asyncio.sleep(300)

# ===============================================================
# БЛОК 5: ЛОГИКА ОБРАТНОЙ СВЯЗИ (CONVERSATION HANDLER)
# ===============================================================
async def give_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Начинает опрос обратной связи, отправляя вопросы в новом сообщении.
    У исходного сообщения с заключением убираются кнопки.
    """
    query = update.callback_query
    await query.answer()

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception as e:
        logger.warning(f"Не удалось убрать клавиатуру у сообщения с заключением: {e}")

    context.user_data['awaiting_creative'] = False

    text = "Спасибо за вашу готовность помочь! Ваша обратная связь поможет развитию проекта.\n\n<b>Вопрос 1/4:</b> Оцените, насколько вы согласны с оценкой рисков, предложенной ботом?"
    keyboard = [[
        InlineKeyboardButton("1", callback_data="rate_1"),
        InlineKeyboardButton("2", callback_data="rate_2"),
        InlineKeyboardButton("3", callback_data="rate_3"),
        InlineKeyboardButton("4", callback_data="rate_4"),
        InlineKeyboardButton("5", callback_data="rate_5"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.message.reply_text(
        text=text, 
        reply_markup=reply_markup, 
        parse_mode=ParseMode.HTML
    )
    
    return RATING


async def rating_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2: оценка."""
    query = update.callback_query
    await query.answer()
    context.user_data['feedback_rating'] = query.data

    text = "<b>Вопрос 2/4:</b> Вы воспользуетесь рекомендациями бота?"
    keyboard = [
        [InlineKeyboardButton("Да", callback_data="usage_yes")],
        [InlineKeyboardButton("Нет", callback_data="usage_no")],
        [InlineKeyboardButton("Частично", callback_data="usage_partial")],
        [InlineKeyboardButton("Бот не предлагал исправлений", callback_data="usage_no_recs")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=text, reply_markup=reply_markup, 
        parse_mode=ParseMode.HTML)
    return USAGE

async def usage_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3: профиль."""
    query = update.callback_query
    await query.answer()
    context.user_data['feedback_usage'] = query.data

    text = "<b>Вопрос 3/4:</b> Расскажите немного о себе. Вы..."
    keyboard = [
        [InlineKeyboardButton("из креативной индустрии", callback_data="profile_creative")],
        [InlineKeyboardButton("юрист", callback_data="profile_lawyer")],
        [InlineKeyboardButton("ИИ-энтузиаст", callback_data="profile_ai")],
        [InlineKeyboardButton("неравнодушный гражданин", callback_data="profile_citizen")],
        [InlineKeyboardButton("иное", callback_data="profile_other")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=text, reply_markup=reply_markup, 
        parse_mode=ParseMode.HTML)
    return PROFILE

async def profile_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 4: желание помочь."""
    query = update.callback_query
    await query.answer()
    context.user_data['feedback_profile'] = query.data

    text = "<b>Вопрос 4/4:</b> Я хочу помочь развитию проекта и подробнее рассказать, в чем я согласен или не согласен с ответом бота."
    keyboard = [
        [InlineKeyboardButton("Да, хочу рассказать", callback_data="elaborate_yes")],
        [InlineKeyboardButton("Нет", callback_data="elaborate_no")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=text, reply_markup=reply_markup, 
        parse_mode=ParseMode.HTML)
    return ELABORATE

async def elaborate_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 5: обработка желания помочь."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "elaborate_yes":
        await query.edit_message_text("Спасибо! Поделитесь вашей оценкой заключения бота, это поможет нам с улучшением качества ответов. Просто отправьте текст следующим сообщением.")
        return FEEDBACK_TEXT
    else:
        await query.edit_message_text("Спасибо за ваши ответы! Они помогут боту стать лучше.")
        await post_feedback_menu(query.message, context)
        return ConversationHandler.END

async def feedback_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает текстовую обратную связь."""
    context.user_data['feedback_text'] = update.message.text
    await update.message.reply_text("Ваш подробный отзыв сохранен. Огромное спасибо за помощь!")
    await post_feedback_menu(update.message, context)
    return ConversationHandler.END
    
async def post_feedback_menu(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает меню после завершения опроса и логирует результат."""
    user = message.chat
    user_logger = setup_user_logger(user.id)
    
    feedback_data = {
        'rating': context.user_data.get('feedback_rating'),
        'usage': context.user_data.get('feedback_usage'),
        'profile': context.user_data.get('feedback_profile'),
        'text': context.user_data.get('feedback_text', 'N/A')
    }
    user_logger.info(f"--- ОБРАТНАЯ СВЯЗЬ ---\n{feedback_data}")

    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("✅ Проверить креатив", callback_data="check_another")],
        [InlineKeyboardButton("ℹ️ Узнать об ограничениях", callback_data="learn_more")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.reply_text("Что вы хотите сделать дальше?", reply_markup=reply_markup)

async def cancel_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет опрос."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Опрос отменен.")
    await post_feedback_menu(query.message, context)
    return ConversationHandler.END

async def handle_unexpected_text_in_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сообщает пользователю, что нужно использовать кнопки во время опроса."""
    await update.message.reply_text(
        "Пожалуйста, используйте кнопки для ответа на вопрос. "
        "Если вы хотите прервать опрос и вернуться в главное меню, отправьте команду /start."
    )
# ===============================================================
# БЛОК 6: ЗАПУСК БОТА
# ===============================================================

async def post_init(application: Application) -> None:
    """Запускается после инициализации application: планировщик и уведомление админа."""
    application.create_task(run_daily_scheduler())
    if ADMIN_USER_ID:
        await application.bot.send_message(ADMIN_USER_ID, "Бот успешно запущен/перезапущен!")


def build_application() -> Application:
    """Фабричная функция для сборки Application."""
    return (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )


def main() -> None:
    """Основная функция запуска бота."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Не найден токен TELEGRAM_BOT_TOKEN! Бот не может быть запущен.")
        return

    init_db()
    backend.initialize_backend(LOGS_DIR)

    application = build_application()

    feedback_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(give_feedback, pattern='^give_feedback$')],
        states={            RATING: [
                CallbackQueryHandler(rating_step, pattern='^rate_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unexpected_text_in_feedback)
            ],
            USAGE: [
                CallbackQueryHandler(usage_step, pattern='^usage_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unexpected_text_in_feedback)
            ],
            PROFILE: [
                CallbackQueryHandler(profile_step, pattern='^profile_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unexpected_text_in_feedback)
            ],
            ELABORATE: [
                CallbackQueryHandler(elaborate_step, pattern='^elaborate_'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unexpected_text_in_feedback)
            ],
            FEEDBACK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_text_step)],
        },
        fallbacks=[CommandHandler('start', cancel_feedback)],
        per_user=True 
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("unblock", unblock_command))
    application.add_handler(feedback_conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_creative))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_creative))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    logger.info("Бот запущен и готов к работе.")
    application.run_polling()

if __name__ == '__main__':
    main()
