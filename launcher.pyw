#!/usr/bin/env python3
"""Audio Translation Dashboard - launcher.

A window that does what launch.sh does: installs the servers and downloads
their models if needed, then starts the servers and the dashboard, showing
their output. Stop, or closing the window, stops everything.

With [launcher] show_mock_button = true in config_local.toml, a Mock button
runs the dashboard alone with sample data instead (what mock.sh does).
Launch and Mock each stop the other first, since both use the dashboard's port.

Generate config: detects the GPU's memory (nvidia-smi), suggests a recipe from
config/recipes/, previews it (its fragments from config/fragments/ pasted
together, plus start = false for the servers it turns off) and saves it to
config_generated.toml, which the next Launch uses: on top of the defaults,
under config_local.toml.

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
from tkinter import ttk
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


def assemble(name, recipe):
    """The config text for a recipe: its fragments pasted in order, then a
    start = false section for each server in its "off" list."""
    parts = [f"# Generated by launcher.pyw from config/recipes/{name}.json:\n"
             f"# {recipe.get('description', '')}\n"]
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
        self.save_button = ttk.Button(row, text="Save", command=self.save, state="disabled")
        self.save_button.pack(side="left", padx=(6, 0))
        self.details = ttk.Label(top, text="")  # the selected recipe's VRAM and description
        self.details.pack(anchor="w", pady=(6, 0))
        self.status = ttk.Label(top, text="")
        self.status.pack(anchor="w", pady=(6, 0))

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
        name = self.selected()
        self.details.configure(text=self.describe(name))
        try:
            self.text = assemble(name, self.recipes[name])
            self.status.configure(text=f"Save writes {GENERATED_FILE.name}.")
            self.save_button.configure(state="normal")
        except (ValueError, tomllib.TOMLDecodeError) as err:
            self.text = ""
            self.status.configure(text=f"Can't use this recipe: {err}")
            self.save_button.configure(state="disabled")
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
        bar = ttk.Frame(root, padding=8)
        bar.pack(fill="x")
        self.launch_button = ttk.Button(bar, text="Launch", command=lambda: self.start("launch"))
        self.launch_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Open dashboard", command=self.open_dashboard).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Generate config", command=self.generate_config).pack(side="left", padx=(6, 0))
        self.generate_window = None
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="left", padx=(12, 0))
        self.mock_button = ttk.Button(bar, text="Mock", command=lambda: self.start("mock"))
        if setting("launcher", "show_mock_button", False):
            self.mock_button.pack(side="right")

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
        self.launch_button.configure(state="disabled" if busy or self.mode == "launch" else "normal")
        self.mock_button.configure(state="disabled" if busy or self.mode == "mock" else "normal")
        self.stop_button.configure(state="normal" if self.mode and not busy else "disabled")

    def generate_config(self):
        """Open the Generate config window (or bring it to the front)."""
        if self.generate_window and self.generate_window.window.winfo_exists():
            self.generate_window.window.lift()
        else:
            self.generate_window = GenerateConfigWindow(self.root)

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
