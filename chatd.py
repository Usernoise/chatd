import asyncio
import json
import logging
import os
import tempfile
import requests
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, InlineQueryHandler, filters, ContextTypes, JobQueue
from datetime import datetime, timedelta, time
import pytz
import re
import uuid
from director_analyzer import analyze_director_and_gift, format_gift_message
from song_generator import (
    analyze_chat_and_generate_song,
    generate_music_with_suno,
    check_suno_task_status,
    wait_for_suno_completion,
    format_song_message,
    create_song_from_user_request,
)
from director_photo_generator import generate_director_photo

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Импортируем промпты
try:
    from prompts import SUMMARY_PROMPT, TOP_SUMMARY_PROMPT, CHATGPT_PROMPT, RECENT_SUMMARY_PROMPT
    logger.info("Промпты загружены")
except ImportError:
    # Fallback промпты если файл не найден
    SUMMARY_PROMPT = "Ты анализируешь сообщения чата и создаешь краткую сводку."
    TOP_SUMMARY_PROMPT = "Ты анализируешь сообщения чата и выбираешь топ участников."
    CHATGPT_PROMPT = "Ты полезный ассистент."
    RECENT_SUMMARY_PROMPT = "Ты создаешь краткую сводку недавних сообщений."
    logger.warning("Используются fallback промпты")

# Импортируем конфиг
try:
    from config import TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, MOSCOW_TIMEZONE, MESSAGE_STORE_FILE, AUTO_RESPONSE_INTERVAL
    bot_token = TELEGRAM_BOT_TOKEN
    openai_api_key = OPENAI_API_KEY
    moscow_tz = pytz.timezone(MOSCOW_TIMEZONE)
    logger.info("Загружен конфиг из config.py")
except ImportError:
    bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    openai_api_key = os.getenv('OPENAI_API_KEY', '')
    moscow_tz = pytz.timezone('Europe/Moscow')
    MESSAGE_STORE_FILE = "message_store.json"
    AUTO_RESPONSE_INTERVAL = 20  # Fallback значение
    logger.warning("Используются fallback значения конфига")

# Инициализируем OpenAI клиента
if openai_api_key:
    openai_client = OpenAI(api_key=openai_api_key)
else:
    openai_client = None
    logger.warning("OpenAI API ключ не найден")

# Импортируем генератор фото директора после инициализации логгера
try:
    from director_photo_generator import generate_director_photo
    PHOTO_GENERATOR_AVAILABLE = True
    logger.info("Генератор фото директора подключен")
except ImportError as e:
    PHOTO_GENERATOR_AVAILABLE = False
    logger.warning(f"Генератор фото директора недоступен: {e}")

# Импортируем генератор фото по промпту
try:
    from photo_generator_api import generate_photo
    PHOTO_GENERATOR_API_AVAILABLE = True
    logger.info("API генератор фото подключен")
except ImportError as e:
    PHOTO_GENERATOR_API_AVAILABLE = False
    logger.warning(f"API генератор фото недоступен: {e}")

# Импортируем анализатор директора
try:
    from director_analyzer import analyze_director_and_gift, format_gift_message
    DIRECTOR_ANALYZER_AVAILABLE = True
    logger.info("Анализатор директора подключен")
except ImportError as e:
    DIRECTOR_ANALYZER_AVAILABLE = False
    logger.warning(f"Анализатор директора недоступен: {e}")

# Импортируем генератор песен
try:
    from song_generator import analyze_chat_and_generate_song, format_song_message, generate_music_with_suno
    SONG_GENERATOR_AVAILABLE = True
    logger.info("Генератор песен подключен")
except ImportError as e:
    SONG_GENERATOR_AVAILABLE = False
    logger.warning(f"Генератор песен недоступен: {e}")

message_store = {}
chat_threads = {}

# Счетчик для батчинга сохранения
save_counter = 0
SAVE_BATCH_SIZE = 10

# Счетчик сообщений для автоматических ответов
message_counters = {}

def load_messages_from_file():
    """Загрузка сообщений из файла с обработкой ошибок"""
    if os.path.exists(MESSAGE_STORE_FILE):
        try:
            with open(MESSAGE_STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for chat_id, messages in data.items():
                for message in messages.values():
                    message['timestamp'] = datetime.fromisoformat(message['timestamp'])
            logger.info(f"Загружено сообщений из {len(data)} чатов")
            return data
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Ошибка при загрузке сообщений: {e}")
            # Создаем резервную копию поврежденного файла
            backup_name = f"{MESSAGE_STORE_FILE}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            os.rename(MESSAGE_STORE_FILE, backup_name)
            logger.info(f"Поврежденный файл сохранен как {backup_name}")
            return {}
    else:
        logger.info("Файл хранения сообщений не существует. Начинаем с пустого.")
        return {}

def reset_message_counters():
    """Сброс счетчиков сообщений при перезапуске бота"""
    global message_counters
    message_counters = {}
    logger.info("Счетчики сообщений сброшены")

def save_messages_to_file(force=False):
    """Сохранение сообщений в файл с батчингом"""
    global save_counter
    save_counter += 1
    
    if not force and save_counter < SAVE_BATCH_SIZE:
        return
        
    save_counter = 0
    
    try:
        serializable_data = {
            chat_id: {
                msg_id: {**msg, 'timestamp': msg['timestamp'].isoformat()}
                for msg_id, msg in chat_messages.items()
            }
            for chat_id, chat_messages in message_store.items()
        }
        
        # Атомарная запись через временный файл
        temp_file = f"{MESSAGE_STORE_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(serializable_data, f, ensure_ascii=False, indent=2)
        
        os.replace(temp_file, MESSAGE_STORE_FILE)
        logger.info("Сообщения успешно сохранены")
    except Exception as e:
        logger.error(f"Ошибка при сохранении сообщений: {e}")
        # Удаляем временный файл если он остался
        if os.path.exists(f"{MESSAGE_STORE_FILE}.tmp"):
            os.remove(f"{MESSAGE_STORE_FILE}.tmp")

def cleanup_chat_threads():
    """Очистка старых тредов чата для экономии памяти"""
    for chat_id in list(chat_threads.keys()):
        if len(chat_threads[chat_id]) > 20:  # Оставляем только последние 20 сообщений
            chat_threads[chat_id] = chat_threads[chat_id][:1] + chat_threads[chat_id][-10:]
            logger.info(f"Очищен тред чата {chat_id}")

def get_main_keyboard():
    """Создание основной клавиатуры с кнопками"""
    keyboard = [
         ["📋 Итоги", "🏆 Топ дня", "📅 Топ 7д"],
        ["🤔 Че у вас тут происходит"],
         ["🎁 Подарок", "🎵 Песня дня", "🎶 Заказать песню"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

message_store = load_messages_from_file()

def get_current_time():
    """Получение текущего времени в московской зоне"""
    return datetime.now(moscow_tz)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    keyboard = get_main_keyboard()
    
    await update.message.reply_text(
        'АЛО!!!!!! Я бот  для суммаризации чатов.\n\n'
        '🎯 **Быстрые кнопки:**\n'
        '📋 Итоги - суммаризация за сегодня\n'
        '🏆 Топ дня - рейтинг участников\n'
        '📅 Топ 7д - топ участников недели\n'
        '🤔 Че у вас тут происходит - что происходило последние 2 часа\n'
        '🎁 Подарок - подарок для директора чата\n'
        '🎵 Песня дня - песня на основе событий чата\n\n'
        '⌨️ **Команды:**\n'
        '/sum - итоги дня\n'
        '/top - топ участников дня\n'
        '/week - топ участников недели\n'
        '/date YYYY-MM-DD - итоги за конкретную дату\n'
        '/topdate YYYY-MM-DD - топ участников за дату\n'
        '/q <текст> - вопрос ChatGPT\n'
        '/photo <промпт> - генерация фото\n'
        '/debug - информация о чате\n\n'
        '💡 **Примеры:**\n'
        '/date 2024-07-20\n'
        '/topdate 2024-07-20\n'
        '/photo cute cat sitting on a chair\n\n'
        '🔍 **Inline режим:**\n'
        'Используйте @aisumbot day, week или /photo промпт в любом чате.',
        parse_mode='Markdown',
        reply_markup=keyboard
    )

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка inline запросов"""
    query = update.inline_query.query.strip()
    
    # Проверяем команду /photo
    if query.startswith('/photo '):
        if not PHOTO_GENERATOR_API_AVAILABLE:
            await update.inline_query.answer([
                InlineQueryResultArticle(
                    id='error',
                    title='Ошибка',
                    input_message_content=InputTextMessageContent(
                        "Генератор фото недоступен"
                    )
                )
            ], cache_time=0)
            return
        
        prompt = query[7:].strip()  # Убираем '/photo '
        if not prompt:
            await update.inline_query.answer([
                InlineQueryResultArticle(
                    id='help',
                    title='Помощь',
                    input_message_content=InputTextMessageContent(
                        "Используйте: /photo ваш промпт\n\nПример: /photo cute cat"
                    )
                )
            ], cache_time=300)
            return
        
        try:
            # Генерируем фото в отдельном потоке
            import asyncio
            loop = asyncio.get_event_loop()
            photo_path = await loop.run_in_executor(None, generate_photo, prompt)
            
            if photo_path and os.path.exists(photo_path):
                # Отправляем фото
                with open(photo_path, 'rb') as photo:
                    await update.inline_query.answer([
                        InlineQueryResultArticle(
                            id=str(uuid.uuid4()),
                            title=f'Фото: {prompt[:30]}...',
                            input_message_content=InputTextMessageContent(
                                f"🖼️ Сгенерированное фото по запросу: {prompt}"
                            )
                        )
                    ], cache_time=0)
                
                # Удаляем временный файл
                try:
                    os.remove(photo_path)
                except Exception as e:
                    logger.warning(f"Не удалось удалить временный файл {photo_path}: {e}")
            else:
                await update.inline_query.answer([
                    InlineQueryResultArticle(
                        id='error',
                        title='Ошибка',
                        input_message_content=InputTextMessageContent(
                            "Не удалось сгенерировать фото. Попробуйте другой промпт."
                        )
                    )
                ], cache_time=0)
                
        except Exception as e:
            logger.error(f"Ошибка генерации фото: {e}")
            await update.inline_query.answer([
                InlineQueryResultArticle(
                    id='error',
                    title='Ошибка',
                    input_message_content=InputTextMessageContent(
                        "Произошла ошибка при генерации фото. Попробуйте позже."
                    )
                )
            ], cache_time=0)
        return
    
    # Обработка обычных inline запросов
    query_lower = query.lower()
    if query_lower not in ["day", "week"]:
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id='help',
                title='Доступные команды',
                input_message_content=InputTextMessageContent(
                    "Используйте: day (итоги дня), week (итоги недели) или /photo промпт (генерация фото)"
                )
            )
        ], cache_time=300)
        return

    try:
        days = 1 if query_lower == "day" else 7
        # Получаем chat_id из контекста inline запроса
        # Для inline запросов используем from_user.id как fallback
        chat_id = update.inline_query.chat_type or str(update.inline_query.from_user.id)
        
        summary = await get_summary(days, chat_id)
        title = 'Итоги дня' if query_lower == "day" else 'Итоги недели'
        unique_id = str(uuid.uuid4())
        
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id=unique_id,
                title=title,
                input_message_content=InputTextMessageContent(summary)
            )
        ], cache_time=0)
        
    except Exception as e:
        logger.error(f"Ошибка в inline query: {e}")
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id='error',
                title='Ошибка',
                input_message_content=InputTextMessageContent(
                    "Произошла ошибка при обработке запроса. Попробуйте позже."
                )
            )
        ], cache_time=0)

async def get_summary(days, chat_id):
    """Получение суммаризации сообщений"""
    if days == 1:
        # Для кнопки "Итоги дня" используем сообщения за последние 24 часа
        messages = get_messages_last_hours(24, chat_id)
        period_text = "последние 24 часа"
    else:
        # Для остальных случаев используем обычную логику
        messages = get_messages(days, chat_id)
        period_text = f"{'день' if days == 1 else f'{days} дней'}"
    
    if not messages:
        return f"Нет сообщений для суммаризации за {period_text}."
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за {period_text}:\n\n{messages}"}
            ],
            temperature=0.8,
            max_tokens=2500
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка при вызове OpenAI API: {e}")
        return "Произошла ошибка при создании суммаризации. Попробуйте позже."

async def get_summary_for_date(date_str, chat_id):
    """Получение суммаризации за конкретную дату"""
    messages = get_messages_for_date(date_str, chat_id)
    if messages is None:
        return "❌ Неправильный формат  даты.  Используйте: YYYY-MM-DD (например: 2024-07-20)"
    if not messages:
        return f"Нет сообщений за {date_str}."
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за {date_str}:\n\n{messages}"}
            ],
            temperature=0.8,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка при вызове OpenAI API: {e}")
        return "Произошла ошибка при создании суммаризации. Попробуйте позже."

async def get_summary_last_hours(hours, chat_id):
    """Получение суммаризации за последние N часов"""
    messages = get_messages_last_hours(hours, chat_id)
    if not messages:
        return f"Нет сообщений за последние {hours} часов."
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": RECENT_SUMMARY_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за последние {hours} часов:\n\n{messages}"}
            ],
            temperature=0.8,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка при вызове OpenAI API: {e}")
        return "Произошла ошибка при создании суммаризации. Попробуйте позже."

async def get_top_summary(days, chat_id):
    """Получение топа участников"""
    if days == 1:
        # Для кнопки "Топ дня" используем сообщения за последние 24 часа
        messages = get_messages_last_hours(24, chat_id)
        period_text = "последние 24 часа"
    else:
        # Для остальных случаев используем обычную логику
        messages = get_messages(days, chat_id)
        period_text = f"{'день' if days == 1 else f'{days} дней'}"
    
    if not messages:
        return f"Нет сообщений за {period_text}."
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": TOP_SUMMARY_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за {period_text}:\n\n{messages}"}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка при вызове OpenAI API: {e}")
        return f"Произошла ошибка при создании топа участников за {period_text}."

async def get_top_summary_for_date(date_str, chat_id):
    """Получение топа участников за конкретную дату"""
    messages = get_messages_for_date(date_str, chat_id)
    if messages is None:
        return "❌ Неправильный формат даты. Используйте: YYYY-MM-DD (например: 2024-07-20)"
    if not messages:
        return f"Нет сообщений за {date_str}."
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": TOP_SUMMARY_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за {date_str}:\n\n{messages}"}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка при вызове OpenAI API: {e}")
        return f"Произошла ошибка при создании топа участников за {date_str}."

def get_messages(days, chat_id):
    """Получение сообщений за определенный период"""
    current_time = get_current_time()
    chat_id = str(chat_id)
    
    if chat_id not in message_store:
        return ""
    
    if days == 1:
        # Для дня берем сообщения с начала текущего дня (00:00) до сейчас
        start_time = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = current_time
    else:
        # Для периодов больше дня используем полные дни назад
        start_time = current_time - timedelta(days=days)
        end_time = current_time
    
    relevant_messages = [
        f"{msg['sender']}: {msg['text']}"
        for msg in message_store[chat_id].values()
        if start_time <= msg['timestamp'] <= end_time
    ]
    
    return "\n".join(relevant_messages)

def get_messages_for_date(date_str, chat_id):
    """Получение сообщений за конкретную дату (формат: YYYY-MM-DD)"""
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d')
        target_date = moscow_tz.localize(target_date)
        
        # Диапазон: от 00:00 до 23:59:59 указанной даты
        start_time = target_date
        end_time = target_date + timedelta(days=1) - timedelta(seconds=1)
        
        chat_id = str(chat_id)
        
        if chat_id not in message_store:
            return ""
        
        relevant_messages = [
            f"{msg['sender']}: {msg['text']}"
            for msg in message_store[chat_id].values()
            if start_time <= msg['timestamp'] <= end_time
        ]
        
        return "\n".join(relevant_messages)
    except ValueError:
        return None

def get_messages_last_hours(hours, chat_id):
    """Получение сообщений за последние N часов"""
    current_time = get_current_time()
    chat_id = str(chat_id)
    
    if chat_id not in message_store:
        return ""
    
    # Время начала: текущее время минус N часов
    start_time = current_time - timedelta(hours=hours)
    end_time = current_time
    
    relevant_messages = [
        f"{msg['sender']}: {msg['text']}"
        for msg in message_store[chat_id].values()
        if start_time <= msg['timestamp'] <= end_time
    ]
    
    return "\n".join(relevant_messages)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка обычных сообщений и голосовых"""
    message = update.message
    chat_id = str(message.chat_id)
    
    if message.text and not message.via_bot:
        # Проверяем режим "заказать песню"
        if context.user_data.get('custom_song_wait', {}).get(chat_id):
            user_text = message.text.strip()
            # Снимаем флаг ожидания
            try:
                context.user_data['custom_song_wait'].pop(chat_id, None)
            except Exception:
                pass

            # Обрабатываем создание песни по запросу
            await message.reply_text("🎼 Создаю текст и запускаю генерацию музыки...")
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                song_data = await loop.run_in_executor(None, create_song_from_user_request, user_text)
                if not song_data:
                    await message.reply_text("❌ Не удалось создать песню по вашему запросу.")
                    return

                # Показываем описание
                song_message = format_song_message(song_data)
                await safe_send_message(update, song_message)

                # Запускаем Suno
                await asyncio.sleep(1)
                await update.message.reply_text("🎤 Запускаю генерацию музыки в Suno...")
                suno_result = await loop.run_in_executor(None, generate_music_with_suno, song_data)
                if suno_result and suno_result.get('task_id'):
                    task_id = suno_result['task_id']
                    # Сохраняем задачу для авто-проверки
                    if 'song_tasks' not in context.bot_data:
                        context.bot_data['song_tasks'] = {}
                    context.bot_data['song_tasks'][task_id] = {
                        'song_data': song_data,
                        'chat_id': chat_id,
                    }
                    # Планируем авто-проверку
                    context.job_queue.run_once(
                        check_song_automatically,
                        180,
                        data={'task_id': task_id, 'chat_id': chat_id}
                    )
                    await update.message.reply_text(
                        "⏳ Музыка будет готова примерно через 2-3 минуты. Я пришлю ссылки автоматически.")
                else:
                    await update.message.reply_text("❌ Не удалось отправить задачу в Suno.")
            except Exception as e:
                logger.error(f"Ошибка при заказе песни: {e}")
                await update.message.reply_text("❌ Ошибка при заказе песни. Попробуйте позже.")
            return

        # Проверяем кнопки клавиатуры
        if message.text in ["📋 Итоги", "🏆 Топ дня", "📅 Топ 7д", "🤔 Че у вас тут происходит", "🎁 Подарок", "🎵 Песня дня"]:
            await handle_keyboard_buttons(update, context)
            return
            
        # Проверяем вопросы начинающиеся с "?"
        if message.text.startswith('?'):
            prompt = message.text[1:].strip()
            if prompt:
                # Создаем контекст для ChatGPT
                if chat_id not in chat_threads:
                    chat_threads[chat_id] = [{"role": "system", "content": CHATGPT_PROMPT}]

                chat_threads[chat_id].append({"role": "user", "content": prompt})

                try:
                    response = openai_client.chat.completions.create(
                        model="gpt-4.1-mini",
                        messages=chat_threads[chat_id],
                        temperature=0.7,
                        max_tokens=2500
                    )
                    
                    reply = response.choices[0].message.content
                    chat_threads[chat_id].append({"role": "assistant", "content": reply})
                    
                    # Ограничиваем размер треда
                    if len(chat_threads[chat_id]) > 20:
                        chat_threads[chat_id] = chat_threads[chat_id][:1] + chat_threads[chat_id][-10:]
                        
                    await update.message.reply_text(f"🤖 {reply}")
                    
                except Exception as e:
                    logger.error(f"Ошибка в ChatGPT-запросе: {e}")
                    await update.message.reply_text("❌ Ошибка при обработке запроса. Попробуйте позже.")
                return
        
        # Обычное сохранение текстового сообщения
        if chat_id not in message_store:
            message_store[chat_id] = {}
            
        message_store[chat_id][str(message.message_id)] = {
            'sender': message.from_user.first_name or "Аноним",
            'text': message.text,
            'timestamp': message.date.replace(tzinfo=pytz.UTC).astimezone(moscow_tz)
        }
        save_messages_to_file()
        
        # Проверяем счетчик сообщений для автоматического ответа
        if chat_id not in message_counters:
            message_counters[chat_id] = 0
        
        message_counters[chat_id] += 1
        
        # Если это каждое N-е сообщение и автоответы включены, отправляем его в ChatGPT
        if AUTO_RESPONSE_INTERVAL > 0 and message_counters[chat_id] % AUTO_RESPONSE_INTERVAL == 0:
            logger.info(f"Автоматический ответ на {message_counters[chat_id]}-е сообщение в чате {chat_id}")
            
            # Создаем контекст для ChatGPT
            if chat_id not in chat_threads:
                chat_threads[chat_id] = [{"role": "system", "content": CHATGPT_PROMPT}]

            chat_threads[chat_id].append({"role": "user", "content": message.text})

            try:
                response = openai_client.chat.completions.create(
                    model="gpt-4.1-mini",
                    messages=chat_threads[chat_id],
                    temperature=0.7,
                    max_tokens=2500
                )
                
                reply = response.choices[0].message.content
                chat_threads[chat_id].append({"role": "assistant", "content": reply})
                
                # Ограничиваем размер треда
                if len(chat_threads[chat_id]) > 20:
                    chat_threads[chat_id] = chat_threads[chat_id][:1] + chat_threads[chat_id][-10:]
                    
                await update.message.reply_text(f"🤖 {reply}")
                
            except Exception as e:
                logger.error(f"Ошибка в автоматическом ChatGPT-запросе: {e}")
                await update.message.reply_text("❌ Ошибка при автоматической обработке сообщения.")
        
    elif message.voice:
        # Обработка голосового сообщения
        temp_file = None
        try:
            file = await context.bot.get_file(message.voice.file_id)
            
            # Создаем временный файл
            with tempfile.NamedTemporaryFile(suffix='.oga', delete=False) as temp_file:
                temp_file_path = temp_file.name
            
            # Скачиваем файл
            response = requests.get(file.file_path, timeout=30)
            response.raise_for_status()
            
            with open(temp_file_path, 'wb') as f:
                f.write(response.content)
            
            # Транскрибируем
            with open(temp_file_path, 'rb') as audio_file:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
            
            # Сохраняем транскрипцию
            if chat_id not in message_store:
                message_store[chat_id] = {}
                
            message_store[chat_id][str(message.message_id)] = {
                'sender': message.from_user.first_name or "Аноним",
                'text': f"[Голосовое]: {transcript.text}",
                'timestamp': message.date.replace(tzinfo=pytz.UTC).astimezone(moscow_tz)
            }
            save_messages_to_file()
            
        except Exception as e:
            logger.error(f"Ошибка при обработке голосового сообщения: {e}")
        finally:
            # Удаляем временный файл
            if temp_file and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception as e:
                    logger.error(f"Ошибка при удалении временного файла: {e}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /debug для отладочной информации"""
    chat_id = str(update.message.chat_id)
    messages = message_store.get(chat_id, {})
    message_count = len(messages)
    current_time = get_current_time()
    
    oldest = min(messages.values(), key=lambda x: x['timestamp']) if messages else None
    newest = max(messages.values(), key=lambda x: x['timestamp']) if messages else None
    
    debug_info = "📊 <b>Статистика чата</b>\n\n"
    debug_info += f"Количество сообщений: <code>{message_count}</code>\n"
    
    if oldest:
        debug_info += f"Старейшее: <code>{oldest['timestamp'].strftime('%d.%m.%Y %H:%M')}</code>\n"
    if newest:
        debug_info += f"Новейшее: <code>{newest['timestamp'].strftime('%d.%m.%Y %H:%M')}</code>\n"
        
    debug_info += f"Сейчас: <code>{current_time.strftime('%d.%m.%Y %H:%M')}</code>\n"
    debug_info += f"Размер chat_threads: <code>{len(chat_threads)}</code>\n"
    debug_info += f"Счетчик сообщений: <code>{message_counters.get(chat_id, 0)}</code>\n"
    if AUTO_RESPONSE_INTERVAL > 0:
        next_response = AUTO_RESPONSE_INTERVAL - (message_counters.get(chat_id, 0) % AUTO_RESPONSE_INTERVAL)
        debug_info += f"Следующий автоответ: <code>{next_response}</code>"
    else:
        debug_info += f"Автоответы: <code>отключены</code>"
    
    await update.message.reply_text(debug_info, parse_mode='HTML')

async def send_director_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, top_summary_text: str):
    """
    Генерирует и отправляет фото директора на основе топа дня
    """
    if not PHOTO_GENERATOR_AVAILABLE:
        logger.warning("Генератор фото директора недоступен")
        return
    
    try:
        # Генерируем фото в отдельном потоке чтобы не блокировать бота
        import asyncio
        loop = asyncio.get_event_loop()
        
        # Запускаем генерацию в отдельном потоке
        photo_path = await loop.run_in_executor(None, generate_director_photo, top_summary_text)
        
        if photo_path and os.path.exists(photo_path):
            # Отправляем фото
            with open(photo_path, 'rb') as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption="📸 <b>ФОТО ДИРЕКТОРА ЧАТА</b> 📸",
                    parse_mode='HTML'
                )
            
            # Удаляем временный файл
            try:
                os.remove(photo_path)
                logger.info(f"Временный файл удален: {photo_path}")
            except Exception as e:
                logger.warning(f"Не удалось удалить временный файл {photo_path}: {e}")
                
        else:
            logger.warning("Фото директора не было создано")
            
    except Exception as e:
        logger.error(f"Ошибка при отправке фото директора: {e}")

async def handle_keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки клавиатуры"""
    text = update.message.text
    chat_id = str(update.message.chat_id)
    
    if text == "📋 Итоги":
        summary = await get_summary(1, chat_id)
        await safe_send_message(update, f"📋 <b>Итоги за последние 24 часа:</b>\n\n{summary}")
        
    elif text == "🏆 Топ дня":
        top_summary = await get_top_summary(1, chat_id)
        await safe_send_message(update, f"🏆 <b>Топ участников за последние 24 часа:</b>\n\n{top_summary}")
        
        # Генерируем и отправляем фото директора
        await send_director_photo(update, context, top_summary)
        
    elif text == "📅 Топ 7д":
        top_summary = await get_top_summary(7, chat_id)
        await safe_send_message(update, f"📅 <b>Топ участников недели:</b>\n\n{top_summary}")
        
    elif text == "🤔 Че у вас тут происходит":
        summary = await get_summary_last_hours(2, chat_id)
        await safe_send_message(update, f"🤔 <b>Что происходило последние 2 часа:</b>\n\n{summary}")
        
    elif text == "🎁 Подарок":
        await handle_director_gift(update, context)
        
    elif text == "🎵 Песня дня":
        await handle_song_generation(update, context)
    
    elif text == "🎶 Заказать песню":
        await update.message.reply_text(
            "🎶 Напишите тему или текст песни. Я создам текст и музыку (2-3 минуты).",
            parse_mode='HTML'
        )
        # Маркируем чат в ожидании пользовательского ввода для заказа песни
        if 'custom_song_wait' not in context.user_data:
            context.user_data['custom_song_wait'] = {}
        context.user_data['custom_song_wait'][chat_id] = True

async def handle_director_gift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик генерации подарка для директора чата"""
    if not DIRECTOR_ANALYZER_AVAILABLE:
        await update.message.reply_text("❌ Анализатор директора недоступен")
        return
    
    chat_id = str(update.message.chat_id)
    
    # Отправляем сообщение о начале анализа
    status_message = await update.message.reply_text("🎁 Анализирую директора и создаю подарок...")
    
    try:
        # Анализируем директора в отдельном потоке
        import asyncio
        loop = asyncio.get_event_loop()
        analysis_result = await loop.run_in_executor(None, analyze_director_and_gift, message_store, chat_id)
        
        if analysis_result:
            # Форматируем сообщение с подарком
            gift_message = format_gift_message(analysis_result)
            await safe_send_message(update, gift_message)
            
            # Генерируем фото подарка если доступен генератор фото
            if PHOTO_GENERATOR_API_AVAILABLE and analysis_result.get('gift_photo_prompt'):
                try:
                    photo_prompt = analysis_result['gift_photo_prompt']
                    photo_path = await loop.run_in_executor(None, generate_photo, photo_prompt)
                    
                    if photo_path and os.path.exists(photo_path):
                        # Отправляем фото подарка
                        with open(photo_path, 'rb') as photo:
                            await update.message.reply_photo(
                                photo=photo,
                                caption=f"📸 <b>ФОТО ПОДАРКА ДЛЯ: {analysis_result['director_name']}</b> 📸",
                                parse_mode='HTML'
                            )
                        
                        # Удаляем временный файл
                        try:
                            os.remove(photo_path)
                            logger.info(f"Временный файл удален: {photo_path}")
                        except Exception as e:
                            logger.warning(f"Не удалось удалить временный файл {photo_path}: {e}")
                    else:
                        await update.message.reply_text("❌ Не удалось сгенерировать фото подарка.")
                        
                except Exception as e:
                    logger.error(f"Ошибка генерации фото подарка: {e}")
                    await update.message.reply_text("❌ Ошибка при генерации фото подарка.")
                    
        else:
            await update.message.reply_text("❌ Не удалось проанализировать директора чата. Возможно, недостаточно сообщений за последние 24 часа.")
            
    except Exception as e:
        logger.error(f"Ошибка генерации подарка: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании подарка. Попробуйте позже.")
    finally:
        # Удаляем сообщение о статусе
        try:
            await status_message.delete()
        except:
            pass

async def handle_song_generation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик генерации песни дня"""
    if not SONG_GENERATOR_AVAILABLE:
        await update.message.reply_text("❌ Генератор песен недоступен")
        return
    
    chat_id = str(update.message.chat_id)
    
    # Отправляем сообщение о начале генерации
    status_message = await update.message.reply_text("🎵 Анализирую чат и создаю песню...")
    
    try:
        # Анализируем чат и создаем песню в отдельном потоке
        import asyncio
        loop = asyncio.get_event_loop()
        song_data = await loop.run_in_executor(None, analyze_chat_and_generate_song, message_store, chat_id)
        
        if song_data:
            # Отправляем описание песни
            song_message = format_song_message(song_data)
            await safe_send_message(update, song_message)
            
            # Генерируем музыку через Suno API если доступен
            if song_data.get('lyrics'):
                try:
                    await asyncio.sleep(1)  # Пауза перед генерацией музыки
                    await update.message.reply_text("🎼 Я уже у микрофона, все будет готово через 3 минуты...")
                    
                    suno_result = await loop.run_in_executor(None, generate_music_with_suno, song_data)
                    
                    if suno_result and suno_result.get('task_id'):
                        # Сохраняем задачу для автоматической проверки
                        if 'song_tasks' not in context.bot_data:
                            context.bot_data['song_tasks'] = {}
                        
                        task_id = suno_result['task_id']
                        context.bot_data['song_tasks'][task_id] = {
                            'song_data': song_data,
                            'chat_id': chat_id
                        }
                        
                        # Запускаем автоматическую проверку через 3 минуты
                        context.job_queue.run_once(
                            check_song_automatically,
                            180,  # 3 минуты
                            data={'task_id': task_id, 'chat_id': chat_id}
                        )
                        

                    else:
                        await update.message.reply_text("❌ Не удалось создать музыку через Suno API.")
                        
                except Exception as e:
                    logger.error(f"Ошибка генерации музыки: {e}")
                    await update.message.reply_text("❌ Ошибка при генерации музыки.")
                    
        else:
            await update.message.reply_text("❌ Не удалось создать песню. Возможно, недостаточно сообщений за последние 24 часа.")
            
    except Exception as e:
        logger.error(f"Ошибка генерации песни: {e}")
        await update.message.reply_text("❌ Произошла ошибка при создании песни. Попробуйте позже.")
    finally:
        # Удаляем сообщение о статусе
        try:
            await status_message.delete()
        except:
            pass

async def check_song_automatically(context: ContextTypes.DEFAULT_TYPE):
    """Автоматическая проверка готовности песни через 3 минуты"""
    job_data = context.job.data
    task_id = job_data['task_id']
    chat_id = job_data['chat_id']
    
    try:
        # Импортируем функцию проверки статуса
        from song_generator import check_suno_task_status
        
        # Проверяем статус
        status_result = check_suno_task_status(task_id)
        
        if status_result:
            status = status_result.get('status', 'unknown')
            
            if status == 'SUCCESS':
                # Музыка готова - отправляем в чат
                song_info = context.bot_data.get('song_tasks', {}).get(task_id, {}).get('song_data', {})
                
                # Получаем данные о музыке из response
                response_data = status_result.get('response', {})
                suno_data = response_data.get('sunoData', [])
                
                if suno_data:
                    # Логируем информацию о песне
                    logger.info(f"Отправляем песню: {song_info.get('song_title', 'Песня дня')} - {len(suno_data)} треков")
                    
                    # Отправляем каждый трек с аудио и обложкой
                    for i, track in enumerate(suno_data, 1):
                        try:
                            audio_url = track.get('audioUrl')
                            image_url = track.get('imageUrl')
                            
                            if audio_url and image_url:
                                # Загружаем аудио и обложку
                                audio_response = requests.get(audio_url, timeout=30)
                                image_response = requests.get(image_url, timeout=30)
                                
                                if audio_response.status_code == 200 and image_response.status_code == 200:
                                    # Создаем временные файлы
                                    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as audio_file:
                                        audio_file.write(audio_response.content)
                                        audio_path = audio_file.name
                                    
                                    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as image_file:
                                        image_file.write(image_response.content)
                                        image_path = image_file.name
                                    
                                    # Отправляем аудио с обложкой
                                    current_date = datetime.now().strftime("%d.%m.%y")
                                    
                                    with open(audio_path, 'rb') as audio, open(image_path, 'rb') as image:
                                        await context.bot.send_audio(
                                            chat_id=int(chat_id),
                                            audio=audio,
                                            thumbnail=image,
                                            title=f"{song_info.get('song_title', 'Песня дня')} - Трек {i}",
                                            performer=current_date,
                                            caption=f"🎵 <b>Трек {i}</b>",
                                            parse_mode='HTML'
                                        )
                                    
                                    # Удаляем временные файлы
                                    os.unlink(audio_path)
                                    os.unlink(image_path)
                                    
                                else:
                                    # Если не удалось загрузить файлы, отправляем ссылки
                                    fallback_message = f"🎵 <b>Трек {i}</b>\n"
                                    fallback_message += f"Аудио: {audio_url}\n"
                                    fallback_message += f"Обложка: {image_url}"
                                    
                                    await context.bot.send_message(
                                        chat_id=int(chat_id),
                                        text=fallback_message,
                                        parse_mode='HTML'
                                    )
                                    
                        except Exception as e:
                            logger.error(f"Ошибка отправки трека {i}: {e}")
                            # Отправляем fallback сообщение
                            fallback_message = f"🎵 <b>Трек {i}</b>\n"
                            fallback_message += f"Аудио: {track.get('audioUrl', 'N/A')}\n"
                            fallback_message += f"Обложка: {track.get('imageUrl', 'N/A')}"
                            
                            await context.bot.send_message(
                                chat_id=int(chat_id),
                                text=fallback_message,
                                parse_mode='HTML'
                            )
                else:
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"🎵 <b>Музыка готова!</b> 🎵\n\n"
                             f"Название: <b>{song_info.get('song_title', 'Песня дня')}</b>\n"
                             f"Жанр: <b>{song_info.get('genre', 'Pop')}</b>\n"
                             f"Настроение: <b>{song_info.get('mood', 'Happy')}</b>\n\n"
                             f"Но данные о треках не найдены.",
                        parse_mode='HTML'
                    )
                
                # Удаляем задачу из сохраненных
                if 'song_tasks' in context.bot_data and task_id in context.bot_data['song_tasks']:
                    del context.bot_data['song_tasks'][task_id]
                    
            elif status in ['CREATE_TASK_FAILED', 'GENERATE_AUDIO_FAILED', 'CALLBACK_EXCEPTION', 'SENSITIVE_WORD_ERROR']:
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"❌ <b>Ошибка генерации музыки</b>\n\n"
                         f"Статус: {status}\n"
                         "Попробуйте создать песню заново.",
                    parse_mode='HTML'
                )
                
                # Удаляем задачу из сохраненных
                if 'song_tasks' in context.bot_data and task_id in context.bot_data['song_tasks']:
                    del context.bot_data['song_tasks'][task_id]
                    
            else:
                # Если еще не готово, запускаем еще одну проверку через 30 секунд
                context.job_queue.run_once(
                    check_song_automatically, 
                    30,  # 30 секунд
                    data={'task_id': task_id, 'chat_id': chat_id}
                )
                
        else:
            # Если не удалось получить статус, пробуем еще раз через минуту
            logger.warning(f"Не удалось получить статус для задачи {task_id}")
            context.job_queue.run_once(
                check_song_automatically, 
                60,  # 1 минута
                data={'task_id': task_id, 'chat_id': chat_id}
            )
            
    except Exception as e:
        logger.error(f"Ошибка автоматической проверки песни: {e}")
        # В случае ошибки пробуем еще раз через 2 минуты
        context.job_queue.run_once(
            check_song_automatically, 
            120,  # 2 минуты
            data={'task_id': task_id, 'chat_id': chat_id}
        )

async def send_daily_reports(context: ContextTypes.DEFAULT_TYPE):
    """Отправка ежедневных отчетов"""
    logger.info("Начинаю отправку ежедневных отчетов")
    
    for chat_id in list(message_store.keys()):
        try:
            # Проверяем есть ли сообщения за день
            messages_today = get_messages(1, chat_id)
            if not messages_today.strip():
                continue
                
            top_summary = await get_top_summary(1, chat_id)
            
            await context.bot.send_message(
                chat_id=int(chat_id), 
                text=f"🏆 <b>Топ участников за последние 24 часа:</b>\n\n{top_summary}",
                parse_mode='HTML'
            )
            

            
            # Генерируем и отправляем подарок директору
            if DIRECTOR_ANALYZER_AVAILABLE:
                try:
                    await asyncio.sleep(2)  # Пауза между сообщениями
                    
                    # Анализируем директора и создаем подарок
                    analysis_result = await loop.run_in_executor(None, analyze_director_and_gift, message_store, chat_id)
                    
                    if analysis_result:
                        # Отправляем описание подарка
                        gift_message = format_gift_message(analysis_result)
                        await context.bot.send_message(
                            chat_id=int(chat_id),
                            text=gift_message,
                            parse_mode='HTML'
                        )
                        
                        # Генерируем фото подарка если доступен генератор фото
                        if PHOTO_GENERATOR_API_AVAILABLE and analysis_result.get('gift_photo_prompt'):
                            try:
                                await asyncio.sleep(1)  # Пауза перед фото
                                photo_prompt = analysis_result['gift_photo_prompt']
                                photo_path = await loop.run_in_executor(None, generate_photo, photo_prompt)
                                
                                if photo_path and os.path.exists(photo_path):
                                    # Отправляем фото подарка
                                    with open(photo_path, 'rb') as photo:
                                        await context.bot.send_photo(
                                            chat_id=int(chat_id),
                                            photo=photo,
                                            caption=f"📸 <b>ФОТО ПОДАРКА ДЛЯ: {analysis_result['director_name']}</b> 📸",
                                            parse_mode='HTML'
                                        )
                                    
                                    # Удаляем временный файл
                                    try:
                                        os.remove(photo_path)
                                    except Exception as e:
                                        logger.warning(f"Не удалось удалить временный файл: {e}")
                                        
                            except Exception as e:
                                logger.error(f"Ошибка генерации фото подарка в ежедневном отчете: {e}")
                                
                    else:
                        logger.warning(f"Не удалось проанализировать директора для чата {chat_id}")
                        
                except Exception as e:
                    logger.error(f"Ошибка генерации подарка директора в ежедневном отчете: {e}")
            
        except Exception as e:
            logger.error(f"Ошибка при отправке отчета в чат {chat_id}: {e}")
    
    # Очищаем память после отправки отчетов
    cleanup_chat_threads()

async def manual_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /sum для ручной суммаризации"""
    chat_id = str(update.message.chat_id)
    summary = await get_summary(1, chat_id)
    await safe_send_message(update, f"📋 <b>Итоги за последние 24 часа:</b>\n\n{summary}")

async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /top для топа участников дня"""
    chat_id = str(update.message.chat_id)
    top_summary = await get_top_summary(1, chat_id)
    await safe_send_message(update, f"🏆 <b>Топ участников за последние 24 часа:</b>\n\n{top_summary}")
    
    # Генерируем и отправляем фото директора
    await send_director_photo(update, context, top_summary)

async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /week для топа участников недели"""
    chat_id = str(update.message.chat_id)
    top_summary = await get_top_summary(7, chat_id)
    await safe_send_message(update, f"🏆 <b>Топ участников недели:</b>\n\n{top_summary}")

async def safe_send_message(update, text, parse_mode='HTML'):
    """
    Безопасная отправка сообщения с обработкой ошибок форматирования
    """
    try:
        # Конвертируем Markdown в HTML если нужно
        if '**' in text and parse_mode == 'HTML':
            # Заменяем **текст** на <b>текст</b>
            import re
            text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
        
        await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception as e:
        if "parse entities" in str(e).lower() or "can't parse" in str(e).lower():
            logger.warning(f"Ошибка HTML форматирования, отправляю без форматирования: {e}")
            # Убираем все теги и отправляем как обычный текст
            import re
            clean_text = re.sub(r'<[^>]+>', '', text)
            clean_text = re.sub(r'\*\*(.*?)\*\*', r'\1', clean_text)
            await update.message.reply_text(clean_text)
        else:
            logger.error(f"Ошибка отправки сообщения: {e}")
            await update.message.reply_text("❌ Произошла ошибка при отправке сообщения.")

async def date_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /date для получения итогов за конкретную дату"""
    if not context.args:
        await update.message.reply_text(
            "📅 Укажите дату в формате YYYY-MM-DD\n\n"
            "Пример: <code>/date 2024-07-20</code>",
            parse_mode='HTML'
        )
        return
    
    date_str = context.args[0]
    chat_id = str(update.message.chat_id)
    
    summary = await get_summary_for_date(date_str, chat_id)
    await safe_send_message(update, f"📋 <b>Итоги за {date_str}:</b>\n\n{summary}")

async def topdate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /topdate для получения топа участников за конкретную дату"""
    if not context.args:
        await update.message.reply_text(
            "📅 Укажите дату в формате YYYY-MM-DD\n\n"
            "Пример: <code>/topdate 2024-07-20</code>",
            parse_mode='HTML'
        )
        return
    
    date_str = context.args[0]
    chat_id = str(update.message.chat_id)
    
    top_summary = await get_top_summary_for_date(date_str, chat_id)
    await safe_send_message(update, f"🏆 <b>Топ участников за {date_str}:</b>\n\n{top_summary}")
    
    # Генерируем и отправляем фото директора
    await send_director_photo(update, context, top_summary)

async def chatgpt_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /q для запросов к ChatGPT"""
    chat_id = str(update.message.chat_id)
    prompt = update.message.text[2:].strip()  # убираем /q
    
    if not prompt:
        await update.message.reply_text("❓ Введите вопрос после команды /q\n\nПример: <code>/q Как дела?</code>", parse_mode='HTML')
        return

    if chat_id not in chat_threads:
        chat_threads[chat_id] = [{"role": "system", "content": CHATGPT_PROMPT}]

    chat_threads[chat_id].append({"role": "user", "content": prompt})

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=chat_threads[chat_id],
            temperature=0.7,
            max_tokens=2500
        )
        
        reply = response.choices[0].message.content
        chat_threads[chat_id].append({"role": "assistant", "content": reply})
        
        # Ограничиваем размер треда
        if len(chat_threads[chat_id]) > 20:
            chat_threads[chat_id] = chat_threads[chat_id][:1] + chat_threads[chat_id][-10:]
            
        await update.message.reply_text(reply)
        
    except Exception as e:
        logger.error(f"Ошибка в ChatGPT-запросе: {e}")
        await update.message.reply_text("❌ Ошибка при обработке запроса. Попробуйте позже.")

async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /photo для генерации фото"""
    if not PHOTO_GENERATOR_API_AVAILABLE:
        await update.message.reply_text("❌ Генератор фото недоступен")
        return
    
    prompt = update.message.text[7:].strip()  # убираем /photo
    
    if not prompt:
        await update.message.reply_text(
            "🖼️ Введите промпт после команды /photo\n\n"
            "Пример: <code>/photo cute cat sitting on a chair</code>",
            parse_mode='HTML'
        )
        return
    
    # Отправляем сообщение о начале генерации
    status_message = await update.message.reply_text("🎨 Генерирую фото...")
    
    try:
        # Генерируем фото в отдельном потоке
        import asyncio
        loop = asyncio.get_event_loop()
        photo_path = await loop.run_in_executor(None, generate_photo, prompt)
        
        if photo_path and os.path.exists(photo_path):
            # Отправляем фото
            with open(photo_path, 'rb') as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=f"🖼️ Сгенерированное фото по запросу: {prompt}"
                )
            
            # Удаляем временный файл
            try:
                os.remove(photo_path)
                logger.info(f"Временный файл удален: {photo_path}")
            except Exception as e:
                logger.warning(f"Не удалось удалить временный файл {photo_path}: {e}")
                
        else:
            await update.message.reply_text("❌ Не удалось сгенерировать фото. Попробуйте другой промпт.")
            
    except Exception as e:
        logger.error(f"Ошибка генерации фото: {e}")
        await update.message.reply_text("❌ Произошла ошибка при генерации фото. Попробуйте позже.")
    finally:
        # Удаляем сообщение о статусе
        try:
            await status_message.delete()
        except:
            pass

def main():
    """Основная функция запуска бота"""
    try:
        # Сбрасываем счетчики сообщений при запуске
        reset_message_counters()
        
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Добавляем обработчики
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("debug", debug_command))
        application.add_handler(CommandHandler("sum", manual_summary))
        application.add_handler(CommandHandler("top", top_command))
        application.add_handler(CommandHandler("week", week_command))
        application.add_handler(CommandHandler("date", date_command))
        application.add_handler(CommandHandler("topdate", topdate_command))
        application.add_handler(CommandHandler("q", chatgpt_query))
        application.add_handler(CommandHandler("photo", photo_command))
        application.add_handler(InlineQueryHandler(inline_query))
        # Обработчик текстовых сообщений (включая кнопки и вопросы с "?")
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        application.add_handler(MessageHandler(filters.VOICE, message_handler))

        # Настраиваем ежедневную задачу используя встроенный job_queue
        job_queue = application.job_queue
        daily_time = time(hour=23, minute=59, tzinfo=moscow_tz)
        job_queue.run_daily(send_daily_reports, daily_time, name='daily_reports')
        
        logger.info("Бот и планировщик запущены успешно")
        
        # Сохраняем сообщения при завершении
        try:
            application.run_polling()
        finally:
            save_messages_to_file(force=True)
            logger.info("Сообщения сохранены при завершении")
            
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")
        raise

if __name__ == '__main__':
    main()
