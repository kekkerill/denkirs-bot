import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

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
from gspread.utils import rowcol_to_a1


logging.basicConfig(level=logging.INFO)

ACTIVITY_OPTIONS: Final[list[str]] = [
    "Интернет-продажники",
    "Потолочники",
    "Проектные продажи",
    "Дизайнер/архитектор",
]
REQUIRED_CHANNELS: Final[list[str]] = ["@denkirsru", "@denkirsceiling"]
CHECK_SUBSCRIPTIONS_TEXT: Final[str] = "Проверить подписки"
CONSENT_TEXT: Final[str] = "Я согласен"
PHONE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\+?[0-9()\-\s]{10,20}$")
MOSCOW_TZ: Final[ZoneInfo] = ZoneInfo("Europe/Moscow")
SHEET_HEADERS: Final[list[str]] = [
    "Номер в розыгрыше",
    "Дата регистрации",
    "Telegram ID",
    "Username",
    "Имя и фамилия",
    "Номер телефона",
    "Сфера деятельности",
    "Подписка подтверждена",
]
META_WORKSHEET_NAME: Final[str] = "Meta"
META_HEADERS: Final[list[str]] = ["Параметр", "Значение"]
META_COUNTER_KEY: Final[str] = "Последний номер в розыгрыше"
RAFFLE_NUMBER_HEADER: Final[str] = "Номер в розыгрыше"
CREATED_AT_HEADER: Final[str] = "Дата регистрации"
TELEGRAM_ID_HEADER: Final[str] = "Telegram ID"
USERNAME_HEADER: Final[str] = "Username"
FULL_NAME_HEADER: Final[str] = "Имя и фамилия"
PHONE_NUMBER_HEADER: Final[str] = "Номер телефона"
ACTIVITY_HEADER: Final[str] = "Сфера деятельности"
SUBSCRIPTION_STATUS_HEADER: Final[str] = "Подписка подтверждена"


@dataclass(frozen=True)
class Settings:
    bot_token: str
    spreadsheet_id: str
    worksheet_name: str
    google_credentials_json: str


class RegistrationStates(StatesGroup):
    waiting_for_consent = State()
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_activity = State()
    waiting_for_subscription_check = State()


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


def build_subscription_check_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CHECK_SUBSCRIPTIONS_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def build_consent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CONSENT_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def get_worksheet(settings: Settings):
    credentials = json.loads(settings.google_credentials_json)
    client = gspread.service_account_from_dict(credentials)
    spreadsheet = client.open_by_key(settings.spreadsheet_id)
    try:
        return spreadsheet.worksheet(settings.worksheet_name)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(settings.worksheet_name, rows=1000, cols=10)


def get_meta_worksheet(settings: Settings):
    credentials = json.loads(settings.google_credentials_json)
    client = gspread.service_account_from_dict(credentials)
    spreadsheet = client.open_by_key(settings.spreadsheet_id)
    try:
        return spreadsheet.worksheet(META_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(META_WORKSHEET_NAME, rows=20, cols=2)
        worksheet.append_row(META_HEADERS)
        worksheet.append_row([META_COUNTER_KEY, "0"])
        return worksheet


def normalize_phone_number(phone_number: str) -> str:
    return re.sub(r"\D+", "", phone_number)


def ensure_headers(worksheet) -> list[str]:
    values = worksheet.row_values(1)
    if not values:
        worksheet.append_row(SHEET_HEADERS)
        return SHEET_HEADERS.copy()

    missing_headers = [header for header in SHEET_HEADERS if header not in values]
    if missing_headers:
        updated_headers = values + missing_headers
        end_cell = rowcol_to_a1(1, len(updated_headers))
        worksheet.update(f"A1:{end_cell}", [updated_headers])
        values.extend(missing_headers)

    return values


def ensure_meta_headers(worksheet) -> None:
    values = worksheet.row_values(1)
    if not values:
        worksheet.append_row(META_HEADERS)
        worksheet.append_row([META_COUNTER_KEY, "0"])
        return

    if values != META_HEADERS:
        worksheet.update("A1:B1", [META_HEADERS])

    keys = worksheet.col_values(1)
    if META_COUNTER_KEY not in keys:
        worksheet.append_row([META_COUNTER_KEY, "0"])


def get_existing_entry(
    rows: list[dict],
    telegram_user_id: str,
    phone_number: str,
) -> tuple[int, dict] | None:
    normalized_phone = normalize_phone_number(phone_number)
    for index, row in enumerate(rows, start=2):
        row_user_id = str(row.get(TELEGRAM_ID_HEADER) or "").strip()
        row_phone = normalize_phone_number(str(row.get(PHONE_NUMBER_HEADER) or ""))
        if row_user_id and row_user_id == telegram_user_id:
            return index, row
        if normalized_phone and row_phone and row_phone == normalized_phone:
            return index, row

    return None


def get_next_raffle_number(settings: Settings) -> int:
    worksheet = get_meta_worksheet(settings)
    ensure_meta_headers(worksheet)
    records = worksheet.get_all_records(expected_headers=META_HEADERS)

    for index, row in enumerate(records, start=2):
        if (row.get("key") or "").strip() != META_COUNTER_KEY:
            continue

        raw_value = str(row.get("value") or "").strip()
        current_value = int(raw_value) if raw_value.isdigit() else 0
        next_value = current_value + 1
        worksheet.update_cell(index, 2, str(next_value))
        return next_value

    worksheet.append_row([META_COUNTER_KEY, "1"])
    return 1


def append_lead_sync(settings: Settings, payload: dict) -> int:
    worksheet = get_worksheet(settings)
    headers = ensure_headers(worksheet)
    rows = worksheet.get_all_records(expected_headers=headers)

    existing_entry = get_existing_entry(
        rows,
        payload["telegram_user_id"],
        payload["phone_number"],
    )
    if existing_entry:
        row_index, row_data = existing_entry
        existing_number = str(row_data.get(RAFFLE_NUMBER_HEADER) or "").strip()
        if existing_number.isdigit():
            return int(existing_number)

        fallback_number = get_next_raffle_number(settings)
        worksheet.update_cell(row_index, headers.index(RAFFLE_NUMBER_HEADER) + 1, fallback_number)
        return fallback_number

    raffle_number = get_next_raffle_number(settings)
    worksheet.append_row(
        [
            raffle_number,
            payload[CREATED_AT_HEADER],
            payload[TELEGRAM_ID_HEADER],
            payload[USERNAME_HEADER],
            payload[FULL_NAME_HEADER],
            payload[PHONE_NUMBER_HEADER],
            payload[ACTIVITY_HEADER],
            "Да",
        ]
    )
    return raffle_number


async def append_lead(settings: Settings, payload: dict) -> int:
    return await asyncio.to_thread(append_lead_sync, settings, payload)


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


async def prompt_consent(message: Message, state: FSMContext) -> None:
    await state.set_state(RegistrationStates.waiting_for_consent)
    await message.answer(
        "Нажимая кнопку ниже, вы соглашаетесь на обработку персональных данных и принимаете условия политики конфиденциальности.",
        reply_markup=build_consent_keyboard(),
    )


async def finalize_registration(message: Message, state: FSMContext, bot: Bot, settings: Settings) -> None:
    data = await state.get_data()
    is_subscribed, missing_channels = await check_subscriptions(bot, message.from_user.id)

    if not is_subscribed:
        links = "\n".join(
            f"- https://t.me/{channel.removeprefix('@')}" for channel in missing_channels
        )
        await state.set_state(RegistrationStates.waiting_for_subscription_check)
        await message.answer(
            "Подпишитесь на все каналы и нажмите кнопку проверки:\n"
            f"{links}",
            reply_markup=build_subscription_check_keyboard(),
        )
        return

    payload = {
        CREATED_AT_HEADER: message.date.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M"),
        TELEGRAM_ID_HEADER: str(message.from_user.id),
        USERNAME_HEADER: message.from_user.username or "",
        FULL_NAME_HEADER: data["full_name"],
        PHONE_NUMBER_HEADER: data["phone_number"],
        ACTIVITY_HEADER: data["activity"],
    }
    raffle_number = await append_lead(settings, payload)
    await message.answer(
        f"Заявка принята. Спасибо. Ваш номер в розыгрыше: {raffle_number}",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()


def register_handlers(dp: Dispatcher, settings: Settings) -> None:
    @dp.message(CommandStart())
    async def start_handler(message: Message, state: FSMContext) -> None:
        await prompt_consent(message, state)

    @dp.message(RegistrationStates.waiting_for_consent, F.text == CONSENT_TEXT)
    async def consent_handler(message: Message, state: FSMContext) -> None:
        await prompt_name(message, state)

    @dp.message(RegistrationStates.waiting_for_consent)
    async def invalid_consent_handler(message: Message) -> None:
        await message.answer(
            "Для продолжения нажмите кнопку «Я согласен».",
            reply_markup=build_consent_keyboard(),
        )

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

    @dp.message(RegistrationStates.waiting_for_subscription_check, F.text == CHECK_SUBSCRIPTIONS_TEXT)
    async def subscription_check_handler(message: Message, state: FSMContext, bot: Bot) -> None:
        await finalize_registration(message, state, bot, settings)

    @dp.message(RegistrationStates.waiting_for_subscription_check)
    async def invalid_subscription_check_handler(message: Message) -> None:
        await message.answer(
            "Нажмите кнопку «Проверить подписки», когда подпишетесь на оба канала.",
            reply_markup=build_subscription_check_keyboard(),
        )


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
