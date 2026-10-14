"""sherpa_server_win.py - speech server on sherpa-onnx: text-to-speech,
speech-to-text and voice activity detection, in one process, through
sherpa-onnx's C API. Windows only for now: it loads sherpa-onnx-c-api.dll
(other systems would load libsherpa-onnx-c-api.so/.dylib from their builds).

Each engine is optional; only those given on the command line are loaded:
    --kokoro MODEL       Kokoro TTS (model.onnx; voices.bin etc. next to it)
      --voice NAME=FILE  an extra Kokoro voicepack (repeatable): a PyTorch .pt
                         of shape [510, 1, 256], or the same as raw float32 (.bin),
                         made for the same model; spoken as NAME. It takes the
                         place of a stock voice, the last ones first (zm_100,
                         then zm_098, ...): sherpa only accepts a voices.bin with
                         the model's own voice count
    --sense-voice MODEL  SenseVoice STT (model.onnx or model.int8.onnx; tokens.txt next to it)
    --vad MODEL          Silero VAD (silero_vad.onnx)

Recommended models (from sherpa-onnx's releases; unpack the .tar.bz2 archives):
    Kokoro: fp32, kokoro-multi-lang-v1_1/model.onnx (Chinese + English; 103 voices)
        https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_1.tar.bz2
        Not the int8 version: its quantized convolutions are slower than fp32 on
        both CPU (about 1.5x vs 3x real time, one thread) and GPU.
    SenseVoice: int8, model.int8.onnx from the 2024-07-17 release (the original
        FunAudioLLM SenseVoiceSmall: Chinese, English, Japanese, Korean, Cantonese)
        https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
        On CPU, int8 is the fastest and smallest (about 90x real time, under 400 MB
        of RAM, vs about 65x and 1.1 GB for fp32), with the same accuracy in tests.
        Not the 2025-09-09 release: that's a Cantonese fine-tune (WSYue-ASR).
    Silero VAD:
        https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx

Endpoints (each loaded engine's), shaped like the servers the dashboard already uses:
    POST /v1/audio/speech  OpenAI-style TTS: JSON {"input", "voice" (name such as
                           "zf_001", or speaker id), "speed",
                           "response_format": "pcm" (streamed, s16le mono) or "wav"}
    POST /inference        whisper-server-style STT: multipart "file" (WAV);
                           returns {"text", "language", "detected_language", ...}
    POST /vad              multipart "file" (WAV), optional fields as in
                           Silero's get_speech_timestamps: threshold,
                           min_speech_duration_ms, min_silence_duration_ms,
                           max_speech_duration_s, speech_pad_ms;
                           returns {"duration", "segments": [{"start", "end"}]} in seconds
    GET  /v1/audio/voices  {"voices": [names, in speaker-id order]}
    GET  /health           {"status": "ok"} once loaded

Usage:
    python sherpa_server_win.py --lib DIR [--kokoro MODEL [--voice NAME=FILE ...]]
                                [--sense-voice MODEL] [--vad MODEL]
                                [--host 127.0.0.1] [--port 8084] [--threads 4] [--provider cpu]

--lib is the folder with sherpa-onnx-c-api.dll (and onnxruntime.dll) from a
sherpa-onnx release; the structs below mirror its c-api.h, so the release is
pinned: v1.13.8. Standard library only.
"""

import argparse
import array
import ctypes as C
import email.parser
import email.policy
import io
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import wave
import zipfile
from pathlib import Path

from microweb import App, HTTPError, Stream

SHERPA_VERSION = "1.13.8"
log = logging.getLogger("sherpa_server")

# --- C API (c-api.h v1.13.8). Structs are mirrored field for field: every
# model type's config must be present, in order, for the layout to match.

S, F, I, P = C.c_char_p, C.c_float, C.c_int32, C.c_void_p


def struct(name, *fields):
    return type(name, (C.Structure,), {"_fields_": list(fields)})


# Text-to-speech
TtsVits = struct("TtsVits", ("model", S), ("lexicon", S), ("tokens", S), ("data_dir", S),
                 ("noise_scale", F), ("noise_scale_w", F), ("length_scale", F), ("dict_dir", S))
TtsMatcha = struct("TtsMatcha", ("acoustic_model", S), ("vocoder", S), ("lexicon", S), ("tokens", S),
                   ("data_dir", S), ("noise_scale", F), ("length_scale", F), ("dict_dir", S))
TtsKokoro = struct("TtsKokoro", ("model", S), ("voices", S), ("tokens", S), ("data_dir", S),
                   ("length_scale", F), ("dict_dir", S), ("lexicon", S), ("lang", S))
TtsKitten = struct("TtsKitten", ("model", S), ("voices", S), ("tokens", S), ("data_dir", S), ("length_scale", F))
TtsZipvoice = struct("TtsZipvoice", ("tokens", S), ("encoder", S), ("decoder", S), ("vocoder", S),
                     ("data_dir", S), ("lexicon", S), ("feat_scale", F), ("t_shift", F),
                     ("target_rms", F), ("guidance_scale", F))
TtsPocket = struct("TtsPocket", ("lm_flow", S), ("lm_main", S), ("encoder", S), ("decoder", S),
                   ("text_conditioner", S), ("vocab_json", S), ("token_scores_json", S),
                   ("voice_embedding_cache_capacity", I))
TtsSupertonic = struct("TtsSupertonic", ("duration_predictor", S), ("text_encoder", S),
                       ("vector_estimator", S), ("vocoder", S), ("tts_json", S),
                       ("unicode_indexer", S), ("voice_style", S))
TtsModelConfig = struct("TtsModelConfig", ("vits", TtsVits), ("num_threads", I), ("debug", I),
                        ("provider", S), ("matcha", TtsMatcha), ("kokoro", TtsKokoro),
                        ("kitten", TtsKitten), ("zipvoice", TtsZipvoice), ("pocket", TtsPocket),
                        ("supertonic", TtsSupertonic))
TtsConfig = struct("TtsConfig", ("model", TtsModelConfig), ("rule_fsts", S), ("max_num_sentences", I),
                   ("rule_fars", S), ("silence_scale", F))
GenerationConfig = struct("GenerationConfig", ("silence_scale", F), ("speed", F), ("sid", I),
                          ("reference_audio", P), ("reference_audio_len", I),
                          ("reference_sample_rate", I), ("reference_text", S), ("num_steps", I),
                          ("extra", S))
GeneratedAudio = struct("GeneratedAudio", ("samples", C.POINTER(F)), ("n", I), ("sample_rate", I))
# int32_t (*)(const float *samples, int32_t n, float progress, void *arg); return 0 to stop.
AudioCallback = C.CFUNCTYPE(I, C.POINTER(F), I, F, P)

# Speech-to-text
FeatureConfig = struct("FeatureConfig", ("sample_rate", I), ("feature_dim", I))
HomophoneReplacer = struct("HomophoneReplacer", ("dict_dir", S), ("lexicon", S), ("rule_fsts", S))
AsrTransducer = struct("AsrTransducer", ("encoder", S), ("decoder", S), ("joiner", S))
AsrModel1 = struct("AsrModel1", ("model", S))  # the many configs with just a model path
AsrWhisper = struct("AsrWhisper", ("encoder", S), ("decoder", S), ("language", S), ("task", S),
                    ("tail_paddings", I), ("enable_token_timestamps", I),
                    ("enable_segment_timestamps", I))
AsrCanary = struct("AsrCanary", ("encoder", S), ("decoder", S), ("src_lang", S), ("tgt_lang", S),
                   ("use_pnc", I))
AsrCohere = struct("AsrCohere", ("encoder", S), ("decoder", S), ("language", S), ("use_punct", I),
                   ("use_itn", I))
AsrFireRed = struct("AsrFireRed", ("encoder", S), ("decoder", S))
AsrMoonshine = struct("AsrMoonshine", ("preprocessor", S), ("encoder", S), ("uncached_decoder", S),
                      ("cached_decoder", S), ("merged_decoder", S))
AsrLm = struct("AsrLm", ("model", S), ("scale", F))
AsrSenseVoice = struct("AsrSenseVoice", ("model", S), ("language", S), ("use_itn", I))
AsrFunAsrNano = struct("AsrFunAsrNano", ("encoder_adaptor", S), ("llm", S), ("embedding", S),
                       ("tokenizer", S), ("system_prompt", S), ("user_prompt", S),
                       ("max_new_tokens", I), ("temperature", F), ("top_p", F), ("seed", I),
                       ("language", S), ("itn", I), ("hotwords", S))
AsrQwen3 = struct("AsrQwen3", ("conv_frontend", S), ("encoder", S), ("decoder", S), ("tokenizer", S),
                  ("max_total_len", I), ("max_new_tokens", I), ("temperature", F), ("top_p", F),
                  ("seed", I), ("hotwords", S))
AsrModelConfig = struct("AsrModelConfig", ("transducer", AsrTransducer), ("paraformer", AsrModel1),
                        ("nemo_ctc", AsrModel1), ("whisper", AsrWhisper), ("tdnn", AsrModel1),
                        ("tokens", S), ("num_threads", I), ("debug", I), ("provider", S),
                        ("model_type", S), ("modeling_unit", S), ("bpe_vocab", S),
                        ("telespeech_ctc", S), ("sense_voice", AsrSenseVoice),
                        ("moonshine", AsrMoonshine), ("fire_red_asr", AsrFireRed),
                        ("dolphin", AsrModel1), ("zipformer_ctc", AsrModel1), ("canary", AsrCanary),
                        ("wenet_ctc", AsrModel1), ("omnilingual", AsrModel1), ("medasr", AsrModel1),
                        ("funasr_nano", AsrFunAsrNano), ("fire_red_asr_ctc", AsrModel1),
                        ("qwen3_asr", AsrQwen3), ("cohere_transcribe", AsrCohere))
RecognizerConfig = struct("RecognizerConfig", ("feat_config", FeatureConfig),
                          ("model_config", AsrModelConfig), ("lm_config", AsrLm),
                          ("decoding_method", S), ("max_active_paths", I), ("hotwords_file", S),
                          ("hotwords_score", F), ("rule_fsts", S), ("rule_fars", S),
                          ("blank_penalty", F), ("hr", HomophoneReplacer))
RecognizerResult = struct("RecognizerResult", ("text", S), ("timestamps", P), ("count", I),
                          ("tokens", S), ("tokens_arr", P), ("json", S), ("lang", S),
                          ("emotion", S), ("event", S))  # (more fields follow; not read)

# Voice activity detection
VadSilero = struct("VadSilero", ("model", S), ("threshold", F), ("min_silence_duration", F),
                   ("min_speech_duration", F), ("window_size", I), ("max_speech_duration", F))
VadModelConfig = struct("VadModelConfig", ("silero_vad", VadSilero), ("sample_rate", I),
                        ("num_threads", I), ("provider", S), ("debug", I), ("ten_vad", VadSilero))
SpeechSegment = struct("SpeechSegment", ("start", I), ("samples", C.POINTER(F)), ("n", I))


def load_library(lib_dir):
    """Load sherpa-onnx-c-api.dll and declare the functions used."""
    lib_dir = Path(lib_dir).resolve()  # add_dll_directory needs an absolute path
    os.add_dll_directory(str(lib_dir))  # its onnxruntime.dll sits next to it
    lib = C.CDLL(str(lib_dir / "sherpa-onnx-c-api.dll"))
    for name, args, result in [
        ("SherpaOnnxCreateOfflineTts", [C.POINTER(TtsConfig)], P),
        ("SherpaOnnxOfflineTtsSampleRate", [P], I),
        ("SherpaOnnxOfflineTtsNumSpeakers", [P], I),
        ("SherpaOnnxOfflineTtsGenerateWithConfig",
         [P, S, C.POINTER(GenerationConfig), AudioCallback, P], C.POINTER(GeneratedAudio)),
        ("SherpaOnnxDestroyOfflineTtsGeneratedAudio", [C.POINTER(GeneratedAudio)], None),
        ("SherpaOnnxCreateOfflineRecognizer", [C.POINTER(RecognizerConfig)], P),
        ("SherpaOnnxCreateOfflineStream", [P], P),
        ("SherpaOnnxDestroyOfflineStream", [P], None),
        ("SherpaOnnxAcceptWaveformOffline", [P, I, C.POINTER(F), I], None),
        ("SherpaOnnxDecodeOfflineStream", [P, P], None),
        ("SherpaOnnxGetOfflineStreamResult", [P], C.POINTER(RecognizerResult)),
        ("SherpaOnnxDestroyOfflineRecognizerResult", [C.POINTER(RecognizerResult)], None),
        ("SherpaOnnxCreateVoiceActivityDetector", [C.POINTER(VadModelConfig), F], P),
        ("SherpaOnnxDestroyVoiceActivityDetector", [P], None),
        ("SherpaOnnxVoiceActivityDetectorAcceptWaveform", [P, C.POINTER(F), I], None),
        ("SherpaOnnxVoiceActivityDetectorFlush", [P], None),
        ("SherpaOnnxVoiceActivityDetectorEmpty", [P], I),
        ("SherpaOnnxVoiceActivityDetectorFront", [P], C.POINTER(SpeechSegment)),
        ("SherpaOnnxVoiceActivityDetectorPop", [P], None),
        ("SherpaOnnxDestroySpeechSegment", [C.POINTER(SpeechSegment)], None),
    ]:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
    return lib


def b(path):
    return str(path).encode()


# --- Audio helpers

def read_wav(data):
    """WAV bytes -> (float32 samples as an array, sample rate). 16-bit PCM;
    stereo is mixed to mono."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                raise HTTPError(400, "expected 16-bit PCM WAV")
            channels, rate = w.getnchannels(), w.getframerate()
            pcm = array.array("h", w.readframes(w.getnframes()))
    except (wave.Error, EOFError) as err:
        raise HTTPError(400, f"not a WAV file: {err}") from None
    if sys.byteorder == "big":
        pcm.byteswap()
    if channels > 1:
        pcm = array.array("h", (sum(pcm[i:i + channels]) // channels for i in range(0, len(pcm), channels)))
    return array.array("f", (x / 32768 for x in pcm)), rate


def resample(samples, rate, to_rate):
    """Linear-interpolation resampling: rough, but plenty for voice activity detection."""
    if rate == to_rate:
        return samples
    step, last = rate / to_rate, len(samples) - 1
    out = array.array("f", bytes(4 * int(len(samples) / step)))
    for i in range(len(out)):
        pos = i * step
        j = int(pos)
        frac = pos - j
        out[i] = samples[j] * (1 - frac) + samples[min(j + 1, last)] * frac
    return out


def float_buffer(samples):
    """A float32 array as a ctypes float* (no copy)."""
    return (F * len(samples)).from_buffer(samples)


def to_pcm16(ptr, n):
    """float* (n samples, -1..1) -> s16le bytes."""
    floats = (F * n).from_address(C.addressof(ptr.contents))
    pcm = array.array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in floats))
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm.tobytes()


def wav_bytes(pcm, rate):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def multipart_form(req, max_bytes):
    """Parse a multipart/form-data body: {name: bytes (files) or str (fields)}."""
    content_type = req.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPError(400, "expected multipart/form-data")
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode() + req.body(max_bytes))
    form = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            payload = part.get_payload(decode=True) or b""
            form[name] = payload if part.get_filename() else payload.decode("utf-8", "replace")
    return form


# --- Engines

# Kokoro v1.1's voice names, in speaker-id order (voices.bin itself has no names):
# hexgrad's voice files sorted by name, as listed in sherpa-onnx's docs for
# kokoro-multi-lang-v1_1. af/bf = American/British female; zf/zm = Chinese female/male.
KOKORO_V1_1_VOICES = (
    ["af_maple", "af_sol", "bf_vale"]
    + [f"zf_{n:03}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 17, 18, 19, 21, 22, 23, 24, 26, 27, 28, 32, 36,
                              38, 39, 40, 42, 43, 44, 46, 47, 48, 49, 51, 59, 60, 67, 70, 71, 72, 73,
                              74, 75, 76, 77, 78, 79, 83, 84, 85, 86, 87, 88, 90, 92, 93, 94, 99)]
    + [f"zm_{n:03}" for n in (9, 10, 11, 12, 13, 14, 15, 16, 20, 25, 29, 30, 31, 33, 34, 35, 37, 41, 45,
                              50, 52, 53, 54, 55, 56, 57, 58, 61, 62, 63, 64, 65, 66, 68, 69, 80, 81,
                              82, 89, 91, 95, 96, 97, 98, 100)])


# A voicepack: one 256-number style vector (float32) for each input length up to 510.
VOICE_BYTES = 510 * 256 * 4


def read_voicepack(path):
    """A voicepack's raw float32 data (little-endian, as in voices.bin): from a
    PyTorch .pt file (a zip; the tensor's data is its one data/0 entry), or a
    raw .bin of the same."""
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
    if len(raw) != VOICE_BYTES:
        raise SystemExit(f"error: {path}: {len(raw)} bytes of data, not a [510, 1, 256] float32 voicepack")
    return raw


class Kokoro:
    """Kokoro TTS. Generation streams sentence by sentence; one at a time.

    extra_voices: {name: voicepack file}. sherpa reads voices.bin once, at
    load, and only with the model's own voice count, so each takes the place
    of a stock voice, the last ones first, in a temporary copy of voices.bin
    (deleted once loaded); the downloaded file stays as it is."""

    def __init__(self, lib, model, threads, provider, extra_voices=None):
        d = Path(model).parent
        stock = d / "voices.bin"
        stock_count = stock.stat().st_size // VOICE_BYTES
        extra_voices = extra_voices or {}
        voices_file = stock
        if len(extra_voices) >= stock_count:
            raise SystemExit(f"error: {len(extra_voices)} extra voices, but only {stock_count} slots")
        first_extra = stock_count - len(extra_voices)  # the slots the extra voices take
        if extra_voices:
            data = bytearray(stock.read_bytes())
            for slot, path in enumerate(extra_voices.values(), first_extra):
                data[slot * VOICE_BYTES:(slot + 1) * VOICE_BYTES] = read_voicepack(path)
            merged = tempfile.NamedTemporaryFile(prefix="kokoro_voices_", suffix=".bin", delete=False)
            with merged:
                merged.write(data)
            voices_file = Path(merged.name)
        config = TtsConfig()
        k = config.model.kokoro
        k.model, k.voices, k.tokens = b(model), b(voices_file), b(d / "tokens.txt")
        k.data_dir, k.dict_dir = b(d / "espeak-ng-data"), b(d / "dict")
        k.lexicon = b(",".join(str(p) for p in sorted(d.glob("lexicon-*.txt"))))
        k.length_scale = 1.0
        config.model.num_threads, config.model.provider = threads, provider.encode()
        config.rule_fsts = b(",".join(str(p) for p in sorted(d.glob("*.fst"))))
        config.max_num_sentences = 1  # one sentence per callback: the streaming granularity
        self.lib = lib
        self.tts = lib.SherpaOnnxCreateOfflineTts(C.byref(config))
        if voices_file != stock:
            voices_file.unlink(missing_ok=True)  # read by now (or failed to load)
        if not self.tts:
            raise SystemExit(f"error: could not load Kokoro from {model}")
        self.sample_rate = lib.SherpaOnnxOfflineTtsSampleRate(self.tts)
        self.speakers = lib.SherpaOnnxOfflineTtsNumSpeakers(self.tts)
        # Stock names when the model is v1.1 (same voice count); otherwise ids
        # only. The extra voices' names replace those of the slots they took.
        names = list(KOKORO_V1_1_VOICES) if stock_count == len(KOKORO_V1_1_VOICES) else []
        names = names or [str(sid) for sid in range(stock_count)]
        self.replaced = names[first_extra:]  # the stock voices given up, for the log
        self.voices = names[:first_extra] + list(extra_voices)
        self.lock = threading.Lock()

    def speaker_id(self, voice):
        """A voice name (e.g. "zf_001") or speaker id ("3") -> id, or None if unknown."""
        voice = str(voice)
        if voice in self.voices:
            return self.voices.index(voice)
        if voice.isdigit() and int(voice) < self.speakers:
            return int(voice)
        return None

    def generate(self, text, sid, speed, on_audio):
        """Generate speech; on_audio(pcm bytes) is called per sentence and
        returns False to stop early."""
        def callback(samples, n, progress, arg):
            return 1 if on_audio(to_pcm16(samples, n)) else 0

        gen = GenerationConfig(silence_scale=0.2, speed=speed, sid=sid)
        with self.lock:
            audio = self.lib.SherpaOnnxOfflineTtsGenerateWithConfig(
                self.tts, text.encode(), C.byref(gen), AudioCallback(callback), None)
            if audio:
                self.lib.SherpaOnnxDestroyOfflineTtsGeneratedAudio(audio)


class SenseVoice:
    """SenseVoice STT (Chinese, English, Japanese, Korean, Cantonese)."""

    LANGUAGES = {"zh": "chinese", "en": "english", "ja": "japanese", "ko": "korean", "yue": "cantonese"}

    def __init__(self, lib, model, threads, provider):
        config = RecognizerConfig()
        config.feat_config.sample_rate, config.feat_config.feature_dim = 16000, 80
        m = config.model_config
        m.sense_voice.model, m.sense_voice.language, m.sense_voice.use_itn = b(model), b"auto", 1
        m.tokens = b(Path(model).parent / "tokens.txt")
        m.num_threads, m.provider = threads, provider.encode()
        config.decoding_method = b"greedy_search"
        self.lib = lib
        self.recognizer = lib.SherpaOnnxCreateOfflineRecognizer(C.byref(config))
        if not self.recognizer:
            raise SystemExit(f"error: could not load SenseVoice from {model}")

    def transcribe(self, samples, rate):
        """-> (text, language code such as "zh", or "")"""
        lib = self.lib
        stream = lib.SherpaOnnxCreateOfflineStream(self.recognizer)
        try:
            lib.SherpaOnnxAcceptWaveformOffline(stream, rate, float_buffer(samples), len(samples))
            lib.SherpaOnnxDecodeOfflineStream(self.recognizer, stream)
            result = lib.SherpaOnnxGetOfflineStreamResult(stream)
            try:
                r = result.contents
                text = (r.text or b"").decode("utf-8", "replace").strip()
                lang = (r.lang or b"").decode().strip("<|>")
            finally:
                lib.SherpaOnnxDestroyOfflineRecognizerResult(result)
        finally:
            lib.SherpaOnnxDestroyOfflineStream(stream)
        return text, lang


class SileroVad:
    """Silero VAD over a whole clip. A detector is made per request (it's
    stateful, and settings can vary per request); the model is small."""

    SAMPLE_RATE = 16000
    WINDOW = 512  # samples per step (32 ms), as Silero expects at 16 kHz
    DEFAULTS = {"threshold": 0.5, "min_speech_duration_ms": 250, "min_silence_duration_ms": 100,
                "max_speech_duration_s": 30.0, "speech_pad_ms": 30}

    def __init__(self, lib, model, provider):
        self.lib, self.model, self.provider = lib, model, provider

    def segments(self, samples, settings):
        """-> [(start, end)] in seconds."""
        lib = self.lib
        config = VadModelConfig(sample_rate=self.SAMPLE_RATE, num_threads=1, provider=self.provider.encode())
        s = config.silero_vad
        s.model, s.window_size = b(self.model), self.WINDOW
        s.threshold = settings["threshold"]
        s.min_speech_duration = settings["min_speech_duration_ms"] / 1000
        s.min_silence_duration = settings["min_silence_duration_ms"] / 1000
        s.max_speech_duration = settings["max_speech_duration_s"]
        duration = len(samples) / self.SAMPLE_RATE
        vad = lib.SherpaOnnxCreateVoiceActivityDetector(C.byref(config), duration + 1)
        if not vad:
            raise RuntimeError("could not create the voice activity detector")
        found = []
        try:
            base = C.addressof(float_buffer(samples))
            for start in range(0, len(samples), self.WINDOW):
                n = min(self.WINDOW, len(samples) - start)
                window = C.cast(base + start * C.sizeof(F), C.POINTER(F))
                lib.SherpaOnnxVoiceActivityDetectorAcceptWaveform(vad, window, n)
            lib.SherpaOnnxVoiceActivityDetectorFlush(vad)
            while not lib.SherpaOnnxVoiceActivityDetectorEmpty(vad):
                segment = lib.SherpaOnnxVoiceActivityDetectorFront(vad)
                found.append((segment.contents.start, segment.contents.n))
                lib.SherpaOnnxDestroySpeechSegment(segment)
                lib.SherpaOnnxVoiceActivityDetectorPop(vad)
        finally:
            lib.SherpaOnnxDestroyVoiceActivityDetector(vad)

        # Pad each segment (as Silero's speech_pad_ms), merging any that then overlap.
        pad = settings["speech_pad_ms"] / 1000
        merged = []
        for start, n in found:
            begin = max(0.0, start / self.SAMPLE_RATE - pad)
            end = min(duration, (start + n) / self.SAMPLE_RATE + pad)
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((begin, end))
        return [(round(a, 3), round(e, 3)) for a, e in merged]


# --- Web app

app = App()
engines = {}  # "tts", "stt", "vad" -> engine, set in main()
MAX_UPLOAD = 50 * 1024 * 1024
PCM_TYPE = "audio/pcm"


def engine(name):
    if name not in engines:
        raise HTTPError(404, f"no {name} model loaded (see --help)")
    return engines[name]


@app.get("/health")
def health(req):
    return {"status": "ok", "engines": sorted(engines)}


@app.get("/v1/audio/voices")
def voices(req):
    tts = engine("tts")
    return {"voices": tts.voices}


@app.post("/v1/audio/speech")
def speech(req):
    tts = engine("tts")
    data = req.json()
    if not isinstance(data, dict) or not isinstance(data.get("input"), str) or not data["input"].strip():
        raise HTTPError(400, 'expected JSON {"input": "...", "voice": "<name or speaker id>"}')
    text = data["input"].strip()
    voice = str(data.get("voice", "0"))
    sid = tts.speaker_id(voice)
    if sid is None:
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
        tts.generate(text, sid, speed, lambda pcm: pieces.append(pcm) or True)
        return Stream([wav_bytes(b"".join(pieces), tts.sample_rate)], "audio/wav")

    # Streamed: generate in a worker thread, relaying each sentence as it's done.
    chunks = queue.Queue()
    stop = threading.Event()

    def work():
        try:
            tts.generate(text, sid, speed, lambda pcm: chunks.put(pcm) or not stop.is_set())
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
            log.info("spoke %.1fs of audio in %.2fs (voice %s): %r", total / 2 / tts.sample_rate,
                     time.monotonic() - start, voice, text)

    return Stream(relay(), f"{PCM_TYPE}; rate={tts.sample_rate}; channels=1")


@app.post("/inference")
def inference(req):
    stt = engine("stt")
    form = multipart_form(req, MAX_UPLOAD)
    if not isinstance(form.get("file"), bytes):
        raise HTTPError(400, 'expected a WAV in multipart field "file"')
    samples, rate = read_wav(form["file"])
    start = time.monotonic()
    text, lang = stt.transcribe(samples, rate)
    seconds = time.monotonic() - start
    duration = len(samples) / rate
    log.info("transcribed %.1fs of audio in %.2fs (%s): %r", duration, seconds, lang, text)
    if form.get("response_format") == "text":
        return text
    language = SenseVoice.LANGUAGES.get(lang, lang)
    # The fields of whisper-server's verbose_json that the dashboard uses, plus timing.
    return {"task": "transcribe", "language": language, "duration": round(duration, 3), "text": text,
            "detected_language": language, "processing_seconds": round(seconds, 3)}


@app.post("/vad")
def vad(req):
    detector = engine("vad")
    form = multipart_form(req, MAX_UPLOAD)
    if not isinstance(form.get("file"), bytes):
        raise HTTPError(400, 'expected a WAV in multipart field "file"')
    settings = dict(SileroVad.DEFAULTS)
    for key, default in SileroVad.DEFAULTS.items():
        if key in form:
            try:
                settings[key] = type(default)(form[key])
            except ValueError:
                raise HTTPError(400, f"{key} must be a number") from None
    samples, rate = read_wav(form["file"])
    samples = resample(samples, rate, SileroVad.SAMPLE_RATE)
    found = detector.segments(samples, settings)
    return {"duration": round(len(samples) / SileroVad.SAMPLE_RATE, 3),
            "segments": [{"start": start, "end": end} for start, end in found]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lib", required=True, type=Path,
                        help=f"folder with sherpa-onnx-c-api.dll (sherpa-onnx v{SHERPA_VERSION})")
    parser.add_argument("--kokoro", type=Path, help="Kokoro TTS model.onnx")
    parser.add_argument("--voice", action="append", default=[], metavar="NAME=FILE",
                        help="an extra Kokoro voicepack (.pt or raw .bin, [510, 1, 256]) spoken as NAME; repeatable")
    parser.add_argument("--sense-voice", type=Path, help="SenseVoice STT model.onnx or model.int8.onnx")
    parser.add_argument("--vad", type=Path, help="Silero VAD silero_vad.onnx")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8084)
    parser.add_argument("--threads", type=int, default=4, help="CPU threads per engine (default: 4)")
    parser.add_argument("--provider", default="cpu", help="onnxruntime provider: cpu or cuda (default: cpu)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    if not (args.kokoro or args.sense_voice or args.vad):
        parser.error("give at least one of --kokoro, --sense-voice, --vad")
    for path in (args.kokoro, args.sense_voice, args.vad):
        if path and not path.is_file():
            raise SystemExit(f"error: {path} not found")
    extra_voices = {}
    for spec in args.voice:
        name, _, file = spec.partition("=")
        if not name or not file:
            parser.error(f"--voice {spec!r}: expected NAME=FILE")
        if name in KOKORO_V1_1_VOICES or name in extra_voices:
            parser.error(f"--voice {name}: that name is already taken")
        if not Path(file).is_file():
            raise SystemExit(f"error: voicepack {file} not found")
        extra_voices[name] = Path(file)
    if extra_voices and not args.kokoro:
        parser.error("--voice needs --kokoro")

    lib = load_library(args.lib)
    if args.kokoro:
        start = time.monotonic()
        engines["tts"] = Kokoro(lib, args.kokoro, args.threads, args.provider, extra_voices)
        tts = engines["tts"]
        log.info("Kokoro loaded in %.1fs: %d voices, %d Hz", time.monotonic() - start, tts.speakers,
                 tts.sample_rate)
        for name, replaced in zip(extra_voices, tts.replaced):
            log.info("voice %s: from %s, in place of %s", name, extra_voices[name], replaced)
    if args.sense_voice:
        start = time.monotonic()
        engines["stt"] = SenseVoice(lib, args.sense_voice, args.threads, args.provider)
        log.info("SenseVoice loaded in %.1fs", time.monotonic() - start)
    if args.vad:
        engines["vad"] = SileroVad(lib, args.vad, args.provider)
    app.run(args.host, args.port,
            on_ready=lambda: log.info("listening on http://%s:%d/", args.host, args.port))


if __name__ == "__main__":
    main()
