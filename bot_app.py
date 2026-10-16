from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import tempfile
from logging.handlers import RotatingFileHandler

import disnake
from dotenv import load_dotenv
from disnake.ext import commands


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
_instance_lock_handle = None
LOG_DIR = BASE_DIR / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_DIR / "bot.log",
            maxBytes=5_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("task_bot")


def acquire_instance_lock(token: str) -> None:
    """Prevent duplicate local instances, even when launched from different folders."""
    global _instance_lock_handle
    token_fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    lock_path = Path(tempfile.gettempdir()) / f"mgbs-bot-{token_fingerprint}.lock"
    _instance_lock_handle = lock_path.open("a+b")
    _instance_lock_handle.seek(0, os.SEEK_END)
    if _instance_lock_handle.tell() == 0:
        _instance_lock_handle.write(b"\0")
        _instance_lock_handle.flush()
    _instance_lock_handle.seek(0)

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(_instance_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_instance_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        _instance_lock_handle.close()
        _instance_lock_handle = None
        raise SystemExit(
            "MGBS Bot с этим Discord-токеном уже запущен. Остановите предыдущий экземпляр, "
            "в том числе запущенный из другой папки."
        ) from exc


def create_bot() -> commands.InteractionBot:
    from runtime import TaskBot

    sync_flags = commands.CommandSyncFlags.default()
    sync_flags.sync_commands_debug = True
    test_guild_id = os.getenv("DISCORD_TEST_GUILD_ID", "").strip()
    test_guilds = [int(test_guild_id)] if test_guild_id.isdecimal() else None
    return TaskBot(
        intents=disnake.Intents.default(),
        command_sync_flags=sync_flags,
        test_guilds=test_guilds,
    )


def main() -> None:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Не задан DISCORD_TOKEN. Создайте .env по примеру в .env.example или задайте переменную окружения."
        )
    logger.info("Starting bot process: pid=%s, project=%s", os.getpid(), BASE_DIR)
    acquire_instance_lock(token)
    logger.info("Instance lock acquired; duplicate local processes using this token are blocked")
    create_bot().run(token)
