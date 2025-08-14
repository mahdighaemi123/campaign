import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI
from pydantic import BaseModel
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
import dotenv

# Load environment variables
dotenv.load_dotenv()

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class Config:
    """Configuration class to store all environment variables"""
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    MONGODB_URL = os.getenv(
        "MONGODB_URL", "mongodb://mongo_user:mongo_pass@localhost:27017/campaign_pilot?authSource=admin")
    DATABASE_NAME = os.getenv("DATABASE_NAME")
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
    # New: Telegram group ID for unanswered questions
    ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))

    # OpenAI settings
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "2000"))
    OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
    MAX_HISTORY = int(os.getenv("MAX_HISTORY", "12"))


# Constants
VOICE_MAP = {
    "1": "689c96879af3fd1e57fc869d",  # ۱:هزینه آموزش اصلی چنده؟
    # ۲:من هیچی از ادمینی بلد نیستم این فرصت برای منم مناسبه؟
    "2": "689c969e9af3fd1e57fc869e",
    # ۳: من آموزش ادمین ثروت ساز رو دارم ولی ندیدم اصلا، برای من هم مناسبه؟
    "3": "689c96ba9af3fd1e57fc869f",
    # ۴: بعد دیدن آموزش استخدام میشیم؟ تضمینیه استخداممون؟
    "4": "689c96d39af3fd1e57fc86a0",
    # ۵:فرصت های شغلی این حیطه دور کاری هستن؟  و با گوشی موبایل انجام میشه؟
    "5": "689c971a9af3fd1e57fc86a1",
    # ۶:من کارفرما هستم، این آموزش برای من هم مناسبه؟
    "6": "689c97359af3fd1e57fc86a2",
    # ۷:برخی از کوچ های اینستاگرام میگن باید به مخاطب گپ و گفت داشته باشی متن های آماده نفرستی
    "7": "689c97549af3fd1e57fc86a3",
    # ۸:وقتی این شاخه رو یاد بگیریم، اشتراک دایرکتم رو باید خودمون تهیه کنیم یا کارفرما؟
    "8": "689c977a9af3fd1e57fc86a4",
    # ۹:من آموزش های دیگرتون رو دیدم و استخدام نشدم یعنی توی این شاخه میتونم استخدام بشم؟
    "9": "689c979a9af3fd1e57fc86a5",
    # ۱۰:چقدر طول میکشه آموزش اصلی رو یاد بگیریم؟
    "10": "689c97ac9af3fd1e57fc86a6",
    # ۱۱:حقوق این شاخه چقدره؟
    "11": "689c97c09af3fd1e57fc86a7",
    # ۱۲:شما از این ۹ ابزار دایرکتم ک تک ب تک میخواین توضیح بدید  ما چطور میتونیم تمرین کنیم ک قشنگ تسلط داشته باشیم؟
    "12": "689c97cc9af3fd1e57fc86a8",
    # ۱۳ : شرایط اقساط دارید؟
    "13": "689da04125b0ece6b34a9d9a",
    # ۱۴:شانس پروژه گرفتنم بین ادمینایی که چند ساله دارن کار میکنن و سابقه و رزومه دارن پایین نیست؟
    "14": "689c980f9af3fd1e57fc86aa",
    # ۱۵:چند ساعت در روز وقتم رو میگیره؟
    "15": "۱۵:چند ساعت در روز وقتم رو میگیره؟",

    # میتونم بعدا ثبت نام کنم؟
    "16": "689c98409af3fd1e57fc86ac",

    # بچه دارم تایمم محدوده میتونم یاد بگیرم؟ پروژه میدید بهم؟
    "17": "689c984c9af3fd1e57fc86ad",

    # برای دانشجوهای قبلی تخفیف یا معرفی اختصاصی تری هست؟
    "18": "689c986b9af3fd1e57fc86ae",

    # توضیح کامل
    "19": "689cbd1a8ab715f165cfd04b",
}


class StatusResponse(BaseModel):
    status: str


class SingleResponse(BaseModel):
    response: str
    voice: str
    response_id: str
    matched_question: str
    question: str
    confidence: float
    skip: bool


class MultipleResponse(BaseModel):
    responses: List[SingleResponse]


class DatabaseManager:
    """Handles all database operations"""

    def __init__(self):
        self.client = AsyncIOMotorClient(Config.MONGODB_URL)
        self.db = self.client[Config.DATABASE_NAME]
        self.messages = self.db.business_messages
        self.chats = self.db.business_chats
        self.ready_messages = self.db.ready_messages

    async def close(self):
        """Close database connection"""
        self.client.close()

    async def get_messages(self, business_connection_id: str, chat_id: int) -> List[Dict]:
        """Get recent messages from a chat"""
        cursor = self.messages.find(
            {"business_connection_id": business_connection_id, "chat_id": chat_id}
        ).sort([("timestamp", -1)]).limit(Config.MAX_HISTORY)

        result = await cursor.to_list(length=Config.MAX_HISTORY)
        return result[::-1]  # Reverse to get chronological order

    async def is_message_exist(self, chat_id: int, business_connection_id: str, stored_message_id: str) -> bool:
        """Check if a stored message was already sent to this chat"""
        try:
            doc = await self.messages.find_one({
                "chat_id": chat_id,
                "business_connection_id": business_connection_id,
                "ai_response_data.stored_message_id": stored_message_id
            })

            if doc:
                logger.info(
                    f"Found duplicate stored message {stored_message_id} in chat {chat_id}")
                return True

            return False
        except Exception as e:
            logger.error(f"Error checking message existence: {e}")
            return False

    async def update_message_telegram_id(self, doc_id: ObjectId, telegram_message_id: int) -> bool:
        """Update message document with Telegram message ID"""
        try:
            result = await self.messages.update_one(
                {"_id": doc_id},
                {"$set": {"message_id": telegram_message_id,
                          "updated_at": datetime.utcnow()}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating message telegram ID: {e}")
            return False

    async def save_ai_response(self, chat_id: int, business_connection_id: str,
                               text: str, ai_data: Dict, message_type: str = "ai_response") -> Optional[ObjectId]:
        """Save AI response to messages collection"""
        try:
            message_doc = {
                "chat_id": chat_id,
                "business_connection_id": business_connection_id,
                "text": text,
                "timestamp": datetime.utcnow(),
                "date": datetime.utcnow(),
                "is_ai_response": True,
                "ai_response_type": message_type,
                "ai_response_data": ai_data,
                "from": {
                    "id": "ai_assistant",
                    "is_bot": True,
                    "first_name": "AI Assistant",
                    "username": "ai_bot"
                },
                "message_id": None,  # Will be updated after sending
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }

            result = await self.messages.insert_one(message_doc)
            logger.info(f"Saved AI response to database: {result.inserted_id}")
            return result.inserted_id

        except Exception as e:
            logger.error(f"Error saving AI response: {e}")
            return None

    async def get_unread_chat(self) -> List[Dict]:
        """Get unread messages and atomically tag them as AI_SEEN"""

        one_minute_ago = datetime.utcnow() - timedelta(minutes=10)

        try:
            result = await self.chats.find_one_and_update(
                {"status": {"$eq": "NEW"},
                 "last_message_date": {"$lte": one_minute_ago}
                 },
                {"$set": {"status": "AI_SEEN", "ai_processed_at": datetime.utcnow()}},
                return_document=True,

                sort=[("timestamp", 1)]
            )
            if result:
                logger.info(
                    f"Tagged chat {result.get('chat_id')} as AI_SEEN")
                return result

        except Exception as e:
            logger.error(f"Error getting unread messages: {e}")
            return None

    async def mark_chat_as_answered(self, chat_id: int, business_connection_id: str):
        """Mark a chat as answered"""
        try:
            result = await self.chats.update_one(
                {"chat_id": chat_id, "business_connection_id": business_connection_id},
                {
                    "$set": {
                        "status": "AI_ANSWERED",
                        "ai_responded_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            logger.info(
                f"Marked chat {chat_id} as AI_ANSWERED: {result.modified_count > 0}")
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error marking chat as answered: {e}")
            return False

    async def mark_chat_as_link(self, chat_id: int, business_connection_id: str):
        """Mark a chat as answered"""
        try:
            result = await self.chats.update_one(
                {"chat_id": chat_id, "business_connection_id": business_connection_id},
                {
                    "$set": {
                        "link": True,
                    }
                }
            )
            logger.info(
                f"Marked chat {chat_id} as LINK: {result.modified_count > 0}")
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error marking chat as answered: {e}")
            return False

    async def is_message_linked(self, chat_id: int, business_connection_id: str) -> bool:
        """Check if a stored message was already sent to this chat"""
        try:

            sended_before = await self.is_message_exist(
                chat_id,
                business_connection_id,
                "689cb213ab59a3ec1b86e6ff"
            )

            if sended_before:
                return True

            doc = await self.chats.find_one({
                "chat_id": chat_id,
                "business_connection_id": business_connection_id
            })

            if doc:
                logger.info(
                    f"Found link message in chat {chat_id}")

                if doc.get("link", False):
                    return True

            return False
        except Exception as e:
            logger.error(f"Error checking message existence: {e}")
            return False

    async def mark_chat_as_forwarded(self, chat_id: int, business_connection_id: str):
        """Mark a chat as forwarded to admin"""
        try:
            result = await self.chats.update_one(
                {"chat_id": chat_id, "business_connection_id": business_connection_id},
                {
                    "$set": {
                        "status": "FORWARDED_TO_ADMIN",
                        "forwarded_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            logger.info(
                f"Marked chat {chat_id} as FORWARDED_TO_ADMIN: {result.modified_count > 0}")
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error marking chat as forwarded: {e}")
            return False

    async def get_stored_message(self, message_id_str: str) -> Optional[Dict]:
        """Get a stored message by ID"""
        try:
            return await self.ready_messages.find_one({"_id": ObjectId(message_id_str)})
        except Exception as e:
            logger.error(f"Error getting stored message {message_id_str}: {e}")
            return None


class OpenAIClient:
    """Handles all OpenAI API calls"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_BASE_URL
        )

        try:
            with open("faq_system_prompt.txt", "r", encoding="utf8") as f:
                self.faq_system_prompt = f.read()
        except FileNotFoundError:
            logger.error("faq_system_prompt.txt not found, using default")
            exit(1)

        try:
            with open("status_system_prompt.txt", "r", encoding="utf8") as f:
                self.status_system_prompt = f.read()
        except FileNotFoundError:
            logger.error("status_system_prompt.txt not found, using default")
            exit(1)

    async def get_status(self, history: List[Dict]) -> tuple[str, Dict]:
        """Get status from AI (first AI check) and return status + full response data"""
        try:
            messages = [
                {"role": "system", "content": self.status_system_prompt},
                *[{"role": msg["role"], "content": msg["content"]} for msg in history]
            ]

            response = await self.client.chat.completions.parse(
                model=Config.OPENAI_MODEL,
                messages=messages,
                max_tokens=Config.OPENAI_MAX_TOKENS,
                temperature=Config.OPENAI_TEMPERATURE,
                response_format=StatusResponse
            )

            status = response.choices[0].message.parsed.status

            # Prepare full response data for storage
            response_data = {
                "status": status,
                "model": Config.OPENAI_MODEL,
                "messages": messages,
                "raw_response": response.model_dump() if hasattr(response, 'model_dump') else str(response),
                "processed_at": datetime.utcnow().isoformat(),
                "response_type": "status_check"
            }

            logger.info(f"Status check result: {status}")
            return status, response_data

        except Exception as e:
            logger.error(f"Status check error: {e}")
            error_data = {
                "status": "skip",
                "error": str(e),
                "processed_at": datetime.utcnow().isoformat(),
                "response_type": "status_check_error"
            }
            return "skip", error_data

    async def get_faq_response(self, history: List[Dict]) -> tuple[MultipleResponse, Dict]:
        """Get FAQ response from AI and return response + full response data"""
        try:
            messages = [
                {"role": "system", "content": self.faq_system_prompt},
                *[{"role": msg["role"], "content": msg["content"]} for msg in history]
            ]

            response = await self.client.chat.completions.parse(
                model=Config.OPENAI_MODEL,
                messages=messages,
                max_tokens=Config.OPENAI_MAX_TOKENS,
                temperature=Config.OPENAI_TEMPERATURE,
                response_format=MultipleResponse
            )

            parsed_response = response.choices[0].message.parsed

            # Prepare full response data for storage
            response_data = {
                "model": Config.OPENAI_MODEL,
                "messages": messages,
                "parsed_response": parsed_response.dict(),
                "raw_response": response.model_dump() if hasattr(response, 'model_dump') else str(response),
                "processed_at": datetime.utcnow().isoformat(),
                "response_type": "faq_response",
                "responses_count": len(parsed_response.responses),
                "valid_responses": len([r for r in parsed_response.responses if not r.skip])
            }

            logger.info(f"FAQ Response: {parsed_response}")
            return parsed_response, response_data

        except Exception as e:
            logger.error(f"FAQ AI error: {e}")
            # error_response = MultipleResponse(
            #     responses=[SingleResponse(
            #         response=f"❌ خطا در پردازش: {str(e)}",
            #         response_id="error",
            #         matched_question="",
            #         confidence=0.0,
            #         voice="",
            #         question="",
            #         skip=True
            #     )]
            # )

            # error_data = {
            #     "error": str(e),
            #     "processed_at": datetime.utcnow().isoformat(),
            #     "response_type": "faq_response_error"
            # }

            # return error_response, error_data

    async def close(self):
        """Close the OpenAI client"""
        await self.client.close()


async def read_business_message(bot, business_connection_id, chat_id, message_id):
    method = "readBusinessMessage"
    data = {
        "business_connection_id": business_connection_id,
        "chat_id": chat_id,
        "message_id": message_id,
    }
    result = await bot._post(method, data=data)
    return result


class MessageSender:
    """Handles sending messages via Telegram Bot"""

    def __init__(self, bot: Bot, db_manager: DatabaseManager):
        self.bot = bot
        self.db_manager = db_manager

    async def send_status_message(self, status: str, chat_id: int, business_connection_id: str, ai_data: Dict) -> bool:
        """Send status-based response and save to database"""

        message = status
        if not message:
            return False

        try:
            # Save to database first
            await self.db_manager.save_ai_response(
                chat_id=chat_id,
                business_connection_id=business_connection_id,
                text=message,
                ai_data=ai_data,
                message_type="status_response"
            )

            await self.bot.send_message(
                chat_id=chat_id,
                text=message,
                business_connection_id=business_connection_id
            )

            logger.info(f"Sent status message '{status}' to chat {chat_id}")
            return True

        except Exception as e:
            logger.error(f"Error sending status message: {e}")
            return False

    async def send_stored_message(self, message_id_str: str, chat_id: int, business_connection_id: str, ai_data: Dict = None) -> bool:
        """Send a stored message from database"""
        try:
            doc = await self.db_manager.get_stored_message(message_id_str)

            if not doc:
                logger.error(f"Message {message_id_str} not found in database")
                return False

            msg_type = doc.get("type")
            caption = doc.get("caption")
            text = caption or f"[{msg_type.upper()} MESSAGE] for [{ai_data.get('status')} STATUS]"

            stored_message_id = message_id_str
            if await self.db_manager.is_message_exist(chat_id, business_connection_id, stored_message_id):
                logger.info("dup message")
                return

            if ai_data:
                await self.db_manager.save_ai_response(
                    chat_id=chat_id,
                    business_connection_id=business_connection_id,
                    text=text,
                    ai_data={**ai_data, "stored_message_id": message_id_str,
                             "original_type": msg_type},
                    message_type="stored_media"
                )

            send_methods = {
                "text": lambda: self.bot.send_message(
                    chat_id=chat_id,
                    text=doc.get("text", ""),
                    business_connection_id=business_connection_id
                ),
                "photo": lambda: self.bot.send_photo(
                    chat_id=chat_id,
                    photo=doc["file_id"],
                    caption=caption,
                    business_connection_id=business_connection_id
                ),
                "video": lambda: self.bot.send_video(
                    chat_id=chat_id,
                    video=doc["file_id"],
                    caption=caption,
                    business_connection_id=business_connection_id
                ),
                "voice": lambda: self.bot.send_voice(
                    chat_id=chat_id,
                    voice=doc["file_id"],
                    # caption=caption,
                    business_connection_id=business_connection_id
                ),
                "document": lambda: self.bot.send_message(
                    chat_id=chat_id,
                    document=doc["file_id"],
                    caption=caption,
                    business_connection_id=business_connection_id
                )
            }

            # if message_id_str == "689c97e69af3fd1e57fc86a9":
            #     self.bot.send_message(
            #         chat_id=chat_id,
            #         text="https://wa.me/989361025864",
            #         business_connection_id=business_connection_id
            #     )

            if msg_type in send_methods:
                await send_methods[msg_type]()
                logger.info(
                    f"Message {message_id_str} sent successfully to chat {chat_id}")
                return True
            else:
                logger.error(f"Unknown message type: {msg_type}")
                return False

        except Exception as e:
            logger.error(f"Error sending stored message {message_id_str}: {e}")
            return False

    async def send_faq_responses(self, responses: MultipleResponse, chat_id: int, business_connection_id: str, ai_data: Dict) -> int:
        """Send FAQ responses and save to database"""
        sent_count = 0

        for idx, response in enumerate(responses.responses):
            if response.skip:
                logger.info(
                    f"Skipping response for chat {chat_id}: No answer found")
                continue

            try:
                # Prepare AI data for this specific response
                response_ai_data = {
                    **ai_data,
                    "status": "faq_response",
                    "response_index": idx,
                    "response_id": response.response_id,
                    "matched_question": response.matched_question,
                    "confidence": response.confidence,
                    "voice": response.voice,
                    "individual_response": response.model_dump()
                }

                if response.voice and response.voice in VOICE_MAP:

                    if response.voice == "13":
                        await self.send_stored_message(
                            "689da05925b0ece6b34a9da6",
                            chat_id,
                            business_connection_id,
                            response_ai_data
                        )

                    # Try to send voice message
                    await self.send_stored_message(
                        VOICE_MAP[response.voice],
                        chat_id,
                        business_connection_id,
                        response_ai_data
                    )

                else:
                    # Save text response to database
                    doc_id = await self.db_manager.save_ai_response(
                        chat_id=chat_id,
                        business_connection_id=business_connection_id,
                        text=response.response,
                        ai_data=response_ai_data,
                        message_type="faq_text"
                    )

                    # Send text response
                    sent_message = await self.bot.send_message(
                        chat_id=chat_id,
                        text=response.response,
                        business_connection_id=business_connection_id
                    )

                    if doc_id and sent_message:
                        await self.db_manager.update_message_telegram_id(doc_id, sent_message.message_id)

                    sent_count += 1

            except Exception as e:
                logger.error(f"Error sending response to chat {chat_id}: {e}")

        return sent_count

    async def forward_to_admin(self, chat_id: int, username: int, name: int, user_question: str, business_connection_id: str) -> bool:
        """Forward unanswered question to admin group and save to database"""
        try:
            if not Config.ADMIN_GROUP_ID:
                logger.warning("ADMIN_GROUP_ID not configured")
                return False

            message = f"""❓ پیام بدون پاسخ:

نام کاربر: {name}
آیدی کاربر: @{username}
آیدی چت: {chat_id}

💬 پیام:
{user_question}"""

            # Save forwarding action to database
            forward_ai_data = {
                "forwarded_to": Config.ADMIN_GROUP_ID,
                "original_question": user_question,
                "forward_type": "admin_group",
                "processed_at": datetime.utcnow().isoformat()
            }

            await self.db_manager.save_ai_response(
                chat_id=chat_id,
                business_connection_id=business_connection_id,
                text="[سوال برای ادمین ارسال شد]",
                ai_data=forward_ai_data,
                message_type="admin_forward"
            )

            await self.bot.send_message(
                chat_id=Config.ADMIN_GROUP_ID,
                text=message,
                parse_mode=ParseMode.HTML
            )

            logger.info(
                f"Forwarded question from chat {chat_id} to admin group")
            return True

        except Exception as e:
            logger.error(f"Error forwarding to admin: {e}")
            return False


class FAQBot:
    """Main bot class that orchestrates the entire flow"""

    def __init__(self):
        self.bot = Bot(token=Config.BOT_TOKEN)
        self.db_manager = DatabaseManager()
        self.ai_client = OpenAIClient()
        self.message_sender = MessageSender(self.bot, self.db_manager)

    async def prepare_history(self, messages: List[Dict]) -> List[Dict]:
        """Prepare message history for AI processing - include both user and AI messages"""
        history = []
        for msg in messages:
            if msg.get("text"):
                if msg.get("is_ai_response", False) or msg.get("from", {}).get("username", False) == Config.ADMIN_USERNAME:
                    history.append({
                        "role": "assistant",
                        "content": msg["text"]
                    })
                else:
                    # User message
                    history.append({
                        "role": "user",
                        "content": msg["text"]
                    })
        return history

    async def get_user_question(self, messages: List[Dict]) -> str:
        """Extract the latest user question (non-AI message)"""
        for msg in reversed(messages):
            if msg.get("text") and not msg.get("is_ai_response", False):
                return msg["text"]
        return "سوال نامشخص"

    async def process_chat(self, chat: Dict):
        """Process a single chat through the complete flow"""
        chat_id = chat['chat_id']

        try:
            username = chat['client_info'].get('username', "-")

        except KeyError:
            username = "-"

        try:
            name = chat['client_info']["first_name"] + \
                " " + chat['client_info']["last_name"]
        except KeyError:
            name = "-"

        business_connection_id = chat['business_connection_id']

        logger.info(f"Processing chat {chat_id}")

        try:
            # Get chat history
            messages = await self.db_manager.get_messages(business_connection_id, chat_id)
            history = await self.prepare_history(messages)

            if not history:
                logger.info(f"No text messages found for chat {chat_id}")
                return

            status, status_ai_data = await self.ai_client.get_status(history)

            print(f"Status for chat {chat_id}: {status}")

            is_link = await self.db_manager.is_message_linked(chat_id, business_connection_id)

            if status == "hi":

                if not is_link:
                    if not (await self.db_manager.is_message_exist(
                        chat_id,
                        business_connection_id,
                        "689dab0bb7b7ce7565a4b28e"
                    )):

                        await self.message_sender.send_stored_message(
                            "689dab0bb7b7ce7565a4b28e",
                            chat_id,
                            business_connection_id,
                            status_ai_data
                        )

                        await read_business_message(
                            self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

                        await self.bot.send_message(
                            chat_id=-1002788857939,
                            text="سلام و روز بخیر گفت" + f"\n @{username}",
                            parse_mode=ParseMode.HTML
                        )
                return
            if status == "inter_view_recived->voice_accept":

                if not is_link:

                    if not (await self.db_manager.is_message_exist(
                        chat_id,
                        business_connection_id,
                        "689cd96471425e1fdede8cc1"
                    )):
                        # await self.message_sender.send_stored_message(
                        #     "689cd96471425e1fdede8cc1",
                        #     chat_id,
                        #     business_connection_id,
                        #     status_ai_data
                        # )

                        await read_business_message(
                            self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

                        await self.bot.send_message(
                            chat_id=-1002788857939,
                            text="مصاحبه تایید شد" + f"\n @{username}",
                            parse_mode=ParseMode.HTML
                        )
                return

            # elif status == "inter_view_again":
            #     if not is_link:
            #         await self.message_sender.send_stored_message(
            #             "689ca7229af3fd1e57fc86cf",
            #             chat_id,
            #             business_connection_id,
            #             status_ai_data
            #         )

            #         await read_business_message(
            #             self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

            #         await self.bot.send_message(
            #             chat_id=-1002788857939,
            #             text="مصاحبه ناقص" + f"\n @{username}",
            #             parse_mode=ParseMode.HTML
            #         )
            #     return

            elif status == "user_name_recvied->send-salam-video-message":
                if not is_link:

                    if not (await self.db_manager.is_message_exist(
                        chat_id,
                        business_connection_id,
                        "689cb9098ab715f165cfd030"
                    )):

                        await self.message_sender.send_stored_message(
                            "689cb9098ab715f165cfd030",
                            chat_id,
                            business_connection_id,
                            status_ai_data
                        )

                        await read_business_message(
                            self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

                        await self.bot.send_message(
                            chat_id=-1002788857939,
                            text="ویس سلام" + f"\n @{username}",
                            parse_mode=ParseMode.HTML
                        )

                return

            elif status == "ready->send_cta_and_link":

                if not is_link:

                    if not (await self.db_manager.is_message_exist(
                        chat_id,
                        business_connection_id,
                        "689cb213ab59a3ec1b86e6ff"
                    )):

                        await self.db_manager.mark_chat_as_link(
                            chat_id,
                            business_connection_id
                        )

                        await self.message_sender.send_stored_message(
                            "689cb213ab59a3ec1b86e6ff",
                            chat_id,
                            business_connection_id,
                            status_ai_data
                        )

                        await read_business_message(
                            self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

                        await self.bot.send_message(
                            chat_id=-1002788857939,
                            text="لینک خرید" + f"\n @{username}",
                            parse_mode=ParseMode.HTML
                        )

                return

            elif status == "interview_first":

                if not is_link:

                    if not (await self.db_manager.is_message_exist(
                        chat_id,
                        business_connection_id,
                        "689cbb648ab715f165cfd042"
                    )):

                        await self.message_sender.send_stored_message(
                            "689cbb648ab715f165cfd042",
                            chat_id,
                            business_connection_id,
                            status_ai_data
                        )

                        await read_business_message(
                            self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

                        await self.bot.send_message(
                            chat_id=-1002788857939,
                            text="اول مصاحبه کن" + f"\n @{username}",
                            parse_mode=ParseMode.HTML
                        )
                return

            # elif status != "skip":
            #     await self.message_sender.send_status_message(
            #         status, chat_id, business_connection_id, status_ai_data
            #     )
            #     await self.db_manager.mark_chat_as_answered(chat_id, business_connection_id)
            #     await read_business_message(
            #         self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

            #     logger.info(f"Handled status '{status}' for chat {chat_id}")
            #     return

            elif status == "skip":
                faq_response, faq_ai_data = await self.ai_client.get_faq_response(history)
                forward_admin_is = False

                has_valid_response = all(
                    not response.skip and response.confidence >= 0.98
                    for response in faq_response.responses
                )

                if has_valid_response and faq_response:
                    for response in faq_response.responses:
                        if not response.skip and response.confidence >= 0.98:
                            # Step 4: Send FAQ responses
                            sent_count = await self.message_sender.send_faq_responses(
                                faq_response, chat_id, business_connection_id, faq_ai_data
                            )
                            await self.db_manager.mark_chat_as_answered(chat_id, business_connection_id)
                            logger.info(
                                f"Sent {sent_count} FAQ responses to chat {chat_id}")

                            await read_business_message(
                                self.bot, business_connection_id, chat_id, messages[-1]['message_id'])

                            await self.bot.send_message(
                                chat_id=-1002788857939,
                                text="سوال متداول" +
                                str(response.voice) + f"\n @{username}",
                                parse_mode=ParseMode.HTML
                            )

                else:
                    forward_admin_is = True

                if forward_admin_is:
                    user_question = await self.get_user_question(messages)
                    await self.message_sender.forward_to_admin(chat_id, username, name, user_question, business_connection_id)
                    await self.db_manager.mark_chat_as_forwarded(chat_id, business_connection_id)
                    logger.info(
                        f"Forwarded unanswered question from chat {chat_id} to admin")

        except Exception as e:
            logger.error(f"Error processing chat {chat_id}: {e}")
            # Mark chat as error for manual handling
            await self.db_manager.chats.update_one(
                {"chat_id": chat_id, "business_connection_id": business_connection_id},
                {"$set": {"status": "AI_ERROR", "error": str(
                    e), "updated_at": datetime.utcnow()}}
            )

    async def one_round(self):
        unread_chat = await self.db_manager.get_unread_chat()
        if unread_chat:
            await self.process_chat(unread_chat)
        else:
            logger.debug("No unread chats found")

    async def run(self):
        """Main bot loop"""
        logger.info("Starting AI FAQ Bot...")

        try:
            while True:
                tasks = [self.one_round() for _ in range(1)]
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(1)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
            await asyncio.sleep(5)  # Wait before retrying
        finally:
            await self.cleanup()

    async def cleanup(self):
        """Clean up resources"""
        try:
            await self.ai_client.close()
            await self.db_manager.close()
            logger.info("Bot shutdown complete")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    async def validate_config(self) -> bool:
        """Validate required configuration"""
        required_configs = [
            ("BOT_TOKEN", Config.BOT_TOKEN),
            ("DATABASE_NAME", Config.DATABASE_NAME),
            ("OPENAI_API_KEY", Config.OPENAI_API_KEY),
        ]

        missing_configs = []
        for config_name, config_value in required_configs:
            if not config_value:
                missing_configs.append(config_name)

        if missing_configs:
            logger.error(
                f"Missing required configuration: {', '.join(missing_configs)}")
            return False

        # Test database connection
        try:
            await self.db_manager.client.admin.command('ping')
            logger.info("Database connection successful")
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            return False

        # Test bot token
        try:
            bot_user = await self.bot.get_me()
            logger.info(f"Bot connection successful: @{bot_user.username}")
        except Exception as e:
            logger.error(f"Bot token validation failed: {e}")
            return False

        return True


async def main():
    """Entry point - Initialize and run the FAQ bot"""
    logger.info("="*50)
    logger.info("🤖 Starting AI FAQ Bot with Message Storage")
    logger.info("="*50)

    try:
        # Initialize bot
        bot = FAQBot()

        # Validate configuration
        logger.info("🔧 Validating configuration...")
        if not await bot.validate_config():
            logger.error("❌ Configuration validation failed. Exiting.")
            return

        logger.info("✅ Configuration validation passed")

        # Log configuration summary
        logger.info(f"📊 Configuration Summary:")
        logger.info(f"   - Database: {Config.DATABASE_NAME}")
        logger.info(f"   - OpenAI Model: {Config.OPENAI_MODEL}")
        logger.info(
            f"   - Admin Group: {'Configured' if Config.ADMIN_GROUP_ID else 'Not Configured'}")
        logger.info(f"   - AI Response Storage: Enabled")

        # Start the bot
        logger.info("🚀 Starting bot main loop...")
        await bot.run()

    except KeyboardInterrupt:
        logger.info("⏹️  Bot stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"💥 Fatal error in main: {e}")
        logger.exception("Full error traceback:")
    finally:
        logger.info("🔚 Bot shutdown complete")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
