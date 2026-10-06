import os, asyncio, shutil, subprocess, tempfile
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
OUT_W, OUT_H, DURATION, MAX_MB = 720, 1280, 8, 32

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Send me a video and I will convert it to 8s, 720×1280, 9:16 MP4 for Snapchat."
    )

async def video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_obj = msg.video or msg.document
    if not file_obj:
        return

    status = await msg.reply_text("📥 Downloading...")
    work = Path(tempfile.mkdtemp(prefix="snapbot_"))

    try:
        tg_file = await context.bot.get_file(file_obj.file_id)
        src = work / "input.mp4"
        await tg_file.download_to_drive(src)

        out = work / "snapchat_preview.mp4"
        await status.edit_text("⚙️ Processing 8s / 720×1280 / 9:16...")

        vf = "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280"
        cmd = [
            "ffmpeg", "-y", "-i", str(src),
            "-ss", "0", "-t", str(DURATION),
            "-vf", vf,
            "-r", "30",
            "-c:v", "libx264",
            "-preset", "medium",
            "-pix_fmt", "yuv420p",
            "-b:v", "2500k",
            "-maxrate", "3000k",
            "-bufsize", "6000k",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(out),
        ]

        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-1500:])

        size = out.stat().st_size / 1024 / 1024

        if size > MAX_MB:
            await status.edit_text(
                f"⚠️ Created, but it is {size:.1f} MB (over the {MAX_MB} MB target)."
            )
        else:
            await status.edit_text(f"✅ Done — {size:.1f} MB")

        with out.open("rb") as f:
            await msg.reply_document(
                document=f,
                filename="snapchat_preview.mp4",
                caption=f"8s • 720×1280 • 9:16 • {size:.1f} MB",
            )

    except FileNotFoundError:
        await status.edit_text(
            "❌ FFmpeg was not found. Install FFmpeg and make sure `ffmpeg -version` works in your terminal."
        )
    except Exception as e:
        await status.edit_text("❌ Error: " + str(e)[:3500])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Put it in the .env file.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video))

    print("Bot running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
