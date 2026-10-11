"""Generate voices/test_audio/: Beth's English sample in other languages, for
testing the dashboard with known speech.

Usage, from the project root, with the servers running (launch.sh):
    python src/tools/generate_test_audio.py [--voice NAME] [config_local.toml]

voices/beth_thomas.wav is transcribed by whisper (forced to English),
translated by llama into Simplified Chinese and Japanese (one sentence per
line, with its own short prompt, not the dashboard's), and each sentence is
spoken by the TTS server ([tts] server) in the voice beth_thomas (Qwen3-TTS's
clone of her; --voice for another). The sentences are joined with 0.6 s pauses,
well over [auto_split] split_pause_ms, so the dashboard splits each clip into
several phrases. Writes beth_chinese.wav and beth_japanese.wav (the TTS
server's 24 kHz mono 16-bit) and transcripts.json (the English text and the
translated sentences). Run again to regenerate them, e.g. after a model change.

Standard library only (Python 3.11+ for tomllib).
"""

import argparse
import json
import sys
import urllib.request
import uuid
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # src/, for common and configuration

from common import CONFIG_LOCAL_FILE, load_config, run_script, setup_logging
from configuration import ROOT

NAME = "generate_test_audio"
SOURCE = ROOT / "voices" / "beth_thomas.wav"
OUT_DIR = ROOT / "voices" / "test_audio"
LANGUAGES = {"chinese": "Simplified Chinese", "japanese": "Japanese"}  # file name: language for llama
TTS_RATE = 24000  # the TTS server's raw PCM: 16-bit mono at this rate
PAUSE = b"\0\0" * int(TTS_RATE * 0.6)  # between sentences
TIMEOUT = 600  # seconds, for each server call


def server_url(cfg, name):
    server = cfg["api_servers"][name]
    return f"http://{server['host']}:{server['port']}"


def post(url, body, content_type="application/json"):
    request = urllib.request.Request(url, body, {"Content-Type": content_type})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def transcribe(whisper_url, wav):
    """whisper-server's text for the WAV, forced to English."""
    boundary = uuid.uuid4().hex
    fields = {"response_format": "json", "temperature": "0.0", "language": "en"}
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                    for key, value in fields.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{SOURCE.name}"\r\n'
             f'Content-Type: audio/wav\r\n\r\n').encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    result = json.loads(post(f"{whisper_url}/inference", body, f"multipart/form-data; boundary={boundary}"))
    return result["text"].strip()


def translate(llama_url, text, language):
    """llama's translation of text into language, as a list of sentences."""
    body = {
        "messages": [
            {"role": "system", "content": f"Translate the user's text into natural {language}. "
                                          "Output only the translation, one sentence per line."},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,  # the same translation each run (the speech still varies a little)
        "chat_template_kwargs": {"enable_thinking": False},
    }
    result = json.loads(post(f"{llama_url}/v1/chat/completions", json.dumps(body).encode()))
    lines = result["choices"][0]["message"]["content"].strip().splitlines()
    return [line.strip() for line in lines if line.strip()]


def speak(tts_url, voice, sentences, path):
    """Each sentence in voice, joined with pauses, written to path; its length in seconds."""
    pcm = PAUSE[: len(PAUSE) // 2]
    for sentence in sentences:
        body = {"input": sentence, "voice": voice, "response_format": "pcm"}
        pcm += post(f"{tts_url}/v1/audio/speech", json.dumps(body).encode()) + PAUSE
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(TTS_RATE)
        out.writeframes(pcm)
    return len(pcm) / 2 / TTS_RATE


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("config", nargs="?", default=CONFIG_LOCAL_FILE, type=Path,
                        help="settings on top of the defaults and config_generated.toml "
                             "(default: the project's config_local.toml)")
    parser.add_argument("--voice", default="beth_thomas", help="the TTS voice (default: beth_thomas)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logging(cfg, NAME)
    tts = cfg["tts"]["server"]
    whisper_url, llama_url, tts_url = (server_url(cfg, "whisper"), server_url(cfg, "llama"),
                                       server_url(cfg, tts))
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    english = transcribe(whisper_url, SOURCE.read_bytes())
    print(f"== {SOURCE.name}, transcribed:\n   {english}")
    transcripts = {"english": english}
    for name, language in LANGUAGES.items():
        sentences = translate(llama_url, english, language)
        transcripts[name] = sentences
        path = OUT_DIR / f"beth_{name}.wav"
        seconds = speak(tts_url, args.voice, sentences, path)
        print(f"== {language}:", *sentences, sep="\n   ")
        print(f"   wrote {path.relative_to(ROOT).as_posix()} ({seconds:.1f} s, {tts} voice {args.voice})")
        log.info("wrote %s: %d sentences, %.1f s", path, len(sentences), seconds)
    path = OUT_DIR / "transcripts.json"
    path.write_text(json.dumps(transcripts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"== wrote {path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    run_script(main, NAME)
