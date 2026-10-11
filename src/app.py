"""Audio Translation Dashboard - backend server.

Usage, from the project folder: python src/app.py [--mock] [path/to/config_local.toml]

Settings: config/defaults.toml, then config_generated.toml, then
config_local.toml (or the given file); see configuration.py.

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
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import configuration
from microweb import App, HTTPError, Stream

ROOT = configuration.ROOT  # the project folder
STATIC_DIR = Path(__file__).resolve().parent / "static"
STT_TIMEOUT = 300  # seconds to wait for whisper-server
LLM_TIMEOUT = 300  # seconds to wait for llama-server
TTS_TIMEOUT = 120  # seconds to wait for the TTS server
# The TTS server (Kokoro or Qwen3-TTS) streams raw PCM: 16-bit little-endian
# samples, mono, at this rate.
TTS_PCM_TYPE = "audio/pcm; rate=24000; channels=1"

# Shared log file and line format (same as common.py).
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
    """Log to logs/app.log (next to config_local.toml); warnings also go to the console."""
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
    """The settings (see configuration.py), with path standing in for
    config_local.toml, plus values derived from them: absolute temp_dir, the
    servers' URLs and the rendered system prompt."""
    config = configuration.load(path)
    temp_dir = Path(config["audio"]["temp_dir"])
    if not temp_dir.is_absolute():
        temp_dir = ROOT / temp_dir
    config["audio"]["temp_dir"] = temp_dir
    whisper = config["api_servers"]["whisper"]
    config["whisper_url"] = f"http://{whisper['host']}:{whisper['port']}"
    llama = config["api_servers"]["llama"]
    config["llama_url"] = f"http://{llama['host']}:{llama['port']}"
    tts = config["api_servers"][config["tts"]["server"]]  # the [api_servers] section of the TTS engine
    config["tts_url"] = f"http://{tts['host']}:{tts['port']}"
    config["system_prompt"] = render_prompt(config["llama_prompt"], config["languages"])
    return config


def render_prompt(prompt, languages):
    """Fill {language1} and {language2} from [languages] into the [llama_prompt] system prompt."""
    text = prompt["system"]
    for key in ("language1", "language2"):
        text = text.replace("{" + key + "}", languages[key])
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


def open_server(url, body, content_type, timeout):
    """POST body to whisper-server or llama-server and return the open response
    as soon as the server starts answering (the caller reads and closes it).

    HTTP errors are raised as RuntimeError including the server's error text.
    """
    outgoing = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
    try:
        return urllib.request.urlopen(outgoing, timeout=timeout)
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace").strip()
        raise RuntimeError(f"HTTP {err.code}: {detail[:500]}") from err


def call_server(url, body, content_type, timeout):
    """Call whisper-server or llama-server: POST body to url and return the
    parsed JSON response."""
    with open_server(url, body, content_type, timeout) as resp:
        return json.load(resp)


def sse_event(name, data):
    """One server-sent event of our own, named so it can't be mistaken for
    llama's (unnamed) events."""
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


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


def translation_request(config, text):
    """The (url, body) of a streamed translation request to llama-server's
    OpenAI-style chat endpoint."""
    prompt = config["llama_prompt"]
    body = {
        "messages": [
            {"role": "system", "content": config["system_prompt"]},
            {"role": "user", "content": text},
        ],
        "temperature": prompt.get("temperature", 0.2),
        # Passed to the model's chat template, which switches thinking on or off.
        "chat_template_kwargs": {"enable_thinking": prompt.get("enable_thinking", False)},
        "stream": True,
        "stream_options": {"include_usage": True},  # token counts in the last chunk
    }
    return f"{config['llama_url']}/v1/chat/completions", body


def stream_content(line):
    """The translation text in one line of llama's stream ("data: {...}"), if any."""
    if not line.startswith(b"data: {"):
        return None
    try:
        chunk = json.loads(line[len(b"data: "):])
        return chunk["choices"][0]["delta"].get("content")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def relay_translation(text, url, body, resp, start):
    """Our "request" event, then llama's response exactly as it arrives. The
    stream is also read as it passes, to log the finished translation."""
    yield sse_event("request", {"url": url, "body": body})
    pieces = []
    first = None
    try:
        with resp:
            for line in resp:
                yield line
                content = stream_content(line)
                if content:
                    first = first if first is not None else time.monotonic() - start
                    pieces.append(content)
    except OSError as err:
        error = f"relaying llama's response failed: {err}"
        log.warning("%s; text: %r", error, text)
        yield sse_event("error", {"error": error})
    seconds = time.monotonic() - start
    log.info("translated in %.2fs (first token after %.2fs): %r -> %r",
             seconds, first or 0, text, "".join(pieces).strip())


def mock_translation_stream(text):
    """A made-up stream in llama's format, word by word, for --mock."""
    known = {item["text"]: item["translation"] for item in MOCK_CONVERSATION}
    translation = known.get(text, f"[mock translation] {text}")
    yield sse_event("request", {"mock": True, "text": text})
    time.sleep(MOCK_DELAY / 2)
    # Pieces like a tokenizer's: single CJK characters, or words with their spaces.
    for piece in re.findall(r"[㐀-鿿　-〿＀-￯]|[^㐀-鿿　-〿＀-￯\s]+\s*|\s+",
                            translation):
        chunk = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                 "object": "chat.completion.chunk", "model": "mock"}
        yield f"data: {json.dumps(chunk)}\n\n"
        time.sleep(0.06)
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "object": "chat.completion.chunk", "model": "mock"}
    yield f"data: {json.dumps(done)}\n\n"
    yield "data: [DONE]\n\n"


def mock_speech_stream(text):
    """Silence in the TTS server's format (s16le, 24 kHz), about as long as the text
    would take to say, streamed in pieces, for --mock."""
    seconds = min(1.0 + 0.07 * len(text), 15.0)
    piece = bytes(2 * 24000 // 10)  # 0.1 s of silence
    time.sleep(0.05)
    for _ in range(int(seconds * 10)):
        yield piece
        time.sleep(0.01)


# The web app. Handlers use the module-level settings below, set in main().

app = App(static_dir=STATIC_DIR)  # request bodies up to 1 MB unless a handler allows more
config = None  # the settings (see load_config)
mock = False  # --mock: sample data instead of whisper/llama
mock_counter = itertools.count(MOCK_PRELOAD)  # next sample line in mock mode


@app.get("/api/config")
def get_config(req):
    """The settings the browser needs."""
    global mock_counter
    languages = config["languages"]
    payload = {
        "sample_rate": config["audio"]["sample_rate"],
        "language1": languages["language1"],
        "language2": languages["language2"],
        "auto_split": config.get("auto_split", {}),
        "auto_play": config["tts"].get("auto_play", False),
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
    """Body: {"text": "..."}. Streams text/event-stream:
        event: request    ours: the request sent to llama-server
        data: {...}       llama's streamed response, unchanged
        ...
        data: [DONE]
        event: error      ours, only if relaying fails midway
    If llama-server can't be reached, a 502 JSON error instead."""
    data = req.json()
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str):
        raise HTTPError(400, 'expected JSON {"text": "..."}')
    text = text.strip()
    if not text:
        raise HTTPError(400, "no text to translate")

    if mock:
        return Stream(mock_translation_stream(text), "text/event-stream")

    url, body = translation_request(config, text)
    start = time.monotonic()
    try:
        # Connect before returning the stream, so an unreachable server is a plain 502.
        resp = open_server(url, json.dumps(body).encode("utf-8"), "application/json", LLM_TIMEOUT)
    except (OSError, RuntimeError) as err:
        error = f"translation failed: {err} (is llama-server running at {config['llama_url']}?)"
        log.warning("%s; text: %r", error, text)
        raise HTTPError(502, error) from err
    return Stream(relay_translation(text, url, body, resp, start), "text/event-stream")


@app.get("/api/voices")
def get_voices(req):
    """The TTS server's voices, for the test page: {"voices": [names], "default"}."""
    default = config["tts"]["voice"]
    if mock:
        return {"voices": [default], "default": default}
    url = f"{config['tts_url']}/v1/audio/voices"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            result = json.load(resp)
    except (OSError, ValueError) as err:
        raise HTTPError(502, f"could not list voices: {err} (is the TTS server running at "
                             f"{config['tts_url']}?)") from err
    # Kokoro's server lists names; other servers may list objects with a name.
    voices = result.get("voices", result) if isinstance(result, dict) else result
    names = [v.get("name") if isinstance(v, dict) else v for v in voices or []]
    return {"voices": [str(n) for n in names if n], "default": default}


@app.post("/api/speak")
def post_speak(req):
    """Body: {"text": "...", "voice": optional, default the configured one}.
    Streams the TTS server's audio for the text unchanged: raw PCM (see
    TTS_PCM_TYPE). If the TTS server can't be reached, a 502 JSON error instead."""
    data = req.json()
    text = data.get("text") if isinstance(data, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise HTTPError(400, 'expected JSON {"text": "..."} with some text')
    text = text.strip()
    voice = data.get("voice") or config["tts"]["voice"]
    if not isinstance(voice, str):
        raise HTTPError(400, "voice must be a string")

    if mock:
        return Stream(mock_speech_stream(text), TTS_PCM_TYPE)

    body = {"input": text, "voice": voice, "response_format": "pcm"}
    start = time.monotonic()
    try:
        # Connect before returning the stream, so an unreachable server is a plain 502.
        resp = open_server(f"{config['tts_url']}/v1/audio/speech", json.dumps(body).encode("utf-8"),
                           "application/json", TTS_TIMEOUT)
    except (OSError, RuntimeError) as err:
        # RuntimeError: the server answered with an error; OSError: it couldn't be reached.
        hint = "" if isinstance(err, RuntimeError) else f" (is the TTS server running at {config['tts_url']}?)"
        error = f"speech failed: {err}{hint}"
        log.warning("%s; text: %r", error, text)
        raise HTTPError(502, error) from err

    def relay():
        n = 0
        try:
            with resp:
                while chunk := resp.read1(65536):
                    n += len(chunk)
                    yield chunk
        except OSError as err:
            log.warning("relaying speech failed: %s; text: %r", err, text)
        log.info("spoke %.1fs of audio in %.2fs (voice %r): %r", n / 2 / 24000,
                 time.monotonic() - start, voice, text)

    return Stream(relay(), TTS_PCM_TYPE)


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
    parser.add_argument("config", nargs="?", default=configuration.CONFIG_LOCAL_FILE, type=Path,
                        help="settings on top of the defaults and config_generated.toml "
                             "(default: config_local.toml in the project folder)")
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
        *(["Mode:       MOCK (sample data; whisper, llama and tts are not used)"] if mock else []),
        f"Config:     {' + '.join(p.name for p in config['_files'])}",
        f"Log:        {log_path}",
        f"Recordings: {config['audio']['temp_dir']}",
        f"Whisper:    {config['whisper_url']}",
        f"Llama:      {config['llama_url']}",
        f"TTS:        {config['tts_url']} ({config['tts']['server']}, voice {config['tts']['voice']!r})",
        f"Languages:  {config['languages']['language1']} <-> {config['languages']['language2']}",
        f"Dashboard:  http://{shown_host}:{port}/",
        f"All services started, and ready for translation.",
    ]

    def ready():
        print("\n".join(banner))
        log.info("started\n  " + "\n  ".join(banner))

    app.run(host, port, on_ready=ready)
    log.info("stopped")


if __name__ == "__main__":
    main()
