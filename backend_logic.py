# ===============================================================
# БЛОК 1: ИМПОРТЫ И КОНФИГУРАЦИЯ
# ===============================================================
import os
import pandas as pd
import numpy as np
import google.generativeai as genai
import re
from PIL import Image
import io
import logging

# --- Глобальные переменные для кеширования ---
rag_df = None
corpus_embeddings = None
doc_to_case_map = None
embedding_model = None
generative_model = None
PROMPT_1_PREPROCESSING = None
PROMPT_2_ANALYSIS = None
RAG_TOP_N = 5
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

def initialize_backend(logs_dir_path: str):
    """
    Загружает все необходимые данные и модели в память при старте.
    Эта функция вызывается один раз при запуске бота.
    """
    global rag_df, corpus_embeddings, doc_to_case_map, embedding_model, generative_model
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
    GENERATIVE_MODEL_NAME = load_env_variable('GENERATIVE_MODEL')
    RAG_TOP_N = load_env_variable('RAG_TOP_N', is_int=True, default=5)

    PROMPT_1_PREPROCESSING = load_prompt_from_file(PROMPT1_PATH)
    PROMPT_2_ANALYSIS = load_prompt_from_file(PROMPT2_PATH)

    # --- Конфигурация API и моделей ---
    genai.configure(api_key=API_KEY)
    embedding_model = genai.GenerativeModel(EMBEDDING_MODEL_NAME)
    generative_model = genai.GenerativeModel(GENERATIVE_MODEL_NAME)
    
    # --- Загрузка данных RAG ---
    print(f"Загрузка RAG данных из {RAG_DATA_PATH}")
    rag_df = pd.read_csv(RAG_DATA_PATH)
    print(f"Загрузка эмбеддингов из {CORPUS_EMBEDDINGS_PATH}")
    corpus_embeddings = np.load(CORPUS_EMBEDDINGS_PATH)
    
    if 'docID' not in rag_df.columns or 'caseID' not in rag_df.columns:
        raise ValueError("В RAG файле отсутствуют колонки 'docID' и/или 'caseID'.")
        
    doc_to_case_map = pd.Series(rag_df.caseID.values, index=rag_df.docID.astype(str)).to_dict()

# ===============================================================
# БЛОК 2: ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
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
# БЛОК 3: ОСНОВНЫЕ ФУНКЦИИ БЭКЭНДА (ШАГИ АНАЛИЗА)
# ===============================================================

SAFETY_SETTINGS = {
    "HARM_CATEGORY_HARASSMENT": "BLOCK_ONLY_HIGH",
    "HARM_CATEGORY_HATE_SPEECH": "BLOCK_ONLY_HIGH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_ONLY_HIGH",
    "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_ONLY_HIGH",
}

def preprocess_content(user_content, user_logger: logging.Logger = None):
    """Шаг 1: Предварительная обработка (описание картинки, очистка текста)."""
    print("Шаг 1: Предварительная обработка контента...")
    try:
        content_for_api = [PROMPT_1_PREPROCESSING] + user_content
        print("\n--- ДИАГНОСТИКА: СОДЕРЖИМОЕ ЗАПРОСА В GEMINI ---")
        for i, part in enumerate(content_for_api):
            print(f"  - Часть {i+1}: Тип = {type(part)}")
            if isinstance(part, str):
                print(f"    Содержимое (первые 100 символов): '{part[:100]}...'")
            elif isinstance(part, Image.Image):
                print(f"    Содержимое: Объект изображения, режим={part.mode}, размер={part.size}")
            else:
                print(f"    Содержимое: {part}")
        print("--- КОНЕЦ ДИАГНОСТИКИ ---\n")

        response = generative_model.generate_content(
            content_for_api, 
            safety_settings=SAFETY_SETTINGS
        )
        
        if user_logger and hasattr(response, 'usage_metadata'):
            user_logger.info(f"[API METRICS - Preprocessing] {response.usage_metadata}")
            
        if not response.parts:
             print("❗️ Запрос заблокирован системой безопасности Gemini.")
             return {"error": "safety_block", "message": "Контент заблокирован системой безопасности."}
        return response.text
    except Exception as e:
        print(f"❗️ Ошибка на этапе предварительной обработки: {e}")
        return {"error": "processing_error", "message": str(e)}

def semantic_search(query_text: str, user_logger: logging.Logger = None):
    """Шаг 2: Семантический поиск релевантных кейсов."""
    print(f"Шаг 2: Поиск {RAG_TOP_N} релевантных кейсов...")
    try:
        result = genai.embed_content(
            model=embedding_model.model_name,
            content=query_text,
            task_type="RETRIEVAL_QUERY"
        )
        query_embedding = np.array(result['embedding']).reshape(1, -1)
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
            f"Кейс (docId: \"{row['docID']}\"):\n"
            f"- Описание нарушения: \"{row.get('violation_summary', '')}\"\n"
            f"- Аргументы ФАС: \"{row.get('fas_arguments', '')}\"\n"
            f"- Теги: \"{row.get('thematic_tags', '')}\""
        )
    return "\n---\n".join(context_parts)

def get_final_analysis(processed_text, rag_context, user_logger: logging.Logger = None):
    """Шаг 3: Генерация финального заключения."""
    print("Шаг 3: Генерация финального юридического заключения...")
    try:
        final_prompt = PROMPT_2_ANALYSIS.replace("{{user_creative_text}}", processed_text)
        final_prompt = final_prompt.replace("{{rag_cases_context}}", rag_context)

        if user_logger:
            user_logger.info(f"[ПРОМПТ 2 (ФИНАЛЬНЫЙ)]\n{final_prompt}")
            
        response = generative_model.generate_content(final_prompt, safety_settings=SAFETY_SETTINGS)

        if user_logger and hasattr(response, 'usage_metadata'):
            user_logger.info(f"[API METRICS - Final Analysis] {response.usage_metadata}")
            
        if not response.parts:
            return "Ошибка: Финальный анализ был заблокирован системой безопасности. Если вы уверены, что бот ошибся, не допустив креатив к проверке, попробуйте отправить его повторно или перезапустите бота командой /start."
        return response.text
    except Exception as e:
        print(f"❗️ Ошибка на этапе финального анализа: {e}")
        return "Ошибка при формировании юридического заключения."

def postprocess_final_answer(final_text):
    """Шаг 4: Постобработка - добавление ссылок и дисклеймера."""
    print("Шаг 4: Пост-обработка ответа...")
    
    def replace_link(match):
        doc_id = match.group(1).strip()
        case_id = doc_to_case_map.get(doc_id)
        if case_id:
            url = f"https://br.fas.gov.ru/cases/{case_id}/"
            return f'<a href="{url}">[ссылка]</a>'
        print(f"--- DEBUG: FAILED to find case_id for doc_id: {doc_id} ---")
        return "в одном из дел (ID не найден)"

    processed_text = re.sub(r"\[CASE_ID:\s*([^\]]+)\]", replace_link, final_text)
    
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
# БЛОК 4: ГЛАВНАЯ ФУНКЦИЯ (ТОЧКА ВХОДА ДЛЯ БОТА)
# ===============================================================
async def analyze_creative_flow(file_bytes=None, text_content="", file_path=None, original_filename=None, user_id: int = None, user_logger: logging.Logger = None) -> dict:
    """
    Основной пайплайн анализа. Принимает данные от бота, возвращает словарь с результатом.
    """
    if not any([file_bytes, text_content, file_path]):
        return {"final_output": "Ошибка: не предоставлено данных для анализа.", "preprocessed_text": "N/A", "safety_violation": False}

    user_content_for_api = []
    image_obj = None

    if file_path and file_path.lower().endswith('.pdf'):
        print(f"Загрузка PDF файла: {original_filename}")
        uploaded_file = genai.upload_file(path=file_path, display_name=original_filename)
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
                    print(f"❗️ Ошибка при сохранении файла для пользователя {user_id}: {e}")
                    user_logger.error(f"Не удалось сохранить обработанный файл: {e}")
        user_content_for_api = build_user_content(image=image_obj, text=text_content)
    
    # 1. Предварительная обработка
    processed_text_result = preprocess_content(user_content_for_api, user_logger=user_logger)
    
    # Проверяем, вернулся ли словарь с ошибкой от preprocess_content
    if isinstance(processed_text_result, dict):
        if processed_text_result.get('error') == 'safety_block':
            return {"final_output": None, "preprocessed_text": processed_text_result['message'], "safety_violation": True}
        else:
            error_msg = processed_text_result.get('message', 'Неизвестная ошибка предобработки')
            return {"final_output": error_msg, "preprocessed_text": error_msg, "safety_violation": False}

    processed_text = processed_text_result
    
    # 2. Поиск в RAG
    rag_results = semantic_search(processed_text, user_logger=user_logger)
    rag_context = format_rag_context(rag_results)
    
    # 3. Финальный анализ
    final_text = get_final_analysis(processed_text, rag_context, user_logger=user_logger)
    if "Ошибка:" in final_text:
        return {"final_output": final_text, "preprocessed_text": processed_text, "safety_violation": False}
    
    # 4. Пост-обработка
    final_output = postprocess_final_answer(final_text)
    
    return {
        "final_output": final_output,
        "preprocessed_text": processed_text,
        "safety_violation": False
    }