#!/usr/bin/env python3
"""Audio Translation Dashboard - launcher.

A window that does what launch.sh does: installs the servers and downloads
their models if needed, then starts the servers and the dashboard, showing
their output. Stop, or closing the window, stops everything.

With [launcher] show_mock_button = true in config_local.toml, a Mock button
runs the dashboard alone with sample data instead (what mock.sh does).
Launch and Mock each stop the other first, since both use the dashboard's port.

Generate config: detects the GPU's memory (nvidia-smi), suggests a recipe from
config/recipes/, takes the two languages to translate between, previews the
result (the languages, the recipe's fragments from config/fragments/ pasted
together, and start = false for the servers it turns off) and saves it to
config_generated.toml, which the next Launch uses: on top of the defaults,
under config_local.toml.

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
else:
    # Own process group, so Stop can signal the whole tree.
    CHILD_OPTIONS = {"start_new_session": True}


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


def detect_gpu():
    """(name, VRAM in GiB) of the NVIDIA GPU with the most memory; raises
    RuntimeError with the reason if nvidia-smi can't tell."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
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
        name, _, mib = line.rpartition(",")
        if mib.strip().isdigit():
            gpus.append((name.strip(), int(mib) / 1024))
    if not gpus:
        raise RuntimeError(f"no GPUs in nvidia-smi's output: {result.stdout.strip()[:200]!r}")
    return max(gpus, key=lambda gpu: gpu[1])


def suggest_recipe(recipes, vram_gib):
    """The recipe for the largest card that fits: cards report a little under
    their nominal size (a 24 GB card: about 23.99 GiB), hence the margin."""
    fitting = [name for name, recipe in recipes.items() if recipe.get("vram_gb", 0) <= vram_gib + 0.5]
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


def assemble(name, recipe, languages=None):
    """The config text for a recipe: the [languages] section if languages is
    given, then the recipe's fragments pasted in order, then a start = false
    section for each server in its "off" list."""
    parts = [f"# Generated by launcher.pyw from config/recipes/{name}.json:\n"
             f"# {recipe.get('description', '')}\n"]
    if languages:
        # json.dumps quotes and escapes a string the way TOML's basic strings need.
        parts.append("[languages]\n" + "".join(f"language{i} = {json.dumps(language)}\n"
                                               for i, language in enumerate(languages, 1)))
    for fragment in recipe.get("fragments", []):
        path = FRAGMENTS_DIR / f"{fragment}.toml"
        if not path.is_file():
            raise ValueError(f"recipe {name}: fragment {fragment!r} not found ({path.name})")
        parts.append(f"# --- fragment {fragment}\n\n" + path.read_text(encoding="utf-8").strip() + "\n")
    for server in recipe.get("off", []):
        parts.append(f"# --- off\n\n[api_servers.{server}]\nstart = false\n")
    text = "\n".join(parts)
    tomllib.loads(text)  # raises if the fragments don't fit together (e.g. a table defined twice)
    return text


class GenerateConfigWindow:
    """Detect the GPU, suggest a recipe, preview it, and save it to config_generated.toml."""

    def __init__(self, parent):
        self.window = tk.Toplevel(parent)
        self.window.title("Generate config")
        self.recipes = load_recipes()
        self.text = ""

        top = ttk.Frame(self.window, padding=8)
        top.pack(fill="x")
        self.gpu = ttk.Label(top, text="Checking the GPU...")
        self.gpu.pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Recipe:").pack(side="left")
        self.choice = tk.StringVar()
        self.combo = ttk.Combobox(row, textvariable=self.choice, state="readonly", width=24,
                                  values=list(self.recipes))
        self.combo.pack(side="left", padx=(6, 0))
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.show())
        self.details = ttk.Label(top, text="")  # the selected recipe's VRAM and description
        self.details.pack(anchor="w", pady=(6, 0))

        # The two languages to translate between; the preview follows as you type.
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(8, 0))
        self.languages = [tk.StringVar(value=value) for value in initial_languages()]
        for i, var in enumerate(self.languages, 1):
            ttk.Label(row, text=f"Language {i}:").pack(side="left", padx=(12 if i > 1 else 0, 0))
            ttk.Entry(row, textvariable=var, width=20).pack(side="left", padx=(6, 0))
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
        self.preview.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        if not self.recipes:
            self.gpu.configure(text=f"No recipes found in {RECIPES_DIR}")
            return
        threading.Thread(target=self.check_gpu, daemon=True).start()

    def describe(self, name):
        recipe = self.recipes[name]
        return recipe.get("description") or f"For {recipe.get('vram_gb', '?')} GB of VRAM."

    def check_gpu(self):
        """In a worker thread (nvidia-smi can take a moment); the result goes back to the window."""
        try:
            name, vram = detect_gpu()
            found = f"{name}, {vram:.1f} GiB", suggest_recipe(self.recipes, vram)
        except RuntimeError as err:
            found = f"GPU not detected: {err}", None
        self.window.after(0, self.gpu_found, *found)

    def gpu_found(self, description, suggested):
        if suggested:
            self.gpu.configure(text=f"GPU: {description}. Suggested recipe: {suggested}")
        elif "not detected" in description:
            self.gpu.configure(text=f"{description}. Choose a recipe.")
        else:
            self.gpu.configure(text=f"GPU: {description}. No recipe fits; choose one.")
        names = list(self.recipes)
        self.combo.current(names.index(suggested) if suggested else 0)
        self.show()

    def selected(self):
        return list(self.recipes)[self.combo.current()]

    def show(self):
        if self.combo.current() < 0:
            return  # no recipe selected yet (the GPU check is still running)
        name = self.selected()
        self.details.configure(text=self.describe(name))
        languages = [var.get().strip() for var in self.languages]
        try:
            if not all(languages):
                raise ValueError("enter both languages")
            self.text = assemble(name, self.recipes[name], languages)
            self.status.configure(text=f"Save writes {GENERATED_FILE.name}.")
            set_enabled(self.save_button, True)
        except (ValueError, tomllib.TOMLDecodeError) as err:
            self.text = ""
            self.status.configure(text=f"Can't save: {err}")
            set_enabled(self.save_button, False)
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", self.text)
        self.preview.configure(state="disabled")

    def save(self):
        temp = GENERATED_FILE.with_name(GENERATED_FILE.name + ".tmp")
        temp.write_text(self.text, encoding="utf-8", newline="\n")
        temp.replace(GENERATED_FILE)
        self.status.configure(text=f"Saved {self.selected()} to {GENERATED_FILE.name}: used from the next "
                                   f"Launch ({configuration.CONFIG_LOCAL_FILE.name} still takes priority)")


class Launcher:
    def __init__(self, root):
        self.root = root
        self.proc = None  # the step running now
        self.mode = None  # what's running: "launch", "mock" or None
        self.next_mode = None  # to start once the current run has stopped
        self.stopping = False
        self.output = queue.Queue()  # (kind, value) from the worker thread to the window
        self.carriage_return = False  # a "\r" was written: the next text replaces the line

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
        self.mock_button = tool_button(bar, "Mock", lambda: self.start("mock"))
        if setting("launcher", "show_mock_button", False):
            self.mock_button.pack(side="right")
        tool_button(bar, "Generate config", self.generate_config).pack(side="right", padx=(0, 6))
        self.kill_button = tool_button(bar, "Kill servers", self.kill_servers)
        self.kill_button.pack(side="right", padx=(0, 6))
        self.generate_window = None
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
        if self.mode:
            self.next_mode = mode  # started when the current run reports done
            self.stop()
            return
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
        (or a switch is under way); Stop while anything runs."""
        busy = self.stopping and self.mode is not None
        set_enabled(self.launch_button, not busy and self.mode != "launch")
        set_enabled(self.mock_button, not busy and self.mode != "mock")
        set_enabled(self.stop_button, bool(self.mode) and not busy)

    def generate_config(self):
        """Open the Generate config window (or bring it to the front)."""
        if self.generate_window and self.generate_window.window.winfo_exists():
            self.generate_window.window.lift()
        else:
            self.generate_window = GenerateConfigWindow(self.root)

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
        self.next_mode = None
        if self.mode:
            self.stop()
        self.root.destroy()

    # --- worker thread: runs the steps, passing their output to the window

    def run_steps(self, steps):
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        result = "Dashboard exited"
        for title, argv in steps:
            if self.stopping:
                break
            self.output.put(("status", title + "..."))
            self.proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, **CHILD_OPTIONS)
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while chunk := self.proc.stdout.read1(65536):
                self.output.put(("text", decoder.decode(chunk)))
            code = self.proc.wait()
            if self.stopping:
                break
            if code != 0:
                result = f"{title} failed (exit code {code})"
                break
        if self.stopping:
            result = "Stopped"
        self.output.put(("done", result))

    # --- window side

    def poll(self):
        """Show whatever the worker thread has sent since the last poll."""
        try:
            while True:
                kind, value = self.output.get_nowait()
                if kind == "text":
                    self.write(value)
                elif kind == "status":
                    self.status.configure(text=value)
                elif kind == "done":
                    self.write(f"== {value}\n\n")
                    self.status.configure(text=value)
                    self.mode = None
                    self.stopping = False
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
