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
    # Imported only after the persistent data directory and OS key store are set up.
    load_api_keys_from_vault()
    from gitd.services.scheduled_agent_task import run_payload

    return run_payload(encoded)


class Launcher:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} — Windows Launcher")
        self.root.geometry("900x760")
        self.root.minsize(780, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.server_process: subprocess.Popen | None = None
        self.startup_enabled = tk.BooleanVar(value=self._is_startup_enabled())
        self.status_var = tk.StringVar(value="Service not checked")
        self.device_var = tk.StringVar(value="")
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
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        heading = ttk.Frame(outer)
        heading.pack(fill="x")
        ttk.Label(heading, text="GHOST IN THE DROID", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            heading,
            text="Windows launcher • Android/iOS agent control • Saved daily automations",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(2, 8))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(2, 8))
        ttk.Button(controls, text="Start / Check Service", command=self.ensure_service).pack(side="left", padx=(0, 6))
        ttk.Button(controls, text="Open Control Center", command=self.open_dashboard).pack(side="left", padx=6)
        ttk.Button(controls, text="Refresh Tasks", command=self.refresh_tasks).pack(side="left", padx=6)
        ttk.Button(controls, text="Configure Gemini Key", command=self.configure_gemini_key).pack(side="left", padx=6)
        ttk.Label(outer, textvariable=self.status_var, foreground="#176b3a").pack(anchor="w", pady=(0, 8))

        task_frame = ttk.LabelFrame(outer, text="Create a saved daily task", padding=12)
        task_frame.pack(fill="x", pady=(0, 10))
        task_frame.columnconfigure(1, weight=1)
        task_frame.columnconfigure(3, weight=1)

        ttk.Label(task_frame, text="Task name").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(task_frame, textvariable=self.task_name_var).grid(row=0, column=1, sticky="ew", padx=(0, 14), pady=4)
        ttk.Label(task_frame, text="Phone serial").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=4)
        self.device_combo = ttk.Combobox(task_frame, textvariable=self.device_var)
        self.device_combo.grid(row=0, column=3, sticky="ew", pady=4)

        ttk.Label(task_frame, text="AI provider").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        self.provider_combo = ttk.Combobox(
            task_frame, textvariable=self.provider_var, values=list(self._provider_data), state="readonly"
        )
        self.provider_combo.grid(row=1, column=1, sticky="ew", padx=(0, 14), pady=4)
        self.provider_combo.bind("<<ComboboxSelected>>", self.on_provider_changed)
        ttk.Label(task_frame, text="Model ID").grid(row=1, column=2, sticky="w", padx=(0, 6), pady=4)
        self.model_combo = ttk.Combobox(task_frame, textvariable=self.model_var, values=self._provider_data["gemini"])
        self.model_combo.grid(row=1, column=3, sticky="ew", pady=4)

        ttk.Label(task_frame, text="Daily run time (24-hour)").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(task_frame, textvariable=self.daily_time_var, width=12).grid(row=2, column=1, sticky="w", padx=(0, 14), pady=4)
        ttk.Label(task_frame, text="Local time • repeats every day").grid(row=2, column=2, columnspan=2, sticky="w", pady=4)

        ttk.Label(task_frame, text="What should the agent do?").grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 4))
        self.prompt_text = tk.Text(task_frame, height=4, wrap="word", font=("Segoe UI", 10))
        self.prompt_text.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        self.prompt_text.insert("1.0", "Open the app I specify and complete this authorized task: ")
        action_row = ttk.Frame(task_frame)
        action_row.grid(row=5, column=0, columnspan=4, sticky="ew")
        ttk.Button(action_row, text="Save Daily Task", command=self.save_task).pack(side="left")
        ttk.Button(action_row, text="Refresh Connected Phones", command=self.refresh_devices).pack(side="left", padx=8)
        ttk.Label(
            action_row,
            text="Saved schedules remain on this computer; the background service must be running.",
            foreground="#555555",
        ).pack(side="right")

        saved_frame = ttk.LabelFrame(outer, text="Saved daily tasks", padding=10)
        saved_frame.pack(fill="both", expand=True)
        columns = ("name", "device", "time", "enabled", "next")
        self.tree = ttk.Treeview(saved_frame, columns=columns, show="headings", height=9, selectmode="browse")
        for key, title, width in (
            ("name", "Task", 220),
            ("device", "Phone", 170),
            ("time", "Daily at", 95),
            ("enabled", "Status", 80),
            ("next", "Next run", 150),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(saved_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Run Now", command=self.run_selected).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Pause / Enable", command=self.toggle_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="Delete", command=self.delete_selected).pack(side="left", padx=6)
        ttk.Checkbutton(
            buttons,
            text="Start automation service when I sign in to Windows",
            variable=self.startup_enabled,
            command=self.set_startup,
        ).pack(side="right")

        ttk.Label(
            outer,
            text=f"Local data: {DATA_DIR}  •  Dashboard: {_api_base()}",
            foreground="#666666",
        ).pack(anchor="w", pady=(10, 0))

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
            self.device_combo.configure(values=serials)
            if not self.device_var.get() and serials:
                self.device_var.set(serials[0])
        except Exception:
            pass

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
        for item in self.tree.get_children():
            self.tree.delete(item)
        for row in schedules:
            if row.get("job_type") != "agent_task":
                continue
            times = _daily_times(row.get("daily_times"))
            status = "Enabled" if row.get("is_enabled") else "Paused"
            self.tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                values=(
                    row.get("name") or "Saved task",
                    row.get("phone_serial") or "—",
                    ", ".join(times) or "—",
                    status,
                    row.get("next_run") or "—",
                ),
            )

    def save_task(self):
        prompt = self.prompt_text.get("1.0", "end").strip()
        name = self.task_name_var.get().strip() or (prompt[:45] + ("…" if len(prompt) > 45 else ""))
        device = self.device_var.get().strip()
        provider = self.provider_var.get().strip()
        model = self.model_var.get().strip()
        daily_time = self.daily_time_var.get().strip()
        if not device:
            messagebox.showwarning(APP_NAME, "Connect your phone with Android ADB or configure the target device first.")
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
        self.ensure_service(lambda: self._post_new_task(name, prompt, device, provider, model, daily_time))

    def _post_new_task(self, name: str, prompt: str, device: str, provider: str, model: str, daily_time: str):
        payload = {
            "name": name[:100],
            "job_type": "agent_task",
            "phone_serial": device,
            "priority": 2,
            "schedule_type": "daily",
            "daily_times": [daily_time],
            "max_duration_s": 1800,
            "is_enabled": 1,
            "config_json": {"prompt": prompt, "provider": provider, "model": model, "source": "windows_launcher"},
        }
        try:
            result = _api_request("/api/schedules", method="POST", payload=payload, timeout=8)
            if not isinstance(result, dict) or not result.get("ok"):
                raise RuntimeError("The scheduler did not confirm the saved task.")
            self.status_var.set(f"Saved daily task #{result.get('id')}: {name}")
            self.refresh_tasks()
            if provider == "gemini" and not self._gemini_key_present():
                messagebox.showinfo(
                    APP_NAME,
                    "Task saved. Add a Gemini API key with Configure Gemini Key before its scheduled run.",
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            messagebox.showerror(APP_NAME, f"Could not save the task (HTTP {exc.code}):\n{detail}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save the task: {type(exc).__name__}")

    def _selected_schedule_id(self) -> int | None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo(APP_NAME, "Select a saved task first.")
            return None
        try:
            return int(selected[0])
        except ValueError:
            return None

    def run_selected(self):
        sid = self._selected_schedule_id()
        if sid is None:
            return
        self.ensure_service(lambda: self._schedule_action(sid, "run-now"))

    def toggle_selected(self):
        sid = self._selected_schedule_id()
        if sid is None:
            return
        self.ensure_service(lambda: self._schedule_action(sid, "toggle"))

    def delete_selected(self):
        sid = self._selected_schedule_id()
        if sid is None:
            return
        if not messagebox.askyesno(APP_NAME, "Delete this saved task and its daily schedule?"):
            return
        self.ensure_service(lambda: self._schedule_action(sid, "delete"))

    def _schedule_action(self, sid: int, action: str):
        try:
            if action == "delete":
                _api_request(f"/api/schedules/{sid}", method="DELETE", timeout=4)
            else:
                _api_request(f"/api/schedules/{sid}/{action}", method="POST", payload={}, timeout=6)
            self.refresh_tasks()
            self.status_var.set(f"Task #{sid}: {action} completed")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            messagebox.showerror(APP_NAME, f"Task action failed (HTTP {exc.code}):\n{detail}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Task action failed: {type(exc).__name__}")

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
    root = tk.Tk()
    Launcher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
