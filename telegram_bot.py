# ===============================================================
# БЛОК 1: ИМПОРТЫ И НАСТРОЙКА
# ===============================================================
import os
import logging
import sqlite3
import pytz
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
import backend_logic as backend

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

# --- Настройки бота ---
DAILY_LIMIT = 5
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
CONSECUTIVE_BLOCK_LIMIT = 3
TOTAL_BLOCK_LIMIT = 5
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

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
        
Это MVP проекта, поэтому в заключениях могут быть ошибки или преувеличения. Мы работаем над развитием функционала и улучшением качества ответов. Вы можете узнать об ограничениях и их причинах подробнее здесь ⤵️"""
    )
    keyboard = [
        [InlineKeyboardButton("ℹ️ Больше об ограничениях", callback_data="learn_more")],
        [InlineKeyboardButton("✅ Соглашаюсь и хочу загрузить креатив", callback_data="agree_and_upload")],
        
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(main_text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий на основные inline-кнопки."""
    query = update.callback_query
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
    context.user_data['is_processing'] = False
    upload_text = (
        f"""-------------
Отлично! Остаток проверок на сегодня: <b>{remaining}</b>.
        
Отправьте мне:
        • Изображение в формате .jpg или .png или PDF-файл объёмом до 5 страниц. Максимальный размер файла — <b>до 10 МБ</b>.
        • Текст вашего креатива (например, слоган или текст рассылки), вставив его в строку ввода. Не добавляйте комментариев или инструкций (например, «проверь этот слоган») – <b>только сам текст</b>.
        
Вы можете отправить как что-то одно (только файл или только текст), так и файл с текстом. Пожалуйста, не загружайте контент, нарушающий нормы этики и морали – нейросеть не допустит его к проверке, а ваш доступ к боту будет заблокирован. 
        """
    )
    await query.edit_message_text(text=upload_text, parse_mode=ParseMode.HTML)

async def learn_more(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    text_part1 = (
        """ <b>Спасибо за ваш интерес к нашему проекту!</b> 
Этот бот проверяет рекламные креативы на соответствие ФЗ «О рекламе», опираясь на 700 + свежих (вынесенных за прошедшие 2,5 года) решений ФАС. Он работает по принципу Retrieval‑Augmented Generation (RAG): сначала ищет похожие кейсы, затем формирует ответ, обращаясь к нейросети Gemini 2.5 Pro.

<i>По каким критериям отбирались дела, как это было осуществлено технически, как структурирована база знаний, какие есть планы по ее дальнейшему развитию, и ДА КТО ТАКОЙ ЭТОТ ВАШ РАГ – об этом можно прочесть в <a href="https://t.me/delay_RAG">канале проекта</a>.</i> 

<b>Какие задачи решает бот:</b>
        1. проводит предварительную обработку вашего креатива: максимально подробно описывает изображения и подчищает тексты от «шумных» сведений, затрудняющих поиск по базе знаний;
        2. выявляет до <b>5</b> самых вероятных рисков нарушения ФЗ «О рекламе», на которые в своей практике в реальности обращает внимание ФАС;
        3. оценивает их по светофорной шкале «высокий — средний — низкий» и объясняет, в чем состоят риски;
        4. приводит, при наличии, кейсы из практики ФАС по рекламе, чем-то схожей с вашим креативом;
        5. даёт конкретные советы, как доработать креатив. 

Текущая версия бота — это тестовый MVP-продукт, который уже неплохо справляется с главными задачами. Но есть некоторые нюансы, над которыми мы уже работаем, чтобы приблизить заключения к ответам опытного юриста по рекламе, который хорошо представляет себе актуальную практику ФАС.

<b>Что бот не умеет:</b>
        1. отвечать на уточняющие вопросы. Любой загруженный материал и введенный текст бот рассматривает как рекламный креатив и будет подвергать его проверке на соответствие ФЗ «О рекламе».
        2. оценивать риски, относимые к каналам распространения. Самый правильный по содержанию креатив, размещенный в интернете без erid или отправленный рассылкой без согласия получателя, <s>обречен</s> может принести вам весточку от ФАС. Если у вас есть какие-либо сомнения, лучше обратиться за консультацией к юристу.
        3. оценивать вероятные размеры штрафов и перспективы оспаривания решения ФАС в суде – база знаний состоит только из решений ФАС, и только в части, касающейся квалификации наличия/отсутствия нарушений. 

<b>В чем бот может ошибаться:</b>
        1. оценка риска может оказаться несколько чрезмерной. Действительно высокорискованные моменты бот точно не пропустит, но к рискам, помеченным как «средним» и «низким» в некоторых случаях следует отнестись критично;
        2. известные и существующие похожие кейсы могут быть не упомянуты в заключении из-за технических особенностей реализации процесса retrieval-augmentation, или из-за того, что кейс пока не включен в базу знаний;
        3. иногда бот некорректно оформляет ссылки на дела на сайте ФАС или может сказать, что caseID не найден — обычно при повторной проверке креатива этот момент налаживается. Если отладка не произошла, но вам принципиально узнать, какие кейсы цитировал бот, вы можете связаться с автором проекта через <a href="https://t.me/delay_RAG">Telegram-канал</a>.
        4. иногда бот может допускать ошибки при предварительной обработке креатива (то есть при описании изображения). Если вы явно видите по приведенным цитатам, что этого не было в вашем креативе, можно попробовать отправить креатив на повторную проверку.
    
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
        2. размер загружаемого файла — 10 мб;
        3. форматы загружаемых файлов — JPG, PNG, PDF. В PDF-файле должно быть не более 5 страниц; 
        4. файлы в интерфейсе Telegram можно загружать как файлы (но тогда не получится загрузить сделанное на iPhone фото — их стандартный формат HEIC) или как изображения (тогда фото с iPhone пройдет — Telegram сам их конвертирует в нужный формат);
        5. лимит знаков загружаемых текстов соответствует установленному Telegram лимиту для 1 сообщения. 

В боте установлена защита от непристойного контента, нарушающего нормы морали и этики. 3 загрузки такого контента подряд или 5 загрузок в общей сложности влекут <b>блокировку</b> и невозможность использовать бот. Если вы уверены в том, что произошла ошибка, и контент ошибочно распознан как непристойный, вы можете связаться с автором проекта через <a href="https://t.me/delay_RAG">Telegram-канал</a>.

В целом приглашаем вас присоединиться к <a href="https://t.me/delay_RAG">каналу</a>! Он может быть интересен юристам, энтузиастам ИИ, и тем, кто интересуется low-code разработкой. Как оказалось, создание даже такого небольшого pet-проекта — весёлый и нюансированный процесс, о котором интересно рассказать. 
Мы хотели создать доступный инструмент, который сделает деятельность рекламщиков, юристов и предпринимателей более эффективной, поэтому очень ценим обратную связь, конструктивную критику и предложения о сотрудничестве."""
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
    context.user_data['is_processing'] = False
    
    upload_text = f"Остаток проверок на сегодня: <b>{remaining}</b>.\n\n Отправьте мне изображение, PDF или текст вашего креатива."

    await query.message.reply_text(text=upload_text, parse_mode=ParseMode.HTML)
    await query.answer()

async def handle_creative(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главный обработчик креативов с логикой блокировки и точными ответами."""
    user = update.message.from_user
    user_logger = setup_user_logger(user.id)

    if context.user_data.get('is_processing', False):
        user_logger.info("Запрос получен во время обработки предыдущего. Игнорируется.")
        return

    if not context.user_data.get('awaiting_creative', False):
        user_logger.warning("Попытка отправить креатив без получения разрешения (из главного меню).")
        await update.message.reply_text("Пожалуйста, нажмите на одну из кнопок, чтобы продолжить.")
        return

    context.user_data['is_processing'] = True
    context.user_data['awaiting_creative'] = False
    
    temp_file_path = None
    try:
        if is_user_blocked(user.id):
            user_logger.warning("Попытка запроса от заблокированного пользователя.")
            return

        user_logger.info(f"--- Новый запрос от пользователя {user.first_name} (@{user.username}) ---")

        can_request, remaining = check_and_update_limit(user.id)
        if not can_request:
            await update.message.reply_text("Лимит на сегодня исчерпан. Спасибо за доверие, буду рад помочь завтра!")
            user_logger.warning("Попытка запроса при исчерпанном лимите.")
            return 

        await update.message.reply_text("Креатив принят в работу, подготовка ответа может занять до 5 минут ⏳")
        
        text_content = update.message.text or update.message.caption or ""
        file_bytes, file_name = None, None
        
        if update.message.photo:
            photo = update.message.photo[-1]
            file_id = photo.file_id
            file_name = f"{user.id}_{datetime.now().timestamp()}.jpg"
            new_file = await context.bot.get_file(file_id)
            file_bytes = bytes(await new_file.download_as_bytearray())
        elif update.message.document:
            doc = update.message.document
            if doc.mime_type in ['application/pdf', 'image/jpeg', 'image/png']:
                file_id = doc.file_id
                file_name = doc.file_name
                new_file = await context.bot.get_file(file_id)
                if doc.mime_type == 'application/pdf':
                    temp_file_path = os.path.join(LOGS_DIR, file_name)
                    await new_file.download_to_drive(temp_file_path)
                else:
                    file_bytes = bytes(await new_file.download_as_bytearray())
            else:
                await update.message.reply_text("Ошибка: поддерживаются только файлы .jpg, .png и .pdf.")
                return 
        
        if not file_bytes and not text_content and not temp_file_path:
            return 

        user_logger.info("Запуск анализа бэкендом...")
        analysis_result = await backend.analyze_creative_flow(
            file_bytes=file_bytes, text_content=text_content, file_path=temp_file_path, original_filename=file_name
        )
        
        if analysis_result.get('safety_violation'):
            was_just_blocked = handle_safety_violation(user.id, user.username)
            if was_just_blocked:
                await update.message.reply_text("Ваш аккаунт был заблокирован за многократные попытки отправки недопустимого контента.")
            else:
                keyboard = [[InlineKeyboardButton("✅ Проверить еще один креатив", callback_data="check_another")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("Вы направили недопустимый запрос. Пожалуйста, убедитесь, что ваш контент соответствует правилам.", reply_markup=reply_markup)
            return

        reset_consecutive_blocks(user.id)
        
        user_logger.info(f"[ПРОМПТ 1 РЕЗУЛЬТАТ] {analysis_result.get('preprocessed_text', 'N/A')}")
        final_output = analysis_result.get('final_output', "Произошла непредвиденная ошибка при анализе.")
        user_logger.info(f"[ФИНАЛЬНЫЙ ОТВЕТ] {final_output}")

        header = "### Заключение по рекламному материалу\n\n"
        full_message = final_output

        keyboard = [
            [InlineKeyboardButton("✅ Проверить еще один креатив", callback_data="check_another")],
            [InlineKeyboardButton("✍️ Дать обратную связь", callback_data="give_feedback")],
            [InlineKeyboardButton("👩🏻‍💻 Узнать больше о проекте", url=CHANNEL_URL)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        TELEGRAM_MAX_LENGTH = 4000
        
        if len(full_message) <= TELEGRAM_MAX_LENGTH:
            await update.message.reply_text(
                full_message, 
                reply_markup=reply_markup, 
                parse_mode=ParseMode.HTML, 
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
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id, 
                        text=part, 
                        parse_mode=ParseMode.HTML, 
                        disable_web_page_preview=True
                    )
            
            if parts[-1].strip():
                await context.bot.send_message(
                    chat_id=update.effective_chat.id, 
                    text=parts[-1], 
                    reply_markup=reply_markup, 
                    parse_mode=ParseMode.HTML, 
                    disable_web_page_preview=True
                )
        
    except Exception as e:
        logger.error(f"Критическая ошибка в handle_creative для user {user.id}: {e}", exc_info=True)
        user_logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: {e}")
        await update.message.reply_text("Произошла внутренняя ошибка. Мы уже работаем над ее исправлением. Пожалуйста, попробуйте позже.")
        if ADMIN_USER_ID:
            await context.bot.send_message(ADMIN_USER_ID, f"Авария у пользователя {user.id}!\nОшибка: {e}")
    finally:
        context.user_data['is_processing'] = False
            
# ===============================================================
# БЛОК 4: ЛОГИКА ОБРАТНОЙ СВЯЗИ (CONVERSATION HANDLER)
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
        await query.edit_message_text("Спасибо! Поделитесь вашей оценкой ответа бота, это поможет нам с улучшением его ответов. Просто отправьте текст следующим сообщением.")
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
# БЛОК 5: ЗАПУСК БОТА
# ===============================================================

def main() -> None:
    """Основная функция запуска бота."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Не найден токен TELEGRAM_BOT_TOKEN! Бот не может быть запущен.")
        return

    init_db()
    backend.initialize_backend()
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
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
    application.add_handler(feedback_conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_creative))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_creative))
    application.add_handler(CallbackQueryHandler(button_handler))

    loop = asyncio.get_event_loop()
    if ADMIN_USER_ID:
        loop.run_until_complete(application.bot.send_message(ADMIN_USER_ID, "Бот успешно запущен/перезапущен!"))

    logger.info("Бот запущен и готов к работе.")
    application.run_polling()

if __name__ == '__main__':
    main()