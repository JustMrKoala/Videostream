
import os
import sys
import shutil
import signal
import socket
import struct
import threading
import subprocess
import tempfile
import mimetypes
import tkinter as tk
from tkinter import filedialog, messagebox
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
PORT = 8765
CHUNK = 1 << 16  # 64 KB chunks for streaming

# ── State shared between threads ──────────────────────────────────────────────
state = {
    "video_path": None,
    "server":     None,
    "server_thread": None,
    "tunnel_proc": None,
    "tunnel_url":  None,
}

# ── HTTP handler with Range support ──────────────────────────────────────────
class VideoHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence server logs

    def do_GET(self):
        if self.path not in ("/", "/video"):
            self.send_error(404)
            return

        if self.path == "/":
            self._serve_player()
        else:
            self._serve_video()

    def _serve_player(self):
        filename = Path(state["video_path"]).name
        mime, _ = mimetypes.guess_type(state["video_path"])
        mime = mime or "video/mp4"
        # truncate long names for display
        display_name = filename if len(filename) <= 40 else filename[:37] + "…"
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0f0f0f">
<title>{display_name}</title>
<style>
:root {{
  --bg:     #0f0f0f;
  --panel:  #181818;
  --border: #2a2a2a;
  --lime:   #e0ff4f;
  --fg:     #e8e8e8;
  --muted:  #555;
  --safe-bottom: env(safe-area-inset-bottom, 0px);
}}
*, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent }}
html, body {{
  height:100%; background:var(--bg); color:var(--fg);
  font-family:'Courier New', Courier, monospace;
  overscroll-behavior:none;
}}

/* ── layout shell ── */
body {{
  display:flex; flex-direction:column; min-height:100dvh;
}}

/* ── top bar ── */
header {{
  flex-shrink:0;
  display:flex; align-items:center; justify-content:space-between;
  padding:14px 18px 12px;
  border-bottom:1px solid var(--border);
}}
.logo {{ color:var(--lime); font-size:.8rem; font-weight:700; letter-spacing:.12em }}
.fname {{
  color:var(--muted); font-size:.72rem; letter-spacing:.04em;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  max-width:55vw; text-align:right;
}}

/* ── video wrapper — fills remaining space ── */
.stage {{
  flex:1; display:flex; align-items:center; justify-content:center;
  background:#000; position:relative; overflow:hidden;
}}
video {{
  width:100%; height:100%;
  object-fit:contain;
  /* tell iOS this is inline, not fullscreen-forced */
  -webkit-playsinline:true;
}}

/* ── custom overlay controls (shown when native controls are awkward on mobile) ── */
/* we keep native controls — they're best on mobile — but style the container */
.stage video::-webkit-media-controls-panel {{
  background:linear-gradient(transparent 60%, rgba(0,0,0,.7));
}}

/* ── bottom info bar ── */
footer {{
  flex-shrink:0;
  padding:10px 18px calc(10px + var(--safe-bottom));
  border-top:1px solid var(--border);
  background:var(--panel);
  display:flex; align-items:center; gap:12px;
}}
.dot {{
  width:7px; height:7px; border-radius:50%;
  background:var(--lime); flex-shrink:0;
  animation:pulse 2s ease-in-out infinite;
}}
@keyframes pulse {{
  0%,100% {{ opacity:1; transform:scale(1) }}
  50%      {{ opacity:.4; transform:scale(.75) }}
}}
.status {{ font-size:.7rem; color:var(--muted); letter-spacing:.06em; flex:1 }}
.fullscreen-btn {{
  background:none; border:1px solid var(--border); color:var(--muted);
  font-size:.65rem; font-family:inherit; letter-spacing:.08em;
  padding:5px 10px; border-radius:3px; cursor:pointer;
  transition:color .15s, border-color .15s;
  -webkit-appearance:none;
}}
.fullscreen-btn:active {{ color:var(--lime); border-color:var(--lime) }}

/* ── landscape: hide bars for more screen ── */
@media (orientation:landscape) and (max-height:500px) {{
  header, footer {{ display:none }}
  .stage {{ height:100dvh }}
}}
</style>
</head>
<body>

<header>
  <span class="logo">▶ VIDEOSTREAM</span>
  <span class="fname" title="{filename}">{display_name}</span>
</header>

<div class="stage">
  <video
    id="v"
    controls
    autoplay
    playsinline
    webkit-playsinline
    preload="metadata"
    x-webkit-airplay="allow"
  >
    <source src="/video" type="{mime}">
    Your browser does not support HTML5 video.
  </video>
</div>

<footer>
  <span class="dot"></span>
  <span class="status" id="st">connecting…</span>
  <button class="fullscreen-btn" id="fsBtn" onclick="toggleFS()">FULLSCREEN</button>
</footer>

<script>
const v  = document.getElementById('v');
const st = document.getElementById('st');

function fmt(s) {{
  if (!s || !isFinite(s)) return '--:--';
  const m = Math.floor(s/60), sec = Math.floor(s%60);
  return m + ':' + String(sec).padStart(2,'0');
}}

function updateStatus() {{
  if (v.readyState === 0) {{ st.textContent = 'loading…'; return; }}
  const cur = fmt(v.currentTime);
  const dur = fmt(v.duration);
  st.textContent = v.paused
    ? 'paused · ' + cur + ' / ' + dur
    : 'playing · ' + cur + ' / ' + dur;
}}

v.addEventListener('loadedmetadata', updateStatus);
v.addEventListener('timeupdate', updateStatus);
v.addEventListener('pause', updateStatus);
v.addEventListener('play',  updateStatus);
v.addEventListener('error', () => {{ st.textContent = 'error loading video'; }});
v.addEventListener('waiting', () => {{ st.textContent = 'buffering…'; }});

/* fullscreen — works across browsers + iOS fallback */
function toggleFS() {{
  const el = document.documentElement;
  if (document.fullscreenElement || document.webkitFullscreenElement) {{
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  }} else if (v.webkitEnterFullscreen) {{
    /* iOS Safari — enter native video fullscreen */
    v.webkitEnterFullscreen();
  }} else {{
    (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
  }}
}}

/* hide fullscreen button when already in fullscreen */
document.addEventListener('fullscreenchange', () => {{
  document.getElementById('fsBtn').textContent =
    document.fullscreenElement ? 'EXIT' : 'FULLSCREEN';
}});

/* double-tap the video to toggle play/pause (feels natural on mobile) */
let lastTap = 0;
v.addEventListener('touchend', e => {{
  const now = Date.now();
  if (now - lastTap < 300) {{
    v.paused ? v.play() : v.pause();
    e.preventDefault();
  }}
  lastTap = now;
}});
</script>
</body>
</html>"""
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_video(self):
        path = state["video_path"]
        size = os.path.getsize(path)
        mime, _ = mimetypes.guess_type(path)
        mime = mime or "video/mp4"

        range_header = self.headers.get("Range")
        if range_header:
            # parse "bytes=start-end"
            byte_range = range_header.strip().replace("bytes=", "")
            parts = byte_range.split("-")
            start = int(parts[0]) if parts[0] else 0
            end   = int(parts[1]) if parts[1] else size - 1
            end   = min(end, size - 1)
            length = end - start + 1

            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining:
                    data = f.read(min(CHUNK, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    data = f.read(CHUNK)
                    if not data:
                        break
                    self.wfile.write(data)


# ── Server helpers ────────────────────────────────────────────────────────────
def start_server():
    srv = HTTPServer(("0.0.0.0", PORT), VideoHandler)
    state["server"] = srv
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    state["server_thread"] = t


def stop_server():
    if state["server"]:
        state["server"].shutdown()
        state["server"] = None


# ── Cloudflare tunnel helpers ─────────────────────────────────────────────────
def find_cloudflared():
    """Return path to cloudflared binary, or None."""
    return shutil.which("cloudflared")


def start_tunnel(on_url):
    """Launch cloudflared in a temp dir; call on_url(url) when ready."""
    cf = find_cloudflared()
    if not cf:
        on_url(None)
        return

    tmp = tempfile.mkdtemp(prefix="cfstream_")

    def run():
        try:
            proc = subprocess.Popen(
                [cf, "tunnel", "--url", f"http://localhost:{PORT}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=tmp,
                text=True,
            )
            state["tunnel_proc"] = proc
            url_found = False
            for line in proc.stdout:
                if not url_found and "trycloudflare.com" in line:
                    # extract URL
                    for token in line.split():
                        if token.startswith("https://") and "trycloudflare" in token:
                            state["tunnel_url"] = token.strip()
                            on_url(token.strip())
                            url_found = True
                            break
        except Exception as e:
            on_url(None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    t = threading.Thread(target=run, daemon=True)
    t.start()


def stop_tunnel():
    if state["tunnel_proc"]:
        state["tunnel_proc"].terminate()
        state["tunnel_proc"] = None


# ── GUI ───────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    DARK   = "#0f0f0f"
    PANEL  = "#181818"
    BORDER = "#2a2a2a"
    ACCENT = "#e0ff4f"      # neon lime
    FG     = "#e8e8e8"
    MUTED  = "#666666"

    def __init__(self):
        super().__init__()
        self.title("VideoStream")
        self.configure(bg=self.DARK)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._quit)

        self._build_ui()
        self._center()

    # ── layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        pad = dict(padx=24, pady=0)

        # top bar
        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x")

        header = tk.Frame(self, bg=self.DARK)
        header.pack(fill="x", padx=24, pady=(20, 0))

        tk.Label(
            header, text="▶ VIDEOSTREAM",
            bg=self.DARK, fg=self.ACCENT,
            font=("Courier New", 13, "bold"), anchor="w",
        ).pack(side="left")

        tk.Label(
            header, text="no trace · temp tunnel",
            bg=self.DARK, fg=self.MUTED,
            font=("Courier New", 9), anchor="e",
        ).pack(side="right")

        # divider
        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x", pady=(14, 0))

        # file row
        file_row = tk.Frame(self, bg=self.PANEL)
        file_row.pack(fill="x", padx=0, pady=0)
        file_row.pack(fill="x")

        inner = tk.Frame(file_row, bg=self.PANEL)
        inner.pack(fill="x", padx=24, pady=16)

        self.file_label = tk.Label(
            inner, text="No file selected",
            bg=self.PANEL, fg=self.MUTED,
            font=("Courier New", 10), anchor="w", width=46,
        )
        self.file_label.pack(side="left")

        tk.Button(
            inner, text="BROWSE",
            bg=self.BORDER, fg=self.FG,
            font=("Courier New", 9, "bold"),
            relief="flat", cursor="hand2",
            activebackground="#333", activeforeground=self.ACCENT,
            padx=12, pady=5,
            command=self._pick_file,
        ).pack(side="right")

        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x")

        # status box
        status_frame = tk.Frame(self, bg=self.DARK)
        status_frame.pack(fill="x", padx=24, pady=(18, 6))

        tk.Label(
            status_frame, text="STATUS",
            bg=self.DARK, fg=self.MUTED,
            font=("Courier New", 8), anchor="w",
        ).pack(anchor="w")

        self.status_var = tk.StringVar(value="Idle — pick a video to begin.")
        tk.Label(
            status_frame, textvariable=self.status_var,
            bg=self.DARK, fg=self.FG,
            font=("Courier New", 10), anchor="w", wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # URL boxes
        url_frame = tk.Frame(self, bg=self.DARK)
        url_frame.pack(fill="x", padx=24, pady=(14, 0))

        self._make_url_row(url_frame, "LOCAL", f"http://localhost:{PORT}")
        self.tunnel_url_var = tk.StringVar(value="—")
        self._make_url_row(url_frame, "TUNNEL", None, var=self.tunnel_url_var)

        # action buttons
        btn_frame = tk.Frame(self, bg=self.DARK)
        btn_frame.pack(fill="x", padx=24, pady=(20, 24))

        self.start_btn = tk.Button(
            btn_frame, text="START STREAM",
            bg=self.ACCENT, fg="#000",
            font=("Courier New", 10, "bold"),
            relief="flat", cursor="hand2",
            activebackground="#c8e020", activeforeground="#000",
            padx=16, pady=8, state="disabled",
            command=self._start,
        )
        self.start_btn.pack(side="left")

        self.stop_btn = tk.Button(
            btn_frame, text="STOP",
            bg=self.BORDER, fg=self.FG,
            font=("Courier New", 10, "bold"),
            relief="flat", cursor="hand2",
            activebackground="#333", activeforeground="#f55",
            padx=16, pady=8, state="disabled",
            command=self._stop,
        )
        self.stop_btn.pack(side="left", padx=(10, 0))

        self.copy_btn = tk.Button(
            btn_frame, text="COPY TUNNEL URL",
            bg=self.BORDER, fg=self.MUTED,
            font=("Courier New", 9),
            relief="flat", cursor="hand2",
            activebackground="#333", activeforeground=self.FG,
            padx=12, pady=8, state="disabled",
            command=self._copy_tunnel,
        )
        self.copy_btn.pack(side="right")

        # footer
        tk.Frame(self, bg=self.BORDER, height=1).pack(fill="x")
        tk.Label(
            self, text="Requires cloudflared in PATH for public tunnel",
            bg=self.DARK, fg="#3a3a3a",
            font=("Courier New", 8),
        ).pack(pady=8)

    def _make_url_row(self, parent, label, fixed_url, var=None):
        row = tk.Frame(parent, bg=self.DARK)
        row.pack(fill="x", pady=3)

        tk.Label(
            row, text=f"{label}:",
            bg=self.DARK, fg=self.MUTED,
            font=("Courier New", 8), width=7, anchor="w",
        ).pack(side="left")

        if var:
            tk.Label(
                row, textvariable=var,
                bg=self.DARK, fg=self.FG,
                font=("Courier New", 10), anchor="w",
            ).pack(side="left")
        else:
            tk.Label(
                row, text=fixed_url,
                bg=self.DARK, fg=self.FG,
                font=("Courier New", 10), anchor="w",
            ).pack(side="left")

    # ── actions ───────────────────────────────────────────────────────────────
    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Select a video file",
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm *.m4v *.flv *.ts *.wmv"),
                ("All files", "*.*"),
            ]
        )
        if path:
            state["video_path"] = path
            name = Path(path).name
            display = name if len(name) <= 45 else "…" + name[-43:]
            self.file_label.config(text=display, fg=self.FG)
            self.start_btn.config(state="normal")
            self.status_var.set("File ready. Press START STREAM.")

    def _start(self):
        if not state["video_path"]:
            return

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("Starting local server…")

        start_server()
        self.status_var.set(
            f"Serving on localhost:{PORT}  ·  launching Cloudflare tunnel…"
        )

        cf = find_cloudflared()
        if cf:
            start_tunnel(self._on_tunnel_url)
        else:
            self.tunnel_url_var.set("cloudflared not found in PATH")
            self.status_var.set(
                f"Streaming locally on port {PORT}. Install cloudflared for public URL."
            )

    def _on_tunnel_url(self, url):
        # called from tunnel thread — schedule GUI update on main thread
        self.after(0, self._set_tunnel_url, url)

    def _set_tunnel_url(self, url):
        if url:
            self.tunnel_url_var.set(url)
            self.copy_btn.config(state="normal", fg=self.FG)
            self.status_var.set("Live! Share the TUNNEL URL or open LOCAL in your browser.")
        else:
            self.tunnel_url_var.set("Tunnel failed — check cloudflared")
            self.status_var.set("Local stream active. Tunnel unavailable.")

    def _stop(self):
        stop_tunnel()
        stop_server()
        self.tunnel_url_var.set("—")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.copy_btn.config(state="disabled", fg=self.MUTED)
        self.status_var.set("Stopped. Pick a file and start again.")

    def _copy_tunnel(self):
        url = state.get("tunnel_url")
        if url:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.copy_btn.config(text="COPIED ✓")
            self.after(2000, lambda: self.copy_btn.config(text="COPY TUNNEL URL"))

    def _quit(self):
        stop_tunnel()
        stop_server()
        self.destroy()

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # ensure ctrl-c works from terminal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = App()
    app.mainloop()
