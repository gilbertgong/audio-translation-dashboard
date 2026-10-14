# Installing on a Mac

A first attempt: nothing here has been tried on a real Mac yet. It's written for an **Apple Silicon** Mac (M1 or
later), with at least 16 GB of memory for comfort (8 GB works with smaller models). Expect to need the
troubleshooting section.

What's different from Windows: whisper.cpp publishes no macOS server build, so whisper and llama come from Homebrew
(built for Metal, the Mac's GPU) instead of our downloader; `setup_mac.sh` links them in and writes the config,
which the launcher's **Generate config** can't do on a Mac (it asks nvidia-smi). Qwen3-TTS has no macOS build, so
speech is Kokoro (on the CPU, which is quick on Apple Silicon). Everything else is the same Python as on Windows.

## Steps

1. **Python 3.12** from [python.org](https://www.python.org/downloads/macos/) (the macOS installer). Better than
   Homebrew's here: it includes Tk (for the launcher window) and lets pip install packages.
   - After installing, run **Install Certificates.command** in `/Applications/Python 3.12/` (double-click it).
     Without it, every download fails with `CERTIFICATE_VERIFY_FAILED`.
2. **Homebrew**, if it isn't there: the one-line install from [brew.sh](https://brew.sh), and the two lines it prints
   at the end to add it to the PATH.
3. **The project folder.** Clone it, or copy it from the PC **without** `api_servers/`, `logs/`, `tmp/`,
   `config_local.toml` and `config_generated.toml` (all per machine). `models/` can come along: the model files work
   on any system and save several GB of downloads, as long as they're the ones the Mac's config asks for.
4. **Setup**, in Terminal, from the project folder:
   ```bash
   bash setup_mac.sh
   ```
   It checks Python, runs `brew install whisper-cpp llama.cpp`, links `whisper-server` and `llama-server` into
   `api_servers/`, and writes `config_generated.toml` by the Mac's memory:

   | Memory | whisper | Translation | Speech |
   |---|---|---|---|
   | 32 GB or more | large-v3 | Qwen3.8-27B IQ3_XXS | Kokoro (Beth) |
   | 16-31 GB | large-v3 q5_0 | Qwen3.5-9B Q3_K_XL | Kokoro (Beth) |
   | under 16 GB | large-v3-turbo q5_0 | Qwen3.5-9B IQ3_XXS | Kokoro (Beth) |

   Languages are English and Chinese; for another pair, add to the top of `config_generated.toml`:
   ```toml
   [languages]
   language1 = "English"
   language2 = "Japanese"
   ```
5. **Run**, with the command `setup_mac.sh` prints at the end, e.g.:
   ```bash
   PYTHON=/usr/local/bin/python3.12 bash launch.sh
   ```
   The first run installs Kokoro's packages (about 800 MB) and downloads the models (several GB), then starts the
   dashboard and the servers. Open **http://127.0.0.1:8080/** in **Chrome**. Ctrl+C in Terminal stops everything.
6. **The launcher (optional), once step 5 works:**
   ```bash
   /usr/local/bin/python3.12 launcher.pyw
   ```
   Launch, Stop, the server buttons and Kill servers should work; **Generate config doesn't** (no nvidia-smi):
   rerun `setup_mac.sh` instead, after deleting `config_generated.toml`.

## Checking each piece

Logs are in `logs/`: one per server (`whisper.log`, `llama.log`, `kokoro.log`) plus `app.log`. Each server can also
be started alone, which shows its errors directly:

```bash
PY=/usr/local/bin/python3.12
$PY src/install_api_servers.py          # what's installed; installs Kokoro's packages
$PY src/download_models.py              # downloads what's missing
$PY src/run_servers.py --only whisper   # one server, Ctrl+C to stop; also: llama, kokoro
curl http://127.0.0.1:8081/health       # whisper; llama is 8082, Kokoro 8083
```

`src/static/smoke.html` (the **Smoke test** link on the test page, http://127.0.0.1:8080/test.html) runs the whole
pipeline on recorded audio with no microphone: the quickest way to see that everything works together.

## Troubleshooting

**Python and setup**
- `CERTIFICATE_VERIFY_FAILED` (or setup says Python can't open huggingface.co): python.org's Python needs
  **Install Certificates.command** (step 1).
- `error: externally-managed-environment` while installing Kokoro's packages: that's Homebrew's Python refusing pip.
  Use python.org's Python, or run once with `PIP_BREAK_SYSTEM_PACKAGES=1` in front of the command (safe here: the
  packages go into `api_servers/kokoro/lib`, not into that Python).
- `bash\r: No such file or directory` or `$'\r': command not found`: the scripts got Windows line endings on the way
  (a copy through Windows tools). Fix with `sed -i '' 's/\r$//' setup_mac.sh launch.sh`.
- `Permission denied` on `./launch.sh`: the copy lost the executable bit; run it as `bash launch.sh`.
- `No module named '_tkinter'` from the launcher: Homebrew's Python without Tk (`brew install python-tk@3.12`), or
  use python.org's.

**whisper and llama (Homebrew)**
- setup says `whisper-server not found`: Homebrew may name it differently in some versions; the error lists the
  whisper programs it has. Link the server one by hand:
  `ln -sf "$(brew --prefix)/bin/<its name>" api_servers/whisper/whisper-server`.
- whisper or llama exits at once: run it alone (`run_servers.py --only whisper`) and read `logs/whisper.log`. An
  `unknown argument` means Homebrew's version differs from the one the config was written for: remove that option
  from `args` in `config_generated.toml`.
- The first request after starting is slow (a few seconds): Metal compiles its shaders on first use. Later ones
  should be quick.
- llama runs out of memory, or the Mac swaps hard: use the next smaller row of the table (edit the fragments named
  at the top of `config_generated.toml`, or delete it, and rerun `setup_mac.sh` after editing its thresholds).
  macOS lets the GPU use only about two thirds to three quarters of the memory.
- After `brew upgrade`, nothing to redo: the links point at Homebrew's current version.

**Kokoro**
- pip fails building a package (`error: command 'clang' failed`, or `xcrun: error`): a package had no prebuilt
  wheel for this Mac and needs a compiler: `xcode-select --install`, then run again. The likeliest candidates are
  `mojimoji` and `fugashi` (Japanese).
- Kokoro starts but English fails with an espeak error: `espeakng-loader` brought no library for this Mac.
  `brew install espeak-ng` may be enough; otherwise, tell me what the log says.
- `Kokoro's packages are missing`: the install didn't finish; run `$PY src/install_api_servers.py --only kokoro`
  and read its output.

**In the browser**
- Use **Chrome**. The first time, macOS asks whether Chrome may use the microphone: allow it (if it was refused:
  System Settings → Privacy & Security → Microphone → Chrome on).
- **Capture audio** on a Mac: share a **Chrome tab** and tick **Also share tab audio**. Chrome on macOS can't share
  system audio or a window's audio, only a tab's.
- Nothing at http://127.0.0.1:8080/: the dashboard didn't start; see `logs/app.log` and the Terminal output.
