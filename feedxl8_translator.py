import signal
import sys
import time
import logging
import json
import os
from pathlib import Path
import feedxl8_ollama
import re
import configparser
import threading

sys.stdout.reconfigure(encoding="utf-8")

class FeedXL8Translator:
    def __init__(self, config_file='feedxl8.conf'):
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
        self.running = True
        self.config_file = config_file
        self.target_languages = []
        self._load_config()
        self.feed_translate = feedxl8_ollama.FeedXL8OllamaClient(self.config_file)
        logging.info("FeedXL8 Translator initialized.")

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
            s = config['settings']
            self.target_languages = [{'language': s['target_language'], 'language_code': s['target_language_code']}]
            self.scan_interval = int(s.get('translate_interval_minutes', 15)) * 60
            self.translate_timeout = int(s.get('translate_timeout_seconds', 30))
            self.max_translate_batch_size = int(s.get('max_translate_batch_size', 4000))
            self.max_feed_summary_size = int(s.get('max_feed_summary_size', 400))
            self.retention_hours = int(s.get('retention_hours', 24))
            self.downloads_dir = s.get('downloads_dir', '/opt/feedxl8/downloads')
            self.translated_dir = s.get('translated_dir', '/opt/feedxl8/translated')
            self.published_dir = s.get('published_dir', '/opt/feedxl8/published')
            logging.getLogger().setLevel(getattr(logging, s.get('log_level', 'INFO').upper(), logging.INFO))
            logging.info(f"Config loaded: target={self.target_languages}")
        except Exception as e:
            logging.error(f"Config error: {e}")
            sys.exit(1)

    def _cleanup_old_files(self):
        cutoff = time.time() - self.retention_hours * 3600
        removed = 0
        for root, _, files in os.walk(self.translated_dir):
            for fname in files:
                path = os.path.join(root, fname)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
        logging.info(f"Cleanup: removed {removed} translated files older than {self.retention_hours}h.")

    def _crop_feed_summary(self, text):
        n = self.max_feed_summary_size
        if n <= 0:
            return ""
        if len(text) <= n:
            return text
        return "..."[:n] if n < 3 else text[:n - 3] + "..."

    def _translate_with_timeout(self, src_lang, src_code, tgt_lang, tgt_code, text_batch):
        result = [None]
        def _translate():
            try:
                result[0] = self.feed_translate.translate_text(src_lang, src_code, tgt_lang, tgt_code, text_batch)
            except Exception as e:
                logging.error(f"Translation error: {e}")
        t = threading.Thread(target=_translate, daemon=False)
        t.start()
        t.join(timeout=self.translate_timeout)
        if t.is_alive():
            logging.error(f"Translation timed out after {self.translate_timeout}s")
            return None
        return result[0]

    def _translate_lang(self, target_language, target_language_code):
        if not self.running:
            return
        logging.info(f"Translating to {target_language} ({target_language_code})")
        ROOT = Path(self.downloads_dir)
        groups = {}
        for json_path in ROOT.rglob("*.json"):
            parts = json_path.relative_to(ROOT).parts
            if len(parts) < 4:
                continue
            src_lang, src_code, publisher, filename = parts[0], parts[1], parts[2], parts[3]
            target_file = Path(self.translated_dir) / target_language / target_language_code / publisher / filename
            published_file = Path(self.published_dir) / target_language / target_language_code / publisher / filename
            if not target_file.exists() and not published_file.exists():
                groups.setdefault((src_lang, src_code), []).append((json_path, publisher))

        for (src_lang, src_code), items in sorted(groups.items()):
            if not self.running:
                return
            total = len(items)
            logging.info(f"Processing {total} files for {src_lang} ({src_code})")
            idx = batch_no = 0
            while idx < total and self.running:
                text_batch, batch = "", []
                while idx < total and self.running:
                    json_path, publisher = items[idx]
                    try:
                        item = json.loads(json_path.read_text(encoding="utf-8"))
                        title = re.sub(r'\s*\r?\n\s*', ' ', item.get("title", ""))
                        summary = self._crop_feed_summary(re.sub(r'\s*\r?\n\s*', ' ', item.get("summary", "")))
                        sep = f"||PARA_{len(batch) + 1}||"
                        candidate = ("" if not batch else "\n") + f"{sep}\n{title}\n||S||\n{summary}"
                        if batch and len((text_batch + candidate).encode('utf-8')) > self.max_translate_batch_size:
                            break
                        text_batch += candidate
                        batch.append((json_path, publisher))
                        idx += 1
                    except Exception as e:
                        logging.error(f"Failed to load {json_path}: {e}")
                        idx += 1

                if not self.running or not batch:
                    break
                batch_no += 1
                logging.info(f"Batch {batch_no}: {len(batch)} files ({idx - len(batch) + 1}-{idx} of {total})")
                headed_batch = f"[{len(batch)} items to translate]\n{text_batch}"
                translated = self._translate_with_timeout(src_lang, src_code, target_language, target_language_code, headed_batch)
                if translated is None:
                    logging.warning(f"Batch {batch_no}: timed out, skipping")
                    continue
                result_map = {
                    int(m.group(1)): m.group(2).strip(" \n\r-")
                    for m in re.finditer(r'\|\|PARA_(\d+)\|\|\s*(.*?)(?=\|\|PARA_\d+\|\||$)', translated, re.DOTALL)
                }
                if not result_map:
                    logging.warning(f"Batch {batch_no}: no parseable items in response, skipping")
                    continue
                missing = [i for i in range(1, len(batch) + 1) if i not in result_map]
                if missing:
                    logging.warning(f"Batch {batch_no}: missing items {missing}, will retry next scan")
                for i, (json_path, publisher) in enumerate(batch, 1):
                    if i not in result_map:
                        continue
                    text = result_map[i]
                    try:
                        item = json.loads(json_path.read_text(encoding="utf-8"))
                        if "||S||" in text:
                            parts = text.split("||S||", 1)
                            item["title"] = parts[0].strip()
                            item["summary"] = parts[1].strip()
                        else:
                            item["title"] = text.split("\n")[0] if "\n" in text else text
                            item["summary"] = "\n".join(text.split("\n")[1:]) if "\n" in text else ""
                        target_dir = Path(self.translated_dir) / target_language / target_language_code / publisher
                        target_dir.mkdir(parents=True, exist_ok=True)
                        (target_dir / json_path.name).write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                        logging.debug(f"Saved: {target_dir / json_path.name}")
                    except Exception as e:
                        logging.error(f"Failed to save {json_path}: {e}")

    def run(self):
        logging.info("FeedXL8 Translator starting...")
        self._cleanup_old_files()
        next_run = time.time()
        while self.running:
            wait = next_run - time.time()
            if wait > 0:
                logging.info(f"Waiting {wait:.1f}s until next scan...")
                while wait > 0 and self.running:
                    time.sleep(min(1, wait))
                    wait -= 1
            else:
                logging.info(f"Next scan is due (overdue by {-wait:.1f}s)")
            if not self.running:
                break
            for lang in self.target_languages:
                if not self.running:
                    break
                try:
                    self._translate_lang(lang['language'], lang['language_code'])
                except Exception as e:
                    logging.exception(f"Translation failed for {lang['language']}: {e}")
            logging.info("Translation scan completed.")
            next_run = time.time() + self.scan_interval
        logging.info("FeedXL8 Translator stopped.")

def main():
    scanner = FeedXL8Translator()
    signal.signal(signal.SIGINT, scanner.handle_signal)
    signal.signal(signal.SIGTERM, scanner.handle_signal)
    scanner.run()

if __name__ == "__main__":
    main()
