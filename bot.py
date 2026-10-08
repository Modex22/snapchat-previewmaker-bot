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

from telegram import Update
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
PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")


# ============================================================
# FFMPEG
# ============================================================

def find_ffmpeg():
    path = shutil.which("ffmpeg")

    if path:
        return path

    possible_paths = [
        r"C:\Users\USER\AppData\Local\Microsoft\WinGet\Packages"
    ]

    for base in possible_paths:
        base_path = Path(base)

        if base_path.exists():
            matches = list(base_path.rglob("ffmpeg.exe"))

            if matches:
                return str(matches[0])

    return None


def find_ffprobe():
    path = shutil.which("ffprobe")

    if path:
        return path

    possible_paths = [
        r"C:\Users\USER\AppData\Local\Microsoft\WinGet\Packages"
    ]

    for base in possible_paths:
        base_path = Path(base)

        if base_path.exists():
            matches = list(base_path.rglob("ffprobe.exe"))

            if matches:
                return str(matches[0])

    return None


FFMPEG = find_ffmpeg()
FFPROBE = find_ffprobe()

if not FFMPEG:
    print("WARNING: FFmpeg was not found.")

if not FFPROBE:
    print("WARNING: FFprobe was not found.")


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

# Only ONE FFmpeg conversion at a time.
# This is important for Render Free.
queue_worker_task = None

# Prevent duplicate Telegram webhook deliveries.
seen_updates = OrderedDict()

MAX_SEEN_UPDATES = 5000


@dataclass
class VideoJob:
    chat_id: int
    message_id: int
    file_id: str
    status_message_id: int


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def claim_update(update_id: int) -> bool:
    """
    Returns True if this update is new.
    Returns False if we've already handled it.
    """

    if update_id in seen_updates:
        return False

    seen_updates[update_id] = time.time()

    # Keep memory under control.
    while len(seen_updates) > MAX_SEEN_UPDATES:
        seen_updates.popitem(last=False)

    return True


# ============================================================
# SAFE TELEGRAM HELPERS
# ============================================================

async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception as e:
        print(f"Could not edit message: {e}")


async def safe_delete(message):
    try:
        await message.delete()
    except Exception as e:
        print(f"Could not delete message: {e}")


# ============================================================
# PROGRESS BAR
# ============================================================

def progress_bar(percent: int, width: int = 10):
    percent = max(0, min(100, percent))

    filled = int(width * percent / 100)
    empty = width - filled

    return "█" * filled + "░" * empty


# ============================================================
# START COMMAND
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🎬 Snapchat Preview Maker\n\n"
        "Send me a video and I'll convert it to:\n\n"
        "• 720 × 1280\n"
        "• 9:16\n"
        "• Maximum 8 seconds\n"
        "• MP4 / H.264\n"
        "• Under 32 MB\n\n"
        "You can send multiple videos.\n"
        "They'll be processed one at a time."
    )


# ============================================================
# QUEUE COMMAND
# ============================================================

async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    size = video_queue.qsize()

    if size == 0:
        await update.message.reply_text(
            "📭 The processing queue is empty."
        )
    else:
        await update.message.reply_text(
            f"📋 Videos waiting in queue: {size}"
        )


# ============================================================
# VIDEO HANDLER
# ============================================================

async def handle_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    # --------------------------------------------------------
    # DUPLICATE UPDATE PROTECTION
    # --------------------------------------------------------

    if not update.update_id:
        return

    if not claim_update(update.update_id):
        print(
            f"Ignored duplicate Telegram update: "
            f"{update.update_id}"
        )
        return

    message = update.message

    if not message:
        return

    video = message.video

    if not video:
        return

    # --------------------------------------------------------
    # QUEUE POSITION
    # --------------------------------------------------------

    position = video_queue.qsize() + 1

    status = await message.reply_text(
        "📥 Added to processing queue\n\n"
        f"📋 Position: {position}\n"
        "⏳ Waiting..."
    )

    # --------------------------------------------------------
    # CREATE JOB
    # --------------------------------------------------------

    job = VideoJob(
        chat_id=message.chat_id,
        message_id=message.message_id,
        file_id=video.file_id,
        status_message_id=status.message_id,
    )

    # --------------------------------------------------------
    # ADD TO QUEUE
    # --------------------------------------------------------

    await video_queue.put(job)

    print(
        f"Video queued | "
        f"chat={job.chat_id} | "
        f"message={job.message_id} | "
        f"queue={video_queue.qsize()}"
    )


# ============================================================
# GET VIDEO DURATION
# ============================================================

async def get_duration(file_path):

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

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        return None

    try:
        return float(stdout.decode().strip())
    except Exception:
        return None


# ============================================================
# PROCESS VIDEO
# ============================================================

async def process_video_job(job: VideoJob):

    bot = application.bot

    status = None

    temp_dir = tempfile.mkdtemp(prefix="snapchat_video_")

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
        # GET STATUS MESSAGE
        # ----------------------------------------------------

        try:
            status = await bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                text=(
                    "📥 Downloading video...\n\n"
                    "0%"
                ),
            )
        except Exception:
            status = None

        # ----------------------------------------------------
        # DOWNLOAD
        # ----------------------------------------------------

        telegram_file = await bot.get_file(job.file_id)

        await telegram_file.download_to_drive(
            custom_path=input_file
        )

        if status:

            await safe_edit(
                status,
                "📥 Download complete\n\n"
                "Preparing conversion...\n\n"
                "0%"
            )

        # ----------------------------------------------------
        # CHECK DURATION
        # ----------------------------------------------------

        duration = await get_duration(input_file)

        if duration:
            print(
                f"Input duration: {duration:.2f}s"
            )

        # ----------------------------------------------------
        # FFMPEG
        # ----------------------------------------------------

        if not FFMPEG:
            raise RuntimeError(
                "FFmpeg is not installed."
            )

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

            # Keep output comfortably under 32MB
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

            # Better playback
            "-movflags",
            "+faststart",

            # Progress information
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
        # READ FFMPEG PROGRESS
        # ----------------------------------------------------

        last_percent = -1

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            line = line.decode(
                errors="ignore"
            ).strip()

            if line.startswith("out_time_ms="):

                try:
                    out_time_ms = int(
                        line.split("=", 1)[1]
                    )

                    out_seconds = (
                        out_time_ms / 1_000_000
                    )

                    if duration:
                        percent = int(
                            min(
                                99,
                                (
                                    out_seconds
                                    / min(duration, 8)
                                )
                                * 100,
                            )
                        )
                    else:
                        percent = 0

                    # Only update when percentage changes
                    # enough to matter.
                    if (
                        percent != last_percent
                        and (
                            percent == 0
                            or percent == 100
                            or percent % 5 == 0
                        )
                    ):

                        last_percent = percent

                        if status:

                            await safe_edit(
                                status,
                                (
                                    "🎬 Converting...\n\n"
                                    f"{progress_bar(percent)} "
                                    f"{percent}%"
                                ),
                            )

                except Exception:
                    pass

        stderr = await process.stderr.read()

        return_code = await process.wait()

        if return_code != 0:

            print(
                "FFmpeg error:",
                stderr.decode(
                    errors="ignore"
                )
            )

            raise RuntimeError(
                "FFmpeg conversion failed."
            )

        # ----------------------------------------------------
        # ENSURE OUTPUT EXISTS
        # ----------------------------------------------------

        if not os.path.exists(output_file):

            raise RuntimeError(
                "FFmpeg did not create an output file."
            )

        # ----------------------------------------------------
        # FILE SIZE
        # ----------------------------------------------------

        file_size = os.path.getsize(
            output_file
        )

        size_mb = file_size / (
            1024 * 1024
        )

        print(
            f"Output size: {size_mb:.2f} MB"
        )

        if file_size > 32 * 1024 * 1024:

            raise RuntimeError(
                "Output file is larger than 32 MB."
            )

        # ----------------------------------------------------
        # UPLOAD
        # ----------------------------------------------------

        if status:

            await safe_edit(
                status,
                (
                    "✅ Conversion complete\n\n"
                    "📤 Uploading...\n\n"
                    "100%"
                ),
            )

        print(
            f"Uploading video for chat {job.chat_id}"
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
                    8,
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
            f"Video successfully sent for "
            f"message {job.message_id}"
        )

        # ----------------------------------------------------
        # DELETE STATUS MESSAGE
        # ----------------------------------------------------

        if status:

            try:
                await bot.delete_message(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                )
            except Exception:
                pass

    except Exception as e:

        print(
            f"Job failed: {repr(e)}"
        )

        # ----------------------------------------------------
        # IMPORTANT:
        # If Telegram already received the video,
        # DON'T send "Conversion failed".
        # ----------------------------------------------------

        if not video_sent:

            try:

                await bot.edit_message_text(
                    chat_id=job.chat_id,
                    message_id=job.status_message_id,
                    text=(
                        "❌ Conversion failed\n\n"
                        f"{str(e)}"
                    ),
                )

            except Exception as edit_error:

                print(
                    "Could not show error:",
                    edit_error
                )

    finally:

        # ----------------------------------------------------
        # CLEANUP
        # ----------------------------------------------------

        try:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )
        except Exception:
            pass


# ============================================================
# QUEUE WORKER
# ============================================================

async def queue_worker():

    print("Queue worker started.")

    while True:

        job = await video_queue.get()

        try:

            print(
                f"Processing queued video | "
                f"chat={job.chat_id} | "
                f"message={job.message_id}"
            )

            await process_video_job(job)

        except asyncio.CancelledError:

            raise

        except Exception as e:

            print(
                f"Unexpected queue error: {repr(e)}"
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

async def telegram_webhook(request):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            application.bot
        )

        # Process in background so Telegram gets
        # an immediate HTTP 200 response.
        asyncio.create_task(
            application.process_update(update)
        )

        return web.Response(
            text="OK"
        )

    except Exception as e:

        print(
            f"Webhook error: {repr(e)}"
        )

        # Still return 200 so Telegram doesn't
        # repeatedly resend a malformed update.
        return web.Response(
            text="OK"
        )


# ============================================================
# RENDER SERVER
# ============================================================

async def run_render():

    global queue_worker_task

    print("Starting Render server...")

    # Initialize Telegram application
    await application.initialize()

    await application.start()

    # Start ONE queue worker
    queue_worker_task = asyncio.create_task(
        queue_worker()
    )

    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

    webhook_url = (
        f"{RENDER_EXTERNAL_URL}/telegram"
    )

    print(
        f"Setting Telegram webhook: "
        f"{webhook_url}"
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    # --------------------------------------------------------
    # HTTP SERVER
    # --------------------------------------------------------

    app = web.Application()

    app.router.add_get(
        "/healthz",
        health
    )

    app.router.add_post(
        "/telegram",
        telegram_webhook
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(
        f"Server running on port {PORT}"
    )

    # Keep process alive
    try:

        while True:

            await asyncio.sleep(3600)

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
# LOCAL POLLING
# ============================================================

async def run_local():

    global queue_worker_task

    print(
        "Running bot locally with polling..."
    )

    await application.initialize()

    await application.start()

    # Remove webhook when testing locally.
    await application.bot.delete_webhook(
        drop_pending_updates=False
    )

    queue_worker_task = asyncio.create_task(
        queue_worker()
    )

    await application.updater.start_polling()

    print("Bot is running.")

    try:

        while True:

            await asyncio.sleep(3600)

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

    if RENDER_EXTERNAL_URL:

        asyncio.run(
            run_render()
        )

    else:

        asyncio.run(
            run_local()
        )