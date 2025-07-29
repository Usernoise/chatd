import logging
from datetime import datetime, timedelta
import pytz
from openai import OpenAI
import os

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Импортируем конфиг
try:
    from config import OPENAI_API_KEY_EXTENDED, MOSCOW_TIMEZONE
    openai_api_key = OPENAI_API_KEY_EXTENDED
    moscow_tz = pytz.timezone(MOSCOW_TIMEZONE)
    logger.info("Загружен конфиг из config.py")
except ImportError:
    # Fallback значения если конфиг не найден
    openai_api_key = os.getenv('OPENAI_API_KEY', '')
    moscow_tz = pytz.timezone('Europe/Moscow')
    logger.warning("Используются fallback значения конфига")

# Инициализируем OpenAI клиента
if openai_api_key:
    openai_client = OpenAI(api_key=openai_api_key)
else:
    openai_client = None
    logger.warning("OpenAI API ключ не найден")

DIRECTOR_ANALYSIS_PROMPT = """
Ты аналитик чата, который выбирает директора чата на основе анализа всех сообщений за 24 часа.

Твоя задача:
1. Проанализировать ВСЕ сообщения за последние 24 часа
2. Выбрать ОДНОГО участника как "Директора чата"
3. Создать подробное описание этого человека на основе его сообщений

КРИТЕРИИ ВЫБОРА ДИРЕКТОРА:
- Кто задает тон беседы
- Кто больше всего влияет на обсуждения
- Кто проявляет лидерские качества
- Кто инициирует важные темы
- Кто поддерживает активность чата
- Кто получает больше всего реакций от других

ФОРМАТ ВЫВОДА:
**Директор чата: [ИМЯ]**

**Описание личности:**
[Подробное описание характера, стиля общения, привычек на основе его сообщений]

**Почему именно он/она:**
[Обоснование выбора с конкретными примерами из сообщений]

**Стиль управления:**
[Как этот человек влияет на чат, какие темы поднимает, как общается]

**Характерные фразы:**
[3-5 характерных фраз или выражений этого человека]

**Внешнее описание:**
[Краткое описание как должен выглядеть директор на фото - стиль, выражение лица, одежда, НЕ ВЫДУМЫВАЙ а только на основе сообщений, гиперболизируй для лучшей узнаваемости]

ПРАВИЛА:
- Анализируй ТОЛЬКО сообщения за последние 24 часа
- Не выдумывай - используй только реальные данные из сообщений
- Будь объективным в анализе
- Используй разговорный стиль
- Можешь использовать нецензурную лексику для передачи характера
- Если нет подходящего кандидата, пиши "Директор не найден"
"""

def get_messages_last_24h(message_store, chat_id):
    """
    Получает сообщения за последние 24 часа для конкретного чата
    """
    try:
        current_time = datetime.now(moscow_tz)
        chat_id = str(chat_id)
        
        if chat_id not in message_store:
            return ""
        
        # Время начала: текущее время минус 24 часа
        start_time = current_time - timedelta(hours=24)
        end_time = current_time
        
        relevant_messages = [
            f"{msg['sender']}: {msg['text']}"
            for msg in message_store[chat_id].values()
            if start_time <= msg['timestamp'] <= end_time
        ]
        
        return "\n".join(relevant_messages)
        
    except Exception as e:
        logger.error(f"Ошибка при получении сообщений за 24 часа: {e}")
        return ""

def analyze_director(message_store, chat_id):
    """
    Анализирует сообщения за 24 часа и выбирает директора чата
    Возвращает кортеж (описание_директора, имя_директора) или (None, None)
    """
    try:
        if not openai_client:
            logger.error("OpenAI клиент недоступен")
            return None, None
        
        # Получаем сообщения за 24 часа
        messages = get_messages_last_24h(message_store, chat_id)
        if not messages:
            logger.info("Нет сообщений за последние 24 часа")
            return None, None
        
        # Анализируем через OpenAI
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": DIRECTOR_ANALYSIS_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за последние 24 часа:\n\n{messages}"}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        
        analysis = response.choices[0].message.content
        
        # Извлекаем имя директора из анализа
        director_name = extract_director_name(analysis)
        
        logger.info(f"Анализ директора завершен. Найден: {director_name}")
        return analysis, director_name
        
    except Exception as e:
        logger.error(f"Ошибка при анализе директора: {e}")
        return None, None

def extract_director_name(analysis_text):
    """
    Извлекает имя директора из текста анализа
    """
    try:
        import re
        
        # Паттерны для поиска имени директора
        patterns = [
            r'Директор чата:\s*([^\n]+?)(?:\s*\n|$)',
            r'\*\*Директор чата:\*\*\s*([^\n]+?)(?:\s*\n|$)',
            r'Директор чата\s*[-–—]\s*([^\n]+?)(?:\s*\n|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, analysis_text, re.IGNORECASE | re.MULTILINE)
            if match:
                director_name = match.group(1).strip()
                # Убираем лишние символы
                director_name = re.sub(r'[*_`]', '', director_name)
                return director_name
        
        return None
        
    except Exception as e:
        logger.error(f"Ошибка при извлечении имени директора: {e}")
        return None

def generate_director_photo_prompt(analysis_text):
    """
    Генерирует промпт для создания фото директора на основе анализа
    """
    try:
        if not openai_client:
            logger.error("OpenAI клиент недоступен")
            return None
        
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system", 
                    "content": """Ты создаешь промпты для генерации официальных корпоративных портретов на основе описания директора чата.

ТРЕБОВАНИЯ К ФОТО:
- Максимально ОФИЦИАЛЬНЫЙ корпоративный стиль
- АБСУРДНО серьезное выражение лица 
- Деловой костюм или формальная одежда
- Профессиональная студийная съемка
- Нейтральный офисный фон или библиотека
- Поза как у генерального директора крупной корпорации
- Взгляд прямо в камеру с абсолютной уверенностью
- Высокое качество как iPhone фото

СТИЛЬ: Смешай серьезность корпоративного портрета с абсурдностью ситуации - обычный человек из чата позирует как топ-менеджер международной корпорации.

Основывайся на описании директора и создай КРАТКИЙ английский промпт (максимум 50 слов) для генерации изображения."""
                },
                {
                    "role": "user", 
                    "content": f"Создай промпт для официального корпоративного портрета на основе этого анализа директора:\n\n{analysis_text}"
                }
            ],
            temperature=0.7,
            max_tokens=200
        )
        
        prompt = response.choices[0].message.content.strip()
        
        # Добавляем технические детали для качества
        enhanced_prompt = f"{prompt}, professional corporate headshot, business suit, serious expression, studio lighting, neutral background, direct eye contact, confident pose, high resolution, iPhone photo quality, realistic, 8k"
        
        logger.info(f"Сгенерирован промпт для фото директора: {enhanced_prompt}")
        return enhanced_prompt
        
    except Exception as e:
        logger.error(f"Ошибка генерации промпта для фото директора: {e}")
        return None

# Тестовая функция
def test_director_analysis():
    """
    Тестовая функция для проверки работы анализатора
    """
    test_messages = """
    Иван: Привет всем! Как дела?
    Петя: Привет, все нормально
    Иван: Кто что делал сегодня?
    Маша: Я работала, устала
    Иван: Понятно, всем спасибо за ответы
    Петя: Иван, ты как всегда всех опрашиваешь
    Иван: А что, мне интересно что у людей происходит
    """
    
    print("Тестируем анализ директора...")
    analysis, director_name = analyze_director({"test_chat": {"1": {"sender": "test", "text": "test", "timestamp": datetime.now(moscow_tz)}}}, "test_chat")
    if analysis:
        print(f"Анализ директора: {analysis[:200]}...")
        print(f"Имя директора: {director_name}")
    else:
        print("Анализ директора не удался")

if __name__ == "__main__":
    # Запуск тестирования анализа директора
    test_director_analysis() 