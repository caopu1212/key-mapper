"""按键映射工具：支持组合键、鼠标中键/侧键/滚轮触发。按键改键（目标可以是鼠标按键/滚轮）、鼠标点击/连点、拖动、滚轮、按键连发。纯标准库（ctypes + tkinter），仅 Windows。

原理：WH_KEYBOARD_LL / WH_MOUSE_LL 低级钩子拦截源按键，再用 SendInput 发送目标按键或鼠标动作。
"""
import ctypes
import json
import os
import sys
import queue
import threading
import time
import tkinter as tk
import winsound
from ctypes import wintypes
from tkinter import messagebox, simpledialog, ttk

# 打包成 exe 时 __file__ 在临时解压目录里，配置要放在 exe 旁边
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "keymap_config.json")
# 图标：打包时用 --add-data 放进 exe，运行时解压在 sys._MEIPASS
ICON_PATH = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "icon.ico")

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_APP = 0x8000  # 发给钩子线程：重新判断是否需要鼠标钩子
WM_WHEEL_UP = WM_APP + 1  # 发给钩子线程：滚轮触发的"按键"到时间松开（wParam=vk, lParam=序号）
# 鼠标按下要保持的时间：游戏通常每帧（60fps 约 16ms）才读一次鼠标状态，按下/松开在同一帧里就会被漏掉
PRESS_S = 0.04
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP, WM_QUIT = 0x100, 0x101, 0x104, 0x105, 0x12
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x1, 0x800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
MOUSE_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}
BTN_TEXT = {"left": "左键", "right": "右键", "middle": "中键"}
# 鼠标按键也当作"按键"映射/连发：vk -> (按下 flags, 松开 flags, mouseData)
MOUSE_BTN_VKS = {0x01: (0x2, 0x4, 0), 0x02: (0x8, 0x10, 0), 0x04: (0x20, 0x40, 0),
                 0x05: (0x80, 0x100, 1), 0x06: (0x80, 0x100, 2)}
# 滚轮没有 vk，用 0x100 以上的自定义编号：vk -> (flags, 每格的 mouseData)
WHEEL_VKS = {0x101: (MOUSEEVENTF_WHEEL, 120), 0x102: (MOUSEEVENTF_WHEEL, -120),
             0x103: (MOUSEEVENTF_HWHEEL, -120), 0x104: (MOUSEEVENTF_HWHEEL, 120)}
WHEEL_DIRS = {"up": (MOUSEEVENTF_WHEEL, 1), "down": (MOUSEEVENTF_WHEEL, -1),
              "left": (MOUSEEVENTF_HWHEEL, -1), "right": (MOUSEEVENTF_HWHEEL, 1)}
WHEEL_TEXT = {"up": "上", "down": "下", "left": "左", "right": "右"}
MODE_TEXT = {"once": "按一次执行一次", "hold": "按住时连续执行", "toggle": "按一下开始，再按停止"}
TYPE_TEXT = {"key": "映射为按键", "disable": "禁用按键", "click": "鼠标点击", "drag": "鼠标拖动",
             "wheel": "鼠标滚轮", "turbo": "按键连发"}
REPEATABLE = {"click", "wheel", "turbo"}  # 支持连点/连发的动作
MARKER = 0x4B4D4150  # 标记自己发出的输入，避免被钩子再次处理

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]

# ---------- 按键名称 ----------
VK_NAMES = {0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "CapsLock", 0x1B: "Esc",
            0x20: "Space", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
            0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2C: "PrintScreen", 0x2D: "Insert",
            0x2E: "Delete", 0x5B: "LWin", 0x5C: "RWin", 0x5D: "Menu", 0x90: "NumLock", 0x91: "ScrollLock",
            0xA0: "LShift", 0xA1: "RShift", 0xA2: "LCtrl", 0xA3: "RCtrl", 0xA4: "LAlt", 0xA5: "RAlt",
            0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
            0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
            0x1C: "Convert", 0x1D: "NonConvert", 0x19: "Kanji", 0xF3: "Zenkaku/Hankaku", 0xF4: "Zenkaku/Hankaku",
            0xF2: "Hiragana", 0xF0: "Eisu",
            0x01: "鼠标左键", 0x02: "鼠标右键", 0x04: "鼠标中键", 0x05: "鼠标侧键1(后退)", 0x06: "鼠标侧键2(前进)",
            0x101: "滚轮向上", 0x102: "滚轮向下", 0x103: "滚轮向左", 0x104: "滚轮向右"}
MOUSE_TARGETS = [*MOUSE_BTN_VKS, *WHEEL_VKS]  # GUI 里"鼠标按键"菜单的顺序
# 可作触发键的鼠标输入（左/右键不行：会让鼠标没法正常用，也会在点"捕获按键"后被误捕获）
# 鼠标消息 -> (vk, 是否松开)；滚轮消息按方向另算
MOUSE_MSGS = {0x207: (0x04, False), 0x208: (0x04, True)}
WM_XBUTTONDOWN, WM_XBUTTONUP, WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x20B, 0x20C, 0x20A, 0x20E
MOUSE_TRIGGERS = {0x04, 0x05, 0x06, *WHEEL_VKS}
MOUSE_SOURCES = [0x04, 0x05, 0x06, *WHEEL_VKS]  # GUI 里原按键"鼠标"菜单的顺序
for _c in range(0x30, 0x3A):
    VK_NAMES[_c] = chr(_c)
for _c in range(0x41, 0x5B):
    VK_NAMES[_c] = chr(_c)
for _i in range(24):
    VK_NAMES[0x70 + _i] = f"F{_i + 1}"
for _i in range(10):
    VK_NAMES[0x60 + _i] = f"Num{_i}"
VK_NAMES.update({0x6A: "Num*", 0x6B: "Num+", 0x6D: "Num-", 0x6E: "Num.", 0x6F: "Num/"})
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D,
                0x6F, 0x90, 0xA3, 0xA5}


MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_WIN = 1, 2, 4, 8
MOD_BITS = {0x11: MOD_CTRL, 0xA2: MOD_CTRL, 0xA3: MOD_CTRL, 0x10: MOD_SHIFT, 0xA0: MOD_SHIFT, 0xA1: MOD_SHIFT,
            0x12: MOD_ALT, 0xA4: MOD_ALT, 0xA5: MOD_ALT, 0x5B: MOD_WIN, 0x5C: MOD_WIN}
MOD_SEND = ((MOD_CTRL, 0xA2, "Ctrl"), (MOD_SHIFT, 0xA0, "Shift"), (MOD_ALT, 0xA4, "Alt"), (MOD_WIN, 0x5B, "Win"))
VK_MASK = 0xE8  # 未分配的 vk：松开 Alt/Win 前插入一下，避免弹出菜单栏/开始菜单


def vk_name(vk):
    return VK_NAMES.get(vk, f"VK_0x{vk:02X}")


def combo_name(mods, vk):
    return "+".join([name for bit, _, name in MOD_SEND if mods & bit] + [vk_name(vk)])


def parse_trigger(s):
    """配置里的触发键："65"（旧格式）或 "1:65"（修饰键位掩码:vk）。"""
    mods, _, vk = str(s).rpartition(":")
    return int(mods or 0), int(vk)


# ---------- 输入模拟 ----------
def send_key(vk, up):
    if vk in MOUSE_BTN_VKS:  # 鼠标按键：在当前位置按下/松开
        down, release, data = MOUSE_BTN_VKS[vk]
        send_mouse(release if up else down, data=data)
        return
    if vk in WHEEL_VKS:  # 滚轮：按下时转一格，松开不做事
        if not up:
            send_mouse(WHEEL_VKS[vk][0], data=WHEEL_VKS[vk][1])
        return
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, MARKER))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_combo(mods, vk, up):
    """按下时先按修饰键再按主键，松开时顺序相反。"""
    keys = [v for bit, v, _ in MOD_SEND if mods & bit] + [vk]
    for v in (reversed(keys) if up else keys):
        send_key(v, up)


def send_mouse(flags, dx=0, dy=0, data=0):
    inp = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, MARKER))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def cursor_pos():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _abs(x, y):
    """屏幕坐标 -> SendInput 绝对坐标（0~65535，覆盖整个虚拟桌面），保证换算回来正好是同一像素。"""
    gm = user32.GetSystemMetrics
    vx, vy, vw, vh = gm(76), gm(77), gm(78), gm(79)
    return ((x - vx) * 65536 + vw - 1) // vw, ((y - vy) * 65536 + vh - 1) // vh


ABS_MOVE = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK


def move_abs(x, y):
    """用 SendInput 绝对移动（与点击在同一输入队列里按顺序处理，程序能收到移动消息）。"""
    send_mouse(ABS_MOVE, *_abs(int(x), int(y)))


def do_click(act):
    # 移动、按下、松开、移回之间都留间隔：目标程序处理慢时，避免点击落在移动之前或移回之后
    old = cursor_pos()
    fixed = act["pos"] == "fixed"
    x, y = (int(act["x"]), int(act["y"])) if fixed else old
    if fixed:
        move_abs(x, y)
        time.sleep(PRESS_S)
    down, up = MOUSE_FLAGS[act["btn"]]
    for i in range(act["count"]):
        if i:
            time.sleep(PRESS_S)
        send_mouse(down | ABS_MOVE, *_abs(x, y))  # 按下/松开事件本身也带坐标
        time.sleep(PRESS_S)
        send_mouse(up | ABS_MOVE, *_abs(x, y))
    if fixed and act["back"]:
        time.sleep(PRESS_S)
        move_abs(*old)


def do_drag(act):
    old = cursor_pos()
    down, up = MOUSE_FLAGS[act["btn"]]
    x1, y1, x2, y2 = act["x1"], act["y1"], act["x2"], act["y2"]
    move_abs(x1, y1)
    time.sleep(PRESS_S)
    send_mouse(down | ABS_MOVE, *_abs(x1, y1))
    steps = max(10, act["ms"] // 10)
    for i in range(1, steps + 1):
        time.sleep(act["ms"] / 1000 / steps)
        move_abs(round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps))
    time.sleep(PRESS_S)
    send_mouse(up | ABS_MOVE, *_abs(x2, y2))
    if act["back"]:
        time.sleep(PRESS_S)
        move_abs(*old)


def do_wheel(act):
    flags, sign = WHEEL_DIRS[act["dir"]]
    send_mouse(flags, data=120 * act["notches"] * sign)


def do_turbo(act):
    send_combo(act["mods"], act["vk"], False)
    time.sleep(PRESS_S if act["vk"] in MOUSE_BTN_VKS else 0.01)
    send_combo(act["mods"], act["vk"], True)


RUNNERS = {"click": do_click, "drag": do_drag, "wheel": do_wheel, "turbo": do_turbo}


# ---------- 键盘钩子引擎 ----------
class Engine:
    def __init__(self):
        self.mapping = {}       # 触发键 (修饰键位掩码, vk) -> 动作 dict 列表（同一个键可绑多个动作，见 norm_action）
        self.enabled = True
        self.pause = None       # 全局暂停热键 (mods, vk)
        self.on_pause = None    # 按下暂停热键时的回调
        self.on_change = None   # 连点开始/停止时的回调（刷新状态栏）
        self._capture_cb = None  # 非 None 时进入"捕获"模式：吞掉下一次按键并回调 cb((mods, vk))
        self.esc_cb = None      # 拾取坐标时，全局捕获 Esc 用于取消
        self._phys = set()      # 物理上按住的修饰键（不含本程序模拟的）
        self._released = set()  # 物理上仍按住、但系统认为已松开的修饰键（被本程序模拟松开或吞掉）
        self._cap_mod = None    # 捕获模式下单独按下的修饰键（松开时若没按别的键，就捕获它本身）
        self._active = {}       # 已触发、尚未松开的物理键 vk -> (触发键, 是否放行原按键)
        self._held = set()      # 已按下、尚未抬起的触发键（忽略按住时系统自动重复的按下事件）
        self._running = {}      # 正在连点的触发键 -> 停止信号
        self._latched = {}      # 正在"一直按住"的触发键 -> 动作（停止时要松开）
        self._hook = None
        self._outq = queue.Queue()
        self._out_thread = None
        self._wheel_seq = {}    # 滚轮触发键 vk -> 最近一次按下的序号（过期的松开消息忽略）
        self._mhook = None      # 鼠标钩子：只在有鼠标触发键或捕获时安装，避免每次移动鼠标都进 Python
        self._tid = None
        self._thread = None
        self._proc = HOOKPROC(self._on_key)  # 必须持有引用，否则会被回收
        self._mproc = HOOKPROC(self._on_mouse)

    @property
    def capture_cb(self):
        return self._capture_cb

    @capture_cb.setter
    def capture_cb(self, cb):
        self._capture_cb = cb
        self.update_hooks()

    def update_hooks(self):
        """规则或捕获状态变化后调用：让钩子线程按需装/卸鼠标钩子。"""
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_APP, 0, 0)

    def _need_mouse(self):
        keys = [*self.mapping, *([self.pause] if self.pause else [])]
        return self._capture_cb is not None or any(vk in MOUSE_TRIGGERS for _, vk in keys)

    def _sync_mouse_hook(self):
        need = self._need_mouse()
        if need and not self._mhook:
            self._mhook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mproc, kernel32.GetModuleHandleW(None), 0)
        elif not need and self._mhook:
            user32.UnhookWindowsHookEx(self._mhook)
            self._mhook = None

    def _out(self, fn, *args):
        """输出（模拟按键/鼠标、回调界面）都交给输出线程按顺序执行，钩子回调里只记录不等待：
        在鼠标钩子回调里直接 SendInput 鼠标事件会和系统互相等待，点击发不出去、钩子也卡死。
        未启动（离线测试）时直接执行。"""
        if self._out_thread is None:
            fn(*args)
        else:
            self._outq.put((fn, args))

    def _out_loop(self):
        while True:
            fn, args = self._outq.get()
            try:
                fn(*args)
            except Exception:
                pass

    def start(self):
        self._out_thread = threading.Thread(target=self._out_loop, daemon=True)
        self._out_thread.start()
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        ready.wait(3)
        if not self._hook:
            raise OSError("安装键盘钩子失败")

    def stop(self):
        self.stop_all()
        done = threading.Event()  # 等输出线程把"松开"发完再退出，避免目标键卡在按下状态
        self._out(done.set)
        done.wait(1)
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(1)

    def _run(self, ready):
        self._tid = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        self._sync_mouse_hook()
        ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_APP:
                self._sync_mouse_hook()
                continue
            if msg.message == WM_WHEEL_UP:
                if self._wheel_seq.get(msg.wParam) == msg.lParam and msg.wParam in self._active:
                    self._handle(msg.wParam, True)
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._mhook:
            user32.UnhookWindowsHookEx(self._mhook)
            self._mhook = None

    # 连点
    @property
    def running_count(self):
        return len(self._running) + len(self._latched)

    def _start_repeat(self, rk, act):
        stop = threading.Event()
        self._running[rk] = stop

        def loop():
            while not stop.is_set():
                RUNNERS[act["t"]](act)
                stop.wait(act["interval"] / 1000)
        threading.Thread(target=loop, daemon=True).start()
        self._notify()

    def _stop_repeat(self, rk):
        stop = self._running.pop(rk, None)
        if stop:
            stop.set()
            self._notify()

    def stop_all(self):
        for rk in list(self._running):
            self._stop_repeat(rk)
        for rk in list(self._latched):
            self._unlatch(rk)

    def _unlatch(self, rk):
        act = self._latched.pop(rk, None)
        if act:
            self._out(send_combo, act["mods"], act["vk"], True)
            self._notify()

    def _notify(self):
        if self.on_change:
            self._out(self.on_change)

    def _mods(self, exclude=None):
        # 锁屏/UAC 等情况下可能收不到松开事件：系统认为没按下的修饰键就剔除（自己模拟松开过的除外）
        for v in list(self._phys):
            if v not in self._released and not user32.GetAsyncKeyState(v) & 0x8000:
                self._phys.discard(v)
        m = 0
        for v in self._phys:
            if v != exclude:
                m |= MOD_BITS[v]
        return m

    def _release_mods(self, mods):
        """组合键触发时，让目标程序认为修饰键已松开，避免点击变成 Ctrl+点击之类。"""
        held = [v for v in self._phys if MOD_BITS[v] & mods]
        if held and mods & (MOD_ALT | MOD_WIN):
            self._out(send_key, VK_MASK, False)
            self._out(send_key, VK_MASK, True)
        for v in held:
            self._out(send_key, v, True)
            self._released.add(v)

    def _trigger(self, rk, act, up, first):
        if act["t"] == "key":
            if up:
                self._out(send_combo, act["mods"], act["vk"], True)
            elif first:
                self._out(send_combo, act["mods"], act["vk"], False)
            elif act["vk"] not in MOUSE_BTN_VKS:  # 鼠标按键不重复按下
                self._out(send_key, act["vk"], False)  # 按住时的自动重复（滚轮会持续滚动）
            return
        if act["t"] == "disable":
            return
        if act["t"] == "hold":
            if first and not up:
                if rk in self._latched:
                    self._unlatch(rk)
                else:
                    self._latched[rk] = act
                    self._out(send_combo, act["mods"], act["vk"], False)
                    self._notify()
            return
        mode = act.get("mode", "once")
        if up:
            self._held.discard(rk)
            if mode == "hold":
                self._stop_repeat(rk)
            return
        if rk in self._held:
            return
        self._held.add(rk)
        if mode == "once":
            threading.Thread(target=RUNNERS[act["t"]], args=(act,), daemon=True).start()
        elif mode == "hold" or rk not in self._running:
            self._start_repeat(rk, act)
        else:
            self._stop_repeat(rk)

    def _capture(self, vk, up):
        if vk in MOD_BITS:
            if not up:
                self._cap_mod = vk
                self._released.add(vk)  # 被吞掉了，系统不知道它按下了，别被 _mods 当成卡键剔除
            elif self._cap_mod == vk:  # 单独按下又松开修饰键：捕获修饰键本身
                self._cap_mod = None
                cb, self.capture_cb = self.capture_cb, None
                self._out(cb, (self._mods(), vk))
        elif not up:
            self._cap_mod = None
            cb, self.capture_cb = self.capture_cb, None
            self._out(cb, (self._mods(vk), vk))

    def _lookup(self, vk):
        mods = self._mods(vk)
        if self.pause and (mods, vk) == self.pause:
            return "pause"
        if not self.enabled:
            return None
        if (mods, vk) in self.mapping:
            return (mods, vk)
        if (0, vk) in self.mapping:  # 没有对应组合键规则时，单键规则照常生效（如 Ctrl+A → Ctrl+B）
            return (0, vk)
        return None

    def _on_key(self, nCode, wParam, lParam):
        if nCode == 0:
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if kb.dwExtraInfo != MARKER:
                vk, up = kb.vkCode, wParam in (WM_KEYUP, WM_SYSKEYUP)
                if vk in MOD_BITS:
                    if up:
                        self._phys.discard(vk)
                        self._released.discard(vk)
                    else:
                        self._phys.add(vk)
                if self.esc_cb and vk == 0x1B:
                    if not up:
                        cb, self.esc_cb = self.esc_cb, None
                        self._out(cb)
                    return 1
                if self._capture_cb:
                    self._capture(vk, up)
                    return 1
                if not self._handle(vk, up):
                    return 1
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _on_mouse(self, nCode, wParam, lParam):
        if nCode == 0 and wParam != 0x200:  # 鼠标移动直接放行
            ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if ms.dwExtraInfo != MARKER:
                hi = ms.mouseData >> 16
                if wParam in MOUSE_MSGS:
                    vk, up = MOUSE_MSGS[wParam]
                elif wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                    vk, up = (0x05 if hi == 1 else 0x06), wParam == WM_XBUTTONUP
                elif wParam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    delta = hi - 0x10000 if hi & 0x8000 else hi
                    if wParam == WM_MOUSEWHEEL:
                        vk = 0x101 if delta > 0 else 0x102
                    else:
                        vk = 0x104 if delta > 0 else 0x103
                    # 滚轮只有一个事件：当作按下，PRESS_S 后再松开（立刻松开的话游戏会漏掉这次点击）
                    if self._capture_cb:
                        self._capture(vk, False)
                        return 1
                    if vk in self._active:  # 上一格还没松开：先松开，再按
                        self._handle(vk, True)
                    keep = self._handle(vk, False)
                    if vk in self._active:
                        seq = self._wheel_seq.get(vk, 0) + 1
                        self._wheel_seq[vk] = seq
                        threading.Timer(PRESS_S, user32.PostThreadMessageW,
                                        (self._tid, WM_WHEEL_UP, vk, seq)).start()
                    if not keep:
                        return 1
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                else:
                    return user32.CallNextHookEx(None, nCode, wParam, lParam)
                if self._capture_cb:
                    if not up:
                        self._capture(vk, False)
                    return 1
                if not self._handle(vk, up):
                    return 1
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    def _handle(self, vk, up):
        """处理一个物理按键/鼠标键事件，返回 True 表示原事件放行，False 表示吞掉。"""
        first = False
        if vk not in self._active:
            if up:
                return True
            rk = self._lookup(vk)
            if rk is None:
                return True
            keep = rk != "pause" and any(a.get("keep") for a in self.mapping[rk])
            self._active[vk] = (rk, keep)
            first = True
            if not keep:  # 放行原按键时，修饰键也原样保留
                self._release_mods((self.pause if rk == "pause" else rk)[0])
        rk, keep = self._active.pop(vk) if up else self._active[vk]
        if rk == "pause":
            if first and self.on_pause:
                self._out(self.on_pause)
        else:
            for i, act in enumerate(self.mapping.get(rk, [])):
                self._trigger((rk, i), act, up, first)
        return keep


# ---------- 规则数据 ----------
# keep：触发动作的同时，原按键照常输入（鼠标动作默认保留，改键/连发默认屏蔽）
DEFAULTS = {
    "key": {"vk": None, "mods": 0, "keep": False},
    "disable": {"keep": False},
    "click": {"pos": "fixed", "x": None, "y": None, "btn": "left", "count": 1, "back": True,
              "mode": "once", "interval": 100, "keep": True},
    "drag": {"x1": None, "y1": None, "x2": None, "y2": None, "btn": "left", "ms": 300, "back": True,
             "keep": True},
    "wheel": {"dir": "down", "notches": 1, "mode": "once", "interval": 100, "keep": True},
    "turbo": {"vk": None, "mods": 0, "mode": "hold", "interval": 50, "keep": False},
    "hold": {"vk": None, "mods": 0, "keep": False},
}


def norm_action(v):
    """补全默认字段，并兼容旧配置（值为整数 vk、"禁用"用 vk=0 表示）。"""
    if isinstance(v, int):
        v = {"t": "key", "vk": v}
    if v["t"] == "key" and not v.get("vk"):
        return {"t": "disable", "keep": False}
    return {"t": v["t"], **DEFAULTS.get(v["t"], {}), **v}


def describe(act):
    t = act["t"]
    if t == "key":
        return f"按键 {combo_name(act['mods'], act['vk'])}"
    if t == "disable":
        return "（禁用）"
    if t == "click":
        where = f"({act['x']}, {act['y']})" if act["pos"] == "fixed" else "当前位置"
        return f"{'双击' if act['count'] == 2 else '点击'}{BTN_TEXT[act['btn']]} {where}"
    if t == "drag":
        return f"拖动 ({act['x1']}, {act['y1']}) → ({act['x2']}, {act['y2']})"
    if t == "wheel":
        return f"滚轮{WHEEL_TEXT[act['dir']]} {act['notches']} 格"
    if t == "hold":
        return f"一直按住 {combo_name(act['mods'], act['vk'])}"
    return f"连发 {combo_name(act['mods'], act['vk'])}"


def describe_mode(act):
    if act["t"] not in REPEATABLE:
        return ""
    if act["mode"] == "once":
        return "单次"
    return ("按住连续" if act["mode"] == "hold" else "开关") + f" · {act['interval']}ms"


# ---------- GUI ----------
# 按键映射的方式 -> (动作类型, 连点模式)
KEY_MODES = {"map": "普通：按住原按键时按住目标",
             "latch": "按一下开始一直按住，再按一下松开",
             "turbo_hold": "按住原按键时连点目标",
             "turbo_toggle": "按一下开始连点，再按一下停止"}
CLICK_MODES = {"once": "按一下点一次", "hold": "按住时连点", "toggle": "按一下开始，再按停止"}
DISABLE = "disable"  # 目标按键按钮的特殊值：禁用原按键


def is_point_rule(act):
    """固定位置的鼠标点击放在"位置点击"页，其余都在"按键映射"页。"""
    return act["t"] == "click" and act["pos"] == "fixed"


class KeyButton(ttk.Button):
    """点一下进入捕获，再按键（可带修饰键，也可以是鼠标中键/侧键/滚轮）设置；value 为 (mods, vk)、DISABLE 或 None。"""
    capturing = None  # 正在捕获的按钮（同一时间只有一个）

    def __init__(self, master, app, on_set=None, width=18, empty="点这里，然后按键"):
        super().__init__(master, width=width, command=self._start)
        self.app, self.on_set, self.empty = app, on_set, empty
        self.set(None)

    def set(self, value):
        self.value = value
        if value == DISABLE:
            text = "（禁用，什么都不做）"
        else:
            text = combo_name(*value) if value else self.empty
        self.config(text=text)

    def _start(self):
        prev = KeyButton.capturing
        if prev is self:  # 再点一次：取消
            self.cancel()
            return
        if prev:
            prev.cancel()
        KeyButton.capturing = self
        self.config(text="请按键…（再点取消）")

        def got(rk):
            def apply():
                KeyButton.capturing = None
                self.set(rk)
                if self.on_set:
                    self.on_set(rk)
            self.app.root.after(0, apply)
        self.app.engine.capture_cb = got

    def cancel(self):
        if KeyButton.capturing is self:
            KeyButton.capturing = None
            self.app.engine.capture_cb = None
            self.set(self.value)


class App:
    def __init__(self, root):
        self.root = root
        self.engine = Engine()
        self.engine.on_pause = lambda: root.after(0, self._pause_hotkey)
        self.engine.on_change = lambda: root.after(0, self.update_status)
        root.title("按键映射工具")
        try:
            root.iconbitmap(default=ICON_PATH)  # default：之后弹出的窗口也用这个图标
        except tk.TclError:
            pass
        root.geometry("640x520")
        root.minsize(560, 440)

        self.enabled_var = tk.BooleanVar(value=True)
        top = ttk.Frame(root, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        ttk.Checkbutton(top, text="启用", variable=self.enabled_var, command=self._toggle).pack(side="left")
        ttk.Label(top, text="暂停热键：").pack(side="left", padx=(16, 0))
        self.pause_btn = KeyButton(top, self, self._set_pause, width=14, empty="未设置")
        self.pause_btn.pack(side="left")
        ttk.Button(top, text="清除", width=5, command=self._clear_pause_key).pack(side="left", padx=4)
        self.beep_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="提示音", variable=self.beep_var, command=self.save).pack(side="left", padx=(8, 0))
        self._toast_win = None
        self.status = ttk.Label(top, text="")
        self.status.pack(side="right")

        # 方案：几套规则，选哪套就用哪套；启用/暂停热键/提示音是全局的
        self.profiles = {"默认": {}}
        self.current = "默认"
        prow = ttk.Frame(root, padding=(10, 0, 10, 4))
        prow.pack(fill="x")
        ttk.Label(prow, text="方案：").pack(side="left")
        self.profile_box = ttk.Combobox(prow, state="readonly", width=24)
        self.profile_box.pack(side="left")
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self.switch_profile(self.profile_box.get()))
        mb = ttk.Menubutton(prow, text="管理方案")
        menu = tk.Menu(mb, tearoff=False)
        menu.add_command(label="新建空白方案…", command=lambda: self._new_profile(copy=False))
        menu.add_command(label="复制当前方案…", command=lambda: self._new_profile(copy=True))
        menu.add_command(label="重命名…", command=self._rename_profile)
        menu.add_separator()
        menu.add_command(label="删除当前方案", command=self._delete_profile)
        mb["menu"] = menu
        mb.pack(side="left", padx=6)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self._build_key_tab(nb)
        self._build_point_tab(nb)
        ttk.Label(root, text="点按键按钮后直接按（可组合键、鼠标中键/侧键/滚轮），或从「鼠标」菜单选",
                  foreground="gray", padding=(10, 4, 10, 8)).pack(anchor="w")

        self.load()
        try:
            self.engine.start()
        except OSError as e:
            messagebox.showerror("错误", str(e))
            sys.exit(1)
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self.close)

    # ---------- 界面 ----------
    def _tree(self, parent, cols):
        tree = ttk.Treeview(parent, columns=[c for c, _, _ in cols], show="headings", selectmode="extended", height=8)
        for col, text, w in cols:
            tree.heading(col, text=text)
            tree.column(col, width=w, anchor="center")
        tree.pack(fill="both", expand=True)
        return tree

    def _buttons(self, parent, kind):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="添加", command=lambda: self._submit(kind, new=True)).pack(side="left")
        ttk.Button(bar, text="保存修改", command=lambda: self._submit(kind, new=False)).pack(side="left", padx=6)
        ttk.Button(bar, text="删除所选", command=lambda: self.remove(kind)).pack(side="left")
        ttk.Button(bar, text="清空", command=lambda: self._clear_form(kind)).pack(side="right")

    def _build_key_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  按键映射  ")
        self.key_tree = self._tree(tab, (("src", "原按键", 120), ("dst", "映射为", 150), ("mode", "方式", 200),
                                         ("keep", "原按键", 60)))
        self.key_tree.bind("<<TreeviewSelect>>", lambda e: self._load_form("key"))
        self.key_tree.bind("<Delete>", lambda e: self.remove("key"))

        form = ttk.LabelFrame(tab, text="添加 / 修改", padding=8)
        form.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(form)
        row.pack(fill="x")
        self.k_src = KeyButton(row, self, lambda rk: self._inherit_keep(rk, self.k_keep), width=16)
        self.k_src.pack(side="left")
        self._mouse_menu(row, self.k_src, MOUSE_SOURCES).pack(side="left", padx=(4, 0))
        ttk.Label(row, text="  →  ").pack(side="left")
        self.k_dst = KeyButton(row, self, width=16)
        self.k_dst.pack(side="left")
        self._mouse_menu(row, self.k_dst, MOUSE_TARGETS, disable=True).pack(side="left", padx=(4, 0))
        row = ttk.Frame(form)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="方式：").pack(side="left")
        self.k_mode = tk.StringVar(value=KEY_MODES["map"])
        cb = ttk.Combobox(row, textvariable=self.k_mode, values=list(KEY_MODES.values()), width=30, state="readonly")
        cb.pack(side="left")
        self.k_interval = tk.IntVar(value=50)
        self.k_iv_label = ttk.Label(row, text="连点间隔")
        self.k_iv_spin = ttk.Spinbox(row, from_=10, to=60000, increment=10, textvariable=self.k_interval, width=7)
        self.k_iv_ms = ttk.Label(row, text="ms")
        cb.bind("<<ComboboxSelected>>", lambda e: self._show_interval())
        self._show_interval()
        self.k_keep = tk.BooleanVar(value=False)
        self.k_keep_touched = False
        ttk.Checkbutton(form, text="同时保留原按键的功能", variable=self.k_keep,
                        command=lambda: setattr(self, "k_keep_touched", True)).pack(anchor="w", pady=(6, 0))
        self._buttons(form, "key")

    def _build_point_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  位置点击  ")
        self.pt_tree = self._tree(tab, (("src", "按键", 110), ("pos", "点击位置", 110), ("btn", "点击", 80),
                                        ("mode", "方式", 150), ("keep", "原按键", 60)))
        self.pt_tree.bind("<<TreeviewSelect>>", lambda e: self._load_form("point"))
        self.pt_tree.bind("<Delete>", lambda e: self.remove("point"))

        form = ttk.LabelFrame(tab, text="添加 / 修改", padding=8)
        form.pack(fill="x", pady=(8, 0))
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="按键：").grid(row=0, column=0, sticky="e")
        sf = ttk.Frame(form)
        sf.grid(row=0, column=1, sticky="w")
        self.p_src = KeyButton(sf, self, lambda rk: self._inherit_keep(rk, self.p_keep), width=16)
        self.p_src.pack(side="left")
        self._mouse_menu(sf, self.p_src, MOUSE_SOURCES).pack(side="left", padx=(4, 0))
        ttk.Label(form, text="位置：").grid(row=0, column=2, sticky="e", padx=(16, 0))
        pf = ttk.Frame(form)
        pf.grid(row=0, column=3, sticky="w")
        self.p_pos = None
        self.p_pos_lab = ttk.Label(pf, width=12, anchor="center", relief="sunken")
        self.p_pos_lab.pack(side="left")
        ttk.Button(pf, text="拾取", width=6, command=self._pick).pack(side="left", padx=4)

        self.p_btn = tk.StringVar(value=BTN_TEXT["left"])
        self.p_dbl = tk.BooleanVar(value=False)
        self.p_back = tk.BooleanVar(value=True)
        ttk.Label(form, text="点击：").grid(row=1, column=0, sticky="e", pady=6)
        opt = ttk.Frame(form)
        opt.grid(row=1, column=1, columnspan=3, sticky="w")
        ttk.Combobox(opt, textvariable=self.p_btn, values=list(BTN_TEXT.values()), width=5, state="readonly")\
            .pack(side="left")
        ttk.Checkbutton(opt, text="双击", variable=self.p_dbl).pack(side="left", padx=8)
        ttk.Checkbutton(opt, text="点完鼠标回到原位", variable=self.p_back).pack(side="left")

        self.p_mode = tk.StringVar(value=CLICK_MODES["once"])
        self.p_interval = tk.IntVar(value=100)
        ttk.Label(form, text="方式：").grid(row=2, column=0, sticky="e")
        opt = ttk.Frame(form)
        opt.grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Combobox(opt, textvariable=self.p_mode, values=list(CLICK_MODES.values()), width=18, state="readonly")\
            .pack(side="left")
        ttk.Label(opt, text="连点间隔").pack(side="left", padx=(12, 2))
        ttk.Spinbox(opt, from_=10, to=60000, increment=10, textvariable=self.p_interval, width=7).pack(side="left")
        ttk.Label(opt, text="ms").pack(side="left", padx=2)

        self.p_keep = tk.BooleanVar(value=True)
        self.p_keep_touched = False
        ttk.Checkbutton(form, text="同时保留原按键的功能", variable=self.p_keep,
                        command=lambda: setattr(self, "p_keep_touched", True))\
            .grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))
        bf = ttk.Frame(form)
        bf.grid(row=4, column=0, columnspan=4, sticky="ew")
        self._buttons(bf, "point")
        self._show_pos()

    def _key_mode(self):
        return {v: k for k, v in KEY_MODES.items()}[self.k_mode.get()]

    def _show_interval(self):
        """只有连点方式才显示间隔"""
        widgets = (self.k_iv_label, self.k_iv_spin, self.k_iv_ms)
        for w in widgets:
            w.pack_forget()
        if self._key_mode().startswith("turbo"):
            self.k_iv_label.pack(side="left", padx=(12, 2))
            self.k_iv_spin.pack(side="left")
            self.k_iv_ms.pack(side="left", padx=2)

    def _mouse_menu(self, parent, btn, vks, disable=False):
        """按钮旁的"鼠标"下拉菜单：直接选鼠标按键/滚轮（不用去按）。"""
        mb = ttk.Menubutton(parent, text="鼠标", width=5)
        menu = tk.Menu(mb, tearoff=False)

        def choose(v):
            btn.cancel()
            btn.set(v)
            if btn.on_set:
                btn.on_set(v)
        for vk in vks:
            menu.add_command(label=vk_name(vk), command=lambda vk=vk: choose((0, vk)))
        if disable:
            menu.add_separator()
            menu.add_command(label="（禁用，什么都不做）", command=lambda: choose(DISABLE))
        mb["menu"] = menu
        return mb

    def _show_pos(self):
        self.p_pos_lab.config(text=f"({self.p_pos[0]}, {self.p_pos[1]})" if self.p_pos else "（未设置）")

    def _pick(self):
        def done(p):
            if p:
                self.p_pos = p
                self._show_pos()
        self.pick_point("点击要自动点击的位置", done)

    def _inherit_keep(self, rk, var):
        """选的按键已有其他规则时，沿用它们的"保留原按键"设置（同一个键共用）。"""
        kind = "key" if var is self.k_keep else "point"
        touched = self.k_keep_touched if kind == "key" else self.p_keep_touched
        others = [a for i, a in enumerate(self.engine.mapping.get(rk, [])) if (rk, i) != self._editing(kind)]
        if others and not touched:
            var.set(others[0]["keep"])

    # ---------- 配置 ----------
    def load(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            def parse(m):
                return {parse_trigger(k): [norm_action(a) for a in (v if isinstance(v, list) else [v])]
                        for k, v in m.items()}
            raw = data.get("profiles") or {"默认": data.get("mapping", {})}  # 旧配置：只有一套规则
            self.profiles = {name: parse(m) for name, m in raw.items()}
            cur = data.get("current")
            self.current = cur if cur in self.profiles else next(iter(self.profiles))
            self.engine.mapping = self.profiles[self.current]
            self.enabled_var.set(data.get("enabled", True))
            self.engine.enabled = self.enabled_var.get()
            p = data.get("pause") or ([0, data["pause_vk"]] if data.get("pause_vk") else None)
            self.engine.pause = tuple(p) if p else None
            self.beep_var.set(data.get("beep", True))
        except (OSError, ValueError, KeyError):
            pass

    def save(self):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"enabled": self.engine.enabled, "pause": self.engine.pause, "beep": self.beep_var.get(),
                       "current": self.current,
                       "profiles": {name: {f"{m}:{v}": a for (m, v), a in mp.items()}
                                    for name, mp in self.profiles.items()}},
                      f, ensure_ascii=False, indent=2)

    # ---------- 方案 ----------
    def switch_profile(self, name):
        if name == self.current or name not in self.profiles:
            self.profile_box.set(self.current)
            return
        self.engine.stop_all()  # 先停掉连点、松开"一直按住"的键，避免卡键
        self.current = name
        self.engine.mapping = self.profiles[name]
        self._clear_form("key")
        self._clear_form("point")
        self.save()
        self.refresh()

    def _ask_name(self, title, initial=""):
        while True:
            name = simpledialog.askstring(title, "方案名称：", initialvalue=initial, parent=self.root)
            if name is None:
                return None
            name = name.strip()
            if not name:
                messagebox.showwarning("提示", "名称不能为空", parent=self.root)
            elif name in self.profiles and name != initial:
                messagebox.showwarning("提示", f"已经有叫「{name}」的方案了", parent=self.root)
            else:
                return name
            initial = name

    def _new_profile(self, copy):
        name = self._ask_name("复制当前方案" if copy else "新建方案",
                              f"{self.current} 副本" if copy else "")
        if not name:
            return
        self.profiles[name] = ({rk: [dict(a) for a in acts] for rk, acts in self.engine.mapping.items()}
                               if copy else {})
        self.switch_profile(name)

    def _rename_profile(self):
        old = self.current
        name = self._ask_name("重命名方案", old)
        if not name or name == old:
            return
        # 保持原顺序
        self.profiles = {(name if k == old else k): v for k, v in self.profiles.items()}
        self.current = name
        self.save()
        self.refresh()

    def _delete_profile(self):
        if len(self.profiles) <= 1:
            messagebox.showinfo("提示", "至少要保留一个方案", parent=self.root)
            return
        n = sum(map(len, self.engine.mapping.values()))
        if not messagebox.askyesno("删除方案", f"删除方案「{self.current}」及其中 {n} 条规则？此操作不能撤销。",
                                   icon="warning", parent=self.root):
            return
        old = self.current
        names = list(self.profiles)
        nxt = names[names.index(old) - 1] if names.index(old) > 0 else names[1]
        self.switch_profile(nxt)
        del self.profiles[old]
        self.save()
        self.refresh()

    def refresh(self):
        self.engine.update_hooks()
        for tree in (self.key_tree, self.pt_tree):
            tree.delete(*tree.get_children())
        for s, a in self.engine.mapping.items():
            for i, act in enumerate(a):
                keep = "保留" if act["keep"] else "屏蔽"
                if is_point_rule(act):
                    btn = ("双击" if act["count"] == 2 else "单击") + BTN_TEXT[act["btn"]]
                    mode = CLICK_MODES[act["mode"]] + ("" if act["mode"] == "once" else f" · {act['interval']}ms")
                    self.pt_tree.insert("", "end", iid=f"{s[0]}:{s[1]}:{i}", values=(
                        combo_name(*s), f"({act['x']}, {act['y']})", btn, mode, keep))
                else:
                    if act["t"] in ("key", "hold", "turbo"):
                        dst = combo_name(act["mods"], act["vk"])
                        if act["t"] == "key":
                            mode = "普通"
                        elif act["t"] == "hold":
                            mode = "按一下锁定按住"
                        else:
                            mode = ("按住时连点" if act["mode"] == "hold" else "开关连点") + f" · {act['interval']}ms"
                    else:
                        dst, mode = describe(act), describe_mode(act)
                    self.key_tree.insert("", "end", iid=f"{s[0]}:{s[1]}:{i}",
                                         values=(combo_name(*s), dst, mode, keep))
        self.pause_btn.set(self.engine.pause)
        self.profile_box["values"] = list(self.profiles)
        self.profile_box.set(self.current)
        self.update_status()

    def update_status(self):
        text = f"{sum(map(len, self.engine.mapping.values()))} 条规则 · " + ("运行中" if self.engine.enabled else "已暂停")
        if self.engine.running_count:
            text += f" · {self.engine.running_count} 个连点/按住进行中"
        self.status.config(text=text)

    def _set_enabled(self, on):
        self.engine.enabled = on
        self.enabled_var.set(on)
        if not on:
            self.engine.stop_all()
        self.save()
        self.update_status()

    def _toggle(self):
        self._set_enabled(self.enabled_var.get())

    def _pause_hotkey(self):
        on = not self.engine.enabled
        self._set_enabled(on)
        self.toast(on)
        if self.beep_var.get():  # 上升两声 = 开启，下降两声 = 暂停；Beep 会阻塞，放到线程里
            tones = (880, 1320) if on else (660, 440)
            threading.Thread(target=lambda: [winsound.Beep(f, 70) for f in tones], daemon=True).start()

    def toast(self, on):
        """在前台窗口所在屏幕的顶部中间显示「已开启/已暂停」，1 秒后淡出。
        不抢焦点、鼠标可穿透、主窗口最小化时也能显示。独占全屏的游戏里可能看不到，靠提示音。"""
        if self._toast_win is not None:
            try:
                self._toast_win.destroy()
            except tk.TclError:
                pass
        w = tk.Toplevel(self.root)
        self._toast_win = w
        w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True, "-alpha", 0.92)
        bg = "#1f9d55" if on else "#c0392b"
        tk.Label(w, text=("▶  按键映射已开启" if on else "⏸  按键映射已暂停"), bg=bg, fg="white",
                 font=("Microsoft YaHei UI", 16, "bold"), padx=26, pady=12).pack()
        w.update_idletasks()
        hwnd = user32.GetParent(w.winfo_id()) or w.winfo_id()
        # 去掉所有者（主窗口最小化时也显示）；不激活、工具窗口（不进任务栏）、鼠标穿透
        user32.SetWindowLongPtrW(hwnd, -8, 0)  # GWLP_HWNDPARENT
        ex = user32.GetWindowLongPtrW(hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongPtrW(hwnd, -20, ex | 0x08000000 | 0x80 | 0x20 | 0x80000)
        mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), 2)  # MONITOR_DEFAULTTONEAREST
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            left, top, right = mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right
        else:
            left, top, right = 0, 0, w.winfo_screenwidth()
        x = left + (right - left - w.winfo_reqwidth()) // 2
        w.geometry(f"+{x}+{top + 60}")
        w.deiconify()

        def fade(a=0.92):
            if self._toast_win is not w:
                return
            if a <= 0.05:
                w.destroy()
                self._toast_win = None
                return
            w.attributes("-alpha", a)
            w.after(30, fade, a - 0.08)
        w.after(1000, fade)

    def _set_pause(self, rk):
        self.engine.pause = rk
        self.save()
        self.refresh()

    def _clear_pause_key(self):
        self.pause_btn.cancel()
        self.engine.pause = None
        self.save()
        self.refresh()

    # ---------- 规则操作 ----------
    @staticmethod
    def _row(iid):
        """列表行 id "mods:vk:序号" -> ((mods, vk), 序号)"""
        trig, _, i = iid.rpartition(":")
        return parse_trigger(trig), int(i)

    def _tree_of(self, kind):
        return self.key_tree if kind == "key" else self.pt_tree

    def _editing(self, kind=None):
        """当前页选中的那一行 (触发键, 序号)，没有则 None。"""
        trees = [self._tree_of(kind)] if kind else [self.key_tree, self.pt_tree]
        for tree in trees:
            sel = tree.selection()
            if sel:
                return self._row(sel[0])
        return None

    def _load_form(self, kind):
        row = self._editing(kind)
        if not row:
            return
        rk, i = row
        act = self.engine.mapping[rk][i]
        if kind == "key":
            self.k_src.set(rk)
            mode = "map"
            if act["t"] in ("key", "hold", "turbo"):
                self.k_dst.set((act["mods"], act["vk"]))
                if act["t"] == "hold":
                    mode = "latch"
                elif act["t"] == "turbo":
                    mode = "turbo_hold" if act["mode"] == "hold" else "turbo_toggle"
                    self.k_interval.set(act["interval"])
            elif act["t"] == "disable":
                self.k_dst.set(DISABLE)
            else:
                self.k_dst.set(None)  # 旧版的拖动/滚轮等动作：只能删除或改成按键映射
            self.k_mode.set(KEY_MODES[mode])
            self._show_interval()
            self.k_keep.set(act["keep"])
            self.k_keep_touched = True
        else:
            self.p_src.set(rk)
            self.p_pos = (act["x"], act["y"])
            self._show_pos()
            self.p_btn.set(BTN_TEXT[act["btn"]])
            self.p_dbl.set(act["count"] == 2)
            self.p_back.set(act["back"])
            self.p_mode.set(CLICK_MODES[act["mode"]])
            self.p_interval.set(act["interval"])
            self.p_keep.set(act["keep"])
            self.p_keep_touched = True

    def _clear_form(self, kind):
        self._tree_of(kind).selection_set(())
        if kind == "key":
            self.k_src.set(None)
            self.k_dst.set(None)
            self.k_mode.set(KEY_MODES["map"])
            self._show_interval()
            self.k_keep.set(False)
            self.k_keep_touched = False
        else:
            self.p_src.set(None)
            self.p_pos = None
            self._show_pos()
            self.p_keep.set(True)
            self.p_keep_touched = False

    def _form_action(self, kind):
        """从表单生成 (触发键, 动作)；缺内容时提示并返回 None。"""
        if kind == "key":
            src, dst = self.k_src.value, self.k_dst.value
            if not src or not dst:
                messagebox.showwarning("提示", "请先设置原按键和目标按键", parent=self.root)
                return None
            if dst == DISABLE:
                return src, {"t": "disable", "keep": False}
            mode = self._key_mode()
            base = {"mods": dst[0], "vk": dst[1], "keep": self.k_keep.get()}
            if mode == "map":
                return src, {"t": "key", **base}
            if mode == "latch":
                return src, {"t": "hold", **base}
            try:
                interval = max(10, int(self.k_interval.get()))
            except (tk.TclError, ValueError):
                messagebox.showwarning("提示", "连点间隔要填数字（毫秒）", parent=self.root)
                return None
            return src, {"t": "turbo", **base, "mode": "hold" if mode == "turbo_hold" else "toggle",
                         "interval": interval}
        src = self.p_src.value
        if not src or not self.p_pos:
            messagebox.showwarning("提示", "请先设置按键和点击位置", parent=self.root)
            return None
        try:
            interval = max(10, int(self.p_interval.get()))
        except (tk.TclError, ValueError):
            messagebox.showwarning("提示", "连点间隔要填数字（毫秒）", parent=self.root)
            return None
        mode = {v: k for k, v in CLICK_MODES.items()}[self.p_mode.get()]
        btn = {v: k for k, v in BTN_TEXT.items()}[self.p_btn.get()]
        return src, norm_action({"t": "click", "pos": "fixed", "x": self.p_pos[0], "y": self.p_pos[1], "btn": btn,
                                 "count": 2 if self.p_dbl.get() else 1, "back": self.p_back.get(), "mode": mode,
                                 "interval": interval, "keep": self.p_keep.get()})

    def _submit(self, kind, new):
        """new=True 添加一条；False 用表单内容替换当前选中的那条。"""
        old = None if new else self._editing(kind)
        if not new and not old:
            messagebox.showwarning("提示", "请先在列表里选中要修改的规则", parent=self.root)
            return
        res = self._form_action(kind)
        if not res:
            return
        src, act = res
        KeyButton.capturing and KeyButton.capturing.cancel()
        self.engine.stop_all()
        m = self.engine.mapping
        pos = None
        if old:
            del m[old[0]][old[1]]
            if old[0] == src:
                pos = old[1]  # 原地修改，保持顺序
            elif not m[old[0]]:
                del m[old[0]]
        lst = m.setdefault(src, [])
        lst.insert(len(lst) if pos is None else pos, act)
        if act["t"] != "disable":
            for a in lst:  # 同一个键的规则共用"保留原按键"设置
                if a["t"] != "disable":
                    a["keep"] = act["keep"]
        self.save()
        self.refresh()
        iid = f"{src[0]}:{src[1]}:{lst.index(act)}"
        tree = self._tree_of(kind)
        if tree.exists(iid):
            tree.selection_set(iid)
            tree.see(iid)

    def remove(self, kind):
        rows = [self._row(i) for i in self._tree_of(kind).selection()]
        if not rows:
            return
        self.engine.stop_all()
        for rk, i in sorted(rows, key=lambda r: -r[1]):
            del self.engine.mapping[rk][i]
            if not self.engine.mapping[rk]:
                del self.engine.mapping[rk]
        self.save()
        self.refresh()

    def pick_point(self, tip, done):
        """全屏遮罩拾取一个屏幕坐标，done(point) 回调；Esc 取消时 point 为 None。"""
        self.root.withdraw()
        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True, "-alpha", 0.35)
        ov.configure(bg="black", cursor="crosshair")
        gm = user32.GetSystemMetrics
        vx, vy = gm(76), gm(77)
        ov.geometry(f"{gm(78)}x{gm(79)}+{vx}+{vy}")
        tk.Label(ov, text=f"{tip}（Esc 取消）", bg="black", fg="white",
                 font=("Microsoft YaHei UI", 18)).place(relx=0.5, rely=0.4, anchor="center")
        coord = tk.Label(ov, text="", bg="yellow", fg="black", font=("Consolas", 12))

        def move(e):
            coord.config(text=f" {e.x_root}, {e.y_root} ")
            coord.place(x=e.x_root - vx + 18, y=e.y_root - vy + 18)

        def finish(point=None):
            self.engine.esc_cb = None
            ov.grab_release()
            ov.destroy()
            if point:
                self.flash(point)
            self.root.deiconify()
            done(point)

        ov.bind("<Motion>", move)
        ov.bind("<Button-1>", lambda e: finish((e.x_root, e.y_root)))
        self.engine.esc_cb = lambda: self.root.after(0, finish)
        ov.focus_force()
        ov.grab_set()

    def flash(self, point):
        """在拾取到的位置闪一个红圈作为确认。"""
        m = tk.Toplevel(self.root)
        m.overrideredirect(True)
        m.attributes("-topmost", True, "-transparentcolor", "white")
        m.geometry(f"40x40+{point[0] - 20}+{point[1] - 20}")
        c = tk.Canvas(m, width=40, height=40, bg="white", highlightthickness=0)
        c.pack()
        c.create_oval(4, 4, 36, 36, outline="red", width=3)
        c.create_line(20, 0, 20, 40, fill="red")
        c.create_line(0, 20, 40, 20, fill="red")
        m.after(1200, m.destroy)

    def close(self):
        self.engine.stop()
        self.root.destroy()


def main():
    try:  # 让坐标为物理像素，与 SetCursorPos 一致
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
