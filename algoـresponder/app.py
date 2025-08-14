import asyncio
from telegram import Bot
from telegram.error import TelegramError
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
import os
import dotenv
from bson import ObjectId
from telegram.constants import ParseMode

dotenv.load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://mongo_user:mongo_pass@localhost:27017/campaign_pilot?authSource=admin")
DATABASE_NAME = os.getenv("DATABASE_NAME")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

OFFSET = None

PATTERNS = {
    "test": {
        "type": "text",
        "text": "This is a test message"
    },
    "3": {
        "type": "copy_message",
        "db_id": "6899ca078764655e72983197"
    }
}

bot = Bot(token=BOT_TOKEN)


class DatabaseManager:
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGODB_URL)
        self.db = self.client[DATABASE_NAME]
        self.messages = self.db.business_messages
        self.chats = self.db.business_chats
        self.ready_messages = self.db.ready_messages

    async def close(self):
        """Close database connection"""
        self.client.close()

    async def get_unread_messages(self):
        """Get unread messages and atomically tag them as ALGO_RESPONDER_SEEN"""
        try:
            unread_messages = []

            for _ in range(10):
                result = await self.messages.find_one_and_update(
                    {"algo_handle": {"$eq": False}},
                    {"$set": {"algo_handle": True}},
                    return_document=True,
                    sort=[("timestamp", 1)]
                )

                if result:
                    unread_messages.append(result)
                    logger.info(
                        f"Tagged message {result.get('message_id')} as ALGO_RESPONDER_SEEN")
                else:
                    break

            logger.info(
                f"Found and tagged {len(unread_messages)} unread messages")

            return unread_messages

        except Exception as e:
            logger.error(f"Error getting unread messages: {e}")
            return []


async def send_message_by_id(message_id_str, chat_id, business_connection_id, db_manager):
    """Retrieve a saved message from DB by _id and resend it."""
    try:
        doc = await db_manager.ready_messages.find_one({"_id": ObjectId(message_id_str)})
        if not doc:
            logger.error("message not found in db")
            return False

        msg_type = doc.get("type")
        caption = doc.get("caption")

        if msg_type == "text":
            await bot.send_message(chat_id=chat_id, text=doc.get("text", ""), business_connection_id=business_connection_id)
        
        elif msg_type == "photo":
            await bot.send_photo(chat_id=chat_id, photo=doc["file_id"], caption=caption, business_connection_id=business_connection_id)
        
        elif msg_type == "video":
            await bot.send_video(chat_id=chat_id, video=doc["file_id"], caption=caption, business_connection_id=business_connection_id)
        
        elif msg_type == "voice":
            await bot.send_voice(chat_id=chat_id, voice=doc["file_id"], caption=caption, business_connection_id=business_connection_id)
        
        elif msg_type == "document":
            await bot.send_document(chat_id=chat_id, document=doc["file_id"], caption=caption, business_connection_id=business_connection_id)

        logger.info(f"Message {chat_id} sent successfully.")
        return True

    except Exception as e:
        logger.error(f"Error sending stored message: {e}")
        return False


async def read_business_message(bot, business_connection_id, chat_id, message_id):
    method = "readBusinessMessage"
    data = {
        "business_connection_id": business_connection_id,
        "chat_id": chat_id,
        "message_id": message_id,
    }
    result = await bot._post(method, data=data)
    return result


async def main():
    db_manager = DatabaseManager()
    print("starting bot...")

    try:
        while True:
            messages = await db_manager.get_unread_messages()
            if messages:
                for message in messages:
                    chat_id = message['chat_id']
                    message_id = message['message_id']
                    text = message['text']
                    business_connection_id = message['business_connection_id']
                    logger.info(f"Processing message from chat {chat_id}")

                    pattern = PATTERNS.get(text, None)

                    if not pattern:
                        continue

                    if pattern["type"] == "text":
                        await bot.send_message(
                            chat_id=chat_id,
                            text=pattern["text"],
                            business_connection_id=business_connection_id
                        )

                    elif pattern["type"] == "copy_message":

                        await send_message_by_id(
                            pattern["db_id"],
                            chat_id,
                            business_connection_id,
                            db_manager
                        )

                    await read_business_message(
                        bot, business_connection_id, chat_id, message_id)

                await asyncio.sleep(1)
            else:
                await asyncio.sleep(10)

    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
