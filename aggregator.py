# ===============================================================
# aggregator.py - Скрипт для парсинга логов и отправки отчетов
# ===============================================================
import os
import re
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import sqlite3
from datetime import datetime, timedelta
import json
import logging #

# ===============================================================
# БЛОК 1: КОНФИГУРАЦИЯ (значения подтянутся из переменных окружения на Render)
# ===============================================================
# Абсолютный путь к директории с логами на сервере Render
LOGS_DIRECTORY = os.getenv('LOGS_DIR') 
# Абсолютный путь к базе данных SQLite на сервере
DB_PATH = os.getenv('DATABASE_PATH')
# Путь для сохранения итогового CSV-файла
OUTPUT_CSV_PATH = os.getenv('OUTPUT_CSV_PATH')
# --- Путь к лог-файлу самого агрегатора ---
AGGREGATOR_LOG_PATH = os.getenv('AGGREGATOR_LOG_PATH') # 

# --- Настройки Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') # Ваш токен бота
ADMIN_USER_ID = os.getenv('ADMIN_USER_ID')          # Ваш числовой ID администратора

# --- Настройка логирования --- #
# Убедимся, что директория для лога существует
os.makedirs(os.path.dirname(AGGREGATOR_LOG_PATH), exist_ok=True)

# Создаем логгер
logger = logging.getLogger('aggregator_logger')
logger.setLevel(logging.INFO)

# Создаем обработчик, который будет записывать логи в файл
file_handler = logging.FileHandler(AGGREGATOR_LOG_PATH, encoding='utf-8')

# Создаем форматтер и добавляем его в обработчик
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

# Добавляем обработчик в логгер
logger.addHandler(file_handler)

# --- Цены на модели Gemini ---
# (цена_за_1M_input, цена_за_1M_output) в USD, paid-tier.
# Hydrogen и 3.1 Pro сверены: https://aistudio.google.com/docs/pricing (25.11.2026).
# Output оплачивается вместе с thinking. Старые модели оставлены для истории логов.
# Для остальных моделей здесь по-прежнему только базовый тариф ≤200k.
MODEL_PRICES_USD_PER_M = {
    "gemini-2.5-pro":             (1.25, 10.00),
    "gemini-2.5-flash":           (0.30,  2.50),
    "gemini-3.1-pro-preview":     (2.00, 12.00),
    "gemini-3.1-pro":             (2.00, 12.00),  # на случай если модель переименуют — алиас
    "gemini-4-hydrogen":          (1.50,  7.50),
    "gemini-3.5-flash":           (1.50,  9.00),
    "gemini-3.1-flash-lite":      (0.25,  1.50),
    "gemini-3-flash-preview":     (0.50,  3.00),
}

MODEL_CACHED_INPUT_USD_PER_M = {
    "gemini-3.1-pro-preview": 0.20,
    "gemini-3.1-pro": 0.20,
    "gemini-4-hydrogen": 0.075,
}

USAGE_LINE_RE = re.compile(
    r"\[USAGE\] stage=(\S+) model=(\S+) input=(\d+) output=(\d+) cached=(\d+) ok=([01])"
    r"(?: thinking=(\d+))?"
)


def _estimate_cost_usd(model, input_tokens, output_tokens, cached_tokens=0, thinking_tokens=0):
    """Возвращает оценку стоимости в USD или None, если модели нет в прайсе."""
    price = MODEL_PRICES_USD_PER_M.get(model)
    if price is None:
        return None
    in_price, out_price = price
    cache_price = MODEL_CACHED_INPUT_USD_PER_M.get(model, in_price)
    if model in ('gemini-3.1-pro-preview', 'gemini-3.1-pro') and input_tokens > 200_000:
        in_price, out_price, cache_price = 4.00, 18.00, 0.40
    return ((input_tokens - cached_tokens) * in_price + cached_tokens * cache_price
            + (output_tokens + thinking_tokens) * out_price) / 1_000_000


def _fmt_tokens(n):
    """Форматирует число токенов: 12_345 -> '12.3k', 1_200_000 -> '1.20M'."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(int(n))


# ===============================================================
# БЛОК 2: ФУНКЦИИ ПАРСИНГА ОДНОГО ЗАПРОСА
# ===============================================================

def parse_request_block(block_text, user_id, username):
    """Извлекает всю информацию из одного блока лога, соответствующего одному запросу."""
    
    data = {
        # Основные данные
        'telegram_id': user_id,
        'username': f"@{username}",
        'session_id': None,
        'request_date': None,
        
        # Данные по креативу
        'creative_path': None,
        'preprocessing_result': None,
        'semantic_search_top10': None,
        'final_conclusion': None,
        
        # Данные по API и моделям (ЗАПРОШЕННЫЕ)
        'total_tokens': 0,
        'prompt_tokens': 0,
        'output_tokens': 0,
        'model_used': None,
        'is_error': False,
        'error_details': None,
        'is_violation': False,

        # Данные обратной связи
        'feedback_rating': None,        # "Полезность"
        'feedback_usage': None,         # "Использование рекомендаций"
        'feedback_profile': None,       # "Профессия"
        'feedback_comment': None,       # "Комментарий"
        
        # Дополнительные технические данные
        'request_duration_sec': None,

        # Структурированные события расхода токенов из [USAGE]-строк
        # Старые строки без thinking читаются с нулём, прежний расход не восстановить из них.
        'usage_events': [],
    }

    # Извлекаем дату и время (берем первое вхождение)
    match_date = re.search(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', block_text, re.MULTILINE)
    if match_date:
        data['request_date'] = datetime.strptime(match_date.group(1), '%Y-%m-%d %H:%M:%S')
        data['session_id'] = f"{user_id}_{match_date.group(1)}" # Создаем уникальный ID сессии

    # Длительность запроса
    all_dates = re.findall(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', block_text, re.MULTILINE)
    if len(all_dates) > 1:
        start_time = datetime.strptime(all_dates[0], '%Y-%m-%d %H:%M:%S')
        end_time = datetime.strptime(all_dates[-1], '%Y-%m-%d %H:%M:%S')
        data['request_duration_sec'] = (end_time - start_time).total_seconds()

    # Путь к креативу
    match_path = re.search(r'Обработанный файл сохранен по пути: (.+?)\n', block_text)
    if match_path:
        data['creative_path'] = match_path.group(1).strip()

    # Результат предобработки
    match_preproc = re.search(r'\[ПРОМПТ 1 РЕЗУЛЬТАТ\](.*?)(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|\Z)', block_text, re.DOTALL)
    if match_preproc:
        data['preprocessing_result'] = match_preproc.group(1).strip()

    # Финальное заключение
    match_final = re.search(r'\[ФИНАЛЬНЫЙ ОТВЕТ.*?\](.*?)(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|\Z)', block_text, re.DOTALL)
    if match_final:
        data['final_conclusion'] = match_final.group(1).strip()
        
    # [ПУНКТ 1] Использованная модель
    match_model = re.search(r'\[ФИНАЛЬНЫЙ ОТВЕТ \((.*?)\)\]', block_text)
    if match_model:
        data['model_used'] = match_model.group(1).strip()

    # Семантический поиск
    match_search = re.search(r'\[SEMANTIC SEARCH\] Топ-10 релевантных дел:(.*?)(?=\d{4}-\d{2}-\d{2})', block_text, re.DOTALL)
    if match_search:
        search_results = re.findall(r'CaseID: ([\w-]+)', match_search.group(1))
        data['semantic_search_top10'] = ", ".join(search_results)

    # Токены (суммируем все) - поддержка обоих форматов: с кавычками (JSON) и без.
    # ОСТАВЛЕНО как контроль на время перехода — сверять с новым парсером [USAGE]-строк.
    token_counts = re.findall(r'"?total_token_count"?: (\d+)', block_text)
    data['total_tokens'] = sum(int(t) for t in token_counts)

    # Токены по типам (input/output)
    prompt_counts = re.findall(r'"?prompt_token_count"?: (\d+)', block_text)
    data['prompt_tokens'] = sum(int(t) for t in prompt_counts)
    output_counts = re.findall(r'"?candidates_token_count"?: (\d+)', block_text)
    data['output_tokens'] = sum(int(t) for t in output_counts)

    # Новый источник истины: [USAGE]-строки с разбивкой по стадиям и моделям.
    for m in USAGE_LINE_RE.finditer(block_text):
        stage, model, in_t, out_t, cached_t, ok, thinking_t = m.groups()
        data['usage_events'].append({
            'stage': stage,
            'model': model,
            'input': int(in_t),
            'output': int(out_t),
            'cached': int(cached_t),
            'thinking': int(thinking_t or 0),
            'ok': ok == '1',
        })

    # Обратная связь
    match_feedback = re.search(r'--- ОБРАТНАЯ СВЯЗЬ ---\n(\{.*?\})', block_text, re.DOTALL)
    if match_feedback:
        try:
            # Преобразуем python-словарь в строку JSON-формата
            feedback_str = match_feedback.group(1).replace("'", '"').replace('None', 'null')
            feedback_dict = json.loads(feedback_str)
            
            if feedback_dict.get('rating'):
                data['feedback_rating'] = int(re.search(r'\d+', feedback_dict['rating']).group())
            
            usage_map = {'usage_yes': 'Да', 'usage_no': 'Нет', 'usage_partial': 'Частично', 'usage_no_recs': 'Бот не предлагал'}
            data['feedback_usage'] = usage_map.get(feedback_dict.get('usage'))
            
            profile_map = {
                'profile_creative': 'Креативная индустрия', 'profile_lawyer': 'Юрист',
                'profile_ai': 'ИИ-энтузиаст', 'profile_citizen': 'Неравнодушный гражданин',
                'profile_other': 'Иное'
            }
            data['feedback_profile'] = profile_map.get(feedback_dict.get('profile'))
            data['feedback_comment'] = feedback_dict.get('text', 'N/A')

        except (json.JSONDecodeError, AttributeError, TypeError):
             pass # Оставляем поля пустыми, если парсинг не удался

    # [ПУНКТ 2, 3] Ошибки
    match_error = re.search(r'КРИТИЧЕСКАЯ ОШИБКА.*?: (.*?)\n', block_text, re.IGNORECASE)
    if match_error:
        data['is_error'] = True
        data['error_details'] = match_error.group(1).strip()
        
    # [ПУНКТ 4] Нарушения
    if "Нарушение безопасности" in block_text:
        data['is_violation'] = True

    return data

# ===============================================================
# БЛОК 3: ОСНОВНАЯ ЛОГИКА АГРЕГАЦИИ
# ===============================================================

def process_all_logs(log_dir):
    """Обрабатывает все log-файлы в директории и возвращает единый DataFrame."""
    all_requests_data = []
    
    logger.info(f"Начинаю обработку логов из директории: {log_dir}")
    if not os.path.isdir(log_dir):
        logger.error(f"Директория {log_dir} не найдена.")
        return pd.DataFrame()

    for filename in os.listdir(log_dir):
        if filename.endswith(".log"):
            user_id = filename.split('.')[0]
            filepath = os.path.join(log_dir, filename)
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                logger.error(f"Не удалось прочитать файл {filename}: {e}")
                continue
            
            # Разделяем весь лог на блоки по каждому новому запросу
            # Паттерн ищет дату, время и ключевую фразу начала нового запроса
            request_blocks = re.split(r'(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - --- Новый запрос от пользователя)', content)
            
            for block in request_blocks:
                if not block.strip() or '--- Новый запрос от пользователя' not in block:
                    continue

                username_match = re.search(r'--- Новый запрос от пользователя .*? \(@(.*?)\) ---', block)
                username = username_match.group(1) if username_match else "unknown"

                parsed_data = parse_request_block(block, user_id, username)
                all_requests_data.append(parsed_data)

    logger.info(f"Обработка логов завершена. Найдено {len(all_requests_data)} записей.")
    if not all_requests_data:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_requests_data)
    df['request_date'] = pd.to_datetime(df['request_date'])
    # Переименовываем колонки для лучшей читаемости
    df.rename(columns={
        'feedback_rating': 'полезность',
        'feedback_usage': 'использование_рекомендаций',
        'feedback_profile': 'профессия',
        'feedback_comment': 'комментарий'
    }, inplace=True)
    return df

# ===============================================================
# БЛОК 4: ПОДГОТОВКА И ОТПРАВКА ОТЧЕТА
# ===============================================================

def generate_summary_report(df, db_path):
    """Генерирует текстовый отчет на основе данных за сегодня."""
    
    today = datetime.now().date()
    
    df_today = df[df['request_date'].dt.date == today].copy()
    
    # 1. Пользователи сегодня
    users_today_set = set(df_today['telegram_id'].unique())
    daily_active_users = len(users_today_set)

    # 2. Новые пользователи (сравниваем с БД, т.к. в логах может не быть всей истории)
    new_users_today = 0
    try:
        with sqlite3.connect(db_path) as conn:
            # Получаем всех пользователей, чья первая дата запроса - сегодня
            # (предполагаем, что last_request_date обновляется, нужна колонка first_request_date для точности,
            # но для простоты будем считать новых по-другому)
            
            # Более надежный способ: все пользователи из логов до сегодня
            users_before_today_set = set(df[df['request_date'].dt.date < today]['telegram_id'].unique())
            new_users_today = len(users_today_set - users_before_today_set)
    except Exception as e:
        logger.error(f"Не удалось посчитать новых пользователей: {e}")
    
    # 3. Всего пользователей (из БД)
    total_users_all_time = 0
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(user_id) FROM users")
            total_users_all_time = cursor.fetchone()[0]
    except Exception as e:
        logger.error(f"Не удалось получить данные из БД: {e}")

    # 4. Запросов сегодня
    daily_requests = len(df_today)

    # 5a. Контрольный подсчёт по старому JSON-парсеру (на время перехода)
    daily_total_tokens = int(df_today['total_tokens'].sum())
    daily_prompt_tokens = int(df_today['prompt_tokens'].sum())
    daily_output_tokens = int(df_today['output_tokens'].sum())

    # 5b. Разбивка по (стадия, модель) из [USAGE]-строк
    # Учитываем только успешные вызовы (ok=True).
    stage_model_breakdown = {}
    for events in df_today['usage_events']:
        if not isinstance(events, list):
            continue
        for ev in events:
            if not ev.get('ok'):
                continue
            key = (ev['stage'], ev['model'])
            agg = stage_model_breakdown.setdefault(key, {
                'input': 0, 'output': 0, 'cached': 0, 'thinking': 0, 'cost': 0.0,
            })
            agg['input'] += ev['input']
            agg['output'] += ev['output']
            agg['cached'] += ev['cached']
            agg['thinking'] += ev.get('thinking', 0)
            # Порог 200k проверяется для отдельного вызова, а не для суммы за день.
            cost = _estimate_cost_usd(ev['model'], ev['input'], ev['output'],
                                      ev['cached'], ev.get('thinking', 0))
            if cost is None:
                agg['cost'] = None
            elif agg['cost'] is not None:
                agg['cost'] += cost

    # 7. Заполнили опросник
    feedback_users = df_today[df_today['полезность'].notna()]['telegram_id'].nunique()

    # 8. Ошибки
    errors_today = df_today[df_today['is_error'] == True]
    error_messages = [f"  - У {row['username']} ({row['telegram_id']})" for _, row in errors_today.iterrows()]
    
    # 9. Нарушения
    violations_today = df_today[df_today['is_violation'] == True]
    violation_messages = [f"  - {row['username']} ({row['telegram_id']})" for _, row in violations_today.iterrows()]
            
    # 10. Блокировки (ищем в логах)
    blocked_messages = []
    # Для этого нужна более сложная логика, анализирующая security.log или ищущая фразу в логах
    # Пока что это просто заглушка.
    # TODO: Добавить парсинг 'security.log' для точного определения блокировок.

    # --- Сборка блока «API и Расходы» ---
    api_lines = []
    total_cost_usd = 0.0
    any_unknown_price = False
    if stage_model_breakdown:
        for (stage, model), agg in sorted(stage_model_breakdown.items()):
            cost = agg['cost']
            if cost is None:
                any_unknown_price = True
                cost_str = "цена ?"
            else:
                total_cost_usd += cost
                cost_str = f"≈ ${cost:.2f}"
            api_lines.append(
                f"• {stage} ({model}): in *{_fmt_tokens(agg['input'])}* / "
                f"out *{_fmt_tokens(agg['output'])}* / thinking *{_fmt_tokens(agg['thinking'])}*  {cost_str}"
            )
        suffix = " (часть моделей без цены)" if any_unknown_price else ""
        api_lines.append(f"• Итого ≈ *${total_cost_usd:.2f}*{suffix}")
    else:
        # Фолбэк: нет ни одной [USAGE]-строки (старые логи)
        api_lines.append(f"• Потрачено токенов: *{daily_total_tokens:,}*")
        api_lines.append(f"  ↳ input: *{daily_prompt_tokens:,}* / output: *{daily_output_tokens:,}*")

    # Контроль на время перехода — суммарно по старому JSON-парсеру
    api_lines.append(
        f"_(контроль по JSON: total {daily_total_tokens:,}, "
        f"in {daily_prompt_tokens:,}, out {daily_output_tokens:,})_"
    )
    api_block = "\n".join(api_lines)

    report = f"""
*Ежедневный отчет по боту за {today.strftime('%d.%m.%Y')}*

📊 *Активность:*
• Пользователей сегодня: *{daily_active_users}*
• Из них новых: *{new_users_today}*
• Всего пользователей в базе: *{total_users_all_time}*
• Всего запросов сегодня: *{daily_requests}*

🤖 *API и Расходы:*
{api_block}

📝 *Обратная связь:*
• Заполнили опросник: *{feedback_users}* чел.

⚠️ *Инциденты:*
• Ошибки: *{len(errors_today)}*
{chr(10).join(error_messages) if error_messages else '  Ошибок не было.'}

• Нарушения правил: *{len(violations_today)}*
{chr(10).join(violation_messages) if violation_messages else '  Нарушений не было.'}

• Блокировки: *{len(blocked_messages)}*
{chr(10).join(blocked_messages) if blocked_messages else '  Блокировок за сегодня не зафиксировано.'}
"""
    # Логируем отчет с экранированием для читаемости в терминале
    escaped_report = report.replace('*', '\\*').replace('_', '\\_')
    logger.info(f"Сгенерированный отчет:\n{escaped_report}")
    return report.replace(',', ' ') # Убираем запятую из чисел для Markdown

def send_telegram_message(token, chat_id, text):
    """Отправляет сообщение в Telegram."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'Markdown'
    }
    session = requests.Session()
    retry_strategy = Retry(
        total=3,  # Общее количество повторных попыток
        status_forcelist=[429, 500, 502, 503, 504],  # Коды состояния HTTP, при которых нужно повторить
        allowed_methods=["POST"],  # Методы, для которых будет работать повтор
        backoff_factor=1  # Задержка между попытками (1с, 2с, 4с)
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    # -----------------------------

    try:
        # Используем созданную сессию вместо прямого вызова requests.post
        response = session.post(url, json=payload, timeout=20) # Увеличим таймаут до 20 секунд
        response.raise_for_status()
        logger.info("Отчет успешно отправлен в Telegram.")
    except requests.exceptions.RequestException as e:
        escaped_text = text.replace('*', '\\*').replace('_', '\\_')
        logger.error(f"Не удалось отправить отчет в Telegram после нескольких попыток: {e}\nТекст отчета:\n{escaped_text}")

# ===============================================================
# БЛОК 5: ТОЧКА ВХОДА
# ===============================================================

def run_aggregation_logic():
    """Главная функция-оркестратор, которую можно импортировать."""
    logger.info("--- Запуск задачи агрегации ---")
    output_dir = os.path.dirname(OUTPUT_CSV_PATH)
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        logger.info(f"Создана директория для аналитики: {output_dir}")

    main_df = process_all_logs(LOGS_DIRECTORY)

    if main_df.empty:
        logger.warning("Не найдено данных для обработки. Отправка отчета и сохранение CSV пропущены.")
        if TELEGRAM_BOT_TOKEN and ADMIN_USER_ID:
            send_telegram_message(TELEGRAM_BOT_TOKEN, ADMIN_USER_ID, f"Отчет за {datetime.now().date().strftime('%d.%m.%Y')}:\n\nНовых запросов за сегодня не было.")
        return

    try:
        main_df.to_csv(OUTPUT_CSV_PATH, index=False, encoding='utf-8-sig')
        logger.info(f"Данные успешно сохранены в файл: {OUTPUT_CSV_PATH}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении CSV файла: {e}")

    if TELEGRAM_BOT_TOKEN and ADMIN_USER_ID:
        report_text = generate_summary_report(main_df, DB_PATH)
        send_telegram_message(TELEGRAM_BOT_TOKEN, ADMIN_USER_ID, report_text)
    else:
        logger.warning("Токен бота или ID администратора не указаны. Отправка отчета пропущена.")
    logger.info("--- Задача агрегации завершена ---")
