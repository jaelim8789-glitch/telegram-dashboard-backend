import os
import sys
import asyncio

sys.path.insert(0, r'c:\Dev\TeleMon-kiro\telegram-dashboard-backend')
os.environ['DATABASE_URL'] = 'postgresql+asyncpg://telegram_dashboard:telegram_dashboard@localhost:5432/telegram_dashboard'
os.environ['ENCRYPTION_KEY'] = 'ykuBbbZPcjYdt0OPO24BKmfZCJsOQumA0KkxEF5sZlo='
os.environ['TELEGRAM_API_ID'] = '35314984'
os.environ['TELEGRAM_API_HASH'] = '8aa3e9813c6f82dc98a5799c9bfa4e15'

from app.database import async_session_maker
from sqlalchemy import text

async def main() -> None:
    async with async_session_maker() as session:
        result = await session.execute(text('select 1'))
        print(result.scalar())

asyncio.run(main())
