import os
import time
import asyncio
import threading
import secrets

import aiohttp
from flask import Flask
from yt_dlp import YoutubeDL
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ========== ENV ==========
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

# OPTIONAL: Force-sub
FORCE_CH = os.environ.get("FORCE_CH")  # e.g. "serenaunzipbot"
FORCE_LINK = os.environ.get("FORCE_LINK")  # e.g. "https://t.me/serenaunzipbot"

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ===== OPTIONAL YT COOKIES (as raw text) + USER-AGENT =====

YT_COOKIES_STR = os.environ.get("YT_COOKIES").strip()

def build_cookie_header(raw: str) -> str | None:
    """
    Tum jo lines doge (space-separated):
    .youtube.com TRUE / TRUE 1765358320 GPS 1
    se ye 'GPS=1; __Secure-3PAPISID=...' jaisa Cookie header bana dega.
    """
    pairs = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 7:
            continue
        name = parts[5]
        value = " ".join(parts[6:])
        pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else None

YT_COOKIE_HEADER = build_cookie_header(YT_COOKIES_STR) if YT_COOKIES_STR else None

YT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ========== BOT + FLASK ==========
bot = Client(
    "yt-quality-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=enums.ParseMode.MARKDOWN,
)

app = Flask(__name__)


@app.route("/")
def home():
    return "YouTube Quality Bot is running"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# YT jobs: job_id -> info
YT_JOBS = {}


# ========== HELPERS ==========

def is_youtube_link(text: str) -> bool:
    u = text.lower()
    return (
        "youtube.com/watch" in u
        or "youtu.be/" in u
        or "youtube.com/shorts" in u
    )


def sizeof_fmt(num: int) -> str:
    if num <= 0:
        return "0 MB"
    return f"{num / (1024 * 1024):.2f} MB"


def time_fmt(sec: float) -> str:
    sec = int(sec)
    if sec <= 0:
        return "0s"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h, {m}m"
    if m > 0:
        return f"{m}m, {s}s"
    return f"{s}s"


def progress_text(title: str, current: int, total: int | None,
                  start_time: float, stage: str) -> str:
    now = time.time()
    elapsed = max(1e-3, now - start_time)
    speed = current / (1024 * 1024 * elapsed)

    if total and total > 0:
        pct = current * 100 / total
        bar_len = 20
        filled = int(bar_len * pct / 100)
        bar = "●" * filled + "○" * (bar_len - filled)
        done_str = f"{sizeof_fmt(current)} of  {sizeof_fmt(total)}"
        remain = max(0, total - current)
        eta = remain / max(1, current) * elapsed
        eta_str = time_fmt(eta)
    else:
        pct = 0
        bar = "●○" * 10
        done_str = f"{sizeof_fmt(current)} of  ?"
        eta_str = "calculating..."

    return (
        "➵⋆🪐ᴛᴇᴄʜɴɪᴄᴀʟ_sᴇʀᴇɴᴀ𓂃\n\n"
        f"{title}\n"
        f"{stage}\n"
        f" [{bar}] \n"
        f"◌Progress😉:〘 {pct:.2f}% 〙\n"
        f"Done: 〘{done_str}〙\n"
        f"◌Speed🚀:〘 {speed:.2f} MB/s 〙\n"
        f"◌Time Left⏳:〘 {eta_str} 〙"
    )


async def ensure_subscribed(client: Client, m):
    if not FORCE_CH:
        return True
    if m.chat.type != enums.ChatType.PRIVATE:
        return True
    try:
        member = await client.get_chat_member(FORCE_CH, m.from_user.id)
        if member.status in (
            enums.ChatMemberStatus.LEFT,
            enums.ChatMemberStatus.BANNED,
        ):
            raise Exception("not joined")
        return True
    except Exception:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(
                "📢 Join Channel",
                url=FORCE_LINK or f"https://t.me/{FORCE_CH}"
            )]]
        )
        await m.reply_text(
            "⚠️ Bot use karne se pehle hamare channel ko join karein.",
            reply_markup=kb,
        )
        return False


async def extract_yt_info(url: str):
    loop = asyncio.get_running_loop()

    def _extract():
        headers = {
            "User-Agent": YT_USER_AGENT,
        }
        if YT_COOKIE_HEADER:
            headers["Cookie"] = YT_COOKIE_HEADER

        ydl_opts = {
            "quiet": True,
            "skip_download": True,
            "noplaylist": True,
            "geo_bypass": True,
            "http_headers": headers,
        }

        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await loop.run_in_executor(None, _extract)
    except Exception as e:
        msg = str(e)
        if ("Sign in to confirm you’re not a bot" in msg) or \
           ("Sign in to confirm you're not a bot" in msg):
            raise Exception(
                "Ye YouTube video login/cookies ke bina download nahi ho sakta.\n"
                "YouTube ne 'not a bot' protection laga diya hai.\n"
                "Normal public video try karo, ya owner YT_COOKIES env set kare."
            )
        raise Exception(msg)


def pick_quality_formats(info: dict):
    formats = info.get("formats") or []
    best_for = {}  # "360" -> format dict with _score

    for f in formats:
        if f.get("vcodec") == "none":
            continue  # audio-only
        url_f = f.get("url")
        if not url_f:
            continue

        h = f.get("height") or 0
        try:
            h = int(h)
        except Exception:
            continue
        if h <= 0:
            continue

        q = None
        if 240 <= h < 420:
            q = "360"
        elif 420 <= h < 560:
            q = "480"
        elif 560 <= h < 800:
            q = "720"
        else:
            continue

        score = 0
        if f.get("ext") == "mp4":
            score += 10
        if f.get("acodec") != "none":
            score += 20
        score += h / 1000

        cur = best_for.get(q)
        if (not cur) or (score > cur["_score"]):
            f2 = dict(f)
            f2["_score"] = score
            best_for[q] = f2

    return best_for  # e.g. {"360": f1, "480": f2, ...}


async def download_direct(url, dest, status_msg, title, headers=None):
    start = time.time()
    session_headers = headers.copy() if headers else {}
    session_headers.setdefault("User-Agent", YT_USER_AGENT)
    if YT_COOKIE_HEADER and "Cookie" not in session_headers:
        session_headers["Cookie"] = YT_COOKIE_HEADER

    async with aiohttp.ClientSession(headers=session_headers) as sess:
        async with sess.get(url) as resp:
            if resp.status == 403:
                raise Exception(
                    "Direct link HTTP 403 (forbidden) – YouTube/CDN ne access block kiya.\n"
                    "Ye ho sakta hai:\n"
                    "• Anti-bot / login required video\n"
                    "• Ya IP/geo ke wajah se restricted\n"
                )
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            last = 0
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if total and now - last > 2:
                        txt = progress_text(title, done, total, start, "to my server")
                        try:
                            await status_msg.edit_text(txt)
                        except Exception:
                            pass
                        last = now
    if total:
        txt = progress_text(title, total, total, start, "to my server")
        try:
            await status_msg.edit_text(txt)
        except Exception:
            pass
    return dest


# ========== COMMANDS ==========

@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client, m):
    if not await ensure_subscribed(client, m):
        return
    await m.reply_text(
        "🌸 **YouTube Quality Downloader**\n\n"
        "Mujhe koi bhi YouTube link bhejo (video ya Shorts), "
        "main tumse quality poochungi:\n"
        "360p, 480p, 720p – jo chaho select karo 💫\n\n"
        "Note:\n"
        "• Sirf normal public videos kaam karenge.\n"
        "• Age/geo restricted / login required videos par error aa sakta hai "
        "(ye YouTube ka restriction hai).\n"
        "• Owner agar `YT_COOKIES` env me cookies de, to kuch restricted videos bhi chal sakte hain."
    )


@bot.on_message(filters.command("help") & filters.private)
async def help_cmd(client, m):
    if not await ensure_subscribed(client, m):
        return
    await m.reply_text(
        "🧿 **How to use**\n\n"
        "1. Bas YouTube ka koi link bhejo, jaise:\n"
        "`https://youtu.be/abc123`\n"
        "ya\n"
        "`https://www.youtube.com/watch?v=abc123`\n"
        "ya Shorts: `https://www.youtube.com/shorts/xyz`\n"
        "2. Bot tumhe video ka title + thumbnail dikhayegi.\n"
        "3. Phir 360p / 480p / 720p buttons se quality select karo.\n"
        "4. Selected quality me video Telegram pe mil jayega.\n\n"
        "⚠️ Age‑restricted / country‑restricted / 'you’re not a bot' "
        "protected videos ko bina cookies ke hum download nahi kar sakte.\n"
        "Ye YouTube ki side ki limit hai."
    )


@bot.on_message(filters.private & filters.text & ~filters.command(["start", "help"]))
async def yt_handler(client, m):
    if not await ensure_subscribed(client, m):
        return

    text = m.text.strip()
    if not is_youtube_link(text):
        return await m.reply_text(
            "❌ Yeh YouTube link nahi lag raha.\n"
            "Example: `https://youtu.be/abc123`"
        )

    await m.reply_chat_action(enums.ChatAction.TYPING)

    try:
        info = await extract_yt_info(text)
    except Exception as e:
        return await m.reply_text(f"❌ YouTube se info nahi mil paayi:\n`{e}`")

    title = info.get("title") or "YouTube Video"
    thumb = info.get("thumbnail")
    formats = pick_quality_formats(info)

    if not formats:
        return await m.reply_text(
            "❌ Is video ke liye 360p/480p/720p jaisa koi usable format nahi mila.\n"
            "Koi aur video try karo."
        )

    job_id = secrets.token_urlsafe(8)
    YT_JOBS[job_id] = {
        "user_id": m.from_user.id,
        "url": text,
        "title": title,
        "thumb": thumb,
        "formats": formats,
        "time": int(time.time()),
    }

    buttons = []
    row = []
    for q in ["360", "480", "720"]:
        if q in formats:
            row.append(InlineKeyboardButton(f"{q}p", callback_data=f"ytq|{job_id}|{q}"))
    if row:
        buttons.append(row)
    buttons.append(
        [InlineKeyboardButton("❌ Cancel", callback_data=f"ytq_cancel|{job_id}")]
    )
    kb = InlineKeyboardMarkup(buttons)

    caption = f"📺 **{title}**\n\nQuality select karo:"
    if thumb:
        await m.reply_photo(thumb, caption=caption, reply_markup=kb)
    else:
        await m.reply_text(caption, reply_markup=kb)

@bot.on_callback_query()
async def cb_handler(client, cq):
    data = cq.data

    if data.startswith("ytq_cancel|"):
        _, job_id = data.split("|", 1)
        YT_JOBS.pop(job_id, None)
        await cq.answer("Cancelled.", show_alert=False)
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    if data.startswith("ytq|"):
        try:
            _, job_id, q = data.split("|", 2)
        except ValueError:
            return await cq.answer("Invalid data.", show_alert=False)

        job = YT_JOBS.get(job_id)
        if not job:
            return await cq.answer("Session expired. Naya link bhejo.", show_alert=True)

        if cq.from_user.id != job["user_id"]:
            return await cq.answer("Yeh tumhara session nahi hai.", show_alert=True)

        fmt = job["formats"].get(q)
        if not fmt:
            return await cq.answer("Is quality ka format nahi mila.", show_alert=True)

        await cq.answer(f"{q}p selected, downloading…", show_alert=False)

        url = fmt["url"]
        headers = fmt.get("http_headers") or {}
        ext = fmt.get("ext") or "mp4"
        safe_title = "".join(c for c in job["title"] if c not in r'\/:*?\"<>|')
        file_name = f"{safe_title}_{q}p.{ext}"
        dest = os.path.join(DOWNLOAD_DIR, file_name)
        full_title = f"{job['title']} [{q}p]"

        status = await cq.message.reply_text("⬇️ Download shuru ho raha hai…")

        path = None
        try:
            path = await download_direct(url, dest, status, full_title, headers=headers)
            await status.edit_text("📤 Telegram pe upload ho raha hai…")

            start = time.time()

            async def up_progress(current, total):
                txt = progress_text(full_title, current, total, start, "to Telegram")
                try:
                    await status.edit_text(txt)
                except Exception:
                    pass

            await cq.message.reply_video(
                path,
                caption=full_title,
                progress=up_progress,
            )

            try:
                await status.delete()
            except Exception:
                pass

        except Exception as e:
            try:
                await status.edit_text(f"❌ Error: `{e}`")
            except Exception:
                pass
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
            YT_JOBS.pop(job_id, None)


if __name__ == "__main__":
    threading.Thread(target=run_flask).start()
    bot.run()
