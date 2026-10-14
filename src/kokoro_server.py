"""kokoro_server.py - Kokoro text-to-speech server: misaki's phonemes (the
front end Kokoro was trained with) on the Kokoro ONNX model, through
onnxruntime, on the CPU.

The model and its voices are sherpa-onnx's packaging of Kokoro v1.1-zh
(model.onnx, voices.bin and tokens.txt from kokoro-multi-lang-v1_1); its
own text front end isn't used. misaki turns text into phonemes as the
official kokoro package does (KPipeline): Japanese (any kana) with its
Japanese G2P (cutlet, KPipeline's for lang_code "j"), Chinese (Han characters
without kana) with its v1.1 G2P (any English words in it with the English
one), anything else with its American English G2P and the espeak fallback for
unknown words; chunked the same way,
each chunk spoken with the voice's style vector for its phoneme count. Voices
made by a search on that pipeline (voices/RC3) sound as they did there.

Endpoints, shaped like the servers the dashboard already uses:
    POST /v1/audio/speech  OpenAI-style TTS: JSON {"input", "voice" (name such as
                           "zf_001", or index), "speed",
                           "response_format": "pcm" (streamed chunk by chunk,
                           s16le mono 24 kHz) or "wav"}
    GET  /v1/audio/voices  {"voices": [names]}
    GET  /health           {"status": "ok"} once loaded (and warmed up)

Usage:
    python kokoro_server.py --kokoro MODEL [--voice NAME=FILE ...] [--lib DIR]
                            [--host 127.0.0.1] [--port 8083] [--threads 4]

--kokoro is model.onnx, with voices.bin and tokens.txt next to it. --voice
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

log = logging.getLogger("kokoro_server")
SAMPLE_RATE = 24000
PCM_TYPE = f"audio/pcm; rate={SAMPLE_RATE}; channels=1"
MAX_PHONEMES = 510  # the model's limit, and the voicepacks' rows
VOICE_FLOATS = MAX_PHONEMES * 256
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

    def __init__(self, model, extra_voices, threads):
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

        # misaki, as KPipeline sets it up for Kokoro v1.1-zh.
        self.english = en.G2P(trf=False, british=False, fallback=espeak.EspeakFallback(british=False), unk="")
        self.chinese = zh.ZHG2P(version="1.1", en_callable=lambda text: self.english(text)[0])
        try:
            self.japanese = ja.JAG2P()  # cutlet: fugashi, with unidic-lite's dictionary
        except (ImportError, RuntimeError) as err:  # fugashi raises RuntimeError without a dictionary
            raise SystemExit(f"error: misaki's Japanese G2P failed to load ({err}): Kokoro's packages "
                             f"are missing or out of date (run install_api_servers.py)") from None
        self.lock = threading.Lock()

    def voice_pack(self, voice):
        """A voice name, or its index ("3") -> its pack, or None if unknown."""
        voice = str(voice)
        if voice in self.packs:
            return self.packs[voice]
        if voice.isdigit() and int(voice) < len(self.voices):
            return self.packs[self.voices[int(voice)]]
        return None

    # --- text -> phoneme chunks, as KPipeline does it

    def chunks(self, text):
        """The phoneme strings to speak, in order: per line, Japanese (any
        kana), Chinese (any Han characters) or English."""
        for line in re.split(r"\n+", text.strip()):
            if not line.strip():
                continue
            if JAPANESE.search(line):
                yield from self.sentence_chunks(line, self.japanese)
            elif CHINESE.search(line):
                yield from self.sentence_chunks(line, self.chinese)
            else:
                yield from self.english_chunks(line)

    def sentence_chunks(self, line, g2p):
        """Sentences grouped into chunks of up to 400 characters (KPipeline's
        non-English chunking; also split at 。！？), each to phonemes by g2p."""
        parts = re.split(r"([.!?。！？]+)", line)
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
        for piece in pieces:
            if piece:
                phonemes, _ = g2p(piece)
                if phonemes:
                    yield phonemes[:MAX_PHONEMES]

    def english_chunks(self, line):
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

        _, tokens = self.english(line)
        current, count = [], 0
        for token in tokens:
            token.phonemes = token.phonemes or ""
            next_ps = token.phonemes + (" " if token.whitespace else "")
            next_count = count + len(next_ps.rstrip())
            if next_count > MAX_PHONEMES:
                z = split_point(current, next_count)
                if ps := to_ps(current[:z]):
                    yield ps
                current = current[z:]
                count = len(to_ps(current))
                if not current:
                    next_ps = next_ps.lstrip()
            current.append(token)
            count += len(next_ps)
        if ps := to_ps(current):
            yield ps[:MAX_PHONEMES]

    # --- speech

    def generate(self, text, pack, speed, on_audio):
        """Speak text; on_audio(pcm bytes) is called per chunk and returns False
        to stop early."""
        np = self.np
        with self.lock:
            for phonemes in self.chunks(text):
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
        tts.generate(text, pack, speed, lambda pcm: pieces.append(pcm) or True)
        return Stream([wav_bytes(b"".join(pieces))], "audio/wav")

    # Streamed: generate in a worker thread, relaying each chunk as it's done.
    chunks = queue.Queue()
    stop = threading.Event()

    def work():
        try:
            tts.generate(text, pack, speed, lambda pcm: chunks.put(pcm) or not stop.is_set())
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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    parser.add_argument("--threads", type=int, default=4, help="CPU threads (default: 4)")
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
    tts = Kokoro(args.kokoro, extra_voices, args.threads)
    # Warm up (spaCy, jieba's and MeCab's dictionaries, the first inference) before answering /health.
    for text in ("Hello.", "你好。", "こんにちは。"):
        tts.generate(text, tts.packs[tts.voices[0]], 1.0, lambda pcm: True)
    log.info("Kokoro loaded in %.1fs: %d voices (%d added: %s), %d threads", time.monotonic() - start,
             len(tts.voices), len(extra_voices), ", ".join(extra_voices) or "none", args.threads)
    app.run(args.host, args.port,
            on_ready=lambda: log.info("listening on http://%s:%d/", args.host, args.port))


if __name__ == "__main__":
    main()
