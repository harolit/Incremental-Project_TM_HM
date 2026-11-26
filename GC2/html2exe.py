"""
HTML2EXE v2 - package an HTML game (folder or single .html file) for:

  * Windows  -> .exe   (Python launcher + pywebview, bundled with PyInstaller)
  * Linux    -> .tar.gz (Electron runtime + your game; built from ANY OS, no WSL needed)
  * Android  -> .apk   (Capacitor + Gradle; needs Node.js, JDK 21 and the Android SDK)

Start it with Start_HTML2EXE.bat (Windows) or:  python html2exe.py
"""

import glob
import importlib.util
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

IS_WIN = os.name == "nt"

# ===========================================================================
# WINDOWS launcher (goes INSIDE the exe)
# ===========================================================================
RUNNER_SOURCE = r'''
import http.server
import json
import os
import socketserver
import sys
import threading
import zlib
from urllib.parse import quote

import webview


def base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


BASE = base_dir()
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)
GAME_DIR = os.path.join(BASE, "game")


class Handler(http.server.SimpleHTTPRequestHandler):
    # Windows registry sometimes maps .js to text/plain, which breaks modules
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".json": "application/json",
        ".wasm": "application/wasm",
        ".svg": "image/svg+xml",
        ".css": "text/css",
        ".html": "text/html",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=GAME_DIR, **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_server():
    # Stable port per game so localStorage / saves keep working between runs
    preferred = 20000 + zlib.crc32(CFG["name"].encode("utf-8")) % 20000
    srv = None
    for port in (preferred, 0):
        try:
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def main():
    port = start_server()
    url = "http://127.0.0.1:%d/%s" % (port, quote(CFG["entry"]))
    webview.create_window(
        CFG["name"],
        url,
        width=CFG["width"],
        height=CFG["height"],
        resizable=CFG["resizable"],
        fullscreen=CFG["fullscreen"],
    )
    storage = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")), CFG["safe_name"]
    )
    os.makedirs(storage, exist_ok=True)
    webview.start(private_mode=False, storage_path=storage, debug=CFG["debug"])


if __name__ == "__main__":
    main()
'''

# ===========================================================================
# LINUX: Electron main process (goes into resources/app/main.js)
# ===========================================================================
ELECTRON_MAIN_JS = r'''
const { app, BrowserWindow, protocol, net, shell } = require('electron');
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const cfg = require('./config.json');
const GAME_DIR = path.join(__dirname, 'game');

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'app',
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true },
  },
]);

// The bundled chrome-sandbox helper cannot be setuid-root from a plain archive,
// so the sandbox is turned off. The window only ever loads the bundled game.
if (cfg.noSandbox) app.commandLine.appendSwitch('no-sandbox');
app.commandLine.appendSwitch('autoplay-policy', 'no-user-gesture-required');

// Linux file systems are case-sensitive, games made on Windows often are not.
function resolveInsensitive(rel) {
  let current = GAME_DIR;
  const parts = rel.split('/').filter(Boolean);
  for (const part of parts) {
    const exact = path.join(current, part);
    if (fs.existsSync(exact)) { current = exact; continue; }
    let match = null;
    try {
      match = fs.readdirSync(current).find((n) => n.toLowerCase() === part.toLowerCase());
    } catch (e) { /* ignore */ }
    if (!match) return null;
    current = path.join(current, match);
  }
  return current;
}

function createWindow() {
  const opts = {
    width: cfg.width,
    height: cfg.height,
    resizable: cfg.resizable,
    fullscreen: cfg.fullscreen,
    title: cfg.name,
    autoHideMenuBar: true,
    backgroundColor: '#000000',
    webPreferences: { contextIsolation: true, nodeIntegration: false, devTools: !!cfg.debug },
  };
  if (cfg.hasIcon) opts.icon = path.join(__dirname, 'icon.png');
  const win = new BrowserWindow(opts);
  win.removeMenu();

  win.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    if (input.key === 'F11') win.setFullScreen(!win.isFullScreen());
    if (input.key === 'F12' && cfg.debug) win.webContents.toggleDevTools();
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('app://')) return { action: 'allow' };
    shell.openExternal(url);
    return { action: 'deny' };
  });

  win.loadURL('app://game/' + cfg.entry.split('/').map(encodeURIComponent).join('/'));
}

app.whenReady().then(() => {
  protocol.handle('app', (request) => {
    let rel = decodeURIComponent(new URL(request.url).pathname);
    if (rel.endsWith('/')) rel += 'index.html';
    let file = resolveInsensitive(rel);
    if (!file) return new Response('Not found', { status: 404 });
    file = path.normalize(file);
    if (file !== GAME_DIR && !file.startsWith(GAME_DIR + path.sep)) {
      return new Response('Forbidden', { status: 403 });
    }
    try {
      if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
    } catch (e) { /* ignore */ }
    return net.fetch(pathToFileURL(file).toString());
  });
  createWindow();
});

app.on('window-all-closed', () => app.quit());
'''

# ===========================================================================
# ANDROID: replacement MainActivity (Capacitor project)
# ===========================================================================
ANDROID_ACTIVITY_JAVA = r'''package __PKG__;

import android.os.Bundle;
import android.view.WindowManager;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsControllerCompat;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    private static final boolean IMMERSIVE = __IMMERSIVE__;
    private static final boolean KEEP_ON = __KEEP_ON__;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (KEEP_ON) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (IMMERSIVE && hasFocus) {
            WindowInsetsControllerCompat c =
                WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
            c.setSystemBarsBehavior(WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            c.hide(WindowInsetsCompat.Type.systemBars());
        }
    }
}
'''


# ===========================================================================
# Pure helpers (no GUI, easy to test)
# ===========================================================================
def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .")
    return cleaned or "Game"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "game"


def default_package(name: str) -> str:
    core = re.sub(r"[^a-z0-9]", "", name.lower())
    return "com.html2exe.g" + (core or "ame")


PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


def patch_manifest(text: str, orientation: str) -> str:
    """Add android:screenOrientation to the MainActivity element."""
    value = {"Landscape": "sensorLandscape", "Portrait": "portrait"}.get(orientation)
    if not value:
        return text
    pattern = re.compile(r'(<activity\b[^>]*?android:name="[^"]*MainActivity")')
    if "android:screenOrientation" in text:
        return text
    return pattern.sub(r'\1 android:screenOrientation="%s"' % value, text, count=1)


def make_activity(pkg: str, immersive: bool, keep_on: bool) -> str:
    return (
        ANDROID_ACTIVITY_JAVA.replace("__PKG__", pkg)
        .replace("__IMMERSIVE__", "true" if immersive else "false")
        .replace("__KEEP_ON__", "true" if keep_on else "false")
    )


def _tar_bytes(tar, arcname, data, mode=0o644):
    ti = tarfile.TarInfo(arcname)
    ti.size = len(data)
    ti.mode = mode
    ti.mtime = int(time.time())
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    tar.addfile(ti, io.BytesIO(data))


def _tar_dir(tar, arcname):
    ti = tarfile.TarInfo(arcname.rstrip("/"))
    ti.type = tarfile.DIRTYPE
    ti.mode = 0o755
    ti.mtime = int(time.time())
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    tar.addfile(ti)


def package_linux_tar(electron_zip, out_path, safe, game_dir, app_files):
    """
    Build <out_path> (.tar.gz) = Electron linux runtime + your app.
    app_files: list of (path_relative_to_resources_app, bytes)
    Written with correct Unix permissions even when run on Windows.
    """
    root = safe
    app_root = f"{root}/resources/app"
    seen_dirs = set()

    def ensure_dir(tar, arc):
        parts = arc.split("/")
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d and d not in seen_dirs:
                seen_dirs.add(d)
                _tar_dir(tar, d)

    with tarfile.open(out_path, "w:gz") as tar, zipfile.ZipFile(electron_zip) as z:
        ensure_dir(tar, root)
        for zi in z.infolist():
            name = zi.filename
            if name == "resources/default_app.asar":
                continue
            if name.endswith("/"):
                ensure_dir(tar, f"{root}/{name}")
                continue
            arc = f"{root}/{safe if name == 'electron' else name}"
            ensure_dir(tar, os.path.dirname(arc))
            unix_mode = (zi.external_attr >> 16) & 0o777
            is_exec = name in ("electron", "chrome-sandbox", "chrome_crashpad_handler") or bool(
                unix_mode & 0o111
            )
            with z.open(zi) as fh:
                _tar_bytes(tar, arc, fh.read(), 0o755 if is_exec else 0o644)

        ensure_dir(tar, app_root)
        for rel, data in app_files:
            arc = f"{app_root}/{rel}"
            ensure_dir(tar, os.path.dirname(arc))
            _tar_bytes(tar, arc, data)

        for dirpath, dirnames, filenames in os.walk(game_dir):
            rel_dir = os.path.relpath(dirpath, game_dir)
            arc_dir = f"{app_root}/game" + ("" if rel_dir == "." else "/" + rel_dir.replace(os.sep, "/"))
            ensure_dir(tar, arc_dir)
            for fn in filenames:
                with open(os.path.join(dirpath, fn), "rb") as fh:
                    _tar_bytes(tar, f"{arc_dir}/{fn}", fh.read())


def detect_sdk() -> str:
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        p = os.environ.get(var)
        if p and os.path.isdir(p):
            return p
    for c in (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
        os.path.expanduser("~/Android/Sdk"),
        os.path.expanduser("~/Library/Android/sdk"),
    ):
        if c and os.path.isdir(c):
            return c
    return ""


def _java_exe(home):
    return os.path.join(home, "bin", "java.exe" if IS_WIN else "java")


def detect_jdk() -> str:
    """Prefer a JDK Gradle can run on (17-23); fall back to any JDK found."""
    cands = []
    p = os.environ.get("JAVA_HOME")
    if p:
        cands.append(p)
    for pattern in (
        r"C:\Program Files\Eclipse Adoptium\jdk-*",
        r"C:\Program Files\Java\jdk-*",
        r"C:\Program Files\Microsoft\jdk-*",
        r"C:\Program Files\Amazon Corretto\jdk*",
        r"C:\Program Files\Zulu\zulu-*",
        "/usr/lib/jvm/*",
    ):
        cands += sorted(glob.glob(pattern), reverse=True)
    cands += [
        r"C:\Program Files\Android\Android Studio\jbr",
        r"C:\Program Files\Android\Android Studio\jre",
    ]
    cands += sorted(glob.glob(r"C:\Program Files\Android\Android Studio*\jbr"), reverse=True)

    fallback = ""
    for c in cands:
        if not os.path.isfile(_java_exe(c)):
            continue
        major = java_major(_java_exe(c))
        if major is not None and 17 <= major <= 23:
            return c
        fallback = fallback or c
    return fallback


def java_major(java_path):
    try:
        out = subprocess.run(
            [java_path, "-version"], capture_output=True, text=True, timeout=30
        )
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out.stdout + out.stderr)
        if m:
            major = int(m.group(1))
            return int(m.group(2)) if major == 1 and m.group(2) else major
    except Exception:  # noqa: BLE001
        pass
    return None


# ===========================================================================
# GUI
# ===========================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HTML2EXE - package HTML games for Windows, Linux and Android")
        self.geometry("760x860")
        self.minsize(720, 780)

        self.mode = tk.StringVar(value="folder")
        self.source = tk.StringVar()
        self.entry = tk.StringVar()
        self.name = tk.StringVar()
        self.width = tk.StringVar(value="1280")
        self.height = tk.StringVar(value="720")
        self.fullscreen = tk.BooleanVar(value=False)
        self.resizable = tk.BooleanVar(value=True)
        self.debug = tk.BooleanVar(value=False)
        self.icon = tk.StringVar()
        self.output = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Documents", "HTML2EXE_Output")
        )

        self.t_windows = tk.BooleanVar(value=IS_WIN)
        self.t_linux = tk.BooleanVar(value=False)
        self.t_android = tk.BooleanVar(value=False)

        self.onefile = tk.BooleanVar(value=True)
        self.linux_arch = tk.StringVar(value="x64")
        self.electron_version = tk.StringVar(value="33.0.0")
        self.orientation = tk.StringVar(value="Auto")
        self.immersive = tk.BooleanVar(value=True)
        self.keep_on = tk.BooleanVar(value=True)
        self.pkg = tk.StringVar()
        self.sdk = tk.StringVar(value=detect_sdk())
        self.jdk = tk.StringVar(value=detect_jdk())
        self.cap_version = tk.StringVar(value="7")

        self.log_queue = queue.Queue()
        self.building = False
        self._build_ui()
        self.after(100, self._drain_log)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)

        def heading(text, r):
            ttk.Label(root, text=text, font=("Segoe UI", 10, "bold")).grid(
                row=r, column=0, columnspan=3, sticky="w", **pad
            )

        r = 0
        heading("1. Game source", r)
        r += 1
        ttk.Radiobutton(root, text="Game folder (with assets)", variable=self.mode,
                        value="folder").grid(row=r, column=0, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(root, text="Single .html file", variable=self.mode,
                        value="file").grid(row=r, column=2, sticky="w", **pad)
        r += 1
        ttk.Entry(root, textvariable=self.source).grid(row=r, column=0, columnspan=2,
                                                       sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self.pick_source).grid(
            row=r, column=2, sticky="ew", **pad)
        r += 1
        ttk.Label(root, text="Start page:").grid(row=r, column=0, sticky="w", **pad)
        self.entry_box = ttk.Combobox(root, textvariable=self.entry)
        self.entry_box.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)

        r += 1
        ttk.Separator(root).grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        r += 1
        heading("2. App settings", r)
        r += 1
        ttk.Label(root, text="App name:").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.name).grid(row=r, column=1, columnspan=2,
                                                     sticky="ew", **pad)
        r += 1
        ttk.Label(root, text="Window size:").grid(row=r, column=0, sticky="w", **pad)
        size = ttk.Frame(root)
        size.grid(row=r, column=1, columnspan=2, sticky="w", **pad)
        ttk.Entry(size, textvariable=self.width, width=7).pack(side="left")
        ttk.Label(size, text="  x  ").pack(side="left")
        ttk.Entry(size, textvariable=self.height, width=7).pack(side="left")
        ttk.Label(size, text="   (Windows / Linux)").pack(side="left")
        r += 1
        ttk.Label(root, text="Icon (.ico/.png):").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.icon).grid(row=r, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self.pick_icon).grid(
            row=r, column=2, sticky="ew", **pad)
        r += 1
        opts = ttk.Frame(root)
        opts.grid(row=r, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(opts, text="Start fullscreen", variable=self.fullscreen).grid(
            row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(opts, text="Resizable window", variable=self.resizable).grid(
            row=0, column=1, sticky="w", padx=(0, 16))
        ttk.Checkbutton(opts, text="Enable dev tools", variable=self.debug).grid(
            row=0, column=2, sticky="w")

        r += 1
        ttk.Separator(root).grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        r += 1
        heading("3. Build for", r)
        r += 1
        targets = ttk.Frame(root)
        targets.grid(row=r, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(targets, text="Windows (.exe)", variable=self.t_windows).pack(
            side="left", padx=(0, 18))
        ttk.Checkbutton(targets, text="Linux (.tar.gz)", variable=self.t_linux).pack(
            side="left", padx=(0, 18))
        ttk.Checkbutton(targets, text="Android (.apk)", variable=self.t_android).pack(
            side="left")

        r += 1
        nb = ttk.Notebook(root)
        nb.grid(row=r, column=0, columnspan=3, sticky="ew", padx=10, pady=4)

        # --- Windows tab
        tw = ttk.Frame(nb, padding=8)
        nb.add(tw, text="Windows options")
        ttk.Checkbutton(tw, text="Single .exe file (slower start; untick for a faster folder build)",
                        variable=self.onefile).grid(row=0, column=0, sticky="w")
        ttk.Label(tw, text="Needs to be run on Windows. Uses Edge WebView2 (built into Win 10/11).",
                  foreground="#555").grid(row=1, column=0, sticky="w", pady=(4, 0))

        # --- Linux tab
        tl = ttk.Frame(nb, padding=8)
        nb.add(tl, text="Linux options")
        tl.columnconfigure(1, weight=1)
        ttk.Label(tl, text="CPU:").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(tl, textvariable=self.linux_arch, values=["x64", "arm64"],
                     state="readonly", width=8).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(tl, text="Electron version:").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(tl, textvariable=self.electron_version, width=12).grid(
            row=1, column=1, sticky="w", pady=2)
        ttk.Label(tl, text="Works from any OS (downloads Electron once, then caches it). "
                           "Result: extract the .tar.gz and run the file named after your game.",
                  foreground="#555", wraplength=640, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- Android tab
        ta = ttk.Frame(nb, padding=8)
        nb.add(ta, text="Android options")
        ta.columnconfigure(1, weight=1)
        ttk.Label(ta, text="Orientation:").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(ta, textvariable=self.orientation,
                     values=["Auto", "Landscape", "Portrait"], state="readonly",
                     width=12).grid(row=0, column=1, sticky="w", pady=2)
        chk = ttk.Frame(ta)
        chk.grid(row=1, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Checkbutton(chk, text="Immersive fullscreen (hide system bars)",
                        variable=self.immersive).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(chk, text="Keep screen on", variable=self.keep_on).pack(side="left")
        ttk.Label(ta, text="Package ID:").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(ta, textvariable=self.pkg).grid(row=2, column=1, columnspan=2,
                                                  sticky="ew", pady=2)
        ttk.Label(ta, text="(blank = auto, e.g. com.yourname.mygame)",
                  foreground="#555").grid(row=3, column=1, sticky="w")
        ttk.Label(ta, text="Android SDK:").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Entry(ta, textvariable=self.sdk).grid(row=4, column=1, sticky="ew", pady=2)
        ttk.Button(ta, text="Browse...", command=lambda: self._pick_dir(self.sdk)).grid(
            row=4, column=2, padx=(6, 0))
        ttk.Label(ta, text="JDK folder:").grid(row=5, column=0, sticky="w", pady=2)
        ttk.Entry(ta, textvariable=self.jdk).grid(row=5, column=1, sticky="ew", pady=2)
        ttk.Button(ta, text="Browse...", command=lambda: self._pick_dir(self.jdk)).grid(
            row=5, column=2, padx=(6, 0))
        ttk.Label(ta, text="Capacitor major:").grid(row=6, column=0, sticky="w", pady=2)
        ttk.Entry(ta, textvariable=self.cap_version, width=6).grid(row=6, column=1,
                                                                   sticky="w", pady=2)
        ttk.Label(ta, text="Needs Node.js 20+, JDK 21 and the Android SDK (Android Studio installs the last two). "
                           "Result is a debug-signed .apk you can install on a phone.",
                  foreground="#555", wraplength=640, justify="left").grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))

        r += 1
        ttk.Separator(root).grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)
        r += 1
        heading("4. Output folder", r)
        r += 1
        ttk.Entry(root, textvariable=self.output).grid(row=r, column=0, columnspan=2,
                                                       sticky="ew", **pad)
        ttk.Button(root, text="Browse...", command=self.pick_output).grid(
            row=r, column=2, sticky="ew", **pad)
        r += 1
        self.build_btn = ttk.Button(root, text="BUILD", command=self.start_build)
        self.build_btn.grid(row=r, column=0, columnspan=3, sticky="ew", padx=10, pady=8)
        r += 1
        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.grid(row=r, column=0, columnspan=3, sticky="ew", padx=10)
        r += 1
        root.rowconfigure(r, weight=1)
        log_frame = ttk.Frame(root)
        log_frame.grid(row=r, column=0, columnspan=3, sticky="nsew", padx=10, pady=6)
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled",
                           font=("Consolas", 9))
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ------------------------------------------------------------- pickers
    def pick_source(self):
        if self.mode.get() == "folder":
            path = filedialog.askdirectory(title="Select the game folder")
        else:
            path = filedialog.askopenfilename(
                title="Select the HTML file",
                filetypes=[("HTML files", "*.html *.htm"), ("All files", "*.*")],
            )
        if not path:
            return
        self.source.set(path)
        if self.mode.get() == "folder":
            pages = self._find_html(path)
            self.entry_box["values"] = pages
            if pages:
                self.entry.set("index.html" if "index.html" in pages else pages[0])
            if not self.name.get():
                self.name.set(os.path.basename(path.rstrip("/\\")))
        else:
            self.entry_box["values"] = [os.path.basename(path)]
            self.entry.set(os.path.basename(path))
            if not self.name.get():
                self.name.set(os.path.splitext(os.path.basename(path))[0])

    def pick_icon(self):
        path = filedialog.askopenfilename(
            title="Select an icon",
            filetypes=[("Images", "*.ico *.png *.jpg *.jpeg"), ("All files", "*.*")],
        )
        if path:
            self.icon.set(path)

    def pick_output(self):
        path = filedialog.askdirectory(title="Where to save the results")
        if path:
            self.output.set(path)

    def _pick_dir(self, var):
        path = filedialog.askdirectory(title="Select folder")
        if path:
            var.set(path)

    @staticmethod
    def _find_html(folder, max_depth=2):
        found = []
        base_depth = folder.rstrip("/\\").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(folder):
            if dirpath.count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
            for fn in filenames:
                if fn.lower().endswith((".html", ".htm")):
                    rel = os.path.relpath(os.path.join(dirpath, fn), folder)
                    found.append(rel.replace(os.sep, "/"))
        found.sort(key=lambda p: (p.count("/"), p))
        return found

    # --------------------------------------------------------------- log
    def write(self, text):
        self.log_queue.put(text)

    def _drain_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "\0DONE":
                    self._finish()
                    continue
                self.log.configure(state="normal")
                self.log.insert("end", msg if msg.endswith("\n") else msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def _finish(self):
        self.building = False
        self.progress.stop()
        self.build_btn.configure(state="normal")

    # ------------------------------------------------------------- build
    def start_build(self):
        if self.building:
            return
        src = self.source.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showerror("Missing game", "Choose a game folder or HTML file first.")
            return
        entry = self.entry.get().strip()
        if not entry:
            messagebox.showerror("Missing start page", "Choose the HTML file that starts the game.")
            return
        if self.mode.get() == "folder" and not os.path.isfile(os.path.join(src, entry)):
            messagebox.showerror("Start page not found", f"'{entry}' doesn't exist in that folder.")
            return
        try:
            width, height = int(self.width.get()), int(self.height.get())
        except ValueError:
            messagebox.showerror("Bad size", "Width and height must be whole numbers.")
            return
        targets = [t for t, v in (("windows", self.t_windows), ("linux", self.t_linux),
                                  ("android", self.t_android)) if v.get()]
        if not targets:
            messagebox.showerror("Nothing to build", "Tick at least one target (Windows, Linux or Android).")
            return
        if "windows" in targets and not IS_WIN:
            messagebox.showerror("Windows target", "The Windows .exe can only be built while running on Windows.")
            return
        pkg = self.pkg.get().strip()
        if "android" in targets and pkg and not PACKAGE_RE.match(pkg):
            messagebox.showerror("Bad package ID",
                                 "Use lowercase letters/digits, dot separated, e.g. com.yourname.mygame")
            return
        name = self.name.get().strip() or "Game"
        out = self.output.get().strip()
        if not out:
            messagebox.showerror("Missing output", "Choose an output folder.")
            return

        settings = dict(
            mode=self.mode.get(), src=src, entry=entry, name=name,
            width=width, height=height, fullscreen=self.fullscreen.get(),
            resizable=self.resizable.get(), debug=self.debug.get(),
            icon=self.icon.get().strip(), out=out, targets=targets,
            onefile=self.onefile.get(),
            linux_arch=self.linux_arch.get(), electron_version=self.electron_version.get().strip(),
            orientation=self.orientation.get(), immersive=self.immersive.get(),
            keep_on=self.keep_on.get(), pkg=pkg, sdk=self.sdk.get().strip(),
            jdk=self.jdk.get().strip(), cap_version=self.cap_version.get().strip() or "7",
        )
        self.building = True
        self.build_btn.configure(state="disabled")
        self.progress.start(12)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        threading.Thread(target=self._build_worker, args=(settings,), daemon=True).start()

    def _build_worker(self, s):
        work = tempfile.mkdtemp(prefix="h2e_")
        try:
            self._build(s, work)
        except Exception as exc:  # noqa: BLE001
            self.write(f"\nERROR: {exc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self.write("\0DONE")

    def _run(self, cmd, cwd, env=None):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=flags,
        )
        for line in proc.stdout:
            self.write(line.rstrip())
        return proc.wait()

    def _ensure_pillow(self, cwd):
        if importlib.util.find_spec("PIL") is None:
            self.write("Installing Pillow (needed to convert the icon)...")
            if self._run([sys.executable, "-m", "pip", "install", "pillow"], cwd) != 0:
                raise RuntimeError("Could not install Pillow.")

    # --------------------------------------------------------------- main
    def _build(self, s, work):
        safe = safe_filename(s["name"])
        self.write("Copying game files...")
        game_dst = os.path.join(work, "game")
        if s["mode"] == "folder":
            shutil.copytree(
                s["src"], game_dst,
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"),
            )
            entry = s["entry"].replace("\\", "/")
        else:
            os.makedirs(game_dst)
            shutil.copy2(s["src"], game_dst)
            entry = os.path.basename(s["src"])

        os.makedirs(s["out"], exist_ok=True)
        builders = {
            "windows": self._build_windows,
            "linux": self._build_linux,
            "android": self._build_android,
        }
        ok, failed = [], []
        for t in s["targets"]:
            self.write(f"\n===== {t.upper()} =====")
            tdir = os.path.join(work, t)
            os.makedirs(tdir, exist_ok=True)
            try:
                result = builders[t](s, tdir, game_dst, entry, safe)
                ok.append(result)
                self.write(f"OK: {result}")
            except Exception as exc:  # noqa: BLE001
                failed.append(t)
                self.write(f"ERROR ({t}): {exc}")

        self.write("\n----- SUMMARY -----")
        for p in ok:
            self.write(f"Built: {p}")
        for t in failed:
            self.write(f"Failed: {t} (see the log above)")
        if ok and IS_WIN:
            try:
                os.startfile(s["out"])
            except OSError:
                pass

    # ------------------------------------------------------------ WINDOWS
    def _icon_ico(self, s, tdir):
        if not s["icon"]:
            return None
        if not os.path.isfile(s["icon"]):
            raise RuntimeError("Icon file not found.")
        if s["icon"].lower().endswith(".ico"):
            return s["icon"]
        self._ensure_pillow(tdir)
        from PIL import Image

        ico = os.path.join(tdir, "icon.ico")
        img = Image.open(s["icon"]).convert("RGBA")
        img.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        return ico

    def _build_windows(self, s, tdir, game_dst, entry, safe):
        missing = [
            pkg for pkg, mod in (("pyinstaller", "PyInstaller"), ("pywebview", "webview"))
            if importlib.util.find_spec(mod) is None
        ]
        if missing:
            self.write("Installing missing packages: " + ", ".join(missing))
            if self._run([sys.executable, "-m", "pip", "install", *missing], tdir) != 0:
                raise RuntimeError("Could not install " + ", ".join(missing))

        cfg = {
            "name": s["name"], "safe_name": safe, "entry": entry,
            "width": s["width"], "height": s["height"],
            "fullscreen": s["fullscreen"], "resizable": s["resizable"],
            "debug": s["debug"],
        }
        cfg_path = os.path.join(tdir, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        runner_path = os.path.join(tdir, "runner.py")
        with open(runner_path, "w", encoding="utf-8") as f:
            f.write(RUNNER_SOURCE)

        icon_path = self._icon_ico(s, tdir)

        cmd = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
            "--name", safe,
            "--distpath", s["out"],
            "--workpath", os.path.join(tdir, "build"),
            "--specpath", tdir,
            "--add-data", f"{game_dst};game",
            "--add-data", f"{cfg_path};.",
            "--collect-all", "webview",
            "--hidden-import", "clr",
            "--onefile" if s["onefile"] else "--onedir",
        ]
        if icon_path:
            cmd += ["--icon", icon_path]
        cmd.append(runner_path)

        self.write("Building exe (this can take a few minutes)...\n")
        code = self._run(cmd, tdir)
        if code != 0:
            raise RuntimeError(f"PyInstaller failed (exit code {code}).")
        return (
            os.path.join(s["out"], safe + ".exe") if s["onefile"]
            else os.path.join(s["out"], safe, safe + ".exe")
        )

    # -------------------------------------------------------------- LINUX
    def _download(self, url, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "HTML2EXE"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, -10
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct - last >= 10:
                        last = pct
                        self.write(f"  downloading... {pct}%")
        os.replace(tmp, dest)

    def _build_linux(self, s, tdir, game_dst, entry, safe):
        arch = s["linux_arch"] if s["linux_arch"] in ("x64", "arm64") else "x64"
        ver = s["electron_version"].lstrip("v") or "33.0.0"
        if not re.match(r"^\d+\.\d+\.\d+$", ver):
            raise RuntimeError("Electron version must look like 33.0.0")

        zip_name = f"electron-v{ver}-linux-{arch}.zip"
        cache_base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
        zip_path = os.path.join(cache_base, "HTML2EXE", "cache", zip_name)
        if not (os.path.isfile(zip_path) and zipfile.is_zipfile(zip_path)):
            url = f"https://github.com/electron/electron/releases/download/v{ver}/{zip_name}"
            self.write(f"Downloading Electron {ver} for Linux {arch} (one time, ~100 MB)...")
            try:
                self._download(url, zip_path)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Could not download {url}\n  {exc}\n"
                    "Check your internet connection or try another Electron version."
                ) from exc
            if not zipfile.is_zipfile(zip_path):
                os.remove(zip_path)
                raise RuntimeError("Downloaded file is not a valid zip.")
        else:
            self.write(f"Using cached {zip_name}")

        has_icon = False
        app_files = []
        if s["icon"]:
            if not os.path.isfile(s["icon"]):
                raise RuntimeError("Icon file not found.")
            self._ensure_pillow(tdir)
            from PIL import Image

            buf = io.BytesIO()
            img = Image.open(s["icon"]).convert("RGBA")
            img.thumbnail((512, 512))
            img.save(buf, "PNG")
            app_files.append(("icon.png", buf.getvalue()))
            has_icon = True

        cfg = {
            "name": s["name"], "entry": entry, "width": s["width"], "height": s["height"],
            "fullscreen": s["fullscreen"], "resizable": s["resizable"],
            "debug": s["debug"], "noSandbox": True, "hasIcon": has_icon,
        }
        pkg_json = {"name": slugify(s["name"]), "productName": s["name"],
                    "version": "1.0.0", "main": "main.js"}
        app_files += [
            ("main.js", ELECTRON_MAIN_JS.encode("utf-8")),
            ("package.json", json.dumps(pkg_json, indent=2).encode("utf-8")),
            ("config.json", json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")),
        ]

        out_path = os.path.join(s["out"], f"{safe}-linux-{arch}.tar.gz")
        self.write("Packaging...")
        package_linux_tar(zip_path, out_path, safe, game_dst, app_files)
        return out_path

    # ------------------------------------------------------------ ANDROID
    def _make_android_icons(self, icon_src, res_dir, cwd):
        self._ensure_pillow(cwd)
        from PIL import Image, ImageDraw

        src = Image.open(icon_src).convert("RGBA")
        densities = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}
        for d, m in densities.items():
            folder = os.path.join(res_dir, f"mipmap-{d}")
            if not os.path.isdir(folder):
                continue
            legacy = int(48 * m)
            img = src.resize((legacy, legacy), Image.LANCZOS)
            img.save(os.path.join(folder, "ic_launcher.png"))
            mask = Image.new("L", (legacy, legacy), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, legacy - 1, legacy - 1), fill=255)
            rnd = Image.new("RGBA", (legacy, legacy), (0, 0, 0, 0))
            rnd.paste(img, (0, 0), mask)
            rnd.save(os.path.join(folder, "ic_launcher_round.png"))
            fg = int(108 * m)
            inner = int(fg * 0.72)
            canvas = Image.new("RGBA", (fg, fg), (0, 0, 0, 0))
            small = src.resize((inner, inner), Image.LANCZOS)
            canvas.paste(small, ((fg - inner) // 2, (fg - inner) // 2), small)
            canvas.save(os.path.join(folder, "ic_launcher_foreground.png"))

    def _build_android(self, s, tdir, game_dst, entry, safe):
        node, npm, npx = shutil.which("node"), shutil.which("npm"), shutil.which("npx")
        if not (node and npm and npx):
            raise RuntimeError("Node.js 20+ is required for Android builds. "
                               "Install it from https://nodejs.org, then restart HTML2EXE.")
        sdk, jdk = s["sdk"], s["jdk"]
        if not sdk or not os.path.isdir(sdk):
            raise RuntimeError("Android SDK folder not found. Install Android Studio "
                               "(it installs the SDK) and set the folder in the Android tab.")
        java = _java_exe(jdk) if jdk else ""
        if not java or not os.path.isfile(java):
            raise RuntimeError("JDK not found. Install JDK 21 (Android Studio bundles one in its 'jbr' "
                               "folder) and set the JDK folder in the Android tab.")
        major = java_major(java)
        if major is not None:
            self.write(f"Using Java {major} at {jdk}")
            if major > 23:
                raise RuntimeError(
                    f"Java {major} is too new: the Gradle used by Capacitor only runs on Java 17-23.\n"
                    "Install JDK 21 (Windows: open a terminal and run\n"
                    "    winget install EclipseAdoptium.Temurin.21.JDK\n"
                    "), then set the 'JDK folder' in the Android tab to something like\n"
                    "    C:\\Program Files\\Eclipse Adoptium\\jdk-21.x.x-hotspot\n"
                    "(restart HTML2EXE after installing so it can auto-detect it)."
                )
            if major < 21:
                self.write("WARNING: Capacitor 7 expects JDK 21; the build may fail on older JDKs.")

        pkg = s["pkg"] or default_package(s["name"])
        if not PACKAGE_RE.match(pkg):
            raise RuntimeError(f"Invalid package ID: {pkg}")
        app_name = re.sub(r"[<>&\"']", "", s["name"]).strip() or "Game"

        proj = os.path.join(tdir, "app")
        www = os.path.join(proj, "www")
        os.makedirs(proj)
        shutil.copytree(game_dst, www)
        if entry != "index.html":
            if os.path.exists(os.path.join(www, "index.html")):
                self.write("Note: existing index.html renamed to _original_index.html "
                           "(Capacitor always starts from index.html).")
                os.replace(os.path.join(www, "index.html"), os.path.join(www, "_original_index.html"))
            target = "/".join(urllib_quote(p) for p in entry.split("/"))
            with open(os.path.join(www, "index.html"), "w", encoding="utf-8") as f:
                f.write(
                    '<!doctype html><html><head><meta charset="utf-8">'
                    f'<script>location.replace("{target}");</script>'
                    f'<noscript><meta http-equiv="refresh" content="0; url={target}"></noscript>'
                    "</head><body></body></html>"
                )

        with open(os.path.join(proj, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "game-app", "version": "1.0.0", "private": True}, f)
        with open(os.path.join(proj, "capacitor.config.json"), "w", encoding="utf-8") as f:
            json.dump({"appId": pkg, "appName": app_name, "webDir": "www"}, f, indent=2)

        env = os.environ.copy()
        env["JAVA_HOME"] = jdk
        env["ANDROID_HOME"] = sdk
        env["ANDROID_SDK_ROOT"] = sdk
        env["PATH"] = os.path.join(jdk, "bin") + os.pathsep + env.get("PATH", "")
        cap = s["cap_version"]

        self.write("\n[1/4] Installing Capacitor (needs internet)...")
        if self._run([npm, "install", f"@capacitor/core@{cap}", f"@capacitor/cli@{cap}",
                      f"@capacitor/android@{cap}", "--no-audit", "--no-fund"], proj, env) != 0:
            raise RuntimeError("npm install failed.")

        self.write("\n[2/4] Creating the Android project...")
        if self._run([npx, "cap", "add", "android"], proj, env) != 0:
            raise RuntimeError("'cap add android' failed.")
        android = os.path.join(proj, "android")
        if not os.path.isdir(android):
            raise RuntimeError("Capacitor did not create the android folder.")

        self.write("\n[3/4] Applying your settings...")
        with open(os.path.join(android, "local.properties"), "w", encoding="utf-8") as f:
            f.write("sdk.dir=" + sdk.replace("\\", "/") + "\n")

        manifest = os.path.join(android, "app", "src", "main", "AndroidManifest.xml")
        with open(manifest, "r", encoding="utf-8") as f:
            text = f.read()
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(patch_manifest(text, s["orientation"]))

        activity = None
        for dp, _, fns in os.walk(os.path.join(android, "app", "src", "main", "java")):
            if "MainActivity.java" in fns:
                activity = os.path.join(dp, "MainActivity.java")
                break
        if activity:
            with open(activity, "r", encoding="utf-8") as f:
                m = re.search(r"^\s*package\s+([\w.]+)\s*;", f.read(), re.M)
            real_pkg = m.group(1) if m else pkg
            with open(activity, "w", encoding="utf-8") as f:
                f.write(make_activity(real_pkg, s["immersive"], s["keep_on"]))
        else:
            self.write("WARNING: MainActivity.java not found; fullscreen/keep-awake options skipped.")

        if s["icon"]:
            if not os.path.isfile(s["icon"]):
                raise RuntimeError("Icon file not found.")
            try:
                self._make_android_icons(s["icon"], os.path.join(android, "app", "src", "main", "res"), tdir)
            except Exception as exc:  # noqa: BLE001
                self.write(f"WARNING: could not apply the icon ({exc}); default icon used.")

        self.write("\n[4/4] Building the APK with Gradle "
                   "(first run downloads a lot and can take 10+ minutes)...")
        gradlew = os.path.join(android, "gradlew.bat" if IS_WIN else "gradlew")
        if not IS_WIN:
            os.chmod(gradlew, 0o755)
        code = self._run([gradlew, "assembleDebug", "--console=plain", "--no-daemon"], android, env)
        if code != 0:
            raise RuntimeError(
                f"Gradle failed (exit code {code}). If the log mentions SDK licenses, run "
                "'sdkmanager --licenses' from the Android SDK's cmdline-tools and accept them."
            )
        apk = os.path.join(android, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
        if not os.path.isfile(apk):
            raise RuntimeError("Build finished but app-debug.apk was not found.")
        dest = os.path.join(s["out"], f"{safe}.apk")
        shutil.copy2(apk, dest)
        return dest


def urllib_quote(part: str) -> str:
    from urllib.parse import quote

    return quote(part)


if __name__ == "__main__":
    App().mainloop()
