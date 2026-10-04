"""Install the experiment as a user service without requiring root."""

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


def quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--world", type=Path, help="Prototype World definition, defaulting to the team experiment")
    parser.add_argument("--service-name", help="User service name ending in .service; defaults to a name for this world")
    parser.add_argument("--url", help="Client URL used by the access command, including a proxy URL if applicable")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("The port must be between 1 and 65535.")
    if any(char in args.host for char in '\n\r\"\\ '):
        parser.error("The listen host must be a host name or an IP address.")
    codex = shutil.which("codex")
    if not codex:
        parser.error("Install Codex and run codex login before installing the service.")
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    from world.definition import WorldDefinition
    from world.schema import WorldError
    world_path = (args.world or root / "definitions" / "team.world.json").expanduser().resolve()
    try:
        definition = WorldDefinition.load(world_path)
    except (OSError, ValueError, WorldError) as error:
        parser.error(str(error))
    service_name = args.service_name or ("world-experiment.service" if definition.id == "team" else f"world-{definition.id}-experiment.service")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.service", service_name):
        parser.error("The service name must be a simple name ending in .service.")
    default_data = "~/.local/share/world" if definition.id == "team" else f"~/.local/share/world-{definition.id}-experiment"
    data_dir = (args.data_dir or Path(os.environ.get("WORLD_DATA_DIR", default_data))).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(data_dir, 0o700)
    host = "localhost" if args.host in {"127.0.0.1", "0.0.0.0"} else args.host
    if ":" in host:
        host = "[" + host + "]"
    url = args.url or f"http://{host}:{args.port}"
    if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
        parser.error("The client URL must be an absolute HTTP or HTTPS URL.")
    units = Path.home() / ".config" / "systemd" / "user"
    units.mkdir(parents=True, exist_ok=True)
    unit_path = units / service_name
    marker = "# Installed by World's scripts/install-service.sh"
    if unit_path.exists() and marker not in unit_path.read_text():
        parser.error(f"An existing unrelated service already uses {unit_path}.")
    unit = f"""{marker}
[Unit]
Description=World {definition.id} experiment
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={str(root).replace('%', '%%')}
ExecStart={quote(sys.executable)} -m world.server --world {quote(world_path)} --host {quote(args.host)} --port {args.port} --data-dir {quote(data_dir)}
Environment=PYTHONUNBUFFERED=1
Environment={quote('WORLD_CODEX_BIN=' + codex)}
Restart=on-failure
RestartSec=3
TimeoutStopSec=20
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit)
    (data_dir / "client-url").write_text(url.rstrip("/") + "/\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", service_name], check=True)
    subprocess.run(["systemctl", "--user", "restart", service_name], check=True)
    subprocess.run(["systemctl", "--user", "is-active", service_name], check=True)
    print("World is running at " + url)
    print("Print a private sign-in link with:")
    print(f"  cd {root}")
    print(f"  {sys.executable} -m world.server access --world {quote(world_path)} --data-dir {quote(data_dir)} --actor {definition.actor_ids[0]}")


if __name__ == "__main__":
    main()
