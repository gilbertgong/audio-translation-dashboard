#!/usr/bin/env python3
"""Audio Translation Dashboard - launcher.

A window that does what launch.sh does: installs the servers and downloads
their models if needed, then starts the dashboard and the servers (the
dashboard first, so its header shows them coming up), showing their output.
Stop, or closing the window, stops everything.

With [launcher] show_dev_controls = true in config_local.toml, more
controls: Mock runs the dashboard alone with sample data instead (what mock.sh
does); Launch and Mock each stop the other first, since both use the
dashboard's port. Restart app restarts just the dashboard, to pick up changes
to it, keeping the servers running (run_servers.py restarts it when told
"restart" on its input); for Mock, it restarts the mock. And a row of server
buttons, one per server Launch starts (start = true in the merged settings),
each starting or stopping its server and showing its state as reported back.
Before a launch (or after), a server is started on its own, with Launch's
steps for just that server (install_api_servers.py, download_models.py and
run_servers.py, each with --only NAME); a later Launch uses it as it is and
leaves it running. During a
launch, the launch's servers are stopped and started through its
run_servers.py ("stop NAME" / "start NAME"). Restart launcher closes this
window (stopping what it runs, as closing it does) and opens a new one, to
pick up changes to the launcher itself.

Generate config: lists NVIDIA GPUs (nvidia-smi), with a selector when there
is more than one, and suggests a recipe for the selected card from
config/recipes/, takes the two languages to translate between (from whisper's
list, config/whisper_languages.json), previews the result (the languages, the
recipe's fragments from config/fragments/ pasted together, and start = false
for the servers it turns off, and [gpu] devices = that GPU's index, so the
servers use just that card) and saves it to config_generated.toml, which the
next Launch uses: on top of the defaults, under config_local.toml. Saving
also refreshes the server buttons for the new settings. Remote recipes show a
host entry and skip local GPU pinning, engine installation and startup.
Advanced mode replaces the recipe selector with per-backend local fragments,
remote host/port entries or Off, grouping speech engines together. The preview
colors TOML comments, sections, strings and literals without changing its text.

Check for updates: compares app.version with its masterVersionFile. A newer
app can be installed from its ZIP while this launcher remains running and
unchanged; its own app/server processes are stopped and resumed. New launcher
versions are informational and point to a full download.

Kill servers: lists this project's running processes (servers from
api_servers/, Python running src/ scripts), e.g. strays left by an earlier
run, and kills them after you confirm.

Double-click to run (.pyw: no console window), or: python launcher.pyw

Standard library only (Python 3.11+, with tkinter).
"""

import codecs
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import tkinter as tk
import tomllib
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))  # the project's Python modules
import configuration  # noqa: E402
import app_updates  # noqa: E402
from common import model_files  # noqa: E402

# Run the steps with python.exe, not the pythonw.exe running this window: with
# a (hidden) console of their own, the servers they start don't each pop up one.
_exe = Path(sys.executable)
_console_exe = _exe.with_name("python.exe")
PYTHON = str(_console_exe if _exe.name.lower() == "pythonw.exe" and _console_exe.is_file() else _exe)

# What each button runs: a list of steps, in order. "launch": the launch.sh
# steps; "mock": the dashboard alone with sample data (mock.sh).
MODES = {
    "launch": [
        ("Installing servers", [PYTHON, "src/install_api_servers.py"]),
        ("Downloading models", [PYTHON, "src/download_models.py"]),
        ("Running", [PYTHON, "src/run_servers.py", PYTHON, "src/app.py"]),
    ],
    "mock": [
        ("Running mock", [PYTHON, "src/app.py", "--mock"]),
    ],
}

if os.name == "nt":
    # Hidden console; the servers inherit it.
    CHILD_OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW}
    # A new launcher (Restart launcher), independent of this one.
    DETACHED = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    # Own process group, so Stop can signal the whole tree.
    CHILD_OPTIONS = {"start_new_session": True}
    DETACHED = {"start_new_session": True}


def launched_servers():
    """The servers Launch starts: [api_servers] sections with start = true
    (the default), in config order, from all the layers; [] if unreadable."""
    try:
        servers = configuration.load().get("api_servers", {})
    except SystemExit:
        return []
    return [name for name, server in servers.items() if isinstance(server, dict) and server.get("start", True)]


# run_servers.py's report of a server's state, on a line of its own.
SERVER_STATE = re.compile(r"^== server (\S+): (\w+)\s*$")


def setting(section, key, default=None):
    """A setting, from all the layers (see configuration.py); default if it's
    missing or the settings can't be read (app.py reports that when run)."""
    try:
        return configuration.load().get(section, {}).get(key, default)
    except SystemExit:
        return default


# Buttons are classic Tk buttons, all styled alike: themed (ttk) buttons on
# Windows ignore colors. Each color has a faded version for when the button
# doesn't apply; the gray ones are for tools.
GREEN, GREEN_FADED = "#16a34a", "#b7dfc4"
RED, RED_FADED = "#dc2626", "#f2c0c0"
BLUE, BLUE_FADED = "#2563eb", "#bfd0f6"
GRAY, GRAY_FADED = "#e2e5ea", "#f1f2f4"


def color_button(parent, text, color, faded, command, font_size=10, text_color="white", bold=True):
    button = tk.Button(parent, text=text, command=command, fg=text_color, bg=color, activebackground=color,
                       activeforeground=text_color, disabledforeground="#9aa1ab" if color == GRAY else text_color,
                       relief="flat", bd=0, padx=14, pady=4, cursor="hand2",
                       font=("Segoe UI", font_size, "bold" if bold else "normal"))
    button.colors = (color, faded)
    return button


def tool_button(parent, text, command):
    """A gray button, for the tools (Kill servers, Generate config, Mock)."""
    return color_button(parent, text, GRAY, GRAY_FADED, command, text_color="#1d2125", bold=False)


def set_enabled(button, enabled):
    """Enable or disable a color_button, fading its color when disabled."""
    color, faded = button.colors
    button.configure(state="normal" if enabled else "disabled", bg=color if enabled else faded,
                     activebackground=color if enabled else faded, cursor="hand2" if enabled else "arrow")


# --- Kill servers: this project's processes, wherever they came from (e.g. a
# launch whose window was closed oddly): anything run from api_servers/, and
# Python running the project's scripts in src/.

SCRIPTS = ("app.py", "run_servers.py", "sherpa_server_win.py", "install_api_servers.py", "download_models.py")


def project_processes():
    """[(pid, name, command line)] of this project's running processes."""
    norm = lambda path: path.replace("\\", "/").lower()
    folders = [norm(str(ROOT / "api_servers")) + "/", norm(str(ROOT / "src")) + "/"]
    # The launcher runs scripts by relative path (from the project folder).
    relative = [f"src/{script}" for script in SCRIPTS]
    if os.name == "nt":
        query = ("Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath,CommandLine"
                 " | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True,
                                text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        rows = [(p["ProcessId"], p["Name"], p.get("ExecutablePath") or "", p.get("CommandLine") or "")
                for p in json.loads(result.stdout or "[]")]
    else:
        result = subprocess.run(["ps", "-eo", "pid=,comm=,args="], capture_output=True, text=True, timeout=60)
        rows = []
        for line in result.stdout.splitlines():
            pid, name, args = (line.split(None, 2) + ["", ""])[:3]
            rows.append((int(pid), name, "", args))
    found = []
    for pid, name, exe, command in rows:
        text = norm(exe) + " " + norm(command)
        if pid != os.getpid() and (any(f in text for f in folders) or any(r in text for r in relative)):
            found.append((pid, name, command))
    return found


def kill(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def stop_tree(proc):
    """Stop proc and everything it started (the servers and the dashboard)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(proc.pid, signal.SIGINT)  # like Ctrl+C: run_servers.py stops the servers


# --- Generate config: recipes (config/recipes/*.json) list fragments
# (config/fragments/*.toml) to paste together, and servers to switch off.

RECIPES_DIR = ROOT / "config" / "recipes"
FRAGMENTS_DIR = ROOT / "config" / "fragments"
GENERATED_FILE = configuration.GENERATED_FILE


def load_recipes():
    """{name: recipe} from config/recipes/*.json, smallest vram_gb first."""
    recipes = {}
    for path in RECIPES_DIR.glob("*.json"):
        recipes[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return dict(sorted(recipes.items(), key=lambda item: item[1].get("vram_gb", 0)))


def detect_gpus():
    """All NVIDIA GPUs as (nvidia-smi index, name, VRAM in GiB).
    Raises RuntimeError with the reason if nvidia-smi can't tell."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except FileNotFoundError:
        raise RuntimeError("nvidia-smi not found (no NVIDIA driver?)") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("nvidia-smi did not answer") from None
    if result.returncode != 0:
        raise RuntimeError(f"nvidia-smi failed: {(result.stderr or result.stdout).strip()[:200]}")
    gpus = []
    for line in result.stdout.strip().splitlines():
        index, _, rest = line.partition(",")
        name, _, mib = rest.rpartition(",")
        if index.strip().isdigit() and mib.strip().isdigit():
            gpus.append((int(index), name.strip(), int(mib) / 1024))
    if not gpus:
        raise RuntimeError(f"no GPUs in nvidia-smi's output: {result.stdout.strip()[:200]!r}")
    return gpus


def suggest_recipe(recipes, vram_gib):
    """The recipe for the largest card that fits: cards report a little under
    their nominal size (a 24 GB card: about 23.99 GiB), hence the margin."""
    fitting = [name for name, recipe in recipes.items()
               if "remote_host" not in recipe and recipe.get("vram_gb", 0) <= vram_gib + 0.5]
    return fitting[-1] if fitting else None


def initial_languages():
    """(language1, language2): from config_generated.toml if it sets them,
    else from the defaults."""
    languages = {}
    for path in (configuration.DEFAULTS_FILE, GENERATED_FILE):
        try:
            with open(path, "rb") as f:
                languages.update(tomllib.load(f).get("languages", {}))
        except (OSError, tomllib.TOMLDecodeError):
            pass  # no generated file yet (or an unreadable one): the defaults stand
    return languages.get("language1", ""), languages.get("language2", "")


def assemble(name, recipe, languages=None, gpu=None, host=None):
    """The config text for a recipe: the [languages] section if languages is
    given, the [gpu] section if gpu (nvidia-smi index) is, then the recipe's
    fragments pasted in order, then a start = false section for each server in
    its "off" list. Remote recipes fill quoted {host} placeholders and omit GPU pinning."""
    parts = [f"# Generated by launcher.pyw from config/recipes/{name}.json:\n"
             f"# {recipe.get('description', '')}\n"]
    if languages:
        # json.dumps quotes and escapes a string the way TOML's basic strings need.
        parts.append("[languages]\n" + "".join(f"language{i} = {json.dumps(language)}\n"
                                               for i, language in enumerate(languages, 1)))
    if gpu is not None and "remote_host" not in recipe:
        parts.append(f"[gpu]\n# The card the recipe was sized for (nvidia-smi index).\ndevices = \"{gpu}\"\n")
    for fragment in recipe.get("fragments", []):
        path = FRAGMENTS_DIR / f"{fragment}.toml"
        if not path.is_file():
            raise ValueError(f"recipe {name}: fragment {fragment!r} not found ({path.name})")
        text = path.read_text(encoding="utf-8").strip()
        if "remote_host" in recipe:
            text = text.replace('"{host}"', json.dumps(host if host is not None else recipe["remote_host"]))
        parts.append(f"# --- fragment {fragment}\n\n" + text + "\n")
    for server in recipe.get("off", []):
        parts.append(f"# --- off\n\n[api_servers.{server}]\nstart = false\n")
    text = "\n".join(parts)
    tomllib.loads(text)  # raises if the fragments don't fit together (e.g. a table defined twice)
    return text


def backend_choices():
    """Discover local fragments and remote endpoints; TTS shares one selector."""
    defaults = tomllib.loads(configuration.DEFAULTS_FILE.read_text(encoding="utf-8"))
    fragments = {path.stem: tomllib.loads(path.read_text(encoding="utf-8"))
                 for path in sorted(FRAGMENTS_DIR.glob("*.toml"))}
    tts = {data["tts"]["server"] for data in fragments.values() if "tts" in data}
    tts.add(defaults["tts"]["server"])
    groups = {}
    for server, settings in defaults["api_servers"].items():
        if not isinstance(settings, dict) or "host" not in settings or "port" not in settings:
            continue
        group = "Speech" if server in tts else {"whisper": "Transcription", "llama": "Translation"}.get(server, server)
        choices = groups.setdefault(group, {"Off": ("off", None, None)})
        for name, data in fragments.items():
            servers = data.get("api_servers", {})
            if list(servers) == [server] and servers[server].get("start", True):
                choices[name] = ("local", server, name)
        choices[f"Remote {server}"] = ("remote", server, None)
    order = {"Transcription": 0, "Translation": 1, "Speech": 2}
    groups = dict(sorted(groups.items(), key=lambda item: order.get(item[0], 3)))
    for group, choices in groups.items():
        groups[group] = dict(sorted(choices.items(), key=lambda item: item[1][0] == "remote"))
    return defaults, groups, fragments


def downloaded_model_size(data):
    """Size of complete local model files, using the downloader's path mapping."""
    cfg = configuration.merge(tomllib.loads(configuration.DEFAULTS_FILE.read_text(encoding="utf-8")), data)
    cfg["_config_dir"] = ROOT
    cfg["models"]["dir"] = setting("models", "dir", "models")
    paths = set()
    try:
        for server, settings in data.get("api_servers", {}).items():
            if settings.get("start", True):
                files = model_files(cfg, server)
                if not files:
                    return None
                paths.update(path for _, _, path in files)
        if not paths or any(not path.is_file() for path in paths):
            return None
        sizes = [path.stat().st_size for path in paths]
        return sum(sizes) / 1_000_000_000 if all(sizes) else None
    except (OSError, KeyError, TypeError, ValueError, SystemExit):
        return None  # Missing/unreadable paths don't prevent choosing the fragment.


def fragment_resources(name, data):
    """Advisory model GB and VRAM GB from the fragment's size comment and args."""
    text = (FRAGMENTS_DIR / f"{name}.toml").read_text(encoding="utf-8")
    size = re.search(r"^# Model files: ([\d.]+) GB([^\n]*)", text, re.MULTILINE)
    gb = float(size[1]) if size else downloaded_model_size(data)
    if gb is None:
        return None, None
    cpu = "CPU" in size[2] if size else any(
        server.get("script") == "src/kokoro_server.py" for server in data.get("api_servers", {}).values())
    if cpu or gb == 0:
        return gb, 0.0
    args = data.get("api_servers", {}).get("llama", {}).get("args", [])
    context = None
    for index, arg in enumerate(args):
        if arg in ("--ctx-size", "-c") and index + 1 < len(args):
            try:
                context = int(args[index + 1])
            except ValueError:
                pass
    overhead = 0.10 if context is not None and 0 < context <= 2048 else 0.20
    return gb, gb * (1 + overhead)


def assemble_advanced(selections, languages=None, gpu=None):
    """Selections are (kind, server, fragment, host, port); remote uses defaults."""
    defaults, groups, _ = backend_choices()
    selected = {server for kind, server, *_ in selections if kind != "off"}
    recipe = {"description": "Advanced backend selections",
              "fragments": [fragment for kind, server, fragment, *_ in selections if kind == "local"],
              "off": [server for server, settings in defaults["api_servers"].items()
                      if isinstance(settings, dict) and server not in selected]}
    local = any(kind == "local" for kind, *_ in selections)
    text = assemble("advanced", recipe, languages, gpu if local else None)
    text = text.replace("from config/recipes/advanced.json:", "using advanced mode:", 1)
    for kind, server, fragment, host, port in selections:
        if kind == "remote":
            text += (f"\n# --- remote {server}\n\n[api_servers.{server}]\nstart = false\n"
                     f"host = {json.dumps(host)}\nport = {port}\n")
    tts = {entry[1] for entry in groups.get("Speech", {}).values()}
    speech = next((server for kind, server, *_ in selections if kind == "remote" and server in tts), None)
    if speech:
        text += f'\n[tts]\nserver = {json.dumps(speech)}\n'
    return text


# A lexical preview only: comments inside strings remain part of the string.
TOML_TOKENS = re.compile(
    r'(?P<string>"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'[^\'\n]*\')'
    r'|(?P<comment>\#[^\n]*)|(?P<section>^\s*\[\[?[^\n]*?\]\]?)'
    r'|(?P<literal>\b(?:true|false|[+-]?\d[\w.+:-]*)\b)', re.MULTILINE)


def colorize_toml(widget, text):
    for tag, color in {"comment": "#6b7280", "section": "#2563eb",
                       "string": "#15803d", "literal": "#7c3aed"}.items():
        widget.tag_configure(tag, foreground=color)
        widget.tag_remove(tag, "1.0", "end")
    for match in TOML_TOKENS.finditer(text):
        widget.tag_add(match.lastgroup, f"1.0+{match.start()}c", f"1.0+{match.end()}c")


def tooltip(widget, text):
    """Show help while hovering over the label or giving it keyboard focus."""
    tip = None

    def hide(event=None):
        nonlocal tip
        if tip is not None:
            popup = tip
            tip = None
            popup.destroy()

    def show(event=None):
        nonlocal tip
        if tip is not None:
            return
        content = text() if callable(text) else text
        if not content:
            return
        tip = tk.Toplevel(widget)
        # Don't map at Tk's initial location: it can steal the pointer and
        # trigger Leave on the hovered label before we've positioned it.
        tip.withdraw()
        tip.overrideredirect(True)
        tip.transient(widget.winfo_toplevel())
        frame = tk.Frame(tip, background="white", highlightbackground="#d5dbe5", highlightthickness=1)
        frame.pack()
        tk.Label(frame, text=content, wraplength=400, padx=12, pady=8, background="white",
                 foreground="#334155", justify="left", font=("Segoe UI", 10)).pack()
        tip.update_idletasks()
        x = max(0, min(widget.winfo_rootx(), widget.winfo_screenwidth() - tip.winfo_reqwidth() - 12))
        tip.geometry(f"+{x}+{widget.winfo_rooty() + widget.winfo_height() + 5}")
        tip.bind("<Escape>", hide)
        tip.deiconify()
        tip.lift()

    widget.bind("<Enter>", show)
    widget.bind("<FocusIn>", show)
    widget.bind("<Leave>", hide)
    widget.bind("<FocusOut>", hide)
    widget.bind("<Escape>", hide)
    widget.bind("<Destroy>", hide)


class GenerateConfigWindow:
    """Detect the GPU, suggest a recipe, preview it, and save it to config_generated.toml."""

    def __init__(self, parent, on_save):
        self.window = tk.Toplevel(parent)
        self.window.title("Generate config")
        self.on_save = on_save  # called after saving, so the launcher picks up the new settings
        self.recipes = load_recipes()
        self.defaults, self.backend_groups, self.fragments = backend_choices()
        self.resources = {name: fragment_resources(name, data) for name, data in self.fragments.items()}
        self.backend_groups = {
            group: {(f"{label} ({self.resources[fragment][0]:.2f} GB)"
                     if kind == "local" and self.resources[fragment][0] is not None else label):
                    (kind, server, fragment) for label, (kind, server, fragment) in choices.items()}
            for group, choices in self.backend_groups.items()}
        self.text = ""
        self.gpu_index = None  # the detected card's nvidia-smi index, once known
        self.gpus = []
        self.gpu_description = "Checking the GPU..."

        top = ttk.Frame(self.window, padding=8)
        top.pack(fill="x")
        self.gpu = ttk.Label(top, text="Checking the GPU...")
        self.gpu.pack(anchor="w")
        self.gpu_row = ttk.Frame(top)  # shown only when there is a choice of cards
        ttk.Label(self.gpu_row, text="GPU:").pack(side="left")
        self.gpu_combo = ttk.Combobox(self.gpu_row, state="readonly", width=54)
        self.gpu_combo.pack(side="left", padx=(6, 0))
        self.gpu_combo.bind("<<ComboboxSelected>>", lambda e: self.select_gpu())
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(8, 0))
        self.recipe_row = row
        ttk.Label(row, text="Recipe:").pack(side="left")
        self.choice = tk.StringVar()
        self.combo = ttk.Combobox(row, textvariable=self.choice, state="readonly", width=24,
                                  values=list(self.recipes))
        self.combo.pack(side="left", padx=(6, 0))
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.show())
        self.advanced = tk.BooleanVar(value=False)
        self.mode_row = ttk.Frame(top)
        self.mode_row.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(self.mode_row, text="Advanced mode", variable=self.advanced,
                        command=self.toggle_advanced).pack(side="left")
        self.backend_frame = ttk.Frame(top)
        totals = ttk.Frame(self.backend_frame)
        totals.pack(side="right", anchor="n", fill="x", expand=True, padx=(24, 0), pady=3)
        self.resource_totals = ttk.Label(totals, text="", justify="center", anchor="center", takefocus=True)
        self.resource_totals.pack(fill="x", anchor="n")
        tooltip(self.resource_totals,
                "Approximate local VRAM usage. GB = decimal.\n\n"
                "Estimated VRAM adds 10% for llama context ≤2048, or 20% otherwise. "
                "CPU models, remote backends and Off use no local VRAM.\n\n"
                "Excludes voices, support files and other apps. Actual usage varies. "
                "These estimates do not check capacity or account for config_local.toml overrides. "
                "An asterisk marks a known subtotal when a model size is unavailable.")
        selectors = ttk.Frame(self.backend_frame)
        selectors.pack(side="left", fill="x")
        self.backends = {}
        self.backend_sizes = {}
        for group, choices in self.backend_groups.items():
            row = ttk.Frame(selectors)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=f"{group}:", width=14).pack(side="left")
            choice = tk.StringVar(value="Off")
            combo = ttk.Combobox(row, textvariable=choice, values=list(choices), state="readonly", width=44)
            combo.pack(side="left", padx=(0, 8))
            size = ttk.Label(row, text="", width=23)
            size.pack(side="left", padx=(0, 8))
            self.backend_sizes[group] = size
            remote = ttk.Frame(row)
            host, port = tk.StringVar(value="127.0.0.1"), tk.StringVar()
            ttk.Label(remote, text="Host / IP:").pack(side="left")
            ttk.Entry(remote, textvariable=host, width=22).pack(side="left", padx=4)
            ttk.Label(remote, text="Port:").pack(side="left")
            ttk.Entry(remote, textvariable=port, width=6).pack(side="left", padx=4)
            self.backends[group] = (choice, host, port, remote)
            tooltip(size, lambda group=group: self.cpu_hint(group))
            combo.bind("<<ComboboxSelected>>", lambda e, group=group: self.select_backend(group))
            host.trace_add("write", lambda *args: self.show())
            port.trace_add("write", lambda *args: self.show())
        self.details = ttk.Label(top, text="")  # the selected recipe's VRAM and description
        self.details.pack(anchor="w", pady=(6, 0))

        self.host_row = ttk.Frame(top)  # shown for recipes that request a remote host
        ttk.Label(self.host_row, text="Remote host:").pack(side="left")
        self.remote_host = tk.StringVar(value="127.0.0.1")
        ttk.Entry(self.host_row, textvariable=self.remote_host, width=32).pack(side="left", padx=(6, 0))
        ttk.Label(self.host_row, text="IP address or hostname").pack(side="left", padx=(8, 0))
        self.remote_host.trace_add("write", lambda *args: self.show())

        # The two languages to translate between, chosen from the ones whisper
        # knows (config/whisper_languages.json), in its order, capitalized
        # ("Chinese"): the dashboard's language checks need whisper's names.
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(8, 0))
        self.language_names = [name.title() for _code, name in configuration.whisper_languages()]
        self.languages = [tk.StringVar(value=value) for value in initial_languages()]
        for i, var in enumerate(self.languages, 1):
            ttk.Label(row, text=f"Language {i}:").pack(side="left", padx=(12 if i > 1 else 0, 0))
            ttk.Combobox(row, textvariable=var, values=self.language_names, state="readonly",
                         width=18).pack(side="left", padx=(6, 0))
            var.trace_add("write", lambda *args: self.show())

        self.status = ttk.Label(top, text="")
        self.status.pack(anchor="w", pady=(6, 0))

        # Save at the bottom, centered (packed before the preview, so the
        # preview takes the space that's left and Save stays visible).
        bottom = ttk.Frame(self.window, padding=(8, 0, 8, 10))
        bottom.pack(side="bottom", fill="x")
        self.save_button = color_button(bottom, "✔  Save", GREEN, GREEN_FADED, self.save, font_size=12)
        self.save_button.configure(padx=32, pady=5)
        self.save_button.pack()
        set_enabled(self.save_button, False)

        self.preview = ScrolledText(self.window, font=("Consolas", 10), width=100, height=32, state="disabled")
        self.preview.configure(background="white", foreground="#1d2125")
        self.preview.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        if not self.recipes:
            self.gpu.configure(text=f"No recipes found in {RECIPES_DIR}")
            return
        self.restore_advanced()
        threading.Thread(target=self.check_gpu, daemon=True).start()

    def restore_advanced(self):
        """Restore our generated advanced selections before GPU detection finishes."""
        try:
            text = GENERATED_FILE.read_text(encoding="utf-8")
            if not text.startswith("# Generated by launcher.pyw using advanced mode:"):
                return
            saved = tomllib.loads(text)
        except (OSError, tomllib.TOMLDecodeError):
            return
        fragments = set(re.findall(r"^# --- fragment (.+)$", text, re.MULTILINE))
        remotes = set(re.findall(r"^# --- remote (.+)$", text, re.MULTILINE))
        self.combo.current(0)  # Recipe remains available when switching back to simple mode.
        self.advanced.set(True)
        self.toggle_advanced()
        self.gpu_index = saved.get("gpu", {}).get("devices")
        for group, choices in self.backend_groups.items():
            choice, host, port, _ = self.backends[group]
            label = next((label for label, (kind, server, fragment) in choices.items()
                          if (kind == "local" and fragment in fragments)
                          or (kind == "remote" and server in remotes)), "Off")
            choice.set(label)
            self.select_backend(group)
            kind, server, _ = choices[label]
            if kind == "remote":
                endpoint = saved["api_servers"][server]
                host.set(endpoint["host"])
                port.set(str(endpoint["port"]))
        self.show()

    def describe(self, name):
        recipe = self.recipes[name]
        return recipe.get("description") or f"For {recipe.get('vram_gb', '?')} GB of VRAM."

    def check_gpu(self):
        """In a worker thread (nvidia-smi can take a moment); the result goes back to the window."""
        try:
            found = detect_gpus(), None
        except RuntimeError as err:
            found = [], str(err)
        self.window.after(0, self.gpu_found, *found)

    def gpu_found(self, gpus, error):
        self.gpus = gpus
        if gpus:
            self.gpu_combo.configure(values=[f"GPU {index}: {name} ({vram:.1f} GiB)"
                                             for index, name, vram in gpus])
            selected = max(range(len(gpus)), key=lambda i: gpus[i][2])
            try:
                with open(GENERATED_FILE, "rb") as f:
                    saved = tomllib.load(f).get("gpu", {}).get("devices")
                selected = next((i for i, gpu in enumerate(gpus) if str(gpu[0]) == saved), selected)
            except (OSError, tomllib.TOMLDecodeError):
                pass
            self.gpu_combo.current(selected)
            if len(gpus) > 1:
                self.gpu_row.pack(fill="x", pady=(8, 0), after=self.gpu)
            self.select_gpu()
        else:
            self.gpu_description = f"GPU not detected: {error}. Choose a recipe."
            if self.combo.current() < 0:
                self.combo.current(0)
            self.show()

    def select_gpu(self):
        index, name, vram = self.gpus[self.gpu_combo.current()]
        self.gpu_index = index
        description = f"{name}, {vram:.1f} GiB (GPU {index})"
        suggested = suggest_recipe(self.recipes, vram)
        if suggested:
            self.gpu_description = f"GPU: {description}. Suggested recipe: {suggested}"
        else:
            self.gpu_description = f"GPU: {description}. No recipe fits; choose one."
        names = list(self.recipes)
        if self.combo.current() < 0 or (not self.advanced.get() and "remote_host" not in self.recipes[self.selected()]):
            self.combo.current(names.index(suggested) if suggested else 0)
        self.show()

    def selected(self):
        return list(self.recipes)[self.combo.current()]

    def toggle_advanced(self):
        if self.advanced.get():
            self.recipe_row.pack_forget()
            self.backend_frame.pack(fill="x", after=self.mode_row)
            recipe = self.recipes[self.selected()] if self.combo.current() >= 0 else {}
            merged = self.defaults
            for fragment in recipe.get("fragments", []):
                merged = configuration.merge(merged, self.fragments[fragment])
            for group, choices in self.backend_groups.items():
                choice, host, port, _ = self.backends[group]
                label = "Off"
                for candidate, (kind, server, fragment) in choices.items():
                    if kind == "local" and fragment in recipe.get("fragments", []):
                        label = candidate
                    elif kind == "remote" and "remote_host" in recipe and server not in recipe.get("off", []):
                        if group != "Speech" or server == merged.get("tts", {}).get("server"):
                            label = candidate
                choice.set(label)
                host.set(self.remote_host.get())
                self.select_backend(group)
        else:
            self.backend_frame.pack_forget()
            self.recipe_row.pack(fill="x", pady=(8, 0), before=self.mode_row)
        self.show()

    def cpu_hint(self, group):
        choice = self.backends[group][0].get()
        kind, server, fragment = self.backend_groups[group][choice]
        if kind == "local" and self.resources[fragment][1] == 0:
            return "Runs on CPU"
        return None

    def select_backend(self, group):
        choice, host, port, remote = self.backends[group]
        kind, server, _ = self.backend_groups[group][choice.get()]
        if kind == "remote":
            port.set(str(self.defaults["api_servers"][server]["port"]))
            remote.pack(side="left")
        else:
            remote.pack_forget()
        self.show()

    def show(self):
        if self.combo.current() < 0:
            return  # no recipe selected yet (the GPU check is still running)
        name = self.selected()
        recipe = self.recipes[name]
        advanced = self.advanced.get()
        selections = [(self.backend_groups[group][choice.get()] + (host.get().strip(), port.get().strip()))
                      for group, (choice, host, port, _) in self.backends.items()]
        remote = not any(kind == "local" for kind, *_ in selections) if advanced else "remote_host" in recipe
        self.gpu.configure(text="Remote servers: no local GPU required." if remote else self.gpu_description)
        if remote:
            self.gpu_row.pack_forget()
        else:
            self.host_row.pack_forget()
            if len(self.gpus) > 1:
                self.gpu_row.pack(fill="x", pady=(8, 0), after=self.gpu)
        if remote and not advanced:
            self.host_row.pack(fill="x", pady=(8, 0), after=self.details)
        else:
            self.host_row.pack_forget()
        self.details.configure(text="Choose a local fragment, remote endpoint, or Off for each backend."
                               if advanced else self.describe(name))
        if advanced:
            total_vram, unknown = 0.0, False
            for group, (choice, *_rest) in self.backends.items():
                kind, server, fragment = self.backend_groups[group][choice.get()]
                size, vram = self.resources[fragment] if kind == "local" else (0.0, 0.0)
                unknown |= size is None
                total_vram += vram or 0
                detail = "Est. VRAM: unavailable" if size is None else f"Est. VRAM: {vram:.2f} GB"
                self.backend_sizes[group].configure(text=detail)
            self.resource_totals.configure(text=(
                f"Total Estimated VRAM: {total_vram:.2f} GB{'*' if unknown else ''}"))
        languages = [var.get().strip() for var in self.languages]
        known = {name.lower() for name in self.language_names}
        try:
            if not all(languages):
                raise ValueError("choose both languages")
            unknown = [language for language in languages if language.lower() not in known]
            if unknown and known:  # e.g. a hand-written "Chinese-simplified" from an earlier file
                raise ValueError(f"not a whisper language: {', '.join(unknown)}; choose one from the list")
            self.text = (assemble_advanced(selections, languages, self.gpu_index) if advanced else
                         assemble(name, recipe, languages, self.gpu_index,
                                  self.remote_host.get().strip() if remote else None))
            self.status.configure(text=f"Save writes {GENERATED_FILE.name}.")
            set_enabled(self.save_button, True)
        except (ValueError, tomllib.TOMLDecodeError) as err:
            self.text = ""
            self.status.configure(text=f"Can't save: {err}")
            set_enabled(self.save_button, False)
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", self.text)
        colorize_toml(self.preview, self.text)
        self.preview.configure(state="disabled")

    def save(self):
        temp = GENERATED_FILE.with_name(GENERATED_FILE.name + ".tmp")
        temp.write_text(self.text, encoding="utf-8", newline="\n")
        temp.replace(GENERATED_FILE)
        self.on_save()
        self.window.destroy()


class Launcher:
    def __init__(self, root):
        self.root = root
        self.proc = None  # the step running now
        self.step = None  # its title, e.g. "Running"
        self.mode = None  # what's running: "launch", "mock" or None
        self.next_mode = None  # to start once the current run has stopped
        self.stopping = False
        self.output = queue.Queue()  # (kind, value) from the worker thread to the window
        self.carriage_return = False  # a "\r" was written: the next text replaces the line
        self.server_states = {}  # name: state reported by run_servers.py
        self.server_buttons = {}  # name: button (developer controls)
        self.solo = {}  # name: run_servers.py --only NAME, for a server started on its own
        self.updating = None  # check, download, stop or install; launcher itself stays running
        self.update_package = None
        self.update_resume = None
        self.update_resume_solo = []
        self.update_killed = set()

        root.title("Audio Translation Dashboard - Launcher")
        # Row 1: the dashboard itself, the thing to open once it's running.
        row = ttk.Frame(root, padding=(8, 10, 8, 4))
        row.pack(fill="x")
        self.open_button = color_button(row, "Open dashboard", BLUE, BLUE_FADED, self.open_dashboard, font_size=13)
        self.open_button.configure(padx=28, pady=6)
        self.open_button.pack()

        # Row 2: run controls and status; tools on the right.
        bar = ttk.Frame(root, padding=(8, 4, 8, 8))
        bar.pack(fill="x")
        self.launch_button = color_button(bar, "▶  Launch", GREEN, GREEN_FADED, lambda: self.start("launch"))
        self.launch_button.pack(side="left")
        self.stop_button = color_button(bar, "■  Stop", RED, RED_FADED, self.stop)
        self.stop_button.pack(side="left", padx=(6, 0))
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="left", padx=(12, 0))
        # Developer controls (only with [launcher] show_dev_controls = true).
        self.mock_button = tool_button(bar, "Mock", lambda: self.start("mock"))
        self.restart_button = tool_button(bar, "Restart app", self.restart_app)
        if setting("launcher", "show_dev_controls", False):
            self.mock_button.pack(side="right")
            self.restart_button.pack(side="right", padx=(0, 6))
            tool_button(bar, "Restart launcher", self.restart_launcher).pack(side="right", padx=(0, 6))
        self.generate_button = tool_button(bar, "Generate config", self.generate_config)
        self.generate_button.pack(side="right", padx=(0, 6))
        self.kill_button = tool_button(bar, "Kill servers", self.kill_servers)
        self.kill_button.pack(side="right", padx=(0, 6))
        self.generate_window = None

        # Row 3 (developer controls): a button per server, to stop or start it
        # on its own while a launch runs.
        self.servers_row = ttk.Frame(root, padding=(8, 0, 8, 8))
        if setting("launcher", "show_dev_controls", False):
            self.servers_row.pack(fill="x")
            self.make_server_buttons()

        row = ttk.Frame(root, padding=(8, 0, 8, 8))
        row.pack(fill="x")
        self.update_button = tool_button(row, "Check for updates", self.check_updates)
        self.update_button.pack(side="right")
        self.version_label = ttk.Label(row, text="")
        self.version_label.pack(side="left")
        self.show_versions()
        self.update_buttons()

        # Sized in characters, like a terminal; the window fits around it.
        self.log = ScrolledText(root, font=("Consolas", 10), width=120, height=30, wrap="word",
                                state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    # --- buttons

    def start(self, mode):
        """Run mode's steps; if something else is running, stop it first."""
        if self.updating:
            return
        if self.mode:
            self.next_mode = mode  # started when the current run reports done
            self.stop()
            return
        self.reload_config()
        self.mode = mode
        self.stopping = False
        self.update_buttons()
        threading.Thread(target=self.run_steps, args=(MODES[mode],), daemon=True).start()

    def stop(self):
        self.stopping = True
        self.update_buttons()
        self.status.configure(text="Stopping...")
        if self.proc and self.proc.poll() is None:
            stop_tree(self.proc)

    def update_buttons(self):
        """Launch and Mock are available unless that mode is already running
        (or a switch is under way); Stop while anything runs; Restart app once
        the dashboard is running."""
        busy = bool(self.updating) or (self.stopping and self.mode is not None)
        set_enabled(self.update_button, not self.updating)
        set_enabled(self.generate_button, not self.updating)
        set_enabled(self.kill_button, not self.updating)
        set_enabled(self.launch_button, not busy and self.mode != "launch")
        set_enabled(self.mock_button, not busy and self.mode != "mock")
        set_enabled(self.stop_button, bool(self.mode) and not busy)
        dashboard_up = self.mode == "mock" or (self.mode == "launch" and self.step == "Running")
        set_enabled(self.restart_button, dashboard_up and not busy)
        for name, button in self.server_buttons.items():
            self.show_server(name, button, busy)

    # --- server buttons (developer controls)

    def make_server_buttons(self):
        for child in self.servers_row.winfo_children():
            child.destroy()
        self.server_buttons = {}
        names = launched_servers()
        ttk.Label(self.servers_row, text="Servers:" if names else "Servers: none to start").pack(side="left")
        for name in names:
            button = tool_button(self.servers_row, name, lambda name=name: self.toggle_server(name))
            button.pack(side="left", padx=(6, 0))
            self.server_buttons[name] = button

    # A server's state -> (its button's label glyph, its text color, whether it
    # can be clicked): ■ stops a running server, ▶ starts a stopped one, … while
    # it changes.
    SERVER_LOOKS = {
        "ready": ("■", GREEN, True),
        "stopped": ("▶", "#1d2125", True),
        "failed": ("▶", RED, True),
        "exited": ("▶", RED, True),
        "starting": ("…", "#1d2125", False),
        "stopping": ("…", "#1d2125", False),
    }

    def show_server(self, name, button, busy):
        """A server button as its state says (▶ when it's not running). Plain
        and disabled while a launch is about to start it, or while a launch
        is stopping (except a server started on its own)."""
        state = self.server_states.get(name)
        solo = name in self.solo
        launch_will_start = self.mode == "launch" and state is None and not solo
        if self.updating or (busy and not solo) or launch_will_start:
            button.configure(text=name, fg="#1d2125", disabledforeground="#9aa1ab")
            set_enabled(button, False)
            return
        glyph, color, clickable = self.SERVER_LOOKS.get(state, self.SERVER_LOOKS["stopped"])
        button.configure(text=f"{glyph} {name}", fg=color, activeforeground=color, disabledforeground=color)
        set_enabled(button, clickable)

    def launch_running(self):
        return self.mode == "launch" and self.step == "Running" and self.proc and self.proc.poll() is None

    def toggle_server(self, name):
        """Stop a running server, or start a stopped one: one started on its own
        through its own run_servers.py, a launch's through the launch's; with
        no launch running, a server is started on its own."""
        if self.updating:
            return
        stop = self.server_states.get(name) == "ready"
        if name in self.solo:
            if stop:
                self.server_states[name] = "stopping"
                proc = self.solo[name]
                threading.Thread(target=stop_tree, args=(proc,), daemon=True).start()
        elif self.launch_running():
            action = "stop" if stop else "start"
            try:
                self.proc.stdin.write(f"{action} {name}\n".encode())
                self.proc.stdin.flush()
            except OSError as err:
                self.write(f"== could not {action} {name}: {err}\n")
                return
            self.server_states[name] = "stopping" if stop else "starting"  # until it reports
        elif not stop:
            self.start_solo(name)
        self.update_buttons()

    def start_solo(self, name):
        """Start one server on its own, with Launch's steps for just that server:
        install it and download its models if needed, then run_servers.py
        --only NAME, until stopped. self.solo[name] is the step running now."""
        self.server_states[name] = "starting"
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        steps = [[PYTHON, f"src/{script}", "--only", name]
                 for script in ("install_api_servers.py", "download_models.py", "run_servers.py")]
        self.solo[name] = None  # (claimed now: the button shows it as starting)

        def run():
            for argv in steps:
                if self.updating in ("stop", "install"):
                    break
                proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, **CHILD_OPTIONS)
                self.solo[name] = proc
                self.pump(proc)
                if proc.wait() != 0:
                    if argv is not steps[-1]:  # (run_servers.py reports its own failures)
                        self.output.put(("server", (name, "failed")))
                    break
            self.output.put(("solo-done", name))

        threading.Thread(target=run, daemon=True).start()

    def restart_app(self):
        """Restart just the dashboard: for a launch, run_servers.py restarts it
        (keeping the servers running) when told "restart" on its input; for
        Mock, stop the mock and start it again."""
        if self.mode == "mock":
            self.start("mock")  # stops it first, then starts it again
        elif self.mode == "launch" and self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"restart\n")
                self.proc.stdin.flush()
            except OSError as err:
                self.write(f"== could not restart the dashboard: {err}\n")

    def reload_config(self):
        """Refresh config-dependent controls before Launch/Mock and after Save."""
        if self.server_buttons or setting("launcher", "show_dev_controls", False):
            self.make_server_buttons()
        self.update_buttons()

    def restart_launcher(self):
        """Close this window (stopping what it runs, as closing it does) and
        start the launcher again, e.g. to pick up changes to this file."""
        if self.updating:
            return
        if (self.mode or self.solo) and not messagebox.askyesno(
                "Restart launcher", "Restarting the launcher stops what it's running (the launch, and servers "
                "started on their own), as closing it does. Restart?", parent=self.root):
            return
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=ROOT, **DETACHED)
        self.close()

    def generate_config(self):
        """Open the Generate config window (or bring it to the front)."""
        if self.generate_window and self.generate_window.window.winfo_exists():
            self.generate_window.window.lift()
        else:
            self.generate_window = GenerateConfigWindow(self.root, self.reload_config)

    def kill_servers(self):
        """Find this project's processes (in a worker thread: it takes a moment),
        then ask before killing them."""
        set_enabled(self.kill_button, False)
        self.status.configure(text="Looking for servers...")

        def find():
            try:
                found = project_processes()
            except (OSError, subprocess.SubprocessError, ValueError) as err:
                found = err
            self.root.after(0, self.confirm_kill, found)

        threading.Thread(target=find, daemon=True).start()

    def confirm_kill(self, found):
        set_enabled(self.kill_button, True)
        if isinstance(found, Exception):
            self.status.configure(text="Ready" if not self.mode else self.status.cget("text"))
            messagebox.showerror("Kill servers", f"Could not list processes: {found}", parent=self.root)
            return
        if not found:
            self.status.configure(text="No servers running" if not self.mode else self.status.cget("text"))
            return
        listing = "\n".join(f"{name} (PID {pid})" for pid, name, _ in found[:15])
        more = f"\n...and {len(found) - 15} more" if len(found) > 15 else ""
        if not messagebox.askyesno("Kill servers", f"Kill these {len(found)} processes of this project?\n\n"
                                   f"{listing}{more}", parent=self.root):
            self.status.configure(text="Ready" if not self.mode else self.status.cget("text"))
            return
        if self.mode:  # our own run is among them: report it as stopped, not failed
            self.next_mode = None
            self.stopping = True
            self.update_buttons()
        for pid, name, command in found:
            kill(pid)
            self.write(f"== killed {name} (PID {pid}): {command[:150]}\n")
        self.write("\n")
        self.status.configure(text=f"Killed {len(found)} process{'es' if len(found) != 1 else ''}")

    def open_dashboard(self):
        webbrowser.open(f"http://localhost:{setting('server', 'port', 8080)}/")

    def close(self):
        if self.updating:
            messagebox.showinfo("Update in progress", "Please wait for the update operation to finish.", parent=self.root)
            return
        self.next_mode = None
        if self.mode:
            self.stop()
        for proc in self.solo.values():  # servers started on their own
            if proc:
                stop_tree(proc)
        self.root.destroy()

    # --- app updates (launcher version is informational, never an update prerequisite)

    def show_versions(self, newer_launcher=None):
        try:
            version = app_updates.read_version(ROOT / app_updates.VERSION_FILE)
            text = f"App {version['appVersion']} · Launcher {version['launcherVersion']}"
            if newer_launcher:
                text += f" · Full update available: launcher {newer_launcher}"
            self.version_label.configure(text=text)
        except (OSError, ValueError, KeyError):
            self.version_label.configure(text="Version information unavailable")

    def check_updates(self):
        if self.updating:
            return
        self.updating = "check"
        self.status.configure(text="Checking for updates...")
        self.update_buttons()

        def check():
            try:
                local, remote = app_updates.check_update(ROOT)
                app_new = app_updates.newer(remote["appVersion"], local["appVersion"])
                launcher_new = app_updates.newer(remote["launcherVersion"], local["launcherVersion"])
                self.output.put(("update-check", (local, remote, app_new, launcher_new)))
            except Exception as err:
                self.output.put(("update-error", str(err)))
        threading.Thread(target=check, daemon=True).start()

    def offer_update(self, result):
        local, remote, app_new, launcher_new = result
        self.updating = None
        self.update_buttons()
        self.show_versions(remote["launcherVersion"] if launcher_new else None)
        summary = (f"App: {local['appVersion']} → {remote['appVersion']}\n"
                   f"Launcher: {local['launcherVersion']} (latest {remote['launcherVersion']})")
        if launcher_new:
            summary += "\n\nA newer full installation is available. App updates leave your launcher unchanged."
            self.write(f"== Full update available: launcher {remote['launcherVersion']}\n"
                       f"   Download: {remote['directDownloadURL']}\n")
        if not app_new:
            self.status.configure(text="App is up to date")
            messagebox.showinfo("Version check", summary + "\n\nThe app is up to date.", parent=self.root)
            return
        self.status.configure(text=f"App {remote['appVersion']} available")
        if not messagebox.askyesno("Update app", summary +
                "\n\nDownload and update the app? Running app and launcher-managed servers will stop, "
                "then resume. Your launcher, user configs, models and voices are kept.", parent=self.root):
            return
        self.updating = "download"
        self.status.configure(text="Downloading app update...")
        self.update_buttons()

        def download():
            try:
                self.output.put(("update-ready", app_updates.prepare_update(ROOT, remote)))
            except Exception as err:
                self.output.put(("update-error", str(err)))
        threading.Thread(target=download, daemon=True).start()

    def stop_for_update(self, prepared):
        self.update_package = prepared
        self.update_resume = self.mode
        self.update_resume_solo = list(self.solo)
        self.update_killed.clear()
        self.next_mode = None
        self.updating = "stop"
        if self.mode:
            self.stop()
        self.wait_for_update_stop()

    def wait_for_update_stop(self):
        # Stop only processes owned by this launcher, never remote/independent servers.
        for proc in list(self.solo.values()):
            if proc and proc.poll() is None and proc.pid not in self.update_killed:
                self.update_killed.add(proc.pid)
                threading.Thread(target=stop_tree, args=(proc,), daemon=True).start()
        if self.mode or self.solo:
            self.status.configure(text="Stopping app and local servers for update...")
            self.root.after(100, self.wait_for_update_stop)
            return
        self.updating = "install"
        self.status.configure(text="Installing app update...")
        self.update_buttons()

        def install():
            try:
                app_updates.install_update(ROOT, self.update_package)
                self.output.put(("update-installed", self.update_package.remote))
            except Exception as err:
                self.output.put(("update-error", str(err)))
        threading.Thread(target=install, daemon=True).start()

    def finish_update(self, remote):
        self.update_package.close()
        self.update_package = None
        mode, self.update_resume = self.update_resume, None
        solo, self.update_resume_solo = self.update_resume_solo, []
        self.updating = None
        local = app_updates.read_version(ROOT / app_updates.VERSION_FILE)
        self.show_versions(remote["launcherVersion"] if app_updates.newer(
            remote["launcherVersion"], local["launcherVersion"]) else None)
        self.status.configure(text=f"Updated app to {remote['appVersion']}")
        self.write(f"== Updated app to {remote['appVersion']}; launcher unchanged\n")
        self.reload_config()
        if mode != "launch":
            for name in solo:
                self.start_solo(name)
        if mode:
            self.start(mode)

    def update_error(self, error):
        if self.update_package:
            self.update_package.close()
            self.update_package = None
        self.updating = None
        self.update_resume = None
        self.update_resume_solo = []
        self.status.configure(text="Update failed")
        self.update_buttons()
        self.write(f"== Update failed: {error}\n")
        messagebox.showerror("App update", error, parent=self.root)

    # --- worker thread: runs the steps, passing their output to the window

    def run_steps(self, steps):
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        result = "Dashboard exited"
        for title, argv in steps:
            if self.stopping:
                break
            self.step = title
            self.output.put(("status", title + "..."))
            # stdin: for run_servers.py's "restart" (Restart app).
            self.proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **CHILD_OPTIONS)
            self.pump(self.proc)
            code = self.proc.wait()
            if self.stopping:
                break
            if code != 0:
                result = f"{title} failed (exit code {code})"
                break
        if self.stopping:
            result = "Stopped"
        self.step = None
        self.output.put(("done", result))

    def pump(self, proc):
        """Pass proc's output to the window as it comes, and the server states
        run_servers.py reports in it."""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        partial = ""  # the last, unfinished line, checked for a server state when complete
        while chunk := proc.stdout.read1(65536):
            text = decoder.decode(chunk)
            self.output.put(("text", text))
            *lines, partial = (partial + text).split("\n")
            for line in lines:
                if match := SERVER_STATE.match(line):
                    self.output.put(("server", match.groups()))

    # --- window side

    def poll(self):
        """Show whatever the worker thread has sent since the last poll."""
        try:
            while True:
                kind, value = self.output.get_nowait()
                if kind == "text":
                    self.write(value)
                elif kind == "server":
                    name, state = value
                    self.server_states[name] = state
                    self.update_buttons()
                elif kind == "solo-done":  # a server started on its own has stopped
                    self.solo.pop(value, None)
                    if self.server_states.get(value) not in ("failed", "exited"):
                        self.server_states[value] = "stopped"
                    self.update_buttons()
                elif kind == "status":
                    self.status.configure(text=value)
                    self.update_buttons()  # e.g. Restart app, once the dashboard runs
                elif kind == "update-check":
                    self.offer_update(value)
                elif kind == "update-ready":
                    self.stop_for_update(value)
                elif kind == "update-installed":
                    self.finish_update(value)
                elif kind == "update-error":
                    self.update_error(value)
                elif kind == "done":
                    self.write(f"== {value}\n\n")
                    self.status.configure(text=value)
                    self.mode = None
                    self.stopping = False
                    # The launch's servers are gone (Stop ends them without a word);
                    # those started on their own keep running.
                    self.server_states = {name: state for name, state in self.server_states.items()
                                          if name in self.solo}
                    self.update_buttons()
                    if self.next_mode:  # Launch or Mock was pressed while the other ran
                        mode, self.next_mode = self.next_mode, None
                        self.start(mode)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def write(self, text):
        """Append output, treating a lone "\\r" like a terminal does (download progress)."""
        self.log.configure(state="normal")
        for part in re.split(r"(\r\n|\n|\r)", text):
            if part == "\r":
                self.carriage_return = True
            elif part:
                newline = part in ("\r\n", "\n")
                if self.carriage_return and not newline:
                    self.log.delete("end-1c linestart", "end-1c")
                self.carriage_return = False
                self.log.insert("end", "\n" if newline else part)
        self.log.see("end")
        self.log.configure(state="disabled")


def main():
    os.chdir(ROOT)
    if os.name == "nt":
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
