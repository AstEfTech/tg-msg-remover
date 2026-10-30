#!/usr/bin/env python3
"""
Generates media/demo.gif for the README.

The real frontend from tg_cleaner.py is served by a mock backend with fake chats
(no Telegram account, no real data), driven by headless Chromium and recorded as screenshots.

Requirements:  pip install aiohttp telethon playwright pillow
               playwright install chromium
Run:           python media/make_demo.py
"""
import asyncio
import io
import sys
import time
from pathlib import Path

from aiohttp import web
from PIL import Image
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import tg_cleaner as tg  # noqa: E402

OUT = HERE / "demo.gif"
PORT = 8765
SIZE = (1000, 620)
SCALE = 0.8          # output size relative to SIZE
TOTAL = 670         # fake number of messages in the demo chat

NOW = time.time()
ISO = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - ago))
DIALOGS = [
    {"id": 1, "kind": "user", "name": "Alex Morgan", "preview": "See you tomorrow!", "date": ISO(300), "unread": 0, "has_photo": False},
    {"id": 2, "kind": "group", "name": "Weekend Hiking", "preview": "Photos from the trip", "date": ISO(3600), "unread": 3, "has_photo": False},
    {"id": 3, "kind": "user", "name": "Sam Rivera", "preview": "Thanks!", "date": ISO(7200), "unread": 0, "has_photo": False},
    {"id": 4, "kind": "channel", "name": "Tech News", "preview": "New release is out", "date": ISO(20000), "unread": 12, "has_photo": False},
    {"id": 5, "kind": "group", "name": "Book Club", "preview": "Next meeting on Friday", "date": ISO(90000), "unread": 0, "has_photo": False},
    {"id": 6, "kind": "user", "name": "Taylor Kim", "preview": "Voice message", "date": ISO(200000), "unread": 0, "has_photo": False},
    {"id": 7, "kind": "saved", "name": tg.T("chat.saved"), "preview": "Shopping list", "date": ISO(400000), "unread": 0, "has_photo": False},
]
ME = {"id": 100, "name": "Demo User", "phone": "10000000000", "has_photo": False}

BOT = dict(running=False, has_token=False, username=None, verified=False, key=None, locked=0)
job = None


async def api_bot(req):
    return tg.j(bot=BOT)


async def fake_job(j):
    """Walk a Job through the same states the real one goes through."""
    j.add("service", ("log.count_" if j.dry else "log.clean_") + "m", name=j.name)
    j.total = TOTAL
    step = 100
    while j.found < TOTAL:
        await asyncio.sleep(0.25 if j.dry else 0.45)
        n = min(step, TOTAL - j.found)
        j.found += n
        if not j.dry:
            j.done += n
            j.add("info", "log.deleted", n=n, total=j.done)
    summary = tg.T("log.sum_found_m", n=j.found) if j.dry else tg.T("log.sum_deleted", n=j.done)
    j.status = "done"
    j.add("service", "log.done", sum=summary)


async def api_job_start(req):
    global job
    d = await req.json()
    job = tg.Job("messages", d["dialog_id"], "Alex Morgan", bool(d.get("dry_run")))
    asyncio.get_running_loop().create_task(fake_job(job))
    return tg.j(job=job.to_dict())


async def api_job(req):
    if not job:
        return tg.j(job=None)
    known = int(req.query.get("id", 0)) == job.id
    return tg.j(job=job.to_dict(int(req.query.get("since", 0)) if known else 0))


async def api_bot_start(req):
    BOT.update(running=True, has_token=True, username="demo_cleaner_bot", key="K7QM-4XH2-9WTP-R3DF")
    return tg.j(bot=BOT)


def static_json(data):
    async def handler(req):
        return tg.j(**data)
    return handler


def make_app():
    app = web.Application()
    routes = {
        "/api/status": dict(configured=True, authorized=True, me=ME),
        "/api/dialogs": dict(dialogs=DIALOGS),
        "/api/schedules": dict(schedules=[]),
    }
    for path, data in routes.items():
        app.router.add_get(path, static_json(data))
    app.router.add_get("/api/bot", api_bot)
    app.router.add_post("/api/bot/start", api_bot_start)
    app.router.add_get("/", tg.index)
    app.router.add_post("/api/job/start", api_job_start)
    app.router.add_get("/api/job", api_job)
    return app


async def main():
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    frames = []  # (PIL image, duration ms)

    async def shot(page, ms=150, repeat=1):
        img = Image.open(io.BytesIO(await page.screenshot())).convert("RGB")
        img = img.resize((int(SIZE[0] * SCALE), int(SIZE[1] * SCALE)), Image.LANCZOS)
        for _ in range(repeat):
            frames.append((img, ms))

    async def record(page, seconds, ms=150):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await shot(page, ms)
            await asyncio.sleep(0.05)

    async def wait_done(page, dry):
        # Match the dry flag so a previous, already finished job is not mistaken for this one.
        while True:
            await shot(page, 150)
            if await page.evaluate(f"ST.job && ST.job.dry === {str(dry).lower()} && ST.job.status !== 'running'"):
                return
            await asyncio.sleep(0.05)

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": SIZE[0], "height": SIZE[1]}, locale="en-US",
                                        color_scheme="light")
        page = await ctx.new_page()
        await page.goto(f"http://127.0.0.1:{PORT}/")
        await page.wait_for_selector(".chat-item")
        await shot(page, 1200)

        await page.click(".chat-item[data-id='1']")
        await shot(page, 1000)

        await page.click("#btn-count")                       # dry run
        await wait_done(page, dry=True)
        await shot(page, 1800)

        await page.click("#btn-start")                       # delete → confirmation
        await page.wait_for_selector("#popup-ok", state="visible")
        await asyncio.sleep(0.3)
        await shot(page, 2000)

        await page.click("#popup-ok")
        await wait_done(page, dry=False)
        await shot(page, 1500)

        await page.click("#bot-fab")                         # Telegram bot panel
        await asyncio.sleep(0.5)
        await shot(page, 1500)
        await page.type("#bot-token", "123456789:AAdemo-token-not-a-real-bot-token-xyz", delay=25)
        await shot(page, 1000)
        await page.click("#bot-toggle")
        await page.wait_for_selector("#bot-key-box", state="visible")
        await asyncio.sleep(0.4)
        await shot(page, 3000)
        await browser.close()
    await runner.cleanup()

    # One shared palette keeps the file small and the colours stable between frames.
    imgs = [f for f, _ in frames]
    durations = [d for _, d in frames]
    pal = imgs[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    q = [im.quantize(palette=pal, dither=Image.Dither.NONE) for im in imgs]
    q[0].save(OUT, save_all=True, append_images=q[1:], duration=durations, loop=0, optimize=True, disposal=1)
    print(f"{OUT}  {OUT.stat().st_size / 1024:.0f} KB, {len(q)} frames")


if __name__ == "__main__":
    asyncio.run(main())
