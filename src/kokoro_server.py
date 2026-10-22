"""kokoro_server.py - Kokoro text-to-speech server: misaki's phonemes (the
front end Kokoro was trained with) on the Kokoro ONNX model, through
onnxruntime, on the CPU.

The model and its voices use sherpa-onnx's packaging (model.onnx, voices.bin
and tokens.txt); its own text front end isn't used. --repo-id identifies the
original Kokoro package, as in the reference KPipeline, including its Chinese
phonemizer version. Misaki setup and chunking follow KPipeline without its
PyTorch dependency. Explicit lang_code selects the reference language pipeline.
For existing dashboard clients without lang_code, script-based English/Chinese/
Japanese selection is retained deliberately. Chinese uses the reference's
optional English callback for mixed text. See DESIGN.md for deliberate differences.

Endpoints, shaped like the servers the dashboard already uses:
    POST /v1/audio/speech  OpenAI-style TTS: JSON {"input", "voice" (name such as
                           "zf_001", or index; comma-separated blends), "speed", "lang_code" (optional),
                           "response_format": "pcm" (streamed chunk by chunk,
                           s16le mono 24 kHz) or "wav"}
    GET  /v1/audio/voices  {"voices": [names]}
    GET  /health           {"status": "ok"} once loaded (and warmed up)

Usage:
    python kokoro_server.py --kokoro MODEL [--voice NAME=FILE ...] [--lib DIR]
                            [--repo-id hexgrad/Kokoro-82M-v1.1-zh]
                            [--host 127.0.0.1] [--port 8083] [--threads 0]

--kokoro is model.onnx, with voices.bin and tokens.txt next to it. --repo-id
identifies the original model package (v1.0: hexgrad/Kokoro-82M); it does not
download assets. --voice
adds a voicepack (a PyTorch .pt of shape [510, 1, 256], or the same as raw
float32) made for the same model, spoken as NAME. --lib is a folder of
packages installed with pip install --target (misaki and its English,
Chinese and Japanese dependencies, the spaCy model en_core_web_sm,
onnxruntime, numpy):
install_api_servers.py installs them; otherwise they come from this Python.
"""

import argparse
import io
import logging
import queue
import re
import sys
import threading
import time
import types
import wave
import zipfile
from pathlib import Path

from microweb import App, HTTPError, Stream

# Preprocessing/chunking adapted from hexgrad/kokoro's KPipeline (Apache-2.0).
# Modified for ONNX, bundled voices and the HTTP adapter. License: licenses/Kokoro-LICENSE.txt.
log = logging.getLogger("kokoro_server")
SAMPLE_RATE = 24000
PCM_TYPE = f"audio/pcm; rate={SAMPLE_RATE}; channels=1"
MAX_PHONEMES = 510  # the model's limit, and the voicepacks' rows
VOICE_FLOATS = MAX_PHONEMES * 256
LANG_CODES = {"a": "American English", "b": "British English", "e": "es", "f": "fr-fr",
              "h": "hi", "i": "it", "p": "pt-br", "j": "Japanese", "z": "Mandarin Chinese"}
LANG_ALIASES = {"en-us": "a", "en-gb": "b", "es": "e", "fr-fr": "f", "hi": "h",
                "it": "i", "pt-br": "p", "ja": "j", "zh": "z"}
JAPANESE = re.compile(r"[ぁ-ヿｦ-ﾟ]")  # kana (hiragana, katakana, half-width katakana)
CHINESE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")  # Han characters


def read_voicepack(path, np):
    """A voicepack as a [510, 1, 256] float32 array: from a PyTorch .pt (a zip
    whose one tensor's data is the data/0 entry), or raw little-endian float32."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            order = [n for n in names if n.endswith("/byteorder")]
            if order and z.read(order[0]).strip() != b"little":
                raise SystemExit(f"error: {path}: not little-endian data")
            data = [n for n in names if n.endswith("/data/0")]
            if len(data) != 1:
                raise SystemExit(f"error: {path}: expected one tensor (a [510, 1, 256] voicepack)")
            raw = z.read(data[0])
    else:
        raw = path.read_bytes()
    if len(raw) != VOICE_FLOATS * 4:
        raise SystemExit(f"error: {path}: {len(raw)} bytes of data, not a [510, 1, 256] float32 voicepack")
    return np.frombuffer(raw, dtype="<f4").reshape(MAX_PHONEMES, 1, 256)


def wav_bytes(pcm):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return out.getvalue()


class Kokoro:
    """The model, its voices and misaki. Generation goes chunk by chunk; one
    request at a time."""

    def __init__(self, model, extra_voices, threads, repo_id="hexgrad/Kokoro-82M-v1.1-zh"):
        # Imported here: from --lib, which main() has put on the import path.
        import importlib.util
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as err:
            raise SystemExit(f"error: {err.name} is not installed: Kokoro's packages are missing "
                             f"(run install_api_servers.py, or Launch, which runs it)") from None
        if not importlib.util.find_spec("en_core_web_sm"):
            # misaki would otherwise download it with pip, into this Python.
            raise SystemExit("error: the spaCy model en_core_web_sm is not installed (run install_api_servers.py)")
        # misaki.ja imports pyopenjtalk, but only its non-default G2P uses it
        # (version="pyopenjtalk"); cutlet, KPipeline's, doesn't. A stand-in
        # saves installing it (no wheel; its replacement brings ~370 MB).
        sys.modules.setdefault("pyopenjtalk", types.ModuleType("pyopenjtalk"))
        try:
            from misaki import en, espeak, ja, zh
        except ImportError as err:
            raise SystemExit(f"error: {err.name} is not installed: Kokoro's packages are missing "
                             f"(run install_api_servers.py, or Launch, which runs it)") from None

        self.np = np
        folder = Path(model).parent
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])

        # The stock voices: voices.bin, named in the model's metadata (sherpa-onnx's packaging).
        stock = np.fromfile(folder / "voices.bin", dtype="<f4")
        if stock.size % VOICE_FLOATS:
            raise SystemExit(f"error: {folder / 'voices.bin'} is not a whole number of voicepacks")
        stock = stock.reshape(-1, MAX_PHONEMES, 1, 256)
        meta = self.session.get_modelmeta().custom_metadata_map
        names = meta.get("speaker_names", "").split(",")
        if len(names) != len(stock):
            names = [str(i) for i in range(len(stock))]
        self.packs = dict(zip(names, stock))
        for name, path in extra_voices.items():
            if name in self.packs:
                raise SystemExit(f"error: --voice {name}: that name is already taken")
            self.packs[name] = read_voicepack(path, np)
        self.voices = list(self.packs)

        # Phoneme -> token id (tokens.txt: "symbol id" per line; the symbol may be a space).
        self.vocab = {}
        for line in (folder / "tokens.txt").read_text(encoding="utf-8").splitlines():
            if line.strip():
                symbol, _, index = line.rpartition(" ")
                self.vocab[symbol] = int(index)

        self.repo_id = repo_id
        self.misaki = types.SimpleNamespace(en=en, espeak=espeak, ja=ja, zh=zh)
        self.g2ps = {}
        self.lock = threading.Lock()

    def phonemizer(self, lang_code):
        """KPipeline's language-specific setup; cache one G2P per language."""
        if lang_code not in self.g2ps:
            if lang_code in "ab":
                british = lang_code == "b"
                try:
                    fallback = self.misaki.espeak.EspeakFallback(british=british)
                except Exception:
                    log.warning("EspeakFallback not enabled: out-of-dictionary words will be skipped")
                    fallback = None
                g2p = self.misaki.en.G2P(trf=False, british=british, fallback=fallback, unk="")
            elif lang_code == "j":
                g2p = self.misaki.ja.JAG2P()
            elif lang_code == "z":
                g2p = self.misaki.zh.ZHG2P(
                    version=None if self.repo_id.endswith("/Kokoro-82M") else "1.1",
                    en_callable=lambda text: self.phonemizer("a")(text)[0])
            else:
                g2p = self.misaki.espeak.EspeakG2P(language=LANG_CODES[lang_code])
            self.g2ps[lang_code] = g2p
        return self.g2ps[lang_code]

    def voice_pack(self, voice):
        """A voice name, or its index ("3") -> its pack, or None if unknown."""
        voice = str(voice)
        if voice in self.packs:
            return self.packs[voice]
        if voice.isdigit() and int(voice) < len(self.voices):
            return self.packs[self.voices[int(voice)]]
        if "," in voice:
            packs = [self.voice_pack(name) for name in voice.split(",")]
            if all(pack is not None for pack in packs):
                return self.np.mean(self.np.stack(packs), axis=0)
        return None

    # --- text -> phoneme chunks, as KPipeline does it
    # Reference: hexgrad/kokoro@6d87f4ae7abc2d14dbc4b3ef2e5f19852e861ac2

    def chunks(self, text, lang_code=None):
        """KPipeline's phoneme chunks, with legacy script selection if omitted."""
        for line in re.split(r"\n+", text.strip()):
            if not line.strip():
                continue
            code = lang_code or ("j" if JAPANESE.search(line) else "z" if CHINESE.search(line) else "a")
            g2p = self.phonemizer(code)
            if code in "ab":
                yield from self.english_chunks(line, g2p)
            else:
                yield from self.sentence_chunks(line, g2p)

    def sentence_chunks(self, line, g2p):
        """KPipeline's non-English chunking, including its punctuation rules."""
        parts = re.split(r"([.!?]+)", line)
        pieces, current = [], ""
        for i in range(0, len(parts), 2):
            sentence = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
            if len(current) + len(sentence) <= 400:
                current += sentence
            else:
                if current:
                    pieces.append(current.strip())
                current = sentence
        if current:
            pieces.append(current.strip())
        if not pieces:
            pieces = [line[i:i+400] for i in range(0, len(line), 400)]
        for piece in pieces:
            if piece:
                phonemes, _ = g2p(piece)
                if phonemes:
                    yield phonemes[:MAX_PHONEMES]

    def english_chunks(self, line, g2p):
        """misaki's tokens, gathered into chunks of up to 510 phonemes, split at
        the last sentence end (else clause, else comma) that fits:
        KPipeline.en_tokenize."""
        def to_ps(tokens):
            return "".join(t.phonemes + (" " if t.whitespace else "") for t in tokens).strip()

        def split_point(tokens, count):
            for marks in ("!.?…", ":;", ",—"):
                z = next((i for i, t in reversed(list(enumerate(tokens))) if t.phonemes in set(marks)), None)
                if z is None:
                    continue
                z += 1
                if z < len(tokens) and tokens[z].phonemes in (")", "”"):
                    z += 1
                if count - len(to_ps(tokens[:z])) <= MAX_PHONEMES:
                    return z
            return len(tokens)

        _, tokens = g2p(line)
        current, count = [], 0
        for token in tokens:
            token.phonemes = token.phonemes or ""
            next_ps = token.phonemes + (" " if token.whitespace else "")
            next_count = count + len(next_ps.rstrip())
            if next_count > MAX_PHONEMES:
                z = split_point(current, next_count)
                if ps := to_ps(current[:z]):
                    yield ps[:MAX_PHONEMES]
                current = current[z:]
                count = len(to_ps(current))
                if not current:
                    next_ps = next_ps.lstrip()
            current.append(token)
            count += len(next_ps)
        if ps := to_ps(current):
            yield ps[:MAX_PHONEMES]

    # --- speech

    def generate(self, text, pack, speed, on_audio, lang_code=None):
        """Speak text; on_audio(pcm bytes) is called per chunk and returns False
        to stop early."""
        np = self.np
        with self.lock:
            for phonemes in self.chunks(text, lang_code):
                ids = [self.vocab[p] for p in phonemes if p in self.vocab]
                audio = self.session.run(None, {
                    "tokens": np.array([[0, *ids, 0]], dtype=np.int64),
                    "style": pack[len(phonemes) - 1].astype(np.float32),
                    "speed": np.array([speed], dtype=np.float32),
                })[0]
                pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
                if not on_audio(pcm):
                    return


# --- HTTP

app = App()
tts = None  # the Kokoro engine, set in main()


@app.get("/health")
def health(req):
    return {"status": "ok"}


@app.get("/v1/audio/voices")
def voices(req):
    return {"voices": tts.voices}


@app.post("/v1/audio/speech")
def speech(req):
    data = req.json()
    if not isinstance(data, dict) or not isinstance(data.get("input"), str) or not data["input"].strip():
        raise HTTPError(400, 'expected JSON {"input": "...", "voice": "<name or index>"}')
    text = data["input"].strip()
    lang_code = data.get("lang_code")
    if lang_code is not None:
        if not isinstance(lang_code, str):
            raise HTTPError(400, "lang_code must be a supported Kokoro language code")
        lang_code = LANG_ALIASES.get(lang_code.lower(), lang_code.lower())
        if lang_code not in LANG_CODES:
            raise HTTPError(400, "lang_code must be a supported Kokoro language code")
    voice = str(data.get("voice", tts.voices[0]))
    pack = tts.voice_pack(voice)
    if pack is None:
        raise HTTPError(400, f"unknown voice {voice!r}: see GET /v1/audio/voices")
    try:
        speed = float(data.get("speed", 1.0))
    except (TypeError, ValueError):
        raise HTTPError(400, "speed must be a number") from None
    fmt = data.get("response_format", "pcm")
    if fmt not in ("pcm", "wav"):
        raise HTTPError(400, 'response_format must be "pcm" or "wav"')

    if fmt == "wav":
        pieces = []
        tts.generate(text, pack, speed, lambda pcm: pieces.append(pcm) or True, lang_code)
        return Stream([wav_bytes(b"".join(pieces))], "audio/wav")

    # Streamed: generate in a worker thread, relaying each chunk as it's done.
    chunks = queue.Queue()
    stop = threading.Event()

    def work():
        try:
            tts.generate(text, pack, speed, lambda pcm: chunks.put(pcm) or not stop.is_set(), lang_code)
        except Exception:
            log.exception("speech failed: %r", text)
        finally:
            chunks.put(None)

    def relay():
        start, total = time.monotonic(), 0
        threading.Thread(target=work, daemon=True).start()
        try:
            while (pcm := chunks.get()) is not None:
                total += len(pcm)
                yield pcm
        finally:
            stop.set()  # the client went away: stop generating
            log.info("spoke %.1fs of audio in %.2fs (voice %s): %r", total / 2 / SAMPLE_RATE,
                     time.monotonic() - start, voice, text)

    return Stream(relay(), PCM_TYPE)


def main():
    global tts
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--kokoro", required=True, type=Path,
                        help="Kokoro model.onnx (voices.bin and tokens.txt next to it)")
    parser.add_argument("--voice", action="append", default=[], metavar="NAME=FILE",
                        help="an extra voicepack (.pt or raw .bin, [510, 1, 256]) spoken as NAME; repeatable")
    parser.add_argument("--lib", type=Path, help="folder of packages installed with pip install --target")
    parser.add_argument("--repo-id", default="hexgrad/Kokoro-82M-v1.1-zh",
                        help="original model package identity, as in KPipeline (v1.0: hexgrad/Kokoro-82M)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--threads", type=int, default=0, help="CPU threads (0: ONNX Runtime automatic selection; default: 0)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")

    for path in (args.kokoro, args.kokoro.parent / "voices.bin", args.kokoro.parent / "tokens.txt"):
        if not path.is_file():
            raise SystemExit(f"error: {path} not found")
    extra_voices = {}
    for spec in args.voice:
        name, _, file = spec.partition("=")
        if not name or not file:
            parser.error(f"--voice {spec!r}: expected NAME=FILE")
        if not Path(file).is_file():
            raise SystemExit(f"error: voicepack {file} not found")
        extra_voices[name] = Path(file)
    if args.lib:
        if not args.lib.is_dir():
            raise SystemExit(f"error: {args.lib} not found (run install_api_servers.py)")
        sys.path.insert(0, str(args.lib))

    start = time.monotonic()
    tts = Kokoro(args.kokoro, extra_voices, args.threads, args.repo_id)
    # Warm up (spaCy, jieba's and MeCab's dictionaries, the first inference) before answering /health.
    for text in ("Hello.", "你好。", "こんにちは。"):
        tts.generate(text, tts.packs[tts.voices[0]], 1.0, lambda pcm: True)
    log.info("Kokoro loaded in %.1fs: %d voices (%d added: %s), %d threads", time.monotonic() - start,
             len(tts.voices), len(extra_voices), ", ".join(extra_voices) or "none", args.threads)
    app.run(args.host, args.port,
            on_ready=lambda: log.info("listening on http://%s:%d/", args.host, args.port))


if __name__ == "__main__":
    main()
