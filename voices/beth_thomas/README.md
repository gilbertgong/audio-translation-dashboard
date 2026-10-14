# Voices

Reference recordings for text-to-speech voice cloning. Each WAV is registered
with tts-server under a name, then selected by that name when speaking.

Cloning uses the speaker-embedding mode: the server takes the voice's timbre
from the whole recording, so no transcript is needed. A clean recording of a
single speaker, about 10–30 seconds, works well.

| File | Source |
|------|--------|
| `beth_thomas.wav` | Read by Beth Thomas for LibriVox (*The Art of Kissing*, chapter 7). The first 30 s of [artofkissing_7_rossiter_128kb.mp3](https://dn721504.ca.archive.org/0/items/art_of_kissing_1512_librivox/artofkissing_7_rossiter_128kb.mp3), as mono 16 kHz WAV. LibriVox recordings are in the public domain. |

## Test audio

`test_audio/` has known speech for testing the dashboard, made from
`beth_thomas.wav` by `src/tools/generate_test_audio.py` (run it again to regenerate):
whisper transcribed it, llama translated it, and Qwen3-TTS spoke each sentence
in the cloned voice, with pauses between sentences.

| File | Contents |
|------|----------|
| `test_audio/beth_chinese.wav` | Simplified Chinese, about 18 s |
| `test_audio/beth_japanese.wav` | Japanese, about 18 s |
| `test_audio/transcripts.json` | The English transcription and the translated sentences |

## Beth Thomas (1974–2020)

[Beth Thomas](https://librivox.org/reader/6281) was a prolific LibriVox
contributor, and thus a contributor to the common good. This repo intends only
the greatest respect and appreciation in utilizing her voice. Speech generated
with it is synthetic: it is not her words, and does not reflect her views.
