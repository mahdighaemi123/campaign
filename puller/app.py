import asyncio
from telegram import Bot
from telegram.error import TelegramError
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
import os
import dotenv

dotenv.load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGODB_URL = os.getenv(
    "MONGODB_URL", "mongodb://mongo_user:mongo_pass@localhost:27017/campaign_pilot?authSource=admin")
DATABASE_NAME = os.getenv("DATABASE_NAME")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")

bot = Bot(token=BOT_TOKEN)
OFFSET = None


class DatabaseManager:
    def __init__(self):
        self.client = AsyncIOMotorClient(MONGODB_URL)
        self.db = self.client[DATABASE_NAME]
        self.messages = self.db.business_messages
        self.chats = self.db.business_chats
        self.ready_messages = self.db.ready_messages

    def is_admin(self, user_dict):
        """Check if user is admin by username"""
        if not user_dict or not user_dict.get("username"):
            return False
        return user_dict["username"].lower() == ADMIN_USERNAME.lower()

    def get_sender_type(self, business_message):
        """Determine sender type: 'client' or 'admin'"""
        from_user = business_message.get("from")
        if not from_user:
            return "unknown"
        if self.is_admin(from_user):
            return "admin"
        return "client"

    async def save_message(self, business_message):
        """Save business message to database"""
        try:
            sender_type = self.get_sender_type(business_message)

            message_doc = {
                "message_id": business_message.get("message_id"),
                "business_connection_id": business_message.get("business_connection_id"),
                "chat_id": business_message["chat"]["id"],
                "text": business_message.get("text"),
                "date": datetime.utcfromtimestamp(business_message.get("date", 0)),
                "timestamp": datetime.utcnow(),
                "sender_type": sender_type,
                "algo_handle": False,
                "from_user": business_message.get("from")
            }

            await self.messages.insert_one(message_doc)
            await self.update_chat_status(business_message, sender_type)

            logger.info(f"Saved message from {sender_type}")
            return True

        except Exception as e:
            logger.error(f"Error saving message: {e}")
            return False

    async def update_chat_status(self, business_message, sender_type):
        """Update chat status based on sender type"""
        try:
            chat_id = business_message["chat"]["id"]
            business_connection_id = business_message.get(
                "business_connection_id")

            if sender_type == "client":
                status = "NEW"
            elif sender_type == "admin":
                status = "RESPONDED"
            else:
                status = "UNKNOWN"

            chat_doc = {
                "chat_id": chat_id,
                "business_connection_id": business_connection_id,
                "status": status,
                "last_message_date": datetime.utcfromtimestamp(business_message.get("date", 0)),
                "updated_at": datetime.utcnow()
            }

            if sender_type == "client" and business_message.get("from"):
                chat_doc["client_info"] = business_message["from"]

            await self.chats.update_one(
                {"chat_id": chat_id, "business_connection_id": business_connection_id},
                {"$set": chat_doc},
                upsert=True
            )

            logger.info(f"Updated chat status to: {status}")

        except Exception as e:
            logger.error(f"Error updating chat status: {e}")

    async def save_ready_message(self, message):
        """Save normal (non-business) Telegram messages to the DB."""
        try:
            msg_type = None
            file_id = None
            file_unique_id = None
            caption = message.get("caption")

            # Detect file types
            if "text" in message:
                msg_type = "text"
                content = message.get("text")
                
            elif "photo" in message:
                msg_type = "photo"
                largest_photo = message["photo"][-1]  # largest resolution
                file_id = largest_photo["file_id"]
                file_unique_id = largest_photo["file_unique_id"]
                content = None

            elif "video" in message:
                msg_type = "video"
                file_id = message["video"]["file_id"]
                file_unique_id = message["video"]["file_unique_id"]
                content = None

            elif "voice" in message:
                msg_type = "voice"
                file_id = message["voice"]["file_id"]
                file_unique_id = message["voice"]["file_unique_id"]
                content = None

            elif "document" in message:
                msg_type = "document"
                file_id = message["document"]["file_id"]
                file_unique_id = message["document"]["file_unique_id"]
                content = None

            else:
                msg_type = "unknown"
                content = None

            message_doc = {
                "message_id": message.get("message_id"),
                "chat_id": message["chat"]["id"],
                "type": msg_type,
                "text": content,
                "caption": caption,
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "date": datetime.utcfromtimestamp(message.get("date", 0)),
                "timestamp": datetime.utcnow(),
                "from_user": message.get("from")
            }

            result = await self.ready_messages.insert_one(message_doc)
            logger.info(
                f"Saved standard message type={msg_type} with _id={result.inserted_id}")
            return str(result.inserted_id)

        except Exception as e:
            logger.error(f"Error saving standard message: {e}")
            return None

    async def close(self):
        self.client.close()


async def handle_business_message(business_message, db_manager):
    """Handle incoming business messages from a dict"""
    try:
        sender_type = db_manager.get_sender_type(business_message)
        chat_id = business_message["chat"]["id"]

        logger.info(f"Message from {sender_type} in chat {chat_id}...")

        # Save message
        saved = await db_manager.save_message(business_message)
        if not saved:
            logger.error("Failed to save message to database")

        # Auto reply to client
        # if sender_type == "client":
        #     await bot.send_message(
        #         chat_id=chat_id,
        #         text="پیام شما دریافت شد و به زودی پاسخ داده خواهد شد.",
        #         business_connection_id=business_message.get(
        #             "business_connection_id")
        #     )

    except Exception as e:
        logger.error(f"Error handling business message: {e}")

    except Exception as e:
        logger.error(f"Error saving standard message: {e}")
        return None


async def handle_message(message, db_manager):
    """Handle standard messages from a dict"""
    try:
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]

        db_id = await db_manager.save_ready_message(message)

        reply_text = (
            f"chat_id= {chat_id}\n"
            f"message_id= {message_id}\n"
            f"db_id= {db_id}\n"
        )

        await bot.send_message(
            chat_id=chat_id,
            text=reply_text,
            reply_to_message_id=message_id
        )

    except Exception as e:
        logger.error(f"Error handling message: {e}")


async def process_update(update, db_manager):
    """Process updates fully in dict form"""
    try:
        update_dict = update.to_dict()
        logger.info(f"Processing update dict: {update_dict}")

        if "message" in update_dict:
            await handle_message(update_dict["message"], db_manager)

        elif "business_message" in update_dict:
            await handle_business_message(update_dict["business_message"], db_manager)

    except Exception as e:
        logger.error(f"Error processing update: {e}")


async def main():
    global OFFSET
    db_manager = DatabaseManager()
    logger.info(f"🤖 Business bot started - Admin: @{ADMIN_USERNAME}")

    try:
        while True:
            try:
                updates = await bot.get_updates(
                    offset=OFFSET,
                    limit=10,
                    timeout=10,
                    allowed_updates=["message", "business_message"]
                )

                if updates:
                    await asyncio.gather(*[
                        process_update(update, db_manager) for update in updates
                    ])
                    OFFSET = updates[-1].update_id + 1
                else:
                    await asyncio.sleep(1)

            except TelegramError as e:
                logger.error(f"Telegram error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                await asyncio.sleep(10)

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        await db_manager.close()
        logger.info("🛑 Bot shut down")


if __name__ == "__main__":
    asyncio.run(main())
