#!/usr/bin/env python3
"""
Telegram Cleaner — delete your own messages and reactions in a chosen Telegram chat.

Install:  pip install telethon aiohttp
Run:      python tg_cleaner.py        → opens http://127.0.0.1:8080/?token=...
          (PORT env var changes the port, NO_BROWSER=1 disables opening the browser)

Get api_id and api_hash at https://my.telegram.org → API development tools.
The session is stored next to the script in tg_cleaner.session — never share it.
"""
import asyncio
import hmac
import json
import math
import os
import platform
import re
import secrets
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

try:
    from aiohttp import web
    from telethon import TelegramClient, events, functions, types, utils
    from telethon.errors import (FloodWaitError, MessageNotModifiedError, RPCError,
                                 SessionPasswordNeededError)
    from telethon.tl.custom import Button
except ImportError:
    print("Missing dependencies. Install them with:\n    pip install telethon aiohttp")
    sys.exit(1)

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "tg_cleaner.json"
SESSION = str(BASE / "tg_cleaner")
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", 8080))

# Per-launch secret: only the browser that opened the printed link may use the API.
TOKEN = secrets.token_urlsafe(32)
COOKIE = f"tgc_auth_{PORT}"

DELETE_BATCH = 100
REACTION_DELAY = 0.35
BATCH_DELAY = 0.5

SCHEDULE_FILE = BASE / "tg_cleaner_schedules.json"
SCHED_TICK = 15        # seconds between checks for due tasks
MIN_REPEAT_MIN = 5     # shortest allowed repeat interval, minutes


# ───────────────────────────── state ─────────────────────────────

class State:
    client = None
    phone = None
    phone_code_hash = None
    entities = {}
    avatars = {}
    job = None
    schedules = []
    sched_task = None
    me_id = None


S = State()
avatar_sem = asyncio.Semaphore(4)


def restrict(path):
    """Make a secret file readable only by the current user (POSIX only)."""
    if os.name != "nt" and os.path.exists(path):
        os.chmod(path, 0o600)


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg), "utf-8")
    restrict(CONFIG_FILE)


async def make_client(api_id, api_hash):
    if S.client:
        await S.client.disconnect()
    S.client = TelegramClient(
        SESSION, int(api_id), api_hash,
        device_model="Telegram Cleaner", system_version=platform.system() or "PC",
        app_version="1.1", flood_sleep_threshold=120,
    )
    await S.client.connect()
    restrict(SESSION + ".session")
    S.entities.clear()
    S.avatars.clear()


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg["k"])
        self.status, self.msg = status, msg


async def require_client():
    if not S.client:
        raise ApiError(400, T("err.no_client"))
    if not S.client.is_connected():
        await S.client.connect()
    return S.client


# ───────────────────────────── helpers ─────────────────────────────

def T(key, **args):
    """Translatable value: the frontend renders it in the selected language."""
    return {"k": key, "a": args}


ERRORS = {
    "PhoneNumberInvalidError": "err.phone_invalid",
    "PhoneNumberBannedError": "err.phone_banned",
    "PhoneNumberFloodError": "err.phone_flood",
    "PhoneCodeInvalidError": "err.code_invalid",
    "PhoneCodeExpiredError": "err.code_expired",
    "PhoneCodeEmptyError": "err.code_empty",
    "PasswordHashInvalidError": "err.password_invalid",
    "ApiIdInvalidError": "err.api_invalid",
    "AuthKeyUnregisteredError": "err.auth_unregistered",
    "ChatAdminRequiredError": "err.admin_required",
    "MessageDeleteForbiddenError": "err.delete_forbidden",
    "ChannelPrivateError": "err.channel_private",
    "AccessTokenInvalidError": "err.bot_token_invalid",
    "AccessTokenExpiredError": "err.bot_token_invalid",
}


def describe(e):
    if isinstance(e, FloodWaitError):
        return T("err.flood", s=e.seconds)
    key = ERRORS.get(type(e).__name__)
    return T(key) if key else T("err.raw", text=str(e) or type(e).__name__)


def j(status=200, **data):
    return web.json_response(data, status=status)


def user_info(u):
    return {"id": u.id, "name": utils.get_display_name(u) or "?",
            "phone": u.phone, "has_photo": has_photo(u)}


def has_photo(e):
    p = getattr(e, "photo", None)
    return bool(p) and not isinstance(p, (types.UserProfilePhotoEmpty, types.ChatPhotoEmpty))


def kind_of(e):
    if isinstance(e, types.User):
        if e.is_self:
            return "saved"
        return "bot" if e.bot else "user"
    if isinstance(e, types.Channel) and not (e.megagroup or e.gigagroup):
        return "channel"
    return "group"


def chat_name(e):
    if kind_of(e) == "saved":
        return T("chat.saved")
    return utils.get_display_name(e) or T("chat.deleted")


MEDIA = ("photo", "video_note", "video", "voice", "sticker", "gif",
         "audio", "poll", "geo", "contact", "document")


def preview(m):
    """Message text, or a translatable media label when there is no text."""
    if m is None:
        return ""
    try:
        if getattr(m, "action", None):
            return T("media.service")
        if m.message:
            return m.message.replace("\n", " ")[:100]
        for attr in MEDIA:
            if getattr(m, attr, None):
                return T("media." + attr)
    except Exception:
        pass
    return T("media.message")


def snippet(m, n=40):
    t = preview(m)
    if isinstance(t, str) and len(t) > n:
        return t[:n - 1] + "…"
    return t


def parse_date(s, end=False):
    if not s:
        return None
    d = datetime.strptime(s, "%Y-%m-%d")
    if end:
        d += timedelta(days=1)
    return d.astimezone()


PAID = getattr(types, "ReactionPaid", ())


def my_reactions(m):
    r = getattr(m, "reactions", None)
    if not r:
        return []
    mine = [rc.reaction for rc in (r.results or []) if getattr(rc, "chosen_order", None) is not None]
    if not mine:
        mine = [rr.reaction for rr in (r.recent_reactions or []) if getattr(rr, "my", False)]
    # Paid (Stars) reactions cannot be withdrawn.
    return [x for x in mine if not isinstance(x, PAID)]


def reaction_label(r):
    if isinstance(r, types.ReactionEmoji):
        return r.emoticon
    return T("reaction.custom")


# ───────────────────────────── cleanup job ─────────────────────────────

class Job:
    seq = 0

    def __init__(self, kind, dialog_id, name, dry):
        Job.seq += 1
        self.id = Job.seq
        self.kind, self.dialog_id, self.name, self.dry = kind, dialog_id, name, dry
        self.status = "running"
        self.found = self.done = self.scanned = 0
        self.total = None
        self.error = None
        self.stop = False
        self.log = []
        self.schedule_id = None
        self.task = None

    def add(self, level, key, **args):
        self.log.append({"t": time.time(), "level": level, "msg": T(key, **args)})

    def to_dict(self, since=0):
        return {"id": self.id, "kind": self.kind, "dialog_id": self.dialog_id, "name": self.name,
                "dry": self.dry, "status": self.status, "found": self.found, "done": self.done,
                "scanned": self.scanned, "total": self.total, "error": self.error,
                "schedule_id": self.schedule_id, "log": self.log[since:]}


async def nap(job, seconds):
    end = time.monotonic() + seconds
    while not job.stop and time.monotonic() < end:
        await asyncio.sleep(min(1, end - time.monotonic()))


async def tg_call(job, fn):
    """Call the API, waiting out FloodWait (the pause can be interrupted by Stop)."""
    while True:
        try:
            return await fn()
        except FloodWaitError as e:
            job.add("warn", "log.flood", s=e.seconds)
            await nap(job, e.seconds + 1)
            if job.stop:
                return None


async def run_messages(job, ent, o):
    c = S.client
    if not (o.date_from or o.date_to):
        total = (await c.get_messages(ent, from_user="me", limit=0)).total
        job.total = min(total, o.limit) if o.limit else total

    batch = []

    async def flush():
        if not batch:
            return
        if not o.dry:
            try:
                res = await tg_call(job, lambda: c.delete_messages(ent, list(batch), revoke=o.revoke))
                deleted = sum(getattr(r, "pts_count", 0) for r in (res or [])) or (len(batch) if res else 0)
                job.done += deleted
                job.add("info", "log.deleted", n=deleted, total=job.done)
            except RPCError as e:
                job.add("warn", "log.batch_fail", n=len(batch), e=describe(e))
            await asyncio.sleep(BATCH_DELAY)
        batch.clear()

    async for m in c.iter_messages(ent, from_user="me", offset_date=o.date_to, limit=o.limit):
        if job.stop:
            break
        if o.date_from and m.date < o.date_from:
            break
        job.found += 1
        batch.append(m.id)
        if len(batch) >= DELETE_BATCH:
            await flush()
    if not job.stop:
        await flush()


async def run_reactions(job, ent, o):
    c = S.client
    if not (o.date_from or o.date_to):
        total = (await c.get_messages(ent, limit=0)).total
        job.total = min(total, o.limit) if o.limit else total

    async for m in c.iter_messages(ent, offset_date=o.date_to, limit=o.limit):
        if job.stop:
            break
        if o.date_from and m.date < o.date_from:
            break
        job.scanned += 1
        mine = my_reactions(m)
        if not mine:
            continue
        job.found += 1
        if o.dry:
            continue
        labels = [reaction_label(r) for r in mine]
        try:
            res = await tg_call(job, lambda: c(functions.messages.SendReactionRequest(
                peer=ent, msg_id=m.id, reaction=[])))
            if res is None:
                break
            job.done += 1
            job.add("info", "log.reaction_removed", r=labels, snip=snippet(m))
        except RPCError as e:
            job.add("warn", "log.reaction_fail", r=labels, snip=snippet(m), e=describe(e))
        await asyncio.sleep(REACTION_DELAY)


async def run_job(job, ent, o):
    job.add("service", ("log.count_" if job.dry else "log.clean_") + job.kind[0], name=job.name)
    try:
        await (run_messages if job.kind == "messages" else run_reactions)(job, ent, o)
        job.status = "stopped" if job.stop else "done"
        if job.kind == "messages":
            summary = T("log.sum_found_m", n=job.found) if job.dry else T("log.sum_deleted", n=job.done)
        else:
            summary = (T("log.sum_r_dry", s=job.scanned, n=job.found) if job.dry
                       else T("log.sum_r", s=job.scanned, n=job.done))
        job.add("service", "log.stopped" if job.stop else "log.done", sum=summary)
    except Exception as e:
        job.status = "error"
        job.error = describe(e)
        job.add("error", "log.error", e=job.error)
    if job.schedule_id is not None:
        finish_schedule(job.schedule_id, job.status, job.done, job.error)


def launch(kind, did, ent, o, schedule_id=None):
    """Create a job and run it in the background."""
    job = Job(kind, did, chat_name(ent), o.dry)
    job.schedule_id = schedule_id
    if schedule_id is not None:
        job.add("service", "log.sched_run")
    S.job = job
    job.task = asyncio.get_running_loop().create_task(run_job(job, ent, o))
    return job


# ───────────────────────────── scheduler ─────────────────────────────
# A schedule is a plain dict persisted in tg_cleaner_schedules.json. Times are epoch seconds.
# Repeating tasks keep their cadence; a run missed while the app was off fires once on startup.

def load_schedules():
    try:
        data = json.loads(SCHEDULE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_schedules():
    SCHEDULE_FILE.write_text(json.dumps(S.schedules), "utf-8")
    restrict(SCHEDULE_FILE)


def find_schedule(sid):
    return next((s for s in S.schedules if s["id"] == sid), None)


def next_after(sch, now):
    """Next run time after `now` on the task's cadence; None for one-shot tasks."""
    step = sch["repeat_min"] * 60
    if step <= 0:
        return None
    n = sch["next_run"] or now
    if n <= now:
        n += step * int((now - n) // step + 1)
    return n


def finish_schedule(sid, status, done, error):
    sch = find_schedule(sid)
    if sch is None:  # deleted while running
        return
    now = time.time()
    sch.update(last_run=now, last_status=status, last_done=done, last_error=error)
    sch["next_run"] = next_after(sch, now)
    if sch["next_run"] is None:
        sch["enabled"] = False
    save_schedules()


def schedule_options(sch):
    older = sch["older_days"]
    return SimpleNamespace(
        dry=False, date_from=None,
        # "older than N days" is relative, so it stays meaningful for repeating tasks.
        date_to=datetime.now().astimezone() - timedelta(days=older) if older else None,
        limit=sch["limit"] or None, revoke=sch["revoke"],
    )


async def run_due():
    """Start the earliest due task, unless a job is already running."""
    now = time.time()
    due = [s for s in S.schedules if s["enabled"] and s["next_run"] and s["next_run"] <= now]
    if not due or job_running() or not S.client:
        return
    if not S.client.is_connected():
        await S.client.connect()
    if not await S.client.is_user_authorized():
        return
    sch = min(due, key=lambda s: s["next_run"])
    try:
        ent = S.entities.get(sch["dialog_id"]) or await S.client.get_entity(sch["dialog_id"])
    except Exception as e:
        finish_schedule(sch["id"], "error", 0, describe(e))
        return
    if job_running():  # something started during the awaits above
        return
    launch(sch["kind"], sch["dialog_id"], ent, schedule_options(sch), sch["id"])


async def scheduler():
    while True:
        await asyncio.sleep(SCHED_TICK)
        try:
            await run_due()
        except Exception as e:
            print("Scheduler error:", e)


async def add_schedule(did, kind, start, repeat, older, limit, revoke):
    """Validate and store a new task (shared by the web API and the bot)."""
    if (kind not in ("messages", "reactions") or not math.isfinite(start)
            or not 0 <= older <= 36500 or limit < 0 or not 0 <= repeat <= 525600):
        raise ApiError(400, T("err.sched_bad"))
    if repeat and repeat < MIN_REPEAT_MIN:
        raise ApiError(400, T("err.sched_interval"))
    if start < time.time() - 60:
        raise ApiError(400, T("err.sched_past"))
    c = await require_client()
    ent = S.entities.get(did) or await c.get_entity(did)
    sch = {
        "id": max((s["id"] for s in S.schedules), default=0) + 1,
        "dialog_id": did, "name": chat_name(ent), "kind": kind,
        "older_days": older, "limit": limit, "revoke": revoke,
        "repeat_min": repeat, "enabled": True, "next_run": start,
        "last_run": None, "last_status": None, "last_done": 0, "last_error": None,
    }
    S.schedules.append(sch)
    save_schedules()
    return sch


def set_schedule_enabled(sch, enabled):
    if enabled:
        if sch["next_run"] is None or (not sch["repeat_min"] and sch["next_run"] < time.time() - 60):
            raise ApiError(400, T("err.sched_past"))
        if sch["repeat_min"]:  # skip the runs missed while paused
            sch["next_run"] = next_after(sch, time.time())
    sch["enabled"] = enabled
    save_schedules()


async def run_schedule_now(sch):
    if job_running():
        raise ApiError(409, T("err.job_running"))
    sch.update(enabled=True, next_run=time.time())
    save_schedules()
    await run_due()


# ───────────────────────────── Telegram bot ─────────────────────────────
# The bot is a second Telegram client (a bot account made with @BotFather) that only *triggers*
# the same jobs as the web UI; deletion itself still goes through the signed-in user account.
#
# Access control:
#   * events from anyone except the signed-in account's user id are dropped without any reply,
#   * the owner must also prove possession of a one-time key that is only shown in the web UI,
#   * wrong keys are rate limited (lockout + key rotation),
#   * destructive actions need a fresh confirmation tap,
#   * the bot never shows message texts, only chat names and counters,
#   * it only talks in private chats and never starts on its own: the web UI switches it on.

BOT_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")
KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
KEY_MAX_FAILS = 5
KEY_LOCK_SEC = 15 * 60
BOT_PAGE = 8
BOT_OLDER = (0, 1, 7, 30, 90, 365)
BOT_LIMITS = (0, 100, 1000, 10000)
BOT_STARTS = (1, 6, 12, 24)
BOT_REPEATS = (0, 60, 1440, 10080)
BOT_CONFIRM_TTL = 120
ICONS = {"user": "👤", "bot": "🤖", "group": "👥", "channel": "📢", "saved": "★"}

BT = {
    "en": {
        "home": "🧹 Telegram Cleaner\nDelete your own messages and reactions.",
        "no_job": "No job is running.",
        "need_key": "🔑 Send the access key shown in the web app (Telegram bot panel).",
        "bad_key": "❌ Wrong key. Attempts left: {n}.",
        "locked": "⛔ Locked. Try again in {m} min.",
        "locked_out": "⛔ Too many wrong keys. The bot is locked for {m} min and the key was changed.",
        "verified": "✅ Key accepted. The bot is now bound to your account.",
        "busy": "⏳ Another job is already running.",
        "expired": "⌛ That confirmation expired. Please start again.",
        "saved": "Saved Messages", "deleted": "Deleted Account",
        "b.chats": "📂 Chats", "b.tasks": "🕒 Scheduled", "b.stop": "⏹ Stop", "b.lang": "🌐 Language",
        "b.home": "🏠 Menu", "b.back": "‹ Back", "b.search": "🔎 Search", "b.refresh": "🔄 Refresh",
        "tab.a": "All", "tab.u": "Personal", "tab.g": "Groups", "tab.c": "Channels",
        "list": "Choose a chat ({n}):", "ask_search": "Send a part of the chat name.",
        "chat": "💬 {name}\nMode: {mode}\nOnly older than: {older}\nLimit: {limit}",
        "chat_revoke": "\nAlso for the other person: {v}",
        "m.messages": "my messages", "m.reactions": "my reactions",
        "b.msgs": "My messages", "b.reacts": "My reactions",
        "older.0": "any age", "older.n": "{n} d", "limit.0": "none", "limit.n": "{n}",
        "b.older": "📅 Older than: {v}", "b.limit": "🔢 Limit: {v}", "b.revoke": "👥 Also for them: {v}",
        "b.count": "🔍 Count", "b.del": "🗑 Delete", "b.rm": "🗑 Remove", "b.sched": "🕒 Schedule",
        "x.older": "older than {n} d", "x.limit": "at most {n}",
        "c.messages": "⚠️ Delete ALL your messages in “{name}”{extra}{revoke}?\nThis cannot be undone.",
        "c.reactions": "⚠️ Remove your reactions in “{name}”{extra}?",
        "c.revoke": ", also for the other person",
        "b.yes_del": "✅ Yes, delete", "b.yes_rm": "✅ Yes, remove", "b.cancel": "Cancel",
        "j.m_dry": "Your messages found: {n}", "j.m": "Deleted {n} of {f} found",
        "j.r": "Scanned {s} · with your reactions {n}", "j.r_rm": " · removed {n}",
        "sch.start": "When should it run?", "sch.in": "in {h} h", "sch.repeat": "Repeat:",
        "sch.confirm": "🕒 Schedule “{what}” in “{name}”{extra}\nStart: {d}\nRepeat: {rep}\n"
                       "It runs automatically and does not ask again.",
        "b.ok": "✅ Schedule", "sch.done": "✅ Task scheduled.",
        "r.0": "once", "r.60": "every hour", "r.1440": "every day", "r.10080": "every week",
        "r.m": "every {n} min", "r.h": "every {n} h",
        "t.title": "Scheduled tasks ({n}):", "t.empty": "No scheduled tasks.",
        "t.detail": "🕒 {name}\n{what}{extra}\nRepeat: {rep}\n{state}{last}",
        "t.next": "Next run: {d}", "t.paused": "Paused", "t.finished": "Completed",
        "t.last": "\nLast run: {d} — {r}", "res.done": "done ({n})", "res.stopped": "stopped ({n})",
        "t.run_confirm": "⚠️ Run “{what}” in “{name}”{extra} now? Deletion cannot be undone.",
        "b.run": "▶ Run now", "b.pause": "⏸ Pause", "b.resume": "▶ Resume",
        "b.dtask": "🗑 Remove task", "b.yes_run": "✅ Yes, run",
        "e.generic": "Something went wrong.",
        "e.err.job_running": "Another job is already running.",
        "e.err.sched_past": "The start time is in the past.",
        "e.err.sched_interval": "The minimum interval is 5 minutes.",
        "e.err.sched_bad": "Invalid schedule parameters.",
        "e.err.sched_missing": "Task not found.",
        "e.err.flood": "Too many requests. Wait {s} s.",
        "e.err.channel_private": "No access to this chat.",
        "e.err.admin_required": "Admin rights are required.",
    },
    "ru": {
        "home": "🧹 Telegram Cleaner\nУдаление ваших сообщений и реакций.",
        "no_job": "Сейчас ничего не выполняется.",
        "need_key": "🔑 Отправьте ключ доступа, показанный в веб-приложении (панель Telegram-бота).",
        "bad_key": "❌ Неверный ключ. Осталось попыток: {n}.",
        "locked": "⛔ Заблокировано. Повторите через {m} мин.",
        "locked_out": "⛔ Слишком много неверных ключей. Бот заблокирован на {m} мин, ключ изменён.",
        "verified": "✅ Ключ принят. Бот привязан к вашему аккаунту.",
        "busy": "⏳ Уже выполняется другая задача.",
        "expired": "⌛ Подтверждение устарело. Начните заново.",
        "saved": "Избранное", "deleted": "Удалённый аккаунт",
        "b.chats": "📂 Чаты", "b.tasks": "🕒 Расписание", "b.stop": "⏹ Стоп", "b.lang": "🌐 Язык",
        "b.home": "🏠 Меню", "b.back": "‹ Назад", "b.search": "🔎 Поиск", "b.refresh": "🔄 Обновить",
        "tab.a": "Все", "tab.u": "Личные", "tab.g": "Группы", "tab.c": "Каналы",
        "list": "Выберите чат ({n}):", "ask_search": "Отправьте часть названия чата.",
        "chat": "💬 {name}\nРежим: {mode}\nТолько старше: {older}\nЛимит: {limit}",
        "chat_revoke": "\nУдалить и у собеседника: {v}",
        "m.messages": "мои сообщения", "m.reactions": "мои реакции",
        "b.msgs": "Мои сообщения", "b.reacts": "Мои реакции",
        "older.0": "любой давности", "older.n": "{n} дн.", "limit.0": "нет", "limit.n": "{n}",
        "b.older": "📅 Старше: {v}", "b.limit": "🔢 Лимит: {v}", "b.revoke": "👥 И у собеседника: {v}",
        "b.count": "🔍 Посчитать", "b.del": "🗑 Удалить", "b.rm": "🗑 Убрать", "b.sched": "🕒 Запланировать",
        "x.older": "старше {n} дн.", "x.limit": "не более {n}",
        "c.messages": "⚠️ Удалить ВСЕ ваши сообщения в «{name}»{extra}{revoke}?\nЭто нельзя отменить.",
        "c.reactions": "⚠️ Убрать ваши реакции в «{name}»{extra}?",
        "c.revoke": ", в том числе у собеседника",
        "b.yes_del": "✅ Да, удалить", "b.yes_rm": "✅ Да, убрать", "b.cancel": "Отмена",
        "j.m_dry": "Найдено ваших сообщений: {n}", "j.m": "Удалено {n} из {f} найденных",
        "j.r": "Проверено {s} · с вашими реакциями {n}", "j.r_rm": " · убрано {n}",
        "sch.start": "Когда запустить?", "sch.in": "через {h} ч", "sch.repeat": "Повтор:",
        "sch.confirm": "🕒 Запланировать «{what}» в «{name}»{extra}\nСтарт: {d}\nПовтор: {rep}\n"
                       "Выполняется автоматически, повторного подтверждения не будет.",
        "b.ok": "✅ Запланировать", "sch.done": "✅ Задача запланирована.",
        "r.0": "один раз", "r.60": "каждый час", "r.1440": "каждый день", "r.10080": "каждую неделю",
        "r.m": "каждые {n} мин", "r.h": "каждые {n} ч",
        "t.title": "Запланированные задачи ({n}):", "t.empty": "Нет запланированных задач.",
        "t.detail": "🕒 {name}\n{what}{extra}\nПовтор: {rep}\n{state}{last}",
        "t.next": "Следующий запуск: {d}", "t.paused": "Приостановлено", "t.finished": "Выполнено",
        "t.last": "\nПоследний запуск: {d} — {r}", "res.done": "готово ({n})", "res.stopped": "остановлено ({n})",
        "t.run_confirm": "⚠️ Запустить «{what}» в «{name}»{extra} сейчас? Удаление необратимо.",
        "b.run": "▶ Запустить", "b.pause": "⏸ Пауза", "b.resume": "▶ Возобновить",
        "b.dtask": "🗑 Удалить задачу", "b.yes_run": "✅ Да, запустить",
        "e.generic": "Что-то пошло не так.",
        "e.err.job_running": "Уже выполняется другая задача.",
        "e.err.sched_past": "Время запуска уже прошло.",
        "e.err.sched_interval": "Минимальный интервал — 5 минут.",
        "e.err.sched_bad": "Неверные параметры расписания.",
        "e.err.sched_missing": "Задача не найдена.",
        "e.err.flood": "Слишком много запросов. Подождите {s} с.",
        "e.err.channel_private": "Нет доступа к чату.",
        "e.err.admin_required": "Нужны права администратора.",
    },
}


class BotState:
    client = None
    running = False
    username = None
    key = None          # one-time access key, only while the bot runs unbound
    fails = 0
    locked_until = 0.0
    lang = "en"
    ui = {}             # the owner's navigation state (the bot serves exactly one user)
    dialogs = []        # [(id, kind, name)] snapshot used by the chat picker
    dialogs_at = 0.0
    watcher = None


B = BotState()


def bt(key, **a):
    s = BT[B.lang].get(key) or BT["en"][key]
    return s.format(**a) if a else s


def render_err(v):
    """Human text for a T() error token, in the bot's language."""
    if isinstance(v, dict):
        a = v.get("a") or {}
        s = BT[B.lang].get("e." + str(v.get("k"))) or BT["en"].get("e." + str(v.get("k")))
        try:
            return s.format(**a) if s else str(a.get("text") or bt("e.generic"))
        except (KeyError, IndexError):
            return bt("e.generic")
    return str(v)


def plain(name):
    if isinstance(name, dict):
        return bt("saved") if name.get("k") == "chat.saved" else bt("deleted")
    return str(name)


def bot_cfg():
    return load_config().get("bot") or {}


def bot_cfg_update(**kv):
    cfg = load_config()
    b = cfg.get("bot") or {}
    b.update(kv)
    cfg["bot"] = {k: v for k, v in b.items() if v is not None}
    save_config(cfg)


def new_key():
    return "-".join("".join(secrets.choice(KEY_ALPHABET) for _ in range(4)) for _ in range(4))


def norm_key(s):
    return "".join(ch for ch in s.upper() if ch.isascii() and ch.isalnum())


def bot_verified():
    return S.me_id is not None and bot_cfg().get("user_id") == S.me_id


def btn(text, data):
    return Button.inline(text, data.encode())


def cycle(values, cur):
    return values[(values.index(cur) + 1) % len(values)] if cur in values else values[0]


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def older_text(n):
    return bt("older.n", n=n) if n else bt("older.0")


def extra_text(older, limit):
    parts = ([bt("x.older", n=older)] if older else []) + ([bt("x.limit", n=limit)] if limit else [])
    return f" ({', '.join(parts)})" if parts else ""


def rep_text(m):
    if m in BOT_REPEATS:
        return bt(f"r.{m}")
    return bt("r.h", n=m // 60) if m % 60 == 0 else bt("r.m", n=m)


def job_text(job):
    icon = {"running": "⏳", "done": "✅", "stopped": "⏹", "error": "⚠️"}[job.status]
    if job.kind == "messages":
        line = bt("j.m_dry", n=job.found) if job.dry else bt("j.m", n=job.done, f=job.found)
    else:
        line = bt("j.r", s=job.scanned, n=job.found) + ("" if job.dry else bt("j.r_rm", n=job.done))
    if job.status == "error":
        line += "\n" + render_err(job.error)
    return f"{icon} {plain(job.name)}\n{line}"


def home_text():
    return bt("home") + "\n\n" + (job_text(S.job) if S.job else bt("no_job"))


def kb_home():
    rows = [[btn(bt("b.chats"), "ls"), btn(bt("b.tasks"), "t")]]
    if job_running():
        rows.append([btn(bt("b.stop"), "stop")])
    rows.append([btn(bt("b.lang"), "lang")])
    return rows


def bot_match(kind, tab):
    return (tab == "a" or (tab == "u" and kind in ("user", "bot", "saved"))
            or (tab == "g" and kind == "group") or (tab == "c" and kind == "channel"))


def bot_list(tab, page):
    q = B.ui.get("q", "").lower()
    items = [d for d in B.dialogs if bot_match(d[1], tab) and q in d[2].lower()]
    pages = max(1, -(-len(items) // BOT_PAGE))
    page = min(max(page, 0), pages - 1)
    rows = [[btn(f"{ICONS.get(k, '')} {name}"[:45], f"c:{did}")]
            for did, k, name in items[page * BOT_PAGE:(page + 1) * BOT_PAGE]]
    nav = []
    if page > 0:
        nav.append(btn("‹", f"l:{tab}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("›", f"l:{tab}:{page + 1}"))
    rows.append(nav)
    rows.append([btn(("• " if t == tab else "") + bt("tab." + t), f"l:{t}:0") for t in "augc"])
    rows.append([btn(bt("b.search"), "s"), btn(bt("b.refresh"), "r"), btn(bt("b.home"), "home")])
    return bt("list", n=len(items)), rows


def chat_menu():
    u = B.ui
    text = bt("chat", name=u["name"], mode=bt("m." + u["mode"]), older=older_text(u["older"]),
              limit=bt("limit.n", n=u["limit"]) if u["limit"] else bt("limit.0"))
    can_revoke = u["mode"] == "messages" and u["kind"] in ("user", "bot")
    if can_revoke:
        text += bt("chat_revoke", v="✓" if u["revoke"] else "✗")
    mark = lambda m: ("✓ " if u["mode"] == m else "") + bt("b.msgs" if m == "messages" else "b.reacts")
    rows = [[btn(mark("messages"), "m:messages"), btn(mark("reactions"), "m:reactions")],
            [btn(bt("b.older", v=older_text(u["older"])), "o"),
             btn(bt("b.limit", v=bt("limit.n", n=u["limit"]) if u["limit"] else bt("limit.0")), "n")]]
    if can_revoke:
        rows.append([btn(bt("b.revoke", v="✓" if u["revoke"] else "✗"), "v")])
    rows.append([btn(bt("b.count"), "go:c"), btn(bt("b.del" if u["mode"] == "messages" else "b.rm"), "go:a")])
    rows.append([btn(bt("b.sched"), "sch"), btn(bt("b.back"), "l:a:0")])
    return text, rows


def confirm_text(snap):
    mode, _did, older, limit, revoke, name = snap
    extra = extra_text(older, limit)
    if mode == "messages":
        return bt("c.messages", name=name, extra=extra, revoke=bt("c.revoke") if revoke else "")
    return bt("c.reactions", name=name, extra=extra)


def sched_state(s):
    if s["enabled"] and s["next_run"]:
        return bt("t.next", d=fmt_ts(s["next_run"]))
    return bt("t.paused") if s["next_run"] else bt("t.finished")


def task_detail(s):
    last = ""
    if s["last_run"]:
        if s["last_status"] == "error":
            res = render_err(s["last_error"])
        else:
            res = bt("res.stopped" if s["last_status"] == "stopped" else "res.done", n=s["last_done"])
        last = bt("t.last", d=fmt_ts(s["last_run"]), r=res)
    text = bt("t.detail", name=plain(s["name"]), what=bt("m." + s["kind"]),
              extra=extra_text(s["older_days"], s["limit"]), rep=rep_text(s["repeat_min"]),
              state=sched_state(s), last=last)
    i = s["id"]
    rows = [[btn(bt("b.run"), f"t:{i}:r"),
             btn(bt("b.pause" if s["enabled"] else "b.resume"), f"t:{i}:p")] if s["next_run"]
            else [btn(bt("b.run"), f"t:{i}:r")],
            [btn(bt("b.dtask"), f"t:{i}:d")], [btn(bt("b.back"), "t")]]
    return text, rows


def bot_tasks():
    rows = [[btn(f"{plain(s['name'])} · {rep_text(s['repeat_min'])}"[:45], f"t:{s['id']}")]
            for s in S.schedules]
    rows.append([btn(bt("b.home"), "home")])
    return (bt("t.title", n=len(S.schedules)) if S.schedules else bt("t.empty")), rows


async def bot_load_dialogs(force=False):
    if not force and B.dialogs and time.time() - B.dialogs_at < 120:
        return
    out = []
    async for d in S.client.iter_dialogs():
        S.entities[d.id] = d.entity
        kind = kind_of(d.entity)
        name = bt("saved") if kind == "saved" else (utils.get_display_name(d.entity) or bt("deleted"))
        out.append((d.id, kind, name))
    B.dialogs, B.dialogs_at = out, time.time()


async def bot_watch(chat_id, msg_id, job):
    """Keep one bot message updated with the job's progress."""
    last = None
    while B.running:
        done = job.status != "running"
        text = job_text(job)
        if text != last:
            rows = ([[btn(bt("b.back"), "cm")], [btn(bt("b.home"), "home")]] if done
                    else [[btn(bt("b.stop"), "stop")]])
            try:
                await B.client.edit_message(chat_id, msg_id, text, buttons=rows)
            except MessageNotModifiedError:
                pass
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception:
                return
            last = text
        if done:
            return
        await asyncio.sleep(3)


def bot_watch_start(event, job):
    if B.watcher:
        B.watcher.cancel()
    B.watcher = asyncio.get_running_loop().create_task(bot_watch(event.chat_id, event.message_id, job))


async def bot_job(event, show, snap, dry):
    mode, did, older, limit, revoke, _name = snap
    if job_running():
        return await show(bt("busy") + "\n\n" + home_text(), kb_home())
    ent = S.entities.get(did) or await S.client.get_entity(did)
    if job_running():
        return await show(bt("busy") + "\n\n" + home_text(), kb_home())
    o = SimpleNamespace(
        dry=dry, date_from=None,
        date_to=datetime.now().astimezone() - timedelta(days=older) if older else None,
        limit=limit or None, revoke=revoke)
    job = launch(mode, did, ent, o)
    await show(job_text(job), [[btn(bt("b.stop"), "stop")]])
    bot_watch_start(event, job)


async def bot_action(event, data):
    p = data.split(":")
    a, u = p[0], B.ui

    async def show(text, rows):
        try:
            await event.edit(text, buttons=rows)
        except MessageNotModifiedError:
            pass

    await event.answer()  # stop the button spinner
    if a != "s":
        u.pop("await", None)

    if a == "noop":
        return
    if a == "home":
        return await show(home_text(), kb_home())
    if a == "lang":
        B.lang = "ru" if B.lang == "en" else "en"
        bot_cfg_update(lang=B.lang)
        return await show(home_text(), kb_home())
    if a == "stop":
        if job_running():
            S.job.stop = True
        return await show(home_text(), kb_home())
    if a in ("ls", "l", "r"):
        if a == "ls":
            u.pop("q", None)
        await bot_load_dialogs(force=a == "r")
        tab, page = (p[1], int(p[2])) if a == "l" else ("a", 0)
        return await show(*bot_list(tab if tab in ("a", "u", "g", "c") else "a", page))
    if a == "s":
        u["await"] = "search"
        return await show(bt("ask_search"), [[btn(bt("b.cancel"), "ls")]])
    if a == "c":
        did = int(p[1])
        d = next((x for x in B.dialogs if x[0] == did), None)
        if d is None:
            return await show(home_text(), kb_home())
        u.update(did=did, kind=d[1], name=d[2], mode="messages", older=0, limit=0, revoke=True)
        u.pop("pending", None)
        return await show(*chat_menu())
    if a == "t":
        if len(p) == 1:
            return await show(*bot_tasks())
        sch = find_schedule(int(p[1]))
        if sch is None:
            return await show(*bot_tasks())
        act = p[2] if len(p) > 2 else ""
        i = sch["id"]
        if act == "r":
            text = bt("t.run_confirm", what=bt("m." + sch["kind"]), name=plain(sch["name"]),
                      extra=extra_text(sch["older_days"], sch["limit"]))
            return await show(text, [[btn(bt("b.yes_run"), f"t:{i}:y")], [btn(bt("b.cancel"), f"t:{i}")]])
        if act == "y":
            await run_schedule_now(sch)
            if job_running() and S.job.schedule_id == i:
                await show(job_text(S.job), [[btn(bt("b.stop"), "stop")]])
                return bot_watch_start(event, S.job)
        elif act == "p":
            set_schedule_enabled(sch, not sch["enabled"])
        elif act == "d":
            S.schedules.remove(sch)
            save_schedules()
            return await show(*bot_tasks())
        return await show(*task_detail(sch))

    # Everything below works on the selected chat.
    if "did" not in u:
        return await show(home_text(), kb_home())
    if a == "cm":
        return await show(*chat_menu())
    if a in ("m", "o", "n", "v"):
        if a == "m":
            u["mode"] = p[1] if p[1] in ("messages", "reactions") else "messages"
        elif a == "o":
            u["older"] = cycle(BOT_OLDER, u["older"])
        elif a == "n":
            u["limit"] = cycle(BOT_LIMITS, u["limit"])
        else:
            u["revoke"] = not u["revoke"]
        return await show(*chat_menu())

    snap = (u["mode"], u["did"], u["older"], u["limit"], u["revoke"], u["name"])
    if a == "go":
        if p[1] == "c":
            return await bot_job(event, show, snap, True)
        if p[1] == "a":
            u["pending"] = (time.time(), snap)
            yes = bt("b.yes_del" if snap[0] == "messages" else "b.yes_rm")
            return await show(confirm_text(snap), [[btn(yes, "go:y")], [btn(bt("b.cancel"), "cm")]])
        if p[1] == "y":
            # Run exactly what was confirmed, and only once, shortly after the confirmation.
            ts, pending = u.pop("pending", (0, None))
            if pending is None or time.time() - ts > BOT_CONFIRM_TTL:
                return await show(bt("expired"), kb_home())
            return await bot_job(event, show, pending, False)
    if a == "sch":
        if len(p) == 1:
            u["sch"] = {"snap": snap, "ts": time.time()}
            rows = [[btn(bt("sch.in", h=h), f"sch:s:{h}") for h in BOT_STARTS], [btn(bt("b.cancel"), "cm")]]
            return await show(bt("sch.start"), rows)
        s = u.get("sch")
        if not s or time.time() - s["ts"] > 600:
            return await show(bt("expired"), kb_home())
        if p[1] == "s" and int(p[2]) in BOT_STARTS:
            s["start"] = int(time.time() // 60 * 60 + int(p[2]) * 3600)
            rows = [[btn(bt(f"r.{m}"), f"sch:r:{m}")] for m in BOT_REPEATS] + [[btn(bt("b.cancel"), "cm")]]
            return await show(bt("sch.repeat"), rows)
        if p[1] == "r" and int(p[2]) in BOT_REPEATS and "start" in s:
            s["repeat"] = int(p[2])
            mode, _did, older, limit, _revoke, name = s["snap"]
            text = bt("sch.confirm", what=bt("m." + mode), name=name, extra=extra_text(older, limit),
                      d=fmt_ts(s["start"]), rep=rep_text(s["repeat"]))
            return await show(text, [[btn(bt("b.ok"), "sch:ok")], [btn(bt("b.cancel"), "cm")]])
        if p[1] == "ok" and "repeat" in s:
            mode, did, older, limit, revoke, _name = s["snap"]
            await add_schedule(did, mode, float(s["start"]), s["repeat"], older, limit, revoke)
            u.pop("sch", None)
            return await show(bt("sch.done"), kb_home())
    await show(home_text(), kb_home())


async def bot_verify(event):
    """The owner has not proven the key yet: only a correct key gets any further."""
    now = time.time()
    if now < B.locked_until:
        return await event.respond(bt("locked", m=-(-int(B.locked_until - now) // 60)))
    text = (event.raw_text or "").strip()
    parts = text.split(None, 1)
    cand = norm_key(parts[1] if text.startswith("/start") and len(parts) > 1 else
                    "" if text.startswith("/") else text)
    if not cand:
        return await event.respond(bt("need_key"))
    try:  # do not leave key guesses lying around in the chat
        await event.delete()
    except Exception:
        pass
    if B.key and hmac.compare_digest(cand.encode(), norm_key(B.key).encode()):
        bot_cfg_update(user_id=S.me_id)
        B.key, B.fails = None, 0
        return await event.respond(bt("verified") + "\n\n" + home_text(), buttons=kb_home())
    B.fails += 1
    if B.fails >= KEY_MAX_FAILS:
        B.key, B.fails, B.locked_until = new_key(), 0, now + KEY_LOCK_SEC
        return await event.respond(bt("locked_out", m=KEY_LOCK_SEC // 60))
    await event.respond(bt("bad_key", n=KEY_MAX_FAILS - B.fails))


def bot_error_text(e):
    return "⚠️ " + render_err(e.msg if isinstance(e, ApiError) else describe(e))


async def bot_on_message(event):
    # Not the signed-in account (or not a private chat): behave as if the bot did not exist.
    if not event.is_private or S.me_id is None or event.sender_id != S.me_id:
        return
    try:
        if not bot_verified():
            return await bot_verify(event)
        text = (event.raw_text or "").strip()
        if B.ui.pop("await", None) == "search" and not text.startswith("/"):
            B.ui["q"] = text[:50]
            await bot_load_dialogs()
            text, rows = bot_list("a", 0)
            return await event.respond(text, buttons=rows)
        await event.respond(home_text(), buttons=kb_home())
    except Exception as e:
        await event.respond(bot_error_text(e))


async def bot_on_callback(event):
    if not event.is_private or S.me_id is None or event.sender_id != S.me_id:
        return
    try:
        if not bot_verified():
            return await event.answer(bt("need_key"), alert=True)
        await bot_action(event, event.data.decode("utf-8", "replace"))
    except Exception as e:
        try:
            await event.respond(bot_error_text(e))
        except Exception:
            pass


async def ensure_me():
    c = await require_client()
    if S.me_id is None:
        if not await c.is_user_authorized():
            raise ApiError(400, T("err.bot_need_login"))
        S.me_id = (await c.get_me()).id
    return S.me_id


async def bot_stop():
    if B.watcher:
        B.watcher.cancel()
        B.watcher = None
    client, B.client = B.client, None
    B.running, B.key, B.ui = False, None, {}
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


async def bot_start(token, lang):
    await ensure_me()
    await bot_stop()
    cfg = load_config()
    old = cfg.get("bot") or {}
    # A different bot token is a different bot: the old binding does not carry over.
    verified = old.get("token") == token and old.get("user_id") == S.me_id
    # In-memory session (None): the bot's auth key is never written to disk.
    client = TelegramClient(None, int(cfg["api_id"]), cfg["api_hash"], device_model="Telegram Cleaner Bot",
                            system_version=platform.system() or "PC", app_version="1.1",
                            flood_sleep_threshold=60)
    await client.connect()
    try:
        await client.sign_in(bot_token=token)
        me = await client.get_me()
    except BaseException:
        await client.disconnect()
        raise
    client.add_event_handler(bot_on_message, events.NewMessage(incoming=True))
    client.add_event_handler(bot_on_callback, events.CallbackQuery())
    B.client, B.running, B.username, B.lang = client, True, me.username, lang
    B.ui, B.fails, B.locked_until = {}, 0, 0.0
    B.key = None if verified else new_key()
    bot_cfg_update(token=token, username=me.username, lang=lang, user_id=S.me_id if verified else None)


def bot_info():
    cfg = bot_cfg()
    verified = bot_verified()
    return {
        "running": B.running, "has_token": bool(cfg.get("token")),
        "username": B.username or cfg.get("username"), "verified": verified,
        "key": B.key if B.running and not verified else None,
        "locked": max(0, int(B.locked_until - time.time())),
    }


async def api_bot(req):
    try:
        await ensure_me()
    except ApiError:
        pass
    return j(bot=bot_info())


async def api_bot_start(req):
    d = await req.json()
    token = str(d.get("token") or "").strip() or bot_cfg().get("token") or ""
    if not token:
        raise ApiError(400, T("err.bot_no_token"))
    if not BOT_TOKEN_RE.match(token):
        raise ApiError(400, T("err.bot_token_bad"))
    if not load_config().get("api_id"):
        raise ApiError(400, T("err.no_client"))
    await bot_start(token, d.get("lang") if d.get("lang") in ("ru", "en") else "en")
    return j(bot=bot_info())


async def api_bot_stop(req):
    await bot_stop()
    return j(bot=bot_info())


async def api_bot_newkey(req):
    """Drop the binding; the bot then asks for a fresh key before it obeys anyone again."""
    bot_cfg_update(user_id=None)
    B.ui, B.fails, B.locked_until = {}, 0, 0.0
    B.key = new_key() if B.running else None
    return j(bot=bot_info())


async def api_bot_forget(req):
    await bot_stop()
    cfg = load_config()
    cfg.pop("bot", None)
    save_config(cfg)
    return j(bot=bot_info())


# ───────────────────────────── HTTP API ─────────────────────────────

def authorized(req):
    return hmac.compare_digest(req.cookies.get(COOKIE, ""), TOKEN)


@web.middleware
async def guard(req, handler):
    # Reject foreign Host headers (DNS rebinding).
    if req.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        return j(403, error=T("err.forbidden"))
    # Exchange the one-time link token for an HttpOnly cookie and drop it from the URL.
    if req.path == "/" and hmac.compare_digest(req.query.get("token", ""), TOKEN):
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, TOKEN, httponly=True, samesite="Strict", path="/")
        raise resp
    if not authorized(req):
        if req.path == "/":
            return web.Response(text=LOCKED_HTML, content_type="text/html", status=401)
        return j(401, error=T("err.unauthorized"))
    # Cross-site forms cannot send application/json without a CORS preflight.
    if req.method == "POST" and req.content_type != "application/json":
        return j(415, error=T("err.json"))
    try:
        return await handler(req)
    except web.HTTPException:
        raise
    except ApiError as e:
        return j(e.status, error=e.msg)
    except Exception as e:
        return j(400, error=describe(e))


async def security_headers(req, resp):
    resp.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if req.path.startswith("/api/") and not req.path.startswith("/api/avatar/"):
        resp.headers["Cache-Control"] = "no-store"


async def index(req):
    nonce = secrets.token_urlsafe(16)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
           "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
           "base-uri 'none'; form-action 'none'")
    return web.Response(text=HTML.replace("<script>", f'<script nonce="{nonce}">', 1),
                        content_type="text/html",
                        headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


async def api_status(req):
    if not S.client:
        return j(configured=False, authorized=False)
    c = await require_client()
    if await c.is_user_authorized():
        me = await c.get_me()
        S.entities[me.id] = me
        S.me_id = me.id
        return j(configured=True, authorized=True, me=user_info(me))
    return j(configured=True, authorized=False)


async def api_send_code(req):
    d = await req.json()
    api_id, api_hash = str(d.get("api_id") or "").strip(), str(d.get("api_hash") or "").strip()
    phone = str(d.get("phone") or "").strip()
    if api_id and api_hash:
        if not api_id.isdigit():
            raise ApiError(400, T("err.api_id_number"))
        cfg = load_config()
        if not S.client or (cfg.get("api_id"), cfg.get("api_hash")) != (int(api_id), api_hash):
            await make_client(api_id, api_hash)
            save_config({**cfg, "api_id": int(api_id), "api_hash": api_hash})
    c = await require_client()
    sent = await c.send_code_request(phone)
    S.phone, S.phone_code_hash = phone, sent.phone_code_hash
    via = "app" if isinstance(sent.type, types.auth.SentCodeTypeApp) else "sms"
    return j(ok=True, via=via, length=getattr(sent.type, "length", 5))


async def api_sign_in(req):
    d = await req.json()
    c = await require_client()
    try:
        await c.sign_in(phone=S.phone, code=str(d.get("code") or "").strip(),
                        phone_code_hash=S.phone_code_hash)
    except SessionPasswordNeededError:
        pwd = await c(functions.account.GetPasswordRequest())
        return j(need_password=True, hint=pwd.hint or "")
    return await api_status(req)


async def api_password(req):
    d = await req.json()
    c = await require_client()
    await c.sign_in(password=d.get("password") or "")
    return await api_status(req)


async def api_logout(req):
    if S.job and S.job.status == "running":
        S.job.stop = True
    # The bot is bound to the signed-in account, so it goes away with the session.
    await bot_stop()
    bot_cfg_update(user_id=None)
    S.me_id = None
    c = await require_client()
    await c.log_out()
    cfg = load_config()
    S.client = None
    if cfg.get("api_id"):
        await make_client(cfg["api_id"], cfg["api_hash"])
    return j(ok=True)


async def api_dialogs(req):
    c = await require_client()
    out = []
    async for d in c.iter_dialogs():
        e = d.entity
        S.entities[d.id] = e
        out.append({
            "id": d.id, "kind": kind_of(e), "name": chat_name(e),
            "preview": preview(d.message),
            "date": d.date.isoformat() if d.date else None,
            "unread": d.unread_count, "has_photo": has_photo(e),
        })
    return j(dialogs=out)


async def api_avatar(req):
    did = int(req.match_info["id"])
    if did not in S.avatars:
        ent = S.entities.get(did)
        data = None
        if ent is not None and has_photo(ent) and S.client:
            try:
                async with avatar_sem:
                    data = await S.client.download_profile_photo(ent, file=bytes)
            except Exception:
                data = None
        S.avatars[did] = data
    data = S.avatars[did]
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


def job_running():
    return S.job is not None and S.job.status == "running"


async def api_job_start(req):
    if job_running():
        raise ApiError(409, T("err.job_running"))
    d = await req.json()
    c = await require_client()
    kind = d.get("kind")
    if kind not in ("messages", "reactions"):
        raise ApiError(400, T("err.bad_mode"))
    did = int(d["dialog_id"])
    ent = S.entities.get(did) or await c.get_entity(did)
    o = SimpleNamespace(
        dry=bool(d.get("dry_run")),
        date_from=parse_date(d.get("date_from")),
        date_to=parse_date(d.get("date_to"), end=True),
        limit=max(0, int(d.get("limit") or 0)) or None,
        revoke=bool(d.get("revoke", True)),
    )
    # Re-check after the awaits above so two parallel requests cannot start two jobs.
    if job_running():
        raise ApiError(409, T("err.job_running"))
    return j(job=launch(kind, did, ent, o).to_dict())


async def api_job(req):
    if not S.job:
        return j(job=None)
    # `since` only makes sense for the job the client already knows about.
    known = int(req.query.get("id", 0)) == S.job.id
    return j(job=S.job.to_dict(max(0, int(req.query.get("since", 0))) if known else 0))


async def api_job_stop(req):
    if job_running():
        S.job.stop = True
    return j(ok=True)


def schedule_from(d):
    try:
        sch = find_schedule(int(d.get("id")))
    except (TypeError, ValueError):
        sch = None
    if sch is None:
        raise ApiError(404, T("err.sched_missing"))
    return sch


async def api_schedules(req):
    return j(schedules=S.schedules)


async def api_schedule_create(req):
    d = await req.json()
    try:
        kind, did = d["kind"], int(d["dialog_id"])
        start, repeat = float(d["start"]), int(d.get("repeat_min") or 0)
        older, limit = int(d.get("older_days") or 0), int(d.get("limit") or 0)
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, T("err.sched_bad"))
    sch = await add_schedule(did, kind, start, repeat, older, limit, bool(d.get("revoke", True)))
    return j(schedule=sch)


async def api_schedule_update(req):
    d = await req.json()
    sch = schedule_from(d)
    enabled = bool(d.get("enabled"))
    set_schedule_enabled(sch, enabled)
    return j(ok=True)


async def api_schedule_run(req):
    await run_schedule_now(schedule_from(await req.json()))
    return j(ok=True)


async def api_schedule_delete(req):
    S.schedules.remove(schedule_from(await req.json()))
    save_schedules()
    return j(ok=True)


async def on_startup(app):
    S.schedules = load_schedules()
    S.sched_task = asyncio.get_running_loop().create_task(scheduler())
    cfg = load_config()
    if cfg.get("api_id") and cfg.get("api_hash"):
        try:
            await make_client(cfg["api_id"], cfg["api_hash"])
        except Exception as e:
            print("Could not connect to Telegram:", e)
    url = f"http://{HOST}:{PORT}/?token={TOKEN}"
    print(f"Telegram Cleaner is running. Open this link (it changes on every launch):\n  {url}\n"
          "Press Ctrl+C to quit.")
    if not os.environ.get("NO_BROWSER"):
        asyncio.get_running_loop().call_later(1, webbrowser.open, url)


async def on_cleanup(app):
    await bot_stop()
    if S.sched_task:
        S.sched_task.cancel()
    if S.job:
        S.job.stop = True
    if S.client:
        await S.client.disconnect()


def main():
    app = web.Application(middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/send_code", api_send_code)
    app.router.add_post("/api/sign_in", api_sign_in)
    app.router.add_post("/api/password", api_password)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/dialogs", api_dialogs)
    app.router.add_get("/api/avatar/{id}", api_avatar)
    app.router.add_post("/api/job/start", api_job_start)
    app.router.add_get("/api/job", api_job)
    app.router.add_post("/api/job/stop", api_job_stop)
    app.router.add_get("/api/schedules", api_schedules)
    app.router.add_post("/api/schedules/create", api_schedule_create)
    app.router.add_post("/api/schedules/update", api_schedule_update)
    app.router.add_post("/api/schedules/run", api_schedule_run)
    app.router.add_post("/api/schedules/delete", api_schedule_delete)
    app.router.add_get("/api/bot", api_bot)
    app.router.add_post("/api/bot/start", api_bot_start)
    app.router.add_post("/api/bot/stop", api_bot_stop)
    app.router.add_post("/api/bot/newkey", api_bot_newkey)
    app.router.add_post("/api/bot/forget", api_bot_forget)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.on_response_prepare.append(security_headers)
    web.run_app(app, host=HOST, port=PORT, print=None)


# ───────────────────────────── frontend ─────────────────────────────

LOCKED_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram Cleaner</title>
<style>body{font-family:Roboto,-apple-system,"Segoe UI",sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;padding:16px;box-sizing:border-box;background:#fff;color:#000;text-align:center}
p{color:#707579;line-height:1.5;max-width:420px;margin:8px auto}
@media (prefers-color-scheme:dark){body{background:#212121;color:#fff}p{color:#aaa}}</style></head>
<body><div><h2>Telegram Cleaner</h2>
<p>Open the link printed in the console where tg_cleaner.py is running.</p>
<p>Откройте ссылку, которую tg_cleaner.py вывел в консоль.</p></div></body></html>"""

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram Cleaner</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='12' fill='%233390ec'/%3E%3Cpath fill='%23fff' d='M5.4 11.8l11.2-4.3c.5-.2 1 .1.8.9l-1.9 9c-.1.6-.5.8-1 .5l-2.9-2.1-1.4 1.3c-.2.2-.3.3-.6.3l.2-2.9 5.3-4.8c.2-.2 0-.3-.3-.1l-6.6 4.1-2.8-.9c-.6-.2-.6-.6.1-.9z'/%3E%3C/svg%3E">
<style>
:root{
  --bg:#fff;--surface:#fff;--text:#000;--muted:#707579;--primary:#3390ec;
  --hover:rgba(112,117,121,.08);--search-bg:#f4f4f5;--border:#dfe1e5;--input-border:#dadce0;
  --danger:#e53935;--danger-hover:rgba(229,57,53,.08);--active:#3390ec;--badge:#4fae4e;
  --bubble-out:#eeffde;--bubble-out-text:#000;--bubble-in:#fff;--meta-out:#4fae4e;--meta-in:#686c72;
  --warn:#c77d00;--service-bg:rgba(74,113,62,.55);--shadow:0 1px 2px rgba(16,35,47,.15);
  --menu-bg:rgba(255,255,255,.94);
  --ic-cal:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M19 4h-1V2h-2v2H8V2H6v2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm0 16H5V10h14v10zm0-12H5V6h14v2z'/%3E%3C/svg%3E");
  --ic-chev:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M7 10l5 5 5-5z'/%3E%3C/svg%3E");
  --ic-check:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z'/%3E%3C/svg%3E");
  --chat-bg:radial-gradient(circle at 12% 18%,#e3e5b6 0,transparent 45%),radial-gradient(circle at 88% 22%,#88b884 0,transparent 50%),radial-gradient(circle at 22% 88%,#6ba587 0,transparent 52%),radial-gradient(circle at 82% 82%,#d5d88d 0,transparent 50%),#a8c695;
  --tail:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='9' height='17'%3E%3Cpath d='M6 17H0V0c.193 2.84.876 5.767 2.05 8.782.904 2.325 2.446 4.485 4.625 6.48A1 1 0 016 17z'/%3E%3C/svg%3E");
  color-scheme:light;
}
:root[data-theme=dark]{
  --bg:#212121;--surface:#212121;--text:#fff;--muted:#aaa;--primary:#8774e1;
  --hover:rgba(170,170,170,.08);--search-bg:#181818;--border:#0f0f0f;--input-border:#5b5b5a;
  --danger:#ff595a;--danger-hover:rgba(255,89,90,.1);--active:#8774e1;--badge:#8774e1;
  --bubble-out:#766ac8;--bubble-out-text:#fff;--bubble-in:#212121;--meta-out:rgba(255,255,255,.6);--meta-in:#aaa;
  --warn:#f0b54a;--service-bg:rgba(0,0,0,.38);--shadow:0 1px 2px rgba(0,0,0,.35);
  --menu-bg:rgba(37,37,37,.94);
  --chat-bg:radial-gradient(circle at 12% 18%,#3a2d5e 0,transparent 48%),radial-gradient(circle at 88% 22%,#1c3a3b 0,transparent 50%),radial-gradient(circle at 22% 88%,#26204a 0,transparent 52%),radial-gradient(circle at 82% 82%,#173030 0,transparent 50%),#0f0f0f;
  color-scheme:dark;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:Roboto,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"Apple Color Emoji","Segoe UI Emoji",sans-serif;font-size:16px;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;overflow:hidden}
button{font:inherit;color:inherit;border:0;background:none;cursor:pointer}
input{font:inherit;color:inherit}
a{color:var(--primary);text-decoration:none}a:hover{text-decoration:underline}
.hidden{display:none!important}
.muted{color:var(--muted)}
::-webkit-scrollbar{width:6px;height:6px}::-webkit-scrollbar-thumb{background:rgba(128,128,128,.35);border-radius:3px}

/* language switch */
.lang{position:fixed;top:12px;right:16px;z-index:40;display:flex;gap:2px;background:var(--surface);border-radius:16px;padding:3px;box-shadow:var(--shadow)}
.lang button{height:26px;min-width:38px;padding:0 10px;border-radius:13px;font-size:13px;font-weight:500;color:var(--muted);transition:background .15s,color .15s}
.lang button:hover{background:var(--hover)}
.lang button.active{background:var(--primary);color:#fff}

/* login */
#login{height:100%;overflow:auto;display:flex;justify-content:center;background:var(--bg)}
.login-box{width:100%;max-width:360px;padding:56px 16px 48px;text-align:center}
.logo{width:160px;height:160px;margin:0 auto 28px;border-radius:50%;background:var(--primary);display:grid;place-items:center;color:#fff}
.login-box h1{font-size:32px;font-weight:500;margin-bottom:10px}
.sub{color:var(--muted);font-size:15px;line-height:1.4;margin-bottom:40px}
.hint{color:var(--muted);font-size:14px;line-height:1.4;margin:-14px 0 24px;text-align:left;padding:0 4px}
.field{position:relative;margin-bottom:24px;text-align:left}
.field input{width:100%;height:54px;border:1px solid var(--input-border);border-radius:10px;padding:0 15px;background:var(--surface);outline:none;font-size:16px;transition:border-color .2s,box-shadow .2s}
.field input:hover{border-color:var(--primary)}
.field input:focus{border-color:var(--primary);box-shadow:inset 0 0 0 1px var(--primary)}
.field label{position:absolute;left:12px;top:16px;padding:0 4px;background:var(--surface);color:var(--muted);pointer-events:none;transition:.2s;white-space:nowrap;max-width:calc(100% - 24px);overflow:hidden;text-overflow:ellipsis}
.field input:focus+label,.field input:not(:placeholder-shown)+label,.field.fixed label{top:-9px;font-size:13px}
.field input:focus+label{color:var(--primary)}
.btn{width:100%;height:54px;border-radius:10px;background:var(--primary);color:#fff;font-weight:500;text-transform:uppercase;font-size:15px;position:relative;transition:filter .15s}
.btn:hover{filter:brightness(1.08)}
.loading{pointer-events:none;color:transparent!important}
.loading::after{content:'';position:absolute;inset:0;margin:auto;width:22px;height:22px;border:2.5px solid #fff;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* app */
#app{display:flex;height:100%}
#sidebar{width:420px;min-width:300px;max-width:40%;display:flex;flex-direction:column;border-right:1px solid var(--border);background:var(--surface);position:relative}
.side-top{display:flex;align-items:center;gap:8px;padding:6px 12px 6px 8px;height:56px;position:relative;flex-shrink:0}
.icon-btn{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;color:var(--muted);flex-shrink:0;transition:background .15s}
.icon-btn:hover{background:var(--hover)}
.search{flex:1;height:42px;border-radius:22px;background:var(--search-bg);display:flex;align-items:center;padding:0 12px;gap:10px;color:var(--muted);border:2px solid transparent;transition:.15s}
.search:focus-within{border-color:var(--primary);background:var(--surface);color:var(--primary)}
.search input{flex:1;border:0;outline:0;background:none;color:var(--text);font-size:16px;min-width:0}
.tabs{display:flex;border-bottom:1px solid var(--border);padding:0 8px;overflow-x:auto;scrollbar-width:none;flex-shrink:0}
.tabs::-webkit-scrollbar{display:none}
.tabs button{padding:0 14px;height:44px;color:var(--muted);font-weight:500;font-size:15px;position:relative;white-space:nowrap;border-radius:10px 10px 0 0;display:flex;align-items:center;gap:6px}
.tabs button:hover{background:var(--hover)}
.tabs button.active{color:var(--primary)}
.tabs button.active::after{content:'';position:absolute;left:10px;right:10px;bottom:0;height:3px;border-radius:3px 3px 0 0;background:var(--primary)}
.chats{flex:1;overflow-y:auto;padding:6px 8px}
.chat-item{display:flex;gap:12px;align-items:center;padding:9px;border-radius:12px;cursor:pointer;height:72px}
.chat-item:hover{background:var(--hover)}
.chat-item.active{background:var(--active);color:#fff}
.chat-item.active .ci-time,.chat-item.active .ci-prev{color:rgba(255,255,255,.85)}
.chat-item.active .badge{background:#fff;color:var(--active)}
.ci-body{flex:1;min-width:0}
.ci-row{display:flex;align-items:center;gap:8px}
.ci-row+.ci-row{margin-top:3px}
.ci-name{flex:1;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ci-time{font-size:12px;color:var(--muted);flex-shrink:0}
.ci-prev{flex:1;color:var(--muted);font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{min-width:22px;height:22px;border-radius:11px;padding:0 7px;background:var(--badge);color:#fff;font-size:13px;font-weight:500;display:grid;place-items:center;flex-shrink:0}
.ava{border-radius:50%;flex-shrink:0;position:relative;overflow:hidden;display:grid;place-items:center;color:#fff;font-weight:500;user-select:none}
.ava img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.ava.c0{background:linear-gradient(#ff885e,#ff516a)}.ava.c1{background:linear-gradient(#ffcd6a,#ffa85c)}
.ava.c2{background:linear-gradient(#e0a2f3,#d669ed)}.ava.c3{background:linear-gradient(#a0de7e,#54cb68)}
.ava.c4{background:linear-gradient(#53edd6,#28c9b7)}.ava.c5{background:linear-gradient(#72d5fd,#2a9ef1)}
.ava.c6{background:linear-gradient(#ffa8a8,#ff719a)}.ava.saved{background:linear-gradient(#72d5fd,#2a9ef1)}
.spinner-wrap{display:grid;place-items:center;padding:40px}
.spinner{width:36px;height:36px;border:3px solid var(--primary);border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
.empty-list{text-align:center;color:var(--muted);padding:40px 16px}

.menu{position:absolute;top:52px;left:8px;width:290px;background:var(--menu-bg);backdrop-filter:blur(30px);border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.2);padding:6px 0;z-index:20;animation:pop .15s ease-out;transform-origin:top left}
@keyframes pop{from{opacity:0;transform:scale(.9)}}
.menu-me{display:flex;gap:12px;align-items:center;padding:8px 14px 12px;border-bottom:1px solid var(--border);margin-bottom:6px;min-width:0}
.menu-me>div:last-child{min-width:0}
#me-name{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#me-phone{font-size:14px}
.menu button{display:flex;align-items:center;gap:20px;width:calc(100% - 12px);margin:0 6px;padding:0 12px;height:40px;border-radius:8px;font-size:15px;font-weight:500}
.menu button svg{color:var(--muted);flex-shrink:0}
.menu button:hover{background:var(--hover)}
.menu button.danger,.menu button.danger svg{color:var(--danger)}
.switch{margin-left:auto;width:34px;height:14px;border-radius:7px;background:var(--input-border);position:relative;transition:.2s}
.switch::after{content:'';position:absolute;left:-2px;top:-3px;width:20px;height:20px;border-radius:50%;background:var(--surface);border:2px solid var(--input-border);box-sizing:border-box;transition:.2s}
.switch.on{background:var(--primary)}.switch.on::after{left:16px;border-color:var(--primary)}

#main{flex:1;position:relative;overflow:hidden;min-width:0}
.chat-bg{position:absolute;inset:0;background:var(--chat-bg)}
.empty{position:absolute;inset:0;display:grid;place-items:center;padding:16px}
.service{display:inline-block;background:var(--service-bg);color:#fff;font-size:15px;font-weight:500;padding:4px 10px;border-radius:14px;line-height:1.35;max-width:100%;text-align:center}
.service-wrap{text-align:center;margin:10px 0}
.chat{position:absolute;inset:0;display:flex;flex-direction:column}
.chat-head{height:56px;background:var(--surface);display:flex;align-items:center;gap:12px;padding:0 120px 0 16px;flex-shrink:0;box-shadow:0 1px 0 var(--border);z-index:1}
.head-info{min-width:0}
#head-name{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#head-sub{font-size:14px}
.back{display:none!important}
.scroll{flex:1;overflow-y:auto;position:relative}
.column{max-width:728px;margin:0 auto;padding:12px 16px 8px}
.card{background:var(--surface);border-radius:15px;box-shadow:var(--shadow);padding:0 16px 8px;margin-bottom:12px}
.card .tabs{margin:0 -16px 16px;padding:0 8px;border-radius:15px 15px 0 0}
.desc{color:var(--muted);font-size:15px;line-height:1.4;margin-bottom:22px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.check{display:flex;align-items:center;gap:14px;cursor:pointer;padding:0 4px 14px;user-select:none;font-size:15px}
.check input{display:none}
.check .box{width:20px;height:20px;border:2px solid var(--input-border);border-radius:5px;position:relative;transition:.15s;flex-shrink:0}
.check input:checked+.box{background:var(--primary);border-color:var(--primary)}
.check input:checked+.box::after{content:'';position:absolute;left:5px;top:1px;width:5px;height:10px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}

.log{display:flex;flex-direction:column;gap:4px;padding-bottom:8px}
.msg{max-width:min(480px,85%);padding:6px 10px;border-radius:15px;font-size:15px;line-height:1.35;position:relative;box-shadow:var(--shadow);overflow-wrap:anywhere;white-space:pre-wrap}
.msg.out{align-self:flex-end;background:var(--bubble-out);color:var(--bubble-out-text);border-bottom-right-radius:0;margin-right:9px}
.msg.in{align-self:flex-start;background:var(--bubble-in);border-bottom-left-radius:0;margin-left:9px}
.msg::after{content:'';position:absolute;bottom:0;width:9px;height:17px;background:inherit;-webkit-mask:var(--tail) no-repeat;mask:var(--tail) no-repeat}
.msg.out::after{right:-8.4px}
.msg.in::after{left:-8.4px;transform:scaleX(-1)}
.msg.warn{color:var(--warn)}.msg.error{color:var(--danger)}
.meta{float:right;margin:8px -2px -4px 12px;font-size:12px;color:var(--meta-in);display:inline-flex;align-items:center;gap:3px;line-height:1}
.msg.out .meta{color:var(--meta-out)}

.composer{display:flex;align-items:flex-end;gap:8px;max-width:728px;width:100%;margin:0 auto;padding:8px 16px 20px;flex-shrink:0;position:relative}
.pill{flex:1;min-width:0;min-height:54px;background:var(--surface);border-radius:15px;border-bottom-right-radius:0;box-shadow:var(--shadow);padding:8px 16px;display:flex;flex-direction:column;justify-content:center;gap:8px;position:relative;margin-right:9px}
.pill::after{content:'';position:absolute;bottom:0;right:-8.4px;width:9px;height:17px;background:var(--surface);-webkit-mask:var(--tail) no-repeat;mask:var(--tail) no-repeat}
#status-text{font-size:15px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#status-text.strong{color:var(--text)}
.progress{height:4px;border-radius:2px;background:var(--hover);overflow:hidden;position:relative}
#progress-bar{height:100%;width:0;background:var(--primary);border-radius:2px;transition:width .3s}
.progress.indet #progress-bar{width:30%;position:absolute;animation:indet 1.2s ease-in-out infinite}
@keyframes indet{from{left:-30%}to{left:100%}}
.round{width:54px;height:54px;border-radius:50%;background:var(--surface);box-shadow:var(--shadow);display:grid;place-items:center;color:var(--primary);flex-shrink:0;transition:transform .1s,filter .15s}
.round:hover{filter:brightness(.96)}
.round:active{transform:scale(.93)}
.round.main{background:var(--primary);color:#fff}
.round.main:hover{filter:brightness(1.08)}
.round.main.danger{background:var(--danger)}

.popup-wrap{position:fixed;inset:0;background:rgba(0,0,0,.3);display:grid;place-items:center;z-index:50;opacity:0;transition:opacity .15s;padding:16px}
.popup-wrap.show{opacity:1}
.popup{background:var(--surface);border-radius:12px;padding:20px 20px 10px;width:100%;max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.25);transform:scale(.95);transition:transform .15s}
.popup-wrap.show .popup{transform:none}
.popup-title{font-size:20px;font-weight:500;margin-bottom:10px}
.popup-text{font-size:15px;line-height:1.45;margin-bottom:16px}
.popup-btns{display:flex;justify-content:flex-end;gap:4px}
.popup-btns button{padding:0 14px;height:38px;border-radius:8px;color:var(--primary);font-weight:500;text-transform:uppercase;font-size:15px}
.popup-btns button:hover{background:var(--hover)}
.popup-btns button.danger{color:var(--danger)}
.popup-btns button.danger:hover{background:var(--danger-hover)}
.popup{max-height:calc(100vh - 32px);overflow-y:auto}
.popup.wide{max-width:520px}
.popup .field{margin-bottom:18px}
.note{font-size:13px;color:var(--muted);line-height:1.4;margin-bottom:12px}
/* theme switch: no element may animate, so everything flips in the same frame */
.theme-switching *,.theme-switching *::before,.theme-switching *::after{transition:none!important}
input[type=number]{-moz-appearance:textfield;appearance:textfield}
input[type=number]::-webkit-inner-spin-button,input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.field.ico::after,.field.chev::after{content:'';position:absolute;right:14px;top:17px;width:20px;height:20px;background:var(--muted);pointer-events:none;-webkit-mask-position:center;mask-position:center;-webkit-mask-size:contain;mask-size:contain;-webkit-mask-repeat:no-repeat;mask-repeat:no-repeat}
.field.ico::after{-webkit-mask-image:var(--ic-cal);mask-image:var(--ic-cal)}
.field.chev::after{-webkit-mask-image:var(--ic-chev);mask-image:var(--ic-chev)}
.field input.dp{cursor:pointer;padding-right:44px}
.sel-btn{width:100%;height:54px;border:1px solid var(--input-border);border-radius:10px;padding:0 44px 0 15px;background:var(--surface);color:var(--text);text-align:left;font-size:16px;outline:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sel-btn:hover,.sel-btn:focus-visible{border-color:var(--primary)}
.float{position:fixed;z-index:80;background:var(--menu-bg);backdrop-filter:blur(30px);border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.25);animation:pop .15s ease-out;transform-origin:top left}
.sel-list{padding:6px;max-height:280px;overflow-y:auto}
.sel-opt{height:40px;padding:0 12px;border-radius:8px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;font-size:15px;font-weight:500}
.sel-opt:hover{background:var(--hover)}
.sel-opt.on{color:var(--primary)}
.sel-opt.on::after{content:'';width:18px;height:18px;background:currentColor;-webkit-mask:var(--ic-check) center/contain no-repeat;mask:var(--ic-check) center/contain no-repeat}
.dp-pop{padding:10px}
.dp-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px}
.dp-head button{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;color:var(--muted)}
.dp-head button:hover{background:var(--hover)}
.dp-title{font-weight:500;text-transform:capitalize}
.dp-week,.dp-grid{display:grid;grid-template-columns:repeat(7,1fr)}
.dp-week span{text-align:center;font-size:12px;color:var(--muted);height:24px;line-height:24px;text-transform:capitalize}
.dp-grid button{height:38px;border-radius:50%;font-size:14px}
.dp-grid button:hover{background:var(--hover)}
.dp-grid button.out{color:var(--muted);opacity:.6}
.dp-grid button.today{color:var(--primary);font-weight:700}
.dp-grid button.sel{background:var(--primary);color:#fff;opacity:1}
.dp-time{display:flex;align-items:center;justify-content:center;gap:6px;margin:8px 0 2px}
.dp-time button{width:32px;height:32px;border-radius:50%;color:var(--primary);font-size:20px;line-height:1}
.dp-time button:hover{background:var(--hover)}
.dp-time span{min-width:30px;text-align:center;font-size:20px;font-weight:500}
.dp-foot{display:flex;justify-content:flex-end;gap:4px;margin-top:6px}
.dp-foot button{padding:0 12px;height:34px;border-radius:8px;color:var(--primary);font-weight:500;text-transform:uppercase;font-size:13px}
.dp-foot button:hover{background:var(--hover)}
.dp-foot button[data-a=clear]{margin-right:auto}
.bot-dock{position:absolute;right:16px;bottom:20px;z-index:5;display:flex;flex-direction:column;align-items:flex-end;gap:10px;max-width:calc(100% - 32px);pointer-events:none}
.bot-dock>*{pointer-events:auto}
.bot-panel{width:340px;max-width:100%;max-height:calc(100vh - 190px);overflow-y:auto;background:var(--surface);border-radius:16px;box-shadow:0 6px 28px rgba(0,0,0,.28),var(--shadow);padding:10px 16px 16px;transform-origin:bottom right;opacity:0;visibility:hidden;transform:scale(.88) translateY(8px);transition:opacity .16s,transform .16s,visibility .16s}
.bot-panel.open{opacity:1;visibility:visible;transform:none}
.bot-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.bot-head h3{flex:1;font-size:17px;font-weight:500}
.bot-status{display:flex;align-items:center;gap:8px;font-size:15px;margin-bottom:14px}
.bot-stack{display:flex;flex-direction:column;gap:10px}
.bot-stack .field{margin-bottom:0}
.bot-stack .note{margin-bottom:0}
.bot-stack .keybox{margin-bottom:0}
.pbtn{position:relative;display:flex;align-items:center;justify-content:center;width:100%;height:44px;border-radius:10px;border:1px solid var(--input-border);color:var(--primary);font-weight:500;font-size:15px;text-decoration:none;text-align:center}
.pbtn:hover{background:var(--hover);text-decoration:none}
.pbtn.primary{background:var(--primary);border-color:var(--primary);color:#fff}
.pbtn.primary:hover{background:var(--primary);filter:brightness(1.08)}
.pbtn.danger{color:var(--danger)}
.pbtn.primary.danger,.pbtn.armed{background:var(--danger);border-color:var(--danger);color:#fff}
.pbtn:disabled{opacity:.6;cursor:default}
.bot-fab.busy .dot{width:12px;height:12px;background:none;box-shadow:none;border:2px solid var(--primary);border-right-color:transparent;animation:spin .8s linear infinite}
.bot-fab{flex-shrink:0;height:44px;padding:0 16px 0 12px;border-radius:22px;background:var(--surface);color:var(--text);box-shadow:var(--shadow),0 2px 8px rgba(0,0,0,.12);display:flex;align-items:center;gap:8px;font-weight:500;font-size:15px}
#main.has-chat .bot-dock{bottom:88px}
.bot-fab svg{color:var(--primary)}
.bot-fab .dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
.bot-fab.on .dot{background:var(--badge);box-shadow:0 0 0 3px rgba(79,174,78,.25)}
.keybox{border:1px dashed var(--primary);border-radius:12px;padding:10px 12px;margin-bottom:14px}
.keyrow{display:flex;align-items:center;gap:8px;margin:6px 0}
.keyrow code{flex:1;font:600 19px/1.25 ui-monospace,Consolas,monospace;letter-spacing:.05em;user-select:all;word-break:break-all}
.keybox a{font-size:14px}
.link-btn{padding:0 10px;height:32px;border-radius:8px;color:var(--primary);font-weight:500;text-transform:uppercase;font-size:13px}
.link-btn:hover{background:var(--hover)}
.popup-btns{flex-wrap:wrap}
.note.warn{color:var(--danger)}
.sched-list{display:flex;flex-direction:column;gap:10px;margin-bottom:12px}
.sched-item{border:1px solid var(--border);border-radius:12px;padding:10px 12px}
.si-name{font-weight:500;margin-bottom:2px}
.si-line{font-size:14px;color:var(--muted);line-height:1.4}
.si-line.strong{color:var(--text)}
.si-btns{display:flex;gap:4px;margin:6px -8px -4px;flex-wrap:wrap}
.si-btns button{padding:0 10px;height:32px;border-radius:8px;color:var(--primary);font-weight:500;text-transform:uppercase;font-size:13px}
.si-btns button:hover{background:var(--hover)}
.si-btns button.danger{color:var(--danger)}
.toast{position:fixed;left:50%;top:56px;transform:translate(-50%,-20px);background:rgba(0,0,0,.8);color:#fff;padding:10px 16px;border-radius:10px;font-size:15px;opacity:0;pointer-events:none;transition:.2s;z-index:60;max-width:calc(100% - 32px)}
.toast.show{opacity:1;transform:translate(-50%,0)}

@media (max-width:760px){
  #sidebar{width:100%;max-width:none;min-width:0;border:0}
  .side-top{padding-right:112px}
  #main{position:fixed;inset:0;transform:translateX(100%);transition:transform .25s;z-index:10}
  #app.show-chat #main{transform:none}
  .back{display:grid!important}
  .chat-head{padding:0 112px 0 8px;gap:8px}
  .column{padding:12px 8px 8px}
  .composer{padding:8px 8px 12px}
  .msg{max-width:90%}
}
</style>
</head>
<body>

<div class="lang" id="lang"><button data-lang="ru">RU</button><button data-lang="en">EN</button></div>

<div id="login" class="hidden">
  <div class="login-box">
    <div class="logo"><svg viewBox="0 0 24 24" width="96" height="96"><path fill="currentColor" d="M5.4 11.8l11.2-4.3c.5-.2 1 .1.8.9l-1.9 9c-.1.6-.5.8-1 .5l-2.9-2.1-1.4 1.3c-.2.2-.3.3-.6.3l.2-2.9 5.3-4.8c.2-.2 0-.3-.3-.1l-6.6 4.1-2.8-.9c-.6-.2-.6-.6.1-.9z"/></svg></div>

    <div id="step-phone" class="step">
      <h1>Telegram Cleaner</h1>
      <p class="sub" data-i18n="login.sub"></p>
      <div id="api-fields">
        <div class="field"><input id="api_id" placeholder=" " inputmode="numeric" autocomplete="off"><label>api_id</label></div>
        <div class="field"><input id="api_hash" placeholder=" " autocomplete="off"><label>api_hash</label></div>
        <p class="hint"><span data-i18n="login.api_hint1"></span><a href="https://my.telegram.org/apps" target="_blank" rel="noopener noreferrer">my.telegram.org</a><span data-i18n="login.api_hint2"></span></p>
      </div>
      <p id="api-saved" class="hint hidden"><span data-i18n="login.api_saved"></span> · <a href="#" id="api-change" data-i18n="login.change"></a></p>
      <div class="field"><input id="phone" type="tel" placeholder=" " autocomplete="tel"><label data-i18n="login.phone"></label></div>
      <button class="btn" id="btn-phone" data-i18n="login.next"></button>
    </div>

    <div id="step-code" class="step hidden">
      <h1 id="code-phone"></h1>
      <p class="sub"><span id="code-sub"></span><br><a href="#" id="back-phone" data-i18n="login.change_phone"></a></p>
      <div class="field"><input id="code" placeholder=" " inputmode="numeric" autocomplete="one-time-code"><label data-i18n="login.code"></label></div>
      <button class="btn" id="btn-code" data-i18n="login.next"></button>
    </div>

    <div id="step-pass" class="step hidden">
      <h1 data-i18n="login.pass_title"></h1>
      <p class="sub" id="pass-sub"></p>
      <div class="field"><input id="password" type="password" placeholder=" " autocomplete="current-password"><label data-i18n="login.password"></label></div>
      <button class="btn" id="btn-pass" data-i18n="login.next"></button>
    </div>
  </div>
</div>

<div id="app" class="hidden">
  <aside id="sidebar">
    <div class="side-top">
      <button class="icon-btn" id="menu-btn" data-i18n-title="menu"></button>
      <label class="search"><span id="search-ic"></span><input id="search" data-i18n-ph="search" autocomplete="off"></label>
      <div id="menu" class="menu hidden">
        <div class="menu-me"><div id="me-ava"></div><div><div id="me-name"></div><div id="me-phone" class="muted"></div></div></div>
        <button data-act="refresh"><span data-ic="refresh"></span><span data-i18n="menu.refresh"></span></button>
        <button data-act="sched"><span data-ic="clock"></span><span data-i18n="menu.sched"></span></button>
        <button data-act="theme"><span data-ic="moon"></span><span data-i18n="menu.night"></span><span class="switch" id="theme-switch"></span></button>
        <button data-act="logout" class="danger"><span data-ic="logout"></span><span data-i18n="menu.logout"></span></button>
      </div>
    </div>
    <div class="tabs" id="folders">
      <button data-tab="all" class="active" data-i18n="tab.all"></button>
      <button data-tab="user" data-i18n="tab.user"></button>
      <button data-tab="group" data-i18n="tab.group"></button>
      <button data-tab="channel" data-i18n="tab.channel"></button>
    </div>
    <div id="chats" class="chats"></div>
  </aside>

  <main id="main">
    <div class="chat-bg"></div>
    <div id="empty" class="empty"><span class="service" data-i18n="empty"></span></div>
    <div id="chat" class="chat hidden">
      <header class="chat-head">
        <button class="icon-btn back" id="back" data-i18n-title="back"></button>
        <div id="head-ava"></div>
        <div class="head-info"><div id="head-name"></div><div id="head-sub" class="muted"></div></div>
      </header>
      <div class="scroll" id="scroll">
        <div class="column">
          <div class="service-wrap"><span class="service" data-i18n="settings"></span></div>
          <div class="card">
            <div class="tabs mode-tabs" id="modes">
              <button data-mode="messages" class="active"><span data-ic="del" data-s="20"></span><span data-i18n="mode.messages"></span></button>
              <button data-mode="reactions"><span data-ic="mood" data-s="20"></span><span data-i18n="mode.reactions"></span></button>
            </div>
            <p id="mode-desc" class="desc"></p>
            <div class="grid">
              <div class="field fixed ico"><input type="text" class="dp" id="date_from" readonly placeholder=" "><label data-i18n="date_from"></label></div>
              <div class="field fixed ico"><input type="text" class="dp" id="date_to" readonly placeholder=" "><label data-i18n="date_to"></label></div>
            </div>
            <div class="field"><input id="limit" type="number" min="0" placeholder=" "><label id="limit-label"></label></div>
            <label class="check" id="revoke-row"><input type="checkbox" id="revoke" checked><span class="box"></span><span data-i18n="revoke"></span></label>
          </div>
          <div id="log" class="log"></div>
        </div>
      </div>
      <div class="composer">
        <div class="pill">
          <div id="status-text"></div>
          <div class="progress hidden" id="progress"><div id="progress-bar"></div></div>
        </div>
        <button class="round" id="btn-count" data-i18n-title="count"></button>
        <button class="round" id="btn-sched" data-i18n-title="sched.add"></button>
        <button class="round main danger" id="btn-start"></button>
        <button class="round main danger hidden" id="btn-stop" data-i18n-title="stop"></button>
      </div>
    </div>
    <div class="bot-dock">
      <section class="bot-panel" id="bot-panel" aria-label="Telegram bot">
        <div class="bot-head"><h3 data-i18n="bot.title"></h3><button class="icon-btn" id="bot-min" data-i18n-title="bot.minimize"></button></div>
        <div class="bot-status"><span id="bot-status"></span></div>
        <div class="bot-stack">
          <div class="field hidden" id="bot-token-row"><input type="password" id="bot-token" placeholder=" " autocomplete="off" spellcheck="false"><label data-i18n="bot.token"></label></div>
          <p class="note hidden" id="bot-token-hint" data-i18n="bot.token_hint"></p>
          <button class="pbtn primary" id="bot-toggle"></button>
          <div class="keybox hidden" id="bot-key-box">
            <div class="note" data-i18n="bot.key_label"></div>
            <div class="keyrow"><code id="bot-key"></code><button class="link-btn" id="bot-copy" data-i18n="bot.copy"></button></div>
            <a class="pbtn" id="bot-link" href="#" target="_blank" rel="noopener noreferrer" data-i18n="bot.open"></a>
          </div>
          <p class="note hidden" id="bot-bound" data-i18n="bot.bound"></p>
          <p class="note warn hidden" id="bot-locked"></p>
          <p class="note warn hidden" id="bot-arm-note"></p>
          <button class="pbtn hidden" id="bot-newkey" data-i18n="bot.newkey"></button>
          <button class="pbtn danger hidden" id="bot-forget" data-i18n="bot.forget"></button>
          <p class="note" data-i18n="bot.note"></p>
        </div>
      </section>
      <button class="bot-fab" id="bot-fab" aria-expanded="false" aria-controls="bot-panel" data-i18n-title="bot.title"><span id="bot-fab-ic"></span><span data-i18n="bot.fab"></span><span class="dot"></span></button>
    </div>
  </main>
</div>

<div id="sched-wrap" class="popup-wrap hidden">
  <div class="popup">
    <div class="popup-title" data-i18n="sched.title"></div>
    <div class="popup-text" id="sched-what"></div>
    <div class="field fixed ico"><input type="text" class="dp" data-time="1" id="sched-start" readonly placeholder=" "><label data-i18n="sched.start"></label></div>
    <div class="field fixed chev">
      <select id="sched-repeat">
        <option value="0" data-i18n="rep.0"></option><option value="60" data-i18n="rep.60"></option>
        <option value="1440" data-i18n="rep.1440"></option><option value="10080" data-i18n="rep.10080"></option>
        <option value="custom" data-i18n="rep.custom"></option>
      </select><label data-i18n="sched.repeat"></label>
    </div>
    <div class="field hidden" id="sched-custom-row"><input type="number" id="sched-custom" min="1" placeholder=" "><label data-i18n="sched.custom_h"></label></div>
    <div class="field"><input type="number" id="sched-older" min="0" placeholder=" " value="0"><label data-i18n="sched.older"></label></div>
    <p class="note" data-i18n="sched.note"></p>
    <div class="popup-btns"><button id="sched-cancel" data-i18n="cancel"></button><button id="sched-ok" data-i18n="sched.btn"></button></div>
  </div>
</div>
<div id="list-wrap" class="popup-wrap hidden">
  <div class="popup wide">
    <div class="popup-title" data-i18n="sched.list_title"></div>
    <div id="sched-list" class="sched-list"></div>
    <div class="popup-btns"><button id="list-close" data-i18n="sched.close"></button></div>
  </div>
</div>
<div id="popup-wrap" class="popup-wrap hidden">
  <div class="popup">
    <div class="popup-title" id="popup-title"></div>
    <div class="popup-text" id="popup-text"></div>
    <div class="popup-btns"><button id="popup-cancel" data-i18n="cancel"></button><button id="popup-ok" class="danger"></button></div>
  </div>
</div>
<div id="toast" class="toast"></div>

<script>
/* ── translations ── */
const L={
ru:{
  'login.sub':'Войдите в свой аккаунт, чтобы удалять свои сообщения и реакции в выбранных чатах.',
  'login.api_hint1':'Получить можно на ','login.api_hint2':' → API development tools.',
  'login.api_saved':'API-ключи сохранены','login.change':'изменить','login.phone':'Номер телефона','login.next':'Далее',
  'login.change_phone':'Изменить номер','login.code':'Код',
  'login.code_app':'Мы отправили код в приложение Telegram на другом вашем устройстве.','login.code_sms':'Мы отправили код по SMS.',
  'login.pass_title':'Введите пароль','login.pass_sub':'Аккаунт защищён облачным паролем.','login.hint':' Подсказка: {hint}','login.password':'Пароль',
  'menu':'Меню','search':'Поиск','menu.refresh':'Обновить чаты','menu.night':'Ночной режим','menu.logout':'Выйти',
  'tab.all':'Все чаты','tab.user':'Личные','tab.group':'Группы','tab.channel':'Каналы',
  'empty':'Выберите чат, чтобы начать очистку','back':'Назад','settings':'Настройки очистки',
  'mode.messages':'Мои сообщения','mode.reactions':'Мои реакции','date_from':'С даты','date_to':'По дату',
  'revoke':'Удалить также у собеседника','count':'Посчитать без удаления','stop':'Остановить','cancel':'Отмена','nothing':'Ничего не найдено',
  'kind.user':'личный чат','kind.bot':'бот','kind.group':'группа','kind.channel':'канал','kind.saved':'ваше облачное хранилище',
  'chat.saved':'Избранное','chat.deleted':'Удалённый аккаунт',
  'desc.messages':'Будут найдены и удалены все сообщения, которые вы отправили в этот чат. Можно ограничить период и количество.',
  'desc.channel':'Будут удалены сообщения канала, отправленные от вашего имени. Можно ограничить период и количество.',
  'desc.reactions':'Будут просмотрены сообщения чата, и с них будут сняты ваши реакции. В больших чатах это занимает время — ограничьте период или число проверяемых сообщений.',
  'limit.messages':'Максимум сообщений (пусто — все)','limit.reactions':'Проверить последних N сообщений (пусто — все)',
  'start.messages':'Удалить сообщения','start.reactions':'Убрать реакции',
  'status.ready_m':'Готово к запуску · можно сначала посчитать сообщения','status.ready_r':'Готово к запуску · можно сначала посчитать реакции',
  'status.other':'Идёт очистка в чате «{name}»',
  'job.found_m':'Найдено ваших сообщений: {n}','job.deleted':'Удалено {n}','job.of_approx':' из ~{n}','job.found_paren':' (найдено {n})',
  'job.scanned':'Проверено {n}','job.of':' из {n}','job.with_r':' · с реакциями: {n}','job.removed':' · убрано: {n}',
  'job.counting':'Подсчёт… ','job.deleting':'Удаление… ','job.cleaning':'Очистка… ','job.error':'Ошибка: {e}','job.stopped':'Остановлено · ','job.done':'Готово · ',
  'confirm.del_text':'Все ваши сообщения в чате «{name}»{period}{limit} будут удалены{revoke}. Это действие нельзя отменить.',
  'confirm.limit_m':' (не более {n})','confirm.revoke':' у вас и у собеседника',
  'confirm.react_text':'Ваши реакции в чате «{name}»{period}{limit} будут сняты.','confirm.limit_r':' на последних {n} сообщениях',
  'btn.delete':'Удалить','btn.remove':'Убрать',
  'period.range':' за период {a} — {b}','period.from':' начиная с {a}','period.to':' по {b} включительно',
  'logout.title':'Выйти','logout.text':'Сессия будет завершена. Для входа снова понадобится код из Telegram.',
  'err.dates':'Дата «с» позже даты «по»','err.api_required':'Укажите api_id и api_hash','err.phone_required':'Введите номер телефона',
  'err.server':'Сервер недоступен — запущен ли tg_cleaner.py?','err.http':'Ошибка {s}',
  'err.phone_invalid':'Неверный номер телефона','err.phone_banned':'Номер заблокирован в Telegram','err.phone_flood':'Слишком много попыток, попробуйте позже',
  'err.code_invalid':'Неверный код','err.code_expired':'Код истёк — запросите новый','err.code_empty':'Введите код',
  'err.password_invalid':'Неверный пароль','err.api_invalid':'Неверные api_id / api_hash','err.auth_unregistered':'Сессия недействительна, войдите заново',
  'err.admin_required':'Нужны права администратора','err.delete_forbidden':'Telegram не разрешает удалить часть сообщений','err.channel_private':'Нет доступа к чату',
  'err.flood':'Слишком много запросов. Подождите {s} с','err.raw':'{text}','err.no_client':'Сначала укажите api_id и api_hash',
  'err.api_id_number':'api_id должен быть числом','err.json':'Ожидается application/json','err.forbidden':'Доступ запрещён',
  'err.unauthorized':'Сессия браузера недействительна — откройте ссылку из консоли','err.job_running':'Уже выполняется другая задача — дождитесь её или остановите',
  'err.bad_mode':'Неизвестный режим',
  'media.photo':'Фото','media.video_note':'Видеосообщение','media.video':'Видео','media.voice':'Голосовое сообщение','media.sticker':'Стикер',
  'media.gif':'GIF','media.audio':'Аудио','media.poll':'Опрос','media.geo':'Геопозиция','media.contact':'Контакт','media.document':'Файл',
  'media.service':'Служебное сообщение','media.message':'Сообщение','reaction.custom':'кастомная реакция',
  'log.count_m':'Подсчёт ваших сообщений в «{name}»','log.clean_m':'Очистка ваших сообщений в «{name}»',
  'log.count_r':'Подсчёт ваших реакций в «{name}»','log.clean_r':'Очистка ваших реакций в «{name}»',
  'log.flood':'Telegram просит подождать {s} с — пауза','log.deleted':'Удалено сообщений: {n} (всего {total})',
  'log.batch_fail':'Не удалось удалить пачку из {n}: {e}','log.reaction_removed':'Убрано {r} · «{snip}»','log.reaction_fail':'Не удалось убрать {r} с «{snip}»: {e}',
  'log.sum_found_m':'Найдено ваших сообщений: {n}','log.sum_deleted':'Удалено сообщений: {n}',
  'log.sum_r_dry':'Проверено сообщений: {s}, с вашими реакциями: {n}','log.sum_r':'Проверено сообщений: {s}, реакций убрано: {n}',
  'log.done':'Готово. {sum}','log.stopped':'Остановлено. {sum}','log.error':'Ошибка: {e}',
  'menu.sched':'Запланированные задачи','sched.add':'Запланировать','sched.title':'Запланировать очистку','sched.btn':'Запланировать',
  'sched.start':'Время запуска','sched.repeat':'Повтор','sched.custom_h':'Повторять каждые N часов',
  'sched.older':'Только старше N дней (0 — все)',
  'sched.note':'Приложение должно быть запущено в момент срабатывания; пропущенный запуск выполнится при следующем старте. Задача выполняется автоматически, без подтверждения, — удаление необратимо. Выбранные даты «с/по» не используются: для повторов задавайте возраст в днях.',
  'rep.0':'Один раз','rep.60':'Каждый час','rep.1440':'Каждый день','rep.10080':'Каждую неделю','rep.custom':'Свой интервал',
  'rep.h':'Каждые {n} ч','rep.m':'Каждые {n} мин',
  'sched.list_title':'Запланированные задачи','sched.empty':'Нет запланированных задач','sched.close':'Закрыть',
  'sched.next':'Следующий запуск: {d}','sched.paused':'Приостановлено','sched.finished':'Выполнено',
  'sched.last':'Последний запуск: {d} — {r}','sched.res_done':'готово ({n})','sched.res_stopped':'остановлено ({n})','sched.res_error':'ошибка: {e}',
  'sched.run':'Запустить','sched.pause':'Пауза','sched.resume':'Возобновить','sched.delete':'Удалить',
  'sched.delete_confirm':'Удалить эту задачу из расписания?','sched.created':'Задача запланирована',
  'sched.older_tag':'старше {n} дн.','sched.limit_tag':'не более {n}',
  'err.sched_start':'Укажите время запуска','err.sched_past':'Время запуска уже прошло','err.sched_bad':'Неверные параметры расписания',
  'err.sched_missing':'Задача не найдена','err.sched_interval':'Минимальный интервал — 5 минут','log.sched_run':'Запуск по расписанию',
  'dp.today':'Сегодня','dp.clear':'Сбросить','dp.ok':'Готово',
  'bot.minimize':'Свернуть','bot.sure':'Нажмите ещё раз',
  'bot.fab':'Бот','bot.title':'Telegram-бот','bot.off':'Бот выключен','bot.on':'Бот работает {u}',
  'bot.token':'Токен бота','bot.token_hint':'Создайте бота у @BotFather и вставьте его токен. Токен хранится только в tg_cleaner.json на этом компьютере и никуда не отправляется, кроме Telegram.',
  'bot.start':'Включить бота','bot.stop':'Выключить бота','bot.copy':'Копировать','bot.copied':'Скопировано','bot.open':'Открыть бота в Telegram',
  'bot.key_label':'Ключ доступа. Отправьте его боту при первом обращении — бот отвечает только вам и только после этого.',
  'bot.bound':'Бот привязан к вашему аккаунту. Все остальные пользователи игнорируются без ответа.',
  'bot.note':'Бот работает, пока запущено это приложение. Он показывает названия чатов и счётчики, но не тексты сообщений. Удаление всегда требует подтверждения в боте.',
  'bot.forget':'Забыть токен','bot.newkey':'Новый ключ','bot.forget_confirm':'Токен и привязка бота будут удалены с этого компьютера. Продолжить?',
  'bot.newkey_confirm':'Привязка будет сброшена: чтобы снова пользоваться ботом, придётся ввести новый ключ.',
  'bot.locked':'Слишком много неверных ключей: бот заблокирован ещё на {m} мин., ключ изменён.',
  'err.bot_need_login':'Сначала войдите в аккаунт','err.bot_token_bad':'Неверный формат токена бота',
  'err.bot_token_invalid':'Токен бота недействителен','err.bot_no_token':'Введите токен бота',
},
en:{
  'login.sub':'Sign in to your account to delete your own messages and reactions in selected chats.',
  'login.api_hint1':'Get them at ','login.api_hint2':' → API development tools.',
  'login.api_saved':'API keys saved','login.change':'change','login.phone':'Phone number','login.next':'Next',
  'login.change_phone':'Change number','login.code':'Code',
  'login.code_app':'We have sent the code to the Telegram app on your other device.','login.code_sms':'We have sent the code via SMS.',
  'login.pass_title':'Enter your password','login.pass_sub':'Your account is protected with a cloud password.','login.hint':' Hint: {hint}','login.password':'Password',
  'menu':'Menu','search':'Search','menu.refresh':'Refresh chats','menu.night':'Night mode','menu.logout':'Log out',
  'tab.all':'All chats','tab.user':'Personal','tab.group':'Groups','tab.channel':'Channels',
  'empty':'Select a chat to start cleaning','back':'Back','settings':'Cleanup settings',
  'mode.messages':'My messages','mode.reactions':'My reactions','date_from':'From date','date_to':'To date',
  'revoke':'Also delete for the other person','count':'Count without deleting','stop':'Stop','cancel':'Cancel','nothing':'Nothing found',
  'kind.user':'private chat','kind.bot':'bot','kind.group':'group','kind.channel':'channel','kind.saved':'your cloud storage',
  'chat.saved':'Saved Messages','chat.deleted':'Deleted Account',
  'desc.messages':'All messages you have sent to this chat will be found and deleted. You can limit the period and the number of messages.',
  'desc.channel':'Channel messages sent under your name will be deleted. You can limit the period and the number of messages.',
  'desc.reactions':'Messages in this chat will be scanned and your reactions removed from them. In large chats this takes a while — limit the period or the number of messages to scan.',
  'limit.messages':'Max messages (empty — all)','limit.reactions':'Scan the last N messages (empty — all)',
  'start.messages':'Delete messages','start.reactions':'Remove reactions',
  'status.ready_m':'Ready · you can count the messages first','status.ready_r':'Ready · you can count the reactions first',
  'status.other':'Cleanup running in “{name}”',
  'job.found_m':'Your messages found: {n}','job.deleted':'Deleted {n}','job.of_approx':' of ~{n}','job.found_paren':' (found {n})',
  'job.scanned':'Scanned {n}','job.of':' of {n}','job.with_r':' · with reactions: {n}','job.removed':' · removed: {n}',
  'job.counting':'Counting… ','job.deleting':'Deleting… ','job.cleaning':'Cleaning… ','job.error':'Error: {e}','job.stopped':'Stopped · ','job.done':'Done · ',
  'confirm.del_text':'All your messages in “{name}”{period}{limit} will be deleted{revoke}. This cannot be undone.',
  'confirm.limit_m':' (at most {n})','confirm.revoke':' for both you and the other person',
  'confirm.react_text':'Your reactions in “{name}”{period}{limit} will be removed.','confirm.limit_r':' on the last {n} messages',
  'btn.delete':'Delete','btn.remove':'Remove',
  'period.range':' from {a} to {b}','period.from':' since {a}','period.to':' up to {b} inclusive',
  'logout.title':'Log out','logout.text':'The session will be terminated. You will need a code from Telegram to sign in again.',
  'err.dates':'“From” date is after “To” date','err.api_required':'Enter api_id and api_hash','err.phone_required':'Enter your phone number',
  'err.server':'Server unreachable — is tg_cleaner.py running?','err.http':'Error {s}',
  'err.phone_invalid':'Invalid phone number','err.phone_banned':'This number is banned in Telegram','err.phone_flood':'Too many attempts, try again later',
  'err.code_invalid':'Invalid code','err.code_expired':'The code has expired — request a new one','err.code_empty':'Enter the code',
  'err.password_invalid':'Wrong password','err.api_invalid':'Invalid api_id / api_hash','err.auth_unregistered':'Session is invalid, please sign in again',
  'err.admin_required':'Admin rights are required','err.delete_forbidden':'Telegram does not allow deleting some of the messages','err.channel_private':'No access to this chat',
  'err.flood':'Too many requests. Wait {s} s','err.raw':'{text}','err.no_client':'Set api_id and api_hash first',
  'err.api_id_number':'api_id must be a number','err.json':'application/json expected','err.forbidden':'Forbidden',
  'err.unauthorized':'Browser session is invalid — open the link printed in the console','err.job_running':'Another job is already running — wait for it or stop it',
  'err.bad_mode':'Unknown mode',
  'media.photo':'Photo','media.video_note':'Video message','media.video':'Video','media.voice':'Voice message','media.sticker':'Sticker',
  'media.gif':'GIF','media.audio':'Audio','media.poll':'Poll','media.geo':'Location','media.contact':'Contact','media.document':'File',
  'media.service':'Service message','media.message':'Message','reaction.custom':'custom reaction',
  'log.count_m':'Counting your messages in “{name}”','log.clean_m':'Deleting your messages in “{name}”',
  'log.count_r':'Counting your reactions in “{name}”','log.clean_r':'Removing your reactions in “{name}”',
  'log.flood':'Telegram asks to wait {s} s — pausing','log.deleted':'Messages deleted: {n} (total {total})',
  'log.batch_fail':'Failed to delete a batch of {n}: {e}','log.reaction_removed':'Removed {r} · “{snip}”','log.reaction_fail':'Failed to remove {r} from “{snip}”: {e}',
  'log.sum_found_m':'Your messages found: {n}','log.sum_deleted':'Messages deleted: {n}',
  'log.sum_r_dry':'Messages scanned: {s}, with your reactions: {n}','log.sum_r':'Messages scanned: {s}, reactions removed: {n}',
  'log.done':'Done. {sum}','log.stopped':'Stopped. {sum}','log.error':'Error: {e}',
  'menu.sched':'Scheduled tasks','sched.add':'Schedule','sched.title':'Schedule cleanup','sched.btn':'Schedule',
  'sched.start':'Start at','sched.repeat':'Repeat','sched.custom_h':'Repeat every N hours',
  'sched.older':'Only older than N days (0 — all)',
  'sched.note':'The app must be running when the task fires; a missed run executes on the next launch. Tasks run automatically with no confirmation — deletion cannot be undone. The “from/to” dates are not used: for repeating tasks set the age in days.',
  'rep.0':'Once','rep.60':'Every hour','rep.1440':'Every day','rep.10080':'Every week','rep.custom':'Custom interval',
  'rep.h':'Every {n} h','rep.m':'Every {n} min',
  'sched.list_title':'Scheduled tasks','sched.empty':'No scheduled tasks','sched.close':'Close',
  'sched.next':'Next run: {d}','sched.paused':'Paused','sched.finished':'Completed',
  'sched.last':'Last run: {d} — {r}','sched.res_done':'done ({n})','sched.res_stopped':'stopped ({n})','sched.res_error':'error: {e}',
  'sched.run':'Run now','sched.pause':'Pause','sched.resume':'Resume','sched.delete':'Delete',
  'sched.delete_confirm':'Remove this task from the schedule?','sched.created':'Task scheduled',
  'sched.older_tag':'older than {n} d','sched.limit_tag':'at most {n}',
  'err.sched_start':'Choose a start time','err.sched_past':'Start time is in the past','err.sched_bad':'Invalid schedule parameters',
  'err.sched_missing':'Task not found','err.sched_interval':'Minimum interval is 5 minutes','log.sched_run':'Scheduled run',
  'dp.today':'Today','dp.clear':'Clear','dp.ok':'Done',
  'bot.minimize':'Minimize','bot.sure':'Press again to confirm',
  'bot.fab':'Bot','bot.title':'Telegram bot','bot.off':'Bot is off','bot.on':'Bot is running {u}',
  'bot.token':'Bot token','bot.token_hint':'Create a bot with @BotFather and paste its token. The token is stored only in tg_cleaner.json on this computer and is sent nowhere except to Telegram.',
  'bot.start':'Turn bot on','bot.stop':'Turn bot off','bot.copy':'Copy','bot.copied':'Copied','bot.open':'Open the bot in Telegram',
  'bot.key_label':'Access key. Send it to the bot on first contact — the bot answers only you, and only after that.',
  'bot.bound':'The bot is bound to your account. Everyone else is ignored without a reply.',
  'bot.note':'The bot works while this app is running. It shows chat names and counters, never message texts. Deleting always needs a confirmation in the bot.',
  'bot.forget':'Forget token','bot.newkey':'New key','bot.forget_confirm':'The bot token and binding will be removed from this computer. Continue?',
  'bot.newkey_confirm':'The binding will be reset: to use the bot again you will have to enter a new key.',
  'bot.locked':'Too many wrong keys: the bot is locked for {m} more min and the key was changed.',
  'err.bot_need_login':'Sign in to your account first','err.bot_token_bad':'Invalid bot token format',
  'err.bot_token_invalid':'The bot token is invalid','err.bot_no_token':'Enter the bot token',
}};
let LANG=(()=>{let l;try{l=localStorage.getItem('lang')}catch(e){}
  return L[l]?l:(/^(ru|be|uk|kk)/i.test(navigator.language||'')?'ru':'en')})();

/* A server value is a plain string or {k: key, a: args}; args may nest such values. */
function tr(v){
  if(v==null) return '';
  if(Array.isArray(v)) return v.map(tr).join(' ');
  if(typeof v==='object'){
    const s=L[LANG][v.k]??L.en[v.k]??v.k, a=v.a||{};
    return s.replace(/\{(\w+)\}/g,(m,k)=>k in a?tr(a[k]):m);
  }
  return String(v);
}
const t=(k,a)=>tr({k,a});
const loc=()=>LANG==='ru'?'ru':'en';

/* ── icons & utils ── */
const I={
  bot:'M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3zM7.5 11.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5S9.83 13 9 13s-1.5-.67-1.5-1.5zM16 17H8v-2h8v2zm-1-4c-.83 0-1.5-.67-1.5-1.5S14.17 10 15 10s1.5.67 1.5 1.5S15.83 13 15 13z',
  chevd:'M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z',
  chevl:'M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z',
  chevr:'M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z',
  clock:'M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z',
  menu:'M3 18h18v-2H3v2zm0-5h18v-2H3v2zm0-7v2h18V6H3z',
  search:'M15.5 14h-.79l-.28-.27A6.471 6.471 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z',
  back:'M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z',
  del:'M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z',
  stop:'M7 7h10v10H7z',
  bookmark:'M17 3H7c-1.1 0-1.99.9-1.99 2L5 21l7-3 7 3V5c0-1.1-.9-2-2-2z',
  mood:'M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2zM12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8zm3.5-9c.83 0 1.5-.67 1.5-1.5S16.33 8 15.5 8 14 8.67 14 9.5s.67 1.5 1.5 1.5zm-7 0c.83 0 1.5-.67 1.5-1.5S9.33 8 8.5 8 7 8.67 7 9.5 7.67 11 8.5 11zm3.5 6.5c2.33 0 4.31-1.46 5.11-3.5H6.89c.8 2.04 2.78 3.5 5.11 3.5z',
  logout:'M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z',
  refresh:'M17.65 6.35A7.958 7.958 0 0 0 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0 1 12 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z',
  moon:'M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.389 5.389 0 0 1-4.4 2.26 5.403 5.403 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z',
};
const ic=(n,s=24)=>`<svg viewBox="0 0 24 24" width="${s}" height="${s}"><path fill="currentColor" d="${I[n]}"/></svg>`;
const CHECKS='<svg viewBox="0 0 19 12" width="17" height="11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M1 6.5l3.5 3.5L11 2.5"/><path d="M8.5 9.8l.3.2L16.5 2.5"/></svg>';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ST={me:null,configured:false,dialogs:[],tab:'all',q:'',cur:null,mode:'messages',job:null,jobLog:[],poll:null,
          codeLen:5,codeVia:'app',passHint:'',schedules:[]};

$('#menu-btn').innerHTML=ic('menu'); $('#search-ic').innerHTML=ic('search'); $('#back').innerHTML=ic('back');
$('#btn-count').innerHTML=ic('search'); $('#btn-stop').innerHTML=ic('stop'); $('#btn-sched').innerHTML=ic('clock'); $('#bot-fab-ic').innerHTML=ic('bot',22); $('#bot-min').innerHTML=ic('chevd');
$$('[data-ic]').forEach(el=>el.outerHTML=ic(el.dataset.ic,+(el.dataset.s||24)));
// Avatars that fail to load fall back to initials (no inline handlers: CSP forbids them).
document.addEventListener('error',e=>{if(e.target.tagName==='IMG'&&e.target.closest('.ava'))e.target.remove()},true);

async function api(path,body){
  const opt=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
  let r; try{r=await fetch(path,opt)}catch(e){throw new Error(t('err.server'))}
  let d={}; try{d=await r.json()}catch(e){}
  if(!r.ok||d.error) throw new Error(d.error?tr(d.error):t('err.http',{s:r.status}));
  return d;
}
let toastT;
function toast(msg){const el=$('#toast');el.textContent=msg;el.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove('show'),3500)}
async function busy(btn,fn){
  if(btn.classList.contains('loading')) return;
  btn.classList.add('loading');
  try{return await fn()}catch(e){toast(e.message)}finally{btn.classList.remove('loading')}
}

/* ── theme & language ── */
function applyTheme(th){
  if(!th){try{th=localStorage.getItem('theme')}catch(e){}}
  if(!th) th=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';
  // Transitions are off while the theme flips (see .theme-switching), so every element changes in the same frame.
  // Reading offsetHeight forces the style recalculation with transitions disabled; only then is the class
  // removed, so nothing can start animating afterwards (and it does not depend on animation frames firing).
  const root=document.documentElement;
  root.classList.add('theme-switching');
  root.dataset.theme=th;
  $('#theme-switch').classList.toggle('on',th==='dark');
  void root.offsetHeight;
  root.classList.remove('theme-switching');
}
function toggleTheme(){
  const th=document.documentElement.dataset.theme==='dark'?'light':'dark';
  try{localStorage.setItem('theme',th)}catch(e){}
  applyTheme(th);
}
function applyLang(l){
  if(l){LANG=l;try{localStorage.setItem('lang',l)}catch(e){}}
  document.documentElement.lang=LANG;
  $$('[data-i18n]').forEach(el=>el.textContent=t(el.dataset.i18n));
  $$('[data-i18n-ph]').forEach(el=>el.placeholder=t(el.dataset.i18nPh));
  $$('[data-i18n-title]').forEach(el=>el.title=t(el.dataset.i18nTitle));
  $$('#lang button').forEach(b=>b.classList.toggle('active',b.dataset.lang===LANG));
  closeFloat(); $$('select').forEach(s=>s._sync&&s._sync()); $$('.dp').forEach(showv); renderBot();
  $('#code-sub').textContent=t(ST.codeVia==='app'?'login.code_app':'login.code_sms');
  $('#pass-sub').textContent=t('login.pass_sub')+(ST.passHint?t('login.hint',{hint:ST.passHint}):'');
  if(ST.me){renderChats(); if(ST.cur) renderChat(); if(listTimer) renderSchedules()}
}
$('#lang').onclick=e=>{const b=e.target.closest('button');if(b&&b.dataset.lang!==LANG)applyLang(b.dataset.lang)};

/* ── login ── */
function step(n){$$('.step').forEach(s=>s.classList.add('hidden'));$('#step-'+n).classList.remove('hidden')}
function showLogin(){
  $('#app').classList.add('hidden'); $('#login').classList.remove('hidden'); step('phone');
  $('#api-fields').classList.toggle('hidden',ST.configured);
  $('#api-saved').classList.toggle('hidden',!ST.configured);
  setTimeout(()=>$(ST.configured?'#phone':'#api_id').focus(),50);
}
$('#api-change').onclick=e=>{e.preventDefault();$('#api-fields').classList.remove('hidden');$('#api-saved').classList.add('hidden');$('#api_id').focus()};
$('#back-phone').onclick=e=>{e.preventDefault();step('phone');$('#phone').focus()};
$('#btn-phone').onclick=()=>busy($('#btn-phone'),async()=>{
  const body={phone:$('#phone').value.trim()};
  if(!$('#api-fields').classList.contains('hidden')){
    body.api_id=$('#api_id').value.trim(); body.api_hash=$('#api_hash').value.trim();
    if(!body.api_id||!body.api_hash) throw new Error(t('err.api_required'));
  }
  if(!body.phone) throw new Error(t('err.phone_required'));
  const d=await api('/api/send_code',body);
  ST.configured=true; ST.codeLen=d.length||5; ST.codeVia=d.via;
  $('#code-phone').textContent=body.phone;
  $('#code-sub').textContent=t(d.via==='app'?'login.code_app':'login.code_sms');
  $('#code').value=''; step('code'); $('#code').focus();
});
$('#btn-code').onclick=()=>busy($('#btn-code'),async()=>{
  const d=await api('/api/sign_in',{code:$('#code').value.trim()});
  if(d.need_password){
    ST.passHint=d.hint||'';
    $('#pass-sub').textContent=t('login.pass_sub')+(ST.passHint?t('login.hint',{hint:ST.passHint}):'');
    $('#password').value=''; step('pass'); $('#password').focus();
  } else {ST.me=d.me; showApp()}
});
$('#btn-pass').onclick=()=>busy($('#btn-pass'),async()=>{
  const d=await api('/api/password',{password:$('#password').value});
  ST.me=d.me; showApp();
});
$('#code').oninput=e=>{const v=e.target.value.replace(/\D/g,'');e.target.value=v;if(v.length===ST.codeLen)$('#btn-code').click()};
for(const [inp,btn] of [['#phone','#btn-phone'],['#api_hash','#btn-phone'],['#code','#btn-code'],['#password','#btn-pass']])
  $(inp).addEventListener('keydown',e=>{if(e.key==='Enter')$(btn).click()});

/* ── chat list ── */
const dname=d=>tr(d.name);
function initials(n){const w=String(n||'?').trim().split(/\s+/).filter(Boolean);return(([...(w[0]||'?')][0]||'')+(w[1]?[...w[1]][0]:'')).toUpperCase()}
function ava(d,size){
  const s=`width:${size}px;height:${size}px;font-size:${Math.round(size*.4)}px`;
  if(d.kind==='saved') return `<div class="ava saved" style="${s}">${ic('bookmark',Math.round(size*.5))}</div>`;
  return `<div class="ava c${Math.abs(d.id)%7}" style="${s}"><span>${esc(initials(dname(d)))}</span>${d.has_photo?`<img src="/api/avatar/${+d.id}" loading="lazy" alt="">`:''}</div>`;
}
function fmtDate(s){
  if(!s) return '';
  const d=new Date(s),n=new Date();
  if(d.toDateString()===n.toDateString()) return d.toLocaleTimeString(loc(),{hour:'2-digit',minute:'2-digit'});
  if(n-d<6*864e5) return d.toLocaleDateString(loc(),{weekday:'short'});
  return d.toLocaleDateString(loc(),{day:'2-digit',month:'2-digit',year:'2-digit'});
}
const hhmm=ts=>new Date(ts*1000).toLocaleTimeString(loc(),{hour:'2-digit',minute:'2-digit'});

async function showApp(){
  $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
  const m=ST.me;
  $('#me-name').textContent=m.name; $('#me-phone').textContent=m.phone?'+'+m.phone:'';
  $('#me-ava').innerHTML=ava({id:m.id,name:m.name,has_photo:m.has_photo},48);
  loadBot();
  await loadDialogs();
  try{
    const d=await api('/api/job?since=0');
    if(d.job){ST.job=d.job;ST.jobLog=d.job.log;if(d.job.status==='running')startPolling()}
  }catch(e){}
}
async function loadDialogs(){
  $('#chats').innerHTML='<div class="spinner-wrap"><div class="spinner"></div></div>';
  try{
    const d=await api('/api/dialogs');
    ST.dialogs=d.dialogs;
    if(ST.cur) ST.cur=ST.dialogs.find(x=>x.id===ST.cur.id)||ST.cur;
    renderChats();
  }catch(e){$('#chats').innerHTML=`<div class="empty-list">${esc(e.message)}</div>`}
}
function renderChats(){
  const q=ST.q.toLowerCase(),tab=ST.tab;
  const list=ST.dialogs.filter(d=>{
    if(tab==='user'&&!['user','bot','saved'].includes(d.kind)) return false;
    if((tab==='group'||tab==='channel')&&d.kind!==tab) return false;
    return !q||dname(d).toLowerCase().includes(q);
  });
  $('#chats').innerHTML=list.length?list.map(d=>`<div class="chat-item${ST.cur&&ST.cur.id===d.id?' active':''}" data-id="${+d.id}">${ava(d,54)}
    <div class="ci-body"><div class="ci-row"><span class="ci-name">${esc(dname(d))}</span><span class="ci-time">${esc(fmtDate(d.date))}</span></div>
    <div class="ci-row"><span class="ci-prev">${esc(tr(d.preview))}</span>${d.unread?`<span class="badge">${d.unread>999?'999+':+d.unread}</span>`:''}</div></div></div>`).join('')
    :`<div class="empty-list">${esc(t('nothing'))}</div>`;
}
$('#chats').onclick=e=>{const it=e.target.closest('.chat-item');if(it)selectChat(+it.dataset.id)};
$('#search').oninput=e=>{ST.q=e.target.value;renderChats()};
$('#folders').onclick=e=>{
  const b=e.target.closest('button'); if(!b) return;
  ST.tab=b.dataset.tab; $$('#folders button').forEach(x=>x.classList.toggle('active',x===b)); renderChats();
};

/* ── menu ── */
$('#menu-btn').onclick=e=>{e.stopPropagation();$('#menu').classList.toggle('hidden')};
document.addEventListener('click',e=>{if(!e.target.closest('#menu'))$('#menu').classList.add('hidden')});
$('#menu').onclick=async e=>{
  const b=e.target.closest('button'); if(!b) return;
  if(b.dataset.act==='theme') return toggleTheme();
  $('#menu').classList.add('hidden');
  if(b.dataset.act==='refresh') loadDialogs();
  if(b.dataset.act==='sched') openList();
  if(b.dataset.act==='logout'&&await confirmBox(t('logout.title'),t('logout.text'),t('logout.title'),true)){
    try{
      await api('/api/logout',{});
      stopPolling(); Object.assign(ST,{me:null,dialogs:[],cur:null,job:null,jobLog:[],passHint:''});
      $('#chat').classList.add('hidden'); $('#empty').classList.remove('hidden'); $('#app').classList.remove('show-chat');
      $('#main').classList.remove('has-chat'); ST.bot=null; setBotPanel(false);
      showLogin();
    }catch(err){toast(err.message)}
  }
};

/* ── chat ── */
function selectChat(id){
  const d=ST.dialogs.find(x=>x.id===id); if(!d) return;
  ST.cur=d;
  $$('.chat-item').forEach(el=>el.classList.toggle('active',+el.dataset.id===id));
  $('#empty').classList.add('hidden'); $('#chat').classList.remove('hidden'); $('#app').classList.add('show-chat');
  $('#main').classList.add('has-chat');
  renderChat();
  $('#scroll').scrollTop=0;
}
function renderChat(){
  const d=ST.cur;
  $('#head-ava').innerHTML=ava(d,42); $('#head-name').textContent=dname(d); $('#head-sub').textContent=t('kind.'+d.kind);
  $('#log').innerHTML='';
  if(ST.job&&ST.job.dialog_id===d.id) appendLog(ST.jobLog.slice(-400));
  renderMode(); renderComposer();
}
$('#back').onclick=()=>$('#app').classList.remove('show-chat');
$('#modes').onclick=e=>{const b=e.target.closest('button');if(!b)return;ST.mode=b.dataset.mode;renderMode();renderComposer()};

function renderMode(){
  const m=ST.mode,d=ST.cur;
  $$('#modes button').forEach(b=>b.classList.toggle('active',b.dataset.mode===m));
  $('#mode-desc').textContent=t(m==='reactions'?'desc.reactions':d.kind==='channel'?'desc.channel':'desc.messages');
  $('#limit-label').textContent=t('limit.'+m);
  $('#revoke-row').classList.toggle('hidden',!(m==='messages'&&(d.kind==='user'||d.kind==='bot')));
  const st=$('#btn-start');
  st.innerHTML=ic(m==='messages'?'del':'mood'); st.classList.toggle('danger',m==='messages');
  st.title=t('start.'+m);
}

function jobText(j){
  const run=j.status==='running';
  let s;
  if(j.kind==='messages'){
    s=j.dry?t('job.found_m',{n:j.found})
      :t('job.deleted',{n:j.done})+(j.total!=null?t('job.of_approx',{n:j.total}):'')+(run?'':t('job.found_paren',{n:j.found}));
  } else {
    s=t('job.scanned',{n:j.scanned})+(j.total!=null?t('job.of',{n:j.total}):'')+t('job.with_r',{n:j.found})+(j.dry?'':t('job.removed',{n:j.done}));
  }
  if(run) return t(j.dry?'job.counting':j.kind==='messages'?'job.deleting':'job.cleaning')+s;
  if(j.status==='error') return t('job.error',{e:j.error});
  return t(j.status==='stopped'?'job.stopped':'job.done')+s;
}
function renderComposer(){
  if(!ST.cur) return;
  const j=ST.job,running=!!j&&j.status==='running',same=!!j&&j.dialog_id===ST.cur.id;
  const mine=same&&(running||j.kind===ST.mode);
  $('#btn-stop').classList.toggle('hidden',!running);
  $('#btn-start').classList.toggle('hidden',running);
  $('#btn-count').classList.toggle('hidden',running);
  $('#btn-sched').classList.toggle('hidden',running);
  const st=$('#status-text'),pr=$('#progress');
  let pct=null;
  if(running&&!same) st.textContent=t('status.other',{name:j.name});
  else if(mine){
    st.textContent=jobText(j);
    if(j.total) pct=Math.min(100,(j.kind==='messages'?j.found:j.scanned)/j.total*100);
  }
  else st.textContent=t(ST.mode==='messages'?'status.ready_m':'status.ready_r');
  st.classList.toggle('strong',mine);
  pr.classList.toggle('hidden',!(running&&same));
  pr.classList.toggle('indet',pct===null);
  $('#progress-bar').style.width=pct===null?'':pct+'%';
}

function appendLog(items){
  if(!items.length) return;
  const box=$('#log'),sc=$('#scroll');
  const near=sc.scrollHeight-sc.scrollTop-sc.clientHeight<160;
  const frag=document.createDocumentFragment();
  for(const it of items){
    const el=document.createElement('div'),text=esc(tr(it.msg));
    if(it.level==='service'){el.className='service-wrap';el.innerHTML=`<span class="service">${text}</span>`}
    else{
      const out=it.level==='info';
      el.className='msg '+(out?'out':'in '+(it.level==='warn'?'warn':'error'));
      el.innerHTML=`${text}<span class="meta">${esc(hhmm(it.t))}${out?CHECKS:''}</span>`;
    }
    frag.appendChild(el);
  }
  box.appendChild(frag);
  while(box.children.length>400) box.firstChild.remove();
  if(near) sc.scrollTop=sc.scrollHeight;
}

/* ── jobs ── */
let pollBusy=false;
function startPolling(){stopPolling();ST.poll=setInterval(pollJob,800);pollJob()}
function stopPolling(){clearInterval(ST.poll);ST.poll=null}
async function pollJob(){
  if(pollBusy) return; pollBusy=true;
  try{
    const prev=ST.job, cur=prev?prev.id:0;
    const d=await api(`/api/job?since=${ST.jobLog.length}&id=${cur}`);
    if(!d.job) return stopPolling();
    // A different job (e.g. started by the scheduler): the server sent its whole log.
    if(d.job.id!==cur){ST.jobLog=[];$('#log').innerHTML=''}
    ST.job=d.job; ST.jobLog.push(...d.job.log);
    if(ST.cur&&ST.cur.id===d.job.dialog_id) appendLog(d.job.log);
    renderComposer();
    if(d.job.status==='running'){
      if(!ST.poll) ST.poll=setInterval(pollJob,800);
    } else {
      stopPolling();
      if(d.job.id!==cur||prev.status==='running'){
        if(d.job.status==='error') toast(tr(d.job.error));
        if(!d.job.dry&&d.job.kind==='messages'&&d.job.done) loadDialogs();
        if(listTimer) loadSchedules();
      }
    }
  }catch(e){}finally{pollBusy=false}
}
// Picks up jobs that the scheduler starts on its own.
setInterval(()=>{if(ST.me&&!ST.poll)pollJob()},4000);
function period(o){
  const f=x=>x.split('-').reverse().join('.');
  if(o.date_from&&o.date_to) return t('period.range',{a:f(o.date_from),b:f(o.date_to)});
  if(o.date_from) return t('period.from',{a:f(o.date_from)});
  if(o.date_to) return t('period.to',{b:f(o.date_to)});
  return '';
}
async function startJob(dry){
  const d=ST.cur; if(!d) return;
  const o={dialog_id:d.id,kind:ST.mode,dry_run:dry,date_from:getv($('#date_from')),date_to:getv($('#date_to')),
           limit:$('#limit').value,revoke:$('#revoke').checked};
  if(o.date_from&&o.date_to&&o.date_from>o.date_to) return toast(t('err.dates'));
  if(!dry){
    const isM=ST.mode==='messages';
    const revoke=isM&&!$('#revoke-row').classList.contains('hidden')&&o.revoke?t('confirm.revoke'):'';
    const text=isM
      ?t('confirm.del_text',{name:dname(d),period:period(o),limit:o.limit?t('confirm.limit_m',{n:o.limit}):'',revoke})
      :t('confirm.react_text',{name:dname(d),period:period(o),limit:o.limit?t('confirm.limit_r',{n:o.limit}):''});
    if(!await confirmBox(t('start.'+ST.mode),text,t(isM?'btn.delete':'btn.remove'),isM)) return;
  }
  try{
    const r=await api('/api/job/start',o);
    ST.job=r.job; ST.jobLog=[]; $('#log').innerHTML='';
    renderComposer(); startPolling();
  }catch(e){toast(e.message)}
}
$('#btn-count').onclick=()=>startJob(true);
$('#btn-start').onclick=()=>startJob(false);
$('#btn-stop').onclick=()=>api('/api/job/stop',{}).catch(e=>toast(e.message));

/* ── schedules ── */
const pad=n=>String(n).padStart(2,'0');
const localInput=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const fmtTs=s=>new Date(s*1000).toLocaleString(loc(),{dateStyle:'short',timeStyle:'short'});
function openModal(w){w.classList.remove('hidden');requestAnimationFrame(()=>w.classList.add('show'))}
function closeModal(w){w.classList.remove('show');setTimeout(()=>w.classList.add('hidden'),150)}
function repLabel(m){
  if(!m) return t('rep.0');
  if(m===60||m===1440||m===10080) return t('rep.'+m);
  return m%60===0?t('rep.h',{n:m/60}):t('rep.m',{n:m});
}

function openSchedForm(){
  const d=ST.cur; if(!d) return;
  const dt=new Date(Date.now()+3600e3); dt.setMinutes(0,0,0);
  setv($('#sched-start'),localInput(dt));
  $('#sched-repeat').value='0'; $('#sched-repeat')._sync(); $('#sched-custom-row').classList.add('hidden'); $('#sched-custom').value='';
  $('#sched-older').value='0';
  $('#sched-what').textContent=t('start.'+ST.mode)+' — '+dname(d);
  openModal($('#sched-wrap'));
}
$('#btn-sched').onclick=openSchedForm;
$('#sched-repeat').onchange=e=>$('#sched-custom-row').classList.toggle('hidden',e.target.value!=='custom');
$('#sched-cancel').onclick=()=>closeModal($('#sched-wrap'));
$('#sched-wrap').onclick=e=>{if(e.target===e.currentTarget)closeModal(e.currentTarget)};
$('#sched-ok').onclick=async()=>{
  const d=ST.cur, sd=getv($('#sched-start')), startMs=sd?parseIso(sd).getTime():NaN;
  if(!d) return;
  if(isNaN(startMs)) return toast(t('err.sched_start'));
  const sel=$('#sched-repeat').value;
  const rep=sel==='custom'?Math.round((+$('#sched-custom').value||0)*60):+sel;
  if(sel==='custom'&&rep<5) return toast(t('err.sched_interval'));
  const btn=$('#sched-ok'); btn.disabled=true;
  try{
    await api('/api/schedules/create',{dialog_id:d.id,kind:ST.mode,start:startMs/1000,repeat_min:rep,
      older_days:+$('#sched-older').value||0,limit:$('#limit').value,revoke:$('#revoke').checked});
    closeModal($('#sched-wrap')); toast(t('sched.created'));
  }catch(e){toast(e.message)}finally{btn.disabled=false}
};

let listTimer=null;
async function loadSchedules(){
  try{const d=await api('/api/schedules');ST.schedules=d.schedules;renderSchedules()}catch(e){}
}
async function openList(){
  openModal($('#list-wrap'));
  await loadSchedules();
  if(!listTimer) listTimer=setInterval(loadSchedules,5000);
}
function closeList(){closeModal($('#list-wrap'));clearInterval(listTimer);listTimer=null}
$('#list-close').onclick=closeList;
$('#list-wrap').onclick=e=>{if(e.target===e.currentTarget)closeList()};

function renderSchedules(){
  const list=[...ST.schedules].sort((a,b)=>(b.enabled-a.enabled)||((a.next_run??1e15)-(b.next_run??1e15)));
  $('#sched-list').innerHTML=list.length?list.map(s=>{
    const tags=[t('start.'+s.kind),repLabel(s.repeat_min)];
    if(s.older_days) tags.push(t('sched.older_tag',{n:s.older_days}));
    if(s.limit) tags.push(t('sched.limit_tag',{n:s.limit}));
    const state=s.enabled&&s.next_run?t('sched.next',{d:fmtTs(s.next_run)}):s.next_run?t('sched.paused'):t('sched.finished');
    let last='';
    if(s.last_run){
      const res=s.last_status==='error'?t('sched.res_error',{e:tr(s.last_error)})
        :t(s.last_status==='stopped'?'sched.res_stopped':'sched.res_done',{n:s.last_done});
      last=`<div class="si-line">${esc(t('sched.last',{d:fmtTs(s.last_run),r:res}))}</div>`;
    }
    return `<div class="sched-item" data-id="${+s.id}"><div class="si-name">${esc(tr(s.name))}</div>
      <div class="si-line">${esc(tags.join(' · '))}</div><div class="si-line strong">${esc(state)}</div>${last}
      <div class="si-btns"><button data-act="run">${esc(t('sched.run'))}</button>${s.next_run?`<button data-act="toggle">${esc(t(s.enabled?'sched.pause':'sched.resume'))}</button>`:''}<button data-act="del" class="danger">${esc(t('sched.delete'))}</button></div></div>`;
  }).join(''):`<div class="empty-list">${esc(t('sched.empty'))}</div>`;
}
$('#sched-list').onclick=async e=>{
  const b=e.target.closest('button'); if(!b) return;
  const id=+b.closest('.sched-item').dataset.id, s=ST.schedules.find(x=>x.id===id), act=b.dataset.act;
  if(!s) return;
  if(act==='del'&&!await confirmBox(t('sched.delete'),t('sched.delete_confirm'),t('sched.delete'),true)) return;
  const call={run:['run',{id}],toggle:['update',{id,enabled:!s.enabled}],del:['delete',{id}]}[act];
  try{
    await api('/api/schedules/'+call[0],call[1]);
    await loadSchedules();
    if(act==='run') pollJob();
  }catch(err){toast(err.message)}
};

/* ── floating layer: styled dropdown and calendar instead of the native pickers ── */
const FL={el:null,anchor:null};
function closeFloat(){if(FL.el){FL.el.remove();FL.el=FL.anchor=null}}
function openFloat(el,anchor,w){
  closeFloat();
  el.classList.add('float'); if(w) el.style.width=w+'px';
  el.style.visibility='hidden'; document.body.appendChild(el);
  const r=anchor.getBoundingClientRect(),h=el.offsetHeight,ew=el.offsetWidth;
  let top=r.bottom+4; if(top+h>innerHeight-8&&r.top-4-h>8) top=r.top-4-h;
  top=Math.max(8,Math.min(top,innerHeight-h-8));
  el.style.top=top+'px'; el.style.left=Math.max(8,Math.min(r.left,innerWidth-ew-8))+'px'; el.style.visibility='';
  FL.el=el; FL.anchor=anchor;
}
document.addEventListener('mousedown',e=>{if(FL.el&&!FL.el.contains(e.target)&&!FL.anchor.contains(e.target))closeFloat()},true);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&FL.el){closeFloat();e.stopPropagation()}},true);
addEventListener('scroll',e=>{if(FL.el&&!FL.el.contains(e.target))closeFloat()},true);
addEventListener('resize',closeFloat);

function initSelect(sel){
  sel.style.display='none';
  const btn=document.createElement('button'); btn.type='button'; btn.className='sel-btn'; sel.after(btn);
  sel._sync=()=>{const o=sel.selectedOptions[0];btn.textContent=o?o.textContent:''};
  btn.onclick=()=>{
    if(FL.anchor===btn) return closeFloat();
    const list=document.createElement('div'); list.className='sel-list';
    for(const o of sel.options){
      const it=document.createElement('div'); it.className='sel-opt'+(o.value===sel.value?' on':''); it.textContent=o.textContent;
      it.onclick=()=>{sel.value=o.value;sel._sync();closeFloat();sel.dispatchEvent(new Event('change'))};
      list.appendChild(it);
    }
    openFloat(list,btn,btn.offsetWidth);
  };
  sel._sync();
}

/* dates are kept as ISO strings in data-iso; the visible text is only formatting */
const getv=i=>i.dataset.iso||'';
const toIso=(d,tm)=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`+(tm?`T${pad(d.getHours())}:${pad(d.getMinutes())}`:'');
function parseIso(s){const m=/^(\d+)-(\d+)-(\d+)(?:T(\d+):(\d+))?/.exec(s);return new Date(+m[1],m[2]-1,+m[3],+(m[4]||0),+(m[5]||0))}
function showv(i){
  const s=getv(i);
  i.value=s?parseIso(s).toLocaleString(loc(),i.dataset.time?{dateStyle:'medium',timeStyle:'short'}:{dateStyle:'medium'}):'';
}
function setv(i,s){i.dataset.iso=s||'';showv(i)}
function initDate(i){
  i.addEventListener('click',()=>openDate(i));
  i.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openDate(i)}});
}
function openDate(input){
  if(FL.anchor===input) return closeFloat();
  const tm=!!input.dataset.time,cur=getv(input)?parseIso(getv(input)):null,now=new Date(),base=cur||now;
  const st={y:base.getFullYear(),m:base.getMonth(),sel:cur,h:cur?cur.getHours():(now.getHours()+1)%24,mi:cur?cur.getMinutes():0};
  const el=document.createElement('div'); el.className='dp-pop';
  const commit=d=>{setv(input,d?toIso(d,tm):'');input.dispatchEvent(new Event('change'));closeFloat()};
  const draw=()=>{
    const off=(new Date(st.y,st.m,1).getDay()+6)%7;
    const title=new Date(st.y,st.m,1).toLocaleDateString(loc(),{month:'long',year:'numeric'});
    let h=`<div class="dp-head"><button type="button" data-a="prev">${ic('chevl')}</button><div class="dp-title">${esc(title)}</div><button type="button" data-a="next">${ic('chevr')}</button></div><div class="dp-week">`;
    for(let i=0;i<7;i++) h+=`<span>${esc(new Date(2024,0,1+i).toLocaleDateString(loc(),{weekday:'short'}))}</span>`;
    h+='</div><div class="dp-grid">';
    const today=toIso(now,false),sel=st.sel?toIso(st.sel,false):'';
    for(let i=0;i<42;i++){
      const d=new Date(st.y,st.m,1-off+i),iso=toIso(d,false);
      h+=`<button type="button" data-d="${iso}" class="${d.getMonth()!==st.m?'out ':''}${iso===today?'today ':''}${iso===sel?'sel':''}">${d.getDate()}</button>`;
    }
    h+='</div>';
    if(tm) h+=`<div class="dp-time"><button type="button" data-a="h-">−</button><span>${pad(st.h)}</span><button type="button" data-a="h+">+</button><b>:</b><button type="button" data-a="m-">−</button><span>${pad(st.mi)}</span><button type="button" data-a="m+">+</button></div>`;
    h+=`<div class="dp-foot"><button type="button" data-a="clear">${esc(t('dp.clear'))}</button><button type="button" data-a="today">${esc(t('dp.today'))}</button>${tm?`<button type="button" data-a="ok">${esc(t('dp.ok'))}</button>`:''}</div>`;
    el.innerHTML=h;
  };
  el.onclick=e=>{
    const b=e.target.closest('button'); if(!b) return;
    const a=b.dataset.a;
    if(b.dataset.d){
      const p=parseIso(b.dataset.d);
      if(!tm) return commit(p);
      p.setHours(st.h,st.mi); st.sel=p; st.y=p.getFullYear(); st.m=p.getMonth();
    }
    else if(a==='prev'||a==='next'){const n=new Date(st.y,st.m+(a==='next'?1:-1),1);st.y=n.getFullYear();st.m=n.getMonth()}
    else if(a==='h+'||a==='h-') st.h=(st.h+(a==='h+'?1:23))%24;
    else if(a==='m+'||a==='m-') st.mi=(st.mi+(a==='m+'?5:55))%60;
    else if(a==='clear') return commit(null);
    else if(a==='today'){
      const n=new Date(); if(!tm) return commit(n);
      st.sel=new Date(n.getFullYear(),n.getMonth(),n.getDate(),st.h,st.mi); st.y=n.getFullYear(); st.m=n.getMonth();
    }
    else if(a==='ok'){const d=st.sel||new Date(); return commit(new Date(d.getFullYear(),d.getMonth(),d.getDate(),st.h,st.mi))}
    if(st.sel&&tm) st.sel.setHours(st.h,st.mi);
    draw();
  };
  draw(); openFloat(el,input,304);
}

/* ── Telegram bot panel ── */
async function loadBot(){try{const d=await api('/api/bot');ST.bot=d.bot;renderBot()}catch(e){}}
function renderBot(){
  const b=ST.bot||{running:false,has_token:false,verified:false,key:null,locked:0,username:null},busy=ST.botBusy;
  $('#bot-fab').classList.toggle('on',b.running&&!busy);
  $('#bot-fab').classList.toggle('busy',!!busy);
  $('#bot-status').textContent=b.running?t('bot.on',{u:b.username?'@'+b.username:''}):t('bot.off');
  const needToken=!b.has_token&&!b.running;
  $('#bot-token-row').classList.toggle('hidden',!needToken);
  $('#bot-token-hint').classList.toggle('hidden',!needToken);
  $('#bot-token').disabled=!!busy;
  const showKey=b.running&&!b.verified&&!!b.key;
  $('#bot-key-box').classList.toggle('hidden',!showKey);
  if(showKey){
    $('#bot-key').textContent=b.key;
    const a=$('#bot-link'); a.classList.toggle('hidden',!b.username);
    if(b.username) a.href='https://t.me/'+encodeURIComponent(b.username);
  }
  $('#bot-bound').classList.toggle('hidden',!(b.running&&b.verified));
  const lk=$('#bot-locked'); lk.classList.toggle('hidden',!b.locked);
  if(b.locked) lk.textContent=t('bot.locked',{m:Math.ceil(b.locked/60)});
  const tg=$('#bot-toggle'); tg.textContent=t(b.running?'bot.stop':'bot.start');
  tg.classList.toggle('danger',b.running); tg.classList.toggle('loading',!!busy);
  $('#bot-forget').classList.toggle('hidden',!b.has_token);
  $('#bot-newkey').classList.toggle('hidden',!(b.running&&b.verified));
  $('#bot-forget').disabled=$('#bot-newkey').disabled=!!busy;
}
async function botCall(path,body){
  try{const d=await api(path,body);ST.bot=d.bot;return true}catch(e){toast(e.message);return false}
}
// The panel is a floating window next to the Bot button: no backdrop, it stays open until minimized.
function setBotPanel(open){
  $('#bot-panel').classList.toggle('open',open);
  $('#bot-fab').setAttribute('aria-expanded',String(open));
  if(open) loadBot(); else disarmAll();
}
$('#bot-fab').onclick=()=>setBotPanel(!$('#bot-panel').classList.contains('open'));
$('#bot-min').onclick=()=>setBotPanel(false);
$('#bot-copy').onclick=async()=>{try{await navigator.clipboard.writeText($('#bot-key').textContent);toast(t('bot.copied'))}catch(e){}};
$('#bot-toggle').onclick=async()=>{
  if(ST.botBusy) return;
  const running=!!(ST.bot&&ST.bot.running);
  ST.botBusy=running?'stopping':'starting'; renderBot();
  const ok=await botCall(running?'/api/bot/stop':'/api/bot/start',running?{}:{token:$('#bot-token').value.trim(),lang:LANG});
  ST.botBusy=null;
  if(ok) $('#bot-token').value='';
  renderBot();
};
// Destructive panel buttons ask for a second press instead of opening a dimming dialog.
const ARMABLE=['#bot-forget','#bot-newkey'];
function disarm(sel){
  const b=$(sel); clearTimeout(b._t);
  if(b.classList.contains('armed')){b.classList.remove('armed');b.textContent=t(b.dataset.i18n)}
  $('#bot-arm-note').classList.add('hidden');
}
function disarmAll(){ARMABLE.forEach(disarm)}
function armable(sel,noteKey,path){
  $(sel).onclick=async()=>{
    const b=$(sel);
    if(b.classList.contains('armed')){
      disarm(sel);
      if(await botCall(path,{})) renderBot();
      return;
    }
    disarmAll(); b.classList.add('armed'); b.textContent=t('bot.sure');
    const note=$('#bot-arm-note'); note.textContent=t(noteKey); note.classList.remove('hidden');
    b._t=setTimeout(()=>disarm(sel),5000);
  };
}
armable('#bot-forget','bot.forget_confirm','/api/bot/forget');
armable('#bot-newkey','bot.newkey_confirm','/api/bot/newkey');

/* ── popup ── */
function confirmBox(title,text,okText,danger){
  return new Promise(res=>{
    const w=$('#popup-wrap'),ok=$('#popup-ok'),cancel=$('#popup-cancel');
    $('#popup-title').textContent=title; $('#popup-text').textContent=text;
    ok.textContent=okText; ok.classList.toggle('danger',!!danger);
    w.classList.remove('hidden'); requestAnimationFrame(()=>w.classList.add('show'));
    const key=e=>{if(e.key==='Escape')done(false);if(e.key==='Enter')done(true)};
    const done=v=>{
      w.classList.remove('show'); setTimeout(()=>w.classList.add('hidden'),150);
      ok.onclick=cancel.onclick=w.onclick=null; document.removeEventListener('keydown',key); res(v);
    };
    ok.onclick=()=>done(true); cancel.onclick=()=>done(false);
    w.onclick=e=>{if(e.target===w)done(false)};
    document.addEventListener('keydown',key);
  });
}

/* ── boot ── */
applyTheme(); initSelect($('#sched-repeat')); $$('.dp').forEach(initDate); applyLang();
setInterval(()=>{if(ST.me)loadBot()},4000);
(async()=>{
  try{
    const s=await api('/api/status'); ST.configured=s.configured;
    if(s.authorized){ST.me=s.me;showApp()} else showLogin();
  }catch(e){toast(e.message);showLogin()}
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
