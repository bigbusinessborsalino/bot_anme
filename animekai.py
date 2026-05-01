"""Thin async wrapper around the kai-tmux (AnimeKAI) library.

Same philosophy as the animepahe wrapper: we only do search + episode list +
stream URL resolution. No file downloads, no ffmpeg.

The upstream resolution flow depends on a third-party decoder service
(enc-dec.app) which is occasionally slow or returns malformed payloads.
We harden against that here by:
  * retrying decoder calls a few times,
  * trying every server returned for a stream type (not just the first 3),
  * skipping obviously-malformed embed URLs that point back at the source,
  * if the user's chosen stream type has no working server, falling back
    to other available types so we always return something usable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from animekai_tmux.api import AnimeKAIClient  # type: ignore
from animekai_tmux.utils.constants import ALT_URLS, BASE_URL  # type: ignore
from animekai_tmux.utils.network import get_working_domain  # type: ignore

log = logging.getLogger(__name__)

# Hosts that should never appear as the embed URL host — if the decoder
# returns one of these, the decode result is junk and we must skip it.
_SOURCE_HOSTS = {urlparse(u).netloc for u in ([BASE_URL] + list(ALT_URLS))}

# Lazy domain resolution: pick a reachable AnimeKAI mirror once, then reuse.
_client: Optional[AnimeKAIClient] = None
_client_lock = asyncio.Lock()


async def _get_client() -> AnimeKAIClient:
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            try:
                base_url = await asyncio.wait_for(
                    asyncio.to_thread(get_working_domain), timeout=20.0
                )
            except Exception as e:
                log.warning("AnimeKAI domain probe failed (%s); using default", e)
                base_url = "https://anikai.to"
            _client = AnimeKAIClient(base_url=base_url)
    return _client


@dataclass
class AnimeResult:
    title: str
    path: str            # /watch/<slug>
    poster: Optional[str] = None


@dataclass
class EpisodeResult:
    number: str          # may be "1", "1.5", etc — keep as string
    title: str
    token: str


@dataclass
class StreamVariant:
    quality: str
    stream_type: str     # sub / dub / softsub
    server_name: str
    embed_url: str
    playlist_url: str


def _search_sync(client: AnimeKAIClient, query: str, limit: int) -> List[AnimeResult]:
    raw = client.search(query) or []
    out: List[AnimeResult] = []
    for r in raw[:limit]:
        title = r.get("title")
        path = r.get("path")
        if not title or not path:
            continue
        poster = r.get("poster") or None
        out.append(AnimeResult(title=str(title), path=str(path), poster=poster))
    return out


def _episodes_sync(client: AnimeKAIClient, path: str) -> List[EpisodeResult]:
    raw = client.get_episodes(path) or []
    out: List[EpisodeResult] = []
    for ep in raw:
        num = ep.get("num")
        token = ep.get("token")
        if not num or not token:
            continue
        out.append(EpisodeResult(
            number=str(num),
            title=str(ep.get("title") or ""),
            token=str(token),
        ))
    return out


def _list_stream_types_sync(
    client: AnimeKAIClient, path: str, token: str,
) -> List[str]:
    servers_by_type: Dict[str, list] = client.get_servers(token, path) or {}
    return [t for t in ("sub", "dub", "softsub") if t in servers_by_type] or list(
        servers_by_type.keys()
    )


def _is_valid_embed(embed_url: str) -> bool:
    """A real megaup/etc embed URL must have an http(s) scheme, a netloc that
    isn't the source site, and at least one path segment to use as a token."""
    try:
        p = urlparse(embed_url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    if not p.netloc or p.netloc in _SOURCE_HOSTS:
        return False
    if not [seg for seg in p.path.split("/") if seg]:
        return False
    return True


def _resolve_one_server(
    client: AnimeKAIClient, path: str, srv: Dict, stream_type: str,
    decoder_attempts: int = 3,
) -> List["StreamVariant"]:
    """Try one server with retries on the flaky decoder. Returns variants or []."""
    lid = srv.get("lid") or ""
    name = str(srv.get("name") or "server")
    if not lid:
        return []

    last_error: Optional[Exception] = None
    for attempt in range(1, decoder_attempts + 1):
        try:
            source = client.get_source(lid, path) or {}
            embed_url = (source.get("url") or "").strip()
            if not embed_url or not _is_valid_embed(embed_url):
                log.warning(
                    "AnimeKAI server %s returned invalid embed (attempt %d/%d): %r",
                    name, attempt, decoder_attempts, embed_url,
                )
                last_error = RuntimeError(f"invalid embed: {embed_url!r}")
                time.sleep(0.7 * attempt)
                continue
            variants = client.get_m3u8_variants(embed_url) or []
            if not variants:
                log.warning(
                    "AnimeKAI server %s gave no variants (attempt %d/%d) embed=%s",
                    name, attempt, decoder_attempts, embed_url,
                )
                last_error = RuntimeError("no variants")
                time.sleep(0.7 * attempt)
                continue
            out: List[StreamVariant] = []
            for v in variants:
                playlist_url = (v.get("url") or "").strip()
                if not playlist_url:
                    continue
                out.append(StreamVariant(
                    quality=str(v.get("quality") or "best"),
                    stream_type=stream_type,
                    server_name=name,
                    embed_url=embed_url,
                    playlist_url=playlist_url,
                ))
            if out:
                def _qkey(s: StreamVariant) -> int:
                    digits = "".join(ch for ch in s.quality if ch.isdigit())
                    return int(digits) if digits else 0
                out.sort(key=_qkey, reverse=True)
                return out
        except Exception as e:
            last_error = e
            log.warning(
                "AnimeKAI server %s failed (attempt %d/%d): %s",
                name, attempt, decoder_attempts, e,
            )
            time.sleep(0.7 * attempt)
            continue

    if last_error:
        log.info("Server %s exhausted retries: %s", name, last_error)
    return []


def _list_variants_sync(
    client: AnimeKAIClient, path: str, token: str, stream_type: str,
) -> List[StreamVariant]:
    """Walk every server for the chosen type, then fall back to other types."""
    servers_by_type: Dict[str, list] = client.get_servers(token, path) or {}
    if not servers_by_type:
        log.info("AnimeKAI returned no servers at all for token=%s", token)
        return []

    # Build the order of types to try: requested first, then the rest.
    type_order: List[str] = []
    if stream_type in servers_by_type:
        type_order.append(stream_type)
    for t in ("sub", "softsub", "dub"):
        if t in servers_by_type and t not in type_order:
            type_order.append(t)
    for t in servers_by_type.keys():
        if t not in type_order:
            type_order.append(t)

    for t in type_order:
        servers = servers_by_type.get(t) or []
        if not servers:
            continue
        for srv in servers:  # try every server, not just first 3
            variants = _resolve_one_server(client, path, srv, t)
            if variants:
                if t != stream_type:
                    log.info(
                        "AnimeKAI: requested type=%s had no working server; "
                        "served from fallback type=%s", stream_type, t,
                    )
                return variants

    log.info("All AnimeKAI servers failed for token=%s type=%s", token, stream_type)
    return []


# ---- public async API -----------------------------------------------------


async def search(query: str, limit: int = 10, timeout: float = 30.0) -> List[AnimeResult]:
    client = await _get_client()
    return await asyncio.wait_for(
        asyncio.to_thread(_search_sync, client, query, limit), timeout=timeout
    )


async def list_episodes(path: str, timeout: float = 45.0) -> List[EpisodeResult]:
    client = await _get_client()
    return await asyncio.wait_for(
        asyncio.to_thread(_episodes_sync, client, path), timeout=timeout
    )


async def list_stream_types(
    path: str, token: str, timeout: float = 30.0,
) -> List[str]:
    client = await _get_client()
    return await asyncio.wait_for(
        asyncio.to_thread(_list_stream_types_sync, client, path, token),
        timeout=timeout,
    )


async def list_variants(
    path: str, token: str, stream_type: str, timeout: float = 180.0,
) -> List[StreamVariant]:
    client = await _get_client()
    return await asyncio.wait_for(
        asyncio.to_thread(_list_variants_sync, client, path, token, stream_type),
        timeout=timeout,
    )
