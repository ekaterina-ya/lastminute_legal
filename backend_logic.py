# ===============================================================
# БЛОК 1: ИМПОРТЫ И КОНФИГУРАЦИЯ
# ===============================================================
import os
import pandas as pd
import numpy as np
from google import genai
from google.genai import types
import re
from PIL import Image
import io
import logging
import json

# --- Глобальные переменные для кеширования ---
rag_df = None
corpus_embeddings = None
doc_to_case_map = None
gemini_client = None  # Единый клиент для работы с Gemini API
PROMPT_1_PREPROCESSING = None
PROMPT_2_ANALYSIS = None
RAG_TOP_N = 10
USER_FILES_DIR = None
FILE_COUNTER_PATH = None

def load_env_variable(var_name, is_int=False, default=None):
    """Загружает переменную окружения."""
    value = os.getenv(var_name, default)
    if value is None:
        raise ValueError(f"КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения '{var_name}' не установлена.")
    return int(value) if is_int else value

def load_prompt_from_file(file_path):
    """Загружает текст промпта из файла."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        raise ValueError(f"КРИТИЧЕСКАЯ ОШИБКА: Файл с промптом не найден по пути '{file_path}'.")


# ===============================================================
# БЛОК 2: GEMINI CLIENT (обёртка над API)
# ===============================================================

class GeminiClient:
    """
    Обёртка над google.generativeai для работы с Gemini.
    При смене модели (на другую LLM) — менять только этот класс.
    """
    
    SAFETY_SETTINGS = [
        types.SafetySetting(category='HARM_CATEGORY_HARASSMENT', threshold='BLOCK_NONE'),
        types.SafetySetting(category='HARM_CATEGORY_HATE_SPEECH', threshold='BLOCK_NONE'),
        types.SafetySetting(category='HARM_CATEGORY_SEXUALLY_EXPLICIT', threshold='BLOCK_NONE'),
        types.SafetySetting(category='HARM_CATEGORY_DANGEROUS_CONTENT', threshold='BLOCK_NONE'),
    ]
    
    def __init__(self, api_key: str, primary_model: str, fallback_model: str, embedding_model: str):
        self._client = genai.Client(api_key=api_key)
        self._embedding_model_name = embedding_model
        self._primary_model_name = primary_model
        self._fallback_model_name = fallback_model
    
    def generate(self, content: list, use_fallback: bool = False, user_logger: logging.Logger = None) -> dict:
        """
        Генерация ответа от LLM с парсингом.
        Returns: dict: {status, text/message, model}
        """
        model_name = self._fallback_model_name if use_fallback else self._primary_model_name
        
        try:
            response = self._client.models.generate_content(
                model=model_name,
                contents=content,
                config=types.GenerateContentConfig(safety_settings=self.SAFETY_SETTINGS)
            )
            
            status, result_text = self._parse_response(response, model_name, user_logger)
            
            if status == 'SUCCESS':
                return {"status": "SUCCESS", "text": result_text, "model": model_name}
            elif status == 'SAFETY':
                return {"status": "SAFETY", "message": result_text, "model": model_name}
            else:
                return {"status": "ERROR", "message": result_text, "model": model_name}
                
        except Exception as e:
            error_msg = f"Исключение при вызове API ({model_name}): {str(e)}"
            if user_logger:
                user_logger.error(error_msg)
            return {"status": "ERROR", "message": error_msg, "model": model_name}
    
    def embed(self, text: str) -> np.ndarray:
        """Создание эмбеддинга для текста."""
        result = self._client.models.embed_content(
            model=self._embedding_model_name,
            contents=text,
            config=types.EmbedContentConfig(task_type='RETRIEVAL_QUERY')
        )
        return np.array(result.embeddings[0].values).reshape(1, -1)
    
    def upload_file(self, file_path: str, display_name: str = None):
        """Загрузка файла в Gemini Files API."""
        return self._client.files.upload(file=file_path)
    
    def _parse_response(self, response, model_name: str, user_logger: logging.Logger = None) -> tuple:
        """
        Парсинг ответа от API Gemini.
        Returns: tuple (status, text/message)
        """
        if user_logger and hasattr(response, 'usage_metadata'):
            user_logger.info(
                f"[API RESPONSE - {model_name}]\n"
                f"{response.model_dump_json(exclude_none=True, indent=2)}"
            )

        if not response.candidates:
            reason = ""
            is_safety_blocking = False

            if hasattr(response, 'prompt_feedback') and hasattr(response.prompt_feedback, 'block_reason'):
                block_reason = response.prompt_feedback.block_reason
                reason = f"Причина: {block_reason}"
                
                # Считаем нарушением только явные причины:
                # 3 = SAFETY, 4 = PROHIBITED_CONTENT, 2 = BLOCKLIST
                if str(block_reason) in ('3', '4', '2', 'SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST'):
                    is_safety_blocking = True
            
            if user_logger:
                user_logger.error(f"Пустой ответ от API (нет кандидатов). {reason}")

            if is_safety_blocking:
                return 'SAFETY', f"Контент заблокирован на уровне промпта. {reason}"

            return 'ERROR', f"Пустой ответ от API. {reason}"

        candidate = response.candidates[0]
        finish_reason = candidate.finish_reason

        if finish_reason == 'STOP':
            if hasattr(candidate.content, 'parts') and candidate.content.parts:
                full_text = "".join(part.text for part in candidate.content.parts)
                if full_text.strip():
                    return 'SUCCESS', full_text
            if user_logger:
                user_logger.warning(f"finish_reason=STOP, но текст пустой")
            return 'ERROR', "Успешный статус от API, но пустой ответ."

        if finish_reason in ('SAFETY', 'PROHIBITED_CONTENT', 'BLOCKLIST'):
            safety_info = ""
            if hasattr(candidate, 'safety_ratings'):
                safety_info = f"Safety Ratings: {candidate.safety_ratings}"
            if user_logger:
                user_logger.warning(f"Запрос заблокирован по безопасности. {safety_info}")
            return 'SAFETY', f"Контент заблокирован. Причина: {finish_reason}. {safety_info}"

        if user_logger:
            user_logger.error(f"Техническая ошибка API. Finish Reason: {finish_reason}")
        return 'ERROR', f"Техническая ошибка API. Finish Reason: {finish_reason}"


# ===============================================================
# БЛОК 3: ИНИЦИАЛИЗАЦИЯ БЭКЭНДА
# ===============================================================

def initialize_backend(logs_dir_path: str):
    """
    Загружает все необходимые данные и модели в память при старте.
    Эта функция вызывается один раз при запуске бота.
    """
    global rag_df, corpus_embeddings, doc_to_case_map, gemini_client
    global PROMPT_1_PREPROCESSING, PROMPT_2_ANALYSIS, RAG_TOP_N
    global USER_FILES_DIR, FILE_COUNTER_PATH

    print("Инициализация бэкенда...")
    
     # --- Инициализация путей для сохранения файлов и счетчика ---
    USER_FILES_DIR = os.path.join(logs_dir_path, 'user_files')
    FILE_COUNTER_PATH = os.path.join(logs_dir_path, 'file_counter.txt')

    if not os.path.exists(USER_FILES_DIR):
        os.makedirs(USER_FILES_DIR)
        print(f"Создана директория для файлов: {USER_FILES_DIR}")

    if not os.path.exists(FILE_COUNTER_PATH):
        with open(FILE_COUNTER_PATH, 'w') as f:
            f.write('0')
        print(f"Создан файл счетчика: {FILE_COUNTER_PATH}")
        
    # --- Загрузка конфигурации ---
    API_KEY = load_env_variable('GEMINI_API_KEY')
    RAG_DATA_PATH = load_env_variable('RAG_DATA_PATH')
    CORPUS_EMBEDDINGS_PATH = load_env_variable('CORPUS_EMBEDDINGS_PATH')
    PROMPT1_PATH = load_env_variable('PROMPT1_PREPROCESSING_PATH')
    PROMPT2_PATH = load_env_variable('PROMPT2_ANALYSIS_PATH')
    EMBEDDING_MODEL_NAME = load_env_variable('EMBEDDING_MODEL')
    PRIMARY_GENERATIVE_MODEL_NAME = load_env_variable('PRIMARY_GENERATIVE_MODEL')
    FALLBACK_GENERATIVE_MODEL_NAME = load_env_variable('FALLBACK_GENERATIVE_MODEL')
    RAG_TOP_N = load_env_variable('RAG_TOP_N', is_int=True, default=5)

    PROMPT_1_PREPROCESSING = load_prompt_from_file(PROMPT1_PATH)
    PROMPT_2_ANALYSIS = load_prompt_from_file(PROMPT2_PATH)

    # --- Создание GeminiClient ---
    gemini_client = GeminiClient(
        api_key=API_KEY,
        primary_model=PRIMARY_GENERATIVE_MODEL_NAME,
        fallback_model=FALLBACK_GENERATIVE_MODEL_NAME,
        embedding_model=EMBEDDING_MODEL_NAME
    )
    
    # --- Загрузка данных RAG ---
    print(f"Загрузка RAG данных из {RAG_DATA_PATH}")
    rag_df = pd.read_csv(RAG_DATA_PATH, sep=';')
    print(f"Загрузка эмбеддингов из {CORPUS_EMBEDDINGS_PATH}")
    corpus_embeddings = np.load(CORPUS_EMBEDDINGS_PATH)
    
    if 'docID' not in rag_df.columns or 'caseID' not in rag_df.columns:
        raise ValueError("В RAG файле отсутствуют колонки 'docID' и/или 'caseID'.")
        
    doc_to_case_map = pd.Series(rag_df.caseID.values, index=rag_df.docID.astype(str)).to_dict()


# ===============================================================
# БЛОК 4: ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ===============================================================

def get_and_increment_file_counter() -> int:
    """Читает, инкрементирует и сохраняет глобальный счетчик файлов."""
    try:
        with open(FILE_COUNTER_PATH, 'r+') as f:
            current_count = int(f.read().strip() or 0)
            new_count = current_count + 1
            f.seek(0)
            f.write(str(new_count))
            f.truncate()
            return new_count
    except (IOError, ValueError) as e:
        print(f"Ошибка при работе с файлом счетчика {FILE_COUNTER_PATH}: {e}. Сбрасываю на 1.")
        with open(FILE_COUNTER_PATH, 'w') as f:
            f.write('1')
        return 1

def resize_image(image_bytes: bytes, max_size_px: int = 1024, quality: int = 85) -> Image.Image:
    """Изменяет размер изображения до max_size_px по большей стороне."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.thumbnail((max_size_px, max_size_px))
    return img

def build_user_content(image: Image.Image = None, text: str = "") -> list:
    """Собирает контент для API Gemini, обрабатывая все комбинации."""
    content_parts = []
    if text:
        content_parts.append(text)
    if image:
        content_parts.append(image)
    return content_parts


# ===============================================================
# БЛОК 5: ОСНОВНЫЕ ФУНКЦИИ БЭКЭНДА (ШАГИ АНАЛИЗА)
# ===============================================================

def semantic_search(query_text: str, user_logger: logging.Logger = None):
    """Шаг 2: Семантический поиск релевантных кейсов."""
    print(f"Шаг 2: Поиск {RAG_TOP_N} релевантных кейсов...")
    try:
        query_embedding = gemini_client.embed(query_text)
        similarities = np.dot(corpus_embeddings, query_embedding.T).flatten()

        top_10_indices_for_logging = np.argsort(similarities)[-10:][::-1]
        
        if user_logger:
            log_message = ["[SEMANTIC SEARCH] Топ-10 релевантных дел:"]
            top_10_results = rag_df.iloc[top_10_indices_for_logging]
            for index, row in top_10_results.iterrows():
                similarity_score = similarities[index]
                log_message.append(
                    f"  - CaseID: {row.get('caseID', 'N/A')}, "
                    f"Cosine Similarity: {similarity_score:.4f}"
                )
            user_logger.info('\n'.join(log_message))
        
        top_n_indices = top_10_indices_for_logging[:RAG_TOP_N]

        top_n_indices = np.argsort(similarities)[-RAG_TOP_N:][::-1]
        return rag_df.iloc[top_n_indices].copy()
    except Exception as e:
        print(f"❗️ Ошибка на этапе семантического поиска: {e}")
        return pd.DataFrame()

def format_rag_context(search_results_df):
    """Форматирует найденные кейсы в текстовый контекст."""
    if search_results_df.empty:
        return "Контекстные дела из практики ФАС не найдены."
    
    context_parts = []
    for _, row in search_results_df.iterrows():
        context_parts.append(
            f"Кейс (caseID: \"{row['caseID']}\"):\n"
            f"- Описание нарушения: \"{row.get('violation_summary', '')}\"\n"
            f"- Аргументы ФАС: \"{row.get('fas_arguments', '')}\"\n"
            f"- Теги: \"{row.get('thematic_tags', '')}\""
        )
    return "\n---\n".join(context_parts)

def sanitize_html(text: str) -> str:
    """
    Приводит текст к валидному подмножеству HTML, поддерживаемому Telegram.
    Telegram поддерживает: b, strong, i, em, u, ins, s, strike, del, code, pre, a href, tg-spoiler.
    Всё остальное (включая < и > в тексте) нужно экранировать.
    """
    import html as html_module
    
    # Шаг 1: Выделяем разрешённые теги, экранируем всё остальное
    ALLOWED_TAGS_PATTERN = re.compile(
        r'(</?(b|strong|i|em|u|ins|s|strike|del|code|tg-spoiler)>)'
        r'|(<a\s+href="[^"]*">)'
        r"|(<a\s+href='[^']*'>)"
        r'|(</a>)'
        r'|(<pre(?:\s+language="[^"]*")?>)'
        r'|(</pre>)',
        re.IGNORECASE
    )
    
    parts = []
    last_end = 0
    for match in ALLOWED_TAGS_PATTERN.finditer(text):
        start, end = match.span()
        if start > last_end:
            parts.append(html_module.escape(text[last_end:start]))
        parts.append(match.group(0))
        last_end = end
    if last_end < len(text):
        parts.append(html_module.escape(text[last_end:]))
    
    result = ''.join(parts)
    
    # Шаг 2: Конвертируем Markdown в HTML
    result = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', result, flags=re.MULTILINE)
    result = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', result)
    result = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', result)
    
    # Шаг 3: Починить незакрытые теги
    simple_tags = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'tg-spoiler']
    for tag in simple_tags:
        open_count = len(re.findall(f'<{tag}(?:\\s[^>]*)?>', result, re.IGNORECASE))
        close_count = len(re.findall(f'</{tag}>', result, re.IGNORECASE))
        for _ in range(open_count - close_count):
            result += f'</{tag}>'
        for _ in range(close_count - open_count):
            result = re.sub(f'</{tag}>', '', result, count=1, flags=re.IGNORECASE)
    
    open_a = len(re.findall(r'<a\s', result, re.IGNORECASE))
    close_a = len(re.findall(r'</a>', result, re.IGNORECASE))
    for _ in range(open_a - close_a):
        result += '</a>'
    for _ in range(close_a - open_a):
        result = re.sub(r'</a>', '', result, count=1, flags=re.IGNORECASE)
    
    return result


def postprocess_final_answer(final_text):
    """Шаг 4: Постобработка - добавление ссылок и дисклеймера."""
    print("Шаг 4: Пост-обработка ответа...")

    # Шаблон для UUID v4, который будет захвачен в группу для извлечения.
    UUID_V4_PATTERN = r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"

    # Функция-заменитель. Она принимает объект совпадения от re.sub и возвращает отформатированную HTML-ссылку.
    def replace_with_link(match):
        # Извлекаем чистый UUID из первой (и единственной) захваченной группы.
        case_id = match.group(1).strip().lower()
        url = f"https://br.fas.gov.ru/cases/{case_id}/"
        return f'<a href="{url}">[ссылка]</a>'

    # Создаем единый и точный шаблон для поиска. Он ищет UUID, которому может (но не обязан) предшествовать "case ID" или "caseID". Вся найденная конструкция (ключевое слово + UUID) заменяется одной ссылкой.
    pattern = re.compile(
        # Начало необязательной, незахватываемой группы для ключевого слова.
        # `?:` означает, что группа не будет сохранена в результатах.
        # `?` в конце делает всю эту группу необязательной.
        r"(?:" +
        # Граница слова (`\b`), чтобы не найти "caseID" в середине другого слова.
        r"\bcase\s?ID" +
        # Опциональные разделители, такие как двоеточие или пробелы.
        r"[:\s]*" +
        # Конец необязательной группы.
        r")?" +
        # Основной шаблон UUID, результат которого мы захватываем в группу 1.
        UUID_V4_PATTERN,
        re.IGNORECASE  # Игнорировать регистр для "CaseID", "caseid" и т.д.
    )

    # Применяем замену ко всему тексту с помощью созданного шаблона.
    processed_text = pattern.sub(replace_with_link, final_text)
    
    # Санитизация HTML: экранируем невалидные символы < и >, оставляя только разрешённые теги
    processed_text = sanitize_html(processed_text)
    
    DISCLAIMER = """

<i>А также не забудьте:</i>

• Объекты, входящие в состав креатива (тексты, шрифты, изображения, товарные знаки и иные обозначения), не должны нарушать права третьих лиц — в том числе авторские, смежные и исключительные права, а также право гражданина на охрану его изображения. Убедитесь, что у вас имеются лицензии и все необходимые согласия на использование таких объектов <b>именно в данном креативе</b> и <b>через предполагаемые каналы его распространения</b>.

• При размещении рекламы в Интернете необходимо <b>заранее получить erid</b> у оператора рекламных данных, а также добавить на креатив читаемую пометку «реклама» и наименование рекламодателя. Для размещения креативов в периодических печатных изданиях также требуется пометка «реклама» или «на правах рекламы».

• <b>До направления</b> рассылок по e-mail или sms у вас должно быть получено согласие пользователя на их получение! В последние годы ФАС наиболее часто привлекает к ответственности именно за отсутствие такого согласия.

• То, что вы заявляете в рекламе, должно на 100% соответствовать действительности: у ФАС обширная практика по выявлению недостоверной информации в рекламе. Даже незначительные преувеличения могут стать причиной обращения недовольных клиентов в ФАС.

• Законом предусмотрены правила не только для содержания креативов, но и для способов их размещения по почти любым каналам распространения помимо того, что указано выше. Если у вас остаются сомнения, всегда лучше заручиться консультацией юриста. 
"""
    return processed_text + DISCLAIMER


# ===============================================================
# БЛОК 6: ГЛАВНАЯ ФУНКЦИЯ (ТОЧКА ВХОДА ДЛЯ БОТА)
# ===============================================================
async def analyze_creative_flow(file_bytes=None, text_content="", file_path=None, original_filename=None, user_id: int = None, user_logger: logging.Logger = None, model_to_use: str = 'primary') -> dict:
    """
    Основной пайплайн анализа. Принимает данные от бота, возвращает словарь с результатом.
    """
    try:
        # Определяем, какую модель использовать
        use_fallback = (model_to_use == 'fallback')
        model_name_for_log = "Fallback (Flash)" if use_fallback else "Primary (Pro)"
            
        user_logger.info(f"--- Начало анализа с использованием модели: {model_name_for_log} ---")

        user_content_for_api = []
        image_obj = None

        if file_path and file_path.lower().endswith('.pdf'):
            print(f"Загрузка PDF файла: {original_filename}")
            uploaded_file = gemini_client.upload_file(file_path, display_name=original_filename)
            user_content_for_api = build_user_content(text=text_content, image=uploaded_file)
        else:
            if file_bytes:
                image_obj = resize_image(file_bytes)
                if image_obj and user_id and user_logger:
                    try:
                        file_count = get_and_increment_file_counter()
                        new_filename = f"{user_id}_{file_count}.jpg"
                        save_path = os.path.join(USER_FILES_DIR, new_filename)
                        image_obj.save(save_path, 'JPEG', quality=85)
                        user_logger.info(f"Обработанный файл сохранен по пути: {save_path}")
                    except Exception as e:
                        user_logger.error(f"Не удалось сохранить обработанный файл: {e}")
            user_content_for_api = build_user_content(image=image_obj, text=text_content)
        
        # 1. Предварительная обработка контента через GeminiClient
        print("Шаг 1: Предварительная обработка контента...")
        content_for_preprocessing = [PROMPT_1_PREPROCESSING] + user_content_for_api
        preprocess_result = gemini_client.generate(content_for_preprocessing, use_fallback=use_fallback, user_logger=user_logger)
        
        if preprocess_result["status"] == "SAFETY":
            return {"error_type": "safety", "message": preprocess_result["message"], "model_used": model_to_use}
        if preprocess_result["status"] == "ERROR":
            raise Exception(f"Ошибка предобработки: {preprocess_result['message']}")
        
        processed_text = preprocess_result["text"]

        # 2. Поиск в RAG
        rag_results = semantic_search(processed_text, user_logger=user_logger)
        rag_context = format_rag_context(rag_results)
        
        # 3. Финальный анализ через GeminiClient
        print("Шаг 3: Генерация финального юридического заключения...")
        final_prompt = PROMPT_2_ANALYSIS.replace("{{user_creative_text}}", processed_text)
        final_prompt = final_prompt.replace("{{rag_cases_context}}", rag_context)
        final_result = gemini_client.generate([final_prompt], use_fallback=use_fallback, user_logger=user_logger)
        
        if final_result["status"] == "SAFETY":
            return {"error_type": "safety", "message": final_result["message"], "model_used": model_to_use}
        if final_result["status"] == "ERROR":
            raise Exception(f"Ошибка финального анализа: {final_result['message']}")
        
        final_text = final_result["text"]
        
        # 4. Пост-обработка
        final_output = postprocess_final_answer(final_text)
        
        return {
            "final_output": final_output,
            "preprocessed_text": processed_text,
            "model_used": model_to_use
        }
        
    except Exception as e:
        # Этот блок ловит все технические ошибки
        print(f"❗️ Критическая ошибка в `analyze_creative_flow` с моделью {model_to_use}: {e}")
        if user_logger:
            user_logger.error(f"КРИТИЧЕСКАЯ ОШИБКА в `analyze_creative_flow` с моделью {model_to_use}: {e}", exc_info=True)
        # Возвращаем словарь, который бот сможет обработать как техническую ошибку
        return {"error_type": "technical", "message": str(e), "model_used": model_to_use}
