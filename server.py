#!/usr/bin/env python3
"""
Clean XML Feed Server for Adult DVD Empire (ADE).

Provides a clean, standards-compliant RSS 2.0 / MRSS XML endpoint that:
- Automatically handles ADE age verification handshake in the background.
- Eliminates BOM, malformed unescaped tags, and duplicate/broken GUIDs.
- Includes high-definition front & back cover media attachments.
- Caches responses in memory with configurable TTL to ensure fast responses.

Endpoints:
- GET /clean.xml   -> Clean RSS 2.0 / MRSS XML feed
- GET /rss.xml     -> Alias for /clean.xml
- GET /feed.xml    -> Alias for /clean.xml
- GET /health      -> JSON status and cache telemetry
- GET /            -> Interactive dashboard with quick links
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape as xml_escape

# Force IPv4 socket resolution
_orig_getaddrinfo = socket.getaddrinfo

def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

socket.getaddrinfo = _getaddrinfo_ipv4

import urllib3.util.connection
urllib3.util.connection.HAS_IPV6 = False

import bot

bot.load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("clean_xml_server")

PORT = int(os.getenv("PORT", os.getenv("SERVER_PORT", "10000")))
HOST = os.getenv("HOST", "0.0.0.0")
CACHE_TTL = int(os.getenv("FEED_CACHE_TTL", "300"))  # 5 minutes default cache


class FeedCache:
    """Thread-safe in-memory cache for the cleaned XML feed."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.cached_xml: Optional[str] = None
        self.cached_items: List[Dict[str, Any]] = []
        self.last_fetch: float = 0
        self._scraper: Optional[bot.FeedScraper] = None

    @property
    def scraper(self) -> bot.FeedScraper:
        if self._scraper is None:
            self._scraper = bot.FeedScraper()
        return self._scraper

    def is_valid(self) -> bool:
        return bool(self.cached_xml and (time.time() - self.last_fetch) < self.ttl)

    def get_feed_xml(self, force_refresh: bool = False, enrich_scenes: bool = False) -> str:
        if not force_refresh and self.is_valid() and not enrich_scenes:
            return self.cached_xml

        logger.info("Cache miss or expired. Fetching fresh feed from Adult DVD Empire...")
        raw_feed = self.scraper.fetch_feed(bot.FEED_URL)
        items = self.scraper.parse_feed_items(raw_feed)

        if enrich_scenes:
            logger.info("Enriching scenes for top feed items...")
            for it in items[:10]:
                try:
                    self.scraper.enrich_product_details(it)
                except Exception as e:
                    logger.warning(f"Error enriching item {it.get('id')}: {e}")

        xml_output = self.build_clean_xml(items)

        if not enrich_scenes:
            self.cached_xml = xml_output
            self.cached_items = items
            self.last_fetch = time.time()
            logger.info(f"Cached clean XML feed ({len(items)} items, {len(xml_output)} bytes).")

        return xml_output

    def build_clean_xml(self, items: List[Dict[str, Any]]) -> str:
        """Constructs a strictly valid, standards-compliant RSS 2.0 / MRSS XML."""
        build_date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0"',
            '     xmlns:media="http://search.yahoo.com/mrss/"',
            '     xmlns:atom="http://www.w3.org/2005/Atom"',
            '     xmlns:dc="http://purl.org/dc/elements/1.1/">',
            '  <channel>',
            '    <title>Adult DVD Empire: New Releases (Clean Feed)</title>',
            '    <link>https://www.adultdvdempire.com</link>',
            '    <description>Clean, standards-compliant MRSS feed for Adult DVD Empire releases with high-definition media attachments.</description>',
            '    <language>en-us</language>',
            f'    <lastBuildDate>{build_date}</lastBuildDate>',
            '    <image>',
            '      <url>https://imgs1cdn.adultempire.com/res/pm/logo_ade07.gif</url>',
            '      <title>Adult DVD Empire</title>',
            '      <link>https://www.adultdvdempire.com</link>',
            '    </image>',
        ]

        for it in items:
            item_id = xml_escape(it.get("id", ""))
            title = xml_escape(bot.format_title(it.get("title", "New Release")))
            link = xml_escape(it.get("link", ""))
            pub_date = xml_escape(it.get("pub_date", ""))
            thumb_url = xml_escape(it.get("thumb_url") or "")
            hd_front = xml_escape(it.get("hd_front") or "")
            hd_back = xml_escape(it.get("hd_back") or "")
            studio = xml_escape(it.get("studio") or "")
            scenes = it.get("scenes", [])

            # Clean HTML description wrapped in CDATA
            desc_parts = []
            if hd_front:
                desc_parts.append(f'<p><img src="{hd_front}" alt="{title}" style="max-width:500px;"/></p>')
            if studio:
                desc_parts.append(f'<p><strong>Studio:</strong> {studio}</p>')
            if pub_date:
                desc_parts.append(f'<p><strong>Released:</strong> {pub_date}</p>')
            if scenes:
                desc_parts.append('<p><strong>Scenes:</strong></p><ul>')
                for idx, sc in enumerate(scenes, 1):
                    s_title = html.escape(sc.get("title", f"Scene {idx}"))
                    s_url = html.escape(sc.get("url", link))
                    if s_title.lower() in [f"scene {idx}".lower(), f"scene{idx}".lower(), "scene"]:
                        sc_label = f"Scene {idx}"
                    else:
                        sc_label = f"Scene {idx}: {s_title}"
                    desc_parts.append(f'<li><a href="{s_url}">{sc_label}</a></li>')
                desc_parts.append('</ul>')
            desc_html = "".join(desc_parts)

            lines.append('    <item>')
            lines.append(f'      <title>{title}</title>')
            lines.append(f'      <link>{link}</link>')
            lines.append(f'      <guid isPermaLink="false">{item_id}</guid>')
            if pub_date:
                lines.append(f'      <pubDate>{pub_date}</pubDate>')
            if studio:
                lines.append(f'      <dc:creator>{studio}</dc:creator>')

            lines.append(f'      <description><![CDATA[{desc_html}]]></description>')

            # Enclosure for podcast / RSS media aggregators
            if hd_front:
                lines.append(f'      <enclosure url="{hd_front}" length="0" type="image/jpeg" />')

            # Media RSS elements
            if thumb_url:
                lines.append(f'      <media:thumbnail url="{thumb_url}" />')
            if hd_front:
                lines.append(f'      <media:content url="{hd_front}" medium="image" type="image/jpeg">')
                lines.append('        <media:title>Front Cover (HD)</media:title>')
                lines.append('      </media:content>')
            if hd_back:
                lines.append(f'      <media:content url="{hd_back}" medium="image" type="image/jpeg">')
                lines.append('        <media:title>Back Cover (HD)</media:title>')
                lines.append('      </media:content>')

            # Add scene screenshots if available
            for sc in scenes:
                for cap in sc.get("caps", [])[:4]:
                    c_esc = xml_escape(cap)
                    lines.append(f'      <media:content url="{c_esc}" medium="image" type="image/jpeg" />')

            lines.append('    </item>')

        lines.append('  </channel>')
        lines.append('</rss>')
        return "\n".join(lines)


# Global cache instance
feed_cache = FeedCache(ttl_seconds=CACHE_TTL)


class CleanFeedRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler serving clean XML feeds and dashboard."""

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - - [{self.log_date_time_string()}] {format % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        params = parse_qs(parsed.query)
        refresh = "refresh" in params or "nocache" in params
        enrich = "enrich" in params

        if path in ["/clean.xml", "/rss.xml", "/feed.xml"]:
            try:
                xml_data = feed_cache.get_feed_xml(force_refresh=refresh, enrich_scenes=enrich)
                encoded = xml_data.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", f"public, max-age={CACHE_TTL}")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(encoded)
            except Exception as e:
                logger.error(f"Error generating clean XML: {e}")
                err_msg = f"<?xml version='1.0' encoding='UTF-8'?><error><message>{html.escape(str(e))}</message></error>"
                encoded = err_msg.encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        elif path == "/health":
            age = round(time.time() - feed_cache.last_fetch, 1) if feed_cache.last_fetch else None
            data = {
                "status": "ok",
                "feed_url": bot.FEED_URL,
                "cached_items": len(feed_cache.cached_items),
                "cache_age_seconds": age,
                "cache_ttl_seconds": feed_cache.ttl,
            }
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path in ["/poll", "/trigger", "/run"]:
            cron_secret = os.getenv("CRON_SECRET", "").strip()
            provided_secret = params.get("secret", [""])[0] or self.headers.get("X-Cron-Secret", "")
            if cron_secret and provided_secret != cron_secret:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": "Unauthorized"}).encode("utf-8"))
                return

            import types
            import threading

            poll_args = types.SimpleNamespace(
                dry_run="dry_run" in params,
                seed_only="seed_only" in params,
                limit=int(params.get("limit", [bot.MAX_POSTS_PER_RUN])[0]),
                loop=0,
            )

            def _async_poll():
                try:
                    logger.info("Triggered on-demand poll cycle via HTTP endpoint.")
                    bot.run_once(poll_args)
                except Exception as e:
                    logger.error(f"Error during HTTP-triggered poll: {e}")

            threading.Thread(target=_async_poll, daemon=True).start()

            resp_data = {"status": "ok", "message": "Poll cycle initiated successfully"}
            body = json.dumps(resp_data, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/":
            html_content = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Adult DVD Empire - Clean XML Feed Service</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 800px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #222; }}
        h1 {{ color: #0d47a1; margin-bottom: 8px; }}
        .badge {{ background: #2e7d32; color: #fff; padding: 3px 8px; border-radius: 4px; font-size: 14px; font-weight: bold; }}
        .box {{ background: #f5f5f5; border: 1px solid #ddd; border-radius: 8px; padding: 16px 20px; margin: 20px 0; }}
        code {{ background: #e0e0e0; padding: 2px 6px; border-radius: 4px; font-size: 15px; }}
        a {{ color: #1565c0; text-decoration: none; font-weight: bold; }}
        a:hover {{ text-decoration: underline; }}
        ul {{ padding-left: 20px; }}
        li {{ margin-bottom: 8px; }}
    </style>
</head>
<body>
    <h1>Clean XML Feed Service <span class="badge">ONLINE</span></h1>
    <p>Provides cleaned, valid, and enriched RSS 2.0 / MRSS feeds from Adult DVD Empire without age-verification blocks or broken XML tags.</p>

    <div class="box">
        <h3>Available Endpoints:</h3>
        <ul>
            <li><a href="/clean.xml"><code>/clean.xml</code></a> - Clean, valid RSS 2.0 / MRSS XML feed</li>
            <li><a href="/clean.xml?refresh=1"><code>/clean.xml?refresh=1</code></a> - Force bypass cache and fetch latest feed</li>
            <li><a href="/health"><code>/health</code></a> - Service health &amp; cache status JSON</li>
            <li><a href="/poll"><code>/poll</code></a> - Trigger immediate RSS-to-Telegram check (for cron-job.org / webhooks)</li>
        </ul>
    </div>

    <div class="box">
        <h3>Features:</h3>
        <ul>
            <li>✅ <strong>100% Valid XML</strong>: Fixes unescaped Vue tags, BOM encoding, and broken duplicate GUIDs.</li>
            <li>✅ <strong>Automated Handshake</strong>: Bypasses age confirmation barriers automatically.</li>
            <li>✅ <strong>HD Media Attachments</strong>: Includes high-resolution Front &amp; Back cover images and media thumbnails.</li>
            <li>✅ <strong>Fast In-Memory Cache</strong>: Responds in milliseconds with 5-minute smart caching.</li>
        </ul>
    </div>
</body>
</html>
"""
            encoded = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 Not Found")


def _start_background_bot_thread(interval: int):
    """Runs the RSS bot in a daemon thread periodically (ideal for Render Web Services)."""
    import threading
    import types

    def _worker():
        args = types.SimpleNamespace(
            dry_run=False,
            seed_only=False,
            limit=bot.MAX_POSTS_PER_RUN,
            loop=interval,
        )
        logger.info(f"Background Telegram Bot worker started (interval: {interval} seconds).")
        while True:
            try:
                bot.run_once(args)
            except Exception as e:
                logger.error(f"Error in background bot worker: {e}")
            time.sleep(interval)

    t = threading.Thread(target=_worker, daemon=True, name="RssBotWorker")
    t.start()


def run_server(port: int = PORT, host: str = HOST):
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, CleanFeedRequestHandler)
    logger.info(f"Clean XML Feed Server listening on http://{host}:{port}")
    logger.info(f"Clean XML endpoint available at: http://{host}:{port}/clean.xml")

    if os.getenv("RUN_BOT", "true").lower() in ("1", "true", "yes"):
        interval = int(os.getenv("POLL_INTERVAL_SECONDS", "1800"))
        _start_background_bot_thread(interval)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    finally:
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description="Clean XML Feed Server for Adult DVD Empire")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--host", type=str, default=HOST, help=f"Host address (default: {HOST})")
    args = parser.parse_args()

    run_server(port=args.port, host=args.host)


if __name__ == "__main__":
    main()
