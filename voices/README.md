# Reference voices

Kokoro uses voices bundled with its downloaded model. The v1.1 defaults are
`af_maple` for English and `zf_001` for Chinese; the v1.0 fragment selects its
own stock voices, including `jf_alpha` for Japanese. No custom voicepacks are shipped.

Qwen3-TTS uses the reference WAV linked in the
[official Qwen3-TTS README](https://github.com/QwenLM/Qwen3-TTS#tokenizer-encode-and-decode):
[tokenizer_demo_1.wav](https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/tokenizer_demo_1.wav),
registered as `qwen_demo`. It is mono, 24 kHz, 16-bit PCM, about 10.53 seconds.

Configure another reference in `config_local.toml`:

```toml
[api_servers.qwentts]
default_voice = "my_voice"

[api_servers.qwentts.voices]
my_voice = "https://example.com/reference.wav"
# A local path also works: my_voice = "voices/my_voice/reference.wav"
```

The configured qwentts.cpp release accepts WAV bytes (`wav_b64`), rather than
a URL. The launcher downloads HTTP(S) references once into `voices/cache/`,
keyed by URL, then uploads the WAV through the existing registration endpoint.
Later launches reuse the cached file offline. To refresh an unchanged URL,
remove its cached file. A failed download leaves no completed cache entry.
The server keeps registered voices in memory, so registration occurs at each
server start. This uses speaker-embedding cloning and needs no transcript.

## Local smoke-test fixtures

Run `python src/tools/generate_test_audio.py` explicitly with the servers running
to create `voices/test_audio/english.wav`, `chinese.wav`, `japanese.wav` and
`transcripts.json`. The tool defaults to the configured Qwen reference; `--source`
accepts a local WAV or HTTP(S) URL, and `--voice` overrides the configured TTS voices.
Whisper detects the source language, then the tool translates and synthesizes
each fixture language. Use Qwen or another setup that speaks all fixture languages.

Downloaded voices and generated fixtures are ignored by Git. No reference recordings,
custom voicepacks or generated test recordings are bundled in this repository.
