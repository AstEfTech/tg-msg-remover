# Telegram Cleaner

**English** · [Русский](#русский)

A single-file web app (Python backend + embedded Telegram-style UI) that deletes **your own messages** and removes **your own reactions** in a chat you pick. It works as a regular Telegram user client through [Telethon](https://github.com/LonamiWebs/Telethon); everything runs locally on your machine.

## Features

- Chat list with search, avatars and folders (All / Personal / Groups / Channels)
- **My messages** — delete everything you sent to the chat (optionally also for the other person in private chats)
- **My reactions** — remove your reactions from messages in the chat
- Filters: date range and a maximum number of messages
- **Count** button — a dry run that only shows how many items would be affected
- **Scheduled cleanup** — run a cleanup at a chosen time, once or repeating (hourly / daily / weekly / custom), optionally only for items older than N days
- **Telegram bot** — control the same functions from Telegram itself (see below)
- Confirmation dialog, live progress, Stop button, automatic FloodWait handling
- English / Russian interface (switch in the top-right corner), light and dark theme

## Quick start

1. Get `api_id` and `api_hash` at <https://my.telegram.org> → *API development tools*.
2. Install and run (Windows PowerShell):

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install telethon aiohttp
   python tg_cleaner.py
   ```

   Linux / macOS:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install telethon aiohttp
   python tg_cleaner.py
   ```

3. Open the link printed in the console (it contains a one-time token and changes on every launch; the browser opens automatically).
4. Enter api_id / api_hash, your phone number, the login code (and the 2FA password if set).
5. Pick a chat, choose *My messages* or *My reactions*, set filters, press the red button.

Environment variables: `PORT` (default `8080`), `NO_BROWSER=1` (don't open the browser).

## Scheduled cleanup

Set up the cleanup as usual (mode, item limit, "also delete for the other person"), press the clock button, then choose the start time, repeat interval and an optional *older than N days* filter. Manage tasks via the menu → *Scheduled tasks* (run now, pause / resume, delete). Tasks are stored in `tg_cleaner_schedules.json`.

- The app (and your login session) must be running when a task fires. A run missed while the app was closed executes **once** on the next launch.
- Scheduled tasks run **without confirmation** and deletion is irreversible. Pick the filters carefully.
- The minimum repeat interval is 5 minutes. Only one job runs at a time; a due task waits for the current one.
- The fixed *from / to* dates are not used by schedules — use *older than N days*, which stays correct for repeating tasks.

## Telegram bot

The **Bot** button in the bottom-right corner opens the bot panel — a floating window that does not dim the rest of the screen and stays open until you minimize it (the minimize button, or the Bot button again). The bot offers the same functions in a Telegram chat with inline buttons: pick a chat, choose mode / age / limit, count, delete (with a confirmation step), schedule, manage scheduled tasks, stop a running job.

Setup:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. In the web app press **Bot**, paste the token and press *Turn bot on*.
3. The panel shows an **access key**. Open the bot in Telegram and send it the key — this is the only thing the bot accepts from you at first. The bot then stays bound to your account.

How access is restricted:

- The bot obeys **only the Telegram account that is signed in to this app** (checked by its numeric user id on every event). Messages and button presses from anyone else, and anything from groups, are dropped **silently** — the bot does not even answer.
- On first contact you must also send the **one-time key** shown in the web app. The key exists only while the bot runs unbound, is shown only to the authenticated browser, and your message with it is deleted from the chat.
- 5 wrong keys → the bot locks for 15 minutes and the key is replaced with a new one.
- The binding is reset when you log out, sign in with another account, change the token, or press *New key*.
- Every destructive action needs a fresh confirmation tap (valid for 2 minutes and for exactly one run).
- The bot never shows message texts — only chat names and counters.
- The bot is switched on only from the web app and never starts by itself. It uses outgoing connections only (no webhook, no open port).

## Security

What the app does for you:

- The server listens on `127.0.0.1` only and checks the `Host` header (anti DNS-rebinding).
- Every launch generates a random token; the API works only for the browser that opened the printed link (HttpOnly, SameSite=Strict cookie; the token is removed from the address bar right away).
- The API accepts only `application/json` (blocks cross-site form posts) and a strict Content-Security-Policy with a per-response script nonce is set; no inline event handlers, no external scripts or fonts.
- All displayed data (chat names, texts) is HTML-escaped.
- Secret files (`tg_cleaner.session`, `tg_cleaner.json`, `tg_cleaner_schedules.json`) are restricted to the current user on Linux/macOS (`chmod 600`) and are in `.gitignore`.
- The bot session is kept in memory only; the bot token is stored in `tg_cleaner.json`.
- The bot token and the access key are never returned to the browser after saving (the key is shown only while it is needed).

What you should know (threat model):

- `tg_cleaner.session` gives **full access to your Telegram account**; `tg_cleaner.json` holds your API keys and the bot token. Anyone who gets these files controls your account/bot.
- The app has no password of its own: anyone who can use your computer's browser session or read your files can act as you. Hosting it on a shared or remote machine needs extra care (see below).
- If your Telegram account is compromised, an attacker can use the bot as well — the bot trusts the account, not the device.
- Bot messages travel through Telegram's servers like any bot chat (not end-to-end encrypted): chat names and counters are visible there. Don't use the bot for chats whose names are sensitive.
- Telegram's FloodWait / rate limits apply; abusing them can restrict your account. The app waits automatically, don't work around it.

## Precautions (short)

- Never share or commit `tg_cleaner.session`, `tg_cleaner.json`, `tg_cleaner_schedules.json`; keep them in a folder only you can read; use disk encryption.
- Don't expose the port: no port forwarding, tunnels (ngrok etc.), reverse proxies or `0.0.0.0`. If you host it on a remote server, reach it only through an SSH tunnel / VPN.
- Don't keep the app running when you don't need it; the bot works only while it runs.
- Keep your Telegram account secure: 2FA / cloud password, and review *Settings → Devices* — the app appears there as "Telegram Cleaner". End the session there if the machine is lost.
- Leaked bot token → `/revoke` it in @BotFather and press *Forget token*. Suspect someone saw the key → press *New key*.
- Send the access key only to your own bot; the bot never asks for it twice.
- Use *Count* before deleting; scheduled tasks and the bot run with the same irreversible deletion.
- Install dependencies in a virtual environment and keep `telethon` / `aiohttp` up to date.

## Notes and limitations

- Deletion is **irreversible**.
- Reactions are found by scanning the chat history, so large chats are slow — limit the period or the number of messages.
- Paid (Stars) reactions cannot be removed and are skipped.
- Telegram rate-limits; the app waits automatically when asked to.
- In groups where you are not an admin you can delete only your own messages.

---

## Русский

Однофайловое веб-приложение (бэкенд на Python + встроенный интерфейс в стиле Telegram), которое удаляет **ваши сообщения** и снимает **ваши реакции** в выбранном чате. Работает как обычный клиент пользователя через [Telethon](https://github.com/LonamiWebs/Telethon); всё выполняется локально на вашем компьютере.

### Возможности

- Список чатов с поиском, аватарками и папками (Все / Личные / Группы / Каналы)
- **Мои сообщения** — удаление всего, что вы отправили в чат (в личных чатах можно удалить и у собеседника)
- **Мои реакции** — снятие ваших реакций с сообщений чата
- Фильтры: период и максимальное количество сообщений
- Кнопка **подсчёта** — пробный запуск без удаления
- **Отложенная очистка** — запуск в выбранное время, один раз или по повтору (каждый час / день / неделю / свой интервал), при желании только для записей старше N дней
- **Telegram-бот** — те же функции прямо из Telegram (см. ниже)
- Подтверждение, живой прогресс, остановка, автоматическая обработка FloodWait
- Интерфейс на русском и английском (переключатель в правом верхнем углу), светлая и тёмная темы

### Быстрый старт

1. Получите `api_id` и `api_hash` на <https://my.telegram.org> → *API development tools*.
2. Установите и запустите (Windows PowerShell):

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install telethon aiohttp
   python tg_cleaner.py
   ```

   Linux / macOS:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install telethon aiohttp
   python tg_cleaner.py
   ```

3. Откройте ссылку, выведенную в консоль (в ней одноразовый токен, при каждом запуске он новый; браузер открывается автоматически).
4. Введите api_id / api_hash, номер телефона, код входа (и пароль 2FA, если он есть).
5. Выберите чат, режим *Мои сообщения* или *Мои реакции*, задайте фильтры и нажмите красную кнопку.

Переменные окружения: `PORT` (по умолчанию `8080`), `NO_BROWSER=1` (не открывать браузер).

### Отложенная очистка

Настройте очистку как обычно (режим, лимит, «удалить также у собеседника»), нажмите кнопку с часами и выберите время запуска, повтор и необязательный фильтр *старше N дней*. Управление задачами — меню → *Запланированные задачи* (запустить сейчас, пауза / возобновить, удалить). Задачи хранятся в `tg_cleaner_schedules.json`.

- В момент срабатывания приложение (и ваша сессия) должно быть запущено. Запуск, пропущенный при выключенном приложении, выполнится **один раз** при следующем старте.
- Запланированные задачи выполняются **без подтверждения**, а удаление необратимо. Тщательно выбирайте фильтры.
- Минимальный интервал повтора — 5 минут. Одновременно идёт одна задача; наступившая по времени задача ждёт завершения текущей.
- Фиксированные даты «с / по» в расписании не используются — применяйте «старше N дней», это корректно работает при повторах.

### Telegram-бот

Кнопка **Бот** в правом нижнем углу открывает панель бота — плавающее окно, которое не затемняет остальной экран и остаётся открытым, пока вы его не свернёте (кнопкой сворачивания или повторным нажатием на «Бот»). Бот предоставляет те же функции в чате Telegram с inline-кнопками: выбор чата, режима / возраста / лимита, подсчёт, удаление (с шагом подтверждения), планирование, управление запланированными задачами, остановка текущей задачи.

Настройка:

1. Создайте бота у [@BotFather](https://t.me/BotFather) и скопируйте токен.
2. В веб-приложении нажмите **Бот**, вставьте токен и нажмите *Включить бота*.
3. В панели появится **ключ доступа**. Откройте бота в Telegram и отправьте ему этот ключ — при первом обращении бот принимает от вас только его. После этого бот остаётся привязанным к вашему аккаунту.

Как ограничен доступ:

- Бот подчиняется **только аккаунту Telegram, в который выполнен вход в этом приложении** (проверка по числовому user id при каждом событии). Сообщения и нажатия кнопок от любых других пользователей, а также всё из групп, **молча игнорируются**: бот даже не отвечает.
- При первом обращении нужно отправить ещё и **одноразовый ключ** из веб-приложения. Ключ существует, только пока бот работает без привязки, показывается лишь авторизованному браузеру, а ваше сообщение с ним удаляется из чата.
- 5 неверных ключей → бот блокируется на 15 минут, а ключ заменяется новым.
- Привязка сбрасывается при выходе из аккаунта, входе под другим аккаунтом, смене токена и по кнопке *Новый ключ*.
- Любое разрушительное действие требует свежего нажатия «подтвердить» (действует 2 минуты и ровно на один запуск).
- Бот не показывает тексты сообщений — только названия чатов и счётчики.
- Бот включается только из веб-приложения и сам не стартует. Он использует только исходящие соединения (без вебхука и открытых портов).

### Безопасность

Что делает приложение:

- Сервер слушает только `127.0.0.1` и проверяет заголовок `Host` (защита от DNS-rebinding).
- При каждом запуске создаётся случайный токен; API доступен только браузеру, открывшему выведенную ссылку (cookie HttpOnly, SameSite=Strict; токен сразу убирается из адресной строки).
- API принимает только `application/json` (защита от межсайтовых форм), включена строгая Content-Security-Policy с nonce для скриптов; нет inline-обработчиков, внешних скриптов и шрифтов.
- Все отображаемые данные (названия чатов, тексты) экранируются.
- Секретные файлы (`tg_cleaner.session`, `tg_cleaner.json`, `tg_cleaner_schedules.json`) на Linux/macOS доступны только текущему пользователю (`chmod 600`) и перечислены в `.gitignore`.
- Сессия бота хранится только в памяти; токен бота лежит в `tg_cleaner.json`.
- Токен бота и ключ доступа не возвращаются в браузер после сохранения (ключ показывается, только пока он нужен).

О чём нужно помнить (модель угроз):

- `tg_cleaner.session` даёт **полный доступ к вашему аккаунту Telegram**; в `tg_cleaner.json` лежат ключи API и токен бота. Кто получил эти файлы, тот управляет аккаунтом / ботом.
- У приложения нет собственного пароля: любой, кто может пользоваться браузерной сессией на вашем компьютере или читать ваши файлы, действует от вашего имени. Размещение на общем или удалённом сервере требует дополнительной осторожности (см. ниже).
- Если ваш аккаунт Telegram скомпрометирован, злоумышленник сможет пользоваться и ботом: бот доверяет аккаунту, а не устройству.
- Сообщения бота идут через серверы Telegram, как в любом чате с ботом (без сквозного шифрования): там видны названия чатов и счётчики. Не используйте бота для чатов с чувствительными названиями.
- Действуют лимиты Telegram (FloodWait); их обход может привести к ограничениям аккаунта. Приложение ждёт само — не обходите это.

### Меры предосторожности (кратко)

- Никому не передавайте и не коммитьте `tg_cleaner.session`, `tg_cleaner.json`, `tg_cleaner_schedules.json`; храните их в папке, доступной только вам; используйте шифрование диска.
- Не открывайте порт наружу: никаких пробросов портов, туннелей (ngrok и т. п.), обратных прокси и `0.0.0.0`. На удалённом сервере заходите только через SSH-туннель / VPN.
- Не держите приложение запущенным без необходимости: бот работает, только пока оно запущено.
- Защитите аккаунт Telegram: 2FA / облачный пароль и проверяйте *Настройки → Устройства* — приложение отображается там как «Telegram Cleaner». При потере компьютера завершите эту сессию.
- Утёк токен бота → выполните `/revoke` у @BotFather и нажмите *Забыть токен*. Подозреваете, что ключ кто-то видел → нажмите *Новый ключ*.
- Отправляйте ключ доступа только своему боту; бот не просит его повторно.
- Перед удалением используйте *Посчитать*; запланированные задачи и бот выполняют то же необратимое удаление.
- Ставьте зависимости в виртуальное окружение и обновляйте `telethon` / `aiohttp`.

### Замечания и ограничения

- Удаление **необратимо**.
- Реакции ищутся перебором истории, поэтому в больших чатах это медленно — ограничьте период или число сообщений.
- Платные реакции (звёзды) снять нельзя, они пропускаются.
- У Telegram есть лимиты запросов; приложение само ждёт, когда это нужно.
- В группах, где вы не админ, можно удалить только свои сообщения.
