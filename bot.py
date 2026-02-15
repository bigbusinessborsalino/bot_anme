import os
import glob
import asyncio
import logging
import sys
import stat
import aiohttp
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
        # Splits "123, 456, 789" into [123, 456, 789]
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

# Admins and Channels
ADMIN_IDS = get_env_list("ADMIN_IDS") # New: List of Admin IDs
MAIN_CHANNEL = get_env_int("MAIN_CHANNEL")
DB_CHANNEL = get_env_int("DB_CHANNEL")

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- HELPER: Admin Check ---
async def is_admin(message: Message):
    if not ADMIN_IDS:
        return True # If no admins set, allow everyone (or change to False to lock it)
    if message.from_user.id not in ADMIN_IDS:
        await message.reply_text("⛔ **Access Denied.** You are not an admin.")
        return False
    return True

# --- HELPER: Jikan API ---
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
                        
                        caption = (
                            f"**{title}**\n\n"
                            f"➜ **Genres:** {genres}\n"
                            f"➜ **Status:** {status}\n\n"
                            f"#{hashtag}"
                        )
                        return caption, image_url
        except Exception as e:
            logger.error(f"API Error: {e}")
    return None, None

# --- DUMMY WEB SERVER ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is running!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

# --- COMMANDS ---

@app.on_message(filters.command("start"))
async def start(client, message):
    if not await is_admin(message): return

    status_text = "👋 **Bot is Online!**\n\n"
    
    # Check Main Channel
    try:
        chat = await client.get_chat(MAIN_CHANNEL)
        status_text += f"✅ **Main Channel:** `{chat.title}`\n"
    except Exception:
        status_text += "⚠️ **Main Channel:** Not recognized. Forward a message from it to me!\n"

    # Check DB Channel
    try:
        chat = await client.get_chat(DB_CHANNEL)
        status_text += f"✅ **DB Channel:** `{chat.title}`\n"
    except Exception:
        status_text += "⚠️ **DB Channel:** Not recognized. Forward a message from it to me!\n"

    status_text += "\n**Commands:**\n/anime <name> -e <ep> -r <res>\n/restart - Restart bot"
    await message.reply_text(status_text)

@app.on_message(filters.command("restart"))
async def restart_bot(client, message):
    if not await is_admin(message): return
    await message.reply_text("🔄 **Restarting...**")
    os.execl(sys.executable, sys.executable, *sys.argv)

# --- CHANNEL SETUP VIA FORWARD ---
@app.on_message(filters.forwarded & filters.private)
async def set_channels_via_forward(client, message: Message):
    if not await is_admin(message): return
    
    global MAIN_CHANNEL, DB_CHANNEL
    
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        title = message.forward_from_chat.title
        
        # Ask user which channel this is
        text = (
            f"📢 **Detected Channel:** {title} (`{chat_id}`)\n\n"
            "What do you want to set this as?\n"
            "Reply with **'main'** or **'db'**."
        )
        await message.reply_text(text, quote=True)
    else:
        await message.reply_text("❌ Could not detect channel ID. Make sure it is a public channel or I am admin there.")

@app.on_message(filters.reply & filters.text & filters.private)
async def confirm_channel_set(client, message: Message):
    if not await is_admin(message): return
    
    global MAIN_CHANNEL, DB_CHANNEL
    
    # Check if replying to a channel detection message
    reply = message.reply_to_message
    if not reply or "Detected Channel" not in reply.text:
        return

    # Extract ID from the previous message
    try:
        # Simple extraction assumes format: ... (`-10012345`)
        extracted_id = int(reply.text.split('(`')[1].split('`)')[0])
    except:
        await message.reply_text("❌ Error parsing ID.")
        return

    choice = message.text.lower().strip()
    
    if choice == "main":
        MAIN_CHANNEL = extracted_id
        await message.reply_text(f"✅ **Main Channel** set to `{extracted_id}` temporarily.")
    elif choice == "db":
        DB_CHANNEL = extracted_id
        await message.reply_text(f"✅ **DB Channel** set to `{extracted_id}` temporarily.")
    else:
        await message.reply_text("❌ Invalid choice. Reply 'main' or 'db'.")

@app.on_message(filters.command("anime"))
async def anime_download(client, message: Message):
    if not await is_admin(message): return
    
    if not MAIN_CHANNEL or not DB_CHANNEL:
        await message.reply_text("⚠️ **Error:** Channels not set. Forward a message from your channels to me first!")
        return

    command_text = message.text.split(" ", 1)
    if len(command_text) < 2:
        await message.reply_text("Usage: /anime <name> -e <episode> -r <resolution|all>")
        return

    args = command_text[1]
    
    try:
        if "-e" not in args or "-r" not in args:
             await message.reply_text("Error: Missing -e or -r flags.")
             return

        parts = args.split("-e")
        anime_name = parts[0].strip()
        rest = parts[1].split("-r")
        episode = rest[0].strip()
        resolution_arg = rest[1].strip()

        if resolution_arg.lower() == "all":
            resolutions = ["360", "720", "1080"]
        else:
            resolutions = [resolution_arg]

        status_msg = await message.reply_text(f"🔍 Searching info for **{anime_name}**...")

        # 1. Info Post
        caption, image_url = await get_anime_info(anime_name)
        if caption and image_url:
            try:
                await app.send_photo(MAIN_CHANNEL, photo=image_url, caption=caption)
                await app.send_photo(DB_CHANNEL, photo=image_url, caption=caption)
                await status_msg.edit_text(f"✅ Info Post Sent.\nQueueing **{anime_name}** Episode **{episode}**...")
            except Exception as e:
                await message.reply_text(f"⚠️ Error posting info to channels: {e}\n(Did you introduce the channels?)")
                return
        else:
            await status_msg.edit_text(f"⚠️ Info not found, downloading video only...")

        # 2. Self Repair
        script_path = "./animepahe-dl.sh"
        if os.path.exists(script_path):
            st = os.stat(script_path)
            os.chmod(script_path, st.st_mode | stat.S_IEXEC)

        success_count = 0

        # 3. Download Loop
        for res in resolutions:
            await status_msg.edit_text(f"Processing **{anime_name}** - Episode {episode} [{res}p]...")
            
            # Safe Mode (-t 1)
            cmd = f"./animepahe-dl.sh -d -t 1 -a '{anime_name}' -e {episode} -r {res}"
            logger.info(f"Executing: {cmd}")
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT 
            )

            while True:
                line = await process.stdout.readline()
                if not line: break
                decoded_line = line.decode('utf-8', errors='ignore').strip()
                if decoded_line:
                    print(f"[SCRIPT] {decoded_line}")

            await process.wait()
            
            if process.returncode != 0:
                await message.reply_text(f"❌ Failed to download {res}p. (Exit Code: {process.returncode})")
                continue

            files = glob.glob("**/*.mp4", recursive=True)
            if not files:
                await message.reply_text(f"❌ File not found for {res}p.")
                continue
            
            latest_file = max(files, key=os.path.getctime)
            
            # Rename
            safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
            final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
            new_file_path = os.path.join(os.path.dirname(latest_file), final_filename)
            try:
                os.rename(latest_file, new_file_path)
            except OSError:
                new_file_path = latest_file

            # Upload
            await status_msg.edit_text(f"Uploading {final_filename}...")
            try:
                sent_msg = await app.send_document(
                    chat_id=MAIN_CHANNEL,
                    document=new_file_path,
                    caption=final_filename,
                    force_document=True
                )
                await sent_msg.copy(chat_id=DB_CHANNEL)
                success_count += 1
            except Exception as e:
                await message.reply_text(f"⚠️ Upload Error: {e}")

            # Cleanup
            try:
                os.remove(new_file_path)
                parent_dir = os.path.dirname(new_file_path)
                if not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception:
                pass

            # Cool Down
            if res != resolutions[-1]:
                await status_msg.edit_text(f"❄️ Cooling down for 30s...")
                await asyncio.sleep(30)

        # 4. Finish
        if success_count == len(resolutions):
            await status_msg.edit_text("✅ All done! Download successful.")
            await message.reply_sticker("CAACAgUAAxkBAAEQJ6hpV0JDpDDOI68yH7lV879XbIWiFwACGAADQ3PJEs4sW1y9vZX3OAQ")
        else:
            await status_msg.edit_text(f"⚠️ Job finished. {success_count}/{len(resolutions)} successful.")

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Error: {str(e)}")

if __name__ == "__main__":
    print("Bot Starting...")
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
