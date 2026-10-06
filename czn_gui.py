import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, font as tkfont

from PIL import Image, ImageTk

import czn_local_cdn as engine

APP_TITLE = "CZN Local CDN Responder"

# 可写文件（ini）在 exe/脚本旁；只读资源在 PyInstaller 解包目录
FROZEN = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(
    sys.executable if FROZEN else __file__))
BASE_DIR = getattr(sys, "_MEIPASS", APP_DIR)
ASSETS = os.path.join(BASE_DIR, "assets")
CONFIG = os.path.join(APP_DIR, "czn_cdn.ini")
WIN_W, WIN_H = 1280, 720
BG_FILES = [os.path.join(ASSETS, "bg_1.png"), os.path.join(ASSETS, "bg_2.png")]

# 左列布局
X0 = 48
HINT_Y = 56
PATH_Y = 90
PICK_DX = 520
LOG_TOP = 136
LOG_LINE = 25
LOG_LINES = 10
STATUS_Y = 402
BTN_TOP = 452
BTN_STEP = 62

# bg_1 深蓝底白字 / bg_2 浅底蓝字
PALETTES = [
    dict(text="#f7fbff", dim="#e2efff", bright="#ffffff",
         hover="#ffe9a0", line="#9cc0ee"),
    dict(text="#123c78", dim="#47679c", bright="#0b57b8",
         hover="#c76f00", line="#7fa3cc"),
]

F_HINT = ("Microsoft YaHei UI", 11)
F_TEXT = ("Microsoft YaHei UI", 13)
F_LOG = ("Microsoft YaHei UI", 12)
F_BTN = ("Microsoft YaHei UI", 15, "bold")
BTN_LABELS = ["▶  启动服务", "■  停止并还原", "◈  切换背景", "✕  退出"]


def is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def read_saved_gameres():
    try:
        for line in open(CONFIG, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.lower().startswith("gameres"):
                _, _, v = line.partition("=")
                v = v.strip().strip('"')
                if v:
                    return v
    except Exception:
        pass
    return None


def save_gameres(path):
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write("[game]\ngameres = %s\n" % path)


def descend_to_gameres(picked):
    """定位 gameres 层（直接含 manifest.ssra）；返回 (路径, 相对说明)。"""
    p = os.path.normpath(picked)
    if os.path.exists(os.path.join(p, "manifest.ssra")):
        return p, None
    for sub in (os.path.join("appdata", "cznlive", "gameres"), "gameres"):
        cand = os.path.join(p, sub)
        if os.path.exists(os.path.join(cand, "manifest.ssra")):
            return cand, sub
    for root, dirs, files in os.walk(p):    # 兜底下钻，最多 4 层
        rel = os.path.relpath(root, p)
        if "manifest.ssra" in (f.lower() for f in files):
            return root, ("" if rel == "." else rel)
        if rel != "." and rel.count(os.sep) >= 3:
            dirs[:] = []
    return None, None


def push_log_line(entries, base):
    """连续重复行合并计数；条目 = [首现时间, 内容, 次数]。"""
    if entries and entries[-1][1] == base:
        entries[-1][2] += 1
        return False
    entries.append([time.strftime("%H:%M:%S"), base, 1])
    return True


def light_titlebar(window):
    """强制浅色标题栏（白底黑字）。"""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        for attr in (20, 19):    # DWMWA_USE_IMMERSIVE_DARK_MODE
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(ctypes.c_int(0)), 4) == 0:
                break
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("%dx%d+%d+%d" % (WIN_W, WIN_H, 80, 40))
        self.resizable(False, False)

        self.bg_index = 0
        self.pal = PALETTES[0]
        self.server = None
        self.server_thread = None
        self.busy = False           # 工作线程执行中，忽略重复点击
        self._quit = False
        self.log_entries = deque(maxlen=400)
        self.log_scroll = 0         # 相对底部的滚动偏移（行）
        self.log_items = []
        self.ui_queue = queue.Queue()
        self.tinted = []            # (canvas item, 配色角色)

        engine.detect_gameres()
        saved = read_saved_gameres()
        if saved:
            engine.set_gameres(saved)
        engine.log_callback = self.push_log

        self._build()
        self._render_path()
        self._render_status()
        self._drain_queue()
        self.protocol("WM_DELETE_WINDOW", self._exit_app)

        self.push_log("[ui] 就绪（管理员权限: %s）" % ("是" if is_admin() else "否"))
        if engine.GAMERES:
            self.push_log("[cfg] 已加载 gameres: " + engine.GAMERES)
        else:
            self.push_log("[cfg] 尚未选择游戏目录 —— 点 [选择文件夹]，选到 bin 即可")

    # ------------------------------------------------------------ 界面
    def _build(self):
        self.canvas = tk.Canvas(self, width=WIN_W, height=WIN_H,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._bg_raw = [Image.open(f).convert("RGB").resize((WIN_W, WIN_H))
                        for f in BG_FILES]
        self._bg_photo = ImageTk.PhotoImage(self._bg_raw[0])
        self.bg_item = self.canvas.create_image(0, 0, image=self._bg_photo,
                                                anchor="nw")

        self._reg(self.canvas.create_text(
            X0, HINT_Y, anchor="nw", fill=self.pal["dim"], font=F_HINT,
            text="选择游戏 bin 目录即可，会自动向下定位 gameres 清单"), "dim")

        # 路径按像素截断，按钮位置固定，不重叠
        self.path_item = self._reg(self.canvas.create_text(
            X0, PATH_Y, anchor="nw", fill=self.pal["text"], font=F_TEXT,
            text=""), "text")
        self.pick_item = self.canvas.create_text(
            X0 + PICK_DX, PATH_Y, anchor="nw", fill=self.pal["bright"],
            font=F_TEXT, text="[选择文件夹]")
        self.canvas.tag_bind(self.pick_item, "<Button-1>",
                             lambda e: self._pick_folder())
        self.canvas.tag_bind(self.pick_item, "<Enter>",
                             lambda e: (self.canvas.itemconfig(
                                 self.pick_item, fill=self.pal["hover"]),
                                 self.canvas.config(cursor="hand2")))
        self.canvas.tag_bind(self.pick_item, "<Leave>",
                             lambda e: (self.canvas.itemconfig(
                                 self.pick_item, fill=self.pal["bright"]),
                                 self.canvas.config(cursor="")))

        self._reg(self.canvas.create_line(X0, LOG_TOP - 10, X0 + 620, LOG_TOP - 10,
                                          fill=self.pal["line"], width=1), "line")

        self.status_item = self._reg(self.canvas.create_text(
            X0, STATUS_Y, anchor="nw", fill=self.pal["text"], font=F_TEXT,
            text=""), "text")

        for i, text in enumerate(BTN_LABELS):
            tag = "btn_%d" % i
            item = self.canvas.create_text(
                X0, BTN_TOP + i * BTN_STEP, anchor="nw", fill=self.pal["text"],
                font=F_BTN, text=text, tags=(tag,))
            self._reg(item, "text")
            self.canvas.tag_bind(tag, "<Button-1>",
                                 lambda e, i=i: self._on_button(i))
            self.canvas.tag_bind(tag, "<Enter>",
                                 lambda e, t=tag: (
                                     self.canvas.itemconfig(t, fill=self.pal["hover"]),
                                     self.canvas.config(cursor="hand2")))
            self.canvas.tag_bind(tag, "<Leave>",
                                 lambda e, t=tag: (
                                     self.canvas.itemconfig(t, fill=self.pal["text"]),
                                     self.canvas.config(cursor="")))

        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", self._on_wheel)
        self.canvas.bind("<Button-5>", self._on_wheel)

    def _reg(self, item, role):
        self.tinted.append((item, role))
        return item

    # ------------------------------------------------------------ 日志
    def push_log(self, msg):
        self.ui_queue.put(msg)

    def _drain_queue(self):
        try:
            while True:
                push_log_line(self.log_entries, str(self.ui_queue.get_nowait()))
                self._render_log()
        except queue.Empty:
            pass
        self._render_status()
        if self._quit:
            self.destroy()
            return
        self.after(120, self._drain_queue)

    def _render_log(self):
        for item in self.log_items:
            self.canvas.delete(item)
        self.log_items.clear()
        visible = min(LOG_LINES, len(self.log_entries))
        for i in range(visible):
            idx = len(self.log_entries) - visible + i + self.log_scroll
            if 0 <= idx < len(self.log_entries):
                stamp, base, count = self.log_entries[idx]
                y = LOG_TOP + i * LOG_LINE
                item = self.canvas.create_text(
                    X0, y, anchor="nw", fill=self.pal["text"], font=F_LOG,
                    text=("[%s] %s" % (stamp, base))[:110]
                         + ("  ×%d" % count if count > 1 else ""),
                    tags=("logline",))
                self.log_items.append(item)

    def _on_wheel(self, e):
        if LOG_TOP - 10 <= e.y <= LOG_TOP + LOG_LINES * LOG_LINE:
            d = getattr(e, "delta", 0)
            step = -3 if d > 0 else (3 if d < 0 else
                                     (-3 if e.num == 4 else 3))
            max_off = max(0, len(self.log_entries) - LOG_LINES)
            self.log_scroll = max(0, min(self.log_scroll + step, max_off))
            self._render_log()

    # ------------------------------------------------------------ 渲染
    def _fit_px(self, s, font, maxw):
        f = tkfont.Font(font=font)
        if f.measure(s) <= maxw:
            return s
        while s and f.measure("…" + s) > maxw:
            s = s[1:]
        return "…" + s

    def _render_path(self):
        if engine.GAMERES:
            body = self._fit_px("路径：" + engine.GAMERES, F_TEXT, PICK_DX - 24)
        else:
            body = "路径：（未选择）"
        self.canvas.itemconfig(self.path_item, text=body)

    def _render_status(self):
        if self.busy:
            s = "处理中…"
        elif self.server:
            s = "运行中（端口 %d）—— 启动游戏即可" % engine.PORT
        elif engine.GAMERES:
            s = "就绪，可启动服务"
        else:
            s = "未选择游戏目录"
        self.canvas.itemconfig(self.status_item, text="状态：" + s)

    # ------------------------------------------------------------ 动作
    def _pick_folder(self):
        initial = (engine.GAMERES or read_saved_gameres()
                   or os.path.expanduser("~"))
        picked = filedialog.askdirectory(
            title="选择游戏目录（bin 或其上层均可，自动定位 gameres）",
            initialdir=initial if os.path.isdir(initial) else os.path.expanduser("~"))
        if not picked:
            return
        real, rel = descend_to_gameres(picked)
        if not real:
            self.push_log("[cfg] 所选目录及其下层（≤4 级）都没有 manifest.ssra"
                           " —— 请确认选的是游戏安装目录")
            return
        engine.set_gameres(real)
        save_gameres(real)
        self._render_path()
        self._render_status()
        if rel is None:
            self.push_log("[cfg] gameres 已设置（所选目录即 gameres 层）: " + real)
        else:
            self.push_log("[cfg] gameres 已定位（%s）: %s" % (rel, real))

    def _on_button(self, i):
        if i == 0:
            self._start()
        elif i == 1:
            self._stop_restore()
        elif i == 2:
            self._switch_bg()
        else:
            self._exit_app()

    def _start(self):
        if self.busy:
            self.push_log("[ui] 正在处理上一步，请稍候")
            return
        if self.server:
            self.push_log("[srv] 已在运行中")
            return
        if not engine.GAMERES or not engine.manifest_path():
            self.push_log("[srv] 请先选择游戏目录（bin 或其上层均可）")
            return
        size = engine.manifest_size()
        if size <= 0:
            self.push_log("[srv] manifest.ssra 不可读: " + engine.manifest_path())
            return
        self.busy = True
        self.push_log("[srv] ── 启动 ──")

        def work():
            try:
                self.push_log("[srv] gameres: %s" % engine.GAMERES)
                self.push_log("[srv] 清单: %s（%s 字节）  ETag: %s"
                              % (os.path.basename(engine.manifest_path()),
                                 format(size, ","), engine.current_etag()))
                if engine.hosts_status():
                    self.push_log("[hosts] 条目已存在，跳过")
                else:
                    engine.hosts_install()
                if engine.cert_in_store():
                    self.push_log("[cert] CA 已在受信任根，跳过安装")
                elif not engine.cert_install():
                    self.push_log("[!] CA 未装入 —— 游戏若报 TLS/网络错误多半因此"
                                   "（请以管理员运行本工具）")
                srv = engine.make_server()
                if srv is None:
                    self.push_log("[srv] ✘ 端口 443 绑定失败"
                                   "（是否已有实例或其他程序占用？）")
                    return
                self.server = srv
                self.server_thread = threading.Thread(
                    target=srv.serve_forever, daemon=True, name="responder")
                self.server_thread.start()
                self.push_log("[srv] ✔ 应答器运行中 —— 现在启动游戏即可")
            except Exception as e:
                self.push_log("[!] 启动失败: %r" % e)
            finally:
                self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def _teardown(self):
        """停止服务 + 还原 hosts + 移除自己装入的 CA。"""
        srv, th = self.server, self.server_thread
        self.server = None
        if srv is not None:
            # 仅服务线程存活时才 shutdown，否则死等永不置位的事件
            if th is not None and th.is_alive():
                srv.shutdown()
            srv.server_close()
            self.push_log("[srv] 监听已关闭")
        if engine.hosts_status():
            engine.hosts_uninstall()
        else:
            self.push_log("[hosts] 无本工具条目，跳过")
        if engine.cert_in_store():
            engine.cert_uninstall()
        else:
            self.push_log("[cert] 受信任根中无本工具证书，跳过")

    def _stop_restore(self):
        if self.busy:
            self.push_log("[ui] 正在处理上一步，请稍候")
            return
        self.busy = True
        self.push_log("[srv] ── 停止并还原 ──")

        def work():
            try:
                self._teardown()
                self.push_log("[srv] ✔ 已全部还原（hosts / 证书 / 服务）")
            except Exception as e:
                self.push_log("[!] 还原过程出错: %r" % e)
            finally:
                self.busy = False

        threading.Thread(target=work, daemon=True).start()

    def _switch_bg(self):
        self.bg_index = (self.bg_index + 1) % len(self._bg_raw)
        self.pal = PALETTES[self.bg_index]
        self._bg_photo = ImageTk.PhotoImage(self._bg_raw[self.bg_index])
        self.canvas.itemconfig(self.bg_item, image=self._bg_photo)
        for item, role in self.tinted:
            self.canvas.itemconfig(item, fill=self.pal[role])
        self._render_log()
        self.push_log("[ui] 背景 #%d（文字配色已随背景切换）" % (self.bg_index + 1))

    def _exit_app(self):
        if self.busy:
            self.push_log("[ui] 正在处理上一步，稍候再退出")
            return
        self.busy = True
        self._quit = True

        def work():
            try:
                self._teardown()    # 退出前完整还原，不留系统残留
                self.push_log("[srv] 已还原，正在退出…")
            except Exception as e:
                self.push_log("[!] 退出清理出错: %r" % e)
            finally:
                self.busy = False   # _drain_queue 检测 _quit 后销毁窗口

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    if not is_admin():
        import ctypes
        # 冻结态直接提权 exe 本身；脚本态把脚本路径作为参数传给 python
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            None if FROZEN else " ".join('"%s"' % a for a in sys.argv),
            None, 1)
        sys.exit(0)
    app = App()
    light_titlebar(app)
    app.mainloop()
