import configparser
import html
import re
import signal
import sys
import time
import logging
import json
import os
import feedparser
import hashlib
from datetime import datetime, timezone
import calendar
from email.utils import parsedate_to_datetime
import threading

class FeedXL8Scanner:
    def __init__(self, config_file='feedxl8.conf'):
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
        self.running = True
        self.config_file = config_file
        self.feeds, self.settings = [], {}
        self._load_config()
        self.scan_interval = int(self.settings.get('scan_interval_minutes', 30)) * 60
        self.downloads_dir = self.settings.get('downloads_dir', '/opt/feedxl8/downloads')
        logging.getLogger().setLevel(getattr(logging, self.settings.get('log_level', 'INFO').upper(), logging.INFO))
        self.IMAGE_SRC_REGEX = re.compile(r'<[^>]*\bsrc\s*=\s*[\'"]([^\'"]+\.(?:jpg|jpeg|png|gif|webp))[\'"][^>]*>', re.IGNORECASE)
        logging.info("FeedXL8 Scanner initialized.")

    def handle_signal(self, signum, frame):
        logging.info(f"Received signal: {signum}")
        self.shutdown()

    def shutdown(self):
        logging.info("Shutting down...")
        self.running = False

    def _load_config(self):
        if not os.path.exists(self.config_file):
            logging.error(f"Config file not found: {self.config_file}")
            sys.exit(1)
        try:
            config = configparser.ConfigParser()
            config.read(self.config_file, encoding='utf-8')
            if 'settings' in config:
                self.settings = dict(config['settings'])
                config.remove_section('settings')
            self.feeds = [
                {'publisher': s, 'url': config[s]['url'], 'country': config[s]['country'],
                 'language': config[s]['language'], 'language_code': config[s]['language_code']}
                for s in config.sections()
            ]
            logging.info(f"Loaded {len(self.feeds)} feeds from {self.config_file}.")
        except Exception as e:
            logging.error(f"Config error: {e}")
            sys.exit(1)

    def _parse_published(self, entry):
        try:
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                dt = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
                return dt.isoformat()
            raw = entry.get('published', '')
            if not raw:
                return ""
            for parser in (parsedate_to_datetime, lambda s: datetime.fromisoformat(s.replace('Z', '+00:00'))):
                try:
                    dt = parser(raw)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.astimezone(timezone.utc).isoformat()
                except Exception:
                    pass
            return raw
        except Exception:
            return ""

    def _extract_image(self, entry):
        try:
            if hasattr(entry, 'media_content'):
                for c in entry.media_content:
                    if c.get('type', '').startswith('image/'):
                        return c['url']
            if hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
                return entry.media_thumbnail[0]['url']
            if hasattr(entry, 'description'):
                matches = self.IMAGE_SRC_REGEX.findall(html.unescape(entry.description))
                if matches:
                    return matches[0]
        except Exception:
            pass
        return ""

    def _clean_summary(self, entry):
        if 'description' not in entry:
            return ""
        return html.escape(re.sub(r'<[^>]+>', '', html.unescape(entry.description)))

    def _calculate_feedid(self, title, summary):
        return hashlib.sha256(f"{title}{summary}".encode('utf-8')).hexdigest()

    def _download_feed(self, feed):
        if not self.running:
            return
        logging.debug(f"{feed['publisher']} - scanning {feed['url']}")
        parsed = feedparser.parse(feed['url'])
        if parsed.bozo:
            if not parsed.entries:
                logging.error(f"Feed error (skipping) [{feed['publisher']}]: {parsed.bozo_exception}")
                return
            logging.warning(f"Feed parse warning (continuing) [{feed['publisher']}]: {parsed.bozo_exception}")

        dir_path = os.path.join(self.downloads_dir, feed['language'], feed['language_code'], feed['publisher'])
        os.makedirs(dir_path, exist_ok=True)
        new_items = existing_items = 0

        for entry in parsed.entries:
            if not self.running:
                return
            item_id = self._calculate_feedid(entry.get('title', ''), entry.get('summary', ''))
            item_filename = f"{dir_path}/{item_id}.json"
            if os.path.exists(item_filename):
                existing_items += 1
                continue
            item = {
                "title": entry.get('title', ''),
                "summary": self._clean_summary(entry),
                "link": entry.get('link', ''),
                "image_url": self._extract_image(entry),
                "published": self._parse_published(entry),
                "publisher": feed['publisher'],
                "url": feed['url'],
                "country": feed['country'],
                "language": feed['language'],
                "feedid": item_id
            }
            with open(item_filename, 'w', encoding='utf-8') as f:
                json.dump(item, f, ensure_ascii=False, indent=4)
            logging.debug(f"Saved: {item_filename}")
            new_items += 1

        logging.info(f"{feed['publisher']} - {new_items} new, {existing_items} existing from {feed['url']}")

    def _cleanup_old_files(self):
        retention_hours = int(self.settings.get('retention_hours', 24))
        cutoff = time.time() - retention_hours * 3600
        removed = 0
        for root, _, files in os.walk(self.downloads_dir):
            for fname in files:
                path = os.path.join(root, fname)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
        logging.info(f"Cleanup: removed {removed} files older than {retention_hours}h.")

    def run(self):
        logging.info("FeedXL8 Scanner starting...")
        self._cleanup_old_files()
        while self.running:
            logging.info(f"Scanning feeds. Next scan in {self.scan_interval}s.")
            for feed in self.feeds:
                if not self.running:
                    break
                threading.Thread(target=self._download_feed, args=(feed,), daemon=True).start()
            for _ in range(self.scan_interval):
                if not self.running:
                    break
                time.sleep(1)
        logging.info("FeedXL8 Scanner stopped.")

def main():
    scanner = FeedXL8Scanner()
    signal.signal(signal.SIGINT, scanner.handle_signal)
    signal.signal(signal.SIGTERM, scanner.handle_signal)
    try:
        scanner.run()
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
