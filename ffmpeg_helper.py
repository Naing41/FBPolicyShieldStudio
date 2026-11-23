import json
import os
import subprocess
import sys

import cv2
import numpy as np
import yt_dlp

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Stopped(Exception):
    pass


def _find(name):
    exe = name + (".exe" if os.name == "nt" else "")
    here = [
        getattr(sys, "_MEIPASS", ""),
        os.path.dirname(sys.executable),
        os.path.dirname(os.path.abspath(sys.argv[0])),
        os.getcwd(),
    ]
    for d in here:
        if d and os.path.isfile(os.path.join(d, exe)):
            return os.path.join(d, exe)
        if d and os.path.isfile(os.path.join(d, "ffmpeg", exe)):
            return os.path.join(d, "ffmpeg", exe)
    return name


FFMPEG, FFPROBE = _find("ffmpeg"), _find("ffprobe")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, creationflags=NOWIN, **kw)


def check_ffmpeg():
    try:
        return _run([FFMPEG, "-version"]).returncode == 0
    except OSError:
        return False


def download_facebook_video(url, out_dir, log=None):
    """Downloads a Facebook Video / Reels URL in best MP4 quality using yt-dlp."""
    os.makedirs(out_dir, exist_ok=True)
    if log:
        log(f"⬇ Downloading Facebook Video from URL: {url}")

    ffmpeg_dir = os.path.dirname(FFMPEG) if os.path.isfile(FFMPEG) else None
    out_tmpl = os.path.join(out_dir, "fb_download_%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": out_tmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if ffmpeg_dir:
        ydl_opts["ffmpeg_location"] = ffmpeg_dir

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        base, _ = os.path.splitext(filename)
        mp4_path = base + ".mp4"
        final_file = mp4_path if os.path.isfile(mp4_path) else filename

    if not os.path.isfile(final_file):
        raise IOError("Facebook ဗီဒီယို Download ဆွဲမရပါ။ Link မှန်/မမှန် ပြန်စစ်ပေးပါ။")

    if log:
        log(f"✅ Downloaded FB Video: {final_file}")
    return final_file


def probe(path):
    r = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    if r.returncode != 0:
        raise IOError("ဗီဒီယို ဖတ်မရပါ: " + r.stderr.decode("utf-8", "replace")[-200:])
    j = json.loads(r.stdout)
    v = next((s for s in j["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise IOError("ဗီဒီယို stream မတွေ့ပါ")
    dur = float(j["format"].get("duration") or v.get("duration") or 0)
    if dur <= 0:
        raise IOError("ဗီဒီယို အရှည် ဖတ်မရပါ")
    num, _, den = (v.get("avg_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    if not 5 <= fps <= 120:
        fps = 30.0
    return {"duration": dur, "width": int(v["width"]), "height": int(v["height"]), "fps": fps}


def split_video_range(src_path, out_dir, start_sec, end_sec, chunk_sec, start_num=1, log=None, cancelled=None):
    os.makedirs(out_dir, exist_ok=True)
    outputs = []
    cur = start_sec
    idx = start_num
    while cur < end_sec - 0.5:
        if cancelled and cancelled():
            raise Stopped()
        dur = min(chunk_sec, end_sec - cur)
        out_file = os.path.join(out_dir, f"{idx}.mp4")
        cmd = [
            FFMPEG, "-y", "-v", "error",
            "-ss", f"{cur:.3f}",
            "-i", src_path,
            "-t", f"{dur:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            out_file,
        ]
        r = _run(cmd)
        if r.returncode != 0:
            raise IOError(f"Split မအောင်ပါ ({idx}.mp4): " + r.stderr.decode("utf-8", "replace")[-200:])
        outputs.append(out_file)
        if log:
            log(f"✅ Created: {out_file}")
        cur += dur
        idx += 1
    return outputs


def detect_static_logos(video_path, log=None):
    """
    Automatically scans frames across the video to detect static watermarks/logos
    in any corner and returns FFmpeg delogo filters to erase them cleanly.
    """
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if total < 10 or w == 0 or h == 0:
        cap.release()
        return []

    sample_indices = np.linspace(int(total * 0.05), int(total * 0.90), 14, dtype=int)
    edge_acc = np.zeros((h, w), dtype=np.float32)
    valid = 0

    for idx in sample_indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 90, 200).astype(np.float32) / 255.0
        edge_acc += edges
        valid += 1
    cap.release()

    if valid < 5:
        return []

    persistent = (edge_acc / valid >= 0.65).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (19, 13))
    closed = cv2.morphologyEx(persistent, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    delogo_cmds = []

    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < 26 or bh < 12 or bw > w * 0.45 or bh > h * 0.24:
            continue
        in_top = y < h * 0.30
        in_bottom = (y + bh) > h * 0.76
        if not (in_top or in_bottom):
            continue

        pad = 8
        x1 = max(2, x - pad)
        y1 = max(2, y - pad)
        x2 = min(w - 2, x + bw + pad)
        y2 = min(h - 2, y + bh + pad)
        fw = x2 - x1
        fh = y2 - y1
        if fw > 10 and fh > 10:
            delogo_cmds.append(f"delogo=x={x1}:y={y1}:w={fw}:h={fh}:show=0")

    if log:
        if delogo_cmds:
            log(f"🔍 Auto-Detected {len(delogo_cmds)} static logo/watermark zone(s) -> Removing automatically!")
        else:
            log("🔍 No fixed corner logo detected.")
    return delogo_cmds


def extract_preview_wav(video_path, out_wav):
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-i", video_path, "-vn",
        "-acodec", "pcm_s16le", "-ac", "2", "-ar", "44100",
        out_wav,
    ]
    r = _run(cmd)
    if r.returncode != 0:
        raise IOError("Preview audio ထုတ်မရပါ: " + r.stderr.decode("utf-8", "replace")[-200:])
    return out_wav


def _fmt_ass_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_watermark_ass(ass_path, width, height, duration_sec, watermark_text="", wm_pos="Top-Right", wm_motion="Diagonal Bounce (တစ်စောင်းပြေး)"):
    wm_size = max(22, int(height * 0.032))
    align_map = {
        "Top-Left": 7,
        "Top-Right": 9,
        "Bottom-Left": 1,
        "Bottom-Right": 3,
    }
    wm_align = align_map.get(wm_pos, 9)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Watermark,Segoe UI,{wm_size},&H55FFFFFF,&H000000FF,&H77000000,&H00000000,-1,0,0,0,100,100,1,0,1,2.0,0,{wm_align},24,24,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    wm_clean = watermark_text.replace("\n", " ").strip()
    if wm_clean and duration_sec > 0:
        cycle = 14.0
        t = 0.0
        step = 0
        while t < duration_sec:
            t_next = min(duration_sec + 0.5, t + cycle)
            if "Vertical" in wm_motion:
                x = int(width * 0.82) if "Right" in wm_pos else int(width * 0.18)
                tag = f"{{\\an5\\move({x},{height - 40},{x},40)}}"
                lines.append(f"Dialogue: 1,{_fmt_ass_time(t)},{_fmt_ass_time(t_next)},Watermark,,0,0,0,,{tag}{wm_clean}\n")
            elif "Horizontal" in wm_motion:
                y = int(height * 0.12) if "Top" in wm_pos else int(height * 0.88)
                tag = f"{{\\an5\\move({width - 50},{y},50,{y})}}"
                lines.append(f"Dialogue: 1,{_fmt_ass_time(t)},{_fmt_ass_time(t_next)},Watermark,,0,0,0,,{tag}{wm_clean}\n")
            elif "Diagonal" in wm_motion:
                if step % 2 == 0:
                    tag = f"{{\\an5\\move(90,{height - 60},{width - 90},60)}}"
                else:
                    tag = f"{{\\an5\\move({width - 90},{height - 60},90,60)}}"
                lines.append(f"Dialogue: 1,{_fmt_ass_time(t)},{_fmt_ass_time(t_next)},Watermark,,0,0,0,,{tag}{wm_clean}\n")
            else:
                lines.append(f"Dialogue: 1,0:00:00.00,{_fmt_ass_time(duration_sec + 1.0)},Watermark,,0,0,0,,{wm_clean}\n")
                break
            t += cycle
            step += 1

    with open(ass_path, "w", encoding="utf-8-sig") as f:
        f.writelines(lines)


def transform_video_policy_safe(
    source, ass_path, out_path, info, target_size,
    delogo_cmds=None,
    fb_shield=True,
    mirror_video=False,
    audio_pitch_shift=1.035,
    audio_volume_pct=135,
    on_progress=None, cancelled=None, bitrate="192k"
):
    """
    Single-Pass Facebook Policy-Safe Re-Creator:
    1. Auto-removes original static logos/watermarks (delogo).
    2. Applies Facebook Unoriginal Content Policy Shield:
       - Transformative Blur-Frame Canvas (90% crisp foreground + blurred ambient background)
       - Color/contrast/saturation fingerprint shift + subtle speed adjustment (1.02x)
       - Complete metadata stripping (-map_metadata -1)
    3. Transforms original audio against Facebook Copyright / ContentID:
       - Pitch & formant shift + micro-tempo shift + EQ frequency reshaping + stereo phase width + dynaudnorm
    4. Burns user's custom Moving Watermark.
    """
    tw, th = target_size
    total_dur = max(0.5, info["duration"])
    escaped_ass = ass_path.replace("\\", "/").replace(":", "\\:")

    v_steps = []
    if delogo_cmds:
        v_steps.extend(delogo_cmds)

    if mirror_video:
        v_steps.append("hflip")

    # Subtle 1.02x speed shift alters frame hashes without noticeable speed change
    speed_factor = 1.02 if fb_shield else 1.0
    v_steps.append(f"setpts=PTS/{speed_factor:.4f}")
    prefix = ",".join(v_steps) + ","

    if fb_shield:
        fw = int((tw * 0.90) // 2 * 2)
        fh = int((th * 0.90) // 2 * 2)
        vf = (
            f"[0:v]{prefix}"
            f"eq=contrast=1.04:saturation=1.07:brightness=0.01,"
            f"split[bg][fg];"
            f"[bg]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=20:3[bg_blur];"
            f"[fg]crop=iw*0.95:ih*0.95:iw*0.025:ih*0.025,"
            f"scale={fw}:{fh}:force_original_aspect_ratio=decrease:flags=fast_bilinear[fg_scaled];"
            f"[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,"
            f"ass='{escaped_ass}',format=yuv420p[vout]"
        )
    else:
        vf = (
            f"[0:v]{prefix}"
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,"
            f"ass='{escaped_ass}',format=yuv420p[vout]"
        )

    # Audio Copyright-Bypass Chain: shifts pitch/formant + tempo + EQ + stereo image so ContentID cannot match
    vol_gain = round(max(0.5, audio_volume_pct / 100.0), 2)
    tempo_comp = round(speed_factor / audio_pitch_shift, 4)
    af = (
        f"[0:a]aresample=44100,"
        f"asetrate=44100*{audio_pitch_shift:.4f},aresample=44100,atempo={tempo_comp:.4f},"
        f"equalizer=f=250:t=q:w=1:g=2,equalizer=f=3200:t=q:w=1:g=-2,"
        f"stereotools=mlev=0.85:slev=1.15,"
        f"dynaudnorm=f=150:g=12:p=0.92,volume={vol_gain},alimiter=limit=0.95[aout]"
    )

    filter_complex = vf + ";" + af

    cmd = [
        FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
        "-i", source,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-map_metadata", "-1",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "fastdecode", "-crf", "22", "-threads", "0",
        "-c:a", "aac", "-b:a", bitrate,
        "-shortest", "-movflags", "+faststart",
        out_path,
    ]

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
    try:
        for line in p.stdout:
            if cancelled and cancelled():
                p.kill()
                raise Stopped()
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("out_time_us=") or s.startswith("out_time_ms="):
                try:
                    sec = float(s.split("=")[1]) / 1e6
                    if on_progress:
                        on_progress(min(1.0, max(0.0, sec / (total_dur / speed_factor))))
                except ValueError:
                    pass
        p.wait()
    finally:
        if p.poll() is None:
            p.kill()

    if p.returncode != 0:
        err_msg = p.stderr.read().decode("utf-8", "replace")[-400:]
        raise IOError("Video ထုတ်ရာတွင် Error တက်ပါသည်: " + err_msg)
    return out_path
