# Telegram Cleaner

A single-file local web app (Python backend + embedded Telegram-style UI) that deletes **your own messages** and removes **your own reactions** in a chosen chat. It runs as a regular Telegram user client via [Telethon](https://github.com/LonamiWebs/Telethon); everything stays on your machine.

## Features

- Chat list with search, avatars and folders (All / Personal / Groups / Channels)
- **My messages** — delete everything you sent (optionally for the other person too in private chats)
- **My reactions** — remove your reactions from messages in the chat
- Filters: date range and max number of messages
- **Count** — dry run showing how many items would be affected
- **Scheduled cleanup** — once or repeating (hourly / daily / weekly / custom), optionally only items older than N days
- **Telegram bot** — control the same functions from Telegram itself
- Confirmation dialog, live progress, Stop button, automatic FloodWait handling
- English / Russian UI, light and dark theme

## Quick start

1. Get `api_id` and `api_hash` at <https://my.telegram.org> → *API development tools*.
2. Install and run:

   ```powershell
   # Windows PowerShell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install telethon aiohttp
   python tg_cleaner.py
   ```

   ```bash
   # Linux / macOS
   python3 -m venv .venv && source .venv/bin/activate
   pip install telethon aiohttp
   python tg_cleaner.py
   ```

3. Open the link printed in the console (one-time token, changes every launch; the browser opens automatically).
4. Enter api_id / api_hash, phone number, login code (and 2FA password if set).
5. Pick a chat, choose *My messages* or *My reactions*, set filters, press the red button.

Environment variables: `PORT` (default `8080`), `NO_BROWSER=1` (don't open the browser).

## Scheduled cleanup

Configure the cleanup as usual, press the clock button, then set start time, repeat interval and an optional *older than N days* filter. Manage tasks via menu → *Scheduled tasks* (run now, pause / resume, delete). Tasks are stored in `tg_cleaner_schedules.json`.

- The app and your login session must be running when a task fires. A run missed while the app was closed executes **once** on next launch.
- Scheduled tasks run **without confirmation**; deletion is irreversible.
- Minimum interval is 5 minutes. One job at a time; a due task waits for the current one.
- Fixed *from / to* dates are not used by schedules — use *older than N days*.

## Telegram bot

The **Bot** button (bottom-right) opens a floating panel that stays open until minimized. The bot offers the same functions in Telegram with inline buttons: pick a chat, set mode / age / limit, count, delete (with confirmation), schedule, manage tasks, stop a running job.

Setup:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. In the web app press **Bot**, paste the token, press *Turn bot on*.
3. Send the **access key** shown in the panel to your bot in Telegram. The bot then stays bound to your account.

Access restrictions:

- The bot obeys **only the Telegram account signed in to this app** (checked by user id on every event). Anyone else, and anything from groups, is dropped **silently**.
- First contact requires the **one-time key**, shown only to the authenticated browser while the bot is unbound; your message with it is deleted.
- 5 wrong keys → 15-minute lock and a new key.
- Binding resets on logout, login with another account, token change, or *New key*.
- Every destructive action needs a fresh confirmation tap (valid 2 minutes, one run).
- The bot never shows message texts — only chat names and counters.
- It starts only from the web app and uses outgoing connections only (no webhook, no open port).

## Security

What the app does:

- Listens on `127.0.0.1` only and checks the `Host` header (anti DNS-rebinding).
- Random token per launch; the API works only for the browser that opened the printed link (HttpOnly, SameSite=Strict cookie; token removed from the address bar).
- API accepts only `application/json`; strict CSP with per-response script nonce; no inline handlers, external scripts or fonts.
- All displayed data is HTML-escaped.
- `tg_cleaner.session`, `tg_cleaner.json`, `tg_cleaner_schedules.json` are `chmod 600` on Linux/macOS and in `.gitignore`.
- Bot session is in memory only; the bot token is stored in `tg_cleaner.json` and, like the access key, is never returned to the browser after saving.

Threat model:

- `tg_cleaner.session` gives **full access to your Telegram account**; `tg_cleaner.json` holds your API keys and bot token. Whoever gets these files controls your account/bot.
- The app has no password: anyone with access to your browser session or files can act as you. Be extra careful on shared or remote machines.
- If your Telegram account is compromised, so is the bot — it trusts the account, not the device.
- Bot messages pass through Telegram's servers (not end-to-end encrypted), so chat names and counters are visible there. Avoid the bot for chats with sensitive names.
- Telegram's FloodWait / rate limits apply; abusing them can restrict your account. The app waits automatically — don't work around it.

## Precautions

- Never share or commit the three secret files; keep them in a private folder; use disk encryption.
- Don't expose the port (no forwarding, tunnels, reverse proxies, `0.0.0.0`). On a remote server, use an SSH tunnel / VPN.
- Don't leave the app running when not needed; the bot works only while it runs.
- Secure your account: 2FA / cloud password, review *Settings → Devices* ("Telegram Cleaner") and end the session if the machine is lost.
- Leaked bot token → `/revoke` in @BotFather and press *Forget token*. Suspect the key was seen → press *New key*.
- Send the access key only to your own bot; it never asks twice.
- Use *Count* before deleting; scheduled tasks and the bot delete irreversibly too.
- Use a virtual environment and keep `telethon` / `aiohttp` updated.

## Notes and limitations

- Deletion is **irreversible**.
- Reactions are found by scanning chat history, so large chats are slow — limit the period or message count.
- Paid (Stars) reactions cannot be removed and are skipped.
- In groups where you are not an admin you can delete only your own messages.
