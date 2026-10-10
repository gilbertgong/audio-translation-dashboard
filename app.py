"""Audio Translation Dashboard - backend server.

Usage: python app.py [--mock] [path/to/config.toml]

--mock runs without whisper-server or llama-server, for working on the page
itself: the dashboard opens with the first lines of a sample conversation, and
each recording "transcribes" to the next sample line after a short delay.

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import itertools
import json
import logging
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
LLM_TIMEOUT = 300  # seconds to wait for llama-server
MAX_TEXT_BYTES = 1024 * 1024  # largest /api/translate request body

# Shared log file and line format (same as scripts/common.py and launch.sh).
LOG_FILE = "logs/app.log"
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

log = logging.getLogger("app")

# Sample conversation for --mock mode. "language" is as whisper reports it;
# the audio length and timings are shown for the lines displayed when the page loads.
MOCK_CONVERSATION = [
    {"text": "Hi Li Wei, are we still meeting tomorrow afternoon?",
     "language": "english",
     "translation": "嗨，李伟，我们明天下午还见面吗？",
     "audio_seconds": 2.6, "stt_seconds": 0.41, "llm_seconds": 0.52},
    {"text": "是的，三点在二楼的会议室，别迟到。",
     "language": "chinese",
     "translation": "Yes, three o'clock in the second-floor meeting room. Don't be late.",
     "audio_seconds": 2.2, "stt_seconds": 0.38, "llm_seconds": 0.74},
    {"text": "Great, I'll bring the latest budget numbers so we can go through them together.",
     "language": "english",
     "translation": "太好了，我会带上最新的预算数据，我们可以一起过一遍。",
     "audio_seconds": 3.9, "stt_seconds": 0.46, "llm_seconds": 0.69},
    {"text": "好的，到时候我们先 review 一下 Q4 的计划，然后再讨论预算。",
     "language": "chinese",
     "translation": "Okay, then we'll first review the Q4 plan and discuss the budget after that.",
     "audio_seconds": 3.4, "stt_seconds": 0.44, "llm_seconds": 0.81},
]
MOCK_DELAY = 0.8  # seconds; long enough to see the in-progress states
MOCK_PRELOAD = 2  # lines shown when the page loads; recordings continue from there


def setup_logging(config_dir):
    """Log to logs/app.log (next to config.toml); warnings also go to the console."""
    path = Path(config_dir) / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])
    return path


def load_config(path):
    with open(path, "rb") as f:
        config = tomllib.load(f)
    config_dir = Path(path).resolve().parent
    config["_config_dir"] = config_dir
    # Resolve relative paths against the config file's directory.
    temp_dir = Path(config["audio"]["temp_dir"])
    if not temp_dir.is_absolute():
        temp_dir = config_dir / temp_dir
    config["audio"]["temp_dir"] = temp_dir
    whisper = config["api_servers"]["whisper"]
    config["whisper_url"] = f"http://{whisper['host']}:{whisper['port']}"
    llama = config["api_servers"]["llama"]
    config["llama_url"] = f"http://{llama['host']}:{llama['port']}"
    config["system_prompt"] = render_prompt(config["llama_prompt"])
    return config


def render_prompt(prompt):
    """Fill {language1} and {language2} into the [llama_prompt] system prompt."""
    text = prompt["system"]
    for key in ("language1", "language2"):
        text = text.replace("{" + key + "}", prompt[key])
    return text


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


def request_json(url, body, content_type, timeout):
    """POST body to url and return the parsed JSON response.

    HTTP errors are raised as RuntimeError including the server's error text.
    """
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace").strip()
        raise RuntimeError(f"HTTP {err.code}: {detail[:500]}") from err


def transcribe(whisper_url, wav, filename):
    """Send a WAV to whisper-server; return {"text", "language", "stt_raw"}.

    stt_raw is the request settings and whisper's full reply, for debugging.
    """
    fields = {"response_format": "verbose_json", "temperature": "0.0"}
    body, content_type = multipart(fields, {"file": (filename, "audio/wav", wav)})
    url = f"{whisper_url}/inference"
    result = request_json(url, body, content_type, STT_TIMEOUT)
    if "error" in result:
        raise RuntimeError(result["error"])
    raw = {"request": {"url": url, "fields": fields, "file": f"{filename} ({len(wav)} bytes)"},
           "response": result}
    return {"text": result["text"].strip(), "language": result.get("detected_language"),
            "stt_raw": raw}


def translate(config, text):
    """Translate text with llama-server's OpenAI-style chat endpoint.

    Returns (translation, raw), where raw is the full request and llama's full
    reply, for debugging.
    """
    prompt = config["llama_prompt"]
    body = {
        "messages": [
            {"role": "system", "content": config["system_prompt"]},
            {"role": "user", "content": text},
        ],
        "temperature": prompt.get("temperature", 0.2),
        # Passed to the model's chat template, which switches thinking on or off.
        "chat_template_kwargs": {"enable_thinking": prompt.get("enable_thinking", False)},
    }
    url = f"{config['llama_url']}/v1/chat/completions"
    result = request_json(url, json.dumps(body).encode("utf-8"), "application/json", LLM_TIMEOUT)
    content = result["choices"][0]["message"].get("content") or ""
    # llama-server normally moves thinking to a separate field; drop it if not.
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    return content.strip(), {"request": {"url": url, "body": body}, "response": result}


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

    def log_message(self, format, *args):
        # Per-request lines ("GET / HTTP/1.1" 200) are only logged at debug level.
        log.debug("%s %s", self.address_string(), format % args)

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
            prompt = self.config["llama_prompt"]
            payload = {"sample_rate": self.config["audio"]["sample_rate"],
                       "language1": prompt["language1"], "language2": prompt["language2"],
                       "mock": self.server.mock}
            if self.server.mock:
                # A page (re)load starts the sample conversation over.
                payload["mock_conversation"] = MOCK_CONVERSATION[:MOCK_PRELOAD]
                self.server.mock_counter = itertools.count(MOCK_PRELOAD)
            self.send_json(200, payload)
            return
        super().do_GET()

    def do_POST(self):
        routes = {
            "/api/recordings": self.post_recording,
            "/api/translate": self.post_translate,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self.send_json(404, {"error": "not found"})

    def post_translate(self):
        """Body: {"text": "..."}. Returns {"translation", "llm_seconds"}."""
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_TEXT_BYTES:
            self.send_json(400, {"error": "missing or oversized request body"})
            return
        try:
            text = json.loads(self.rfile.read(length))["text"].strip()
        except (ValueError, KeyError, TypeError, AttributeError):
            self.send_json(400, {"error": 'expected JSON {"text": "..."}'})
            return
        if not text:
            self.send_json(400, {"error": "no text to translate"})
            return

        if self.server.mock:
            time.sleep(MOCK_DELAY)
            known = {item["text"]: item["translation"] for item in MOCK_CONVERSATION}
            translation = known.get(text, f"[mock translation] {text}")
            self.send_json(200, {"translation": translation, "llm_seconds": MOCK_DELAY,
                                 "llm_raw": {"mock": True, "text": text, "translation": translation}})
            return

        start = time.monotonic()
        try:
            translation, raw = translate(self.config, text)
        except (OSError, RuntimeError, ValueError, KeyError, IndexError) as err:
            error = (f"translation failed: {err}"
                     f" (is llama-server running at {self.config['llama_url']}?)")
            log.warning("%s; text: %r", error, text)
            self.send_json(502, {"error": error})
            return
        seconds = round(time.monotonic() - start, 2)
        log.info("translated in %ss: %r -> %r", seconds, text, translation)
        self.send_json(200, {"translation": translation, "llm_seconds": seconds, "llm_raw": raw})

    def post_recording(self):
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

        if self.server.mock:
            # Don't save or transcribe; answer with the next sample line.
            time.sleep(MOCK_DELAY)
            item = MOCK_CONVERSATION[next(self.server.mock_counter) % len(MOCK_CONVERSATION)]
            self.send_json(201, {"file": "(mock mode: not saved)", "bytes": len(data),
                                 "text": item["text"], "language": item["language"],
                                 "stt_seconds": MOCK_DELAY,
                                 "stt_raw": {"mock": True, "text": item["text"],
                                             "language": item["language"]}})
            return

        temp_dir = self.config["audio"]["temp_dir"]
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / datetime.now().strftime("recording_%Y%m%d_%H%M%S_%f.wav")
        path.write_bytes(data)
        log.info("saved recording %s (%d bytes)", path, len(data))
        response = {"file": str(path), "bytes": len(data)}

        start = time.monotonic()
        try:
            response.update(transcribe(self.config["whisper_url"], data, path.name))
        except (OSError, RuntimeError, ValueError) as err:
            # OSError: connection problems; RuntimeError: errors from whisper-server.
            response["error"] = f"transcription failed: {err} (is whisper-server running at {self.config['whisper_url']}?)"
            log.warning("%s; recording: %s", response["error"], path.name)
            self.send_json(502, response)
            return
        response["stt_seconds"] = round(time.monotonic() - start, 2)
        log.info("transcribed %s in %ss (%s): %r", path.name, response["stt_seconds"],
                 response["language"], response["text"])
        self.send_json(201, response)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG, type=Path,
                        help="config file (default: config.toml next to app.py)")
    parser.add_argument("--mock", action="store_true",
                        help="sample data instead of whisper/llama, for styling the page")
    args = parser.parse_args()

    config_path = args.config
    config = load_config(config_path)
    log_path = setup_logging(config["_config_dir"])
    host = config["server"]["host"]
    port = config["server"]["port"]

    server = ThreadingHTTPServer((host, port), Handler)
    server.config = config
    server.mock = args.mock
    server.mock_counter = itertools.count(MOCK_PRELOAD)

    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    banner = [
        *(["Mode:       MOCK (sample data; whisper and llama are not used)"] if args.mock else []),
        f"Config:     {config_path.resolve()}",
        f"Log:        {log_path}",
        f"Recordings: {config['audio']['temp_dir']}",
        f"Whisper:    {config['whisper_url']}",
        f"Llama:      {config['llama_url']}",
        f"Languages:  {config['llama_prompt']['language1']} <-> {config['llama_prompt']['language2']}",
        f"Dashboard:  http://{shown_host}:{port}/",
    ]
    print("\n".join(banner) + "  (Ctrl+C to stop)")
    log.info("started\n  " + "\n  ".join(banner))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log.info("stopped")


if __name__ == "__main__":
    main()
