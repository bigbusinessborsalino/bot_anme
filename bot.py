import os
import glob
import asyncio
import logging
import sys
import stat
import aiohttp
import re
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
from aiohttp import web

# Load environment variables
load_dotenv()

# --- CONFIGURATION ---
def get_env_list(var_name, default=[]):
    val = os.getenv(var_name)
    if val:
        return [int(x.strip()) for x in val.split(',') if x.strip().lstrip("-").isdigit()]
    return default

def get_env_int(var_name, default=None):
    val = os.getenv(var_name)
    if val and val.strip().lstrip("-").isdigit():
        return int(val)
    return default

API_ID = get_env_int("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = get_env_int("PORT", 8000)

ADMIN_IDS = get_env_list("ADMIN_IDS")
MAIN_CHANNEL = get_env_int("MAIN_CHANNEL")
DB_CHANNEL = get_env_int("DB_CHANNEL")
STICKER_ID = "CAACAgUAAxkBAAEQJ6hpV0JDpDDOI68yH7lV879XbIWiFwACGAADQ3PJEs4sW1y9vZX3OAQ"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def is_admin(message: Message):
    if not ADMIN_IDS: return True
    if message.from_user.id not in ADMIN_IDS: return False
    return True

async def get_anime_info(anime_name):
    url = f"https://api.jikan.moe/v4/anime?q={anime_name}&limit=1"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data['data']:
                        anime = data['data'][0]
                        title = anime.get('title_english') or anime.get('title')
                        genres = ", ".join([g['name'] for g in anime.get('genres', [])])
                        status = anime.get('status', 'Unknown')
                        image_url = anime['images']['jpg']['large_image_url']
                        hashtag = "".join(x for x in title if x.isalnum())
                        caption = f"**{title}**\n\n➜ **Genres:** {genres}\n➜ **Status:** {status}\n\n#{hashtag}"
                        return caption, image_url
        except Exception as e: logger.error(f"API Error: {e}")
    return None, None

async def web_server():
    async def handle(request): return web.Response(text="Bot is running!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

@app.on_message(filters.command("start"))
async def start(client, message):
    if not await is_admin(message): return
    await message.reply_text("👋 Bot Online.")

@app.on_message(filters.command("anime"))
async def anime_download(client, message: Message):
    if not await is_admin(message): return
    if not MAIN_CHANNEL or not DB_CHANNEL:
        await message.reply_text("⚠️ Channels not set.")
        return

    command_text = message.text.split(" ", 1)
    if len(command_text) < 2: return

    args = command_text[1]
    if "-e" not in args or "-r" not in args: return

    parts = args.split("-e")
    anime_name = parts[0].strip()
    rest = parts[1].split("-r")
    episode = rest[0].strip()
    resolution_arg = rest[1].strip()

    resolutions = ["360", "720", "1080"] if resolution_arg.lower() == "all" else [resolution_arg]
    status_msg = await message.reply_text(f"🔍 Processing **{anime_name}**...")

    caption, image_url = await get_anime_info(anime_name)
    if caption and image_url:
        try:
            await app.send_photo(MAIN_CHANNEL, photo=image_url, caption=caption)
            await status_msg.edit_text(f"✅ Info Found. Starting Downloads for Ep **{episode}**...")
        except: pass
    else:
        await status_msg.edit_text(f"⚠️ Info not found, starting downloads...")

    script_path = "./animepahe-dl.sh"
    if os.path.exists(script_path): os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)

    success_count = 0
    skipped_count = 0

    for res in resolutions:
        cmd = f"./animepahe-dl.sh -d -t 1 -a '{anime_name}' -e {episode} -r {res}"
        logger.info(f"Executing: {cmd}")
        
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        while True:
            line = await process.stdout.readline()
            if not line: break
            # Keep logs clean
            pass 

        await process.wait()
        
        # --- HANDLE EXIT CODES ---
        if process.returncode == 2:
            await message.reply_text(f"⚠️ Skipped {res}p: File too large (>500MB).")
            skipped_count += 1
            continue
        elif process.returncode != 0:
            await message.reply_text(f"❌ Failed to download {res}p.")
            continue

        files = glob.glob("**/*.mp4", recursive=True)
        if not files:
            await message.reply_text(f"❌ File not found for {res}p.")
            continue
        
        latest_file = max(files, key=os.path.getctime)
        safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
        final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
        try:
            os.rename(latest_file, final_filename)
            await app.send_document(MAIN_CHANNEL, document=final_filename, caption=final_filename, force_document=True)
            # await sent.copy(DB_CHANNEL) # Optional copy
            if "1080" in res: await app.send_sticker(MAIN_CHANNEL, STICKER_ID)
            success_count += 1
            os.remove(final_filename)
        except Exception as e:
            await message.reply_text(f"⚠️ Upload Error: {e}")

        # Cleanup
        try: 
            if os.path.exists(os.path.dirname(latest_file)): os.rmdir(os.path.dirname(latest_file))
        except: pass

        if res != resolutions[-1]: await asyncio.sleep(30)

    # --- FINAL STATUS FOR CONTROLLER ---
    if success_count > 0 or skipped_count > 0:
        # If at least one worked OR we skipped files due to size, tell Controller we are "Done"
        # This prevents infinite retry loops for large files.
        await status_msg.edit_text("✅ All done! (Skipped files handled)")
    else:
        await status_msg.edit_text(f"❌ Task finished, but errors occurred.")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
