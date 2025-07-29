import replicate
import os
import requests
import imghdr
import tempfile
from datetime import datetime
from openai import OpenAI
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Импортируем конфиг
try:
    from config import REPLICATE_API_TOKEN, REPLICATE_IMAGE_MODEL, OPENAI_API_KEY_EXTENDED
    replicate_api_token = REPLICATE_API_TOKEN
    REPLICATE_IMAGE_MODEL = REPLICATE_IMAGE_MODEL
    openai_api_key = OPENAI_API_KEY_EXTENDED
    logger.info("Загружен конфиг из config.py")
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

def enhance_prompt(user_prompt):
    """
    Улучшает пользовательский промпт для лучшего качества изображения
    """
    try:
        if not openai_client:
            # Если OpenAI недоступен, просто добавляем базовые улучшения
            enhanced_prompt = f"{user_prompt}, high quality, detailed, realistic, 8k resolution"
            return enhanced_prompt
        
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system", 
                    "content": """Ты улучшаешь промпты для генерации изображений. 
                    
                    Твоя задача - взять пользовательский промпт и добавить технические детали для улучшения качества изображения.
                    
                    Добавляй:
                    - Детали качества (high quality, detailed, realistic)
                    - Разрешение (8k, high resolution)
                    - Стиль (professional, well-lit)
                    - Но НЕ меняй основную идею пользователя
                    
                    Возвращай только улучшенный промпт на английском языке."""
                },
                {
                    "role": "user", 
                    "content": f"Улучши этот промпт для генерации изображения: {user_prompt}"
                }
            ],
            temperature=0.3,
            max_tokens=200
        )
        
        enhanced_prompt = response.choices[0].message.content.strip()
        logger.info(f"Улучшен промпт: {enhanced_prompt}")
        return enhanced_prompt
        
    except Exception as e:
        logger.error(f"Ошибка улучшения промпта: {e}")
        # Fallback - просто добавляем базовые улучшения
        return f"{user_prompt}, high quality, detailed, realistic, 8k resolution"

def generate_photo(user_prompt):
    """
    Основная функция генерации фото по промпту
    Возвращает путь к созданному изображению или None
    """
    try:
        # Улучшаем промпт
        enhanced_prompt = enhance_prompt(user_prompt)
        if not enhanced_prompt:
            logger.error("Не удалось улучшить промпт")
            return None
        
        # Генерируем изображение через Replicate
        logger.info("Генерируем изображение через Replicate...")
        input_data = {"prompt": enhanced_prompt}
        output = replicate.run(REPLICATE_IMAGE_MODEL, input=input_data)
        
        # Создаем имя файла
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_filename = f"generated_photo_{timestamp}"
        
        # Сохраняем изображение
        image_path = save_generated_image(output, base_filename)
        
        if image_path:
            logger.info(f"Фото сохранено: {image_path}")
            return image_path
        else:
            logger.error("Не удалось сохранить изображение")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка генерации фото: {e}")
        return None

def save_generated_image(output, base_name):
    """
    Сохраняет изображение и возвращает путь к файлу
    """
    try:
        # Создаем папку если её нет
        os.makedirs("generated_photos", exist_ok=True)
        
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
            final_filename = f"generated_photos/{base_name}.{image_type or 'jpg'}"
            
            # Сохраняем файл
            with open(final_filename, "wb") as file:
                file.write(image_bytes)
            
            return final_filename
        
    except Exception as e:
        logger.error(f"Ошибка сохранения изображения: {e}")
        return None

# Тестовая функция
def test_photo_generation():
    """
    Тестовая функция для проверки работы генератора
    """
    test_prompt = "A cute cat sitting on a chair"
    
    print("Тестируем генерацию фото...")
    photo_path = generate_photo(test_prompt)
    if photo_path:
        print(f"Тест прошел успешно! Фото сохранено: {photo_path}")
    else:
        print("Тест не прошел - фото не создано")

if __name__ == "__main__":
    # Запуск тестирования генерации фото
    test_photo_generation() 