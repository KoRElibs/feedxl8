import os
import sys
import ssl
import signal
import logging
import threading
import configparser
import urllib.request
import urllib.error
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

PROXY_PREFIX = '/meili'


class FeedXL8Handler(SimpleHTTPRequestHandler):
    """Static file handler with a server-side Meilisearch proxy.

    All requests to /meili/* are forwarded to the configured Meilisearch
    instance with the API key injected server-side — the browser never sees
    the key or the Meilisearch URL.

    Future proxy routes (e.g. /api/*) can be added here as additional
    do_GET / do_POST branches without restructuring anything.
    """

    # Populated by FeedXL8Webserver.run() before the server starts.
    _meili_url     = ''
    _meili_headers = {}

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

    def do_GET(self):
        if self.path == PROXY_PREFIX or self.path.startswith(PROXY_PREFIX + '/'):
            self._proxy('GET')
        elif self.path == '/favicon.ico':
            self.send_response(404)
            self.end_headers()  # skip send_error to avoid noisy log
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
            self.host          = s.get('web_host', '127.0.0.1')
            self.port          = int(s.get('web_port', '8080'))
            self.tls           = s.getboolean('web_tls', False)
            self.cert          = s.get('web_tls_cert', '/etc/letsencrypt/live/example.com/fullchain.pem')
            self.key           = s.get('web_tls_key',  '/etc/letsencrypt/live/example.com/privkey.pem')
            self.meili_url     = s.get('meili_url', 'http://localhost:7700').rstrip('/')
            self.meili_api_key = s.get('meili_api_key', '')
            logging.getLogger().setLevel(getattr(logging, s.get('log_level', 'INFO').upper(), logging.INFO))
            logging.info(f"Config loaded: proxy → {self.meili_url}")
        except Exception as e:
            logging.error(f"Config error: {e}")
            sys.exit(1)

    def run(self):
        # Inject Meilisearch connection details into the handler class.
        FeedXL8Handler._meili_url = self.meili_url
        FeedXL8Handler._meili_headers = {
            'Authorization': f'Bearer {self.meili_api_key}',
            'Content-Type':  'application/json',
        }

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
