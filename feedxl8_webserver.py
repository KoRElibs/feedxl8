import os
import sys
import ssl
import time
import signal
import hashlib
import logging
import threading
import configparser
import urllib.request
import urllib.error
import urllib.parse
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

PROXY_PREFIX     = '/meili'
IMG_PROXY_PREFIX = '/imgproxy'


class FeedXL8Handler(SimpleHTTPRequestHandler):
    """Static file handler with server-side proxies for Meilisearch and image caching.

    /meili/*            — forwards to Meilisearch with the API key injected server-side.
    /imgproxy?url=<url> — fetches and caches remote images so the browser never
                          contacts external hosts directly.
    """

    # Populated by FeedXL8Webserver.run() before the server starts.
    _meili_url       = ''
    _meili_headers   = {}
    _image_cache_dir = ''
    _img_locks       = {}
    _img_locks_lock  = threading.Lock()

    def _proxy(self, method):
        target = self._meili_url + self.path[len(PROXY_PREFIX):]
        length = int(self.headers.get('Content-Length') or 0)
        body   = self.rfile.read(length) if length > 0 else None
        req = urllib.request.Request(
            target, data=body, method=method, headers=self._meili_headers
        )
        try:
            with urllib.request.urlopen(req) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as e:
            logging.error("Proxy error forwarding %s %s: %s", method, self.path, e.reason)
            self.send_error(502, "Bad Gateway")

    def _img_proxy(self):
        qs   = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        urls = qs.get('url', [])
        if not urls:
            self.send_error(400, "Missing url parameter")
            return
        url = urls[0]
        if urllib.parse.urlparse(url).scheme not in ('http', 'https'):
            self.send_error(400, "Invalid URL scheme")
            return

        cache_key     = hashlib.md5(url.encode()).hexdigest()
        cache_dir     = self.__class__._image_cache_dir
        cache_file    = os.path.join(cache_dir, cache_key)
        cache_ct_file = cache_file + '.ct'

        # Per-URL lock prevents duplicate fetches under concurrent requests.
        with self.__class__._img_locks_lock:
            if cache_key not in self.__class__._img_locks:
                self.__class__._img_locks[cache_key] = threading.Lock()
            lock = self.__class__._img_locks[cache_key]

        with lock:
            if os.path.exists(cache_file) and os.path.exists(cache_ct_file):
                with open(cache_ct_file) as f:
                    ct = f.read().strip()
                with open(cache_file, 'rb') as f:
                    data = f.read()
            else:
                try:
                    req = urllib.request.Request(url, headers={'User-Agent': 'FeedXL8/1.0'})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = resp.read()
                        ct   = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip()
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(cache_file, 'wb') as f:
                        f.write(data)
                    with open(cache_ct_file, 'w') as f:
                        f.write(ct)
                except Exception as e:
                    logging.warning("imgproxy fetch failed for %s: %s", url, e)
                    self.send_error(502, "Failed to fetch image")
                    return

        self.send_response(200)
        self.send_header('Content-Type', ct)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'public, max-age=86400')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == PROXY_PREFIX or self.path.startswith(PROXY_PREFIX + '/'):
            self._proxy('GET')
        elif self.path.startswith(IMG_PROXY_PREFIX + '?') or self.path == IMG_PROXY_PREFIX:
            self._img_proxy()
        elif self.path == '/favicon.ico':
            self.send_response(404)
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == PROXY_PREFIX or self.path.startswith(PROXY_PREFIX + '/'):
            self._proxy('POST')
        else:
            self.send_error(405)

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt, *args):
        logging.warning("%s - %s", self.address_string(), fmt % args)


class FeedXL8Webserver:
    def __init__(self, config_file='feedxl8.conf'):
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
        self.running = True
        self.config_file = config_file
        self._load_config()
        logging.info("FeedXL8 Webserver initialized.")

    def handle_signal(self, signum, _frame):
        logging.info(f"Received signal: {signum}")
        self.shutdown()

    def shutdown(self):
        logging.info("Shutting down...")
        self.running = False
        if hasattr(self, '_server'):
            self._server.shutdown()

    def _load_config(self):
        if not os.path.exists(self.config_file):
            logging.error(f"Config file not found: {self.config_file}")
            sys.exit(1)
        try:
            config = configparser.ConfigParser()
            config.read(self.config_file, encoding='utf-8')
            s = config['settings']

            def resolve_dir(key, default_name):
                val = s.get(key, '').strip()
                if val:
                    return val
                data_dir = s.get('data_dir', '').strip()
                return os.path.join(data_dir, default_name) if data_dir else default_name

            self.host            = s.get('web_host', '127.0.0.1')
            self.port            = int(s.get('web_port', '8080'))
            self.tls             = s.getboolean('web_tls', False)
            self.cert            = s.get('web_tls_cert', '/etc/letsencrypt/live/example.com/fullchain.pem')
            self.key             = s.get('web_tls_key',  '/etc/letsencrypt/live/example.com/privkey.pem')
            self.meili_url       = s.get('meili_url', 'http://localhost:7700').rstrip('/')
            self.meili_api_key   = s.get('meili_api_key', '')
            self.retention_hours = int(s.get('retention_hours', 24))
            self.image_cache_dir = resolve_dir('image_cache_dir', 'imgcache')

            logging.getLogger().setLevel(getattr(logging, s.get('log_level', 'INFO').upper(), logging.INFO))
            logging.info(f"Config loaded: proxy → {self.meili_url}, image cache → {self.image_cache_dir}")
        except Exception as e:
            logging.error(f"Config error: {e}")
            sys.exit(1)

    def _cleanup_image_cache(self):
        if not os.path.isdir(self.image_cache_dir):
            return
        cutoff = time.time() - self.retention_hours * 3600
        removed = 0
        for fname in os.listdir(self.image_cache_dir):
            path = os.path.join(self.image_cache_dir, fname)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        if removed:
            logging.info(f"Image cache cleanup: removed {removed} files older than {self.retention_hours}h.")

    def run(self):
        self._cleanup_image_cache()

        FeedXL8Handler._meili_url = self.meili_url
        FeedXL8Handler._meili_headers = {
            'Authorization': f'Bearer {self.meili_api_key}',
            'Content-Type':  'application/json',
        }
        FeedXL8Handler._image_cache_dir = self.image_cache_dir

        serve_dir = os.path.dirname(os.path.abspath(__file__))
        handler = partial(FeedXL8Handler, directory=serve_dir)
        self._server = HTTPServer((self.host, self.port), handler)

        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.cert, self.key)
            self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
            scheme = 'https'
        else:
            scheme = 'http'

        logging.info(f"Serving on {scheme}://{self.host}:{self.port}/")
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        try:
            while self.running:
                t.join(timeout=1)
        except KeyboardInterrupt:
            self.shutdown()
        t.join()
        self._server.server_close()
        logging.info("FeedXL8 Webserver stopped.")


def main():
    server = FeedXL8Webserver()
    signal.signal(signal.SIGINT,  server.handle_signal)
    signal.signal(signal.SIGTERM, server.handle_signal)
    server.run()


if __name__ == '__main__':
    main()
