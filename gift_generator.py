import logging
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
    # Fallback значения если конфиг не найден
    openai_api_key = os.getenv('OPENAI_API_KEY', '')
    logger.warning("Используются fallback значения конфига")

# Инициализируем OpenAI клиента
if openai_api_key:
    openai_client = OpenAI(api_key=openai_api_key)
else:
    openai_client = None
    logger.warning("OpenAI API ключ не найден")

GIFT_GENERATION_PROMPT = """
Ты создаешь АБСУРДНЫЕ и ДЕБИЛЬНЫЕ подарки для директора чата на основе анализа его сообщений.

Твоя задача:
1. Проанализировать сообщения директора чата
2. Понять его характер, привычки, интересы
3. Создать МАКСИМАЛЬНО АБСУРДНЫЙ подарок, который якобы идеально ему подходит

ПРАВИЛА СОЗДАНИЯ ПОДАРКА:
- Подарок должен быть СОВЕРШЕННО ДЕБИЛЬНЫМ
- Но при этом "логично" подходить к характеру человека
- Используй гиперболу и абсурдность
- Можешь использовать нецензурную лексику
- Подарок должен быть максимально нелепым
- Но с "обоснованием" почему именно это ему нужно

ФОРМАТ ВЫВОДА:
**🎁 ПОДАРОК ДЛЯ ДИРЕКТОРА: [ИМЯ]**

**Что дарим:**
[Абсурдное описание подарка]

**Почему именно это:**
[Дебильное обоснование на основе его сообщений]

**Как это использовать:**
[Еще более абсурдные инструкции по использованию]

**Где купить:**
[Нелепое место покупки]

**Цена:**
[Абсурдная цена]

**Дополнительные аксессуары:**
[Еще более дебильные дополнения к подарку]

ПРИМЕРЫ АБСУРДНЫХ ПОДАРКОВ:
- Для любителя спорить: "Персональный стенд для споров с зеркалом"
- Для многословного: "Автоматический переводчик многословия в краткость"
- Для любителя шуток: "Портативная сцена для выступления в лифте"

Будь максимально креативным и абсурдным!
"""

def generate_director_gift(analysis_text):
    """
    Генерирует абсурдный подарок для директора чата на основе анализа
    """
    try:
        if not openai_client:
            logger.error("OpenAI клиент недоступен")
            return None
        
        # Извлекаем имя директора из анализа
        import re
        director_name_match = re.search(r'\*\*Директор чата:\*\*\s*([^\n]+?)(?:\s*\n|$)', analysis_text, re.IGNORECASE | re.MULTILINE)
        
        if not director_name_match:
            logger.warning("Имя директора не найдено в анализе")
            return None
        
        director_name = director_name_match.group(1).strip()
        director_name = re.sub(r'[*_`]', '', director_name)
        
        # Генерируем подарок
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": GIFT_GENERATION_PROMPT},
                {"role": "user", "content": f"Создай абсурдный подарок для директора чата на основе этого анализа:\n\n{analysis_text}"}
            ],
            temperature=0.9,  # Высокая креативность для абсурдности
            max_tokens=1000
        )
        
        gift_description = response.choices[0].message.content
        
        logger.info(f"Сгенерирован подарок для директора: {director_name}")
        return gift_description
        
    except Exception as e:
        logger.error(f"Ошибка генерации подарка для директора: {e}")
        return None

def generate_gift_photo_prompt(gift_description):
    """
    Генерирует промпт для создания фото подарка
    """
    try:
        if not openai_client:
            logger.error("OpenAI клиент недоступен")
            return None
        
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system", 
                    "content": """Ты создаешь промпты для генерации изображений абсурдных подарков.

ТРЕБОВАНИЯ:
- Создай КРАТКИЙ английский промпт (максимум 50 слов)
- ПЕРЕВЕДИ описание с русского на английский язык
- Изображение должно быть АБСУРДНЫМ и СМЕШНЫМ
- Высокое качество изображения
- Реалистичный стиль

ВАЖНО: Создай промпт для изображения подарка, который будет выглядеть максимально нелепо и забавно."""
                },
                {
                    "role": "user", 
                    "content": f"Переведи на английский и создай промпт для фото подарка на основе этого описания:\n\n{gift_description}"
                }
            ],
            temperature=0.7,
            max_tokens=150
        )
        
        prompt = response.choices[0].message.content.strip()
        
        # Добавляем технические детали для качества
        enhanced_prompt = f"{prompt}, high resolution, realistic, 8k quality, studio lighting"
        
        logger.info(f"Сгенерирован промпт для фото подарка: {enhanced_prompt}")
        return enhanced_prompt
        
    except Exception as e:
        logger.error(f"Ошибка генерации промпта для фото подарка: {e}")
        return None

# Тестовая функция
def test_gift_generation():
    """
    Тестовая функция для проверки работы генератора подарков
    """
    test_analysis = """
    **Директор чата: Иван**
    
    **Описание личности:**
    Иван - вечный спорщик, который всегда готов к дискуссии. Он любит задавать вопросы и получать ответы от всех участников чата.
    
    **Почему именно он:**
    Иван постоянно инициирует обсуждения и поддерживает активность чата своими вопросами.
    
    **Стиль управления:**
    Демократичный лидер, который любит опрашивать всех и собирать мнения.
    
    **Характерные фразы:**
    "Как дела у всех?", "Кто что делал сегодня?", "Понятно, всем спасибо за ответы"
    """
    
    print("Тестируем генерацию подарка...")
    gift = generate_director_gift(test_analysis)
    if gift:
        print(f"Подарок: {gift}")
        
        # Тестируем генерацию промпта для фото
        photo_prompt = generate_gift_photo_prompt(gift)
        if photo_prompt:
            print(f"Промпт для фото: {photo_prompt}")
    else:
        print("Генерация подарка не удалась")

if __name__ == "__main__":
    # Запуск тестирования генерации подарков
    test_gift_generation() 