import os
import asyncio
import tempfile
import shutil
import time
from dataclasses import dataclass
from collections import OrderedDict
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

# Railway public domain
PUBLIC_URL = "https://snapchat-previewmaker-bot-production.up.railway.app"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")


# ============================================================
# FFMPEG
# ============================================================

def find_ffmpeg():
    path = shutil.which("ffmpeg")

    if path:
        return path

    base = Path(
        r"C:\Users\USER\AppData\Local\Microsoft\WinGet\Packages"
    )

    if base.exists():
        matches = list(base.rglob("ffmpeg.exe"))

        if matches:
            return str(matches[0])

    return None


def find_ffprobe():
    path = shutil.which("ffprobe")

    if path:
        return path

    base = Path(
        r"C:\Users\USER\AppData\Local\Microsoft\WinGet\Packages"
    )

    if base.exists():
        matches = list(base.rglob("ffprobe.exe"))

        if matches:
            return str(matches[0])

    return None


FFMPEG = find_ffmpeg()
FFPROBE = find_ffprobe()

print(f"FFmpeg: {FFMPEG}")
print(f"FFprobe: {FFPROBE}")


# ============================================================
# TELEGRAM APPLICATION
# ============================================================

request = HTTPXRequest(
    connect_timeout=30,
    read_timeout=180,
    write_timeout=180,
    pool_timeout=30,
)

application = (
    Application.builder()
    .token(BOT_TOKEN)
    .request(request)
    .build()
)


# ============================================================
# VIDEO QUEUE
# ============================================================

video_queue = asyncio.Queue()
queue_worker_task = None


@dataclass
class VideoJob:
    chat_id: int
    message_id: int
    file_id: str
    status_message_id: int


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

seen_updates = OrderedDict()
MAX_SEEN_UPDATES = 5000


def claim_update(update_id: int) -> bool:

    if update_id in seen_updates:
        return False

    seen_updates[update_id] = time.time()

    while len(seen_updates) > MAX_SEEN_UPDATES:
        seen_updates.popitem(last=False)

    return True


# ============================================================
# TELEGRAM COMMAND MENU
# ============================================================

async def setup_commands():

    await application.bot.set_my_commands(
        [
            BotCommand(
                "start",
                "Bot main menu"
            ),
            BotCommand(
                "help",
                "How to use the bot"
            ),
            BotCommand(
                "queue",
                "Check processing queue"
            ),
        ]
    )


# ============================================================
# PROGRESS BAR
# ============================================================

def progress_bar(
    percent: int,
    width: int = 10
):

    percent = max(
        0,
        min(100, percent)
    )

    filled = int(
        width * percent / 100
    )

    empty = width - filled

    return (
        "█" * filled
        + "░" * empty
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🎬 Snapchat Preview Maker\n\n"
        "Send me a video and I'll convert it to:\n\n"
        "📐 720 × 1280\n"
        "📱 9:16\n"
        "⏱ Maximum 8 seconds\n"
        "🎞 MP4 / H.264\n"
        "📦 Under 32 MB\n\n"
        "You can send multiple videos.\n"
        "They'll be processed one at a time."
    )


# ============================================================
# HELP
# ============================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "ℹ️ How it works\n\n"
        "1. Send a video.\n"
        "2. It is added to the processing queue.\n"
        "3. Videos are processed one at a time.\n"
        "4. Your converted video is sent back automatically.\n\n"
        "Output:\n"
        "• 720 × 1280\n"
        "• 9:16\n"
        "• Maximum 8 seconds\n"
        "• MP4 / H.264\n"
        "• Under 32 MB"
    )


# ============================================================
# QUEUE STATUS
# ============================================================

async def queue_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    size = video_queue.qsize()

    if size == 0:

        text = (
            "📋 Queue\n\n"
            "✅ No videos are waiting."
        )

    else:

        text = (
            "📋 Queue\n\n"
            f"⏳ {size} video(s) waiting.\n\n"
            "Videos are processed one at a time."
        )

    await update.message.reply_text(
        text
    )


# ============================================================
# VIDEO HANDLER
# ============================================================

async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.update_id is None:
        return

    if not claim_update(
        update.update_id
    ):
        print(
            f"Ignored duplicate update "
            f"{update.update_id}"
        )
        return

    message = update.message

    if not message:
        return

    if not message.video:
        return

    video = message.video

    # Current queue size + current video
    position = video_queue.qsize() + 1

    status = await message.reply_text(
        "📥 Added to queue\n\n"
        f"📋 Position: {position}\n"
        "⏳ Waiting..."
    )

    job = VideoJob(
        chat_id=message.chat_id,
        message_id=message.message_id,
        file_id=video.file_id,
        status_message_id=status.message_id,
    )

    await video_queue.put(job)

    print(
        f"Video queued | "
        f"message={job.message_id} | "
        f"queue={video_queue.qsize()}"
    )


# ============================================================
# GET VIDEO DURATION
# ============================================================

async def get_duration(
    file_path
):

    if not FFPROBE:
        return None

    process = await asyncio.create_subprocess_exec(
        FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, _ = await process.communicate()

    if process.returncode != 0:
        return None

    try:

        return float(
            stdout.decode().strip()
        )

    except Exception:

        return None


# ============================================================
# PROCESS VIDEO
# ============================================================

async def process_video_job(
    job: VideoJob
):

    bot = application.bot

    temp_dir = tempfile.mkdtemp(
        prefix="snapchat_video_"
    )

    input_file = os.path.join(
        temp_dir,
        "input_video"
    )

    output_file = os.path.join(
        temp_dir,
        "snapchat_preview.mp4"
    )

    video_sent = False

    try:

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=(
                "📥 Downloading video...\n\n"
                "0%"
            )
        )

        telegram_file = await bot.get_file(
            job.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=input_file
        )

        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=(
                "📥 Download complete\n\n"
                "🎬 Preparing conversion...\n\n"
                "0%"
            )
        )

        # ----------------------------------------------------
        # DURATION
        # ----------------------------------------------------

        duration = await get_duration(
            input_file
        )

        if duration:
            print(
                f"Input duration: "
                f"{duration:.2f}s"
            )

        # ----------------------------------------------------
        # FFMPEG CHECK
        # ----------------------------------------------------

        if not FFMPEG:

            raise RuntimeError(
                "FFmpeg is not installed."
            )

        # ----------------------------------------------------
        # FFMPEG COMMAND
        # ----------------------------------------------------

        command = [
            FFMPEG,

            "-y",

            "-i",
            input_file,

            # Maximum 8 seconds
            "-t",
            "8",

            # 720x1280 / 9:16
            "-vf",
            (
                "scale=720:1280:"
                "force_original_aspect_ratio=increase,"
                "crop=720:1280"
            ),

            # 30 FPS
            "-r",
            "30",

            # H.264
            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-profile:v",
            "high",

            "-pix_fmt",
            "yuv420p",

            # Video bitrate
            "-b:v",
            "2200k",

            "-maxrate",
            "2500k",

            "-bufsize",
            "5000k",

            # Audio
            "-c:a",
            "aac",

            "-b:a",
            "96k",

            "-ar",
            "48000",

            # Web playback
            "-movflags",
            "+faststart",

            # Progress
            "-progress",
            "pipe:1",

            "-nostats",

            output_file,
        ]

        print(
            "Starting FFmpeg:",
            " ".join(command)
        )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # ----------------------------------------------------
        # PROGRESS
        # ----------------------------------------------------

        last_percent = -1

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            line = line.decode(
                errors="ignore"
            ).strip()

            if line.startswith(
                "out_time_ms="
            ):

                try:

                    out_seconds = (
                        int(
                            line.split(
                                "=",
                                1
                            )[1]
                        )
                        / 1_000_000
                    )

                    if duration:

                        percent = int(
                            min(
                                99,
                                (
                                    out_seconds
                                    / min(
                                        duration,
                                        8
                                    )
                                ) * 100
                            )
                        )

                    else:

                        percent = 0

                    if (
                        percent != last_percent
                        and (
                            percent == 0
                            or percent % 5 == 0
                        )
                    ):

                        last_percent = percent

                        await bot.edit_message_text(
                            chat_id=job.chat_id,
                            message_id=job.status_message_id,
                            text=(
                                "🎬 Converting...\n\n"
                                f"{progress_bar(percent)} "
                                f"{percent}%"
                            )
                        )

                except Exception:
                    pass

        stderr = await process.stderr.read()

        return_code = await process.wait()

        if return_code != 0:

            print(
                stderr.decode(
                    errors="ignore"
                )
            )

            raise RuntimeError(
                "FFmpeg conversion failed."
            )

        # ----------------------------------------------------
        # OUTPUT CHECK
        # ----------------------------------------------------

        if not os.path.exists(
            output_file
        ):

            raise RuntimeError(
                "FFmpeg did not create "
                "the output file."
            )

        file_size = os.path.getsize(
            output_file
        )

        size_mb = (
            file_size
            / (1024 * 1024)
        )

        print(
            f"Output size: "
            f"{size_mb:.2f} MB"
        )

        if file_size > (
            32 * 1024 * 1024
        ):

            raise RuntimeError(
                "Output file is larger "
                "than 32 MB."
            )

        # ----------------------------------------------------
        # UPLOAD
        # ----------------------------------------------------

        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=(
                "✅ Conversion complete\n\n"
                "📤 Uploading...\n\n"
                "100%"
            )
        )

        with open(
            output_file,
            "rb"
        ) as video_file:

            await bot.send_video(
                chat_id=job.chat_id,
                video=video_file,
                supports_streaming=True,
                width=720,
                height=1280,
                duration=min(
                    int(duration)
                    if duration
                    else 8,
                    8
                ),
                caption=(
                    "🎬 Snapchat Preview Ready\n\n"
                    "📐 720 × 1280\n"
                    "📱 9:16\n"
                    "⏱ Maximum 8 seconds\n"
                    "🎞 MP4 / H.264"
                ),
                reply_to_message_id=job.message_id,
                read_timeout=180,
                write_timeout=180,
                connect_timeout=30,
                pool_timeout=30,
            )

        video_sent = True

        print(
            f"Video sent successfully | "
            f"message={job.message_id}"
        )

        # ----------------------------------------------------
        # REMOVE STATUS MESSAGE
        # ----------------------------------------------------

        try:

            await bot.delete_message(
                chat_id=job.chat_id,
                message_id=job.status_message_id
            )

        except Exception:
            pass

    except Exception as e:

        print(
            f"Job failed: {repr(e)}"
        )

        # Don't show an error if the video
        # was already successfully sent.

        if not video_sent:

            try:

                await bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text=(
                        "❌ Conversion failed\n\n"
                        f"{str(e)}"
                    )
                )

            except Exception:
                pass

    finally:

        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )


# ============================================================
# QUEUE WORKER
# ============================================================

async def queue_worker():

    print(
        "🎬 Video queue worker started."
    )

    while True:

        job = await video_queue.get()

        try:

            await process_video_job(
                job
            )

        except asyncio.CancelledError:

            raise

        except Exception as e:

            print(
                f"Queue worker error: "
                f"{repr(e)}"
            )

        finally:

            video_queue.task_done()

            print(
                f"Queue remaining: "
                f"{video_queue.qsize()}"
            )


# ============================================================
# HEALTH CHECK
# ============================================================

async def health(request):

    return web.Response(
        text="OK"
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

async def telegram_webhook(
    request
):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            application.bot
        )

        asyncio.create_task(
            application.process_update(
                update
            )
        )

        return web.Response(
            text="OK"
        )

    except Exception as e:

        print(
            f"Webhook error: "
            f"{repr(e)}"
        )

        return web.Response(
            text="OK"
        )


# ============================================================
# RAILWAY SERVER
# ============================================================

async def run_server():

    global queue_worker_task

    print(
        "🚀 Starting Railway server..."
    )

    await application.initialize()

    await application.start()

    # Set Telegram command menu
    await setup_commands()

    # Start ONE queue worker
    queue_worker_task = asyncio.create_task(
        queue_worker()
    )

    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

    webhook_url = (
        f"{PUBLIC_URL}/telegram"
    )

    print(
        f"Setting Telegram webhook: "
        f"{webhook_url}"
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    # --------------------------------------------------------
    # WEB SERVER
    # --------------------------------------------------------

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/healthz",
        health
    )

    app.router.add_post(
        "/telegram",
        telegram_webhook
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(
        f"🌐 Server running on port {PORT}"
    )

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    except asyncio.CancelledError:

        pass

    finally:

        if queue_worker_task:

            queue_worker_task.cancel()

            try:
                await queue_worker_task

            except asyncio.CancelledError:
                pass

        await application.stop()

        await application.shutdown()

        await runner.cleanup()


# ============================================================
# LOCAL MODE
# ============================================================

async def run_local():

    global queue_worker_task

    print(
        "💻 Running locally with polling..."
    )

    await application.initialize()

    await application.start()

    # Set Telegram command menu
    await setup_commands()

    # Remove webhook when running locally
    await application.bot.delete_webhook(
        drop_pending_updates=False
    )

    # Start queue worker
    queue_worker_task = asyncio.create_task(
        queue_worker()
    )

    await application.updater.start_polling()

    print(
        "🤖 Bot is running locally."
    )

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    except asyncio.CancelledError:

        pass

    finally:

        if queue_worker_task:

            queue_worker_task.cancel()

            try:
                await queue_worker_task

            except asyncio.CancelledError:
                pass

        await application.updater.stop()

        await application.stop()

        await application.shutdown()


# ============================================================
# HANDLERS
# ============================================================

application.add_handler(
    CommandHandler(
        "start",
        start
    )
)

application.add_handler(
    CommandHandler(
        "help",
        help_command
    )
)

application.add_handler(
    CommandHandler(
        "queue",
        queue_status
    )
)

application.add_handler(
    MessageHandler(
        filters.VIDEO,
        handle_video
    )
)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    # Railway webhook mode
    if PUBLIC_URL:

        asyncio.run(
            run_server()
        )

    # Local polling mode
    else:

        asyncio.run(
            run_local()
        )