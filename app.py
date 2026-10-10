"""Audio Translation Dashboard - backend server.

Usage: python app.py [--mock] [path/to/config.toml]

--mock runs without whisper-server or llama-server, for working on the page
itself: the dashboard opens with the first lines of a sample conversation, and
each recording "transcribes" to the next sample line after a short delay.

Standard library only (Python 3.11+ for tomllib); the web plumbing is in
microweb.py.
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
from pathlib import Path

from microweb import App, HTTPError

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_CONFIG = ROOT / "config.toml"
STT_TIMEOUT = 300  # seconds to wait for whisper-server
LLM_TIMEOUT = 300  # seconds to wait for llama-server

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


def call_server(url, body, content_type, timeout):
    """Call whisper-server or llama-server: POST body to url and return the
    parsed JSON response.

    HTTP errors are raised as RuntimeError including the server's error text.
    """
    outgoing = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(outgoing, timeout=timeout) as resp:
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
    result = call_server(url, body, content_type, STT_TIMEOUT)
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
    result = call_server(url, json.dumps(body).encode("utf-8"), "application/json", LLM_TIMEOUT)
    content = result["choices"][0]["message"].get("content") or ""
    # llama-server normally moves thinking to a separate field; drop it if not.
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    return content.strip(), {"request": {"url": url, "body": body}, "response": result}


# The web app. Handlers use the module-level settings below, set in main().

app = App(static_dir=STATIC_DIR)  # request bodies up to 1 MB unless a handler allows more
config = None  # the loaded config.toml
mock = False  # --mock: sample data instead of whisper/llama
mock_counter = itertools.count(MOCK_PRELOAD)  # next sample line in mock mode


@app.get("/api/config")
def get_config(req):
    """The settings the browser needs."""
    global mock_counter
    prompt = config["llama_prompt"]
    payload = {
        "sample_rate": config["audio"]["sample_rate"],
        "language1": prompt["language1"],
        "language2": prompt["language2"],
        "auto_split": config.get("auto_split", {}),
        # Browser microphone processing, passed to getUserMedia.
        "audio_processing": {
            key: config["audio"].get(key, default) for key, default in
            [("auto_gain_control", False), ("noise_suppression", True), ("echo_cancellation", True)]},
        "mock": mock,
    }
    if mock:
        # A page (re)load starts the sample conversation over.
        payload["mock_conversation"] = MOCK_CONVERSATION[:MOCK_PRELOAD]
        mock_counter = itertools.count(MOCK_PRELOAD)
    return payload


@app.post("/api/translate")
def post_translate(req):
    """Body: {"text": "..."}. Returns {"translation", "llm_seconds", "llm_raw"}."""
    data = req.json()
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        raise HTTPError(400, 'expected JSON {"text": "..."}')
    text = text.strip()
    if not text:
        raise HTTPError(400, "no text to translate")

    if mock:
        time.sleep(MOCK_DELAY)
        known = {item["text"]: item["translation"] for item in MOCK_CONVERSATION}
        translation = known.get(text, f"[mock translation] {text}")
        return {"translation": translation, "llm_seconds": MOCK_DELAY,
                "llm_raw": {"mock": True, "text": text, "translation": translation}}

    start = time.monotonic()
    try:
        translation, raw = translate(config, text)
    except (OSError, RuntimeError, ValueError, KeyError, IndexError) as err:
        error = f"translation failed: {err} (is llama-server running at {config['llama_url']}?)"
        log.warning("%s; text: %r", error, text)
        raise HTTPError(502, error) from err
    seconds = round(time.monotonic() - start, 2)
    log.info("translated in %ss: %r -> %r", seconds, text, translation)
    return {"translation": translation, "llm_seconds": seconds, "llm_raw": raw}


@app.post("/api/recordings")
def post_recording(req):
    """Body: a WAV file. Saves and transcribes it; returns
    {"file", "bytes", "text", "language", "stt_seconds", "stt_raw"} with status 201."""
    data = req.body(max_bytes=config["audio"]["max_upload_mb"] * 1024 * 1024)
    if not data:
        raise HTTPError(400, "empty upload")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise HTTPError(400, "not a WAV file")

    if mock:
        # Don't save or transcribe; answer with the next sample line.
        time.sleep(MOCK_DELAY)
        item = MOCK_CONVERSATION[next(mock_counter) % len(MOCK_CONVERSATION)]
        return {"file": "(mock mode: not saved)", "bytes": len(data),
                "text": item["text"], "language": item["language"], "stt_seconds": MOCK_DELAY,
                "stt_raw": {"mock": True, "text": item["text"], "language": item["language"]}}, 201

    temp_dir = config["audio"]["temp_dir"]
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / datetime.now().strftime("recording_%Y%m%d_%H%M%S_%f.wav")
    path.write_bytes(data)
    log.info("saved recording %s (%d bytes)", path, len(data))

    start = time.monotonic()
    try:
        result = transcribe(config["whisper_url"], data, path.name)
    except (OSError, RuntimeError, ValueError) as err:
        # OSError: connection problems; RuntimeError: errors from whisper-server.
        error = f"transcription failed: {err} (is whisper-server running at {config['whisper_url']}?)"
        log.warning("%s; recording: %s", error, path.name)
        raise HTTPError(502, error, file=str(path), bytes=len(data)) from err
    seconds = round(time.monotonic() - start, 2)
    log.info("transcribed %s in %ss (%s): %r", path.name, seconds, result["language"], result["text"])
    return {"file": str(path), "bytes": len(data), **result, "stt_seconds": seconds}, 201


def main():
    global config, mock
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG, type=Path,
                        help="config file (default: config.toml next to app.py)")
    parser.add_argument("--mock", action="store_true",
                        help="sample data instead of whisper/llama, for styling the page")
    args = parser.parse_args()

    config = load_config(args.config)
    mock = args.mock
    log_path = setup_logging(config["_config_dir"])
    host = config["server"]["host"]
    port = config["server"]["port"]

    shown_host = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    banner = [
        *(["Mode:       MOCK (sample data; whisper and llama are not used)"] if mock else []),
        f"Config:     {args.config.resolve()}",
        f"Log:        {log_path}",
        f"Recordings: {config['audio']['temp_dir']}",
        f"Whisper:    {config['whisper_url']}",
        f"Llama:      {config['llama_url']}",
        f"Languages:  {config['llama_prompt']['language1']} <-> {config['llama_prompt']['language2']}",
        f"Dashboard:  http://{shown_host}:{port}/",
    ]

    def ready():
        print("\n".join(banner) + "  (Ctrl+C to stop)")
        log.info("started\n  " + "\n  ".join(banner))

    app.run(host, port, on_ready=ready)
    log.info("stopped")


if __name__ == "__main__":
    main()
