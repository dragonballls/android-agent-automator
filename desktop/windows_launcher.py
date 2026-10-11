"""Native Windows launcher for Ghost in the Droid.

Packaged with PyInstaller as GhostLauncher.exe. It starts the local FastAPI
service in the background, serves the bundled Vue dashboard, stores Gemini
keys in the OS credential vault, and manages persistent daily agent tasks.
"""
from __future__ import annotations

import base64
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from wireless_adb import pair_and_connect, reconnect_paired_devices


APP_NAME = "Ghost in the Droid"
KEYRING_SERVICE = "ghost-in-the-droid/api-keys"
KEYRING_BACKENDS = (
    "keyring.backends.windows",
    "keyring.backends.macos",
    "keyring.backends.secretservice",
    "keyring.backends.kwallet",
    "keyring.backends.kdewallet",
    "keyring.backends.libsecret",
)
KEY_NAMES = ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def runtime_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            parent = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        else:
            parent = Path.home() / ".local" / "share"
        return parent / "GhostInTheDroid"
    return Path(__file__).resolve().parents[1]


DATA_DIR = runtime_data_dir()
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(DATA_DIR)


def _keyring_module():
    import keyring

    backend = keyring.get_keyring()
    module_name = backend.__class__.__module__.lower()
    class_name = backend.__class__.__name__.lower()
    priority = float(getattr(backend, "priority", 0))
    if not any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in KEYRING_BACKENDS
    ) or any(word in module_name or word in class_name for word in ("plaintext", "null", "fail", "keyrings.alt")) or priority <= 0:
        raise RuntimeError(
            "No supported secure OS credential vault is active. Configure Windows Credential Manager "
            "or another supported native OS vault. Plaintext file-backed key stores are refused."
        )
    return keyring


def load_api_keys_from_vault(env: dict[str, str] | None = None) -> dict[str, str]:
    """Load keys into only this process environment; never print them."""
    target = os.environ if env is None else env
    try:
        keyring = _keyring_module()
        for name in KEY_NAMES:
            value = keyring.get_password(KEYRING_SERVICE, name)
            if value:
                target[name] = value
    except Exception:
        # Environment/.env configuration continues to work if the vault is not configured.
        pass
    return target


def _self_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def _api_base() -> str:
    port = int(os.environ.get("PORT", "5055"))
    return f"http://127.0.0.1:{port}"


def _api_request(path: str, method: str = "GET", payload: dict | None = None, timeout: float = 3) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _api_base() + path,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _daily_times(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _schedule_config(row: dict) -> dict:
    value = row.get("config_json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except Exception:
            return {}
    return {}


def run_background_service() -> int:
    """Hidden entrypoint for the login-started backend process."""
    load_api_keys_from_vault()
    log_path = LOG_DIR / "server.log"
    try:
        import logging

        logging.basicConfig(
            filename=str(log_path),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            encoding="utf-8",
        )
        import uvicorn
        from gitd.app import app
    except Exception as exc:
        try:
            log_path.write_text(
                f"Could not initialize Ghost server: {type(exc).__name__}\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        return 1

    try:
        port = int(os.environ.get("PORT", "5055"))
        uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception("Ghost server exited: %s", type(exc).__name__)
        return 1
    return 0


def run_scheduled_task(encoded: str) -> int:
    # Windowed PyInstaller executables may set sys.stdout/sys.stderr to None.
    # Reattach them to the scheduler log path before the agent emits any output.
    log_path = os.environ.get("GHOST_SCHEDULE_LOG_FILE")
    log_handle = None
    if log_path and (sys.stdout is None or sys.stderr is None):
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = log_handle
            if sys.stderr is None:
                sys.stderr = log_handle
        except OSError:
            log_handle = None
    # Imported only after the persistent data directory and OS key store are set up.
    load_api_keys_from_vault()
    from gitd.services.scheduled_agent_task import run_payload

    return run_payload(encoded)


class Launcher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} — Windows Launcher")
        self.root.geometry("980x760")
        self.root.minsize(820, 660)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.server_process: subprocess.Popen | None = None
        self.startup_enabled = tk.BooleanVar(value=self._is_startup_enabled())
        self.status_var = tk.StringVar(value="Service not checked")
        self.available_devices: list[str] = []
        self._device_selection_initialized = False
        self._auto_reconnect_pending = False
        self._last_auto_reconnect_attempt = 0.0
        self.device_summary_var = tk.StringVar(value="No phones selected")
        self.connection_message_var = tk.StringVar(value="Looking for authorized Android phones…")
        self.ai_advanced_var = tk.BooleanVar(value=False)
        self.task_name_var = tk.StringVar(value="")
        self.daily_time_var = tk.StringVar(value="09:00")
        self.provider_var = tk.StringVar(value="gemini")
        self.model_var = tk.StringVar(value="gemini-3.8-flash")
        self._provider_data: dict[str, list[str]] = {
            "gemini": ["gemini-3.8-flash", "gemini-3.1-pro-preview"],
            "openrouter": ["google/gemini-2.5-pro"],
            "anthropic": ["claude-sonnet-4-20250514"],
            "openai": ["gpt-4.1-mini"],
            "ollama": ["llama3.2:3b"],
            "vllm": [],
            "on-device": [],
            "claude-code": ["sonnet"],
        }
        self._pending_callback = None
        self._starting = False
        self._build_ui()
        self.root.after(200, self.refresh_service_state)

    def _build_ui(self):
        """Build a guided three-step workflow; keep advanced service controls out of the way."""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.configure(background="#f4f7fb")
        style.configure("TFrame", background="#f4f7fb")
        style.configure("TLabel", background="#f4f7fb", foreground="#243247", font=("Segoe UI", 10))
        style.configure("AppTitle.TLabel", font=("Segoe UI", 19, "bold"), foreground="#172b4d")
        style.configure("PageTitle.TLabel", font=("Segoe UI", 14, "bold"), foreground="#172b4d")
        style.configure("Hint.TLabel", foreground="#566579", font=("Segoe UI", 9))
        style.configure("Status.TLabel", foreground="#1b5e45", font=("Segoe UI", 9, "bold"))
        style.configure("Primary.TButton", foreground="#ffffff", background="#235db5",
                        padding=(12, 8), font=("Segoe UI", 9, "bold"))
        style.map("Primary.TButton",
                  background=[("active", "#174b95"), ("disabled", "#9aa9bd")],
                  foreground=[("disabled", "#e7ebf0")])
        style.configure("TNotebook", background="#f4f7fb", borderwidth=0)
        style.configure("TNotebook.Tab", padding=(15, 9), font=("Segoe UI", 9, "bold"))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        outer = ttk.Frame(self.root, padding=(22, 17))
        outer.pack(fill="both", expand=True)

        heading = ttk.Frame(outer)
        heading.pack(fill="x", pady=(0, 10))
        ttk.Label(heading, text="GHOST IN THE DROID", style="AppTitle.TLabel").pack(anchor="w")
        ttk.Label(
            heading,
            text="Connect your phone. Describe a task. Choose when it runs.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        status_row = ttk.Frame(outer)
        status_row.pack(fill="x", pady=(0, 12))
        ttk.Label(status_row, textvariable=self.status_var, style="Status.TLabel").pack(side="left", anchor="w")
        ttk.Button(status_row, text="Open Control Center", command=self.open_dashboard).pack(side="right")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.phone_tab = ttk.Frame(self.notebook, padding=18)
        self.task_tab = ttk.Frame(self.notebook, padding=18)
        self.tasks_tab = ttk.Frame(self.notebook, padding=18)
        self.settings_tab = ttk.Frame(self.notebook, padding=18)
        self.notebook.add(self.phone_tab, text="1  Connect phone")
        self.notebook.add(self.task_tab, text="2  Create task")
        self.notebook.add(self.tasks_tab, text="3  Saved tasks")
        self.notebook.add(self.settings_tab, text="Settings")

        # STEP 1 — Phones. Existing authorized USB devices are detected automatically;
        # saved ADB wireless pairings are retried in the background.
        ttk.Label(self.phone_tab, text="Connect your phone", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.phone_tab,
            text="Plug in by USB with USB debugging enabled, or pair once over Wi-Fi. "
                 "After Android authorizes this PC, the launcher will look for the phone automatically.",
            wraplength=790, justify="left",
        ).pack(anchor="w", pady=(6, 12))

        device_card = ttk.LabelFrame(self.phone_tab, text="Phones found", padding=12)
        device_card.pack(fill="both", expand=True)
        device_card.columnconfigure(0, weight=1)
        device_card.rowconfigure(0, weight=1)
        self.device_listbox = tk.Listbox(
            device_card, selectmode=tk.EXTENDED, exportselection=False, height=9,
            font=("Segoe UI", 10), activestyle="dotbox", relief="solid", borderwidth=1,
            selectbackground="#d9e8ff", selectforeground="#172b4d",
        )
        device_scrollbar = ttk.Scrollbar(device_card, orient="vertical", command=self.device_listbox.yview)
        self.device_listbox.configure(yscrollcommand=device_scrollbar.set)
        self.device_listbox.grid(row=0, column=0, sticky="nsew")
        device_scrollbar.grid(row=0, column=1, sticky="ns")
        self.device_listbox.bind("<<ListboxSelect>>", self._update_device_summary)

        select_row = ttk.Frame(device_card)
        select_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(9, 2))
        ttk.Label(select_row, textvariable=self.device_summary_var, style="Status.TLabel").pack(side="left")
        ttk.Button(select_row, text="Select all", command=self.select_all_devices).pack(side="right", padx=(6, 0))
        ttk.Button(select_row, text="Clear", command=self.clear_device_selection).pack(side="right")

        ttk.Label(
            self.phone_tab, textvariable=self.connection_message_var, wraplength=790,
            justify="left", style="Hint.TLabel",
        ).pack(anchor="w", pady=(10, 8))
        connection_actions = ttk.Frame(self.phone_tab)
        connection_actions.pack(fill="x")
        ttk.Button(
            connection_actions, text="Start service and find phones",
            style="Primary.TButton", command=lambda: self.ensure_service(self.refresh_devices),
        ).pack(side="left")
        ttk.Button(
            connection_actions, text="Pair over Wi-Fi", command=self.open_wireless_pairing,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            connection_actions, text="Open Phone Link", command=self._open_phone_link,
        ).pack(side="left", padx=(8, 0))
        ttk.Label(
            self.phone_tab,
            text="Note: Phone Link is for viewing/manual use; phone control uses Android's authorized ADB connection.",
            style="Hint.TLabel", wraplength=790, justify="left",
        ).pack(anchor="w", pady=(10, 0))

        # STEP 2 — One plain-language task editor. Model IDs are hidden unless requested.
        ttk.Label(self.task_tab, text="Tell the agent what to do", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.task_tab,
            text="Use clear instructions for an app and task you are authorized to automate. "
                 "Select phones in the Connect phone tab first.",
            style="Hint.TLabel", wraplength=790, justify="left",
        ).pack(anchor="w", pady=(6, 12))

        task_form = ttk.Frame(self.task_tab)
        task_form.pack(fill="both", expand=True)
        task_form.columnconfigure(1, weight=1)
        ttk.Label(task_form, text="Task name").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=7)
        ttk.Entry(task_form, textvariable=self.task_name_var).grid(row=0, column=1, sticky="ew", pady=7)
        ttk.Label(task_form, text="Run every day at").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=7)
        time_row = ttk.Frame(task_form)
        time_row.grid(row=1, column=1, sticky="w", pady=7)
        ttk.Entry(time_row, textvariable=self.daily_time_var, width=10).pack(side="left")
        ttk.Label(time_row, text="  Local time, 24-hour format (for example 09:00)", style="Hint.TLabel").pack(side="left")
        ttk.Label(task_form, text="Instructions").grid(row=2, column=0, sticky="nw", padx=(0, 12), pady=7)
        self.prompt_text = tk.Text(
            task_form, height=10, wrap="word", font=("Segoe UI", 10),
            relief="solid", borderwidth=1, padx=10, pady=9,
            background="#ffffff", foreground="#243247", insertbackground="#243247",
        )
        self.prompt_text.grid(row=2, column=1, sticky="nsew", pady=7)
        task_form.rowconfigure(2, weight=1)
        ttk.Label(
            task_form, text="Saved tasks repeat daily until paused or deleted. Run Now is for a one-time test.",
            style="Hint.TLabel", wraplength=790, justify="left",
        ).grid(row=3, column=1, sticky="w", pady=(4, 8))

        advanced_toggle = ttk.Checkbutton(
            task_form, text="Advanced AI model options", variable=self.ai_advanced_var,
            command=self._toggle_ai_advanced,
        )
        advanced_toggle.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.ai_advanced_frame = ttk.LabelFrame(task_form, text="AI model", padding=10)
        self.ai_advanced_frame.columnconfigure(1, weight=1)
        self.ai_advanced_frame.columnconfigure(3, weight=1)
        ttk.Label(self.ai_advanced_frame, text="Provider").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.provider_combo = ttk.Combobox(
            self.ai_advanced_frame, textvariable=self.provider_var,
            values=list(self._provider_data), state="readonly", width=19,
        )
        self.provider_combo.grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=4)
        self.provider_combo.bind("<<ComboboxSelected>>", self.on_provider_changed)
        ttk.Label(self.ai_advanced_frame, text="Model ID").grid(row=0, column=2, sticky="w", padx=(0, 8), pady=4)
        self.model_combo = ttk.Combobox(
            self.ai_advanced_frame, textvariable=self.model_var, values=self._provider_data["gemini"],
        )
        self.model_combo.grid(row=0, column=3, sticky="ew", pady=4)

        action_row = ttk.Frame(self.task_tab)
        action_row.pack(fill="x", pady=(12, 0))
        ttk.Label(action_row, textvariable=self.device_summary_var, style="Hint.TLabel").pack(side="left")
        ttk.Button(
            action_row, text="Choose phones", command=lambda: self.notebook.select(self.phone_tab),
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            action_row, text="Save daily task", style="Primary.TButton", command=self.save_task,
        ).pack(side="right")

        # STEP 3 — Task management in its own uncluttered page.
        ttk.Label(self.tasks_tab, text="Saved tasks and schedules", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.tasks_tab,
            text="Run a selected task now, pause its recurring schedule, or delete it.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(6, 12))
        saved_frame = ttk.Frame(self.tasks_tab)
        saved_frame.pack(fill="both", expand=True)
        saved_frame.columnconfigure(0, weight=1)
        saved_frame.rowconfigure(0, weight=1)
        columns = ("name", "device", "time", "enabled", "next")
        self.tree = ttk.Treeview(saved_frame, columns=columns, show="headings", height=12, selectmode="extended")
        for key, title, width in (
            ("name", "Task", 235), ("device", "Phone", 180), ("time", "Daily at", 95),
            ("enabled", "Schedule", 85), ("next", "Next run", 155),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        task_scrollbar = ttk.Scrollbar(saved_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=task_scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        task_scrollbar.grid(row=0, column=1, sticky="ns")
        task_actions = ttk.Frame(self.tasks_tab)
        task_actions.pack(fill="x", pady=(12, 0))
        ttk.Button(task_actions, text="Refresh", command=self.refresh_tasks).pack(side="left")
        ttk.Button(task_actions, text="Run Now", style="Primary.TButton", command=self.run_selected).pack(side="left", padx=(8, 0))
        ttk.Button(task_actions, text="Pause / Enable", command=self.toggle_selected).pack(side="left", padx=(8, 0))
        ttk.Button(task_actions, text="Delete", command=self.delete_selected).pack(side="left", padx=(8, 0))

        # Settings are available but no longer compete with the first-time workflow.
        ttk.Label(self.settings_tab, text="Service and account settings", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            self.settings_tab, text="Most users only need to set up Gemini once; the other controls are optional.",
            style="Hint.TLabel", wraplength=790, justify="left",
        ).pack(anchor="w", pady=(6, 14))
        service_box = ttk.LabelFrame(self.settings_tab, text="Local service", padding=12)
        service_box.pack(fill="x", pady=(0, 12))
        ttk.Button(service_box, text="Start / Check Service", style="Primary.TButton", command=self.ensure_service).pack(side="left")
        ttk.Button(service_box, text="Restart Service", command=self.restart_service).pack(side="left", padx=(8, 0))
        ttk.Label(service_box, text=f"Local dashboard: {_api_base()}", style="Hint.TLabel").pack(side="left", padx=(14, 0))
        account_box = ttk.LabelFrame(self.settings_tab, text="AI access", padding=12)
        account_box.pack(fill="x", pady=(0, 12))
        ttk.Label(
            account_box, text="Gemini key is stored in the operating system's credential vault.",
            style="Hint.TLabel", wraplength=720, justify="left",
        ).pack(anchor="w", pady=(0, 8))
        ttk.Button(account_box, text="Configure Gemini API key", command=self.configure_gemini_key).pack(anchor="w")
        ttk.Checkbutton(
            self.settings_tab, text="Start the service when I sign in to Windows",
            variable=self.startup_enabled, command=self.set_startup,
        ).pack(anchor="w", pady=(2, 14))
        ttk.Label(
            self.settings_tab, text=f"Local data folder: {DATA_DIR}", style="Hint.TLabel",
            wraplength=790, justify="left",
        ).pack(anchor="w")

        if not shutil.which("adb"):
            self.connection_message_var.set(
                "ADB was not found on PATH. Install Android SDK Platform-Tools and add adb.exe to PATH "
                "to enable automatic phone detection and wireless reconnection."
            )

    def _toggle_ai_advanced(self):
        if self.ai_advanced_var.get():
            self.ai_advanced_frame.pack(fill="x", pady=(8, 0))
        else:
            self.ai_advanced_frame.pack_forget()

    def _update_device_summary(self, _event=None):
        try:
            count = len(self.device_listbox.curselection())
        except tk.TclError:
            count = 0
        noun = "phone" if count == 1 else "phones"
        self.device_summary_var.set(
            f"{count} {noun} selected" if count else "No phones selected — choose one above"
        )

    def _open_phone_link(self):
        try:
            if os.name == "nt":
                os.startfile("ms-phone:")
            else:
                webbrowser.open("ms-phone:")
        except Exception:
            messagebox.showinfo(APP_NAME, "Open Phone Link from the Windows Start menu.", parent=self.root)

    def _maybe_auto_reconnect_wireless(self):
        """Retry mDNS-advertised, already-paired ADB wireless devices without blocking the UI."""
        if not shutil.which("adb") or self._auto_reconnect_pending:
            return
        now = time.monotonic()
        if now - self._last_auto_reconnect_attempt < 25:
            return
        self._last_auto_reconnect_attempt = now
        self._auto_reconnect_pending = True

        def worker():
            try:
                connected = reconnect_paired_devices()
            except Exception:
                connected = []
            try:
                self.root.after(0, lambda devices=connected: self._finish_auto_reconnect(devices))
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name="adb-auto-reconnect").start()

    def _finish_auto_reconnect(self, connected: list[str]):
        self._auto_reconnect_pending = False
        if connected:
            count = len(connected)
            noun = "phone" if count == 1 else "phones"
            self.connection_message_var.set(
                f"Automatically connected {count} previously paired {noun} over Wi-Fi."
            )
            self.refresh_devices()

    def _service_ready(self) -> bool:
        try:
            health = _api_request("/api/health", timeout=0.8)
            return isinstance(health, dict) and health.get("status") == "ok"
        except Exception:
            return False

    def refresh_service_state(self):
        if self._service_ready():
            self.status_var.set(f"Service running at {_api_base()}")
            self._load_provider_models()
            self.refresh_devices()
            self.refresh_tasks()
        elif not self._starting:
            self.status_var.set("Service is stopped. Start it to manage tasks or open the control center.")
        self.root.after(4000, self.refresh_service_state)

    def ensure_service(self, on_ready=None):
        if self._service_ready():
            self.status_var.set(f"Service running at {_api_base()}")
            self._load_provider_models()
            self.refresh_devices()
            self.refresh_tasks()
            if callable(on_ready):
                on_ready()
            return
        if self._starting:
            if callable(on_ready):
                self._pending_callback = on_ready
            return
        self._starting = True
        self._pending_callback = on_ready
        self.status_var.set("Starting local service…")
        env = os.environ.copy()
        load_api_keys_from_vault(env)
        log_path = LOG_DIR / "server.log"
        try:
            log_file = open(log_path, "a", encoding="utf-8")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            kwargs = {"stdout": log_file, "stderr": subprocess.STDOUT, "env": env}
            if os.name == "nt":
                kwargs["creationflags"] = creationflags
            else:
                kwargs["start_new_session"] = True
            self.server_process = subprocess.Popen(_self_command("--background"), **kwargs)
            log_file.close()
        except Exception as exc:
            self._starting = False
            self.status_var.set("Could not start service")
            messagebox.showerror(APP_NAME, f"Could not start the service: {type(exc).__name__}\nCheck {log_path}")
            return
        self.root.after(500, self._poll_server_start, 0)

    def _poll_server_start(self, attempt: int):
        if self._service_ready():
            self._starting = False
            self.status_var.set(f"Service running at {_api_base()}")
            self._load_provider_models()
            self.refresh_devices()
            self.refresh_tasks()
            callback, self._pending_callback = self._pending_callback, None
            if callable(callback):
                callback()
            return
        if attempt >= 60:
            self._starting = False
            self.status_var.set("Service did not start. Check the launcher log.")
            messagebox.showerror(
                APP_NAME,
                f"The local service did not start. Open this log for details:\n{LOG_DIR / 'server.log'}",
            )
            return
        if self.server_process and self.server_process.poll() is not None:
            self._starting = False
            self.status_var.set("Service exited during startup")
            messagebox.showerror(APP_NAME, f"Service exited. Check {LOG_DIR / 'server.log'}")
            return
        self.root.after(500, self._poll_server_start, attempt + 1)

    def restart_service(self):
        if not messagebox.askyesno(
            APP_NAME,
            "Restart the local service now? Any task currently running may be interrupted. "
            "Saved tasks and daily schedules will remain stored.",
        ):
            return
        stopped = False
        # First stop the child this launcher itself started, if it still exists.
        if self.server_process and self.server_process.poll() is None:
            try:
                self.server_process.terminate()
                self.server_process.wait(timeout=5)
                stopped = True
            except Exception:
                try:
                    self.server_process.kill()
                    stopped = True
                except Exception:
                    pass
        # Also support a server that was started by Windows login startup or a
        # previous launcher window; only stop processes with this explicit mode.
        try:
            import psutil

            current_pid = os.getpid()
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["pid"] == current_pid:
                        continue
                    name = (proc.info.get("name") or "").lower()
                    cmdline = proc.info.get("cmdline") or []
                    joined = " ".join(str(part) for part in cmdline).lower()
                    is_packaged_server = name == "ghostlauncher.exe" and "--background" in joined
                    is_source_server = "windows_launcher.py" in joined and "--background" in joined
                    if is_packaged_server or is_source_server:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except psutil.TimeoutExpired:
                            proc.kill()
                        stopped = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        self.server_process = None
        self.status_var.set("Restarting service…")
        self.root.after(800 if stopped else 100, self.ensure_service)

    def open_dashboard(self):
        self.ensure_service(lambda: webbrowser.open(_api_base() + "/"))

    def _load_provider_models(self):
        try:
            result = _api_request("/api/agent-chat/providers", timeout=1.5)
            if isinstance(result, list):
                self._provider_data = {
                    str(item.get("id")): [str(v) for v in item.get("models", [])]
                    for item in result if item.get("id")
                }
                self.provider_combo.configure(values=list(self._provider_data))
                self.on_provider_changed()
        except Exception:
            pass

    def on_provider_changed(self, _event=None):
        provider = self.provider_var.get()
        models = self._provider_data.get(provider, [])
        self.model_combo.configure(values=models)
        if models and self.model_var.get() not in models:
            self.model_var.set(models[0])

    def refresh_devices(self):
        if not self._service_ready():
            return
        self._maybe_auto_reconnect_wireless()
        try:
            result = _api_request("/api/phone/devices", timeout=2)
            if isinstance(result, dict):
                devices = result.get("devices", result.get("items", []))
            else:
                devices = result if isinstance(result, list) else []
            serials = []
            for item in devices:
                if isinstance(item, str):
                    serials.append(item)
                elif isinstance(item, dict):
                    serial = item.get("serial") or item.get("device") or item.get("ref")
                    if serial and item.get("platform", "android") in ("android", "ios"):
                        serials.append(str(serial))
            previous_selection = {
                self.device_listbox.get(index) for index in self.device_listbox.curselection()
            }
            first_device_list = not self._device_selection_initialized
            self.available_devices = serials
            self.device_listbox.delete(0, tk.END)
            for index, serial in enumerate(serials):
                self.device_listbox.insert(tk.END, serial)
                if serial in previous_selection:
                    self.device_listbox.selection_set(index)
            if first_device_list and serials and not self.device_listbox.curselection():
                self.device_listbox.selection_set(0)
            if serials:
                self._device_selection_initialized = True
            self._update_device_summary()
            if not serials:
                self.connection_message_var.set(
                    "No phones found yet. Connect by USB and approve the debugging prompt, or use Pair over Wi-Fi once."
                )
        except Exception:
            pass

    def open_wireless_pairing(self):
        """Show the guided Android Wireless debugging pairing dialog."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Connect Android over Wi-Fi")
        dialog.geometry("650x610")
        dialog.minsize(620, 570)
        dialog.transient(self.root)

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="WIRELESS ANDROID CONNECTION", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        instructions = (
            "Phone Link can stay connected for viewing and manual interaction. It does not expose the "
            "general phone-control API this automation service needs, so this adds a separate wireless ADB "
            "connection without requiring a USB cable.\n\n"
            "1. On Android 11 or later, open Settings → About phone and tap Build number seven times if "
            "Developer options are not enabled yet.\n"
            "2. Open Settings → System → Developer options → Wireless debugging and turn it on. Keep the "
            "phone and PC on the same trusted Wi-Fi network.\n"
            "3. Tap Pair device with pairing code. Copy the phone IP address, pairing port, and temporary code "
            "from that popup. The code is used once and is not saved.\n"
            "4. On the main Wireless debugging screen, note its separate connection port. Enter both ports below."
        )
        ttk.Label(frame, text=instructions, justify="left", wraplength=590).pack(fill="x", pady=(8, 12))

        form = ttk.Frame(frame)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        ip_var = tk.StringVar()
        pairing_port_var = tk.StringVar()
        code_var = tk.StringVar()
        connection_port_var = tk.StringVar()
        fields = (
            ("Phone IP address", ip_var, None),
            ("Pairing port", pairing_port_var, None),
            ("Temporary pairing code", code_var, "*"),
            ("Wireless debugging port", connection_port_var, None),
        )
        for row, (label, variable, mask) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
            entry = ttk.Entry(form, textvariable=variable, show=mask or "")
            entry.grid(row=row, column=1, sticky="ew", pady=5)

        ttk.Label(
            frame,
            text="Only the temporary ADB pairing code goes here—not your phone lock-screen PIN. "
            "Use Wireless debugging only on networks you trust.",
            wraplength=590,
            justify="left",
            foreground="#555555",
        ).pack(fill="x", pady=(10, 8))

        status_var = tk.StringVar(value="Waiting for phone details.")
        ttk.Label(frame, textvariable=status_var, wraplength=590, justify="left").pack(fill="x", pady=(0, 10))

        actions = ttk.Frame(frame)
        actions.pack(fill="x", side="bottom")

        def open_phone_link():
            try:
                if os.name == "nt":
                    os.startfile("ms-phone:")
                else:
                    webbrowser.open("ms-phone:")
            except Exception:
                messagebox.showinfo(
                    APP_NAME,
                    "Open Phone Link from the Windows Start menu. It stays separate from the wireless ADB connection.",
                    parent=dialog,
                )

        ttk.Button(actions, text="Open Phone Link", command=open_phone_link).pack(side="left")

        def finish_pairing(message: str, error: str | None):
            if not dialog.winfo_exists():
                return
            pair_button.configure(state="normal")
            if error:
                status_var.set(error)
                messagebox.showerror(APP_NAME, error, parent=dialog)
                return
            status_var.set(message)
            self.ensure_service(self.refresh_devices)
            messagebox.showinfo(APP_NAME, message, parent=dialog)

        def start_pairing():
            host = ip_var.get().strip()
            pairing_port = pairing_port_var.get().strip()
            pairing_code = code_var.get().strip()
            connection_port = connection_port_var.get().strip()
            if not all((host, pairing_port, pairing_code, connection_port)):
                messagebox.showwarning(
                    APP_NAME,
                    "Enter the IP address, pairing port, temporary pairing code, and Wireless debugging port.",
                    parent=dialog,
                )
                return
            code_var.set("")
            pair_button.configure(state="disabled")
            status_var.set("Pairing and connecting… The temporary code is being used only for this attempt.")
            def worker():
                try:
                    message = pair_and_connect(host, pairing_port, pairing_code, connection_port)
                    error = None
                except Exception as exc:
                    message = ""
                    error = str(exc)
                try:
                    self.root.after(0, lambda message=message, error=error: finish_pairing(message, error))
                except RuntimeError:
                    pass

            threading.Thread(target=worker, daemon=True, name="wireless-adb-pairing").start()

        pair_button = ttk.Button(actions, text="Pair and Connect", command=start_pairing)
        pair_button.pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="Close", command=dialog.destroy).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.grab_set()

    def _load_schedules(self) -> list[dict]:
        result = _api_request("/api/schedules", timeout=4)
        return result if isinstance(result, list) else []

    def refresh_tasks(self):
        if not self._service_ready():
            return
        try:
            schedules = self._load_schedules()
        except Exception:
            return
        previous_selection = set(self.tree.selection())
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in schedules:
            if row.get("job_type") != "agent_task":
                continue
            times = _daily_times(row.get("daily_times"))
            status = "Enabled" if row.get("is_enabled") else "Paused"
            item_id = str(row["id"])
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    row.get("name") or "Saved task",
                    row.get("phone_serial") or "—",
                    ", ".join(times) or "—",
                    status,
                    row.get("next_run") or "—",
                ),
            )
            if item_id in previous_selection:
                self.tree.selection_add(item_id)

    def save_task(self):
        prompt = self.prompt_text.get("1.0", "end").strip()
        name = self.task_name_var.get().strip() or (prompt[:45] + ("…" if len(prompt) > 45 else ""))
        devices = [self.device_listbox.get(index) for index in self.device_listbox.curselection()]
        provider = self.provider_var.get().strip()
        model = self.model_var.get().strip()
        daily_time = self.daily_time_var.get().strip()
        if not devices:
            messagebox.showwarning(APP_NAME, "Connect and select at least one target phone.")
            return
        if not prompt or len(prompt) > 5000:
            messagebox.showwarning(APP_NAME, "Enter a task description between 1 and 5000 characters.")
            return
        try:
            time.strptime(daily_time, "%H:%M")
        except ValueError:
            messagebox.showwarning(APP_NAME, "Enter the daily time in 24-hour HH:MM format, such as 09:00.")
            return
        if not provider:
            messagebox.showwarning(APP_NAME, "Choose an AI provider.")
            return
        self.ensure_service(lambda: self._post_new_task(name, prompt, devices, provider, model, daily_time))

    def _post_new_task(
        self, name: str, prompt: str, devices: list[str], provider: str, model: str, daily_time: str
    ):
        from gitd.services.multi_device_tasks import build_device_schedule_payloads

        try:
            payloads = build_device_schedule_payloads(name, prompt, devices, provider, model, daily_time)
        except ValueError as exc:
            messagebox.showwarning(APP_NAME, str(exc))
            return

        created_ids: list[int] = []
        try:
            for payload in payloads:
                result = _api_request("/api/schedules", method="POST", payload=payload, timeout=8)
                if not isinstance(result, dict) or not result.get("ok") or result.get("id") is None:
                    raise RuntimeError("The scheduler did not confirm every saved task.")
                created_ids.append(int(result["id"]))
        except Exception as exc:
            rollback_failed: list[int] = []
            for schedule_id in reversed(created_ids):
                try:
                    _api_request(f"/api/schedules/{schedule_id}", method="DELETE", timeout=4)
                except Exception:
                    rollback_failed.append(schedule_id)
            if isinstance(exc, urllib.error.HTTPError):
                detail = exc.read().decode("utf-8", errors="replace")[:800]
                error = f"Could not save the task for every phone (HTTP {exc.code}):\\n{detail}"
            else:
                error = f"Could not save the task for every phone: {type(exc).__name__}"
            if rollback_failed:
                error += "\\nSome partial schedules could not be removed: " + ", ".join(map(str, rollback_failed))
            messagebox.showerror(APP_NAME, error)
            self.refresh_tasks()
            return

        count = len(payloads)
        self.status_var.set(f"Saved daily task on {count} phone{'s' if count != 1 else ''}: {name}")
        self.refresh_tasks()
        if provider == "gemini" and not self._gemini_key_present():
            messagebox.showinfo(
                APP_NAME,
                "Task saved. Add a Gemini API key with Configure Gemini Key before its scheduled run.",
            )

    def select_all_devices(self):
        if self.device_listbox.size():
            self.device_listbox.selection_set(0, tk.END)

    def clear_device_selection(self):
        self.device_listbox.selection_clear(0, tk.END)

    def _selected_schedule_ids(self) -> list[int] | None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo(APP_NAME, "Select one or more saved tasks first.")
            return None
        try:
            return [int(item) for item in selected]
        except ValueError:
            messagebox.showerror(APP_NAME, "The task selection contains an invalid schedule ID.")
            return None

    def run_selected(self):
        schedule_ids = self._selected_schedule_ids()
        if schedule_ids is None:
            return
        self.ensure_service(lambda: self._schedule_action(schedule_ids, "run-now"))

    def toggle_selected(self):
        schedule_ids = self._selected_schedule_ids()
        if schedule_ids is None:
            return
        self.ensure_service(lambda: self._schedule_action(schedule_ids, "toggle"))

    def delete_selected(self):
        schedule_ids = self._selected_schedule_ids()
        if schedule_ids is None:
            return
        count = len(schedule_ids)
        noun = "task" if count == 1 else "tasks"
        if not messagebox.askyesno(APP_NAME, f"Delete {count} saved {noun} and their daily schedules?"):
            return
        self.ensure_service(lambda: self._schedule_action(schedule_ids, "delete"))

    def _schedule_action(self, schedule_ids: list[int], action: str):
        completed: list[int] = []
        failures: list[str] = []
        for sid in schedule_ids:
            try:
                if action == "delete":
                    _api_request(f"/api/schedules/{sid}", method="DELETE", timeout=4)
                else:
                    _api_request(f"/api/schedules/{sid}/{action}", method="POST", payload={}, timeout=6)
                completed.append(sid)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
                failures.append(f"#{sid}: HTTP {exc.code} {detail}".strip())
            except Exception as exc:
                failures.append(f"#{sid}: {type(exc).__name__}")
        self.refresh_tasks()
        action_label = "Queued" if action == "run-now" else action.title()
        if failures:
            messagebox.showerror(
                APP_NAME,
                f"{action_label} for {len(completed)} of {len(schedule_ids)} task(s).\n" + "\n".join(failures),
            )
            self.status_var.set(f"{action_label}: {len(completed)}/{len(schedule_ids)} tasks")
        elif action == "run-now":
            self.status_var.set(f"Queued {len(completed)} task(s); different phones can run in parallel")
        else:
            self.status_var.set(f"{action_label} completed for {len(completed)} task(s)")

    def _gemini_key_present(self) -> bool:
        try:
            value = _keyring_module().get_password(KEYRING_SERVICE, "GEMINI_API_KEY")
            return bool(value or os.environ.get("GEMINI_API_KEY"))
        except Exception:
            return bool(os.environ.get("GEMINI_API_KEY"))

    def configure_gemini_key(self):
        try:
            keyring = _keyring_module()
            current = keyring.get_password(KEYRING_SERVICE, "GEMINI_API_KEY") or ""
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Secure credential storage is unavailable:\n{exc}")
            return
        value = simpledialog.askstring(
            APP_NAME,
            "Google Gemini API key (input is hidden). Leave blank to cancel.",
            show="*",
            parent=self.root,
        )
        if not value:
            return
        confirmation = simpledialog.askstring(
            APP_NAME, "Confirm Gemini API key (input is hidden).", show="*", parent=self.root
        )
        if value != confirmation:
            messagebox.showerror(APP_NAME, "The entries did not match. Nothing was saved.")
            return
        try:
            keyring.set_password(KEYRING_SERVICE, "GEMINI_API_KEY", value.strip())
            self.status_var.set("Gemini API key saved to the OS credential vault. Restart the service to load it.")
            messagebox.showinfo(
                APP_NAME,
                "Gemini API key saved securely. Restart the local service (or sign out and back in if it is "
                "running in the background) before running Gemini tasks.",
            )
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save Gemini API key: {type(exc).__name__}")

    def _is_startup_enabled(self) -> bool:
        if os.name != "nt":
            return False
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                value, _ = winreg.QueryValueEx(key, "GhostInTheDroid")
            return "--background" in value
        except Exception:
            return False

    def set_startup(self):
        if os.name != "nt":
            messagebox.showinfo(APP_NAME, "Automatic sign-in startup is available in the Windows EXE build.")
            self.startup_enabled.set(False)
            return
        try:
            import winreg

            registry_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, registry_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                if self.startup_enabled.get():
                    if getattr(sys, "frozen", False):
                        command = f'"{sys.executable}" --background'
                    else:
                        command = f'"{sys.executable}" "{Path(__file__).resolve()}" --background'
                    winreg.SetValueEx(key, "GhostInTheDroid", 0, winreg.REG_SZ, command)
                else:
                    try:
                        winreg.DeleteValue(key, "GhostInTheDroid")
                    except FileNotFoundError:
                        pass
            state = "enabled" if self.startup_enabled.get() else "disabled"
            self.status_var.set(f"Start with Windows {state}")
        except Exception as exc:
            self.startup_enabled.set(not self.startup_enabled.get())
            messagebox.showerror(APP_NAME, f"Could not update Windows startup: {type(exc).__name__}")

    def on_close(self):
        # Leave the service alive so today's saved schedules can continue to run.
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--background":
        return run_background_service()
    if len(args) == 2 and args[0] == "--run-scheduled-agent-task":
        return run_scheduled_task(args[1])
    if args and args[0] == "--self-test":
        load_api_keys_from_vault()
        try:
            from gitd.app import app

            bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
            if not (bundle_root / "frontend" / "dist" / "index.html").is_file():
                (LOG_DIR / "self-test.txt").write_text(
                    "self-test failed: bundled frontend index.html is missing\\n", encoding="utf-8"
                )
                return 3
            if not any(getattr(route, "path", "") == "/api/health" for route in app.routes):
                (LOG_DIR / "self-test.txt").write_text(
                    "self-test failed: health route was not registered\\n", encoding="utf-8"
                )
                return 4
            (LOG_DIR / "self-test.txt").write_text(
                "launcher import and bundled dashboard OK\\n", encoding="utf-8"
            )
            return 0
        except Exception as exc:
            missing_module = getattr(exc, "name", None)
            detail = f": {missing_module}" if missing_module else ""
            message = f"launcher import failed: {type(exc).__name__}{detail}\\n"
            (LOG_DIR / "self-test.txt").write_text(message, encoding="utf-8")
            return 2
    root = tk.Tk()
    Launcher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
