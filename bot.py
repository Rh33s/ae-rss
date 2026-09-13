#!/usr/bin/env python3
"""
Telegram RSS Bot with Telegram Rich Messages (Bot API 10.1+):
- Native Inline Slideshows (<tg-slideshow>) & Collages (<tg-collage>)
- Per-scene Collapsible Details Sections (<details><summary>...</summary>...)
- Per-scene 4K Screenshot Caps (<tg-collage> inside each scene)
- Metadata Tables (<table bordered compact>)
- Inline Action Buttons (<tg-button>)
- Automated age verification handshake and cookie persistence
- Serverless state tracking in data/history.json for GitHub Actions cron runs
"""

from __future__ import annotations

import os
import sys
import json
import time
import html
import re
import argparse
import logging
import socket
from typing import Any, Dict, List, Optional

# Force IPv4 socket resolution globally
_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _getaddrinfo_ipv4

import urllib3.util.connection
urllib3.util.connection.HAS_IPV6 = False

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("RssTelegramBot")


def load_dotenv(filepath: str = ".env") -> None:
    """Load environment variables from a local .env file if present."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        logger.warning(f"Could not load .env: {e}")


load_dotenv()


def format_title(title: str) -> str:
    """Format title ensuring a single space before parentheses e.g. 'Mrs. Creampie Vol. 9(2026)' -> 'Mrs. Creampie Vol. 9 (2026)'."""
    if not title:
        return ""
    formatted = re.sub(r"(\S)\(", r"\1 (", str(title))
    return re.sub(r"\s+\(", " (", formatted).strip()

# Configuration
DEFAULT_FEED_URL = "https://www.adultdvdempire.com/new-release-porn-videos.html?format=MRSS"
raw_feed_url = os.getenv("FEED_URL", "").strip()
if not raw_feed_url or not raw_feed_url.startswith("http") or "adultdvdempire.com" not in raw_feed_url:
    FEED_URL = DEFAULT_FEED_URL
else:
    FEED_URL = raw_feed_url
    if "format=MRSS" not in FEED_URL:
        delim = "&" if "?" in FEED_URL else "?"
        FEED_URL = f"{FEED_URL}{delim}format=MRSS"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
HISTORY_FILE = os.getenv("HISTORY_FILE", "data/history.json")
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "10"))
INITIAL_POST_LIMIT = int(os.getenv("INITIAL_POST_LIMIT", "0"))
MAX_HISTORY_SIZE = int(os.getenv("MAX_HISTORY_SIZE", "1000"))
MAX_CAPS_IN_SLIDESHOW = int(os.getenv("MAX_CAPS_IN_SLIDESHOW", "6"))
MAX_SCENE_CAPS = int(os.getenv("MAX_SCENE_CAPS", "4"))
POST_DELAY_SECONDS = float(os.getenv("POST_DELAY_SECONDS", "5.0"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "1800"))

# GitHub Gist remote persistence settings (for Render / ephemeral hosts)
GIST_ID = os.getenv("GIST_ID", "").strip()
GIST_TOKEN = os.getenv("GIST_TOKEN", "").strip() or os.getenv("GITHUB_TOKEN", "").strip()
GIST_FILENAME = os.getenv("GIST_FILENAME", "history.json").strip()

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FeedScraper:
    """Handles session negotiation, feed parsing, and detail enrichment."""

    AGE_COOKIE_DOMAINS = [
        ".adultdvdempire.com",
        "www.adultdvdempire.com",
        "adultdvdempire.com",
        ".adultempire.com",
        "www.adultempire.com",
        "adultempire.com",
    ]

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update(REQUEST_HEADERS)
        self.proxy_url = os.getenv("PROXY_URL", "").strip() or os.getenv("HTTPS_PROXY", "").strip()
        if self.proxy_url:
            self._apply_proxy(self.proxy_url)
        self._seed_age_cookies()
        self._perform_age_handshake()

    def _seed_age_cookies(self) -> None:
        """Pre-seed age confirmation cookie across all ADE domains."""
        for d in self.AGE_COOKIE_DOMAINS:
            self.session.cookies.set("ageConfirmed", "true", domain=d, path="/")

    def _apply_proxy(self, proxy_url: str) -> None:
        """Configures the requests session to route traffic through the given proxy."""
        self.session.proxies.update({
            "http": proxy_url,
            "https": proxy_url,
        })
        logger.info(f"Enabled proxy routing: {proxy_url}")

    def _perform_age_handshake(self, redirect_url: str = "https://www.adultdvdempire.com/") -> None:
        """
        Performs age confirmation AJAX handshake matching the site's SiteWide.js:
          $.ajax({ url: "Account/AgeConfirmation", data: { ageConfirmationClicked: true } })
        Sets the server-validated 'ageConfirmed' and session 'etoken' cookies.
        """
        from urllib.parse import urlparse
        parsed = urlparse(redirect_url)
        domain = parsed.netloc or "www.adultdvdempire.com"
        scheme = parsed.scheme or "https"
        base_origin = f"{scheme}://{domain}"

        confirm_url = f"{base_origin}/Account/AgeConfirmation"
        try:
            resp = self.session.get(
                confirm_url,
                params={"ageConfirmationClicked": "true"},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{base_origin}/",
                },
                timeout=(10, 15),
            )
            logger.info(f"AgeConfirmation handshake ({confirm_url}): status={resp.status_code}")
        except Exception as e:
            logger.warning(f"AgeConfirmation handshake notice at {confirm_url}: {e}")

        # Ensure ageConfirmed cookie is present
        self._seed_age_cookies()

    def fetch_feed(self, feed_url: str) -> str:
        """
        Fetches the MRSS feed with automatic age verification handshake.
        1. Pre-seeds age verification cookies.
        2. Pre-authenticates via eager AJAX handshake to obtain real session etoken.
        3. Retries if challenged.
        """
        if not feed_url or not feed_url.startswith("http"):
            feed_url = DEFAULT_FEED_URL
        if "format=MRSS" not in feed_url:
            delim = "&" if "?" in feed_url else "?"
            feed_url = f"{feed_url}{delim}format=MRSS"

        logger.info(f"Connecting to feed provider: {feed_url}")
        self._seed_age_cookies()

        max_attempts = 3
        last_resp = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.get(feed_url, timeout=(10, 25))
                resp.raise_for_status()
            except Exception as e:
                logger.warning(f"Feed fetch attempt {attempt}/{max_attempts} failed: {e}")
                if attempt == max_attempts:
                    raise
                time.sleep(2)
                continue

            if "<item>" in resp.text:
                if attempt > 1:
                    logger.info(f"Feed retrieved successfully on attempt {attempt}.")
                return resp.text

            last_resp = resp

            if attempt < max_attempts:
                logger.info(
                    f"Attempt {attempt}/{max_attempts}: No <item> tags found "
                    f"(redirected to {resp.url}). Performing age handshake..."
                )
                self._perform_age_handshake(resp.url)
                time.sleep(1)

        # All attempts exhausted — return last response with warning
        if last_resp is not None:
            title_m = re.search(r"<title>(.*?)</title>", last_resp.text, re.I)
            page_title = title_m.group(1).strip() if title_m else "No Title"
            logger.warning(
                f"Feed response missing <item> tags after {max_attempts} attempts. "
                f"Title: '{page_title}', URL: {last_resp.url}, Length: {len(last_resp.text)}"
            )
            return last_resp.text

        raise RuntimeError(f"Failed to fetch feed after {max_attempts} attempts")

    @staticmethod
    def parse_feed_items(feed_xml: str) -> List[Dict[str, Any]]:
        """Extracts items from MRSS feed."""
        items: List[Dict[str, Any]] = []
        raw_items = re.findall(r"<item>(.*?)</item>", feed_xml, re.DOTALL)
        
        for raw in raw_items:
            guid_match = re.search(r"<guid(?: [^>]*)?>([0-9a-zA-Z_-]+)</guid>", raw)
            guid = guid_match.group(1).strip() if guid_match else None
            
            title_match = re.search(r"<title>(.*?)</title>", raw, re.DOTALL)
            title = html.unescape(title_match.group(1).strip()) if title_match else ""
            if title:
                title = format_title(title)
            
            link_match = re.search(r"<link>(.*?)</link>", raw, re.DOTALL)
            link = link_match.group(1).strip() if link_match else ""
            
            pub_match = re.search(r"<pubDate>(.*?)</pubDate>", raw, re.DOTALL)
            pub_date = pub_match.group(1).strip() if pub_match else ""
            
            img_match = re.search(r"<media:(?:content|thumbnail)[^>]*\burl=['\"]([^'\"]+)['\"]", raw)
            raw_img_url = img_match.group(1).strip() if img_match else None
            
            hd_front = None
            hd_back = None
            if raw_img_url:
                hd_front = re.sub(r"(\d+)\.jpg$", r"\1h.jpg", raw_img_url)
                hd_back = re.sub(r"(\d+)\.jpg$", r"\1bh.jpg", raw_img_url)
            
            price_match = re.search(r':price=\"([0-9.]+)\"\s*:currency=\"\'?([A-Z]+)\'?', raw)
            price_str = f"{price_match.group(1)} {price_match.group(2)}" if price_match else None
            
            if guid and link:
                items.append({
                    "id": str(guid),
                    "title": title,
                    "link": link,
                    "pub_date": pub_date,
                    "image_url": hd_front or raw_img_url,
                    "thumb_url": raw_img_url,
                    "hd_front": hd_front,
                    "hd_back": hd_back,
                    "price": price_str,
                    "cast": [],
                    "scenes": [],
                    "caps": [],
                    "studio": None
                })
        
        logger.info(f"Parsed {len(items)} items from feed.")
        return items

    def enrich_product_details(self, item: Dict[str, Any]) -> None:
        """
        Fetches product page to extract:
        - Cast list
        - Scenes with direct anchor links and per-scene 4K screenshot caps
        - High-resolution screenshot caps (caps1cdn)
        - Studio
        """
        url = item.get("link")
        if not url:
            return
        try:
            resp = self.session.get(url, timeout=(10, 20))
            if resp.status_code != 200:
                return
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Cast
            cast_list: List[str] = []
            cast_hdr = soup.find(lambda el: el.name in ["h2", "h3", "h4", "strong", "b", "span", "p"] and "Starring" in el.get_text())
            if cast_hdr:
                parent = cast_hdr.find_parent("div") or cast_hdr.find_parent("p")
                if parent:
                    for a_tag in parent.find_all("a"):
                        name = a_tag.get_text(strip=True)
                        if name and name not in cast_list:
                            cast_list.append(name)
            item["cast"] = cast_list

            # Scenes mapping by scene_id
            scene_map: Dict[str, Dict[str, Any]] = {}
            scene_order: List[str] = []

            # 1. Primary: match clip links and extract per-scene cast from the scene row
            for a_clip in soup.find_all("a", href=re.compile(r"/clip/(\d+)/")):
                href = a_clip.get("href", "").strip()
                sid_match = re.search(r"/clip/(\d+)/", href)
                if not sid_match:
                    continue
                sid = sid_match.group(1)

                clip_url = (
                    href
                    if href.startswith("http")
                    else (
                        f"https://www.adultdvdempire.com{href}"
                        if href.startswith("/")
                        else f"https://www.adultdvdempire.com/{href}"
                    )
                )

                raw_text = a_clip.get_text(strip=True)
                is_dur = bool(re.match(r"^(?:\d+K\s*UHD\s*)?\d+\s*min$", raw_text, re.IGNORECASE))
                stitle = raw_text if (raw_text and not is_dur) else ""

                row = a_clip.find_parent("div", class_="row")
                sc_cast: List[str] = []
                if row:
                    for ca in row.find_all("a"):
                        ca_href = ca.get("href", "")
                        if "/porn-videos/" in ca_href and "-pornstars.html" in ca_href:
                            name = ca.get_text(strip=True)
                            if name and name not in sc_cast:
                                sc_cast.append(name)

                if sid not in scene_map:
                    scene_map[sid] = {
                        "id": sid,
                        "title": stitle or "Scene",
                        "url": clip_url,
                        "cast": sc_cast,
                        "caps": []
                    }
                    scene_order.append(sid)
                else:
                    if sc_cast and not scene_map[sid]["cast"]:
                        scene_map[sid]["cast"] = sc_cast
                    if stitle and (
                        scene_map[sid]["title"] in ["Scene", ""]
                        or re.match(r"^(?:\d+K\s*UHD\s*)?\d+\s*min$", scene_map[sid]["title"], re.IGNORECASE)
                    ):
                        scene_map[sid]["title"] = stitle
                    if clip_url and not scene_map[sid].get("url", "").startswith("https://www.adultdvdempire.com/clip/"):
                        scene_map[sid]["url"] = clip_url

            # 2. Fallback / supplement: find any div_scenePreview_<id> modals
            for sp in soup.find_all(id=re.compile(r"^div_scenePreview_(\d+)")):
                sid_match = re.search(r"\d+", sp.get("id", ""))
                if not sid_match:
                    continue
                sid = sid_match.group(0)
                if sid not in scene_map:
                    hdr = sp.find(["h2", "h3", "h4", "h5"])
                    stitle = hdr.get_text(strip=True) if hdr else "Scene"
                    sp_clip = sp.find("a", href=re.compile(r"/clip/"))
                    if sp_clip:
                        sp_href = sp_clip.get("href", "").strip()
                        clip_url = (
                            sp_href
                            if sp_href.startswith("http")
                            else (
                                f"https://www.adultdvdempire.com{sp_href}"
                                if sp_href.startswith("/")
                                else f"https://www.adultdvdempire.com/{sp_href}"
                            )
                        )
                    else:
                        clip_url = f"https://www.adultdvdempire.com/clip/{sid}/scene-{len(scene_order) + 1}-streaming-scene.html"
                    scene_map[sid] = {
                        "id": sid,
                        "title": stitle,
                        "url": clip_url,
                        "cast": [],
                        "caps": []
                    }
                    scene_order.append(sid)

            # Associate screenshot caps directly to their corresponding scene_id
            for a_tag in soup.find_all("a", rel="scenescreenshots"):
                sid = a_tag.get("scene_id")
                href = a_tag.get("href")
                if sid and href and sid in scene_map:
                    if href not in scene_map[sid]["caps"]:
                        scene_map[sid]["caps"].append(href)

            item["scenes"] = [scene_map[sid] for sid in scene_order]

            # Overall high-resolution screenshot caps (caps1cdn.adultempire.com)
            caps_matches = re.findall(r"https://caps1cdn\.adultempire\.com/[a-z]/\d+/3840/\w+\.jpg", resp.text)
            unique_caps: List[str] = []
            for cap_url in caps_matches:
                if cap_url not in unique_caps:
                    unique_caps.append(cap_url)
            item["caps"] = unique_caps
            logger.info(f"Item {item['id']}: Found {len(item['scenes'])} scenes and {len(unique_caps)} total screenshot caps.")

            # Studio
            studio_tag = soup.find("a", class_=re.compile(r"studio", re.I))
            if studio_tag:
                item["studio"] = studio_tag.get_text(strip=True)

        except Exception as e:
            logger.warning(f"Could not enrich item {item.get('id')}: {e}")


class HistoryManager:
    """
    Maintains persistent state of sent items.
    Supports GitHub Gist synchronization (remote persistence for Render/serverless)
    with local filesystem fallback and caching.
    """

    def __init__(
        self,
        filepath: str = HISTORY_FILE,
        gist_id: str = GIST_ID,
        gist_token: str = GIST_TOKEN,
        gist_filename: str = GIST_FILENAME,
        max_size: int = MAX_HISTORY_SIZE,
    ):
        self.filepath = filepath
        self.gist_id = gist_id
        self.gist_token = gist_token
        self.gist_filename = gist_filename
        self.max_size = max_size
        self.history: List[str] = self._load()

    def _get_gist_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ae-rss-bot",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.gist_token:
            headers["Authorization"] = f"Bearer {self.gist_token}"
        return headers

    def _load_from_gist(self) -> Optional[List[str]]:
        if not self.gist_id:
            return None

        url = f"https://api.github.com/gists/{self.gist_id}"
        try:
            resp = requests.get(url, headers=self._get_gist_headers(), timeout=15)
            if resp.status_code == 200:
                gist_data = resp.json()
                files = gist_data.get("files", {})
                
                # Match filename exactly or case-insensitively
                file_info = None
                for fname, finfo in files.items():
                    if fname.lower() == self.gist_filename.lower():
                        file_info = finfo
                        break

                if file_info:
                    raw_content = file_info.get("content")
                    if raw_content is None and file_info.get("raw_url"):
                        raw_resp = requests.get(
                            file_info["raw_url"],
                            headers=self._get_gist_headers(),
                            timeout=15,
                        )
                        raw_content = raw_resp.text
                    if raw_content:
                        data = json.loads(raw_content)
                        if isinstance(data, list):
                            logger.info(
                                f"Successfully loaded {len(data)} item IDs from GitHub Gist "
                                f"({self.gist_id}/{self.gist_filename})."
                            )
                            return [str(x) for x in data]
                else:
                    logger.warning(
                        f"Gist '{self.gist_id}' found, but file '{self.gist_filename}' does not exist in it yet."
                    )
                    return []
            else:
                logger.error(
                    f"Failed to fetch GitHub Gist {self.gist_id}: HTTP {resp.status_code} - {resp.text}"
                )
        except Exception as e:
            logger.error(f"Error fetching history from GitHub Gist: {e}")
        return None

    def _create_gist_if_needed(self, initial_data: List[str]) -> Optional[str]:
        """Auto-creates a private Gist if GIST_TOKEN is provided but GIST_ID is empty."""
        if not self.gist_token or self.gist_id:
            return self.gist_id

        url = "https://api.github.com/gists"
        payload = {
            "description": "ae-rss bot history tracking",
            "public": False,
            "files": {
                self.gist_filename: {
                    "content": json.dumps(initial_data, indent=2)
                }
            },
        }
        try:
            resp = requests.post(
                url, json=payload, headers=self._get_gist_headers(), timeout=15
            )
            if resp.status_code in (200, 201):
                new_id = resp.json().get("id")
                logger.info(
                    f"✨ Automatically created new private GitHub Gist for history: {new_id}. "
                    f"Set GIST_ID={new_id} in your environment variables."
                )
                self.gist_id = new_id
                return new_id
            else:
                logger.error(f"Failed to auto-create GitHub Gist: HTTP {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Error auto-creating GitHub Gist: {e}")
        return None

    def _save_to_gist(self, data: List[str]) -> bool:
        if not self.gist_token:
            return False

        if not self.gist_id:
            created_id = self._create_gist_if_needed(data)
            return bool(created_id)

        url = f"https://api.github.com/gists/{self.gist_id}"
        payload = {
            "files": {
                self.gist_filename: {
                    "content": json.dumps(data, indent=2)
                }
            }
        }
        try:
            resp = requests.patch(
                url, json=payload, headers=self._get_gist_headers(), timeout=15
            )
            if resp.status_code == 200:
                logger.info(
                    f"Successfully synced {len(data)} item IDs to GitHub Gist ({self.gist_id}/{self.gist_filename})."
                )
                return True
            else:
                logger.error(
                    f"Failed to update GitHub Gist {self.gist_id}: HTTP {resp.status_code} - {resp.text}"
                )
        except Exception as e:
            logger.error(f"Error saving history to GitHub Gist: {e}")
        return False

    def _load(self) -> List[str]:
        # 1. Attempt Gist sync first if GIST_ID is configured
        if self.gist_id:
            gist_items = self._load_from_gist()
            if gist_items is not None:
                # Cache remotely fetched items locally
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
                    with open(self.filepath, "w", encoding="utf-8") as f:
                        json.dump(gist_items[-self.max_size:], f, indent=2)
                except Exception:
                    pass
                return gist_items

        # 2. Fallback to local file
        if not os.path.exists(self.filepath):
            os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
            return []
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [str(x) for x in data]
        except Exception as e:
            logger.warning(f"Error reading history file '{self.filepath}': {e}. Starting fresh.")
        return []

    def save(self) -> None:
        trimmed = self.history[-self.max_size:]
        # 1. Local filesystem persistence
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(trimmed, f, indent=2)
            logger.info(f"Saved {len(trimmed)} item IDs to local history ({self.filepath}).")
        except Exception as e:
            logger.error(f"Failed to save history to '{self.filepath}': {e}")

        # 2. Remote GitHub Gist synchronization
        if self.gist_token:
            self._save_to_gist(trimmed)

    def is_seen(self, item_id: str) -> bool:
        return str(item_id) in self.history

    def add(self, item_id: str) -> None:
        sid = str(item_id)
        if sid not in self.history:
            self.history.append(sid)


class TelegramPublisher:
    """
    Formats and publishes releases using Telegram's latest Rich Messages (Bot API 10.1):
    - Collapsible details sections for each scene (<details><summary>...</summary>...)
    - Per-scene 4K screenshot caps (<tg-collage> inside each scene)
    - Full release slideshow of front/back covers and 4K screen caps (<tg-slideshow>)
    - Formatted metadata tables (<table bordered compact>)
    - Inline action buttons (<tg-button>)
    With automatic fallback to sendMediaGroup, sendPhoto, and sendMessage.
    """

    def __init__(self, token: str, chat_id: str):
        self.token = str(token).strip().strip("'\"")
        cid = str(chat_id).strip().strip("'\"")
        # Automatically fix common chat_id formatting mistakes:
        # e.g. -4355835192 -> -1004355835192 (supergroups/channels)
        if cid.startswith("-") and not cid.startswith("-100"):
            cid = f"-100{cid[1:]}"
        # e.g. advdempire -> @advdempire
        elif cid and not cid.startswith("-") and not cid.startswith("@") and not cid.isdigit():
            cid = f"@{cid}"
        self.chat_id = cid
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    @staticmethod
    def esc(text: Any) -> str:
        """Safely escape HTML for Telegram parse mode."""
        if not text:
            return ""
        return html.escape(str(text))

    def build_rich_message_html(self, item: Dict[str, Any]) -> str:
        """
        Builds modern Rich Message HTML according to Telegram Bot API 10.1 specification:
        - <h2> Title Header
        - <table bordered compact> for metadata
        - <tg-slideshow> for main carousel of covers and scene highlights
        - <details><summary>...</summary>...</details> for each scene with its own <tg-collage>
        - <tg-button> for inline interactive buttons
        """
        title = self.esc(format_title(item.get("title", "New Release")))
        link = self.esc(item.get("link", ""))
        studio = self.esc(item.get("studio"))
        date = self.esc(item.get("pub_date"))
        price = self.esc(item.get("price"))
        cast = item.get("cast", [])
        scenes = item.get("scenes", [])
        caps = item.get("caps", [])
        hd_front = self.esc(item.get("hd_front"))
        hd_back = self.esc(item.get("hd_back"))

        parts = []

        MAX_TOTAL_MEDIA = 10
        media_count = 0

        # 1. Top Slideshow: Front Cover and Back Cover at very top
        slides = []
        if hd_front and media_count < MAX_TOTAL_MEDIA:
            slides.append(f'<img src="{hd_front}"/>')
            media_count += 1
        if hd_back and media_count < MAX_TOTAL_MEDIA:
            slides.append(f'<img src="{hd_back}"/>')
            media_count += 1

        if slides:
            parts.append("<tg-slideshow>")
            parts.extend(slides)
            parts.append("</tg-slideshow>")

        # 2. Heading with product URL after slideshow
        if link:
            parts.append(f'<h2><a href="{link}">{title}</a></h2>')
        else:
            parts.append(f"<h2>{title}</h2>")

        # 3. Metadata (no table, original date & time format)
        meta_lines = []
        if studio and studio.lower() not in title.lower():
            meta_lines.append(f"<b>Studio:</b> {studio}")
        if date:
            meta_lines.append(f"<b>Released:</b> {date}")
        if meta_lines:
            parts.append(f"<p>{'<br/>'.join(meta_lines)}</p>")

        # 4. Scenes with clickable direct scene title, starring cast, and photo slideshow
        if scenes:
            for idx, sc in enumerate(scenes, 1):
                sc_title = self.esc(sc["title"])
                sc_url = self.esc(sc["url"])
                sc_caps = sc.get("caps", [])
                sc_cast = sc.get("cast", [])
                sc_cast_str = ", ".join([self.esc(c) for c in sc_cast])

                is_generic = bool(re.match(r"^(Scene\s*\d*|Chapter\s*\d*)$", sc_title, re.IGNORECASE))
                if is_generic and sc_cast_str:
                    display_title = sc_cast_str
                elif re.match(r"^(Scene|Chapter)\s*\d+[:\s-]*(.*)", sc_title, re.IGNORECASE):
                    m = re.match(r"^(Scene|Chapter)\s*\d+[:\s-]*(.*)", sc_title, re.IGNORECASE)
                    rest = m.group(2).strip()
                    display_title = rest if rest else f"Scene {idx}"
                else:
                    display_title = sc_title

                if display_title.lower() in [f"scene {idx}".lower(), f"scene{idx}".lower(), "scene"]:
                    scene_label = f"Scene {idx}"
                else:
                    scene_label = f"Scene {idx}: {display_title}"

                parts.append(f'<p><a href="{sc_url}"><b>{scene_label}</b></a></p>')

                # Collapsible section to hide starring cast and thumbnails
                even_caps = [sc_caps[i] for i in [1, 3, 5, 7] if i < len(sc_caps)] or sc_caps[1::2] or sc_caps
                has_cast = bool(sc_cast_str and not is_generic)

                caps_to_add = []
                remaining_media = MAX_TOTAL_MEDIA - media_count
                if remaining_media > 0 and even_caps:
                    caps_to_add = even_caps[:min(MAX_SCENE_CAPS, remaining_media)]
                    media_count += len(caps_to_add)

                if has_cast or caps_to_add:
                    summary = "Starring &amp; Screenshots" if (has_cast and caps_to_add) else ("Starring" if has_cast else "Screenshots")
                    details_html = [f"<details><summary>{summary}</summary>"]
                    if has_cast:
                        details_html.append(f"<p><b>Starring:</b> {sc_cast_str}</p>")
                    if caps_to_add:
                        details_html.append("<tg-slideshow>")
                        for c_url in caps_to_add:
                            details_html.append(f'<img src="{self.esc(c_url)}"/>')
                        details_html.append("</tg-slideshow>")
                    details_html.append("</details>")
                    parts.append("\n".join(details_html))

        return "\n".join(parts)

    def build_standard_caption(self, item: Dict[str, Any], max_len: int = 1024) -> str:
        """
        Builds a strictly valid HTML caption for Telegram media messages (<= 1024 characters).
        Ensures HTML tags are fully balanced and never sliced mid-tag.
        """
        raw_title = format_title(item.get("title", "New Release"))
        link = item.get("link", "").strip()
        studio = item.get("studio", "").strip() if item.get("studio") else ""
        date = item.get("pub_date", "").strip() if item.get("pub_date") else ""
        scenes = item.get("scenes", [])

        # 1. Base header lines
        if len(raw_title) > 200:
            raw_title = raw_title[:197].rstrip() + "…"
        esc_title = self.esc(raw_title)
        esc_link = self.esc(link)

        header_lines = [
            f'<a href="{esc_link}"><b>{esc_title}</b></a>' if esc_link else f"<b>{esc_title}</b>"
        ]
        if studio and studio.lower() not in raw_title.lower():
            header_lines.append(f"<b>Studio:</b> {self.esc(studio)}")
        if date:
            header_lines.append(f"<b>Released:</b> {self.esc(date)}")

        base_header = "\n".join(header_lines)
        if not scenes:
            return base_header[:max_len]

        # 2. Try adding full scene info (with starring) if budget permits
        full_scene_blocks: List[str] = []
        for idx, sc in enumerate(scenes, 1):
            sc_title = sc.get("title", "")
            sc_url = sc.get("url", "")
            sc_cast = sc.get("cast", [])
            sc_cast_str = ", ".join(sc_cast)

            is_generic = bool(re.match(r"^(Scene\s*\d*|Chapter\s*\d*)$", sc_title, re.IGNORECASE))
            if is_generic and sc_cast_str:
                display_title = sc_cast_str
            elif re.match(r"^(Scene|Chapter)\s*\d+[:\s-]*(.*)", sc_title, re.IGNORECASE):
                m = re.match(r"^(Scene|Chapter)\s*\d+[:\s-]*(.*)", sc_title, re.IGNORECASE)
                rest = m.group(2).strip()
                display_title = rest if rest else f"Scene {idx}"
            else:
                display_title = sc_title

            if display_title.lower() in [f"scene {idx}".lower(), f"scene{idx}".lower(), "scene"]:
                scene_label = f"Scene {idx}"
            else:
                scene_label = f"Scene {idx}: {display_title}"

            esc_sc_url = self.esc(sc_url)
            esc_label = self.esc(scene_label)

            line = f'<a href="{esc_sc_url}"><b>{esc_label}</b></a>' if esc_sc_url else f"<b>{esc_label}</b>"
            if sc_cast_str and not is_generic:
                line += f"\n<b>Starring:</b> {self.esc(sc_cast_str)}"
            full_scene_blocks.append(line)

        full_candidate = base_header + "\n\n" + "\n".join(full_scene_blocks)
        if len(full_candidate) <= max_len:
            return full_candidate

        # 3. Compact mode: compact scene links only
        compact_blocks: List[str] = []
        for idx, sc in enumerate(scenes, 1):
            sc_title = sc.get("title", "")
            sc_url = sc.get("url", "")
            if sc_title.lower() in [f"scene {idx}".lower(), f"scene{idx}".lower(), "scene"]:
                c_label = f"Scene {idx}"
            else:
                c_label = f"Scene {idx}: {sc_title}"

            esc_sc_url = self.esc(sc_url)
            esc_c_label = self.esc(c_label)
            line = f'<a href="{esc_sc_url}"><b>{esc_c_label}</b></a>' if esc_sc_url else f"<b>{esc_c_label}</b>"
            compact_blocks.append(line)

        accepted_scenes: List[str] = []
        base_prefix = base_header + "\n\n"
        for idx, line in enumerate(compact_blocks):
            remaining = len(compact_blocks) - (idx + 1)
            suffix = f"\n<i>...and {remaining} more scenes</i>" if remaining > 0 else ""
            test_caption = base_prefix + "\n".join(accepted_scenes + [line]) + suffix
            if len(test_caption) <= max_len:
                accepted_scenes.append(line)
            else:
                break

        if accepted_scenes:
            remaining = len(compact_blocks) - len(accepted_scenes)
            suffix = f"\n<i>...and {remaining} more scenes</i>" if remaining > 0 else ""
            return base_prefix + "\n".join(accepted_scenes) + suffix

        return base_header

    def build_slideshow_media(self, item: Dict[str, Any], caption: str) -> List[Dict[str, Any]]:
        """Builds multi-photo media group for sendMediaGroup fallback."""
        media_list: List[Dict[str, Any]] = []
        front_img = item.get("hd_front") or item.get("image_url")
        back_img = item.get("hd_back")
        caps = item.get("caps", [])

        if front_img:
            media_list.append({
                "type": "photo",
                "media": front_img,
                "caption": caption,
                "parse_mode": "HTML"
            })
        if back_img:
            media_list.append({
                "type": "photo",
                "media": back_img
            })
        # Top slideshow album contains only Front and Back covers
        return media_list

    def post_item(self, item: Dict[str, Any]) -> bool:
        """
        Dispatches item to Telegram:
        1. sendRichMessage (Telegram Bot API 10.1 with <details>, <tg-slideshow>, tables, buttons)
        2. Fallback to sendMediaGroup (Slideshow Album)
        3. Fallback to sendPhoto (Single Photo)
        4. Fallback to sendMessage (Text)
        """
        # 1. Attempt sendRichMessage
        rich_html = self.build_rich_message_html(item)
        try:
            resp = requests.post(
                f"{self.base_url}/sendRichMessage",
                json={
                    "chat_id": self.chat_id,
                    "rich_message": {"html": rich_html}
                },
                timeout=30
            )
            res = resp.json()
            if res.get("ok"):
                logger.info(f"Posted Rich Message for item '{item['id']}'.")
                return True
            logger.warning(f"sendRichMessage notice ({res.get('description')}). Using sendMediaGroup fallback.")
        except Exception as e:
            logger.warning(f"sendRichMessage error: {e}. Using sendMediaGroup fallback.")

        # 2. Fallback: sendMediaGroup
        standard_caption = self.build_standard_caption(item)
        slideshow_media = self.build_slideshow_media(item, standard_caption)

        if len(slideshow_media) > 1:
            try:
                resp = requests.post(
                    f"{self.base_url}/sendMediaGroup",
                    json={"chat_id": self.chat_id, "media": slideshow_media},
                    timeout=35
                )
                res = resp.json()
                if res.get("ok"):
                    logger.info(f"Posted Media Album Slideshow ({len(slideshow_media)} photos) for item '{item['id']}'.")
                    return True
                logger.warning(f"sendMediaGroup failed ({res.get('description')}). Using sendPhoto fallback.")
            except Exception as e:
                logger.warning(f"sendMediaGroup error: {e}. Using sendPhoto fallback.")

        # 3. Fallback: sendPhoto
        front_img = item.get("hd_front") or item.get("image_url")
        if front_img:
            try:
                resp = requests.post(
                    f"{self.base_url}/sendPhoto",
                    data={
                        "chat_id": self.chat_id,
                        "photo": front_img,
                        "caption": standard_caption,
                        "parse_mode": "HTML"
                    },
                    timeout=25
                )
                res = resp.json()
                if res.get("ok"):
                    logger.info(f"Posted single photo for item '{item['id']}'.")
                    return True
                logger.warning(f"sendPhoto notice ({res.get('description')}). Retrying with plain text.")
                clean_text = re.sub(r"<[^>]+>", "", standard_caption)
                resp2 = requests.post(
                    f"{self.base_url}/sendPhoto",
                    data={"chat_id": self.chat_id, "photo": front_img, "caption": clean_text[:1024]},
                    timeout=25
                )
                if resp2.json().get("ok"):
                    logger.info(f"Posted single photo (plain text) for item '{item['id']}'.")
                    return True
            except Exception as e:
                logger.warning(f"sendPhoto error: {e}.")

        # 4. Fallback: sendMessage
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": standard_caption,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False
                },
                timeout=25
            )
            res = resp.json()
            if res.get("ok"):
                logger.info(f"Posted text message for item '{item['id']}'.")
                return True
            clean_text = re.sub(r"<[^>]+>", "", standard_caption)
            resp2 = requests.post(
                f"{self.base_url}/sendMessage",
                data={"chat_id": self.chat_id, "text": clean_text[:4000]},
                timeout=25
            )
            if resp2.json().get("ok"):
                logger.info(f"Posted text message (plain text) for item '{item['id']}'.")
                return True
        except Exception as e:
            logger.error(f"sendMessage error: {e}")

        return False


def run_once(args) -> None:
    """Executes a single check and broadcast cycle."""
    scraper = FeedScraper()
    try:
        raw_feed = scraper.fetch_feed(FEED_URL)
    except Exception as e:
        logger.error(f"Failed to fetch feed: {e}")
        # In automated scheduled CI / cron, avoid hard crashing on transient remote outages
        if os.getenv("FAIL_ON_FEED_ERROR", "false").lower() not in ("1", "true", "yes"):
            logger.warning("Upstream feed provider is unreachable. Skipping this cycle.")
            return
        if not getattr(args, "loop", 0):
            sys.exit(1)
        return

    items = scraper.parse_feed_items(raw_feed)
    if not items:
        logger.info("No items found in feed.")
        return

    history = HistoryManager(HISTORY_FILE)
    is_initial_run = (len(history.history) == 0)

    if args.seed_only:
        logger.info("Seed-only mode: Marking current feed items as seen.")
        for it in items:
            history.add(it["id"])
        history.save()
        return

    feed_ids = [it["id"] for it in items]
    history_ids = set(history.history)
    new_items = [it for it in items if not history.is_seen(it["id"])]
    new_ids = [it["id"] for it in new_items]

    logger.info(f"Code comparison: {len(feed_ids)} total feed codes vs {len(history_ids)} history codes.")
    logger.info(f"History codes sample (recent): {history.history[-10:] if history.history else []}")

    if new_ids:
        logger.info(f"New codes detected ({len(new_ids)}): {new_ids}")
        for it in new_items:
            logger.info(f"  [NEW CODE] ID: {it['id']} | Title: {it['title']} | Released: {it.get('pub_date')}")
    else:
        logger.info("No new codes found compared to history.")

    if not new_items:
        logger.info("All feed items already processed.")
        return

    if is_initial_run:
        if INITIAL_POST_LIMIT > 0:
            logger.info(
                f"Initial run: Limiting to {INITIAL_POST_LIMIT} items; "
                f"marking {max(0, len(new_items) - INITIAL_POST_LIMIT)} items as seen."
            )
            to_broadcast = new_items[:INITIAL_POST_LIMIT]
            to_seed = new_items[INITIAL_POST_LIMIT:]
            for it in to_seed:
                history.add(it["id"])
        else:
            logger.info(f"Initial run: Broadcasting ALL {len(new_items)} feed items without limit.")
            to_broadcast = new_items
    else:
        to_broadcast = new_items

    if args.limit > 0 and len(to_broadcast) > args.limit:
        logger.info(f"Applying run limit: capping broadcast to {args.limit} items.")
        to_broadcast = to_broadcast[:args.limit]

    to_broadcast.reverse()

    if not args.dry_run:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")
            if not getattr(args, "loop", 0):
                sys.exit(1)
            return
        publisher = TelegramPublisher(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    else:
        publisher = TelegramPublisher("", "")
        logger.info("Running in DRY-RUN mode.")

    posted_count = 0
    for it in to_broadcast:
        scraper.enrich_product_details(it)

        if args.dry_run:
            rich_html = publisher.build_rich_message_html(it)
            logger.info(
                f"[DRY-RUN] Generated Rich Message for ID {it['id']}:\n"
                f"{rich_html}\n"
            )
            history.add(it["id"])
            posted_count += 1
        else:
            success = publisher.post_item(it)
            if success:
                history.add(it["id"])
                posted_count += 1
                time.sleep(POST_DELAY_SECONDS)
            else:
                logger.warning(f"Could not dispatch item {it['id']}; skipping.")

    logger.info(f"Finished processing. Successfully posted {posted_count} items.")
    history.save()

    _bot_telemetry["last_poll"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    _bot_telemetry["last_posted_count"] = posted_count
    _bot_telemetry["total_posted"] += posted_count
    _bot_telemetry["history_size"] = len(history.history)


_bot_telemetry: Dict[str, Any] = {
    "status": "running",
    "service": "ae-rss-bot",
    "uptime_start": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    "last_poll": None,
    "last_posted_count": 0,
    "total_posted": 0,
}


def _start_health_server_if_needed() -> None:
    """Starts a lightweight HTTP server on PORT for Render Web Service health checks and dashboard."""
    port_str = os.getenv("PORT", "").strip()
    if not port_str:
        return
    try:
        port = int(port_str)
    except ValueError:
        return

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            body = json.dumps(_bot_telemetry, indent=2).encode("utf-8")
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    def _serve():
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
            logger.info(f"Port detector health server listening on 0.0.0.0:{port} for Render/platform health checks.")
            httpd.serve_forever()
        except Exception as e:
            logger.warning(f"Could not bind health server to port {port}: {e}")

    t = threading.Thread(target=_serve, daemon=True, name="RenderHealthDetector")
    t.start()


def main():
    _start_health_server_if_needed()
    parser = argparse.ArgumentParser(description="RSS Telegram Bot with Rich Messages & Slideshows")
    parser.add_argument("--dry-run", action="store_true", help="Log output without dispatching to Telegram")
    parser.add_argument("--seed-only", action="store_true", help="Record current feed items without posting")
    parser.add_argument("--limit", type=int, default=MAX_POSTS_PER_RUN, help="Maximum new items to post")
    parser.add_argument(
        "--loop",
        nargs="?",
        const=int(os.getenv("POLL_INTERVAL_SECONDS", "1800")),
        type=int,
        default=0,
        help="Polling interval in seconds for continuous background worker mode (e.g. --loop or --loop 1800). Default is single run.",
    )
    args = parser.parse_args()

    loop_interval = args.loop
    if loop_interval <= 0 and os.getenv("PORT"):
        loop_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "1800"))
        logger.info(f"Cloud/Render environment detected (PORT={os.getenv('PORT')}). Automatically enabling continuous polling (interval: {loop_interval}s).")

    if loop_interval > 0:
        logger.info(f"Starting continuous polling mode (interval: {loop_interval} seconds)...")
        while True:
            try:
                run_once(args)
            except Exception as e:
                logger.error(f"Unexpected error in polling cycle: {e}")
            logger.info(f"Sleeping for {loop_interval} seconds until next check...")
            time.sleep(loop_interval)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
