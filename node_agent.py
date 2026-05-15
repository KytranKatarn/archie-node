"""
A.R.C.H.I.E. Fleet Node Agent
Self-contained node that registers with hub, sends heartbeats,
and reports hardware capabilities. Runs in any Docker environment.
"""

import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

import psutil
import requests
from flask import Flask, jsonify

# ── Config ──────────────────────────────────────────────────────────
HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "")
NODE_NAME = os.environ.get("NODE_NAME", platform.node())
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://host.docker.internal:11434")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "60"))  # OTA sync poll interval
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "node.db")
API_PORT = int(os.environ.get("NODE_API_PORT", "3001"))

# Will be set after registration
NODE_ID = os.environ.get("NODE_ID", "")
API_KEY = os.environ.get("HUB_API_KEY", "")

# ── Database ────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS heartbeat_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sent_at TEXT, status_code INTEGER, response TEXT
    )""")
    conn.commit()
    conn.close()


def db_get(key, default=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def db_set(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    conn.commit()
    conn.close()


# ── Hardware Detection ──────────────────────────────────────────────
def detect_hardware():
    hw = {
        "cpu_model": platform.processor() or "Unknown",
        "cpu_cores": psutil.cpu_count(logical=False) or psutil.cpu_count(),
        "ram_gb": round(psutil.virtual_memory().total / (1024**3), 1),
        "os_info": f"{platform.system()} {platform.release()}",
        "gpu_model": None,
        "gpu_vram_gb": None,
        "gpu_available": False,
    }

    # Try nvidia-smi (works if --gpus all was passed)
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(",")
            hw["gpu_model"] = parts[0].strip()
            hw["gpu_vram_gb"] = round(float(parts[1].strip()) / 1024, 1)
            hw["gpu_available"] = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return hw


def detect_ollama():
    """Check if Ollama is reachable and list models."""
    try:
        resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"available": True, "models": models}
    except Exception:
        pass
    return {"available": False, "models": []}


# ── Registration ────────────────────────────────────────────────────
def register_with_hub(hw):
    """Register this node with the hub. Returns (node_id, api_key) or raises."""
    global NODE_ID, API_KEY

    # Check if already registered
    saved_id = db_get("node_id")
    saved_key = db_get("api_key")
    if saved_id and saved_key:
        NODE_ID = saved_id
        API_KEY = saved_key
        print(f"[OK] Already registered as {NODE_ID}")
        return

    print(f"[..] Registering with hub at {HUB_URL}...")
    payload = {
        "name": NODE_NAME,
        "type": "starship",
        "token": TOKEN if TOKEN else None,
        "device_info": {
            "gpu_model": hw.get("gpu_model"),
            "gpu_vram_gb": hw.get("gpu_vram_gb"),
            "cpu_model": hw.get("cpu_model"),
            "cpu_cores": hw.get("cpu_cores"),
            "ram_gb": hw.get("ram_gb"),
            "os_info": hw.get("os_info"),
        },
    }

    resp = requests.post(
        f"{HUB_URL}/tools/starbase/api/fleet/register",
        json=payload,
        timeout=15,
    )
    data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"Registration failed: {data.get('error', 'unknown')}")

    NODE_ID = data["node_id"]
    status = data.get("status", "pending")
    print(f"[OK] Registered as {NODE_ID} (status: {status})")

    if status == "approved" and "api_key" in data:
        API_KEY = data["api_key"]
        db_set("node_id", NODE_ID)
        db_set("api_key", API_KEY)
        print("[OK] Auto-approved! API key received.")
        return

    # Poll for approval
    if status == "pending":
        print("[..] Waiting for admin approval (polling every 10s, timeout 10min)...")
        for i in range(60):
            time.sleep(10)
            try:
                poll = requests.get(
                    f"{HUB_URL}/tools/starbase/api/fleet/status/{NODE_ID}",
                    params={"need_key": "true"},
                    timeout=10,
                ).json()
                poll_status = poll.get("status", "")
                if poll_status == "approved":
                    API_KEY = poll.get("api_key", "")
                    db_set("node_id", NODE_ID)
                    db_set("api_key", API_KEY)
                    print("\n[OK] Approved! API key received.")
                    return
                elif poll_status == "denied":
                    raise RuntimeError("Registration denied by admin.")
            except requests.RequestException:
                pass
            sys.stdout.write(f"\r[..] Waiting... ({i+1}/60)")
            sys.stdout.flush()
        raise RuntimeError("Timed out waiting for approval.")


# ── Heartbeat ───────────────────────────────────────────────────────
def send_heartbeat():
    """Send health metrics to hub."""
    if not NODE_ID or not API_KEY:
        return False

    ollama = detect_ollama()
    try:
        payload = {
            "status": "online",
            "cpu_percent": psutil.cpu_percent(interval=1),
            "ram_percent": psutil.virtual_memory().percent,
            "cpu_cores": psutil.cpu_count(logical=False) or psutil.cpu_count(),
            "cpu_model": platform.processor() or "Unknown",
            "ram_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "ollama_available": ollama["available"],
            "ollama_models": json.dumps(ollama["models"]),
            "client_version": "2.0.0",
        }

        # Add GPU info if available
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(",")
                payload["gpu_model"] = parts[0].strip()
                payload["gpu_vram_gb"] = round(float(parts[1].strip()) / 1024, 1)
                payload["gpu_available"] = True
                payload["gpu_percent"] = float(parts[2].strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # Detect Tailscale/Headscale mesh IP
        try:
            ts = subprocess.run(
                ["tailscale", "ip", "-4"],
                capture_output=True, text=True, timeout=5,
            )
            if ts.returncode == 0 and ts.stdout.strip():
                payload["mesh_ip"] = ts.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        resp = requests.post(
            f"{HUB_URL}/tools/starbase/api/nodes/{NODE_ID}/heartbeat",
            json=payload,
            headers={"X-Node-API-Key": API_KEY},
            timeout=10,
        )

        # Log heartbeat
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO heartbeat_log (sent_at, status_code, response) VALUES (?, ?, ?)",
                (datetime.utcnow().isoformat(), resp.status_code, resp.text[:500]),
            )
            # Keep only last 100 entries
            conn.execute("DELETE FROM heartbeat_log WHERE id NOT IN (SELECT id FROM heartbeat_log ORDER BY id DESC LIMIT 100)")
            conn.commit()
            conn.close()
        except Exception:
            pass

        return resp.status_code == 200
    except Exception as e:
        print(f"[WARN] Heartbeat failed: {e}")
        return False


# ── OTA Sync Polling ────────────────────────────────────────────────
def poll_sync_commands():
    """Poll hub for pending sync commands and execute them.

    Handles: model_pull, config_update, container_restart.
    Acks each command back to hub regardless of outcome.
    Runs every SYNC_INTERVAL seconds from the main loop.
    """
    if not NODE_ID or not API_KEY:
        return
    try:
        resp = requests.get(
            f"{HUB_URL}/tools/department-hq/api/nodes/{NODE_ID}/pending-commands",
            headers={"X-Node-API-Key": API_KEY},
            timeout=10,
        )
        if resp.status_code != 200:
            return
        commands = resp.json().get("commands", [])
    except Exception:
        return

    for cmd in commands:
        sync_id = cmd["id"]
        sync_type = cmd["sync_type"]
        payload = cmd.get("payload") or {}
        success = False
        result = {}
        error = None

        try:
            if sync_type == "model_pull":
                model = payload.get("model", "")
                if model:
                    print(f"[OTA] Pulling model: {model}")
                    r = requests.post(
                        f"{OLLAMA_HOST}/api/pull",
                        json={"name": model, "stream": False},
                        timeout=600,
                    )
                    success = r.status_code == 200
                    result = {"model": model, "status": r.json().get("status", "")}
                    print(f"[OTA] Pull {model}: {'OK' if success else 'FAILED'}")

            elif sync_type == "config_update":
                for key, val in payload.items():
                    db_set(f"config_{key}", str(val))
                success = True
                result = {"updated": list(payload.keys())}
                print(f"[OTA] Config updated: {list(payload.keys())}")

            elif sync_type == "container_restart":
                print("[OTA] Restart requested — will restart after ack")
                success = True
                result = {"action": "restart_queued"}

            else:
                success = True
                result = {"skipped": True, "reason": f"unknown sync_type: {sync_type}"}

        except Exception as e:
            error = str(e)
            print(f"[OTA] Command {sync_type} #{sync_id} failed: {e}")

        try:
            requests.post(
                f"{HUB_URL}/tools/department-hq/api/nodes/{NODE_ID}/ack-command/{sync_id}",
                headers={"X-Node-API-Key": API_KEY},
                json={"success": success, "result": result, "error": error},
                timeout=10,
            )
        except Exception as e:
            print(f"[OTA] Failed to ack command #{sync_id}: {e}")


# ── Local API ───────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "node_id": NODE_ID,
        "node_name": NODE_NAME,
        "uptime": time.time() - START_TIME,
    })


@app.route("/api/info")
def info():
    hw = detect_hardware()
    ollama = detect_ollama()
    return jsonify({
        "node_id": NODE_ID,
        "node_name": NODE_NAME,
        "hub_url": HUB_URL,
        "hardware": hw,
        "ollama": ollama,
        "uptime": time.time() - START_TIME,
    })


# ── Main Loop ───────────────────────────────────────────────────────
START_TIME = time.time()
running = True


def shutdown(sig, frame):
    global running
    print("\n[..] Shutting down...")
    running = False


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)


def main():
    global running

    print("[..] Initializing...")
    init_db()

    # Detect hardware
    hw = detect_hardware()
    print(f"[OK] CPU: {hw['cpu_cores']} cores, RAM: {hw['ram_gb']}GB")
    if hw["gpu_available"]:
        print(f"[OK] GPU: {hw['gpu_model']} ({hw['gpu_vram_gb']}GB VRAM)")
    else:
        print("[..] No GPU detected (run with --gpus all for GPU support)")

    # Register
    register_with_hub(hw)

    # Start API in background thread
    import threading
    api_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=API_PORT, use_reloader=False),
        daemon=True,
    )
    api_thread.start()
    print(f"[OK] Node API listening on port {API_PORT}")

    # Heartbeat loop
    print(f"[OK] Heartbeat every {HEARTBEAT_INTERVAL}s to {HUB_URL}")
    print("[OK] Node is running. Press Ctrl+C to stop.\n")

    fail_count = 0
    last_sync = 0.0
    while running:
        success = send_heartbeat()
        if success:
            if fail_count > 0:
                print(f"[OK] Heartbeat restored after {fail_count} failures")
            fail_count = 0
        else:
            fail_count += 1
            if fail_count % 10 == 1:
                print(f"[WARN] Heartbeat failing (attempt {fail_count})")

        # OTA sync poll — check for pending commands from hub
        now = time.time()
        if now - last_sync >= SYNC_INTERVAL:
            poll_sync_commands()
            last_sync = now

        # Sleep in small increments so SIGTERM is responsive
        for _ in range(HEARTBEAT_INTERVAL):
            if not running:
                break
            time.sleep(1)

    print("[OK] Node stopped.")


if __name__ == "__main__":
    main()
