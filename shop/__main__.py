import asyncio
import contextlib
import logging
import os
from pathlib import Path

from aiogram import Bot
from aiogram.types import BotCommand
from aiohttp import web

from .app import Shop
from .config import Config
from .store import Store


class ProcessLock:
    """Keep a single polling process per persistent data directory."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another bot process is using this database; run one replica only") from None

    def close(self):
        self.file.close()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Avoid dumping raw updates, customer messages, wallets or bot tokens to logs.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    config = Config.from_env()
    process_lock = ProcessLock(config.db_path + ".lock")
    store = Store(config.db_path)
    store.recover_deliveries()
    bot = Bot(config.token)
    shop = Shop(bot, store, config)
    runner = None
    worker = None
    try:
        me = await bot.get_me()
        shop.username = me.username
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="orders", description="Мои заказы"),
                BotCommand(command="support", description="Поддержка"),
                BotCommand(command="terms", description="Условия покупки"),
                BotCommand(command="cancel", description="Отменить ввод"),
                BotCommand(command="id", description="Мой Telegram ID"),
                BotCommand(command="admin", description="Панель администратора"),
            ]
        )
        app = web.Application()

        async def health(request):
            store.db.execute("SELECT 1").fetchone()
            return web.json_response({"ok": True, "service": "aurora-shop"})

        app.router.add_get("/health", health)
        runner = web.AppRunner(app)
        await runner.setup()
        on_railway = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_ENVIRONMENT_ID")
        host = "0.0.0.0" if on_railway else "127.0.0.1"
        await web.TCPSite(runner, host, config.port).start()
        worker = asyncio.create_task(shop.worker())
        logging.info(
            "Shop started as @%s; manual checkout gate=%s", me.username, config.allow_manual_checkout
        )
        await shop.dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query"],
            close_bot_session=False,
            handle_signals=os.name != "nt",
        )
    finally:
        if worker:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        if runner:
            await runner.cleanup()
        await bot.session.close()
        store.close()
        process_lock.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
