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
    streams = j.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise IOError("ဗီဒီယို stream မတွေ့ပါ")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
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
    return {
        "duration": dur,
        "width": int(v["width"]),
        "height": int(v["height"]),
        "fps": fps,
        "has_audio": has_audio,
    }


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


def write_watermark_ass(ass_path, width, height, duration_sec, watermark_text="", wm_pos="Top-Right", wm_motion="Diagonal Bounce (တစ်စောင်းပြေး)"):
    # Save watermark metadata as JSON so drawtext renders natively without libass fontconfig issues
    meta = {
        "text": watermark_text.replace("\n", " ").strip(),
        "pos": wm_pos,
        "motion": wm_motion,
        "height": height,
    }
    with open(ass_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _build_drawtext_filter(ass_path, th):
    wm_text = "Daily News Mm"
    wm_pos = "Top-Right"
    wm_motion = "Diagonal"
    try:
        with open(ass_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
            wm_text = meta.get("text", "").strip()
            wm_pos = meta.get("pos", "Top-Right")
            wm_motion = meta.get("motion", "Diagonal")
    except Exception:
        pass

    if not wm_text:
        return ""

    # Escape special characters for FFmpeg drawtext
    safe_txt = wm_text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "")
    font_size = max(18, int(th * 0.028))

    win_font = "C\\:/Windows/Fonts/segoeui.ttf"
    font_opt = f"fontfile='{win_font}':" if os.path.isfile("C:/Windows/Fonts/segoeui.ttf") else ""

    if "Vertical" in wm_motion:
        x_expr = "w-tw-24" if "Right" in wm_pos else "24"
        y_expr = "h-th-mod(t*45\\,h-th-40)-20"
    elif "Horizontal" in wm_motion:
        x_expr = "w-tw-mod(t*60\\,w-tw-40)-20"
        y_expr = "24" if "Top" in wm_pos else "h-th-24"
    elif "Diagonal" in wm_motion:
        x_expr = "20+abs(mod(t*55\\,2*(w-tw-40))-(w-tw-40))"
        y_expr = "20+abs(mod(t*40\\,2*(h-th-40))-(h-th-40))"
    else:
        x_expr = "w-tw-24" if "Right" in wm_pos else "24"
        y_expr = "24" if "Top" in wm_pos else "h-th-24"

    return (
        f",drawtext={font_opt}text='{safe_txt}':fontsize={font_size}:"
        f"fontcolor=white@0.45:borderw=1.5:bordercolor=black@0.45:x={x_expr}:y={y_expr}"
    )


def transform_video_policy_safe(
    source, ass_path, out_path, info, target_size,
    manual_blur_boxes=None,
    fb_shield=True,
    mirror_video=False,
    audio_pitch_shift=1.035,
    audio_volume_pct=135,
    on_progress=None, cancelled=None, bitrate="192k"
):
    tw = max(16, int(target_size[0]) // 2 * 2)
    th = max(16, int(target_size[1]) // 2 * 2)
    sw, sh = info["width"], info["height"]
    has_audio = info.get("has_audio", True)
    total_dur = max(0.5, info["duration"])

    v_steps = ["format=yuv420p"]

    if manual_blur_boxes:
        for idx, (nx1, ny1, nx2, ny2) in enumerate(manual_blur_boxes):
            x = max(0, min(sw - 16, int(round(nx1 * sw)) // 2 * 2))
            y = max(0, min(sh - 16, int(round(ny1 * sh)) // 2 * 2))
            bw = max(16, min(sw - x, int(round((nx2 - nx1) * sw)) // 2 * 2))
            bh = max(16, min(sh - y, int(round((ny2 - ny1) * sh)) // 2 * 2))
            # gblur never throws Invalid argument on small boxes
            v_steps.append(
                f"split[base_{idx}][crop_{idx}];"
                f"[crop_{idx}]crop={bw}:{bh}:{x}:{y},gblur=sigma=12[blur_{idx}];"
                f"[base_{idx}][blur_{idx}]overlay={x}:{y}"
            )

    if mirror_video:
        v_steps.append("hflip")

    speed_factor = 1.02 if fb_shield else 1.0
    v_steps.append(f"setpts=PTS/{speed_factor:.4f}")
    prefix = ",".join(v_steps) + ","
    wm_filter = _build_drawtext_filter(ass_path, th)

    if fb_shield:
        fw = max(16, int((tw * 0.90) // 2 * 2))
        fh = max(16, int((th * 0.90) // 2 * 2))
        vf = (
            f"[0:v]{prefix}"
            f"eq=contrast=1.04:saturation=1.07:brightness=0.01,"
            f"split[bg][fg];"
            f"[bg]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},gblur=sigma=18[bg_blur];"
            f"[fg]crop=iw*0.95:ih*0.95:iw*0.025:ih*0.025,"
            f"scale={fw}:{fh}:force_original_aspect_ratio=decrease:flags=fast_bilinear[fg_scaled];"
            f"[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2"
            f"{wm_filter},format=yuv420p[vout]"
        )
    else:
        vf = (
            f"[0:v]{prefix}"
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black"
            f"{wm_filter},format=yuv420p[vout]"
        )

    vol_gain = round(max(0.5, audio_volume_pct / 100.0), 2)
    tempo_comp = round(speed_factor / audio_pitch_shift, 4)

    if has_audio:
        af = (
            f"[0:a]aresample=44100,"
            f"asetrate=44100*{audio_pitch_shift:.4f},aresample=44100,atempo={tempo_comp:.4f},"
            f"equalizer=f=250:t=q:w=1:g=2,equalizer=f=3200:t=q:w=1:g=-2,"
            f"stereotools=mlev=0.85:slev=1.15,"
            f"dynaudnorm=f=150:g=12:p=0.92,volume={vol_gain},alimiter=limit=0.95[aout]"
        )
    else:
        af = f"anullsrc=r=44100:cl=stereo,atrim=0:{total_dur / speed_factor:.3f}[aout]"

    filter_complex = vf + ";" + af

    cmd = [
        FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
        "-i", source,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-map_metadata", "-1",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "0",
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
        err_msg = p.stderr.read().decode("utf-8", "replace")[-500:]
        raise IOError("Video ထုတ်ရာတွင် Error တက်ပါသည်: " + err_msg)
    return out_path
