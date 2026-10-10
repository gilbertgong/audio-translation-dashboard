"""Print the command line or URL for an API server, as configured in config.toml.
Used by launch.sh.

Usage, from the project root:
    python scripts/server_command.py NAME          # command, NUL-separated
    python scripts/server_command.py NAME --url    # base URL, e.g. http://127.0.0.1:8081

NAME is a server under [api_servers] (e.g. whisper, llama). The command is the
server executable, its model (from [models]), host, port and extra args.
"""

import argparse
import sys
from pathlib import Path

from common import config_path, load_config, model_path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("name")
    parser.add_argument("--url", action="store_true", help="print the server's base URL")
    parser.add_argument("--config", default=Path("config.toml"), type=Path)
    args = parser.parse_args()

    cfg = load_config(args.config)
    server = cfg["api_servers"].get(args.name)
    if not isinstance(server, dict):
        raise SystemExit(f"error: no [api_servers.{args.name}] section in {args.config}")
    host, port = server["host"], server["port"]

    if args.url:
        print(f"http://{host}:{port}")
        return

    install_dir = config_path(cfg, cfg["api_servers"].get("install_dir", "api_servers"))
    exe = install_dir / args.name / server["executable"]
    if not exe.is_file() and exe.with_suffix(".exe").is_file():
        exe = exe.with_suffix(".exe")
    model = model_path(cfg, args.name)
    for path, hint in [(exe, "install_api_servers.py"), (model, "download_models.py")]:
        if not path.is_file():
            raise SystemExit(f"error: {path} not found (run scripts/{hint})")

    argv = [str(exe), "-m", str(model), "--host", host, "--port", str(port), *server.get("args", [])]
    sys.stdout.write("\0".join(argv) + "\0")


if __name__ == "__main__":
    main()
