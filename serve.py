"""
Локальный сервер для дашборда WB (режим CSV).

Раздаёт файлы из текущей папки на http://localhost:8765.
Запуск:  python3 serve.py

Раньше сервер ещё проксировал запросы к WB API и отдавал токен из .env —
эта механика удалена вместе с API-режимом дашборда. Теперь сервер только
раздатчик страницы, никаких ключей у него нет.
"""

import http.server
import os
import socketserver
from pathlib import Path

PORT = int(os.environ.get("WB_PORT", "8765"))

# CSP: наш JS работает inline (unsafe-inline), внешних скриптов нет.
# img-src data: — фавиконка-эмодзи встроена как data:URI.
# no-store — Safari не должен держать старую копию страницы.
CSP_HEADER = (
    "default-src 'self' 'unsafe-inline'; "  # noqa: E501
    "img-src 'self' data:"  # noqa: E501
)


class WBHandler(http.server.SimpleHTTPRequestHandler):

    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        try:
            super().log_message(fmt, *args)
        except Exception:
            pass


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.chdir(Path(__file__).parent.resolve())
    print(f"WB dashboard: http://localhost:{PORT}/index.html")
    print("Режим: только CSV. Ключей и API-прокси в сервере больше нет.")
    with ThreadedServer(("127.0.0.1", PORT), WBHandler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


if __name__ == "__main__":
    main()
