"""Portable desktop controller; stdlib only until the backend child starts.

Layout: Start.vbs, runtime/pythonw.exe, app/desktop_launcher.py,
app/backend, app/web/dist and assets/football.seed.sqlite3.
The desktop controller owns a localhost-only backend and its lifetime.
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
from ctypes import wintypes
from contextlib import closing
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser

APP_NAME = "Football Intelligence AI"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


class LauncherError(Exception):
    """A message safe to show to a nontechnical user."""


def writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / (".write-check-" + uuid.uuid4().hex)
        with probe.open("xb") as handle:
            handle.write(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def data_directory(package_root: Path, override: Path | None = None) -> Path:
    if override is not None:
        if not writable_directory(override):
            raise LauncherError("無法儲存資料。請將整個資料夾放到「文件」內，再重新啟動。")
        return override
    local = package_root / "data"
    if writable_directory(local):
        return local
    base = os.environ.get("LOCALAPPDATA")
    if base:
        fallback = Path(base) / "FootballIntelligenceAI"
        if writable_directory(fallback):
            return fallback
    raise LauncherError("此位置無法儲存資料。請將整個資料夾放到「文件」內，再重新啟動。")


def copy_seed_once(seed: Path, destination: Path) -> None:
    """Validate and copy a committed SQLite snapshot, never overwrite user data."""
    if destination.exists():
        return
    if not seed.is_file():
        raise LauncherError("下載包缺少初始足球資料，請重新下載及完整解壓縮。")
    temporary = destination.with_name(destination.name + ".seed-" + uuid.uuid4().hex)
    try:
        # SQLite backup also handles a source with valid WAL state safely.
        # A Connection's own context manager commits/rolls back but does not
        # close its Windows file handle. Both must close before the rename.
        with closing(sqlite3.connect(seed.as_uri() + "?mode=ro", uri=True)) as source:
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise LauncherError("初始足球資料損壞，請重新下載。")
            if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='matches'").fetchone():
                raise LauncherError("初始足球資料格式不正確，請重新下載。")
            with closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
        # Session lock prevents two launchers from copying concurrently.
        if not destination.exists():
            temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy_raw_cache_once(source: Path, destination: Path) -> None:
    """Seed provider evidence without replacing newer user observations or pins."""
    if not source.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if not path.is_file() or path.is_symlink() or path.suffix != ".json":
            continue
        target = destination / path.name
        if target.exists():
            continue
        # Write complete evidence first; interrupted copies must not become a
        # cache file that the next start incorrectly treats as user data.
        temporary = destination / (path.name + ".seed-" + uuid.uuid4().hex)
        try:
            with path.open("rb") as original, temporary.open("xb") as copied:
                shutil.copyfileobj(original, copied)
            if not target.exists():
                temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


class SessionLock:
    """OS lock is released even when the controller crashes."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class WindowsJob:
    """Kernel kills the backend if the desktop controller unexpectedly exits."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise LauncherError("Windows 未能建立啟動管理器。請關閉程式再重試。")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise LauncherError("Windows 未能安全管理背景程式，請重新啟動電腦再試。")

    def assign(self, process: subprocess.Popen) -> None:
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            process.terminate()
            process.wait(timeout=10)
            raise LauncherError("Windows 阻止了背景程式啟動。請檢查防毒軟件的事件記錄；毋須關閉保護。")

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class RedactingFormatter(logging.Formatter):
    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.secrets = [os.environ[key] for key in ("FOOTBALL_DATA_API_KEY", "ADMIN_TOKEN")
                        if os.environ.get(key)]

    def format(self, record: logging.LogRecord) -> str:
        result = super().format(record)
        for value in self.secrets:
            result = result.replace(value, "[REDACTED]")
        return result


def make_logger(directory: Path) -> logging.Logger:
    logger = logging.getLogger("desktop_launcher")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old in list(logger.handlers):
        old.close()
        logger.removeHandler(old)
    handler = RotatingFileHandler(directory / "launcher.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(RedactingFormatter())
    logger.addHandler(handler)
    return logger


def choose_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def get_json(url: str, timeout: float = 2) -> dict:
    # Local readiness must not use system HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.load(response)


class BackendController:
    def __init__(self, root: Path, directory: Path, *, open_browser: bool = True, scheduler: bool = True):
        self.root = root.resolve()
        self.directory = directory.resolve()
        self.open_browser = open_browser
        self.scheduler = scheduler
        self.events = queue.SimpleQueue()
        self.logger = make_logger(directory)
        self.process = None
        self.job = None
        self.port = None
        self.url = None
        self.ready = False
        self.stopping = threading.Event()
        self.guard = threading.RLock()
        self.session_lock = SessionLock(directory / "launcher.lock")
        self.owned_session = False
        self.session_path = directory / "launcher-session.json"
        self.output_thread = None
        self.start_thread = None

    def start(self) -> None:
        self.events.put(("status", "正在啟動網站，請稍候…"))
        if not self.session_lock.acquire():
            # Another launch may be between locking and health readiness.
            for _ in range(80):
                if self.stopping.is_set():
                    return
                try:
                    previous = json.loads(self.session_path.read_text(encoding="utf-8"))
                    port = previous["port"]
                    if not isinstance(port, int) or not 1024 <= port <= 65535:
                        raise ValueError("invalid port")
                    url = f"http://127.0.0.1:{port}"
                    if get_json(url + "/health").get("status") == "ok":
                        opened = self.open_website(url) if self.open_browser else True
                        event = "existing" if opened else "existing_browser_failed"
                        self.events.put((event, "網站已在另一個啟動視窗運行。\n本機網址：" + url))
                        return
                except (OSError, ValueError, KeyError, urllib.error.URLError):
                    pass
                time.sleep(0.25)
            raise LauncherError("程式已在啟動中。請返回原有的啟動視窗；如沒有視窗，請稍候再試。")
        self.owned_session = True
        copy_seed_once(self.root / "assets" / "football.seed.sqlite3", self.directory / "football.db")
        copy_raw_cache_once(self.root / "assets" / "raw", self.directory / "raw")
        application = self.root / "app"
        if not (application / "backend" / "main.py").is_file() or not (application / "web" / "dist" / "index.html").is_file():
            raise LauncherError("下載包不完整。請先解壓縮整個 ZIP，再啟動。")
        python = self.root / "runtime" / "python.exe" if os.name == "nt" else Path(sys.executable)
        if not python.is_file():
            raise LauncherError("找不到附帶的執行程式。請完整解壓縮 ZIP，再檢查防毒軟件是否隔離了檔案。")
        self.port = choose_port()
        self.url = f"http://127.0.0.1:{self.port}"
        environment = os.environ.copy()
        environment.update(DATABASE_PATH=str(self.directory / "football.db"),
                           FOOTBALL_RAW_DIR=str(self.directory / "raw"),
                           SCHEDULER_ENABLED="true" if self.scheduler else "false")
        environment.setdefault("DATA_PROVIDER", "openfootball")
        # This child never listens on a public or LAN interface.
        command = [str(python), "-I", "-X", "utf8", "-B", str(application / "desktop_launcher.py"), "--serve", "--port", str(self.port)]
        with self.guard:
            if self.stopping.is_set():
                return
            self.job = WindowsJob()
            self.process = subprocess.Popen(command, cwd=application, env=environment,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.job.assign(self.process)
        self.output_thread = threading.Thread(target=self._read_output, name="backend-log", daemon=True)
        self.output_thread.start()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not self.stopping.is_set():
            if self.process.poll() is not None:
                self.logger.error("Backend exited during startup, exit code %s", self.process.returncode)
                raise LauncherError("網站未能啟動。請完整解壓縮下載包，並查看資料夾內的 launcher.log 啟動記錄。")
            try:
                response = get_json(self.url + "/health", timeout=1)
                if response.get("status") == "ok" and response.get("database") == "ready":
                    # Frontend must also be present, not just an open backend port.
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(self.url + "/", timeout=2) as front:
                        if front.status != 200 or b"<html" not in front.read(4096).lower():
                            raise LauncherError("網頁檔案不完整，請重新下載及解壓縮。")
                    if self.process.poll() is not None:
                        continue
                    if self.stopping.is_set():
                        return
                    self.session_path.write_text(json.dumps({"port": self.port, "pid": self.process.pid}), encoding="utf-8")
                    self.ready = True
                    self.logger.info("Local website is ready on port %s; data directory: %s", self.port, self.directory)
                    self.events.put(("ready", "網站已啟動；請保留這個視窗。"))
                    if self.open_browser:
                        self.open_website()
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.3)
        if not self.stopping.is_set():
            raise LauncherError("啟動超時。請關閉程式後再試；如仍然失敗，請查看 launcher.log 記錄。")

    def _read_output(self) -> None:
        try:
            for line in self.process.stdout:
                self.logger.info("backend: %s", line.rstrip())
        except (OSError, ValueError):
            pass

    def open_website(self, address: str | None = None) -> bool:
        """A missing default browser must never stop a healthy local server."""
        address = address or self.url
        if not address:
            return False
        try:
            opened = bool(webbrowser.open(address))
        except Exception as error:
            self.logger.warning("Could not open default browser (%s)", type(error).__name__)
            opened = False
        if not opened:
            self.events.put(("browser_failed", "網站已啟動；請在瀏覽器開啟下列網址。"))
        return opened

    def start_in_background(self) -> None:
        def worker():
            try:
                self.start()
            except LauncherError as error:
                self.logger.warning("Startup failed: %s", error)
                self.events.put(("error", str(error)))
            except Exception:
                self.logger.exception("Unexpected launcher startup failure")
                self.events.put(("error", "啟動時遇到問題。請查看 launcher.log 啟動記錄。"))
        self.start_thread = threading.Thread(target=worker, name="backend-start", daemon=True)
        self.start_thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.start_thread and self.start_thread is not threading.current_thread():
            self.start_thread.join(timeout=15)
        with self.guard:
            if self.process is not None:
                if self.process.poll() is None:
                    try:
                        self.process.stdin.write("stop\n")
                        self.process.stdin.flush()
                        self.process.wait(timeout=12)
                    except (OSError, ValueError, subprocess.TimeoutExpired):
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                            self.process.wait(timeout=5)
                if self.process.stdin:
                    self.process.stdin.close()
                if self.output_thread:
                    self.output_thread.join(timeout=2)
                if self.process.stdout:
                    self.process.stdout.close()
                self.process = None
            if self.job:
                self.job.close()
                self.job = None
            if self.owned_session:
                try:
                    self.session_path.unlink(missing_ok=True)
                except OSError:
                    self.logger.warning("Could not remove old desktop session metadata")
                finally:
                    self.session_lock.release()
                    self.owned_session = False
            self.ready = False


def serve(port: int) -> int:
    # -I removes the script directory: restore only this packaged application,
    # never the user's current directory, PYTHONPATH or user site-packages.
    application = str(Path(__file__).resolve().parent)
    if application not in sys.path:
        sys.path.insert(0, application)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config("backend.main:app", host="127.0.0.1", port=port,
                                         access_log=False, log_level="warning", timeout_graceful_shutdown=8))

    def control_pipe():
        try:
            # EOF also means the owner is gone; leave no hidden orphan server.
            sys.stdin.readline()
        except (OSError, ValueError):
            pass
        server.should_exit = True

    threading.Thread(target=control_pipe, name="desktop-control", daemon=True).start()
    server.run()
    return 0 if server.started else 1


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    else:
        print(message, file=sys.stderr)


def windows_window(controller: BackendController) -> int:
    """Small native Windows owner window, no Tk/Qt/WebView install required."""
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM)

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    prototypes = {
        "RegisterClassW": ([ctypes.POINTER(WNDCLASS)], wintypes.ATOM),
        "CreateWindowExW": ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
        "DefWindowProcW": ([wintypes.HWND, wintypes.UINT, WPARAM, LPARAM], LRESULT),
        "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        "UpdateWindow": ([wintypes.HWND], wintypes.BOOL),
        "SetWindowTextW": ([wintypes.HWND, wintypes.LPCWSTR], wintypes.BOOL),
        "EnableWindow": ([wintypes.HWND, wintypes.BOOL], wintypes.BOOL),
        "DestroyWindow": ([wintypes.HWND], wintypes.BOOL),
        "GetMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
        "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], LRESULT),
        "SendMessageW": ([wintypes.HWND, wintypes.UINT, WPARAM, LPARAM], LRESULT),
        "PostMessageW": ([wintypes.HWND, wintypes.UINT, WPARAM, LPARAM], wintypes.BOOL),
        "SetTimer": ([wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p], ctypes.c_size_t),
        "KillTimer": ([wintypes.HWND, ctypes.c_size_t], wintypes.BOOL),
        "PostQuitMessage": ([ctypes.c_int], None),
        "LoadCursorW": ([wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HANDLE),
        "MessageBoxW": ([wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT], ctypes.c_int),
    }
    for name, (arguments, result) in prototypes.items():
        function = getattr(user, name)
        function.argtypes, function.restype = arguments, result
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    gdi.GetStockObject.argtypes = [ctypes.c_int]
    gdi.GetStockObject.restype = wintypes.HANDLE
    try:
        user.SetProcessDPIAware()
    except AttributeError:
        pass
    instance = kernel.GetModuleHandleW(None)
    controls = {}
    state = {"closing": False, "exit_code": 0}
    final_message = 0x8001

    def begin_close(hwnd):
        if state["closing"]:
            return
        state["closing"] = True
        user.SetWindowTextW(controls["status"], "正在儲存及關閉網站，請稍候…")
        user.EnableWindow(controls["open"], False)
        user.EnableWindow(controls["stop"], False)

        def finish():
            controller.stop()
            user.PostMessageW(hwnd, final_message, 0, 0)
        threading.Thread(target=finish, name="backend-stop", daemon=True).start()

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        try:
            if message == 0x0010:  # WM_CLOSE
                begin_close(hwnd)
                return 0
            if message == 0x0111:  # WM_COMMAND
                control_id = wparam & 0xFFFF
                if control_id == 101 and controller.ready:
                    controller.open_website()
                elif control_id == 102:
                    begin_close(hwnd)
                return 0
            if message == 0x0113:  # WM_TIMER
                while True:
                    try:
                        event, text = controller.events.get_nowait()
                    except queue.Empty:
                        break
                    if state["closing"]:
                        continue
                    user.SetWindowTextW(controls["status"], text)
                    if event == "ready":
                        user.SetWindowTextW(controls["detail"], "關閉瀏覽器不會停止更新；按「停止並離開」才會關閉網站。")
                        user.SetWindowTextW(controls["url"], controller.url)
                        user.EnableWindow(controls["open"], True)
                    elif event == "browser_failed":
                        user.SetWindowTextW(controls["detail"], "可選取及複製下列網址，到你的瀏覽器貼上開啟。")
                        if controller.url:
                            user.SetWindowTextW(controls["url"], controller.url)
                    elif event == "error":
                        state["exit_code"] = 1
                        user.MessageBoxW(hwnd, text + "\n\n資料位置：" + str(controller.directory), APP_NAME, 0x10)
                        begin_close(hwnd)
                    elif event in {"existing", "existing_browser_failed"}:
                        if event == "existing_browser_failed":
                            user.MessageBoxW(hwnd, text + "\n請到原有啟動視窗複製網址，在瀏覽器開啟。", APP_NAME, 0x40)
                        begin_close(hwnd)
                if controller.ready and controller.process and controller.process.poll() is not None and not state["closing"]:
                    state["exit_code"] = 1
                    user.MessageBoxW(hwnd, "網站背景程式已停止。你的資料已保留；請重新啟動。", APP_NAME, 0x10)
                    begin_close(hwnd)
                return 0
            if message == final_message:
                user.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:  # WM_DESTROY
                user.KillTimer(hwnd, 1)
                user.PostQuitMessage(state["exit_code"])
                return 0
        except Exception:
            controller.logger.exception("Native window event failed")
            state["exit_code"] = 1
            user.PostQuitMessage(1)
            return 0
        return user.DefWindowProcW(hwnd, message, wparam, lparam)

    class_name = "FootballIntelligenceAI.Portable.Launcher"
    window_class = WNDCLASS()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.hCursor = user.LoadCursorW(None, ctypes.c_void_p(32512))
    window_class.hbrBackground = wintypes.HBRUSH(16)  # COLOR_BTNFACE + 1
    window_class.lpszClassName = class_name
    if not user.RegisterClassW(ctypes.byref(window_class)):
        raise LauncherError("Windows 未能建立程式視窗，請重新啟動後再試。")
    # Caption + system menu + minimize; fixed small controller window.
    hwnd = user.CreateWindowExW(0, class_name, APP_NAME, 0x00CA0000,
                                0x80000000, 0x80000000, 620, 246, None, None, instance, None)
    if not hwnd:
        raise LauncherError("Windows 未能顯示程式視窗，請重新啟動後再試。")
    font = gdi.GetStockObject(17)  # DEFAULT_GUI_FONT

    def add(name, kind, text, x, y, width, height, control_id=0, extra_style=0):
        child = user.CreateWindowExW(0, kind, text, 0x50000000 | extra_style, x, y, width, height,
                                     hwnd, wintypes.HMENU(control_id), instance, None)
        if not child:
            raise LauncherError("Windows 未能顯示操作按鈕，請重新啟動後再試。")
        user.SendMessageW(child, 0x0030, int(font), 1)
        controls[name] = child

    add("title", "STATIC", "Football Intelligence AI — Windows 下載版", 22, 18, 575, 25)
    add("status", "STATIC", "正在啟動網站，請稍候…", 22, 54, 575, 25)
    add("detail", "STATIC", "網站只在這部電腦運行；初次啟動會建立你的個人資料庫。", 22, 86, 575, 22)
    add("url", "EDIT", "啟動後會在這裏顯示本機網址。", 22, 110, 575, 24,
        extra_style=0x00800880)  # WS_BORDER | ES_READONLY | ES_AUTOHSCROLL
    add("open", "BUTTON", "開啟網站", 22, 148, 160, 36, 101)
    add("stop", "BUTTON", "停止並離開", 198, 148, 160, 36, 102)
    user.EnableWindow(controls["open"], False)
    user.ShowWindow(hwnd, 1)
    user.UpdateWindow(hwnd)
    if not user.SetTimer(hwnd, 1, 250, None):
        user.DestroyWindow(hwnd)
        raise LauncherError("Windows 未能啟動狀態監察，請重新啟動後再試。")
    controller.start_in_background()
    message = wintypes.MSG()
    while True:
        result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
        if result == -1:
            state["exit_code"] = 1
            break
        if result == 0:
            break
        user.TranslateMessage(ctypes.byref(message))
        user.DispatchMessageW(ctypes.byref(message))
    return state["exit_code"]


def headless(controller: BackendController) -> int:
    """Integration harness and the visible-console fallback; no commands needed."""
    try:
        controller.start()
        if not controller.owned_session:
            return 0
        print(json.dumps({"status": "ready", "url": controller.url, "data_directory": str(controller.directory)}, ensure_ascii=False), flush=True)
        if os.name == "nt":
            print("網站已開啟。請保留此視窗；關閉視窗即可停止網站。", flush=True)
        while controller.process and controller.process.poll() is None:
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        return 0
    except LauncherError as error:
        show_error(str(error))
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-scheduler", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--package-root", type=Path, default=DEFAULT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.serve:
        if options.port is None or not 1024 <= options.port <= 65535:
            return 2
        return serve(options.port)
    try:
        directory = data_directory(options.package_root, options.data_dir)
        controller = BackendController(options.package_root, directory,
                                       open_browser=not options.no_browser, scheduler=not options.no_scheduler)
    except (OSError, LauncherError) as error:
        show_error(str(error))
        return 1
    atexit.register(controller.stop)
    if os.name != "nt":
        def interrupted(signum, frame):
            raise KeyboardInterrupt()
        signal.signal(signal.SIGTERM, interrupted)
    try:
        if os.name == "nt" and not options.headless:
            return windows_window(controller)
        return headless(controller)
    except LauncherError as error:
        show_error(str(error))
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        controller.logger.exception("Desktop launcher failed")
        show_error("網站未能啟動。請查看資料位置內的 launcher.log 記錄。")
        return 1
    finally:
        controller.stop()


if __name__ == "__main__":
    raise SystemExit(main())
