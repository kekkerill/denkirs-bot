import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Final

import gspread
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove


logging.basicConfig(level=logging.INFO)

ACTIVITY_OPTIONS: Final[list[str]] = [
    "Интернет-продажники",
    "Потолочники",
    "Проектные продажи",
    "Дизайнер/архитектор",
]
REQUIRED_CHANNELS: Final[list[str]] = ["@denkirsru", "@denkirsceiling"]
PHONE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\+?[0-9()\-\s]{10,20}$")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    spreadsheet_id: str
    worksheet_name: str
    google_credentials_json: str


class RegistrationStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_activity = State()


def load_dotenv() -> None:
    if not os.path.exists(".env"):
        return

    with open(".env", "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def get_settings() -> Settings:
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    worksheet_name = os.getenv("GOOGLE_SHEETS_WORKSHEET", "Leads").strip()
    google_credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    missing = [
        name
        for name, value in {
            "BOT_TOKEN": bot_token,
            "GOOGLE_SHEETS_SPREADSHEET_ID": spreadsheet_id,
            "GOOGLE_SERVICE_ACCOUNT_JSON": google_credentials_json,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    return Settings(
        bot_token=bot_token,
        spreadsheet_id=spreadsheet_id,
        worksheet_name=worksheet_name,
        google_credentials_json=google_credentials_json,
    )


def build_phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отправить номер телефона", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def build_activity_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=option)] for option in ACTIVITY_OPTIONS],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_worksheet(settings: Settings):
    credentials = json.loads(settings.google_credentials_json)
    client = gspread.service_account_from_dict(credentials)
    spreadsheet = client.open_by_key(settings.spreadsheet_id)
    try:
        return spreadsheet.worksheet(settings.worksheet_name)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(settings.worksheet_name, rows=1000, cols=10)


def ensure_headers(worksheet) -> None:
    values = worksheet.row_values(1)
    if values:
        return

    worksheet.append_row(
        [
            "created_at",
            "telegram_user_id",
            "username",
            "full_name",
            "phone_number",
            "activity",
            "subscriptions_ok",
        ]
    )


def append_lead_sync(settings: Settings, payload: dict) -> None:
    worksheet = get_worksheet(settings)
    ensure_headers(worksheet)
    worksheet.append_row(
        [
            payload["created_at"],
            payload["telegram_user_id"],
            payload["username"],
            payload["full_name"],
            payload["phone_number"],
            payload["activity"],
            "yes",
        ]
    )


async def append_lead(settings: Settings, payload: dict) -> None:
    await asyncio.to_thread(append_lead_sync, settings, payload)


async def check_subscriptions(bot: Bot, user_id: int) -> tuple[bool, list[str]]:
    missing_channels: list[str] = []

    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(channel, user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            logging.exception("Failed to check subscription for channel %s", channel)
            missing_channels.append(channel)
            continue

        if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
            missing_channels.append(channel)

    return len(missing_channels) == 0, missing_channels


async def prompt_name(message: Message, state: FSMContext) -> None:
    await state.set_state(RegistrationStates.waiting_for_name)
    await message.answer(
        "Введите имя и фамилию.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def finalize_registration(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    data = await state.get_data()
    is_subscribed, missing_channels = await check_subscriptions(bot, message.from_user.id)

    if not is_subscribed:
        links = "\n".join(
            f"• https://t.me/{channel.removeprefix('@')}" for channel in missing_channels
        )
        await message.answer(
            "Сначала подпишитесь на все каналы, затем нажмите /start и заполните форму заново:\n"
            f"{links}",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    payload = {
        "created_at": message.date.isoformat(),
        "telegram_user_id": str(message.from_user.id),
        "username": message.from_user.username or "",
        "full_name": data["full_name"],
        "phone_number": data["phone_number"],
        "activity": data["activity"],
    }
    await append_lead(settings, payload)
    await message.answer(
        "Заявка принята. Спасибо.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()


def register_handlers(dp: Dispatcher, settings: Settings) -> None:
    @dp.message(CommandStart())
    async def start_handler(message: Message, state: FSMContext) -> None:
        await prompt_name(message, state)

    @dp.message(RegistrationStates.waiting_for_name, F.text)
    async def name_handler(message: Message, state: FSMContext) -> None:
        full_name = message.text.strip()
        if len(full_name.split()) < 2:
            await message.answer("Нужно указать имя и фамилию.")
            return

        await state.update_data(full_name=full_name)
        await state.set_state(RegistrationStates.waiting_for_phone)
        await message.answer(
            "Отправьте номер телефона кнопкой ниже.",
            reply_markup=build_phone_keyboard(),
        )

    @dp.message(RegistrationStates.waiting_for_phone, F.contact)
    async def phone_contact_handler(message: Message, state: FSMContext) -> None:
        await state.update_data(phone_number=message.contact.phone_number)
        await state.set_state(RegistrationStates.waiting_for_activity)
        await message.answer(
            "Выберите, чем вы занимаетесь.",
            reply_markup=build_activity_keyboard(),
        )

    @dp.message(RegistrationStates.waiting_for_phone, F.text)
    async def phone_text_handler(message: Message, state: FSMContext) -> None:
        phone_number = message.text.strip()
        if not PHONE_PATTERN.match(phone_number):
            await message.answer("Введите корректный номер телефона или используйте кнопку отправки контакта.")
            return

        await state.update_data(phone_number=phone_number)
        await state.set_state(RegistrationStates.waiting_for_activity)
        await message.answer(
            "Выберите, чем вы занимаетесь.",
            reply_markup=build_activity_keyboard(),
        )

    @dp.message(RegistrationStates.waiting_for_activity, F.text.in_(ACTIVITY_OPTIONS))
    async def activity_handler(message: Message, state: FSMContext, bot: Bot) -> None:
        await state.update_data(activity=message.text.strip())
        await finalize_registration(message, state, bot, settings)

    @dp.message(RegistrationStates.waiting_for_activity)
    async def invalid_activity_handler(message: Message) -> None:
        await message.answer("Выберите один из вариантов на клавиатуре.")


async def main() -> None:
    settings = get_settings()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    register_handlers(dp, settings)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
