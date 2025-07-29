import logging
from datetime import datetime, timedelta, time
import pytz
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler, MessageHandler, filters, JobQueue
from openai import AsyncOpenAI
import uuid
import json
import os
import asyncio
from prompts import SUMMARY_PROMPT, TOP_SUMMARY_PROMPT, CHATGPT_PROMPT, RECENT_SUMMARY_PROMPT
from config import TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, MESSAGE_STORE_FILE, MOSCOW_TIMEZONE
import requests
import tempfile

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

moscow_tz = pytz.timezone(MOSCOW_TIMEZONE)

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

message_store = {}
chat_threads = {}

# Счетчик для батчинга сохранения
save_counter = 0
SAVE_BATCH_SIZE = 10

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
        ["📋 Итоги дня", "🏆 Топ дня"],
        ["❓ Вопрос", "📅 Топ недели"],
        ["🤔 Че у вас тут происходит"]
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
        '📋 Итоги дня - суммаризация за сегодня\n'
        '🏆 Топ дня - рейтинг участников\n'
        '❓ Вопрос - задать вопрос ChatGPT\n'
        '📊 Статистика - информация о чате\n\n'
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
    messages = get_messages(days, chat_id)
    if not messages:
        return f"Нет сообщений для суммаризации за {'день' if days == 1 else f'{days} дней'}."
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за {'день' if days == 1 else f'{days} дней'}:\n\n{messages}"}
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
        response = await client.chat.completions.create(
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
        response = await client.chat.completions.create(
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
    messages = get_messages(days, chat_id)
    if not messages:
        return f"Нет сообщений за {'день' if days == 1 else f'{days} дней'}."
    
    try:
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": TOP_SUMMARY_PROMPT},
                {"role": "user", "content": f"Вот сообщения чата за {'день' if days == 1 else f'{days} дней'}:\n\n{messages}"}
            ],
            temperature=0.7,
            max_tokens=2000
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка при вызове OpenAI API: {e}")
        return f"Произошла ошибка при создании топа участников за {'день' if days == 1 else f'{days} дней'}."

async def get_top_summary_for_date(date_str, chat_id):
    """Получение топа участников за конкретную дату"""
    messages = get_messages_for_date(date_str, chat_id)
    if messages is None:
        return "❌ Неправильный формат даты. Используйте: YYYY-MM-DD (например: 2024-07-20)"
    if not messages:
        return f"Нет сообщений за {date_str}."
    
    try:
        response = await client.chat.completions.create(
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
        # Проверяем кнопки клавиатуры
        if message.text in ["📋 Итоги дня", "🏆 Топ дня", "❓ Вопрос", "📊 Статистика", "🤔 Че у вас тут происходит"]:
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
                    response = await client.chat.completions.create(
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
                transcript = await client.audio.transcriptions.create(
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
    debug_info += f"Размер chat_threads: <code>{len(chat_threads)}</code>"
    
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
    
    if text == "📋 Итоги дня":
        summary = await get_summary(1, chat_id)
        await safe_send_message(update, f"📋 <b>Итоги дня:</b>\n\n{summary}")
        
    elif text == "🏆 Топ дня":
        top_summary = await get_top_summary(1, chat_id)
        await safe_send_message(update, f"🏆 <b>Топ участников дня:</b>\n\n{top_summary}")
        
        # Генерируем и отправляем фото директора
        await send_director_photo(update, context, top_summary)
        
    elif text == "❓ Вопрос":
        await update.message.reply_text(
            "❓ Задайте ваш вопрос:\n\n"
            "Просто напишите сообщение начинающееся с '?' или используйте команду <code>/q ваш вопрос</code>\n\n"
            "Пример: <code>? Как дела у всех?</code>",
            parse_mode='HTML'
        )
        
    elif text == "📅 Топ недели":
        top_summary = await get_top_summary(7, chat_id)
        await safe_send_message(update, f"📅 <b>Топ участников недели:</b>\n\n{top_summary}")
        
    elif text == "Че у вас здесь происходит?":
        summary = await get_summary_last_hours(2, chat_id)
        await safe_send_message(update, f"🤔 <b>Что происходило последние 2 часа:</b>\n\n{summary}")

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
                text=f"🏆 <b>Топ участников дня:</b>\n\n{top_summary}",
                parse_mode='HTML'
            )
            
            # Генерируем и отправляем фото директора для ежедневных отчетов
            if PHOTO_GENERATOR_AVAILABLE:
                try:
                    import asyncio
                    loop = asyncio.get_event_loop()
                    photo_path = await loop.run_in_executor(None, generate_director_photo, top_summary)
                    
                    if photo_path and os.path.exists(photo_path):
                        with open(photo_path, 'rb') as photo:
                            await context.bot.send_photo(
                                chat_id=int(chat_id),
                                photo=photo,
                                caption="📸 <b>ФОТО ДИРЕКТОРА ЧАТА</b> 📸",
                                parse_mode='HTML'
                            )
                        
                        # Удаляем временный файл
                        try:
                            os.remove(photo_path)
                        except Exception as e:
                            logger.warning(f"Не удалось удалить временный файл: {e}")
                            
                except Exception as e:
                    logger.error(f"Ошибка генерации фото директора в ежедневном отчете: {e}")
            
        except Exception as e:
            logger.error(f"Ошибка при отправке отчета в чат {chat_id}: {e}")
    
    # Очищаем память после отправки отчетов
    cleanup_chat_threads()

async def manual_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /sum для ручной суммаризации"""
    chat_id = str(update.message.chat_id)
    summary = await get_summary(1, chat_id)
    await safe_send_message(update, f"📋 <b>Итоги дня:</b>\n\n{summary}")

async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /top для топа участников дня"""
    chat_id = str(update.message.chat_id)
    top_summary = await get_top_summary(1, chat_id)
    await safe_send_message(update, f"🏆 <b>Топ участников дня:</b>\n\n{top_summary}")
    
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
        response = await client.chat.completions.create(
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
