# TODO

- [x] **Language direction — highest priority.** Use Whisper's final language after any recheck to fill
  explicit source/target prompt fields. Remove llama language tags and its recheck trigger.
- [x] **Session retention defaults.** Automatically deleted temporary WAVs and no transcript/translation
  content in application logs. Keep activity/diagnostics and API-server logs. Config-only opt-in
  restores saved files and content logging; rechecks use the original server-side temporary file.
- [x] **Typed text translations.** Chat page styled like the dashboard, with explicit language
  direction, conversation history, streamed replies, copy and spoken playback.
- [x] **Advanced controls.** Playback panel with transport controls, output meter, queue status and independent language speed sliders.
- [x] **Launcher app updates and version checker.** `app.version` records separate `appVersion` and
  `launcherVersion`, plus download/master URLs. ZIP app updates leave the launcher and user data
  intact; newer launcher versions are informational and point to a full download. Publish a matching
  manifest/release to use it. Automatic launcher replacement remains future work.
- [x] **Kokoro Chinese voice.** Default Chinese voice is now the stock `zf_001` voice (Kokoro v1.1).
- [ ] **6 GB recipe tuning.** Investigate translation quality and whether it needs a better llama
  model, considering the existing priority on Whisper transcription quality and the VRAM budget.
