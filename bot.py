import os
import glob
import asyncio
import logging
import sys
import stat
import io
import aiohttp
import re
import urllib.parse
from PIL import Image
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
from aiohttp import web
from pyrogram import idle

import animekai

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


# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

async def is_admin(message: Message):
    if not ADMIN_IDS: return True
    if message.from_user.id not in ADMIN_IDS: return False
    return True

def _build_caption(title, genres, status):
    hashtag = "".join(x for x in title if x.isalnum())
    return f"**{title}**\n\n➜ **Genres:** {genres}\n➜ **Status:** {status}\n\n#{hashtag}"


async def _mirror_to_db(sent_message):
    """
    Copy a message we just sent to MAIN_CHANNEL into DB_CHANNEL so the DB
    channel always has a clean mirror (without the "Forwarded from" header).
    Failures are logged but never break the main flow.
    """
    if not DB_CHANNEL or not sent_message:
        return
    try:
        # If the channels are the same we'd just be duplicating, so skip.
        if MAIN_CHANNEL == DB_CHANNEL:
            return
        await app.copy_message(
            chat_id=DB_CHANNEL,
            from_chat_id=MAIN_CHANNEL,
            message_id=sent_message.id,
        )
    except Exception as e:
        logger.warning(f"DB_CHANNEL mirror failed for msg {getattr(sent_message, 'id', '?')}: {e}")


async def _get_from_jikan(session: aiohttp.ClientSession, anime_name: str):
    """Source 1: Jikan (MyAnimeList wrapper)"""
    try:
        url = f"https://api.jikan.moe/v4/anime?q={anime_name}&limit=1"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get('data'):
                    anime = data['data'][0]
                    title = anime.get('title_english') or anime.get('title')
                    genres = ", ".join([g['name'] for g in anime.get('genres', [])])
                    status = anime.get('status', 'Unknown')
                    image_url = anime['images']['jpg']['large_image_url']
                    if title and image_url:
                        return _build_caption(title, genres, status), image_url
    except Exception as e:
        logger.warning(f"Jikan failed: {e}")
    return None, None


async def _get_from_anilist(session: aiohttp.ClientSession, anime_name: str):
    """Source 2: AniList (GraphQL)"""
    try:
        query = """
        query ($search: String) {
          Media(search: $search, type: ANIME) {
            title { english romaji }
            genres
            status
            coverImage { extraLarge }
          }
        }
        """
        async with session.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": anime_name}},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                media = (data.get("data") or {}).get("Media")
                if media:
                    title = (media.get("title") or {}).get("english") or (media.get("title") or {}).get("romaji")
                    genres = ", ".join(media.get("genres") or [])
                    raw_status = (media.get("status") or "Unknown")
                    status = raw_status.replace("_", " ").title()
                    image_url = (media.get("coverImage") or {}).get("extraLarge")
                    if title and image_url:
                        return _build_caption(title, genres, status), image_url
    except Exception as e:
        logger.warning(f"AniList failed: {e}")
    return None, None


async def _get_from_kitsu(session: aiohttp.ClientSession, anime_name: str):
    """Source 3: Kitsu API"""
    try:
        encoded = urllib.parse.quote(anime_name)
        url = f"https://kitsu.io/api/edge/anime?filter[text]={encoded}&page[limit]=1"
        async with session.get(
            url,
            headers={"Accept": "application/vnd.api+json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                items = data.get("data") or []
                if items:
                    attrs = items[0].get("attributes") or {}
                    titles = attrs.get("titles") or {}
                    title = titles.get("en") or titles.get("en_jp") or attrs.get("canonicalTitle")
                    status_raw = attrs.get("status") or "Unknown"
                    status = status_raw.replace("_", " ").title()
                    image_url = (attrs.get("posterImage") or {}).get("large")
                    if title and image_url:
                        return _build_caption(title, "", status), image_url
    except Exception as e:
        logger.warning(f"Kitsu failed: {e}")
    return None, None


_MAX_PHOTO_SIDE = 2560   # Telegram rejects photos with any side > this
_MAX_PHOTO_SUM  = 9500   # Telegram rejects when width + height exceeds ~10000

def _resize_for_telegram(raw: bytes) -> io.BytesIO:
    """
    Resize an image so it fits Telegram's photo dimension limits:
      - Neither side exceeds 2560 px
      - width + height < 9500
    Always returns a JPEG BytesIO.
    """
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size

    # Scale down if needed
    scale = 1.0
    if w > _MAX_PHOTO_SIDE or h > _MAX_PHOTO_SIDE:
        scale = min(_MAX_PHOTO_SIDE / w, _MAX_PHOTO_SIDE / h)
    if (w * scale) + (h * scale) > _MAX_PHOTO_SUM:
        scale = min(scale, _MAX_PHOTO_SUM / (w + h))

    if scale < 1.0:
        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        logger.info(f"Resized image {w}x{h} → {new_w}x{new_h} for Telegram")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


async def _download_image_bytes(session: aiohttp.ClientSession, url: str) -> io.BytesIO | None:
    """Download an image and return it resized to Telegram-safe dimensions."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status == 200:
                raw = await resp.read()
                return _resize_for_telegram(raw)
    except Exception as e:
        logger.warning(f"Image download/resize failed ({url}): {e}")
    return None




async def _get_wallhaven_image(session: aiohttp.ClientSession, anime_name: str) -> str | None:
    """
    Search Wallhaven for a 16:9 anime fanart of the anime.
    Public API — no key needed for SFW results.

    Strategy (in priority order):
      1. sorting=relevance (matches the query well — picks actual MHA art for "my hero academia"
         instead of generic "anime girls" wallpapers that just happen to be popular)
      2. fall back to landscape ratio if no 16:9 hits
      3. fall back to sorting=favorites only as a last resort
    """
    # Skip generic-looking results (compilations, waifu collages, etc.) since
    # they often outrank the actual show on raw popularity.
    _GENERIC_TAG_BLOCKLIST = {
        "waifu", "waifus", "anime girls", "anime girl", "compilation", "collage",
        "wallpaper", "wallpapers", "mix",
    }

    def _looks_generic(wp: dict) -> bool:
        # /search responses don't include full tags, but the URL slug often
        # gives away generic stuff (e.g. /wallpaper/waifu-...). Cheap check.
        path = (wp.get("url") or wp.get("path") or "").lower()
        return any(t in path for t in _GENERIC_TAG_BLOCKLIST)

    base_params = {
        "q": anime_name,                 # raw title; categories already restrict to anime
        "categories": "010",
        "purity": "100",
        "page": "1",
    }

    async def _try(extra: dict, label: str) -> str | None:
        params = {**base_params, **extra}
        try:
            async with session.get(
                "https://wallhaven.cc/api/v1/search",
                params=params,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Wallhaven {label} returned HTTP {resp.status}")
                    return None
                data = await resp.json(content_type=None)
                results = data.get("data") or []
                if not results:
                    return None
                # Prefer the first non-generic result; if all top results look
                # generic, fall back to the first one anyway.
                for wp in results[:10]:
                    if not _looks_generic(wp):
                        url = wp.get("path")
                        if url:
                            logger.info(
                                f"Wallhaven [{label}] picked '{anime_name}' "
                                f"(favs={wp.get('favorites', '?')})"
                            )
                            return url
                first = results[0].get("path")
                if first:
                    logger.info(
                        f"Wallhaven [{label}] only generic-looking results for "
                        f"'{anime_name}', using top hit anyway"
                    )
                return first
        except Exception as e:
            logger.warning(f"Wallhaven {label} failed: {e}")
        return None

    # 1. Best match: relevance sort, strict 16:9
    img = await _try({"ratios": "16x9", "sorting": "relevance", "order": "desc"}, "relevance/16x9")
    if img:
        return img

    # 2. Relevance sort, any landscape ratio
    img = await _try({"ratios": "landscape", "sorting": "relevance", "order": "desc"}, "relevance/landscape")
    if img:
        return img

    # 3. Relevance sort, ANY ratio — accepts portrait/square fanart too.
    # This ensures we always get a relevant image and never fall back to
    # the plain MAL poster (which Jikan/AniList already supplies as a fallback).
    img = await _try({"sorting": "relevance", "order": "desc"}, "relevance/any-ratio")
    if img:
        return img

    # 4. Last resort: favorites sort, any ratio
    img = await _try({"sorting": "favorites", "order": "desc"}, "favorites/any-ratio")
    if img:
        return img

    logger.info(f"Wallhaven: no image found for '{anime_name}'")
    return None


def _image_search_name(anime_name: str) -> str:
    """
    Strip season/cour/part suffixes so image searches are broader.
    e.g. "My Hero Academia Season 2" → "My Hero Academia"
         "Attack on Titan Final Season Part 2" → "Attack on Titan"
    """
    cleaned = re.sub(
        r"\s+(season|cour|part|arc|s)\s*\d+.*$",
        "",
        anime_name,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or anime_name


async def get_anime_info(anime_name: str):
    """
    Get anime text info and poster image from Jikan (MyAnimeList) → AniList → Kitsu.
    Uses the poster image directly from the info API — no third-party image search.
    Returns (caption, image_url).
    """
    async with aiohttp.ClientSession() as session:
        caption = None
        image_url = None
        for source_fn, source_name in [
            (_get_from_jikan, "Jikan"),
            (_get_from_anilist, "AniList"),
            (_get_from_kitsu, "Kitsu"),
        ]:
            caption, image_url = await source_fn(session, anime_name)
            if caption and image_url:
                logger.info(f"Anime info and image from {source_name} for '{anime_name}'")
                break
            logger.info(f"{source_name} gave no result for '{anime_name}', trying next...")

        if not caption:
            logger.error(f"All info sources failed for '{anime_name}'")
            return None, None

        return caption, image_url


async def _get_pahe_first_ep(anime_name: str) -> int | None:
    """
    Query the AnimePahe search API and return the first (lowest) episode number
    for the best-matching anime.

    AnimePahe numbers episodes *globally* across seasons: MHA Season 2 starts
    at episode 14 because Season 1 had 13 episodes.  Calling this before
    animepahe-dl.sh lets us compute the correct episode number to request.

    Returns the first episode number (int) or None if the lookup fails.
    """
    PAHE_HOST = "https://animepahe.pw"
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    headers = {"User-Agent": UA, "cookie": "__ddg2_=replitbot"}

    try:
        async with aiohttp.ClientSession(headers=headers) as s:
            # 1. Search for the anime
            q = anime_name.replace(" ", "%20")
            async with s.get(
                f"{PAHE_HOST}/api?m=search&q={q}", timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status != 200:
                    logger.warning("AnimePahe search HTTP %s for '%s'", r.status, anime_name)
                    return None
                data = await r.json(content_type=None)

            results = data.get("data") or []
            if not results:
                logger.info("AnimePahe: no search results for '%s'", anime_name)
                return None

            # Pick the best match by title similarity
            scored = sorted(
                results,
                key=lambda x: _title_score(anime_name, x.get("title", "")),
                reverse=True,
            )
            best = scored[0]
            slug = best.get("session") or best.get("slug")
            if not slug:
                return None
            logger.info(
                "AnimePahe: matched '%s' (slug=%s) for '%s'",
                best.get("title"), slug, anime_name,
            )

            # 2. Get the first page of episodes (sorted ascending)
            async with s.get(
                f"{PAHE_HOST}/api?m=release&id={slug}&sort=episode_asc&page=1",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r2:
                if r2.status != 200:
                    return None
                ep_data = await r2.json(content_type=None)

            episodes = ep_data.get("data") or []
            if not episodes:
                return None

            # The very first entry has the lowest episode number
            first_ep_num = int(float(episodes[0].get("episode", 1)))
            logger.info(
                "AnimePahe: '%s' first episode on Pahe is %d", best.get("title"), first_ep_num
            )
            return first_ep_num

    except Exception as exc:
        logger.warning("AnimePahe episode-offset lookup failed: %s", exc)
        return None


async def _download_via_animekai(
    anime_name: str, episode: str, resolution: str
) -> str | None:
    """
    Fallback downloader that uses AnimeKAI stream links + ffmpeg when
    animepahe-dl.sh cannot retrieve a file.

    Flow:
      1. Search AnimeKAI with the same title-scoring logic used for stream links
      2. Find the episode in the matched series
      3. Pick the sub stream variant closest to the requested resolution
         (falls back to best-available quality)
      4. Run ffmpeg to download the m3u8 playlist into an mp4 file
      5. Return the local file path, or None on any failure
    """
    try:
        results = await animekai.search(anime_name, limit=10, timeout=30.0)
        if not results:
            logger.info("AnimeKAI fallback: no results for '%s'", anime_name)
            return None

        scored = sorted(
            results,
            key=lambda r: _title_score(anime_name, r.title),
            reverse=True,
        )

        # Only use the single best-matching candidate — never fall through to a
        # different anime just because it happens to have the right episode count.
        best_candidate = scored[0]
        chosen = None
        ep = None
        try:
            episodes = await animekai.list_episodes(best_candidate.path, timeout=45.0)
        except Exception as e:
            logger.warning("AnimeKAI fallback: list_episodes failed for '%s': %s", best_candidate.title, e)
            episodes = []

        if episodes:
            match = next((e for e in episodes if str(e.number) == str(episode)), None)
            if match:
                chosen = best_candidate
                ep = match
            else:
                logger.info(
                    "AnimeKAI fallback: '%s' does not have ep %s yet (has %d eps) — not using a different anime",
                    best_candidate.title, episode, len(episodes),
                )

        if not chosen or not ep:
            logger.info("AnimeKAI fallback: episode %s not available for '%s'", episode, anime_name)
            return None

        # Try stream types in priority order: sub → dub → softsub
        variants: list = []
        for stype in ("sub", "dub", "softsub"):
            try:
                variants = await animekai.list_variants(chosen.path, ep.token, stype, timeout=180.0)
            except Exception:
                pass
            if variants:
                logger.info("AnimeKAI fallback: using %s stream from '%s'", stype, chosen.title)
                break

        if not variants:
            logger.info("AnimeKAI fallback: no variants found for '%s' ep %s", anime_name, episode)
            return None

        # Pick the variant closest to the requested resolution.
        # If exact match exists, use it. Otherwise pick the quality whose
        # numeric value is nearest to the target (e.g. 480 for a 360 request
        # when only 480/720/1080 are available).
        target = int("".join(ch for ch in str(resolution) if ch.isdigit()) or "0")
        chosen_variant = None
        best_diff = float("inf")
        for v in variants:
            digits = "".join(ch for ch in v.quality if ch.isdigit())
            q_num = int(digits) if digits else 0
            diff = abs(q_num - target)
            if diff < best_diff:
                best_diff = diff
                chosen_variant = v
        if chosen_variant and best_diff == 0:
            logger.info("AnimeKAI fallback: exact %sp match found", target)
        elif chosen_variant:
            logger.info(
                "AnimeKAI fallback: %sp not available, using closest: %s",
                target, chosen_variant.quality,
            )

        safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
        out_file = f"Ep_{episode}_{safe_name}_{resolution}p_kai.mp4"

        # ffmpeg download: -c copy keeps the original stream (fast, no re-encode)
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-headers", "Referer: https://anikai.to/\r\n",
            "-i", chosen_variant.playlist_url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            out_file,
        ]
        logger.info(
            "AnimeKAI fallback: ffmpeg → %s (%s)", out_file, chosen_variant.quality
        )
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=600.0)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("AnimeKAI fallback: ffmpeg timed out")
            return None

        if proc.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
            size_mb = os.path.getsize(out_file) / 1_048_576
            logger.info("AnimeKAI fallback: downloaded %.1f MB → %s", size_mb, out_file)
            return out_file
        else:
            tail = stderr_bytes.decode(errors="replace")[-600:]
            logger.warning("AnimeKAI fallback: ffmpeg rc=%d — %s", proc.returncode, tail)
            if os.path.exists(out_file):
                os.remove(out_file)
            return None

    except Exception as e:
        logger.error("AnimeKAI fallback download error: %s", e)
        return None


def _normalize_title(s: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy title comparison."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _title_score(query: str, candidate: str) -> float:
    """
    Score how well `candidate` matches `query`. Higher = better.
    Penalizes spin-off / arc / movie / OVA matches so we prefer the main series.
    """
    q = _normalize_title(query)
    c = _normalize_title(candidate)
    if not q or not c:
        return 0.0

    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if not q_tokens:
        return 0.0

    overlap = len(q_tokens & c_tokens) / len(q_tokens)
    score = overlap

    # Big bonus for exact match
    if q == c:
        score += 1.0
    # Bonus when candidate starts with the query (e.g. "demon slayer" → "demon slayer kimetsu...")
    elif c.startswith(q):
        score += 0.5

    # Penalize obvious spin-offs / side content the user almost certainly didn't ask for
    spinoff_markers = (
        "arc", "movie", "ova", "ona", "special", "specials",
        "recap", "side story", "the movie",
    )
    extra_tokens = c_tokens - q_tokens
    for marker in spinoff_markers:
        if marker in c and marker not in q:
            score -= 0.4
            break

    # Slight penalty proportional to how much extra fluff the candidate has
    score -= 0.05 * max(0, len(extra_tokens) - 2)

    return score


async def get_stream_links(anime_name: str, episode_number: str) -> str:
    """
    Searches AnimeKAI for the anime and episode, then returns a formatted
    string of stream links (Sub and Dub if available) to append to the caption.
    Returns empty string if nothing is found.

    Picks the best match by:
      1. Title similarity to the query (penalizing spin-offs / arcs / movies)
      2. Whether the episode list actually contains the requested episode
    Walks through multiple candidates if the top one doesn't have the episode,
    so we never return "random" links from the wrong series.
    """
    try:
        results = await animekai.search(anime_name, limit=10, timeout=30.0)
        if not results:
            logger.info(f"AnimeKAI: no results for '{anime_name}'")
            return ""

        # Rank candidates by title similarity
        scored = sorted(
            results,
            key=lambda r: _title_score(anime_name, r.title),
            reverse=True,
        )
        logger.info(
            "AnimeKAI ranked candidates for '%s': %s",
            anime_name,
            [(r.title, round(_title_score(anime_name, r.title), 2)) for r in scored[:5]],
        )

        # Only use the single best-matching candidate.
        # If that anime does not have the episode yet, we stop — we never
        # fall through to a different (wrong) anime just because it happens
        # to have enough episodes.
        best_candidate = scored[0]
        chosen = None
        ep = None
        try:
            episodes = await animekai.list_episodes(best_candidate.path, timeout=45.0)
        except Exception as e:
            logger.warning(
                "AnimeKAI: list_episodes failed for '%s' (%s): %s",
                best_candidate.title, best_candidate.path, e,
            )
            episodes = []

        if episodes:
            match = next(
                (e for e in episodes if str(e.number) == str(episode_number)),
                None,
            )
            if match:
                chosen = best_candidate
                ep = match
                logger.info(
                    "AnimeKAI: found ep %s in '%s' (%s)",
                    episode_number, best_candidate.title, best_candidate.path,
                )
            else:
                logger.info(
                    "AnimeKAI: '%s' does not have ep %s yet (has %d eps) — not using a different anime",
                    best_candidate.title, episode_number, len(episodes),
                )
        else:
            logger.info("AnimeKAI: '%s' has no episodes", best_candidate.title)

        if not chosen or not ep:
            logger.info(
                "AnimeKAI: episode %s not available for '%s' — skipping stream links",
                episode_number, anime_name,
            )
            return ""

        stream_types = await animekai.list_stream_types(chosen.path, ep.token, timeout=30.0)
        if not stream_types:
            logger.info("AnimeKAI: no stream types available")
            return ""

        sections = []
        for stype in stream_types:
            variants = await animekai.list_variants(
                chosen.path, ep.token, stype, timeout=180.0
            )
            if not variants:
                continue

            # Deduplicate by quality, keep first (highest ranked) server per quality
            seen: dict = {}
            for v in variants:
                if v.quality not in seen:
                    seen[v.quality] = v.playlist_url

            def _qsort_key(item):
                digits = "".join(ch for ch in item[0] if ch.isdigit())
                return int(digits) if digits else 0

            sorted_variants = sorted(seen.items(), key=_qsort_key, reverse=True)
            # Escape characters that would break Telegram's Markdown link parser.
            # If a URL contains a raw ")" or "\" the whole caption falls back to
            # plain text and the user sees the URL printed inline instead of as
            # a tappable link. Percent-encoding fixes that without changing what
            # the URL resolves to.
            def _safe_url(u: str) -> str:
                return u.replace("\\", "%5C").replace(")", "%29")
            links = "   ".join(
                f"[{q}]({_safe_url(url)})" for q, url in sorted_variants
            )

            if stype == "sub":
                label = "**Stream**"
            elif stype == "dub":
                label = "**Dub**"
            else:
                label = f"**{stype.title()}**"

            if links:
                sections.append(f"{label}\n{links}")

        return "\n\n".join(sections)

    except Exception as e:
        logger.error(f"Stream link fetch error: {e}")
        return ""


async def web_server():
    async def handle(request): return web.Response(text="Bot is running!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

# --- SAFE STARTUP CHECK (Warm Up) ---
async def check_channels():
    logger.info("🔍 Checking Channel Access...")

    # 🔹 MAIN CHANNEL CHECK
    try:
        chat = await app.get_chat(MAIN_CHANNEL)
        logger.info(f"✅ MAIN CHANNEL Access: OK ({chat.title})")
    except Exception as e:
        logger.warning(f"⚠️ Cannot access MAIN CHANNEL yet. (Error: {e})")

    # 🔹 FORCE FETCH PUBLIC DB CHANNEL USING USERNAME
    try:
        chat = await app.get_chat("REIGEN_100")
        logger.info(f"🔥 Username Fetch Success! ID = {chat.id}")
    except Exception as e:
        logger.warning(f"❌ Username fetch failed: {e}")

    # 🔹 NORMAL DB CHANNEL CHECK
    if not DB_CHANNEL:
        logger.warning("⚠️ DB_CHANNEL is not set in env.")
    else:
        try:
            chat = await app.get_chat(DB_CHANNEL)
            logger.info(f"✅ DB CHANNEL Access: OK ({chat.title})")
        except Exception as e:
            logger.warning(f"⚠️ Cannot access DB CHANNEL yet. (Error: {e})")

@app.on_message(filters.command("start"))
async def start(client, message):
    if not await is_admin(message): return
    await message.reply_text("👋 Bot Online.")



@app.on_message(filters.command("anime"))
async def anime_download(client, message: Message):
    if not await is_admin(message): return
    if not MAIN_CHANNEL or not DB_CHANNEL:
        await message.reply_text("⚠️ Critical: Channels not configured.")
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
        # Fetch stream links from AnimeKAI and append to caption
        await status_msg.edit_text(f"🔗 Fetching stream links for Ep **{episode}**...")
        stream_links = await get_stream_links(anime_name, episode)
        if stream_links:
            caption = f"{caption}\n\n{stream_links}"

        try:
            async with aiohttp.ClientSession() as dl_session:
                img_bytes = await _download_image_bytes(dl_session, image_url)
            if img_bytes:
                sent = await app.send_photo(MAIN_CHANNEL, photo=img_bytes, caption=caption)
            else:
                # Fallback: let Telegram try the URL directly
                sent = await app.send_photo(MAIN_CHANNEL, photo=image_url, caption=caption)
            await _mirror_to_db(sent)
            await status_msg.edit_text(f"✅ Info Found. Starting Downloads for Ep **{episode}**...")
        except Exception as e:
            logger.error(f"Post failed: {e}")
            await status_msg.edit_text(f"⚠️ Info found but post failed: {e}")
    else:
        await status_msg.edit_text(f"⚠️ Info not found, starting downloads...")

    # Fix script permissions
    script_path = "./animepahe-dl.sh"
    if os.path.exists(script_path): os.chmod(script_path, os.stat(script_path).st_mode | stat.S_IEXEC)

    success_count = 0
    skipped_count = 0

    for res in resolutions:
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
            pass 

        await process.wait()
        
        # --- HANDLE EXIT CODES ---
        if process.returncode == 2:
            await message.reply_text(f"⚠️ Skipped {res}p: File too large (>350MB).")
            skipped_count += 1
            continue

        # AnimePahe failed — try AnimeKAI as a fallback source
        if process.returncode != 0:
            await status_msg.edit_text(
                f"⚠️ AnimePahe failed for {res}p — trying AnimeKAI fallback..."
            )
            kai_file = await _download_via_animekai(anime_name, episode, res)
            if not kai_file:
                await message.reply_text(
                    f"❌ Both sources failed for **{res}p** (AnimePahe + AnimeKAI)."
                )
                continue
            final_filename = kai_file
        else:
            # AnimePahe exited 0 — find the downloaded file (mp4 or mkv)
            files = glob.glob("**/*.mp4", recursive=True) + glob.glob("**/*.mkv", recursive=True)
            # Exclude our own already-renamed output files to avoid false matches
            files = [f for f in files if not f.startswith("Ep_")]
            if not files:
                # Exit-0 but no file = AnimePahe had no file at this resolution.
                # Treat it the same as a failure and try AnimeKAI.
                await status_msg.edit_text(
                    f"⚠️ AnimePahe had no file for {res}p — trying AnimeKAI fallback..."
                )
                kai_file = await _download_via_animekai(anime_name, episode, res)
                if not kai_file:
                    await message.reply_text(
                        f"❌ Both sources failed for **{res}p** (AnimePahe + AnimeKAI)."
                    )
                    continue
                final_filename = kai_file
            else:
                latest_file = max(files, key=os.path.getctime)
                safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
                ext = os.path.splitext(latest_file)[1] or ".mp4"
                final_filename = f"Ep_{episode}_{safe_name}_{res}p{ext}"
                os.rename(latest_file, final_filename)

        try:
            sent_doc = await app.send_document(
                MAIN_CHANNEL,
                document=final_filename,
                caption=final_filename,
                force_document=True,
            )
            await _mirror_to_db(sent_doc)
            if "1080" in res:
                sent_sticker = await app.send_sticker(MAIN_CHANNEL, STICKER_ID)
                await _mirror_to_db(sent_sticker)
            success_count += 1
        except Exception as e:
            await message.reply_text(f"⚠️ Upload Error: {e}")
        finally:
            if os.path.exists(final_filename):
                os.remove(final_filename)

        try:
            # Clean up any leftover directory from AnimePahe's download structure.
            # latest_file is only set in the AnimePahe-success branch.
            if process.returncode == 0:
                parent = os.path.dirname(locals().get("latest_file", ""))
                if parent and os.path.isdir(parent):
                    os.rmdir(parent)
        except Exception:
            pass

        if res != resolutions[-1]: await asyncio.sleep(30)

    # --- SPECIFIC COMPLETION MESSAGE (CRITICAL FOR CONTROLLER) ---
    if success_count > 0 or skipped_count > 0:
        await status_msg.edit_text(f"✅ **{anime_name} - Ep {episode} Uploaded!**")
    else:
        await status_msg.edit_text(f"❌ Task finished, but errors occurred.")

async def main():
    await app.start()
    await check_channels()
    await web_server()

    print("Bot is fully running...")

    await idle()


if __name__ == "__main__":
    print("Bot Starting...")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
