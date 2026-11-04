async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    print(f"BOT CONNECTED: @{me.username}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
