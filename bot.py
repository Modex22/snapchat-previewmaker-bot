import os
import asyncio
import tempfile
import shutil
import time
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


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. "
        "Add it to your .env file or Render environment variables."
    )

PORT = int(os.getenv("PORT", "10000"))

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")


# =========================================================
# FFMPEG
# =========================================================

def find_ffmpeg():

    # Normal PATH
    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg:
        print(f"FFmpeg found: {ffmpeg}")
        return ffmpeg

    # Windows WinGet
    local_app_data = os.environ.get(
        "LOCALAPPDATA",
        ""
    )

    winget_path = (
        Path(local_app_data)
        / "Microsoft"
        / "WinGet"
        / "Packages"
    )

    if winget_path.exists():

        matches = list(
            winget_path.glob(
                "Gyan.FFmpeg*/**/bin/ffmpeg.exe"
            )
        )

        if matches:

            ffmpeg = str(matches[0])

            print(
                f"FFmpeg found through WinGet: {ffmpeg}"
            )

            return ffmpeg

    return None


FFMPEG = find_ffmpeg()

if not FFMPEG:
    print("WARNING: FFmpeg was not found.")
else:
    print(f"Using FFmpeg: {FFMPEG}")


# =========================================================
# FFPROBE
# =========================================================

def find_ffprobe():

    ffprobe = shutil.which("ffprobe")

    if ffprobe:
        return ffprobe

    if FFMPEG:

        possible = (
            Path(FFMPEG).parent
            / "ffprobe.exe"
        )

        if possible.exists():
            return str(possible)

    return None


FFPROBE = find_ffprobe()


# =========================================================
# TELEGRAM REQUEST
# =========================================================

request = HTTPXRequest(
    connect_timeout=30,
    read_timeout=180,
    write_timeout=180,
    pool_timeout=30,
)


# =========================================================
# APPLICATION
# =========================================================

application = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .request(request)
    .build()
)


# =========================================================
# TRACK CURRENT JOBS
#
# Prevents the same Telegram update from being processed
# twice.
# =========================================================

active_jobs = set()


# =========================================================
# SAFE TELEGRAM MESSAGE EDIT
# =========================================================

async def safe_edit(message, text):

    try:

        await message.edit_text(text)

        return True

    except Exception as error:

        print(
            "Telegram message update failed:",
            repr(error)
        )

        return False


# =========================================================
# SAFE DELETE
# =========================================================

async def safe_delete(message):

    try:

        await message.delete()

    except Exception as error:

        # This should NEVER make a successful conversion
        # look like a failed conversion.
        print(
            "Could not delete status message:",
            repr(error)
        )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    await update.message.reply_text(
        "👋 Welcome to Snapchat Preview Maker!\n\n"

        "Send me a video and I'll convert it to:\n\n"

        "📱 720 × 1280\n"
        "📐 9:16\n"
        "🎬 Maximum 8 seconds\n"
        "🎥 MP4 / H.264\n\n"

        "Just send your video."
    )


# =========================================================
# VIDEO DURATION
# =========================================================

async def get_video_duration(input_path):

    if not FFPROBE:
        return 8.0

    command = [
        FFPROBE,

        "-v",
        "error",

        "-show_entries",
        "format=duration",

        "-of",
        "default=noprint_wrappers=1:nokey=1",

        str(input_path),
    ]

    try:

        process = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        stdout, _ = await process.communicate()

        if process.returncode != 0:
            return 8.0

        duration = float(
            stdout.decode().strip()
        )

        return max(
            0.1,
            min(duration, 8.0)
        )

    except Exception:

        return 8.0


# =========================================================
# PROGRESS BAR
# =========================================================

def make_progress_bar(percent):

    percent = max(
        0,
        min(100, int(percent))
    )

    filled = int(
        percent / 10
    )

    return (
        "█" * filled
        + "░" * (10 - filled)
    )


# =========================================================
# UPDATE PROGRESS
# =========================================================

async def update_progress(
    status,
    percent,
    last_update
):

    now = time.monotonic()

    # Update at most every 1.5 seconds
    if (
        now - last_update[0] < 1.5
        and percent < 100
    ):
        return

    percent = max(
        0,
        min(100, int(percent))
    )

    bar = make_progress_bar(
        percent
    )

    text = (
        "⚙️ Converting video...\n\n"

        f"{bar} {percent}%\n\n"

        "📱 720 × 1280\n"
        "📐 9:16\n"
        "🎬 Maximum 8 seconds"
    )

    if await safe_edit(
        status,
        text
    ):

        last_update[0] = now


# =========================================================
# PROCESS VIDEO
# =========================================================

async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    if not message or not message.video:
        return

    # =====================================================
    # DUPLICATE PROTECTION
    # =====================================================

    job_id = (
        update.update_id
        if update.update_id is not None
        else message.message_id
    )

    if job_id in active_jobs:

        print(
            f"Ignoring duplicate update: {job_id}"
        )

        return

    active_jobs.add(job_id)

    # =====================================================
    # FFMPEG CHECK
    # =====================================================

    if not FFMPEG:

        await message.reply_text(
            "❌ FFmpeg is not available."
        )

        active_jobs.discard(job_id)

        return

    temp_dir = Path(
        tempfile.gettempdir()
    )

    input_path = (
        temp_dir
        / f"snap_input_{message.message_id}.mp4"
    )

    output_path = (
        temp_dir
        / f"snap_output_{message.message_id}.mp4"
    )

    status = None

    # This becomes True ONLY after the final video
    # has been successfully handed to Telegram.
    video_sent = False

    try:

        # =================================================
        # STATUS
        # =================================================

        status = await message.reply_text(
            "📥 Downloading video...\n\n"
            "0%"
        )

        # =================================================
        # DOWNLOAD
        # =================================================

        telegram_file = await context.bot.get_file(
            message.video.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=str(input_path)
        )

        print(
            "Video downloaded successfully."
        )

        # =================================================
        # DURATION
        # =================================================

        duration = await get_video_duration(
            input_path
        )

        print(
            f"Processing duration: {duration:.2f}s"
        )

        await safe_edit(
            status,
            "📥 Download complete!\n\n"
            "⚙️ Preparing conversion...\n\n"
            "0%"
        )

        # =================================================
        # FFMPEG
        # =================================================

        command = [

            FFMPEG,

            "-y",

            "-i",
            str(input_path),

            # Maximum 8 seconds
            "-t",
            "8",

            # 720 x 1280 / 9:16
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

            # Keep file comfortably under 32 MB
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

            # Web-friendly MP4
            "-movflags",
            "+faststart",

            # Machine-readable progress
            "-progress",
            "pipe:1",

            "-nostats",

            str(output_path),
        ]

        print(
            "Running FFmpeg..."
        )

        process = await asyncio.create_subprocess_exec(

            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        last_update = [0]

        # =================================================
        # READ FFMPEG PROGRESS
        # =================================================

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

                    time_ms = int(
                        line.split(
                            "=",
                            1
                        )[1]
                    )

                    current_seconds = (
                        time_ms / 1_000_000
                    )

                    percent = (
                        current_seconds
                        / duration
                    ) * 100

                    percent = min(
                        percent,
                        99
                    )

                    await update_progress(
                        status,
                        percent,
                        last_update
                    )

                except Exception as error:

                    print(
                        "Progress parsing error:",
                        repr(error)
                    )

        # =================================================
        # GET FFMPEG RESULT
        # =================================================

        stderr = await process.stderr.read()

        await process.wait()

        if process.returncode != 0:

            error_text = stderr.decode(
                errors="ignore"
            )

            print(
                "FFMPEG ERROR:"
            )

            print(
                error_text
            )

            await safe_edit(
                status,
                "❌ Conversion failed.\n\n"
                "Please try another video."
            )

            return

        # =================================================
        # VERIFY OUTPUT
        # =================================================

        if not output_path.exists():

            await safe_edit(
                status,
                "❌ Conversion failed.\n\n"
                "FFmpeg did not create the output."
            )

            return

        # =================================================
        # 100%
        # =================================================

        await safe_edit(
            status,
            "⚙️ Converting video...\n\n"
            "██████████ 100%\n\n"
            "📱 720 × 1280\n"
            "📐 9:16\n"
            "🎬 Maximum 8 seconds"
        )

        await asyncio.sleep(
            0.5
        )

        # =================================================
        # FILE SIZE
        # =================================================

        size_mb = (
            output_path.stat().st_size
            / (1024 * 1024)
        )

        print(
            f"Output size: {size_mb:.2f} MB"
        )

        if size_mb > 32:

            await safe_edit(
                status,
                f"⚠️ Conversion complete, but "
                f"the file is {size_mb:.1f} MB.\n\n"
                "That's above the 32 MB limit."
            )

            return

        # =================================================
        # UPLOAD
        # =================================================

        await safe_edit(
            status,
            "✅ Conversion complete!\n\n"
            "100%\n\n"
            f"📦 {size_mb:.1f} MB\n"
            "⬆️ Uploading..."
        )

        try:

            with open(
                output_path,
                "rb"
            ) as video_file:

                await message.reply_video(

                    video=video_file,

                    caption=(
                        "✅ Snapchat Preview Ready\n\n"

                        "🎬 8 seconds\n"
                        "📱 720 × 1280\n"
                        "📐 9:16\n"
                        "🎥 MP4 / H.264"
                    ),

                    supports_streaming=True,

                    read_timeout=180,

                    write_timeout=180,

                    connect_timeout=30,
                )

            # IMPORTANT:
            # Only mark success AFTER Telegram accepts
            # the video.
            video_sent = True

            print(
                "Video successfully sent to Telegram."
            )

        except Exception as upload_error:

            print(
                "UPLOAD ERROR:",
                repr(upload_error)
            )

            await safe_edit(
                status,
                "⚠️ Conversion completed, but "
                "Telegram timed out while uploading.\n\n"
                f"File size: {size_mb:.1f} MB"
            )

            return

        # =================================================
        # SUCCESS
        # =================================================

        if video_sent:

            # Deleting the status message is OPTIONAL.
            # If Telegram times out here, it must NOT turn
            # the successful conversion into an error.
            await safe_delete(
                status
            )

            print(
                "JOB COMPLETED SUCCESSFULLY."
            )

            return

    # =====================================================
    # GENERAL ERROR
    # =====================================================

    except Exception as error:

        print(
            "VIDEO PROCESSING ERROR:"
        )

        print(
            repr(error)
        )

        # CRITICAL:
        # If the video was already sent successfully,
        # NEVER send "conversion failed".
        if video_sent:

            print(
                "Ignoring error because video "
                "was already successfully sent."
            )

            return

        if status:

            await safe_edit(
                status,
                "❌ Something went wrong "
                "while processing the video."
            )

    finally:

        # =================================================
        # CLEANUP
        # =================================================

        try:

            input_path.unlink(
                missing_ok=True
            )

        except Exception:
            pass

        try:

            output_path.unlink(
                missing_ok=True
            )

        except Exception:
            pass

        active_jobs.discard(
            job_id
        )


# =========================================================
# HEALTH CHECK
# =========================================================

async def health(request):

    return web.Response(
        text="Snapchat Preview Maker is running."
    )


# =========================================================
# WEBHOOK
# =========================================================

async def telegram_webhook(request):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            application.bot
        )

        # IMPORTANT:
        #
        # Do NOT wait for FFmpeg here.
        #
        # Telegram needs the webhook response quickly.
        # Processing the video in the background prevents
        # Telegram from retrying the same update.
        #
        asyncio.create_task(
            application.process_update(
                update
            )
        )

        return web.Response(
            text="OK"
        )

    except Exception as error:

        print(
            "WEBHOOK ERROR:",
            repr(error)
        )

        return web.Response(
            status=500,
            text="ERROR"
        )


# =========================================================
# RENDER
# =========================================================

async def run_render():

    print(
        "Starting Render Web Service..."
    )

    await application.initialize()

    await application.start()

    if not RENDER_EXTERNAL_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing."
        )

    webhook_url = (
        f"{RENDER_EXTERNAL_URL}/telegram"
    )

    print(
        f"Telegram webhook: {webhook_url}"
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    print(
        "Telegram webhook configured."
    )

    # =====================================================
    # HTTP SERVER
    # =====================================================

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
        f"Server running on port {PORT}"
    )

    # Keep alive
    while True:

        await asyncio.sleep(
            3600
        )


# =========================================================
# LOCAL MODE
# =========================================================

async def run_local():

    print(
        "Starting local Telegram bot..."
    )

    print(
        "Polling Telegram for messages."
    )

    await application.initialize()

    await application.start()

    await application.updater.start_polling()

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    finally:

        await application.updater.stop()

        await application.stop()

        await application.shutdown()


# =========================================================
# HANDLERS
# =========================================================

application.add_handler(
    CommandHandler(
        "start",
        start
    )
)

application.add_handler(
    MessageHandler(
        filters.VIDEO,
        process_video
    )
)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    if RENDER_EXTERNAL_URL:

        asyncio.run(
            run_render()
        )

    else:

        asyncio.run(
            run_local()
        )