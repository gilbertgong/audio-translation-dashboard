# Memory: start here

## Start here

1. Read `AI/PLAN.md`: where the project stands now.
2. Read `AI/LOCAL.md` if it exists: this computer's setup and local rules.
3. Read `AI/PROJECT_LOG.md` when history matters: before trying something that may have been tried, or to understand
   how a decision came about.
4. Read `AI/STRATEGY.md` when changing how these docs are organized.

## Map of the docs

- `README.md`: for people. What it is, how to install and run it.
- `DESIGN.md`: how it works, in detail: architecture, endpoints, flow, configuration, scripts, known limitations,
  extension points. Kept current with the code.
- `INSTALL_MAC.md`: step-by-step macOS install, for people.
- `config/defaults.toml`: every setting, documented in its comments.
- `voices/beth_thomas/README.md`: the default voice's source, attribution and voicepack notes.
- `AI/`: for assistants (this file, the plan, the log, the strategy, and the git-ignored `LOCAL.md`).

## Rules that keep these docs working

### Remember in the repo
Save the user's preferences here, the current state in `AI/PLAN.md`, history in `AI/PROJECT_LOG.md`, and anything
about one computer in `AI/LOCAL.md`, not in an assistant's own memory.
*Why:* the next session may be another assistant, account or computer; only the repo travels.

### Keep the docs current without asking
Each change includes updating what it affects: `DESIGN.md` (it describes the code in detail, so most changes touch
it), `README.md`, `config/defaults.toml` comments, the plan, the log and this file.
*Why:* a stale doc misleads the next session more than a missing one.

### README for people, `AI/` for assistants
`README.md`, `DESIGN.md` and `INSTALL_MAC.md` are for people; reasons, history and working rules go in `AI/`.

### Keep the shared docs self-contained
Nothing local or outside the repo in shared docs: no local paths, machine setup or neighbouring folders. That goes in
`AI/LOCAL.md`, which is git-ignored. Reference hardware may be cited for measurements (declared in `AI/PLAN.md`).
*Why:* the repo is meant to be shared; local details are wrong elsewhere and can expose private information.

## How to work here

### The user commits
Don't commit or push; leave changes in the working tree for the user to review and commit.
*Why:* the user reviews and commits everything themselves.

### Discuss before building when asked
When the user asks a question or says "let's discuss", answer and propose; build only once they choose.
*Why:* several requests turned out to be explorations, not orders.

### Answer questions, don't act
"What did you use", "which is it", "double check X" are requests for facts. Answer; if an improvement suggests
itself, mention it, but change nothing until asked.
*Why:* a test sentence was once swapped after the user only asked what it was; they had liked it and it had to be
reverted.

### Do the chosen option only
When the user picks one of several offered options, do that one; don't add another as a "first step". If a skipped
option seems to matter, say so in one line and ask.
*Why:* an unrequested 17-minute calibration run was started before the option the user actually chose.

### Solve observed problems only
Don't design around hypothetical problems (e.g. speaker feedback into the microphone) until they're actually seen.
Mention a risk once at most.
*Why:* the user prefers minimal features and real evidence; speculative fixes have steered designs wrongly before.

### Styling: just edit
For visual and layout changes, make the edit; the user checks it in the Mock dashboard.
*Why:* they judge the look themselves faster than any description.
Skip testing for layout-only changes, per the user's explicit preference.

### Testing
- Test on a separate mock dashboard (`python src/app.py --mock` on another port, with a scratch config), never on
  the user's running dashboard or servers, and stop it afterwards. Don't stop or restart anything the user is
  running.
- Before a test writes a file the user may have made (`config_generated.toml`, `config_local.toml`, anything
  git-ignored), check whether it exists, back it up and restore it, or use a scratch path.
- Before any GPU use, check `nvidia-smi` for which GPUs exist and what's busy, and target one explicitly (CUDA's
  device order can differ from nvidia-smi's).
- Run scripts as designed, with their normal limits; fix the script rather than surveying every input. While the
  user is still shaping a script, wait for them to say to run it.
- When the user says usage is running high, skip testing and say what wasn't tested.
*Why:* tests have overwritten a user's config and competed with their GPU jobs; the user pays for usage.

### Trust the user's ear
The user tunes Kokoro voicepacks by hand and judges audio quality. When they say a model or voice can do something,
take it as established and look for what in the pipeline stops it.
*Why:* documentation said Kokoro v1.1-zh couldn't speak Japanese; the user was right, and the gap was our front end.

### Code and files
- LF line endings everywhere (`.gitattributes` enforces it).
- Standard-library Python only for the app and scripts; engine packages go into each server's own folder.
- Match the surrounding code's style and comment density; keep `config/defaults.toml` comments as the settings'
  documentation.
- TODOs go in the code where they apply, and in `AI/PLAN.md` next steps.
