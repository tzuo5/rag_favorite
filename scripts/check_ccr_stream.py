"""Isolated CCR regression: upstream body reset must close the downstream promptly."""

import http.client
import importlib.util
import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("ccrcheck", "scripts/ccr_reliability.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", "9999")
        self.end_headers()
        self.wfile.write(
            b'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":true}"}\n\n'
        )
        self.wfile.flush()
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()


s = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=s.serve_forever, daemon=True).start()
try:
    with m.isolated_ccr(
        m.stub_config(f"http://127.0.0.1:{s.server_port}"), "retry"
    ) as (url, key):
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {"model": "gpt-6-luna", "input": "test", "stream": True}
            ).encode(),
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
        )
        start = time.monotonic()
        lines = []
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                for line in response:
                    lines.append(line.decode().strip())
            error = "EOF"
        except (OSError, http.client.HTTPException) as exc:
            error = type(exc).__name__
        print(
            json.dumps(
                {
                    "seconds": round(time.monotonic() - start, 3),
                    "result": error,
                    "events": lines,
                }
            )
        )
        assert time.monotonic() - start < 2, (
            "upstream reset did not terminate downstream promptly"
        )
finally:
    s.shutdown()
    s.server_close()
