import replicate
import os
import re
import requests
import imghdr
import tempfile
from datetime import datetime
from openai import OpenAI
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Импортируем конфиг - сначала пробуем основной config.py, потом neuralbot-server
try:
    from config import REPLICATE_API_TOKEN, REPLICATE_IMAGE_MODEL, OPENAI_API_KEY_EXTENDED
    replicate_api_token = REPLICATE_API_TOKEN
    REPLICATE_IMAGE_MODEL = REPLICATE_IMAGE_MODEL
    openai_api_key = OPENAI_API_KEY_EXTENDED
    logger.info("Загружен конфиг из основного config.py")
except ImportError:
    try:
        import sys
        sys.path.append('neuralbot-server')
        from config import replicate_api_token, REPLICATE_IMAGE_MODEL, openai_api_key
        logger.info("Загружен конфиг из neuralbot-server")
    except ImportError:
        # Fallback значения если конфиг не найден
        replicate_api_token = os.getenv('REPLICATE_API_TOKEN', '')
        REPLICATE_IMAGE_MODEL = "black-forest-labs/flux-schnell"
        openai_api_key = os.getenv('OPENAI_API_KEY', '')
        logger.warning("Используются fallback значения конфига")

# Устанавливаем токен Replicate
os.environ["REPLICATE_API_TOKEN"] = replicate_api_token

# Инициализируем OpenAI клиента
if openai_api_key:
    openai_client = OpenAI(api_key=openai_api_key)
else:
    openai_client = None
    logger.warning("OpenAI API ключ не найден")

def extract_director_info(top_summary_text):
    """
    Извлекает информацию о директоре из текста топа дня
    """
    try:
        # Более точные паттерны для поиска директора в реальном формате
        director_patterns = [
            # Формат: "Директор чата: Имя" (с двоеточием)
            r'Директор чата:\s*([^\n]+?)(?:\s*\n|$)',
            # Формат: "**Директор чата** - Имя"
            r'\*\*Директор чата\*\*\s*[-–—]\s*([^\n]+?)(?:\s*\n|$)',
            # Формат: "Директор чата - Имя"
            r'Директор чата\s*[-–—]\s*([^\n]+?)(?:\s*\n|$)',
            # Общий поиск с разными вариантами
            r'(?:^|\n)\s*\*?\*?Директор(?:\s+чата)?\*?\*?\s*[:\-–—]\s*([^\n]+?)(?:\s*\n|$)',
        ]
        
        for pattern in director_patterns:
            match = re.search(pattern, top_summary_text, re.IGNORECASE | re.MULTILINE)
            if match:
                director_info = match.group(1).strip()
                # Убираем лишние символы и форматирование
                director_info = re.sub(r'[*_`]', '', director_info)
                # Убираем восклицательные знаки в конце
                director_info = re.sub(r'[!.]*$', '', director_info).strip()
                
                if director_info:  # Проверяем что информация не пустая
                    logger.info(f"Найден директор: {director_info}")
                    return director_info
        
        # Дополнительная отладка - выводим текст для анализа
        logger.warning("Директор не найден в тексте")
        logger.debug(f"Анализируемый текст топа: {top_summary_text[:500]}...")
        return None
        
    except Exception as e:
        logger.error(f"Ошибка при извлечении информации о директоре: {e}")
        return None

def clean_director_info_for_prompt(director_info):
    """
    Очищает информацию о директоре от потенциально запрещенных слов
    """
    # Список слов которые могут вызвать отказ OpenAI
    forbidden_words = [
        'лох', 'лоха', 'лохом', 'лохи',
        'дурак', 'дурака', 'дураком', 'дураки',
        'идиот', 'идиота', 'идиотом', 'идиоты',
        'кретин', 'кретина', 'кретином', 'кретины',
        'тупой', 'тупого', 'тупым', 'тупые',
        'глупый', 'глупого', 'глупым', 'глупые',
        'мудак', 'мудака', 'мудаком', 'мудаки',
        'гей', 'гея', 'геем', 'геи'
    ]
    
    cleaned_info = director_info.lower()
    
    # Заменяем запрещенные слова на нейтральные
    for word in forbidden_words:
        if word in cleaned_info:
            cleaned_info = cleaned_info.replace(word, 'участник')
    
    # Убираем оскорбительные описания и заменяем на нейтральные
    replacements = {
        'опущенный': 'скромный',
        'бздых': 'отдыхающий',
        'душнила': 'аналитик',
        'писюшка': 'молодой',
        'жертва': 'помощник'
    }
    
    for bad_word, good_word in replacements.items():
        cleaned_info = cleaned_info.replace(bad_word, good_word)
    
    return cleaned_info.strip()

def generate_photo_prompt(director_info):
    """
    Генерирует промпт для создания фотографии директора на основе его описания
    """
    try:
        # Очищаем и подготавливаем входной текст
        clean_info = clean_director_info_for_prompt(director_info)
        
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system", 
                    "content": """
Основывайся на описании человека и создай КРАТКИЙ английский промпт (максимум 50 слов) для генерации изображения."""
                },
                {
                    "role": "user", 
                    "content": f"Создай промпт для фотографии человека на основе этого описания:\n\n{clean_info}"
                }
            ],
            temperature=0.7,
            max_tokens=200
        )
        
        prompt = response.choices[0].message.content.strip()
        
        # Добавляем технические детали для качества
        enhanced_prompt = f"{prompt}, professional corporate headshot, business suit, serious expression, studio lighting, neutral background, direct eye contact, confident pose, high resolution, iPhone photo quality, realistic, 8k"
        
        logger.info(f"Сгенерирован промпт для фото: {enhanced_prompt}")
        return enhanced_prompt
        
    except Exception as e:
        if "can't assist" in str(e).lower() or "i can't" in str(e).lower():
            logger.warning(f"OpenAI отказался генерировать промпт, пробуем без запрещенных слов: {e}")
            
            # Пробуем еще раз с очищенным текстом
            try:
                clean_info = clean_director_info_for_prompt(director_info)
                
                response = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system", 
                            "content": "Создай промпт для официального корпоративного портрета делового человека. Избегай любых проблематичных слов."
                        },
                        {
                            "role": "user", 
                            "content": f"Создай краткий английский промпт для официального фото на основе: {clean_info}"
                        }
                    ],
                    temperature=0.5,
                    max_tokens=100
                )
                
                prompt = response.choices[0].message.content.strip()
                enhanced_prompt = f"{prompt}, professional corporate headshot, business suit, serious expression, studio lighting, high resolution"
                
                logger.info(f"Сгенерирован резервный промпт: {enhanced_prompt}")
                return enhanced_prompt
                
            except Exception as e2:
                logger.error(f"Ошибка при повторной генерации промпта: {e2}")
        
        logger.error(f"Ошибка генерации промпта: {e}")
        
        # Fallback промпт максимально официальный
        return "Professional corporate executive headshot, serious businessman in dark suit, confident direct gaze, studio lighting, neutral office background, formal pose, high resolution, realistic, 8k quality"

def generate_director_photo(top_summary_text):
    """
    Основная функция генерации фото директора
    Возвращает путь к созданному изображению или None
    """
    try:
        # Извлекаем информацию о директоре
        director_info = extract_director_info(top_summary_text)
        if not director_info:
            logger.warning("Информация о директоре не найдена")
            return None
        
        # Генерируем промпт
        prompt = generate_photo_prompt(director_info)
        if not prompt:
            logger.error("Не удалось сгенерировать промпт")
            return None
        
        # Генерируем изображение через Replicate
        logger.info("Генерируем изображение через Replicate...")
        input_data = {"prompt": prompt}
        output = replicate.run(REPLICATE_IMAGE_MODEL, input=input_data)
        
        # Создаем имя файла
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_filename = f"director_photo_{timestamp}"
        
        # Сохраняем изображение
        image_path = save_director_image(output, base_filename)
        
        if image_path:
            logger.info(f"Фото директора сохранено: {image_path}")
            return image_path
        else:
            logger.error("Не удалось сохранить изображение")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка генерации фото директора: {e}")
        return None

def save_director_image(output, base_name):
    """
    Сохраняет изображение и возвращает путь к файлу
    """
    try:
        # Создаем папку если её нет
        os.makedirs("director_photos", exist_ok=True)
        
        image_bytes = None
        
        # Обработка результата в зависимости от типа
        if isinstance(output, list) and output:
            # Для моделей возвращающих список URL
            url = output[0]
            response = requests.get(url)
            response.raise_for_status()
            image_bytes = response.content
        elif hasattr(output, 'read'):
            # Для моделей возвращающих объект с методом read()
            image_bytes = output.read()
        else:
            logger.error(f"Неподдерживаемый тип вывода от Replicate: {type(output)}")
            return None
        
        if image_bytes:
            # Определяем тип изображения
            image_type = imghdr.what(None, h=image_bytes)
            final_filename = f"director_photos/{base_name}.{image_type or 'jpg'}"
            
            # Сохраняем файл
            with open(final_filename, "wb") as file:
                file.write(image_bytes)
            
            return final_filename
        
    except Exception as e:
        logger.error(f"Ошибка сохранения изображения: {e}")
        return None

# Тестовая функция
def test_director_photo_generation():
    """
    Тестовая функция для проверки работы генератора
    """
    test_summary = """
    ТОП-3 Участников:
    **Иван**
    Тестовое прозвище - тестовое описание

    Директор чата: Ало!
    Почему: Этот человек явно задает тон беседы, даже если это не всегда умно. Например, он первым открыл тему общения.
    """
    
    print("Тестируем генерацию фото директора с реальным форматом...")
    photo_path = generate_director_photo(test_summary)
    if photo_path:
        print(f"Тест прошел успешно! Фото сохранено: {photo_path}")
    else:
        print("Тест не прошел - фото не создано")

def test_extraction_patterns():
    """
    Тестирует извлечение директора из разных форматов
    """
    test_cases = [
        "Директор чата: Ало!",
        "**Директор чата** - Петя",
        "Директор чата - Вася",
        "Директор чата: Лох Петрович",
        """ТОП участников:
        
        Директор чата: Иван Иванович
        Почему: Потому что он директор
        
        Лох чата: Петя"""
    ]
    
    print("Тестируем извлечение директора из разных форматов:")
    for i, test_case in enumerate(test_cases, 1):
        director = extract_director_info(test_case)
        print(f"{i}. Найден директор: '{director}' в тексте: '{test_case[:50]}...'")

def test_cleaning_function():
    """
    Тестирует функцию очистки текста
    """
    test_cases = [
        "Лох Петрович",
        "Директор-дурак компании", 
        "Гей чата",
        "Опущенный участник",
        "Душнила аналитик"
    ]
    
    print("Тестируем функцию очистки текста:")
    for test_case in test_cases:
        cleaned = clean_director_info_for_prompt(test_case)
        print(f"'{test_case}' -> '{cleaned}'")

if __name__ == "__main__":
    # Тестируем извлечение паттернов
    test_extraction_patterns()
    print("\n" + "="*30 + "\n")
    # Тестируем очистку текста
    test_cleaning_function()
    print("\n" + "="*50 + "\n")
    # Запуск тестирования генерации фото
    test_director_photo_generation() 