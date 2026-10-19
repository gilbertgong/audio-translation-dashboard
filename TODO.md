# TODO

- [x] **Language direction — highest priority.** Use Whisper's final language after any recheck to fill
  explicit source/target prompt fields. Remove llama language tags and its recheck trigger.
- [ ] **Confidential mode.** No logs or saved session WAV files; cover the dashboard, launcher and
  engine output, and preserve transcription rechecks without saving recordings to disk.
- [x] **Typed text translations.** Chat page styled like the dashboard, with explicit language
  direction, conversation history, streamed replies, copy and spoken playback.
- [ ] **Advanced controls.** Include a manual playback speed control for spoken translations.
- [x] **Launcher app updates and version checker.** `app.version` records separate `appVersion` and
  `launcherVersion`, plus download/master URLs. ZIP app updates leave the launcher and user data
  intact; newer launcher versions are informational and point to a full download. Publish a matching
  manifest/release to use it. Automatic launcher replacement remains future work.
- [ ] **Kokoro Chinese voice.** Change the default Chinese voice; replacement to be selected.
- [ ] **6 GB recipe tuning.** Investigate translation quality and whether it needs a better llama
  model, considering the existing priority on Whisper transcription quality and the VRAM budget.
