# ============================
# Ouroboros — Cyberclaw native Linux launcher
# ============================
# Cyberclaw-optimized launcher for native Linux operation.
# Runs on Ubuntu 24.04, cohabits with Phobos (OpenClaw).
#
# Key differences from docker_launcher:
# - Native filesystem paths (no Docker volume abstraction)
# - Discord + Telegram dual-channel operation
# - Systemd service compatibility
# - GPU access ready (PyTorch/CUDA)
#
# Usage:
#   cd /media/mars/AI/ouroboros
#   pyenv local 3.12.3
#   python -m venv .venv && source .venv/bin/activate
#   pip install -r requirements.txt
#   python cyberclaw_launcher.py
#
# All heavy logic lives in supervisor/ — this file is thin glue.

import logging
import os, sys, json, time, uuid, pathlib, subprocess, datetime, threading, queue as _queue_mod
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ----------------------------
# 0) Install deps (if needed)
# ----------------------------
def install_launcher_deps() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "openai>=1.0.0", "requests"],
        check=True,
    )

install_launcher_deps()

def ensure_claude_code_cli() -> bool:
    """Best-effort install of Claude Code CLI."""
    local_bin = str(pathlib.Path.home() / ".local" / "bin")
    if local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"
    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    if has_cli:
        return True
    subprocess.run(["bash", "-lc", "curl -fsSL https://claude.ai/install.sh | bash"], check=False)
    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    if has_cli:
        return True
    subprocess.run(["bash", "-lc", "command -v npm >/dev/null 2>&1 && npm install -g @anthropic-ai/claude-code"], check=False)
    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    return has_cli

# apply_patch shim (needed for Claude Code integration)
from ouroboros.apply_patch import install as install_apply_patch
from ouroboros.llm import DEFAULT_LIGHT_MODEL
install_apply_patch()

# ----------------------------
# 1) Secrets — from env only
# ----------------------------
def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = os.environ.get(name, default)
    if required:
        assert v is not None and str(v).strip() != "", \
            f"Missing required env var: {name}. Set it in your .env file or export it."
    return v

def get_cfg(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return v
    return default

def _parse_int_cfg(raw: Optional[str], default: int, minimum: int = 0) -> int:
    try:
        val = int(str(raw))
    except Exception:
        val = default
    return max(minimum, val)

import re
OPENROUTER_API_KEY = get_secret("OPENROUTER_API_KEY", required=True)
TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
DISCORD_BOT_TOKEN = get_secret("DISCORD_BOT_TOKEN", default="")  # Optional for now
TOTAL_BUDGET_DEFAULT = get_secret("TOTAL_BUDGET", required=True)
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", required=True)

_raw_budget = str(TOTAL_BUDGET_DEFAULT or "")
_clean_budget = re.sub(r'[^0-9.\-]', '', _raw_budget)
TOTAL_BUDGET_LIMIT = float(_clean_budget) if _clean_budget else 0.0

OPENAI_API_KEY = get_secret("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY", default="")
GITHUB_USER = get_cfg("GITHUB_USER", default=None)
GITHUB_REPO = get_cfg("GITHUB_REPO", default=None)
assert GITHUB_USER and str(GITHUB_USER).strip(), "GITHUB_USER not set."
assert GITHUB_REPO and str(GITHUB_REPO).strip(), "GITHUB_REPO not set."

MAX_WORKERS = int(get_cfg("OUROBOROS_MAX_WORKERS", default="5") or "5")
MODEL_MAIN = get_cfg("OUROBOROS_MODEL", default="anthropic/claude-sonnet-4-5")
MODEL_CODE = get_cfg("OUROBOROS_MODEL_CODE", default="anthropic/claude-sonnet-4-5")
MODEL_LIGHT = get_cfg("OUROBOROS_MODEL_LIGHT", default=DEFAULT_LIGHT_MODEL)

BUDGET_REPORT_EVERY_MESSAGES = 10
SOFT_TIMEOUT_SEC = max(60, int(get_cfg("OUROBOROS_SOFT_TIMEOUT_SEC", default="600") or "600"))
HARD_TIMEOUT_SEC = max(120, int(get_cfg("OUROBOROS_HARD_TIMEOUT_SEC", default="1800") or "1800"))
DIAG_HEARTBEAT_SEC = _parse_int_cfg(get_cfg("OUROBOROS_DIAG_HEARTBEAT_SEC", default="30"), default=30, minimum=0)
DIAG_SLOW_CYCLE_SEC = _parse_int_cfg(get_cfg("OUROBOROS_DIAG_SLOW_CYCLE_SEC", default="20"), default=20, minimum=0)

# Propagate to child processes / workers
os.environ["OPENROUTER_API_KEY"] = str(OPENROUTER_API_KEY)
os.environ["OPENAI_API_KEY"] = str(OPENAI_API_KEY or "")
# Only export when set: an empty/dead ANTHROPIC_API_KEY overrides Claude CLI's stored OAuth login
if str(ANTHROPIC_API_KEY or "").strip():
    os.environ["ANTHROPIC_API_KEY"] = str(ANTHROPIC_API_KEY)
os.environ["GITHUB_USER"] = str(GITHUB_USER)
os.environ["GITHUB_REPO"] = str(GITHUB_REPO)
os.environ["OUROBOROS_MODEL"] = str(MODEL_MAIN or "anthropic/claude-sonnet-4-5")
os.environ["OUROBOROS_MODEL_CODE"] = str(MODEL_CODE or "anthropic/claude-sonnet-4-5")
if MODEL_LIGHT:
    os.environ["OUROBOROS_MODEL_LIGHT"] = str(MODEL_LIGHT)
os.environ["OUROBOROS_DIAG_HEARTBEAT_SEC"] = str(DIAG_HEARTBEAT_SEC)
os.environ["OUROBOROS_DIAG_SLOW_CYCLE_SEC"] = str(DIAG_SLOW_CYCLE_SEC)
os.environ["TELEGRAM_BOT_TOKEN"] = str(TELEGRAM_BOT_TOKEN)
if DISCORD_BOT_TOKEN:
    os.environ["DISCORD_BOT_TOKEN"] = str(DISCORD_BOT_TOKEN)

if str(ANTHROPIC_API_KEY or "").strip() or (pathlib.Path.home() / ".claude" / ".credentials.json").exists():
    ensure_claude_code_cli()

# ----------------------------
# 2) Cyberclaw filesystem paths
# ----------------------------
# Default: /media/mars/AI/ouroboros/data
# Override with OUROBOROS_DRIVE_ROOT env var if needed
DRIVE_ROOT = pathlib.Path(
    os.environ.get("OUROBOROS_DRIVE_ROOT", "/media/mars/AI/ouroboros/data")
).resolve()

REPO_DIR = pathlib.Path(
    os.environ.get("OUROBOROS_REPO_DIR", pathlib.Path(__file__).parent)
).resolve()

for sub in ["state", "logs", "memory", "index", "locks", "archive"]:
    (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)

# Clear stale owner mailbox files from previous session
try:
    from ouroboros.owner_inject import get_pending_path
    _stale_inject = get_pending_path(DRIVE_ROOT)
    if _stale_inject.exists():
        _stale_inject.unlink(missing_ok=True)
    _mailbox_dir = DRIVE_ROOT / "memory" / "owner_mailbox"
    if _mailbox_dir.exists():
        for _f in _mailbox_dir.iterdir():
            _f.unlink(missing_ok=True)
except Exception:
    pass

CHAT_LOG_PATH = DRIVE_ROOT / "logs" / "chat.jsonl"
if not CHAT_LOG_PATH.exists():
    CHAT_LOG_PATH.write_text("", encoding="utf-8")

# ----------------------------
# 3) Git constants
# ----------------------------
BRANCH_DEV = "ouroboros"
BRANCH_STABLE = "ouroboros-stable"
REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"

# ----------------------------
# 4) Initialize supervisor modules
# ----------------------------
from supervisor.state import (
    init as state_init, load_state, save_state, append_jsonl,
    update_budget_from_usage, status_text, rotate_chat_log_if_needed,
    init_state,
)
state_init(DRIVE_ROOT, TOTAL_BUDGET_LIMIT)
init_state()

from supervisor.telegram import (
    init as telegram_init, TelegramClient, send_with_budget, log_chat,
)
TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))
telegram_init(
    drive_root=DRIVE_ROOT,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    budget_report_every=BUDGET_REPORT_EVERY_MESSAGES,
    tg_client=TG,
)

# Discord integration (if token provided)
DISCORD_CLIENT = None
if DISCORD_BOT_TOKEN:
    try:
        from supervisor.discord_gateway import DiscordClient, init as discord_init
        DISCORD_CLIENT = DiscordClient(str(DISCORD_BOT_TOKEN))
        discord_init(
            drive_root=DRIVE_ROOT,
            discord_client=DISCORD_CLIENT,
        )
        log.info("Discord integration enabled")
    except Exception as e:
        log.warning(f"Discord integration failed to initialize: {e}")
        DISCORD_CLIENT = None

from supervisor.git_ops import (
    init as git_ops_init, ensure_repo_present, checkout_and_reset,
    sync_runtime_dependencies, import_test, safe_restart,
)
git_ops_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, remote_url=REMOTE_URL,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

from supervisor.queue import (
    enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
    persist_queue_snapshot, restore_pending_from_snapshot,
    cancel_task_by_id, queue_review_task, sort_pending,
)

from supervisor.workers import (
    init as workers_init, get_event_q, WORKERS, PENDING, RUNNING,
    spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
    handle_chat_direct, _get_chat_agent, auto_resume_after_restart,
)
workers_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, max_workers=MAX_WORKERS,
    soft_timeout=SOFT_TIMEOUT_SEC, hard_timeout=HARD_TIMEOUT_SEC,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

from supervisor.events import dispatch_event

# ----------------------------
# 5) Bootstrap repo
# ----------------------------
ensure_repo_present()
ok, msg = safe_restart(reason="bootstrap", unsynced_policy="rescue_and_reset")
assert ok, f"Bootstrap failed: {msg}"

# ----------------------------
# 6) Start workers
# ----------------------------
kill_workers()
spawn_workers(MAX_WORKERS)
restored_pending = restore_pending_from_snapshot()
persist_queue_snapshot(reason="startup")
if restored_pending > 0:
    st_boot = load_state()
    if st_boot.get("owner_chat_id"):
        send_with_budget(int(st_boot["owner_chat_id"]),
                         f"♻️ Restored pending queue from snapshot: {restored_pending} tasks.")

append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "type": "launcher_start",
    "launcher": "cyberclaw",
    "branch": load_state().get("current_branch"),
    "sha": load_state().get("current_sha"),
    "max_workers": MAX_WORKERS,
    "model_default": MODEL_MAIN, "model_code": MODEL_CODE, "model_light": MODEL_LIGHT,
    "soft_timeout_sec": SOFT_TIMEOUT_SEC, "hard_timeout_sec": HARD_TIMEOUT_SEC,
    "drive_root": str(DRIVE_ROOT),
    "repo_dir": str(REPO_DIR),
    "discord_enabled": DISCORD_CLIENT is not None,
})

# ----------------------------
# 6.1) Auto-resume after restart
# ----------------------------
auto_resume_after_restart()

# ----------------------------
# 6.2) Direct-mode watchdog
# ----------------------------
def _chat_watchdog_loop():
    """Monitor direct-mode chat agent for hangs."""
    soft_warned = False
    while True:
        time.sleep(30)
        try:
            agent = _get_chat_agent()
            if not agent._busy:
                soft_warned = False
                continue
            now = time.time()
            idle_sec = now - agent._last_progress_ts
            total_sec = now - agent._task_started_ts
            if idle_sec >= HARD_TIMEOUT_SEC:
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                                     f"⚠️ Task stuck ({int(total_sec)}s). Restarting agent.")
                reset_chat_agent()
                soft_warned = False
                continue
            if idle_sec >= SOFT_TIMEOUT_SEC and not soft_warned:
                soft_warned = True
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                                     f"⏱️ Task running {int(total_sec)}s, "
                                     f"last progress {int(idle_sec)}s ago. Continuing.")
        except Exception:
            log.debug("Watchdog check failed", exc_info=True)

_watchdog_thread = threading.Thread(target=_chat_watchdog_loop, daemon=True)
_watchdog_thread.start()

# ----------------------------
# 6.3) Background consciousness
# ----------------------------
from ouroboros.consciousness import BackgroundConsciousness

def _get_owner_chat_id() -> Optional[int]:
    try:
        st = load_state()
        cid = st.get("owner_chat_id")
        return int(cid) if cid else None
    except Exception:
        return None

_consciousness = BackgroundConsciousness(
    drive_root=DRIVE_ROOT,
    repo_dir=REPO_DIR,
    event_queue=get_event_q(),
    owner_chat_id_fn=_get_owner_chat_id,
)

def reset_chat_agent():
    import supervisor.workers as _w
    _w._chat_agent = None

# ----------------------------
# 7) Main loop (Telegram + Discord)
# ----------------------------
import types
_event_ctx = types.SimpleNamespace(
    DRIVE_ROOT=DRIVE_ROOT,
    REPO_DIR=REPO_DIR,
    BRANCH_DEV=BRANCH_DEV,
    BRANCH_STABLE=BRANCH_STABLE,
    TG=TG,
    DISCORD=DISCORD_CLIENT,
    WORKERS=WORKERS,
    PENDING=PENDING,
    RUNNING=RUNNING,
    MAX_WORKERS=MAX_WORKERS,
    send_with_budget=send_with_budget,
    load_state=load_state,
    save_state=save_state,
    update_budget_from_usage=update_budget_from_usage,
    append_jsonl=append_jsonl,
    enqueue_task=enqueue_task,
    cancel_task_by_id=cancel_task_by_id,
    queue_review_task=queue_review_task,
    persist_queue_snapshot=persist_queue_snapshot,
    safe_restart=safe_restart,
    kill_workers=kill_workers,
    spawn_workers=spawn_workers,
    sort_pending=sort_pending,
    consciousness=_consciousness,
)

def _safe_qsize(q: Any) -> int:
    try:
        return int(q.qsize())
    except Exception:
        return -1

def _handle_supervisor_command(text: str, chat_id: int, tg_offset: int = 0):
    lowered = text.strip().lower()

    if lowered.startswith("/panic"):
        send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
        kill_workers()
        st2 = load_state()
        st2["tg_offset"] = tg_offset
        save_state(st2)
        raise SystemExit("PANIC")

    if lowered.startswith("/restart"):
        st2 = load_state()
        st2["session_id"] = uuid.uuid4().hex
        st2["tg_offset"] = tg_offset
        save_state(st2)
        send_with_budget(chat_id, "♻️ Restarting (soft).")
        ok, msg = safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
        if not ok:
            send_with_budget(chat_id, f"⚠️ Restart cancelled: {msg}")
            return True
        kill_workers()
        os.execv(sys.executable, [sys.executable, __file__])

    if lowered.startswith("/status"):
        status = status_text(WORKERS, PENDING, RUNNING, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC)
        send_with_budget(chat_id, status, force_budget=True)
        return "[Supervisor handled /status — status text already sent to chat]\n"

    if lowered.startswith("/review"):
        queue_review_task(reason="owner:/review", force=True)
        return "[Supervisor handled /review — review task queued]\n"

    if lowered.startswith("/evolve"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "on"
        turn_on = action not in ("off", "stop", "0")
        st2 = load_state()
        st2["evolution_mode_enabled"] = bool(turn_on)
        save_state(st2)
        if not turn_on:
            PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
            sort_pending()
            persist_queue_snapshot(reason="evolve_off")
        state_str = "ON" if turn_on else "OFF"
        send_with_budget(chat_id, f"🧬 Evolution: {state_str}")
        return f"[Supervisor handled /evolve — evolution toggled {state_str}]\n"

    if lowered.startswith("/bg"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "status"
        if action in ("on", "start", "1"):
            _consciousness.start()
            send_with_budget(chat_id, "🧠 Background consciousness: running")
            return "[Supervisor handled /bg start]\n"
        elif action in ("off", "stop", "0"):
            _consciousness.stop()
            send_with_budget(chat_id, "🧠 Background consciousness: stopped")
            return "[Supervisor handled /bg stop]\n"
        else:
            status_msg = "🧠 Background consciousness: " + ("running" if _consciousness.is_running() else "stopped")
            send_with_budget(chat_id, status_msg)
            return "[Supervisor handled /bg status]\n"

    return None

def _telegram_loop():
    """Telegram message polling loop."""
    import queue as _queue_mod
    log.info("Starting Telegram loop")
    last_heartbeat = time.time()
    last_snapshot = time.time()
    st = load_state()
    offset = st.get("tg_offset", 0)

    while True:
        now = time.time()
        if DIAG_HEARTBEAT_SEC > 0 and now - last_heartbeat >= DIAG_HEARTBEAT_SEC:
            assign_tasks()
            ensure_workers_healthy()
            last_heartbeat = now

        if now - last_snapshot >= 300:
            persist_queue_snapshot(reason="periodic")
            last_snapshot = now

        # Drain event queue (sends replies, processes task completions)
        event_q = get_event_q()
        while True:
            try:
                evt = event_q.get_nowait()
            except _queue_mod.Empty:
                break
            dispatch_event(evt, _event_ctx)

        try:
            updates = TG.get_updates(offset=offset, timeout=15)
        except Exception as e:
            log.warning(f"Telegram getUpdates failed: {e}")
            time.sleep(5)
            continue

        if not updates:
            continue

        for upd in updates:
            offset = max(offset, upd.get("update_id", 0) + 1)
            msg_obj = upd.get("message") or upd.get("edited_message")
            if not msg_obj:
                continue

            text = str(msg_obj.get("text", "")).strip()
            photo_base64 = None
            photo_mime = None

            # Handle photos
            photos = msg_obj.get("photo")  # Array of PhotoSize objects (different resolutions)
            if photos:
                # Get largest photo (last in array)
                largest_photo = photos[-1]
                file_id = largest_photo.get("file_id")
                log.info(f"Photo detected in message, {len(photos)} sizes, file_id={file_id}")
                if file_id:
                    photo_base64, photo_mime = TG.download_file_base64(file_id)
                    log.info(f"Photo download result: {'success' if photo_base64 else 'FAILED'}, mime={photo_mime}")
                # Photo messages carry their text in "caption", not "text"
                if not text:
                    text = str(msg_obj.get("caption", "")).strip()

            chat_id = int(msg_obj["chat"]["id"])
            from_user_id = int(msg_obj["from"]["id"])

            st = load_state()
            owner_id = st.get("owner_id")
            if owner_id is None:
                st["owner_id"] = from_user_id
                st["owner_chat_id"] = chat_id
                save_state(st)
                send_with_budget(chat_id, "👋 I recognize you as my creator. Hello, K.")
                log.info(f"Owner registered: {from_user_id}")

            if from_user_id != st.get("owner_id"):
                continue

            log.info(f"Message from owner: {text[:80]}")

            supervisor_handled = _handle_supervisor_command(text, chat_id, tg_offset=offset)
            if supervisor_handled is True:
                continue
            elif isinstance(supervisor_handled, str):
                text = supervisor_handled

            st["tg_offset"] = offset
            st["last_owner_message_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            save_state(st)
            rotate_chat_log_if_needed(DRIVE_ROOT)
            log_chat("in", chat_id, from_user_id, text)

            try:
                if photo_base64:
                    handle_chat_direct(chat_id, text or "[photo]", photo_base64=photo_base64, photo_mime=photo_mime)
                else:
                    handle_chat_direct(chat_id, text)
            except Exception as e:
                log.error("Chat handling failed", exc_info=True)
                send_with_budget(chat_id, f"⚠️ Internal error: {e}")

        st = load_state()
        st["tg_offset"] = offset
        save_state(st)

def _discord_loop():
    """Discord message polling loop. Event drain is in _telegram_loop only."""
    if not DISCORD_CLIENT:
        return

    from supervisor.discord_gateway import log_chat as discord_log_chat
    log.info("Starting Discord loop")

    while True:
        messages = DISCORD_CLIENT.get_pending_messages()
        if not messages:
            time.sleep(1)
            continue

        for msg in messages:
            if msg.get("type") != "message":
                continue

            channel_id = int(msg["channel_id"])
            from_user_id = int(msg["user_id"])
            text = str(msg.get("content", "")).strip()

            st = load_state()
            discord_owner = st.get("discord_owner_id")

            # Auto-register Discord owner on first message
            if discord_owner is None:
                st["discord_owner_id"] = from_user_id
                save_state(st)
                DISCORD_CLIENT.send_message(channel_id, "👋 I recognize you as my creator on Discord. Hello, K.")
                log.info(f"Discord owner registered: {from_user_id}")
                discord_owner = from_user_id

            if from_user_id != discord_owner:
                continue

            log.info(f"Discord message from owner: {text[:80]}")

            st = load_state()
            st["last_owner_message_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            save_state(st)
            discord_log_chat("in", channel_id, from_user_id, text)

            try:
                handle_chat_direct(channel_id, text)
            except Exception as e:
                log.error("Discord chat handling failed", exc_info=True)
                DISCORD_CLIENT.send_message(channel_id, f"⚠️ Internal error: {e}")

# ----------------------------
# 8) Start loops
# ----------------------------
log.info(f"🐍 Ouroboros cyberclaw launcher starting")
log.info(f"Drive: {DRIVE_ROOT}")
log.info(f"Repo: {REPO_DIR}")
log.info(f"Telegram: enabled")
log.info(f"Discord: {'enabled' if DISCORD_CLIENT else 'disabled'}")

# Start Telegram in main thread
_telegram_thread = threading.Thread(target=_telegram_loop, daemon=False, name="TelegramLoop")
_telegram_thread.start()

# Start Discord in separate thread if enabled
if DISCORD_CLIENT:
    _discord_thread = threading.Thread(target=_discord_loop, daemon=True, name="DiscordLoop")
    _discord_thread.start()

# Wait for Telegram thread (main loop)
try:
    _telegram_thread.join()
except KeyboardInterrupt:
    log.info("Shutdown signal received")
    kill_workers()
    sys.exit(0)
