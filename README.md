# Telegram bot for DENKIRS

Бот собирает:
- имя и фамилию;
- номер телефона;
- сферу деятельности из списка.

После заполнения бот проверяет подписку на каналы:
- `@denkirsru`
- `@denkirsceiling`

Если подписки есть, бот записывает данные в Google Sheets.

## Setup

1. Установите Python 3.11+.
2. Создайте виртуальное окружение:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

3. Скопируйте `.env.example` в `.env`.
4. Заполните:
- `BOT_TOKEN` - токен бота;
- `GOOGLE_SHEETS_SPREADSHEET_ID` - id таблицы;
- `GOOGLE_SHEETS_WORKSHEET` - лист, по умолчанию `Leads`;
- `GOOGLE_SERVICE_ACCOUNT_JSON` - JSON service account в одну строку.

## Google Sheets

1. Создайте service account в Google Cloud.
2. Включите Google Sheets API.
3. Скачайте JSON-ключ.
4. Откройте Google Sheet и дайте доступ `Editor` на `client_email` из service account.
5. Возьмите `spreadsheet_id` из URL таблицы.

## Run

```powershell
.venv\Scripts\Activate.ps1
python bot.py
```

## Deploy To Railway

1. Создайте репозиторий на GitHub и загрузите туда этот проект.
2. Перевыпустите токен бота через BotFather, потому что старый уже был раскрыт.
3. Зайдите в [Railway](https://railway.com/) и создайте новый проект через `Deploy from GitHub repo`.
4. Выберите этот репозиторий.
5. В проекте Railway откройте `Variables` и добавьте:
- `BOT_TOKEN`
- `GOOGLE_SHEETS_SPREADSHEET_ID`
- `GOOGLE_SHEETS_WORKSHEET`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
6. Railway сам установит зависимости из `requirements.txt`.
7. Команда запуска берется из `Procfile`: `python bot.py`.
8. После деплоя откройте логи и убедитесь, что бот стартовал без ошибок.

## Railway Notes

- `Procfile` запускает бота как worker-процесс.
- `runtime.txt` фиксирует версию Python.
- Если Google credentials вставляете в Railway, передавайте JSON одной строкой, как в `.env.example`.
- Для проверки подписки бот должен иметь доступ к каналам `@denkirsru` и `@denkirsceiling`.

## Notes

- Бот использует long polling.
- Для проверки подписки бот должен быть админом или иметь доступ к данным каналов.
- Если пользователь не подписан, запись в таблицу не создается.
