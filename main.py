import ctypes
import math
import os
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
from PIL import Image, ImageTk

from ffmpeg_helper import (
    Stopped,
    check_ffmpeg,
    detect_static_logos,
    download_facebook_video,
    extract_preview_wav,
    probe,
    split_video_range,
    transform_video_policy_safe,
    write_watermark_ass,
)


def parse_hms(s):
    parts = [float(p) for p in s.strip().split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def fmt_hms(sec):
    sec = max(0, int(round(sec)))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


ASPECT_OPTIONS = {
    "Original Size (Keep Source Aspect)": None,
    "9:16 Facebook Reels / Shorts (720x1280)": (720, 1280),
    "16:9 Facebook Video HD (1280x720)": (1280, 720),
    "1:1 Square Social (1080x1080)": (1080, 1080),
}

AUDIO_MODES = {
    "🛡 Auto Copyright-Bypass Audio (Pitch +1.035x & EQ Shift)": 1.035,
    "🎵 Deep Voice Copyright-Bypass Audio (Pitch 0.965x & EQ Shift)": 0.965,
    "⚡ High-Energy Reels Audio (Pitch +1.05x & EQ Shift)": 1.050,
}

WM_POSITIONS = ["Top-Right", "Top-Left", "Bottom-Right", "Bottom-Left"]
WM_MOTIONS = [
    "Diagonal Bounce (တစ်စောင်းပြေး)",
    "Vertical Scroll (ဒေါင်လိုက်ပြေး)",
    "Horizontal Scroll (အလျားလိုက်ပြေး)",
    "Static (အသေ)",
]

SAFE_CAPTIONS = [
    "ဒီဇာတ်လမ်းလေးကို ကြည့်ပြီး ဘယ်လိုခံစားရလဲ? အဆုံးထိ ကြည့်ရှုပေးကြပါဦး။\n\n#MyanmarDrama #ShortStory #MovieClip #DailyNewsMm #ViralReels",
    "တကယ့်ကို စိတ်ဝင်စားစရာကောင်းတဲ့ ဇာတ်ကွက်လေးပါ။ ဒီနေရာမှာ သင်သာဆိုရင် ဘယ်လိုဆုံးဖြတ်မလဲ?\n\n#DramaScene #StoryTime #MyanmarVideo #DailyNewsMm #Reels",
    "မထင်မှတ်ထားတဲ့ အလှည့်အပြောင်းလေးတွေနဲ့ ကြည့်လို့ကောင်းတဲ့ အပိုင်းလေးပါ။\n\n#MovieMoments #ShortClip #Myanmar #DailyNewsMm #Trending",
]


class WinMCIPlayer:
    def __init__(self):
        self.alias = "recap_preview_mci"
        self.loaded = False
        self.winmm = getattr(ctypes, "windll", None) and ctypes.windll.winmm

    def _mci(self, cmd):
        if not self.winmm:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        self.winmm.mciSendStringW(cmd, buf, 255, 0)
        return buf.value

    def load(self, wav_path):
        self.close()
        if not self.winmm or not os.path.isfile(wav_path):
            return
        safe_path = os.path.abspath(wav_path)
        self._mci(f'open "{safe_path}" type waveaudio alias {self.alias}')
        self._mci(f"set {self.alias} time format milliseconds")
        self.loaded = True

    def play(self, start_sec=0.0):
        if not self.loaded:
            return
        ms = max(0, int(start_sec * 1000))
        self._mci(f"play {self.alias} from {ms}")

    def stop(self):
        if self.loaded:
            self._mci(f"stop {self.alias}")

    def set_volume(self, pct):
        if self.loaded:
            vol_1000 = max(0, min(1000, int(pct * 10)))
            self._mci(f"setaudio {self.alias} volume to {vol_1000}")

    def close(self):
        if self.loaded:
            self._mci(f"stop {self.alias}")
            self._mci(f"close {self.alias}")
            self.loaded = False


class InAppVideoPlayer(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.video_path = ""
        self.preview_wav = ""
        self.cap = None
        self.is_playing = False
        self.fps = 30.0
        self.total_frames = 0
        self.duration_sec = 0.0
        self.play_start_wall = 0.0
        self.play_offset_sec = 0.0
        self._photo = None
        self.audio = WinMCIPlayer()

        header = ttk.Frame(self)
        header.pack(fill="x", pady=(4, 4))
        ttk.Label(header, text="🎬 Output Video Preview Player (အသံနှင့်အရုပ် စစ်ဆေးရန်)", foreground="#60a5fa", font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Button(
            header, text="📂 Load Video to Preview", bg="#3f3f46", fg="white",
            font=("Segoe UI", 8, "bold"), relief="flat", padx=10, pady=2, command=self._browse_preview
        ).pack(side="right")

        self.screen = tk.Label(
            self, bg="#000000", text="Generated policy-safe video will play here with transformed audio",
            fg="#71717a", font=("Segoe UI", 10), width=64, height=16
        )
        self.screen.pack(fill="x", pady=4)

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", pady=(2, 6))

        self.btn_play = tk.Button(
            ctrl, text="▶ Play", bg="#16a34a", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", padx=12, command=self.toggle_play
        )
        self.btn_play.pack(side="left", padx=(0, 6))

        tk.Button(
            ctrl, text="⏹ Stop", bg="#dc2626", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", padx=10, command=self.stop
        ).pack(side="left", padx=(0, 10))

        self.time_lbl = ttk.Label(ctrl, text="00:00 / 00:00", width=14)
        self.time_lbl.pack(side="left")

        self.seek_var = tk.DoubleVar(value=0.0)
        self.seek_bar = tk.Scale(
            ctrl, from_=0, to=100, orient="horizontal", variable=self.seek_var,
            bg="#18181b", fg="#e4e4e7", troughcolor="#27272a", highlightthickness=0,
            showvalue=False, command=self._on_seek
        )
        self.seek_bar.pack(side="left", fill="x", expand=True, padx=8)

        ttk.Label(ctrl, text="🔊 Player Vol:").pack(side="left")
        self.vol_var = tk.IntVar(value=100)
        vol_slider = tk.Scale(
            ctrl, from_=0, to=100, orient="horizontal", variable=self.vol_var,
            bg="#18181b", fg="#e4e4e7", troughcolor="#27272a", highlightthickness=0,
            length=100, showvalue=False, command=self._on_vol_change
        )
        vol_slider.pack(side="left", padx=(4, 4))
        self.vol_lbl = ttk.Label(ctrl, text="100%", width=5, foreground="#34d399")
        self.vol_lbl.pack(side="left")

    def reset_player(self):
        self.stop()
        if self.cap:
            self.cap.release()
            self.cap = None
        self.audio.close()
        self.video_path = ""
        self.preview_wav = ""
        self.seek_var.set(0.0)
        self.time_lbl.config(text="00:00 / 00:00")
        self.screen.config(
            image="", text="Generated policy-safe video will play here with transformed audio",
            width=64, height=16
        )

    def _browse_preview(self):
        p = filedialog.askopenfilename(filetypes=[("Video Files", "*.mp4 *.mkv *.mov *.avi")])
        if p:
            self.load_video(p, autoplay=True)

    def load_video(self, path, autoplay=True):
        self.stop()
        if not os.path.isfile(path):
            return
        self.video_path = path
        self.cap = cv2.VideoCapture(path)
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration_sec = self.total_frames / max(1.0, self.fps)
        self.seek_bar.config(to=max(1.0, self.duration_sec))
        self.play_offset_sec = 0.0

        try:
            tmp_wav = os.path.join(tempfile.gettempdir(), f"recap_prev_{int(time.time())}.wav")
            extract_preview_wav(path, tmp_wav)
            self.preview_wav = tmp_wav
            self.audio.load(self.preview_wav)
            self.audio.set_volume(self.vol_var.get())
        except Exception:
            self.preview_wav = ""

        self._show_frame_at(0.0)
        if autoplay:
            self.play()

    def _show_frame_at(self, sec):
        if not self.cap:
            return
        frame_idx = int(sec * self.fps)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, min(self.total_frames - 1, frame_idx)))
        ok, frame = self.cap.read()
        if not ok:
            return
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        target_h = 300
        target_w = max(160, int(w * (target_h / float(h))))
        resized = cv2.resize(rgb, (target_w, target_h), interpolation=cv2.INTER_AREA)
        img = Image.fromarray(resized)
        self._photo = ImageTk.PhotoImage(image=img)
        self.screen.config(image=self._photo, text="", width=target_w, height=target_h)
        self.time_lbl.config(text=f"{fmt_hms(sec)[3:]} / {fmt_hms(self.duration_sec)[3:]}")

    def play(self):
        if not self.cap or self.is_playing:
            return
        self.is_playing = True
        self.btn_play.config(text="⏸ Pause", bg="#d97706")
        self.play_start_wall = time.time() - self.play_offset_sec
        self.audio.play(start_sec=self.play_offset_sec)
        self._tick()

    def pause(self):
        if not self.is_playing:
            return
        self.is_playing = False
        self.play_offset_sec = time.time() - self.play_start_wall
        self.btn_play.config(text="▶ Play", bg="#16a34a")
        self.audio.stop()

    def toggle_play(self):
        if self.is_playing:
            self.pause()
        else:
            if self.play_offset_sec >= max(0.0, self.duration_sec - 0.2):
                self.play_offset_sec = 0.0
            self.play()

    def stop(self):
        self.is_playing = False
        self.play_offset_sec = 0.0
        self.btn_play.config(text="▶ Play", bg="#16a34a")
        self.audio.stop()
        if self.cap:
            self._show_frame_at(0.0)
            self.seek_var.set(0.0)

    def _on_seek(self, val):
        if not self.cap:
            return
        sec = float(val)
        cur = time.time() - self.play_start_wall if self.is_playing else self.play_offset_sec
        if abs(sec - cur) > 0.6:
            was_playing = self.is_playing
            if was_playing:
                self.pause()
            self.play_offset_sec = sec
            self._show_frame_at(sec)
            if was_playing:
                self.play()

    def _on_vol_change(self, val):
        v = int(float(val))
        self.vol_lbl.config(text=f"{v}%")
        self.audio.set_volume(v)

    def _tick(self):
        if not self.is_playing:
            return
        elapsed = time.time() - self.play_start_wall
        if elapsed >= self.duration_sec:
            self.stop()
            return
        self.seek_var.set(elapsed)
        self._show_frame_at(elapsed)
        self.after(33, self._tick)


class MovieRecapApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Facebook Video Policy-Shield & Auto Logo Remover Studio (No AI Key Needed)")
        self.geometry("1040x920")
        self.configure(bg="#18181b")
        self.cancel_flag = False
        self.last_split_dir = ""
        self.last_recap_file = ""
        self.caption_idx = 0

        self._build_scrollable_shell()
        self._build_ui()

        if not check_ffmpeg():
            messagebox.showwarning(
                "FFmpeg Missing",
                "FFmpeg မတွေ့ပါ။ ffmpeg.exe နှင့် ffprobe.exe ကို app folder ထဲထည့်ပါ။",
            )

    def _build_scrollable_shell(self):
        self.canvas = tk.Canvas(self, bg="#18181b", highlightthickness=0)
        self.v_scroll = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.v_scroll.set)

        self.v_scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.container = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.container, anchor="nw")

        self.container.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas_window, width=e.width))
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

    def _build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background="#18181b", borderwidth=0)
        style.configure("TNotebook.Tab", background="#27272a", foreground="#e4e4e7", padding=[16, 8], font=("Segoe UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", "#2563eb")], foreground=[("selected", "#ffffff")])
        style.configure("TFrame", background="#18181b")
        style.configure("TLabel", background="#18181b", foreground="#e4e4e7", font=("Segoe UI", 10))
        style.configure("Header.TLabel", font=("Segoe UI", 12, "bold"), foreground="#60a5fa")

        nb = ttk.Notebook(self.container)
        nb.pack(fill="both", expand=True, padx=14, pady=12)

        self.tab2 = ttk.Frame(nb)
        self.tab1 = ttk.Frame(nb)
        nb.add(self.tab2, text="  Step 1: FB URL / Video Policy-Shield & Logo Remover  ")
        nb.add(self.tab1, text="  Step 2: Movie Splitter (ဇာတ်ကားပိုင်းရန်)  ")

        self._build_main_processor()
        self._build_splitter()

        bottom = ttk.Frame(self.container)
        bottom.pack(fill="both", expand=True, padx=14, pady=(0, 16))

        prog_row = ttk.Frame(bottom)
        prog_row.pack(fill="x", pady=(0, 8))
        self.progress_var = tk.DoubleVar(value=0.0)
        self.pbar = ttk.Progressbar(prog_row, variable=self.progress_var, maximum=100)
        self.pbar.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_cancel = tk.Button(
            prog_row, text="Stop", bg="#dc2626", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", padx=12, command=self._request_cancel
        )
        self.btn_cancel.pack(side="right")

        self.player = InAppVideoPlayer(bottom)
        self.player.pack(fill="x", pady=(4, 10))

        # Policy-Safe Facebook Caption Box (No Inauthentic Engagement Bait)
        cap_header = ttk.Frame(bottom)
        cap_header.pack(fill="x", pady=(2, 4))
        ttk.Label(cap_header, text="📢 Facebook Policy-Safe Caption & Hashtags (No Engagement Bait):", foreground="#38bdf8", font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Button(
            cap_header, text="🔄 Next Caption", bg="#4b5563", fg="white",
            font=("Segoe UI", 8, "bold"), relief="flat", padx=10, pady=3, command=self._next_safe_caption
        ).pack(side="right", padx=(6, 0))
        tk.Button(
            cap_header, text="📋 Copy Caption & #", bg="#2563eb", fg="white",
            font=("Segoe UI", 9, "bold"), relief="flat", padx=12, pady=3, command=self._copy_fb_caption
        ).pack(side="right")

        self.caption_box = tk.Text(
            bottom, height=4, bg="#1e1b4b", fg="#f8fafc", font=("Myanmar Text", 10),
            relief="flat", padx=10, pady=8, wrap="word"
        )
        self.caption_box.pack(fill="x", pady=(0, 10))
        self.caption_box.insert("1.0", SAFE_CAPTIONS[0])

        ttk.Label(bottom, text="System Process Log:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))
        self.log_box = tk.Text(
            bottom, height=8, bg="#09090b", fg="#a1a1aa", font=("Consolas", 9),
            relief="flat", padx=8, pady=8
        )
        self.log_box.pack(fill="both", expand=True)

    # ---------------- MAIN TAB: FB URL OR LOCAL FILE -> POLICY-SAFE VIDEO ----------------
    def _build_main_processor(self):
        f = self.tab2
        ttk.Label(f, text="1. Facebook Video URL or Downloaded File -> Auto Logo Remove + Copyright-Safe Audio + Policy Shield", style="Header.TLabel").pack(anchor="w", pady=(8, 10))

        # Option A: Facebook URL
        r_url = ttk.Frame(f)
        r_url.pack(fill="x", pady=4)
        ttk.Label(r_url, text="Facebook Video URL:", width=19).pack(side="left")
        self.s2_url = tk.StringVar()
        tk.Entry(r_url, textvariable=self.s2_url, bg="#27272a", fg="#38bdf8", insertbackground="white", relief="flat").pack(side="left", fill="x", expand=True, ipady=5, padx=6)
        tk.Button(r_url, text="📋 Paste URL", bg="#4f46e5", fg="white", font=("Segoe UI", 8, "bold"), relief="flat", padx=10, command=self._paste_url).pack(side="right")

        # Option B: Downloaded Video File
        r1 = ttk.Frame(f)
        r1.pack(fill="x", pady=4)
        ttk.Label(r1, text="OR Local Video File:", width=19).pack(side="left")
        self.s2_src = tk.StringVar()
        tk.Entry(r1, textvariable=self.s2_src, bg="#27272a", fg="white", insertbackground="white", relief="flat").pack(side="left", fill="x", expand=True, ipady=5, padx=6)
        tk.Button(r1, text="Browse Video", bg="#3f3f46", fg="white", relief="flat", padx=10, command=self._browse_split_file).pack(side="right")

        # Output Folder
        r_out = ttk.Frame(f)
        r_out.pack(fill="x", pady=4)
        ttk.Label(r_out, text="Output Folder:", width=19).pack(side="left")
        self.s2_out_dir = tk.StringVar()
        tk.Entry(r_out, textvariable=self.s2_out_dir, bg="#27272a", fg="white", insertbackground="white", relief="flat").pack(side="left", fill="x", expand=True, ipady=5, padx=6)
        tk.Button(r_out, text="Choose Folder", bg="#3f3f46", fg="white", relief="flat", padx=10, command=self._browse_recap_out).pack(side="right", padx=(0, 4))
        tk.Button(r_out, text="Open Folder", bg="#16a34a", fg="white", relief="flat", padx=10, command=self._open_recap_folder).pack(side="right")

        # Audio Copyright-Bypass & Volume Boost
        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=6)
        ttk.Label(r3, text="Audio Protection:", width=19).pack(side="left")
        self.s2_audio_mode = tk.StringVar(value=list(AUDIO_MODES.keys())[0])
        ttk.Combobox(r3, textvariable=self.s2_audio_mode, values=list(AUDIO_MODES.keys()), state="readonly", width=44).pack(side="left", padx=(6, 12))

        ttk.Label(r3, text="🔊 Audio Vol:").pack(side="left")
        self.s2_audio_vol = tk.IntVar(value=140)
        self.vol_boost_slider = tk.Scale(
            r3, from_=50, to=300, orient="horizontal", variable=self.s2_audio_vol,
            bg="#18181b", fg="#e4e4e7", troughcolor="#27272a", highlightthickness=0,
            length=120, showvalue=False, command=self._update_vol_boost_label
        )
        self.vol_boost_slider.pack(side="left", padx=(4, 4))
        self.s2_audio_vol_lbl = ttk.Label(r3, text="140%", width=5, foreground="#34d399", font=("Segoe UI", 9, "bold"))
        self.s2_audio_vol_lbl.pack(side="left")

        # Output Size & Watermark
        r4 = ttk.Frame(f)
        r4.pack(fill="x", pady=6)
        ttk.Label(r4, text="Output Size:", width=19).pack(side="left")
        self.s2_size = tk.StringVar(value=list(ASPECT_OPTIONS.keys())[0])
        ttk.Combobox(r4, textvariable=self.s2_size, values=list(ASPECT_OPTIONS.keys()), state="readonly", width=36).pack(side="left", padx=(6, 12))

        ttk.Label(r4, text="My Watermark:").pack(side="left")
        self.s2_wm = tk.StringVar(value="Daily News Mm")
        tk.Entry(r4, textvariable=self.s2_wm, width=18, bg="#27272a", fg="white", insertbackground="white", relief="flat").pack(side="left", ipady=4, padx=(6, 6))

        r_wm2 = ttk.Frame(f)
        r_wm2.pack(fill="x", pady=4)
        ttk.Label(r_wm2, text="Watermark Motion:", width=19).pack(side="left")
        self.s2_wm_motion = tk.StringVar(value=WM_MOTIONS[0])
        ttk.Combobox(r_wm2, textvariable=self.s2_wm_motion, values=WM_MOTIONS, state="readonly", width=28).pack(side="left", padx=(6, 8))

        self.s2_wm_pos = tk.StringVar(value="Top-Right")
        ttk.Combobox(r_wm2, textvariable=self.s2_wm_pos, values=WM_POSITIONS, state="readonly", width=12).pack(side="left", padx=(0, 14))

        # Policy Shield Checkboxes
        r_fb = ttk.Frame(f)
        r_fb.pack(fill="x", pady=6)
        self.s2_auto_logo = tk.BooleanVar(value=True)
        tk.Checkbutton(
            r_fb, text="🔍 Auto-Detect & Remove Original Logos/Watermarks",
            variable=self.s2_auto_logo, bg="#18181b", fg="#f472b6", selectcolor="#27272a",
            activebackground="#18181b", activeforeground="#f9a8d4", font=("Segoe UI", 9, "bold")
        ).pack(side="left", padx=(0, 14))

        self.s2_fb_shield = tk.BooleanVar(value=True)
        tk.Checkbutton(
            r_fb, text="🛡 FB Unoriginal Content & Policy Shield (Blur-Frame + Speed/Color/Hash Shift)",
            variable=self.s2_fb_shield, bg="#18181b", fg="#38bdf8", selectcolor="#27272a",
            activebackground="#18181b", activeforeground="#7dd3fc", font=("Segoe UI", 9, "bold")
        ).pack(side="left", padx=(0, 14))

        self.s2_mirror = tk.BooleanVar(value=False)
        tk.Checkbutton(
            r_fb, text="Flip Mirror (H-Flip)",
            variable=self.s2_mirror, bg="#18181b", fg="#e4e4e7", selectcolor="#27272a",
            activebackground="#18181b", activeforeground="white", font=("Segoe UI", 9)
        ).pack(side="left")

        self.s2_loc_lbl = ttk.Label(f, text="Output File Location: (Will appear here after processing)", foreground="#34d399", font=("Segoe UI", 9, "bold"))
        self.s2_loc_lbl.pack(anchor="w", pady=(6, 6))

        btn_row = ttk.Frame(f)
        btn_row.pack(anchor="w", pady=(2, 6))
        tk.Button(
            btn_row, text="⚡ Process Video (Remove Logo + Bypass Copyright + Add Watermark)",
            bg="#2563eb", fg="white", font=("Segoe UI", 10, "bold"),
            relief="flat", padx=18, pady=8, command=self._start_process
        ).pack(side="left", padx=(0, 10))

        tk.Button(
            btn_row, text="🔄 Refresh / Reset",
            bg="#4b5563", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", padx=14, pady=8, command=self._refresh_app
        ).pack(side="left", padx=(0, 10))

        tk.Button(
            btn_row, text="↗ External Player",
            bg="#3f3f46", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", padx=12, pady=8, command=self._play_recap_file
        ).pack(side="left")

    # ---------------- SPLITTER TAB ----------------
    def _build_splitter(self):
        f = self.tab1
        ttk.Label(f, text="2. Long Movie Splitter (ဘယ်နှပိုင်းခွဲမလဲ / တစ်ပိုင်းဘယ်လောက်ကြာမလဲ ရွေးချယ်ရန်)", style="Header.TLabel").pack(anchor="w", pady=(10, 10))

        r1 = ttk.Frame(f)
        r1.pack(fill="x", pady=5)
        ttk.Label(r1, text="Long Movie File:", width=18).pack(side="left")
        self.s1_src = tk.StringVar()
        tk.Entry(r1, textvariable=self.s1_src, bg="#27272a", fg="white", insertbackground="white", relief="flat").pack(side="left", fill="x", expand=True, ipady=5, padx=6)
        tk.Button(r1, text="Browse Movie", bg="#3f3f46", fg="white", relief="flat", padx=10, command=self._browse_long_movie).pack(side="right")

        r2 = ttk.Frame(f)
        r2.pack(fill="x", pady=5)
        ttk.Label(r2, text="File Output Location:", width=18).pack(side="left")
        self.s1_out = tk.StringVar()
        tk.Entry(r2, textvariable=self.s1_out, bg="#27272a", fg="white", insertbackground="white", relief="flat").pack(side="left", fill="x", expand=True, ipady=5, padx=6)
        tk.Button(r2, text="Choose Folder", bg="#3f3f46", fg="white", relief="flat", padx=10, command=self._browse_split_out).pack(side="right", padx=(0, 4))
        tk.Button(r2, text="Open Folder", bg="#2563eb", fg="white", relief="flat", padx=10, command=self._open_split_folder).pack(side="right")

        r3 = ttk.Frame(f)
        r3.pack(fill="x", pady=6)
        ttk.Label(r3, text="Start Time:", width=18).pack(side="left")
        self.s1_start = tk.StringVar(value="00:00:00")
        e_st = tk.Entry(r3, textvariable=self.s1_start, width=11, bg="#27272a", fg="white", relief="flat")
        e_st.pack(side="left", ipady=4, padx=(6, 16))
        e_st.bind("<KeyRelease>", lambda _e: self._recalc_split_preview())

        ttk.Label(r3, text="End Time:", width=10).pack(side="left")
        self.s1_end = tk.StringVar(value="01:00:00")
        e_en = tk.Entry(r3, textvariable=self.s1_end, width=11, bg="#27272a", fg="white", relief="flat")
        e_en.pack(side="left", ipady=4, padx=(0, 16))
        e_en.bind("<KeyRelease>", lambda _e: self._recalc_split_preview())

        ttk.Label(r3, text="Auto File No Start:", width=18).pack(side="left")
        self.s1_num = tk.StringVar(value="1")
        e_no = tk.Entry(r3, textvariable=self.s1_num, width=6, bg="#27272a", fg="white", relief="flat")
        e_no.pack(side="left", ipady=4, padx=(0, 6))
        e_no.bind("<KeyRelease>", lambda _e: self._recalc_split_preview())
        ttk.Label(r3, text="(Auto output: 1.mp4, 2.mp4, 3.mp4 ...)", foreground="#a1a1aa").pack(side="left")

        r4 = ttk.Frame(f)
        r4.pack(fill="x", pady=8)
        ttk.Label(r4, text="Split Method:", width=18).pack(side="left")
        self.s1_mode = tk.StringVar(value="count")

        rb1 = tk.Radiobutton(
            r4, text="ဘယ်နှပိုင်းခွဲမလဲ (By Total Parts):", variable=self.s1_mode, value="count",
            bg="#18181b", fg="#38bdf8", selectcolor="#27272a", activebackground="#18181b",
            activeforeground="#7dd3fc", font=("Segoe UI", 9, "bold"), command=self._recalc_split_preview
        )
        rb1.pack(side="left", padx=(6, 4))

        self.s1_total_parts = tk.StringVar(value="6")
        e_parts = tk.Entry(r4, textvariable=self.s1_total_parts, width=6, bg="#27272a", fg="white", relief="flat")
        e_parts.pack(side="left", ipady=4, padx=(0, 18))
        e_parts.bind("<KeyRelease>", lambda _e: (self.s1_mode.set("count"), self._recalc_split_preview()))

        rb2 = tk.Radiobutton(
            r4, text="တစ်ပိုင်းကြာချိန် (Mins per Part):", variable=self.s1_mode, value="mins",
            bg="#18181b", fg="#fbbf24", selectcolor="#27272a", activebackground="#18181b",
            activeforeground="#fde68a", font=("Segoe UI", 9, "bold"), command=self._recalc_split_preview
        )
        rb2.pack(side="left", padx=(0, 4))

        self.s1_chunk = tk.StringVar(value="10")
        e_mins = tk.Entry(r4, textvariable=self.s1_chunk, width=6, bg="#27272a", fg="white", relief="flat")
        e_mins.pack(side="left", ipady=4)
        e_mins.bind("<KeyRelease>", lambda _e: (self.s1_mode.set("mins"), self._recalc_split_preview()))

        self.s1_calc_lbl = ttk.Label(
            f,
            text="📊 Split Plan: 6 Parts | Each Part Duration: 00:10:00 (10.0 mins) | Files: 1.mp4 -> 6.mp4",
            foreground="#fbbf24", font=("Segoe UI", 9, "bold")
        )
        self.s1_calc_lbl.pack(anchor="w", pady=(4, 4))

        self.s1_loc_lbl = ttk.Label(f, text="Saved Output Location: (Not split yet)", foreground="#34d399", font=("Segoe UI", 9, "bold"))
        self.s1_loc_lbl.pack(anchor="w", pady=(2, 10))

        tk.Button(
            f, text="✂ Split Movie into Numbered Parts (1.mp4, 2.mp4, 3.mp4 ...)",
            bg="#16a34a", fg="white", font=("Segoe UI", 10, "bold"),
            relief="flat", padx=18, pady=8, command=self._start_split
        ).pack(anchor="w")

    def _recalc_split_preview(self):
        try:
            st = parse_hms(self.s1_start.get())
            en = parse_hms(self.s1_end.get())
            total_sec = max(1.0, en - st)
            start_no = max(1, int(self.s1_num.get() or 1))

            if self.s1_mode.get() == "count":
                parts = max(1, int(self.s1_total_parts.get() or 1))
                part_sec = total_sec / float(parts)
                self.s1_chunk.set(f"{part_sec / 60.0:.2f}")
            else:
                mins = max(0.1, float(self.s1_chunk.get() or 10))
                part_sec = mins * 60.0
                parts = max(1, math.ceil(total_sec / part_sec))
                self.s1_total_parts.set(str(parts))

            end_no = start_no + parts - 1
            self.s1_calc_lbl.config(
                text=f"📊 Split Plan: {parts} Parts | Each Part Duration: {fmt_hms(part_sec)} ({part_sec/60.0:.1f} mins) | Output Files: {start_no}.mp4 to {end_no}.mp4"
            )
        except Exception:
            pass

    # ---------------- ACTIONS ----------------
    def _paste_url(self):
        try:
            clip = self.clipboard_get().strip()
            if clip:
                self.s2_url.set(clip)
                self.s2_src.set("")
                self.log(f"📋 Pasted URL: {clip}")
        except Exception:
            pass

    def _next_safe_caption(self):
        self.caption_idx = (self.caption_idx + 1) % len(SAFE_CAPTIONS)
        self.caption_box.delete("1.0", "end")
        self.caption_box.insert("1.0", SAFE_CAPTIONS[self.caption_idx])

    def _refresh_app(self):
        self.cancel_flag = True
        self.player.reset_player()
        self.progress_var.set(0.0)
        self.s2_url.set("")
        self.s2_src.set("")
        self.log_box.delete("1.0", "end")
        self.s2_loc_lbl.config(text="Output File Location: (Refreshed - Ready for new Facebook URL or Video)")
        self.log("🔄 Refreshed! Paste a Facebook URL or select a downloaded video file.")

    def _copy_fb_caption(self):
        txt = self.caption_box.get("1.0", "end").strip()
        if not txt:
            return
        self.clipboard_clear()
        self.clipboard_append(txt)
        self.update()
        messagebox.showinfo("Copied", "Facebook Policy-Safe Caption နှင့် Hashtags (#) ကို Copy ကူးပြီးပါပြီ!")

    def _update_vol_boost_label(self, val):
        self.s2_audio_vol_lbl.config(text=f"{int(float(val))}%")

    def log(self, msg):
        self.after(0, lambda: (self.log_box.insert("end", msg + "\n"), self.log_box.see("end")))

    def set_progress(self, val):
        self.after(0, lambda: self.progress_var.set(val * 100))

    def _request_cancel(self):
        self.cancel_flag = True
        self.log("⏹ Cancelling task...")

    def _browse_long_movie(self):
        p = filedialog.askopenfilename(filetypes=[("Video Files", "*.mp4 *.mkv *.avi *.mov *.ts")])
        if not p:
            return
        self.s1_src.set(p)
        default_out = os.path.join(os.path.dirname(p), os.path.splitext(os.path.basename(p))[0] + "_splits")
        self.s1_out.set(default_out)
        try:
            info = probe(p)
            self.s1_start.set("00:00:00")
            self.s1_end.set(fmt_hms(info["duration"]))
            self._recalc_split_preview()
            self.log(f"Loaded movie ({fmt_hms(info['duration'])}, {info['width']}x{info['height']})")
        except Exception as e:
            self.log(f"Probe error: {e}")

    def _browse_split_out(self):
        d = filedialog.askdirectory()
        if d:
            self.s1_out.set(d)

    def _open_split_folder(self):
        d = self.s1_out.get().strip() or self.last_split_dir
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo("Folder", "Split folder မရှိသေးပါ။")

    def _browse_split_file(self):
        init_dir = self.s1_out.get().strip() or os.getcwd()
        p = filedialog.askopenfilename(initialdir=init_dir, filetypes=[("Video Files", "*.mp4 *.mkv *.mov *.avi")])
        if p:
            self.s2_src.set(p)
            self.s2_url.set("")
            if not self.s2_out_dir.get().strip():
                self.s2_out_dir.set(os.path.dirname(p))
            try:
                info = probe(p)
                self.log(f"Selected video: {os.path.basename(p)} (Duration: {fmt_hms(info['duration'])})")
            except Exception:
                pass

    def _browse_recap_out(self):
        d = filedialog.askdirectory()
        if d:
            self.s2_out_dir.set(d)

    def _open_recap_folder(self):
        if self.last_recap_file and os.path.isfile(self.last_recap_file):
            os.startfile(os.path.dirname(self.last_recap_file))
            return
        d = self.s2_out_dir.get().strip() or os.path.dirname(self.s2_src.get().strip())
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo("Folder", "Output folder မရှိသေးပါ။")

    def _play_recap_file(self):
        if self.last_recap_file and os.path.isfile(self.last_recap_file):
            self.player.pause()
            os.startfile(self.last_recap_file)
        else:
            messagebox.showinfo("Video", "ထုတ်ပြီးသား ဗီဒီယို မရှိသေးပါ။")

    def _start_split(self):
        src = self.s1_src.get().strip()
        out_dir = self.s1_out.get().strip()
        if not os.path.isfile(src):
            messagebox.showerror("Error", "Long movie file ရွေးပေးပါ။")
            return
        self.cancel_flag = False
        self.set_progress(0)

        def worker():
            try:
                st = parse_hms(self.s1_start.get())
                en = parse_hms(self.s1_end.get())
                total_sec = max(1.0, en - st)
                start_no = max(1, int(self.s1_num.get() or 1))

                if self.s1_mode.get() == "count":
                    parts = max(1, int(self.s1_total_parts.get() or 1))
                    chunk_sec = total_sec / float(parts)
                else:
                    chunk_sec = max(5.0, float(self.s1_chunk.get() or 10) * 60.0)

                self.log(f"✂ Splitting {fmt_hms(st)} -> {fmt_hms(en)} | Each part: {fmt_hms(chunk_sec)} | Starting at {start_no}.mp4...")
                files = split_video_range(
                    src, out_dir, st, en, chunk_sec, start_num=start_no,
                    log=self.log, cancelled=lambda: self.cancel_flag
                )
                self.last_split_dir = out_dir
                end_no = start_no + len(files) - 1
                self.after(0, lambda: self.s1_loc_lbl.config(
                    text=f"✅ Saved {len(files)} Files ({start_no}.mp4 -> {end_no}.mp4) at: {out_dir}"
                ))
                if files:
                    self.after(0, lambda: self.s2_src.set(files[0]))
                    self.after(0, lambda: self.s2_out_dir.set(out_dir))
                self.set_progress(1.0)
                self.log(f"🎉 Split ပြီးပါပြီ! ဖိုင် {len(files)} ခုကို {out_dir} တွင် သိမ်းဆည်းပြီးပါပြီ။")
            except Stopped:
                self.log("⏹ Split stopped by user.")
            except Exception as e:
                self.log(f"❌ Error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _start_process(self):
        fb_url = self.s2_url.get().strip()
        local_src = self.s2_src.get().strip()

        if not fb_url and not os.path.isfile(local_src):
            messagebox.showerror("Input Missing", "Facebook Video URL ထည့်ပါ (သို့မဟုတ်) Downloaded Video File ရွေးပေးပါ။")
            return

        self.player.stop()
        out_dir = self.s2_out_dir.get().strip()
        if not out_dir:
            out_dir = os.path.dirname(local_src) if os.path.isfile(local_src) else os.path.join(os.path.expanduser("~"), "Downloads", "FB_PolicySafe_Videos")
            self.s2_out_dir.set(out_dir)

        auto_logo = bool(self.s2_auto_logo.get())
        fb_shield = bool(self.s2_fb_shield.get())
        mirror_vid = bool(self.s2_mirror.get())
        pitch_shift = AUDIO_MODES[self.s2_audio_mode.get()]
        audio_vol = int(self.s2_audio_vol.get())
        wm_motion = self.s2_wm_motion.get()
        wm_pos = self.s2_wm_pos.get()
        wm_text = self.s2_wm.get()

        self.cancel_flag = False
        self.set_progress(0.05)

        def worker():
            try:
                t_start = time.time()
                os.makedirs(out_dir, exist_ok=True)
                workdir = tempfile.mkdtemp(prefix="fb_shield_")

                if fb_url:
                    src = download_facebook_video(fb_url, workdir, log=self.log)
                else:
                    src = local_src

                self.set_progress(0.20)
                info = probe(src)
                in_dur = info["duration"]

                size_choice = ASPECT_OPTIONS[self.s2_size.get()]
                target_size = size_choice if size_choice else (info["width"] - info["width"] % 2, info["height"] - info["height"] % 2)
                tw, th = target_size

                # 1. Auto-detect & erase static logos/watermarks
                delogo_cmds = detect_static_logos(src, log=self.log) if auto_logo else []
                self.set_progress(0.32)

                # 2. Generate Moving Watermark ASS script
                ass_path = os.path.join(workdir, "watermark.ass")
                write_watermark_ass(ass_path, tw, th, in_dur, watermark_text=wm_text, wm_pos=wm_pos, wm_motion=wm_motion)

                base_name = os.path.splitext(os.path.basename(src))[0]
                final_out = os.path.join(out_dir, f"{base_name}_FB_Safe.mp4")

                self.log("🛡 Applying FB Policy Shield + Auto Logo Removal + Copyright-Safe Audio Transform...")
                transform_video_policy_safe(
                    src, ass_path, final_out, info, target_size,
                    delogo_cmds=delogo_cmds,
                    fb_shield=fb_shield,
                    mirror_video=mirror_vid,
                    audio_pitch_shift=pitch_shift,
                    audio_volume_pct=audio_vol,
                    on_progress=lambda p: self.set_progress(0.32 + p * 0.67),
                    cancelled=lambda: self.cancel_flag,
                )

                elapsed = time.time() - t_start
                self.last_recap_file = final_out
                self.set_progress(1.0)
                self.after(0, lambda: self.s2_loc_lbl.config(text=f"✅ Saved FB Policy-Safe Video in {elapsed:.0f}s: {final_out}"))
                self.log(f"✅ DONE in {elapsed:.1f}s! Playing Preview...")
                self.after(0, lambda: self.player.load_video(final_out, autoplay=True))

            except Stopped:
                self.log("⏹ Cancelled by user.")
            except Exception as e:
                self.log(f"❌ Error: {e}")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    app = MovieRecapApp()
    app.mainloop()
