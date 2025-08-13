# pip install python-telegram-bot openai setuptools --upgrade

from openai import AsyncOpenAI
import asyncio
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
import logging
import json
import os
from typing import Set, Optional


# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

BOT_TOKEN = "376050674:AAGAAEdj2uVc9e0QTXMY5ujgaCQ2ARSJpnQ"
OPENAI_API_KEY = "tpsg-9yjVBxGxNAWf0NdI3Ko9nkE3lzFtiSz"
OPENAI_API_BASE = "https://api.tapsage.com/openai/v1"

bot = Bot(token=BOT_TOKEN)
OFFSET = None

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_API_BASE,
)


class Storage:
    def __init__(self, vip_path="data/vip_users.json", offset_path="data/last_offset.json", memory_path="data/memory.json"):
        self.vip_path = vip_path
        self.offset_path = offset_path
        self.memory_path = memory_path

        os.makedirs("data", exist_ok=True)

    async def load_offset(self) -> Optional[int]:
        try:
            with open(self.offset_path, "r", encoding="utf8") as f:
                return json.load(f).get("offset")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    async def save_offset(self, offset: int):
        with open(self.offset_path, "w", encoding="utf8") as f:
            json.dump({"offset": offset}, f, indent=2)

    def load_memory(self):
        if os.path.exists(self.memory_path):
            try:
                with open(self.memory_path, "r", encoding="utf8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}
        return {}

    def save_memory(self, data):
        with open(self.memory_path, "w", encoding="utf8") as f:
            json.dump(data, f, indent=2)


async def get_ai_response(user_text):
    """Get response from OpenAI API with user context"""
    try:
        system_prompt = """You are a helpful AI assistant."""

        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ],
            max_tokens=500,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI API Error: {e}")
        return f"❌ Sorry, I'm having trouble processing your request right now. Error: {str(e)}"


async def safe_send_chat_action(chat_id, action="typing", business_connection_id=None):
    """Safely send chat action, handling business message limitations"""
    try:
        if business_connection_id:
            await bot.send_chat_action(
                chat_id=chat_id,
                action=action,
                business_connection_id=business_connection_id
            )
        else:
            await bot.send_chat_action(chat_id=chat_id, action=action)
    except TelegramError as e:
        if "Business_peer_invalid" in str(e) or "peer_invalid" in str(e).lower():
            logger.warning(
                f"Chat action not supported for this business chat: {e}")
        else:
            logger.error(f"Chat action error: {e}")


async def handle_message(message):
    """Handle regular messages"""
    chat_id = message.chat.id
    message_id = message.message_id
    user_text = message.text

    text = f"""
    chat_id: {chat_id}
    message_id: {message_id}
    user_text: {user_text}
    """

    print(text)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_to_message_id=message.message_id
    )

    # try:
    #     await bot.send_message(
    #         chat_id=chat_id,
    #         text=reply,
    #         parse_mode=ParseMode.MARKDOWN,
    #         reply_to_message_id=message.message_id
    #     )
    # except TelegramError as e:
    #     logger.error(f"Error sending regular message: {e}")
    #     try:
    #         await bot.send_message(
    #             chat_id=chat_id,
    #             text=reply,
    #             reply_to_message_id=message.message_id
    #         )
    #     except TelegramError as e2:
    #         logger.error(f"Error sending plain message: {e2}")


async def read_business_message(bot, business_connection_id, chat_id, message_id):
    method = "readBusinessMessage"
    data = {
        "business_connection_id": business_connection_id,
        "chat_id": chat_id,
        "message_id": message_id,
    }
    result = await bot._post(method, data=data)
    return result


async def handle_business_message(business_message):
    """Handle business messages (Telegram Business feature)"""
    chat_id = business_message.chat.id
    user_text = business_message.text
    business_connection_id = business_message.business_connection_id
    message_id = business_message.message_id

    logger.info(f"Business message from chat {chat_id}: {user_text[:50]}...")

    reply = await get_ai_response(user_text)

    await bot.send_message(
        chat_id=chat_id,
        text=reply,
        parse_mode=ParseMode.MARKDOWN,
        business_connection_id=business_connection_id,
    )

    await read_business_message(
        bot=bot,
        chat_id=chat_id,
        message_id=message_id,
        business_connection_id=business_connection_id
    )

    logger.info(
        f"Successfully sent business message reply to chat {chat_id}")


async def process_update(update):
    """Process different types of updates"""
    try:
        if update.message:
            await handle_message(update.message)
        elif update.business_message and update.business_message.text:
            await handle_business_message(update.business_message)
        elif update.edited_message and update.edited_message.text:
            logger.info("Edited message received - ignoring")
        else:
            logger.debug(f"Unhandled update type: {type(update)}")
    except Exception as e:
        logger.error(f"Error processing update: {e}")


async def pull_updates():
    global OFFSET

    logger.info("🤖 Bot started - listening for messages...")

    while True:
        try:
            updates = await bot.get_updates(
                offset=OFFSET,
                limit=10,
                timeout=10,
                allowed_updates=["message",
                                 "business_message", "edited_message"]
            )

            if updates:
                await asyncio.gather(*[process_update(update) for update in updates])
                OFFSET = updates[-1].update_id + 1
            else:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Error fetching updates: {e}")
            await asyncio.sleep(10)


async def shutdown():
    """Cleanup function"""
    logger.info("🛑 Shutting down bot...")
    try:
        await client.close()
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(pull_updates())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
    finally:
        asyncio.run(shutdown())
