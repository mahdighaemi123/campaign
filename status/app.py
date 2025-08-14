import asyncio
import csv
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from openai import AsyncOpenAI
from pydantic import BaseModel
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
    MONGODB_URL = os.getenv(
        "MONGODB_URL", "mongodb://mongo_user:mongo_pass@localhost:27017/campaign_pilot?authSource=admin")
    DATABASE_NAME = os.getenv("DATABASE_NAME")

    # OpenAI settings
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "2000"))
    OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
    MAX_HISTORY = int(os.getenv("MAX_HISTORY", "12"))


class FollowUpStatusResponse(BaseModel):
    status: str  # "send_followup", "no_followup", "already_responded"
    reason: str
    confidence: float


class DatabaseManager:
    """Handles all database operations"""

    def __init__(self):
        self.client = AsyncIOMotorClient(Config.MONGODB_URL)
        self.db = self.client[Config.DATABASE_NAME]
        self.messages = self.db.business_messages
        self.chats = self.db.business_chats

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

    async def get_chats_with_links(self, hours_ago: int = 24) -> List[Dict]:
        """Get chats that received links but haven't responded in specified hours"""
        cutoff_time = datetime.utcnow() - timedelta(hours=hours_ago)
        
        try:
            cursor = self.chats.find({
                "link": True,
                "last_message_date": {"$lte": cutoff_time}
            }).sort([("last_message_date", 1)])  # Oldest first
            
            result = await cursor.to_list(length=None)
            logger.info(f"Found {len(result)} chats with links older than {hours_ago} hours")
            return result
            
        except Exception as e:
            logger.error(f"Error getting chats with links: {e}")
            return []

    async def has_user_responded_after_link(self, business_connection_id: str, chat_id: int) -> tuple[bool, Optional[datetime], Optional[str]]:
        """Check if user has sent any message after receiving the link and return details"""
        try:
            # Find the last link message sent by AI
            link_message = await self.messages.find_one(
                {
                    "business_connection_id": business_connection_id,
                    "chat_id": chat_id,
                    "ai_response_data.stored_message_id": "689cb213ab59a3ec1b86e6ff"  # Link message ID
                },
                sort=[("timestamp", -1)]
            )
            
            if not link_message:
                return False, None, "No link message found"
            
            link_timestamp = link_message.get("timestamp")
            
            # Check if there are any user messages after the link
            user_message_after = await self.messages.find_one(
                {
                    "business_connection_id": business_connection_id,
                    "chat_id": chat_id,
                    "timestamp": {"$gt": link_timestamp},
                    "is_ai_response": {"$ne": True}
                },
                sort=[("timestamp", -1)]
            )
            
            if user_message_after:
                return True, user_message_after.get("timestamp"), user_message_after.get("text", "")[:50]
            
            return False, link_timestamp, "No response after link"
            
        except Exception as e:
            logger.error(f"Error checking user response after link: {e}")
            return False, None, f"Error: {str(e)}"

    async def get_last_user_message(self, business_connection_id: str, chat_id: int) -> Optional[Dict]:
        """Get the last message sent by user (not AI)"""
        try:
            return await self.messages.find_one(
                {
                    "business_connection_id": business_connection_id,
                    "chat_id": chat_id,
                    "is_ai_response": {"$ne": True}
                },
                sort=[("timestamp", -1)]
            )
        except Exception as e:
            logger.error(f"Error getting last user message: {e}")
            return None


class OpenAIClient:
    """Handles all OpenAI API calls for follow-up analysis"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=Config.OPENAI_API_KEY,
            base_url=Config.OPENAI_BASE_URL
        )

        # System prompt for follow-up analysis
        self.followup_system_prompt = """
شما یک دستیار تحلیلگر هستید که باید وضعیت کاربران پس از دریافت لینک خرید را بررسی کنید.

تحلیل کنید:
1. آیا کاربر بعد از دریافت لینک هیچ پاسخی نداده است؟
2. آیا کاربر سوال جدیدی پرسیده یا نگرانی ابراز کرده؟
3. آیا کاربر علاقه‌مندی نشان داده اما خرید نکرده؟
4. آیا کاربر قبلاً پاسخ مثبت یا منفی داده است؟
5. آیا کاربر در حال خرید است یا سوالات مربوط به خرید دارد؟

وضعیت‌های ممکن:
- "send_followup": کاربر هیچ پاسخی نداده یا علاقه دارد اما هنوز خرید نکرده
- "no_followup": کاربر پاسخ منفی داده یا علاقه‌ای ندارد
- "already_responded": کاربر پاسخ مثبت داده یا در حال خرید است یا سوالات مربوط به خرید دارد

confidence بین 0.0 تا 1.0 باشد.
reason را به فارسی بنویسید و دلیل تصمیم‌تان را توضیح دهید.
"""

    async def analyze_followup_need(self, history: List[Dict]) -> tuple[FollowUpStatusResponse, Dict]:
        """Analyze if user needs a follow-up message"""
        try:
            messages = [
                {"role": "system", "content": self.followup_system_prompt},
                *[{"role": msg["role"], "content": msg["content"]} for msg in history]
            ]

            response = await self.client.chat.completions.parse(
                model=Config.OPENAI_MODEL,
                messages=messages,
                max_tokens=Config.OPENAI_MAX_TOKENS,
                temperature=Config.OPENAI_TEMPERATURE,
                response_format=FollowUpStatusResponse
            )

            parsed_response = response.choices[0].message.parsed

            response_data = {
                "status": parsed_response.status,
                "reason": parsed_response.reason,
                "confidence": parsed_response.confidence,
                "model": Config.OPENAI_MODEL,
                "processed_at": datetime.utcnow().isoformat(),
                "response_type": "followup_analysis"
            }

            return parsed_response, response_data

        except Exception as e:
            logger.error(f"Follow-up analysis error: {e}")
            error_response = FollowUpStatusResponse(
                status="error",
                reason=f"خطا در تحلیل: {str(e)}",
                confidence=0.0
            )
            error_data = {
                "error": str(e),
                "processed_at": datetime.utcnow().isoformat(),
                "response_type": "followup_analysis_error"
            }
            return error_response, error_data

    async def close(self):
        """Close the OpenAI client"""
        await self.client.close()


class FollowUpAnalyzer:
    """Main analyzer class that processes chats and generates CSV report"""

    def __init__(self):
        self.db_manager = DatabaseManager()
        self.ai_client = OpenAIClient()

    async def prepare_history(self, messages: List[Dict]) -> List[Dict]:
        """Prepare message history for AI processing"""
        history = []
        for msg in messages:
            if msg.get("text"):
                if msg.get("is_ai_response", False):
                    history.append({
                        "role": "assistant",
                        "content": msg["text"]
                    })
                else:
                    history.append({
                        "role": "user",
                        "content": msg["text"]
                    })
        return history

    async def analyze_chat(self, chat: Dict) -> Dict:
        """Analyze a single chat and return results"""
        chat_id = chat['chat_id']
        business_connection_id = chat['business_connection_id']

        try:
            # Extract user info
            client_info = chat.get('client_info', {})
            username = client_info.get('username', '-')
            first_name = client_info.get('first_name', '')
            last_name = client_info.get('last_name', '')
            name = f"{first_name} {last_name}".strip() or '-'
            phone = client_info.get('phone_number', '-')

            # Get chat timestamps
            created_at = chat.get('created_at', '')
            last_message_date = chat.get('last_message_date', '')

            # Check if user responded after link
            has_responded, response_time, last_response = await self.db_manager.has_user_responded_after_link(
                business_connection_id, chat_id
            )

            # Get last user message
            last_user_msg = await self.db_manager.get_last_user_message(business_connection_id, chat_id)
            last_user_text = last_user_msg.get('text', '')[:100] if last_user_msg else ''

            # Get messages for AI analysis
            messages = await self.db_manager.get_messages(business_connection_id, chat_id)
            history = await self.prepare_history(messages)

            # AI Analysis
            if history:
                analysis, _ = await self.ai_client.analyze_followup_need(history)
                ai_status = analysis.status
                ai_reason = analysis.reason
                ai_confidence = analysis.confidence
            else:
                ai_status = "no_history"
                ai_reason = "تاریخچه پیام موجود نیست"
                ai_confidence = 0.0

            return {
                'chat_id': chat_id,
                'username': username,
                'name': name,
                'phone': phone,
                'created_at': created_at.strftime('%Y-%m-%d %H:%M:%S') if isinstance(created_at, datetime) else str(created_at),
                'last_message_date': last_message_date.strftime('%Y-%m-%d %H:%M:%S') if isinstance(last_message_date, datetime) else str(last_message_date),
                'has_responded_after_link': 'بله' if has_responded else 'خیر',
                'response_time': response_time.strftime('%Y-%m-%d %H:%M:%S') if response_time and isinstance(response_time, datetime) else str(response_time or ''),
                'last_response_preview': str(last_response or ''),
                'last_user_message': last_user_text,
                'ai_status': ai_status,
                'ai_reason': ai_reason,
                'ai_confidence': ai_confidence,
                'followup_sent': 'بله' if chat.get('followup_sent', False) else 'خیر',
                'status': chat.get('status', ''),
                'business_connection_id': business_connection_id
            }

        except Exception as e:
            logger.error(f"Error analyzing chat {chat_id}: {e}")
            return {
                'chat_id': chat_id,
                'error': str(e),
                'username': '-',
                'name': '-',
                'phone': '-',
                'created_at': '',
                'last_message_date': '',
                'has_responded_after_link': 'خطا',
                'response_time': '',
                'last_response_preview': '',
                'last_user_message': '',
                'ai_status': 'error',
                'ai_reason': f'خطا در تحلیل: {str(e)}',
                'ai_confidence': 0.0,
                'followup_sent': '',
                'status': '',
                'business_connection_id': business_connection_id
            }

    async def generate_report(self, hours_ago: int = 24, output_file: str = None) -> str:
        """Generate CSV report of follow-up analysis"""
        if output_file is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = f'followup_analysis_{timestamp}.csv'

        logger.info(f"Starting follow-up analysis for chats older than {hours_ago} hours...")

        try:
            # Get chats that received links
            chats_with_links = await self.db_manager.get_chats_with_links(hours_ago)

            if not chats_with_links:
                logger.info("No chats found that have received links")
                return output_file

            logger.info(f"Analyzing {len(chats_with_links)} chats...")

            results = []
            for i, chat in enumerate(chats_with_links):
                try:
                    logger.info(f"Processing chat {i+1}/{len(chats_with_links)} - ID: {chat.get('chat_id')}")
                    result = await self.analyze_chat(chat)
                    results.append(result)
                    
                    # Add small delay to avoid overwhelming the AI API
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    logger.error(f"Error processing chat {chat.get('chat_id')}: {e}")
                    continue

            # Write to CSV
            if results:
                fieldnames = [
                    'chat_id',
                    'username', 
                    'name',
                    'phone',
                    'created_at',
                    'last_message_date',
                    'has_responded_after_link',
                    'response_time',
                    'last_response_preview',
                    'last_user_message',
                    'ai_status',
                    'ai_reason',
                    'ai_confidence',
                    'followup_sent',
                    'status',
                    'business_connection_id'
                ]

                with open(output_file, 'w', newline='', encoding='utf-8-sig') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(results)

                logger.info(f"✅ Report generated successfully: {output_file}")
                logger.info(f"📊 Total chats analyzed: {len(results)}")
                
                # Print summary statistics
                ai_status_counts = {}
                for result in results:
                    status = result.get('ai_status', 'unknown')
                    ai_status_counts[status] = ai_status_counts.get(status, 0) + 1
                
                logger.info("📈 AI Analysis Summary:")
                for status, count in ai_status_counts.items():
                    logger.info(f"   - {status}: {count} chats")

            else:
                logger.warning("No results to write to CSV")

            return output_file

        except Exception as e:
            logger.error(f"Error generating report: {e}")
            raise

    async def cleanup(self):
        """Clean up resources"""
        try:
            await self.ai_client.close()
            await self.db_manager.close()
            logger.info("Analyzer cleanup complete")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")


async def main():
    """Entry point"""
    logger.info("="*60)
    logger.info("📊 Starting Follow-up Analysis & CSV Generator")
    logger.info("="*60)

    try:
        analyzer = FollowUpAnalyzer()

        # Configuration
        hours_ago = 24  # Analyze chats older than X hours
        output_file = None  # Auto-generate filename with timestamp
        
        # You can specify a custom output file:
        # output_file = "my_followup_report.csv"

        logger.info(f"🔍 Analyzing chats with links older than {hours_ago} hours")
        
        # Generate the report
        report_file = await analyzer.generate_report(hours_ago, output_file)
        
        logger.info("="*60)
        logger.info(f"✅ Analysis completed successfully!")
        logger.info(f"📄 Report saved as: {report_file}")
        logger.info("="*60)

    except KeyboardInterrupt:
        logger.info("⏹️ Analysis stopped by user (Ctrl+C)")
    except Exception as e:
        logger.error(f"💥 Fatal error in main: {e}")
        logger.exception("Full error traceback:")
    finally:
        if 'analyzer' in locals():
            await analyzer.cleanup()
        logger.info("🔚 Analysis shutdown complete")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
