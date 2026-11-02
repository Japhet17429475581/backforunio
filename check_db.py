import os
import asyncio
import asyncpg


DATABASE_URL = os.getenv("DATABASE_URL", "")


async def main():
    try:
        conn = await asyncpg.connect(DATABASE_URL)

        result = await conn.fetchval("SELECT 1")

        print("✅ PostgreSQL подключён!")
        print(f"SELECT 1 -> {result}")

        await conn.close()

    except Exception as e:
        print("❌ Ошибка подключения:")
        print("database_connection_failed")


if __name__ == "__main__":
    asyncio.run(main())
