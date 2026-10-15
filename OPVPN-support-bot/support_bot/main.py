"""Standalone aiogram entry point for the technical-support bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from support_bot.backend import SupportBackend
from support_bot.config import Settings
from support_bot.core.engine import SupportEngine
from support_bot.core.types import ToolName
from support_bot.handlers import install_commands, register_handlers
from support_bot.opvpn_readonly import OPVPNReadOnlyStore
from support_bot.panel_status_client import OPVPNPanelStatusClient
from support_bot.providers.deepseek_model import DeepSeekSupportModel
from support_bot.providers.yookassa_readonly import YooKassaReadOnlyClient
from support_bot.storage import SupportStore


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level, None)
    if not isinstance(numeric, int):
        raise ValueError(f"Unsupported log level: {level}")
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    store = SupportStore(settings.database_path)
    await store.connect()
    opvpn_store = None
    if settings.opvpn_database_path is not None:
        opvpn_store = OPVPNReadOnlyStore(settings.opvpn_database_path)
        await opvpn_store.connect()
    panel_status_client = None
    if settings.opvpn_panel_api_url and settings.opvpn_panel_api_token:
        panel_status_client = OPVPNPanelStatusClient(
            settings.opvpn_panel_api_url,
            settings.opvpn_panel_api_token,
        )
    model = DeepSeekSupportModel(settings.deepseek_api_key, settings.deepseek_model)
    yookassa_client = None
    if settings.yookassa_shop_id and settings.yookassa_secret_key:
        yookassa_client = YooKassaReadOnlyClient(
            settings.yookassa_shop_id,
            settings.yookassa_secret_key,
        )
    available_tools = {ToolName.CREATE_SUPPORT_TICKET}
    if opvpn_store is not None:
        available_tools.update({
            ToolName.FIND_USER_ORDERS,
            ToolName.GET_PAYMENT_STATUS,
            ToolName.GET_SUBSCRIPTION_STATUS,
            ToolName.CHECK_VPN_KEY,
        })
    engine = SupportEngine(
        model=model,
        backend=SupportBackend(store, opvpn_store, panel_status_client, yookassa_client),
        store=store,
        allowed_tools=frozenset(available_tools),
    )
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp["settings"] = settings
    dp["store"] = store
    dp["engine"] = engine
    register_handlers(dp)

    try:
        me = await bot.get_me()
        await install_commands(bot)
        logging.getLogger(__name__).info(
            "Support bot @%s started; admins=%d", me.username, len(settings.admin_ids)
        )
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await model.close()
        if panel_status_client is not None:
            await panel_status_client.close()
        if yookassa_client is not None:
            await yookassa_client.close()
        if opvpn_store is not None:
            await opvpn_store.close()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
