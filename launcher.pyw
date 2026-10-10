#!/usr/bin/env python3
"""Audio Translation Dashboard - launcher.

A window that does what launch.sh does: installs the servers and downloads
their models if needed, then starts the servers and the dashboard, showing their output. Stop, or closing the window,
stops everything.

Double-click to run (.pyw: no console window), or: python launcher.pyw

Standard library only (Python 3.11+, with tkinter).
"""

import codecs
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
CONFIG_LOCAL_FILE = ROOT / "config_local.toml"
DEFAULTS = ROOT / "config" / "defaults.toml"

# Run the steps with python.exe, not the pythonw.exe running this window: with
# a (hidden) console of their own, the servers they start don't each pop up one.
_exe = Path(sys.executable)
_console_exe = _exe.with_name("python.exe")
PYTHON = str(_console_exe if _exe.name.lower() == "pythonw.exe" and _console_exe.is_file() else _exe)

# The launch.sh steps, in order.
STEPS = [
    ("Installing servers", [PYTHON, "scripts/install_api_servers.py"]),
    ("Downloading models", [PYTHON, "scripts/download_models.py"]),
    ("Running", [PYTHON, "scripts/run_servers.py", PYTHON, "app.py"]),
]

if os.name == "nt":
    # Hidden console; the servers inherit it.
    CHILD_OPTIONS = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    # Own process group, so Stop can signal the whole tree.
    CHILD_OPTIONS = {"start_new_session": True}


def stop_tree(proc):
    """Stop proc and everything it started (the servers and the dashboard)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(proc.pid, signal.SIGINT)  # like Ctrl+C: run_servers.py stops the servers


class Launcher:
    def __init__(self, root):
        self.root = root
        self.proc = None  # the step running now
        self.running = False
        self.stopping = False
        self.output = queue.Queue()  # (kind, value) from the worker thread to the window
        self.carriage_return = False  # a "\r" was written: the next text replaces the line

        root.title("Audio Translation Dashboard - Launcher")
        bar = ttk.Frame(root, padding=8)
        bar.pack(fill="x")
        self.launch_button = ttk.Button(bar, text="Launch", command=self.launch)
        self.launch_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Open dashboard", command=self.open_dashboard).pack(side="left", padx=(6, 0))
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="left", padx=(12, 0))

        # Sized in characters, like a terminal; the window fits around it.
        self.log = ScrolledText(root, font=("Consolas", 10), width=120, height=30, wrap="word",
                                state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    # --- buttons

    def launch(self):
        self.running = True
        self.stopping = False
        self.launch_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        threading.Thread(target=self.run_steps, daemon=True).start()

    def stop(self):
        self.stopping = True
        self.stop_button.configure(state="disabled")
        self.status.configure(text="Stopping...")
        if self.proc and self.proc.poll() is None:
            stop_tree(self.proc)

    def open_dashboard(self):
        # The port from config_local.toml if it sets one, else from the defaults.
        port = None
        for path in (CONFIG_LOCAL_FILE, DEFAULTS):
            if port is None and path.is_file():
                with open(path, "rb") as f:
                    port = tomllib.load(f).get("server", {}).get("port")
        webbrowser.open(f"http://localhost:{port}/")

    def close(self):
        if self.running:
            self.stop()
        self.root.destroy()

    # --- worker thread: runs the steps, passing their output to the window

    def run_steps(self):
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        result = "Dashboard exited"
        for title, argv in STEPS:
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
                    self.running = False
                    self.launch_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
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
