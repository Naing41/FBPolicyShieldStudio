import json
import os
import subprocess
import sys

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
    manual_blur_boxes=None,
    fb_shield=True,
    mirror_video=False,
    audio_pitch_shift=1.035,
    audio_volume_pct=135,
    on_progress=None, cancelled=None, bitrate="192k"
):
    tw, th = target_size
    sw, sh = info["width"], info["height"]
    total_dur = max(0.5, info["duration"])
    escaped_ass = ass_path.replace("\\", "/").replace(":", "\\:")

    v_steps = []

    if manual_blur_boxes:
        for idx, (nx1, ny1, nx2, ny2) in enumerate(manual_blur_boxes):
            x = max(0, min(sw - 8, int(round(nx1 * sw))))
            y = max(0, min(sh - 8, int(round(ny1 * sh))))
            bw = max(8, min(sw - x, int(round((nx2 - nx1) * sw))))
            bh = max(8, min(sh - y, int(round((ny2 - ny1) * sh))))
            v_steps.append(
                f"split[base_{idx}][crop_{idx}];"
                f"[crop_{idx}]crop={bw}:{bh}:{x}:{y},boxblur=18:3[blur_{idx}];"
                f"[base_{idx}][blur_{idx}]overlay={x}:{y}"
            )

    if mirror_video:
        v_steps.append("hflip")

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
