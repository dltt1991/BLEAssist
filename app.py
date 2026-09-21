"""macOS virtual joystick for the CH9141 wheelchair chassis."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import scrolledtext, ttk

from ble_controller import ScanDevice, WheelchairBLE
from protocol import pointer_to_stick


SETTINGS_PATH = Path.home() / "Library" / "Application Support" / "BLEAssist" / "settings.json"


def load_last_device(path: Path = SETTINGS_PATH) -> dict[str, str] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None

    device = data.get("last_device") if isinstance(data, dict) else None
    if not isinstance(device, dict):
        return None

    key = device.get("key")
    name = device.get("name")
    if not isinstance(key, str) or not key.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return {"key": key, "name": name}


def save_last_device(key: str, name: str, path: Path = SETTINGS_PATH) -> None:
    if not isinstance(key, str) or not key.strip():
        raise ValueError("设备标识不能为空")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("设备名称不能为空")

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"last_device": {"key": key, "name": name}}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def preferred_device_index(
    devices: list[ScanDevice],
    last_device: dict[str, str] | None,
) -> int | None:
    if not devices:
        return None
    if last_device:
        for index, device in enumerate(devices):
            if device.key == last_device["key"]:
                return index
        for index, device in enumerate(devices):
            if device.name == last_device["name"]:
                return index
    return 0


class BLELoopThread:
    """Own the asyncio loop used by Bleak and expose thread-safe calls to Tk."""

    def __init__(self, events: queue.Queue):
        self.events = events
        self.loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started.wait()
        self.controller = WheelchairBLE(self._emit)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._started.set()
        self.loop.run_forever()

    def _emit(self, kind, payload=None) -> None:
        self.events.put((kind, payload))

    def submit(self, coroutine) -> concurrent.futures.Future:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def report_error(done):
            try:
                done.result()
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                pass
            except Exception as error:
                self._emit("error", str(error))

        future.add_done_callback(report_error)
        return future

    def set_stick(self, x: float, y: float, held: bool) -> None:
        self.loop.call_soon_threadsafe(self.controller.set_stick, x, y, held)

    def stop(self) -> None:
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=1.0)
        if not self.loop.is_closed():
            self.loop.close()


class WheelchairApp:
    CANVAS_SIZE = 260
    CENTER = CANVAS_SIZE / 2
    STICK_RADIUS = 94
    KNOB_RADIUS = 25

    STATUS_TEXT = {
        "idle": "未连接",
        "scanning": "扫描中…",
        "connecting": "连接中…",
        "handshaking": "握手中…",
        "ready": "已就绪",
        "disconnected": "已断开",
    }

    def __init__(self, root: tk.Tk, settings_path: Path = SETTINGS_PATH):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.ble = BLELoopThread(self.events)
        self.settings_path = Path(settings_path)
        self.last_device = load_last_device(self.settings_path)
        self.connecting_device: ScanDevice | None = None
        self.devices: dict[str, ScanDevice] = {}
        self.ready = False
        self.dragging = False
        self.closing = False
        self.close_future = None
        self.close_deadline = 0.0

        root.title("智能轮椅 BLE 控制器")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<FocusOut>", self.on_focus_out)
        self._build_ui()
        self._set_ready(False)
        self.root.after(50, self.poll_events)
        self.root.after(100, self.scan)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.grid(sticky="nsew")

        connection = ttk.LabelFrame(outer, text="蓝牙连接", padding=10)
        connection.grid(row=0, column=0, columnspan=2, sticky="ew")
        connection.columnconfigure(1, weight=1)

        self.scan_button = ttk.Button(connection, text="扫描", command=self.scan)
        self.scan_button.grid(row=0, column=0, padx=(0, 8))
        self.device_box = ttk.Combobox(connection, width=38, state="readonly")
        self.device_box.grid(row=0, column=1, sticky="ew")
        self.connect_button = ttk.Button(connection, text="连接", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(connection, text="状态：").grid(row=1, column=0, pady=(8, 0), sticky="e")
        self.status_var = tk.StringVar(value="未连接")
        ttk.Label(connection, textvariable=self.status_var).grid(
            row=1, column=1, columnspan=2, pady=(8, 0), sticky="w"
        )

        control = ttk.LabelFrame(outer, text="虚拟摇杆（按住鼠标才会运动）", padding=10)
        control.grid(row=1, column=0, padx=(0, 10), pady=10, sticky="n")
        self.canvas = tk.Canvas(
            control,
            width=self.CANVAS_SIZE,
            height=self.CANVAS_SIZE,
            background="#f3f4f6",
            highlightthickness=0,
        )
        self.canvas.grid()
        margin = self.CENTER - self.STICK_RADIUS
        self.canvas.create_oval(
            margin,
            margin,
            self.CANVAS_SIZE - margin,
            self.CANVAS_SIZE - margin,
            fill="#dbe4ee",
            outline="#7a8998",
            width=2,
        )
        self.canvas.create_line(self.CENTER, margin, self.CENTER, self.CANVAS_SIZE - margin, fill="#aab4be")
        self.canvas.create_line(margin, self.CENTER, self.CANVAS_SIZE - margin, self.CENTER, fill="#aab4be")
        self.knob = self.canvas.create_oval(0, 0, 0, 0, fill="#3478c9", outline="#1e4f88", width=2)
        self._draw_stick(0.0, 0.0)
        self.canvas.bind("<ButtonPress-1>", self.on_stick_press)
        self.canvas.bind("<B1-Motion>", self.on_stick_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_stick_release)

        side = ttk.Frame(outer)
        side.grid(row=1, column=1, pady=10, sticky="nsew")
        mode_frame = ttk.LabelFrame(side, text="运动配置", padding=10)
        mode_frame.grid(row=0, column=0, sticky="ew")
        self.mode_var = tk.IntVar(value=1)
        self.mode_buttons = []
        ttk.Label(mode_frame, text="原厂固定配置：1").grid(padx=10, pady=6)

        self.stop_button = tk.Button(
            side,
            text="紧急停止",
            command=self.emergency_stop,
            background="#c62828",
            foreground="white",
            activebackground="#8e0000",
            activeforeground="white",
            font=("TkDefaultFont", 14, "bold"),
            height=2,
        )
        self.stop_button.grid(row=1, column=0, pady=(14, 0), sticky="ew")

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.log_widget = scrolledtext.ScrolledText(log_frame, width=76, height=10, state="disabled")
        self.log_widget.grid()
        self.log("程序已启动；正在自动扫描设备。")

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"[{stamp}] {message}\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _set_ready(self, ready: bool) -> None:
        self.ready = ready
        state = "normal" if ready else "disabled"
        for button in self.mode_buttons:
            button.configure(state=state)
        self.connect_button.configure(text="断开" if ready else "连接")
        if not ready:
            self.release_stick()

    def scan(self) -> None:
        if self.ready or self.closing:
            return
        self.scan_button.configure(state="disabled")
        self.device_box.configure(values=())
        self.devices.clear()
        self.ble.submit(self.ble.controller.scan())

    def toggle_connection(self) -> None:
        if self.closing:
            return
        if self.ready:
            self.release_stick()
            self.ble.submit(self.ble.controller.disconnect())
            return
        label = self.device_box.get()
        device = self.devices.get(label)
        if device is None:
            self.log("请先扫描并选择一个设备。")
            return
        self.scan_button.configure(state="disabled")
        self.connect_button.configure(state="disabled")
        self.connecting_device = device
        self.ble.submit(self.ble.controller.connect(device))

    def change_mode(self) -> None:
        if not self.ready:
            return
        for button in self.mode_buttons:
            button.configure(state="disabled")
        self.ble.submit(self.ble.controller.set_mode(self.mode_var.get()))

    def emergency_stop(self) -> None:
        self.release_stick()
        self._set_ready(False)
        self.status_var.set("紧急停止中…")
        self.log("执行紧急停止：回中三次并断开。")
        self.ble.submit(self.ble.controller.emergency_stop())

    def _stick_from_event(self, event) -> tuple[float, float]:
        return pointer_to_stick(
            event.x,
            event.y,
            self.CENTER,
            self.CENTER,
            self.STICK_RADIUS,
        )

    def _draw_stick(self, x: float, y: float) -> None:
        knob_x = self.CENTER + x * self.STICK_RADIUS
        knob_y = self.CENTER + y * self.STICK_RADIUS
        r = self.KNOB_RADIUS
        self.canvas.coords(self.knob, knob_x - r, knob_y - r, knob_x + r, knob_y + r)

    def on_stick_press(self, event) -> None:
        if not self.ready:
            return
        self.dragging = True
        self._update_stick(event)

    def on_stick_motion(self, event) -> None:
        if self.dragging and self.ready:
            self._update_stick(event)

    def _update_stick(self, event) -> None:
        x, y = self._stick_from_event(event)
        self._draw_stick(x, y)
        self.ble.set_stick(x, y, True)

    def on_stick_release(self, _event=None) -> None:
        self.release_stick()

    def on_focus_out(self, _event=None) -> None:
        if self.dragging:
            self.release_stick()

    def release_stick(self) -> None:
        self.dragging = False
        if hasattr(self, "knob"):
            self._draw_stick(0.0, 0.0)
        if hasattr(self, "ble"):
            self.ble.set_stick(0.0, 0.0, False)

    def poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            self.handle_event(kind, payload)
        if not self.closing:
            self.root.after(50, self.poll_events)

    def handle_event(self, kind: str, payload) -> None:
        if kind == "devices":
            labels = []
            for device in payload:
                marker = " ★" if device.preferred else ""
                label = f"{device.name}{marker} — {device.key}"
                labels.append(label)
                self.devices[label] = device
            self.device_box.configure(values=labels)
            selected = preferred_device_index(payload, self.last_device)
            if selected is not None:
                self.device_box.current(selected)
                device = payload[selected]
                if self.last_device and (
                    device.key == self.last_device["key"]
                    or device.name == self.last_device["name"]
                ):
                    self.log(f"已选择上次连接的设备：{device.name}。")
            self.log(f"扫描完成，发现 {len(labels)} 个有名称的设备。")
        elif kind == "status":
            self.status_var.set(self.STATUS_TEXT.get(payload, str(payload)))
            self.log(f"状态：{self.status_var.get()}")
            if payload == "ready":
                if self.connecting_device is not None:
                    device = self.connecting_device
                    try:
                        save_last_device(device.key, device.name, self.settings_path)
                    except (OSError, ValueError) as error:
                        self.log(f"保存上次连接设备失败：{error}")
                    else:
                        self.last_device = {"key": device.key, "name": device.name}
                    self.connecting_device = None
                self._set_ready(True)
                self.scan_button.configure(state="disabled")
                self.connect_button.configure(state="normal")
            elif payload in ("idle", "disconnected"):
                if payload == "disconnected":
                    self.connecting_device = None
                self._set_ready(False)
                self.scan_button.configure(state="normal")
                self.connect_button.configure(state="normal")
            else:
                self.connect_button.configure(state="disabled")
        elif kind == "mode":
            self.mode_var.set(payload)
            for button in self.mode_buttons:
                button.configure(state="normal")
            self.log(f"速度档位已切换为 {payload}。")
        elif kind == "error":
            self.status_var.set("错误")
            self.log(f"错误：{payload}")
            if not self.ready:
                self.connecting_device = None
                self.scan_button.configure(state="normal")
                self.connect_button.configure(state="normal")
        elif kind == "motion":
            self.log(f"TX {payload}（摇杆持续发送）")
        elif kind in ("tx", "rx") and not str(payload).startswith("Y2C"):
            self.log(f"{kind.upper()} {payload}")

    def on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.release_stick()
        self.status_var.set("安全断开中…")
        self.close_future = self.ble.submit(self.ble.controller.disconnect())
        self.close_deadline = time.monotonic() + 2.0
        self.root.after(50, self._finish_close)

    def _finish_close(self) -> None:
        if self.close_future.done() or time.monotonic() >= self.close_deadline:
            self.ble.stop()
            self.root.destroy()
            return
        self.root.after(50, self._finish_close)


def main() -> None:
    root = tk.Tk()
    WheelchairApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
