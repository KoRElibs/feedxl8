import os
import sys
import json
import time
import shutil
import signal
import logging
import configparser
import requests

class FeedXL8Publisher:
    def __init__(self, config_file='feedxl8.conf'):
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
        self.running = True
        self.config_file = config_file
        self._load_config()
        logging.info("FeedXL8 Publisher initialized.")

    def handle_signal(self, signum, _frame):
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
            self.meili_url = s.get('meili_url', 'http://localhost:7700')
            self.meili_index = s.get('meili_index', 'news')
            self.max_meili_batch_size = int(s.get('max_meili_batch_size', 5000000))
            self.publish_interval = int(s.get('publish_interval_minutes', 5)) * 60
            self.retention_hours = int(s.get('retention_hours', 24))
            def resolve_dir(key, default_name):
                val = s.get(key, '').strip()
                if val:
                    return val
                data_dir = s.get('data_dir', '').strip()
                return os.path.join(data_dir, default_name) if data_dir else default_name

            target_lang    = s['target_language'].strip()
            target_code    = s['target_language_code'].strip()
            translated_dir = resolve_dir('translated_dir', 'translated')
            published_dir  = resolve_dir('published_dir',  'published')
            self.doc_dir = os.path.join(translated_dir, target_lang, target_code)
            self.published_doc_dir = os.path.join(published_dir, target_lang, target_code)
            self.published_dir = published_dir
            self.headers = {
                "Authorization": f"Bearer {s.get('meili_api_key', '')}",
                "Content-Type": "application/json",
            }
            logging.getLogger().setLevel(getattr(logging, s.get('log_level', 'INFO').upper(), logging.INFO))
            logging.info(f"Config loaded: index={self.meili_index}, doc_dir={self.doc_dir}")
        except Exception as e:
            logging.error(f"Config error: {e}")
            sys.exit(1)

    def _cleanup_old_files(self):
        cutoff = time.time() - self.retention_hours * 3600
        removed = 0
        for root, _, files in os.walk(self.published_dir):
            for fname in files:
                path = os.path.join(root, fname)
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
        logging.info(f"Cleanup: removed {removed} published files older than {self.retention_hours}h.")

    def _wait_task(self, task_uid, timeout_s=60):
        if not task_uid:
            return None
        end = time.time() + timeout_s
        while time.time() < end:
            r = requests.get(f"{self.meili_url}/tasks/{task_uid}", headers=self.headers)
            if r.status_code == 200:
                j = r.json()
                st = j.get("status") or (j.get("task") or {}).get("status")
                if st in ("succeeded", "failed"):
                    return j
            time.sleep(0.5)
        raise TimeoutError(f"Task {task_uid} did not finish within {timeout_s}s")

    def _ensure_index(self):
        r = requests.get(f"{self.meili_url}/indexes/{self.meili_index}", headers=self.headers)
        if r.status_code != 200:
            r = requests.post(f"{self.meili_url}/indexes", headers=self.headers,
                              json={"uid": self.meili_index, "primaryKey": "feedid"})
            r.raise_for_status()
            logging.info(f"Created index '{self.meili_index}'.")
        settings = {
            "searchableAttributes": ["title", "summary", "publisher"],
            "displayedAttributes": ["title", "summary", "link", "url", "image_url",
                                    "published", "publisher", "country", "language", "feedid"],
            "filterableAttributes": ["publisher", "country", "language", "published"],
            "sortableAttributes": ["published"]
        }
        r = requests.patch(f"{self.meili_url}/indexes/{self.meili_index}/settings",
                           headers=self.headers, json=settings)
        r.raise_for_status()
        body = r.json()
        task_uid = body.get("taskUid") or body.get("uid")
        if task_uid:
            self._wait_task(task_uid)
        logging.info(f"Index '{self.meili_index}' settings applied.")

    def _publish(self):
        if not os.path.isdir(self.doc_dir):
            logging.warning(f"Doc dir not found: {self.doc_dir}")
            return
        to_upload = []  # list of (doc, filepath)
        for root, _, filenames in os.walk(self.doc_dir):
            for fn in filenames:
                if not fn.lower().endswith(".json"):
                    continue
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, self.doc_dir)
                if os.path.exists(os.path.join(self.published_doc_dir, rel)):
                    continue  # already published
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        d = json.load(fh)
                    if not d.get("feedid"):
                        logging.debug(f"Skipping (no feedid): {p}")
                    else:
                        to_upload.append((d, p))
                except json.JSONDecodeError:
                    logging.warning(f"Skipping (invalid JSON): {p}")
                except Exception as e:
                    logging.warning(f"Skipping (error): {p} — {e}")

        total = len(to_upload)
        if not total:
            logging.info("Nothing to publish.")
            return
        batches, current, current_bytes = [], [], 0
        for item in to_upload:
            doc_bytes = len(json.dumps(item[0], ensure_ascii=False).encode('utf-8'))
            if current and current_bytes + doc_bytes > self.max_meili_batch_size:
                batches.append(current)
                current, current_bytes = [], 0
            current.append(item)
            current_bytes += doc_bytes
        if current:
            batches.append(current)
        total_batches = len(batches)
        logging.info(f"Publishing {total} documents in {total_batches} batches (max {self.max_meili_batch_size} bytes each)...")
        moved, batch_no = 0, 0
        for batch in batches:
            if not self.running:
                break
            batch_no += 1
            docs = [d for d, _ in batch]
            paths = [p for _, p in batch]
            try:
                r = requests.post(f"{self.meili_url}/indexes/{self.meili_index}/documents",
                                  headers=self.headers, json=docs)
                r.raise_for_status()
                body = r.json()
                task_uid = body.get("taskUid") or body.get("uid")
                logging.info(f"Batch {batch_no}/{total_batches}: {len(docs)} docs submitted (task {task_uid})")
                result = self._wait_task(task_uid)
                status = result.get("status") if result else "unknown"
                logging.info(f"Batch {batch_no}: task {task_uid} → {status}")
                if status == "succeeded":
                    for p in paths:
                        rel = os.path.relpath(p, self.doc_dir)
                        dst = os.path.join(self.published_doc_dir, rel)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(p, dst)
                    moved += len(paths)
            except Exception as e:
                logging.error(f"Batch {batch_no}: failed — {e}")

        logging.info(f"Publish complete: {moved}/{total} documents indexed and copied to published.")

    def run(self):
        logging.info("FeedXL8 Publisher starting...")
        try:
            self._ensure_index()
        except Exception as e:
            logging.error(f"Failed to ensure index: {e}")
            sys.exit(1)
        self._cleanup_old_files()
        next_run = time.time()
        while self.running:
            wait = next_run - time.time()
            if wait > 0:
                logging.info(f"Waiting {wait:.1f}s until next publish...")
                while wait > 0 and self.running:
                    time.sleep(min(1, wait))
                    wait -= 1
            else:
                logging.info(f"Next publish is due (overdue by {-wait:.1f}s)")
            if not self.running:
                break
            try:
                self._publish()
            except Exception as e:
                logging.exception(f"Publish failed: {e}")
            logging.info("Publish scan completed.")
            next_run = time.time() + self.publish_interval
        logging.info("FeedXL8 Publisher stopped.")

def main():
    publisher = FeedXL8Publisher()
    signal.signal(signal.SIGINT, publisher.handle_signal)
    signal.signal(signal.SIGTERM, publisher.handle_signal)
    publisher.run()

if __name__ == "__main__":
    main()
