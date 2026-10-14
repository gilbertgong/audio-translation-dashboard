# Beth Thomas voicepack: RC3

`beth_thomas_rc3.pt`: a Kokoro-82M v1.1-zh voicepack (`[510, 1, 256]`), the model unchanged. The first release
candidate from series 2 (typicality B1 and the quality term in the search).

## Provenance
- **Series 2 run S2R9, generation 195** (`runs/beth_thomas_s2/S2R9/gen195.pt`, identical copy), the last
  checkpoint of a 200-generation run, and the best of that run by the outside judge.
- CMA-ES over the whole style vector (all 256 numbers), starting from the quality-chosen blend S3.
  - **Objective:** identity 0.45 × the lower + 0.25 × the higher of ECAPA / WavLM-SV similarity to Beth's training
    recordings; typicality (reference set B1, all 256 numbers) at 0.1; the light quality term (naturalness, Chinese
    pronunciation and tones) at 0.3. No proxy and no prosody term.
  - The outside judge, ReDimNet, was never optimized against.
- Series 2 setup: nine training books, three held-out books (`docs/series2_plan.md`).

## Scores
**Identity** (held-out books, gap left to the training-recording ruler; lower is closer):

| ECAPA | WavLM-SV | ReDimNet (outside judge) |
|---|---|---|
| 13.4% | 5.8% | **28.3%** |

(Normalized similarity on the outside judge 0.72.) RC1 and RC2 scored about 29% on the same ruler. Among series-2
voices with the quality term in the search it is the closest; S2R1-S2R6, which had none, reached 20-23%.

**Quality** (full battery, in stock-voice units; 0 is the stock-voice average, 1 is one spread above):
combined **0.87**; naturalness 1.03 (UTMOS 4.56, the best stock voice 4.51); Chinese 1.32 (pronunciation and tones);
Japanese intonation and rhythm -0.33 (the non-native average is 0).

**Typicality** (set B1, all 256 numbers): -0.074; rms z 0.96. Against the Beth-prosody center ("B1 BP"): -0.763.

**Intelligibility** (measured with the Hugging Face Open TTS Leaderboard protocol, run locally; see
`testing/leaderboard_compare/PROJECT.md` in the working tree): English WER 1.48% (Kokoro's `af_heart`: 1.48% on our
rerun), Chinese CER 1.19%, Japanese CER 16.3% (stock voices on v1.1-zh: 18.2-20.3%).

## Known issues
- **Its prosody half has drifted from Beth's.** Prosody distance to her 1.57 on the predictor path (RC2: 0.36), 1.85 on
  the audio path (40 sentences; Qwen3-TTS clone 0.56, RC2 0.61). The search had no prosody term, and likeness doesn't
  hear delivery. Grafting a fitted Beth prosody half onto it kept likeness in a test but cost Chinese tones and
  naturalness.
- **Japanese intonation and rhythm are below the non-native average.** It is the most intelligible Japanese of the
  voices measured, but not natural.
- Identity is a search result on one speaker's recordings: ReDimNet 28.3% gap left, well short of a zero-shot clone's.

## Use
The voicepack works only with **Kokoro-82M v1.1-zh** (`hexgrad/Kokoro-82M-v1.1-zh`); it was searched on that model and
is not meant for the original v1.0 model. It is a plain PyTorch tensor of shape `[510, 1, 256]` (one 256-number style
vector per input length, the first 128 numbers for the decoder and the last 128 for the prosody predictor).

**PyTorch `kokoro`** (`pip install kokoro misaki[en,zh,ja]`):

```python
import torch, soundfile as sf
from kokoro import KModel, KPipeline

REPO = "hexgrad/Kokoro-82M-v1.1-zh"
model = KModel(repo_id=REPO).eval()                       # add .to("cuda") to use a GPU
pack = torch.load("beth_thomas_rc3.pt", weights_only=True)  # the voicepack: pass the tensor as voice=

def speak(text, lang):   # lang: "a" American English, "z" Mandarin Chinese, "j" Japanese
    pipe = KPipeline(lang_code=lang, repo_id=REPO, model=model)
    return torch.cat([r.audio for r in pipe(text, voice=pack)])

sf.write("english.wav", speak("The quick brown fox jumps over the lazy dog.", "a").numpy(), 24000)
sf.write("chinese.wav", speak("今天天气很好，我们一起去公园散步吧。", "z").numpy(), 24000)
sf.write("japanese.wav", speak("今日はとても良い天気ですね。", "j").numpy(), 24000)
```

Output is 24 kHz mono. Voicepacks are not mixed across models: use this file with v1.1-zh only.

**In this project:** `src/kokoro_server.py` loads it as `beth_thomas`, the Kokoro default voice for English, Chinese
and Japanese (`[api_servers.kokoro.voices]` and `language_voices` in `config/defaults.toml`, passed at start as
`--voice beth_thomas=voices/beth_thomas/RC3/beth_thomas_rc3.pt`). The server runs sherpa-onnx's fp32 ONNX export of v1.1-zh
(`kokoro-multi-lang-v1_1`) with onnxruntime, behind misaki, the same front end as `KPipeline` above, so it sounds the
same as the PyTorch render.

**Not sherpa-onnx's own engine:** its text front end differs from misaki's (missing words, no tone sandhi, its own
English pronunciations), and this voice, searched on misaki's phonemes, sounds bad through it, Chinese most of all.
Its `voices.bin` also can't simply have the pack appended: sherpa-onnx checks the file's size against the model's own
voice count (103), so an extra voice can only replace a stock one.
