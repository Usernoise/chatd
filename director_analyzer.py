import json
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
    from config import OPENAI_API_KEY_EXTENDED
    openai_api_key = OPENAI_API_KEY_EXTENDED
    logger.info("Загружен конфиг из config.py")
except ImportError:
    openai_api_key = os.getenv('OPENAI_API_KEY', '')
    logger.warning("Используются fallback значения конфига")

# Инициализируем OpenAI клиента
if openai_api_key:
    openai_client = OpenAI(api_key=openai_api_key)
else:
    openai_client = None
    logger.warning("OpenAI API ключ не найден")

def get_messages_last_24h(message_store, chat_id):
    """
    Получает сообщения за последние 24 часа для конкретного чата
    """
    try:
        from config import MOSCOW_TIMEZONE
        moscow_tz = pytz.timezone(MOSCOW_TIMEZONE)
    except ImportError:
        moscow_tz = pytz.timezone('Europe/Moscow')
    
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

def analyze_director_and_gift(message_store, chat_id):
    """
    Анализирует сообщения за 24 часа, выбирает директора и генерирует подарок
    Возвращает JSON с информацией о директоре и подарке
    """
    try:
        if not openai_client:
            logger.error("OpenAI клиент недоступен")
            return None
        
        messages = get_messages_last_24h(message_store, chat_id)
        if not messages:
            logger.warning("Нет сообщений за последние 24 часа")
            return None
        
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": """Ты анализируешь сообщения чата за 24 часа и выбираешь директора чата, а также создаешь для него максимально дебильный и абсурдный подарок.

ТВОИ ЗАДАЧИ:
1. Проанализируй ВСЕ сообщения за 24 часа
2. Выбери директора чата (самого активного/влиятельного участника)
3. Создай максимально дебильный и абсурдный подарок для директора
4. Обоснуй почему именно этот подарок подходит директору

ТРЕБОВАНИЯ К ПОДАРКУ:
- Максимально дебильный и абсурдный
- Должен быть связан с активностью директора в чате
- Должен быть смешным и нелепым
- Может быть физическим предметом или абстрактной идеей

ФОРМАТ ОТВЕТА (строго JSON):
{
    "director_name": "Имя директора",
    "director_analysis": "Краткий анализ почему этот человек директор",
    "gift_name": "Название подарка",
    "gift_description": "Подробное описание подарка",
    "gift_reasoning": "Обоснование почему именно этот подарок подходит директору",
    "gift_photo_prompt": "Краткий английский промпт для генерации фото подарка (максимум 50 слов)"
}

ПРИМЕРЫ ПОДАРКОВ:
- "Персональный костюм-улитка для медленных решений"
- "Диплом 'Лучший генератор бессмысленных сообщений'"
- "Коллекция пустых коробок от важных решений"
- "Сертификат на бесплатные советы от попугая"

ВАЖНО: Возвращай ТОЛЬКО валидный JSON без дополнительного текста."""
                },
                {
                    "role": "user",
                    "content": f"Вот сообщения чата за последние 24 часа:\n\n{messages}"
                }
            ],
            temperature=0.9,
            max_tokens=1000
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Пытаемся извлечь JSON из ответа
        try:
            # Ищем JSON в ответе
            start_idx = result_text.find('{')
            end_idx = result_text.rfind('}') + 1
            
            if start_idx != -1 and end_idx != 0:
                json_str = result_text[start_idx:end_idx]
                result = json.loads(json_str)
                
                # Проверяем наличие всех необходимых полей
                required_fields = ['director_name', 'director_analysis', 'gift_name', 
                                 'gift_description', 'gift_reasoning', 'gift_photo_prompt']
                
                for field in required_fields:
                    if field not in result:
                        logger.warning(f"Отсутствует поле {field} в ответе")
                        result[field] = "Не указано"
                
                logger.info(f"Анализ директора успешен: {result['director_name']}")
                return result
            else:
                logger.error("JSON не найден в ответе")
                return None
                
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON: {e}")
            logger.debug(f"Ответ модели: {result_text}")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка анализа директора: {e}")
        return None

def format_gift_message(analysis_result):
    """
    Форматирует результат анализа в красивое сообщение
    """
    if not analysis_result:
        return "❌ Не удалось проанализировать директора чата."
    
    message = f"""🎁 <b>ПОДАРОК ДЛЯ ДИРЕКТОРА ЧАТА</b> 🎁

👑 <b>Директор:</b> {analysis_result['director_name']}
📊 <b>Анализ:</b> {analysis_result['director_analysis']}

🎁 <b>Подарок:</b> {analysis_result['gift_name']}
📝 <b>Описание:</b> {analysis_result['gift_description']}

🤔 <b>Почему именно этот подарок:</b>
{analysis_result['gift_reasoning']}"""
    
    return message

# Тестовая функция
def test_director_analysis():
    """
    Тестирует анализ директора
    """
    test_messages = {
        "123": {
            "msg1": {
                "sender": "Иван",
                "text": "Привет всем! Как дела?",
                "timestamp": datetime.now()
            },
            "msg2": {
                "sender": "Петр", 
                "text": "Все хорошо, спасибо!",
                "timestamp": datetime.now()
            },
            "msg3": {
                "sender": "Иван",
                "text": "Отлично! Давайте обсудим планы на завтра",
                "timestamp": datetime.now()
            }
        }
    }
    
    print("Тестируем анализ директора...")
    result = analyze_director_and_gift(test_messages, "123")
    if result:
        print("Тест прошел успешно!")
        print(f"Директор: {result['director_name']}")
        print(f"Подарок: {result['gift_name']}")
        print(f"Описание: {result['gift_description']}")
    else:
        print("Тест не прошел")

if __name__ == "__main__":
    test_director_analysis() 