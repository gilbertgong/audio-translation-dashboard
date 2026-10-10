"""Audio Translation Dashboard - backend server.

Usage: python app.py [path/to/config.toml]

Standard library only (Python 3.11+ for tomllib).
"""

import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_CONFIG = ROOT / "config.toml"
STT_TIMEOUT = 300  # seconds to wait for whisper-server


def load_config(path):
    with open(path, "rb") as f:
        config = tomllib.load(f)
    # Resolve relative paths against the config file's directory.
    temp_dir = Path(config["audio"]["temp_dir"])
    if not temp_dir.is_absolute():
        temp_dir = Path(path).resolve().parent / temp_dir
    config["audio"]["temp_dir"] = temp_dir
    whisper = config["api_servers"]["whisper"]
    config["whisper_url"] = f"http://{whisper['host']}:{whisper['port']}"
    return config


def multipart(fields, files):
    """Encode a multipart/form-data body. files: {name: (filename, content_type, bytes)}."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n".encode())
    for name, (filename, content_type, data) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                     f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
                     + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def transcribe(whisper_url, wav, filename):
    """Send a WAV to whisper-server; return {"text", "language"}."""
    body, content_type = multipart(
        {"response_format": "verbose_json", "temperature": "0.0"},
        {"file": (filename, "audio/wav", wav)},
    )
    req = urllib.request.Request(f"{whisper_url}/inference", data=body,
                                 headers={"Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=STT_TIMEOUT) as resp:
        result = json.load(resp)
    if "error" in result:
        raise RuntimeError(result["error"])
    return {"text": result["text"].strip(), "language": result.get("detected_language")}


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
        response = {"file": str(path), "bytes": len(data)}

        start = time.monotonic()
        try:
            response.update(transcribe(self.config["whisper_url"], data, path.name))
        except (OSError, RuntimeError, ValueError) as err:
            # OSError covers connection errors and HTTP errors from urllib.
            response["error"] = f"transcription failed: {err} (is whisper-server running at {self.config['whisper_url']}?)"
            self.send_json(502, response)
            return
        response["stt_seconds"] = round(time.monotonic() - start, 2)
        self.send_json(201, response)


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
    print(f"Whisper:    {config['whisper_url']}")
    print(f"Dashboard:  http://{shown_host}:{port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
