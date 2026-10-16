# TODO

- [x] **Language direction — highest priority.** Use Whisper's final language after any recheck to fill
  explicit source/target prompt fields. Remove llama language tags and its recheck trigger.
- [ ] **Confidential mode.** No logs or saved session WAV files; cover the dashboard, launcher and
  engine output, and preserve transcription rechecks without saving recordings to disk.
- [x] **Typed text translations.** Chat page styled like the dashboard, with explicit language
  direction, conversation history, streamed replies, copy and spoken playback.
- [ ] **Advanced controls.** Include a manual playback speed control for spoken translations.
- [ ] **Launcher updates and version checker.** Support ZIP installations with a JSON `.version`
  file containing `version`, `directDownloadURL` and `masterVersionFile`. Compare the local version
  with the published master file; offer Update and restart. Stage the release and hand off to a
  temporary updater outside the installation folder to preserve user data and restart the launcher.
- [ ] **Kokoro Chinese voice.** Change the default Chinese voice; replacement to be selected.
- [ ] **6 GB recipe tuning.** Investigate translation quality and whether it needs a better llama
  model, considering the existing priority on Whisper transcription quality and the VRAM budget.
