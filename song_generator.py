import json
import logging
import requests
import os
from datetime import datetime, timedelta
import pytz
import openai

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Импортируем конфиг
try:
    from config import OPENAI_API_KEY_EXTENDED, SUNO_API_KEY
    openai_api_key = OPENAI_API_KEY_EXTENDED
    suno_api_key = SUNO_API_KEY
    logger.info("Загружен конфиг из config.py")
except ImportError:
    openai_api_key = os.getenv('OPENAI_API_KEY', '')
    suno_api_key = os.getenv('SUNO_API_KEY', '')
    logger.warning("Используются fallback значения конфига")

# Инициализируем OpenAI клиента
if openai_api_key:
    openai_client = openai.OpenAI(api_key=openai_api_key)
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

def analyze_chat_and_generate_song(message_store, chat_id):
    """
    Анализирует сообщения за 24 часа и генерирует текст песни
    Возвращает JSON с информацией о песне
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
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": """Ты создаешь песни на основе анализа чата за 24 часа.

ТВОИ ЗАДАЧИ:
1. Проанализируй ВСЕ сообщения за 24 часа
2. Определи главные темы, события, участников
3. Создай оригинальную песню на основе событий чата
4. Выбери подходящий жанр и настроение

ТРЕБОВАНИЯ К ПЕСНЕ:
- Оригинальный текст на основе событий чата
- Подходящий жанр (поп, рок, рэп, электроника, джаз и т.д.)
- Настроение должно соответствовать атмосфере чата
- Включи имена участников и ключевые события
- Сделай песню смешной и запоминающейся
- Длина текста: 200-800 слов

СТРУКТУРА ПЕСНИ:
- Название песни
- 2-3 куплета
- Припев (повторяется 2-3 раза)
- Возможно бридж

ФОРМАТ ОТВЕТА (строго JSON):
{
    "song_title": "Название песни",
    "genre": "Жанр (Pop, Rock, Rap, Electronic, Jazz, Classical)",
    "mood": "Настроение (Happy, Sad, Energetic, Calm, Funny, Dramatic)",
    "lyrics": "Полный текст песни с куплетами и припевом",
    "description": "Краткое описание что происходило в чате",
    "main_characters": ["Участник 1", "Участник 2"],
    "key_events": ["Событие 1", "Событие 2"],
    "style_prompt": "Краткое описание стиля для Suno API (максимум 200 символов)"
}

ПРИМЕРЫ ЖАНРОВ:
- Pop: для веселых и легких тем
- Rock: для драматических событий
- Rap: для быстрых обсуждений
- Electronic: для технических тем
- Jazz: для интеллектуальных дискуссий
- Classical: для серьезных тем

ВАЖНО: Возвращай ТОЛЬКО валидный JSON без дополнительного текста."""
                },
                {
                    "role": "user",
                    "content": f"Вот сообщения чата за последние 24 часа:\n\n{messages}"
                }
            ],
            temperature=0.9,
            max_tokens=1500
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
                required_fields = ['song_title', 'genre', 'mood', 'lyrics', 'description', 
                                 'main_characters', 'key_events', 'style_prompt']
                
                for field in required_fields:
                    if field not in result:
                        logger.warning(f"Отсутствует поле {field} в ответе")
                        result[field] = "Не указано"
                
                logger.info(f"Песня успешно создана: {result['song_title']}")
                return result
            else:
                logger.error("JSON не найден в ответе")
                return None
                
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка парсинга JSON: {e}")
            logger.debug(f"Ответ модели: {result_text}")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка генерации песни: {e}")
        return None

def generate_music_with_suno(song_data):
    """
    Генерирует музыку через Suno API на основе данных песни
    Возвращает task_id для отслеживания статуса
    """
    try:
        if not suno_api_key:
            logger.error("Suno API ключ не найден")
            return None
        
        url = "https://api.sunoapi.org/api/v1/generate"
        
        # Подготавливаем параметры для Suno API
        payload = {
            "prompt": song_data['lyrics'][:3000],  # Ограничиваем длину текста
            "style": song_data['genre'],
            "title": song_data['song_title'][:80],  # Ограничиваем длину названия
            "customMode": True,
            "instrumental": False,  # С вокалом
            "model": "V3_5",  # Используем стабильную модель
            "negativeTags": "Heavy Metal, Upbeat Drums"  # Исключаем нежелательные стили
        }
        
        headers = {
            "Authorization": f"Bearer {suno_api_key}",
            "Content-Type": "application/json"
        }
        
        logger.info("Отправляем запрос к Suno API...")
        response = requests.post(url, json=payload, headers=headers)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 200 and result.get('data', {}).get('id'):
                task_id = result['data']['id']
                logger.info(f"Задача создания музыки отправлена, task_id: {task_id}")
                return {
                    'task_id': task_id,
                    'song_data': song_data
                }
            else:
                logger.error(f"Ошибка в ответе Suno API: {result}")
                return None
        else:
            logger.error(f"Ошибка Suno API: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка генерации музыки: {e}")
        return None

def check_suno_task_status(task_id):
    """
    Проверяет статус задачи генерации музыки
    """
    try:
        if not suno_api_key:
            logger.error("Suno API ключ не найден")
            return None
        
        url = f"https://api.sunoapi.org/api/v1/get?ids={task_id}"
        
        headers = {
            "Authorization": f"Bearer {suno_api_key}",
            "Content-Type": "application/json"
        }
        
        response = requests.get(url, headers=headers)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('code') == 200 and result.get('data'):
                task_data = result['data'][0] if isinstance(result['data'], list) else result['data']
                return task_data
            else:
                logger.error(f"Ошибка в ответе Suno API: {result}")
                return None
        else:
            logger.error(f"Ошибка Suno API: {response.status_code} - {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка проверки статуса: {e}")
        return None

def wait_for_suno_completion(task_id, max_wait_time=180):
    """
    Ждет завершения генерации музыки (максимум 3 минуты)
    Возвращает данные о готовой музыке или None
    """
    import time
    
    logger.info(f"Ждем завершения генерации музыки (task_id: {task_id})...")
    
    start_time = time.time()
    while time.time() - start_time < max_wait_time:
        task_data = check_suno_task_status(task_id)
        
        if task_data:
            status = task_data.get('status')
            if status == 'complete':
                logger.info("Музыка готова!")
                return task_data
            elif status == 'failed':
                logger.error("Генерация музыки не удалась")
                return None
            else:
                logger.info(f"Статус: {status}, ждем...")
        
        # Ждем 10 секунд перед следующей проверкой
        time.sleep(10)
    
    logger.warning("Время ожидания истекло")
    return None

def format_song_message(song_data):
    """
    Форматирует данные песни в красивое сообщение
    """
    if not song_data:
        return "❌ Не удалось создать песню."
    
    message = f"""🎵 <b>ПЕСНЯ ДНЯ</b> 🎵

🎼 <b>Название:</b> {song_data['song_title']}
🎭 <b>Жанр:</b> {song_data['genre']}
😊 <b>Настроение:</b> {song_data['mood']}

📝 <b>Описание событий:</b>
{song_data['description']}

👥 <b>Главные герои:</b>
{', '.join(song_data['main_characters'])}

🎯 <b>Ключевые события:</b>
{', '.join(song_data['key_events'])}

🎤 <b>Текст песни:</b>

{song_data['lyrics']}"""
    
    return message

 