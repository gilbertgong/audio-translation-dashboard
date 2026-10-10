"""Audio Translation Dashboard - backend server.

Usage: python app.py [path/to/config.toml]

Standard library only (Python 3.11+ for tomllib).
"""

import json
import sys
import tomllib
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_CONFIG = ROOT / "config.toml"


def load_config(path):
    with open(path, "rb") as f:
        config = tomllib.load(f)
    # Resolve relative paths against the config file's directory.
    temp_dir = Path(config["audio"]["temp_dir"])
    if not temp_dir.is_absolute():
        temp_dir = Path(path).resolve().parent / temp_dir
    config["audio"]["temp_dir"] = temp_dir
    return config


class Handler(SimpleHTTPRequestHandler):
    # Windows' registry can map .js to text/plain, which breaks module loading.
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".css": "text/css",
        ".html": "text/html",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    @property
    def config(self):
        return self.server.config

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/config":
            # Only the settings the browser needs.
            self.send_json(200, {"sample_rate": self.config["audio"]["sample_rate"]})
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/recordings":
            self.send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        max_bytes = self.config["audio"]["max_upload_mb"] * 1024 * 1024
        if length <= 0:
            self.send_json(400, {"error": "empty upload"})
            return
        if length > max_bytes:
            self.send_json(413, {"error": "recording too large"})
            return

        data = self.rfile.read(length)
        if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            self.send_json(400, {"error": "not a WAV file"})
            return

        temp_dir = self.config["audio"]["temp_dir"]
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / datetime.now().strftime("recording_%Y%m%d_%H%M%S_%f.wav")
        path.write_bytes(data)

        self.send_json(201, {"file": str(path), "bytes": len(data)})


def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    config = load_config(config_path)
    host = config["server"]["host"]
    port = config["server"]["port"]

    server = ThreadingHTTPServer((host, port), Handler)
    server.config = config

    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    print(f"Config:     {config_path.resolve()}")
    print(f"Recordings: {config['audio']['temp_dir']}")
    print(f"Dashboard:  http://{shown_host}:{port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
