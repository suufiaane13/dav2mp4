#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convertisseur DAV -> MP4
========================
Convertit des fichiers .dav (DVR Dahua) en .mp4 compatible Windows 10/11.

- Remux rapide si H.264/MPEG-4.
- Re-encodage H.264 si HEVC/H.265 ou autre.
- GPU auto : NVIDIA NVENC / Intel Quick Sync / AMD AMF.
- Fallback CPU si GPU echoue.
- Progression temps reel.
- Validation MP4 avec ffprobe.
- Annulation propre.
"""

import os
import re
import sys
import json
import shutil
import subprocess
import threading
import queue
import datetime
import platform
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ======================================================================
# 1. CONSTANTES
# ======================================================================

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

NATIVE_VIDEO_CODECS = {"h264", "mpeg4"}
NATIVE_AUDIO_CODECS = {"aac", "mp3", "ac3"}

GPU_ENCODER_ORDER = ["h264_nvenc", "h264_qsv", "h264_amf"]

GPU_ENCODE_ARGS = {
    "h264_nvenc": [
        "-c:v", "h264_nvenc", "-preset", "p5",
        "-rc", "vbr", "-cq", "20", "-b:v", "0",
        "-pix_fmt", "yuv420p",
    ],
    "h264_qsv": [
        "-c:v", "h264_qsv", "-preset", "fast",
        "-global_quality", "20", "-pix_fmt", "nv12",
    ],
    "h264_amf": [
        "-c:v", "h264_amf", "-quality", "speed",
        "-rc", "cqp", "-qp_i", "20", "-qp_p", "22",
        "-pix_fmt", "yuv420p",
    ],
}

GPU_ENCODER_LABELS = {
    "h264_nvenc": "NVIDIA NVENC",
    "h264_qsv": "Intel Quick Sync",
    "h264_amf": "AMD AMF",
}

CPU_FAST_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast",
    "-crf", "21", "-pix_fmt", "yuv420p",
]

CPU_QUALITY_ARGS = [
    "-c:v", "libx264", "-preset", "medium",
    "-crf", "20", "-pix_fmt", "yuv420p",
]

ENCODER_MODES = {
    "Auto (recommande)": "auto",
    "Forcer GPU": "gpu",
    "CPU rapide": "cpu_fast",
    "CPU qualite max": "cpu_quality",
}

DEFAULT_ENCODER_MODE = "Auto (recommande)"


# ======================================================================
# 2. UTILITAIRES
# ======================================================================

def _get_bundle_dir():
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _find_tool(exe_name):
    exe = exe_name + (".exe" if os.name == "nt" else "")
    bundle = _get_bundle_dir()
    for subdir in ("bin", ""):
        local = os.path.join(bundle, subdir, exe)
        if os.path.isfile(local):
            return local
    path = shutil.which(exe)
    if path:
        return path
    path = shutil.which(exe_name)
    if path:
        return path
    return None


def _fmt_duration(seconds):
    if seconds is None or seconds <= 0:
        return "??:??"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    s = int(seconds) % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_size(path):
    try:
        b = os.path.getsize(path)
    except OSError:
        return "?"
    return _fmt_size_bytes(b)


def _fmt_size_bytes(b):
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.0f} Mo"
    if b >= 1024:
        return f"{b / 1024:.0f} Ko"
    return f"{b} o"


def _parse_ffmpeg_time(line):
    m = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", line)
    if not m:
        return None
    try:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except ValueError:
        return None


def _format_cmd(cmd):
    parts = []
    for c in cmd:
        if " " in c:
            parts.append(f'"{c}"')
        else:
            parts.append(c)
    return " ".join(parts)


FFMPEG_PATH = _find_tool("ffmpeg")
FFPROBE_PATH = _find_tool("ffprobe")


# ======================================================================
# 3. FFPROBE
# ======================================================================

def probe_streams(path):
    result = {
        "video_codec": None, "audio_codec": None,
        "has_audio": False, "video_res": None,
        "duration": None, "width": 0, "height": 0,
    }
    if FFPROBE_PATH is None:
        return result
    try:
        proc = subprocess.run(
            [
                FFPROBE_PATH, "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW, timeout=30,
        )
        data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except Exception:
        return result
    for s in data.get("streams", []):
        ct = s.get("codec_type", "")
        if ct == "video" and result["video_codec"] is None:
            result["video_codec"] = s.get("codec_name")
            result["width"] = s.get("width", 0)
            result["height"] = s.get("height", 0)
            result["video_res"] = f"{result['width']}x{result['height']}"
        elif ct == "audio" and result["audio_codec"] is None:
            result["audio_codec"] = s.get("codec_name")
            result["has_audio"] = True
    fmt = data.get("format", {})
    dur = fmt.get("duration")
    if dur:
        try:
            result["duration"] = float(dur)
        except (ValueError, TypeError):
            pass
    return result


# ======================================================================
# 4. GPU DETECTION
# ======================================================================

def detect_gpu_encoder():
    if FFMPEG_PATH is None:
        return None, None
    for codec in GPU_ENCODER_ORDER:
        try:
            r = subprocess.run(
                [
                    FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
                    "-y", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
                    "-c:v", codec, "-frames:v", "5", "-f", "null", "-",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW, timeout=15,
            )
            if r.returncode == 0:
                return codec, GPU_ENCODER_LABELS[codec]
        except Exception:
            continue
    return None, None


def resolve_encoder(mode, log_cb):
    if mode == "cpu_quality":
        return list(CPU_QUALITY_ARGS), "CPU (qualite max, x264 preset medium)"
    if mode == "cpu_fast":
        return list(CPU_FAST_ARGS), "CPU rapide (x264 preset veryfast)"
    log_cb("log", "\n=== Detection GPU en cours... ===")
    codec, label = detect_gpu_encoder()
    if codec:
        log_cb("log", f"GPU detecte : {label} ({codec})")
        return list(GPU_ENCODE_ARGS[codec]), f"GPU ({label})"
    log_cb("log", "Aucun GPU compatible - bascule CPU rapide.")
    return list(CPU_FAST_ARGS), "CPU rapide (x264 preset veryfast)"


# ======================================================================
# 5. FFMPEG RUNNER
# ======================================================================

def run_ffmpeg(cmd, log_cb, cancel_event=None, total_duration=None):
    log_cb("log", f"\nCommande FFmpeg :\n{_format_cmd(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, bufsize=1,
        )
    except Exception as exc:
        log_cb("log", f"Erreur lancement FFmpeg : {exc}")
        return -1
    log_cb("log", "Conversion en cours...")
    last_pct = -1
    last_speed = None
    error_lines = []
    while True:
        if cancel_event and cancel_event.is_set():
            log_cb("log", "Arret de FFmpeg...")
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            return -1
        try:
            line = proc.stderr.readline()
        except Exception as exc:
            log_cb("log", f"Lecture impossible : {exc}")
            break
        if not line:
            if proc.poll() is not None:
                break
            continue
        line = line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        lower = line.lower()
        # Banner info : skip
        banner = (
            "stream #", "output #", "input #", "side data",
            "cpb", "encoder", "metadata", "libsw", "configuration:",
            "dimensions:", "pixel format:", "timebase:", "avg frame rate:",
            "bit rate:", "default group", "locator",
        )
        if any(lower.startswith(p) for p in banner):
            continue
        # Progression
        if total_duration and total_duration > 0:
            ct = _parse_ffmpeg_time(line)
            if ct is not None:
                pct = max(0, min(int(ct / total_duration * 100), 100))
                sm = re.search(r"speed=\s*([\d.]+)x", line)
                speed = sm.group(1) if sm else None
                if pct != last_pct or speed != last_speed:
                    last_pct = pct
                    last_speed = speed
                    log_cb("progress_pct", pct, speed)
                continue
        # Erreurs
        err_kw = (
            "error", "failed", "failure", "invalid", "unable",
            "cannot", "no such", "not found", "non-monotonic", "corrupt",
        )
        if any(k in lower for k in err_kw):
            error_lines.append(line)
            log_cb("log", line)
    try:
        rc = proc.wait(timeout=10)
    except Exception:
        rc = proc.returncode
    if rc is None:
        rc = -1
    if rc == 0:
        log_cb("log", "FFmpeg termine avec succes (code 0).")
    else:
        log_cb("log", f"FFmpeg termine avec le code {rc}.")
    return rc


# ======================================================================
# 6. VALIDATION
# ======================================================================

def validate_output(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    if FFPROBE_PATH is None:
        return True
    try:
        r = subprocess.run(
            [
                FFPROBE_PATH, "-v", "quiet",
                "-print_format", "json",
                "-show_format", path,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW, timeout=15,
        )
        d = json.loads(r.stdout.decode("utf-8", errors="replace"))
        fmt = d.get("format", {}).get("format_name", "")
        return "mp4" in fmt
    except Exception:
        return os.path.getsize(path) > 1024


# ======================================================================
# 7. CONVERSION
# ======================================================================

def _build_remux_cmd(input_path, output_path, has_audio):
    cmd = [
        FFMPEG_PATH, "-hide_banner", "-y",
        "-fflags", "+genpts",
        "-i", input_path,
        "-map", "0:v:0",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0?"]
    cmd += ["-c", "copy", "-fps_mode", "cfr", "-movflags", "+faststart", output_path]
    return cmd


def _build_reencode_cmd(input_path, output_path, video_args, has_audio):
    cmd = [
        FFMPEG_PATH, "-hide_banner", "-y",
        "-fflags", "+genpts",
        "-i", input_path,
        "-map", "0:v:0",
    ]
    if has_audio:
        cmd += ["-map", "0:a:0?"]
    cmd += list(video_args)
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += ["-fps_mode", "cfr", "-movflags", "+faststart", output_path]
    return cmd


def _finalize_output(temp_path, output_path):
    if os.path.isfile(output_path):
        try:
            os.remove(output_path)
        except OSError:
            return False
    try:
        os.replace(temp_path, output_path)
        return True
    except OSError:
        return False


def convert_file(input_path, output_path, video_args, log_cb, cancel_event, total_duration=None):
    name = os.path.basename(input_path)
    info = probe_streams(input_path)
    codec = info["video_codec"]
    has_audio = info["has_audio"]
    temp = output_path + ".part.mp4"
    is_native = codec in NATIVE_VIDEO_CODECS
    is_remux_attempt = False
    if is_native:
        is_remux_attempt = True
        log_cb("log", f"Codec '{codec}' natif : remux rapide...")
        cmd = _build_remux_cmd(input_path, temp, has_audio)
        rc = run_ffmpeg(cmd, log_cb, cancel_event, total_duration)
        if rc == 0 and validate_output(temp):
            if _finalize_output(temp, output_path):
                return True
    is_gpu = any(
        video_args[:2] == ["-c:v", ec]
        for ec in GPU_ENCODE_ARGS
    )
    label = "remux" if is_remux_attempt else "re-encodage"
    log_cb("log", f"\n=== Codec '{codec}' non compatible : {label} ({name}) ===")
    cmd = _build_reencode_cmd(input_path, temp, video_args, has_audio)
    rc = run_ffmpeg(cmd, log_cb, cancel_event, total_duration)
    if rc == 0 and validate_output(temp):
        if _finalize_output(temp, output_path):
            return True
    if is_gpu:
        log_cb("log", "\n=== Echec GPU, fallback CPU rapide ===")
        cmd = _build_reencode_cmd(input_path, temp, CPU_FAST_ARGS, has_audio)
        rc = run_ffmpeg(cmd, log_cb, cancel_event, total_duration)
        if rc == 0 and validate_output(temp):
            if _finalize_output(temp, output_path):
                return True
    for f in (temp, temp + ".temp"):
        if os.path.isfile(f):
            try:
                os.remove(f)
            except OSError:
                pass
    return False


# ======================================================================
# 8. WORKER
# ======================================================================

def conversion_worker(files, output_dir, log_cb, cancel_event, encoder_mode):
    video_args, description = resolve_encoder(encoder_mode, log_cb)
    log_cb("log", f"\nMode : {description}\n")
    total = len(files)
    success = 0
    for idx, path in enumerate(files, 1):
        if cancel_event.is_set():
            log_cb("log", "\nAnnule par l'utilisateur.")
            break
        name = os.path.splitext(os.path.basename(path))[0]
        output_path = os.path.join(output_dir, name + ".mp4")
        log_cb("progress_text", f"Fichier {idx}/{total} : {os.path.basename(path)}")
        info = probe_streams(path)
        vid = f"{info['video_codec'] or '?'} {info['video_res'] or ''}".strip()
        aud = info["audio_codec"] if info["has_audio"] else "absente"
        dur = _fmt_duration(info["duration"])
        sz = _fmt_size(path)
        log_cb("log", f"\n--- [{idx}/{total}] {os.path.basename(path)} ---")
        log_cb("log", f"  Video: {vid} | Audio: {aud}")
        log_cb("log", f"  Duree: {dur} | Taille: {sz}")
        dur_val = info["duration"] if info["duration"] and info["duration"] > 0 else None
        log_cb("set_duration", dur_val)
        ok = convert_file(path, output_path, video_args, log_cb, cancel_event, dur_val)
        if ok:
            success += 1
            out_sz = _fmt_size(output_path)
            log_cb("log", f"  Sortie : {os.path.basename(output_path)} ({out_sz})")
        else:
            log_cb("log", "  Echec de la conversion.")
            if cancel_event.is_set():
                for f in (output_path, output_path + ".part.mp4"):
                    if os.path.isfile(f):
                        try:
                            os.remove(f)
                        except OSError:
                            pass
        log_cb("progress_value", idx)
    log_cb("done", (success, total))


# ======================================================================
# 9. GUI
# ======================================================================

class DavConverterApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Convertisseur DAV -> MP4")
        self.geometry("720x620")
        self.minsize(640, 520)
        icon_path = os.path.join(_get_bundle_dir(), "app_icon.ico")
        if os.path.isfile(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass
        self.files = []
        default_output = os.path.join(os.path.expanduser("~"), "Videos", "DAV-MP4")
        os.makedirs(default_output, exist_ok=True)
        self.output_dir_var = tk.StringVar(value=default_output)
        self.encoder_var = tk.StringVar(value=DEFAULT_ENCODER_MODE)
        self.cancel_event = threading.Event()
        self.start_time = None
        self.worker = None
        self.log_file = None
        self.log_file_path = None
        self.current_duration = None
        self._init_log_file()
        self._build_ui()
        self._check_ffmpeg()
        self.after(100, self._poll_queue)

    def _on_close(self):
        self.cancel_event.set()
        if self.log_file:
            try:
                self.log_file.close()
            except Exception:
                pass
        self.destroy()

    def _init_log_file(self):
        try:
            logs_dir = os.path.join(_get_bundle_dir(), "logs")
            os.makedirs(logs_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_file_path = os.path.join(logs_dir, f"dav_converter_debug_{ts}.txt")
            self.log_file = open(self.log_file_path, "w", encoding="utf-8")
            self.log_file.write(f"=== DAV Converter Debug Log ===\n")
            self.log_file.write(f"Date: {datetime.datetime.now().isoformat()}\n")
            self.log_file.write(f"Platform: {platform.platform()}\n")
            self.log_file.write(f"Python: {sys.version}\n")
            self.log_file.write(f"FFMPEG: {FFMPEG_PATH}\n")
            self.log_file.write(f"FFPROBE: {FFPROBE_PATH}\n")
            self.log_file.write(f"===\n\n")
            self.log_file.flush()
        except Exception:
            self.log_file = None

    def _write_log(self, text):
        if self.log_file:
            try:
                ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self.log_file.write(f"[{ts}] {text}\n")
                self.log_file.flush()
            except Exception:
                pass

    def _build_ui(self):
        pad = {"padx": 8, "pady": 3}
        # --- Header ---
        hdr = ttk.Frame(self)
        hdr.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(hdr, text="dev by HAJJI Soufiane", foreground="#666", font=("Segoe UI", 9, "italic")).pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8, pady=(4, 2))
        # --- 1. Fichiers ---
        f_files = ttk.LabelFrame(self, text=" 1. Fichiers a convertir (.dav) ")
        f_files.pack(fill="x", **pad)
        btn_row = ttk.Frame(f_files)
        btn_row.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Button(btn_row, text="Ajouter des fichiers...", command=self.add_files).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Ajouter un dossier...", command=self.add_folder).pack(side="left", padx=2)
        ttk.Button(btn_row, text="Vider la liste", command=self.clear_files).pack(side="left", padx=2)
        self.listbox = tk.Listbox(f_files, height=8, selectmode="extended")
        self.listbox.pack(fill="x", padx=6, pady=(0, 2))
        self.listbox_total = ttk.Label(f_files, text="Aucun fichier", foreground="gray")
        self.listbox_total.pack(anchor="w", padx=8, pady=(0, 4))
        # --- 2. Dossier de sortie ---
        f_out = ttk.LabelFrame(self, text=" 2. Dossier de sortie ")
        f_out.pack(fill="x", **pad)
        out_row = ttk.Frame(f_out)
        out_row.pack(fill="x", padx=6, pady=4)
        ttk.Entry(out_row, textvariable=self.output_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(out_row, text="Choisir...", command=self.choose_output_dir).pack(side="left")
        # --- 3. Mode encodeur ---
        f_spd = ttk.LabelFrame(self, text=" 3. Vitesse d'encodage ")
        f_spd.pack(fill="x", **pad)
        spd_row = ttk.Frame(f_spd)
        spd_row.pack(fill="x", padx=6, pady=4)
        ttk.Label(spd_row, text="Mode :").pack(side="left", padx=(0, 4))
        self.speed_combo = ttk.Combobox(
            spd_row, textvariable=self.encoder_var,
            values=list(ENCODER_MODES.keys()), state="readonly", width=22,
        )
        self.speed_combo.pack(side="left")
        self.speed_info = ttk.Label(spd_row, text="Auto = GPU si dispo, sinon CPU", foreground="gray")
        self.speed_info.pack(side="left", padx=(8, 0))
        # --- 4. Boutons ---
        f_btns = ttk.Frame(self)
        f_btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(f_btns, text="Demarrer la conversion", command=self.start_conversion)
        self.start_btn.pack(side="left", padx=2)
        self.cancel_btn = ttk.Button(f_btns, text="Annuler", command=self.cancel_conversion, state="disabled")
        self.cancel_btn.pack(side="left", padx=2)
        self.play_btn = ttk.Button(f_btns, text="Lire le MP4", command=self.play_selected, state="disabled")
        self.play_btn.pack(side="left", padx=2)
        # --- 5. Progression ---
        f_prog = ttk.LabelFrame(self, text=" Progression ")
        f_prog.pack(fill="x", **pad)
        info_row = ttk.Frame(f_prog)
        info_row.pack(fill="x", padx=6, pady=(4, 0))
        self.prog_file = ttk.Label(info_row, text="")
        self.prog_file.pack(side="left")
        self.prog_counter = ttk.Label(info_row, text="")
        self.prog_counter.pack(side="left", padx=(12, 0))
        self.prog_pct = tk.Label(info_row, text="", font=("Segoe UI", 10, "bold"), fg="#333")
        self.prog_pct.pack(side="left", padx=(12, 0))
        self.prog_time = ttk.Label(info_row, text="", anchor="e")
        self.prog_time.pack(side="right")
        self.prog_bar = ttk.Progressbar(f_prog, maximum=100, mode="determinate")
        self.prog_bar.pack(fill="x", padx=6, pady=(2, 6))
        # --- 6. Journal ---
        f_log = ttk.LabelFrame(self, text=" Journal ")
        f_log.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(f_log, height=10, state="disabled", bg="#111", fg="#eee", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)
        self.log_text.tag_configure("success", foreground="#66ff66")
        self.log_text.tag_configure("error", foreground="#ff6666")
        self.log_text.tag_configure("warning", foreground="#ffcc66")
        self.log_text.tag_configure("info", foreground="#66b3ff")
        self.log_text.tag_configure("dim", foreground="#888888")
        # --- 7. Footer ---
        f_foot = ttk.Frame(self)
        f_foot.pack(fill="x", **pad)
        self.status_lbl = ttk.Label(f_foot, text="", foreground="red")
        self.status_lbl.pack(side="left")
        log_row = ttk.Frame(f_foot)
        log_row.pack(side="right")
        self.log_lbl = ttk.Label(log_row, text="", foreground="gray")
        self.log_lbl.pack(side="left", padx=(0, 4))
        ttk.Button(log_row, text="Ouvrir les logs", command=self.open_log_folder).pack(side="left")
        if self.log_file_path:
            self.log_lbl.configure(text=f"Log : {os.path.basename(self.log_file_path)}")

    def _check_ffmpeg(self):
        if FFMPEG_PATH:
            self.status_lbl.configure(text=f"FFmpeg : {os.path.basename(FFMPEG_PATH)}", foreground="green")
        else:
            self.status_lbl.configure(text="FFmpeg introuvable !", foreground="red")

    # --- Fichiers ---
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Selectionner des fichiers DAV",
            filetypes=[("Fichiers DAV", "*.dav"), ("Tous", "*.*")],
        )
        if paths:
            self._add_paths(list(paths))

    def add_folder(self):
        folder = filedialog.askdirectory(title="Selectionner un dossier")
        if not folder:
            return
        paths = []
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".dav"):
                    paths.append(os.path.join(root, f))
        if paths:
            self._add_paths(sorted(paths))
        else:
            messagebox.showinfo("Aucun fichier", "Aucun fichier .dav trouve dans ce dossier.")

    def _add_paths(self, paths):
        added = 0
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                name = os.path.basename(p)
                try:
                    sz = _fmt_size(p)
                except Exception:
                    sz = "?"
                self.listbox.insert("end", f"{name}  —  {sz}")
                added += 1
        self._update_total()
        if added:
            threading.Thread(target=self._probe_worker, args=(paths,), daemon=True).start()

    def _update_total(self):
        count = len(self.files)
        total = 0
        for p in self.files:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        self.listbox_total.configure(text=f"{count} fichier(s) — {_fmt_size_bytes(total)}")

    def _probe_worker(self, paths):
        for p in paths:
            info = probe_streams(p)
            self.after(0, self._update_listbox_item, p, info)

    def _update_listbox_item(self, path, info):
        try:
            idx = self.files.index(path)
        except ValueError:
            return
        codec = info["video_codec"] or "?"
        res = info["video_res"] or ""
        tag = "success" if codec in NATIVE_VIDEO_CODECS else "warning"
        name = os.path.basename(path)
        try:
            sz = _fmt_size(path)
        except Exception:
            sz = "?"
        display = f"{name}  —  {codec} {res}  —  {sz}"
        self.listbox.delete(idx)
        self.listbox.insert(idx, display)
        self.listbox.itemconfigure(idx, fg="#66ff66" if tag == "success" else "#ffcc66")

    def clear_files(self):
        self.files.clear()
        self.listbox.delete(0, "end")
        self._update_total()

    def choose_output_dir(self):
        d = filedialog.askdirectory(title="Dossier de sortie")
        if d:
            self.output_dir_var.set(d)

    # --- Conversion ---
    def start_conversion(self):
        if not self.files:
            messagebox.showwarning("Aucun fichier", "Ajoutez des fichiers DAV d'abord.")
            return
        output_dir = self.output_dir_var.get().strip()
        if not output_dir:
            messagebox.showwarning("Dossier requis", "Selectionnez un dossier de sortie.")
            return
        os.makedirs(output_dir, exist_ok=True)
        self.cancel_event.clear()
        self.start_time = datetime.datetime.now()
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.play_btn.configure(state="disabled")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.prog_bar["value"] = 0
        self.prog_file.configure(text="")
        self.prog_counter.configure(text="")
        self.prog_pct.configure(text="")
        self.prog_time.configure(text="")
        mode = ENCODER_MODES.get(self.encoder_var.get(), "auto")
        self.worker = threading.Thread(
            target=conversion_worker,
            args=(list(self.files), output_dir, self._queue_put, self.cancel_event, mode),
            daemon=True,
        )
        self.worker.start()
        self.after(1000, self._tick_timer)

    def cancel_conversion(self):
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")

    def _queue_put(self, *args):
        q.put(args)

    def _tick_timer(self):
        if self.start_time is None:
            return
        elapsed = (datetime.datetime.now() - self.start_time).total_seconds()
        self.prog_time.configure(text=f"Temps : {_fmt_duration(elapsed)}")
        if not self.cancel_event.is_set():
            self.after(1000, self._tick_timer)

    # --- Lecture ---
    def play_selected(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx >= len(self.files):
            return
        name = os.path.splitext(os.path.basename(self.files[idx]))[0]
        mp4 = os.path.join(self.output_dir_var.get(), name + ".mp4")
        if os.path.isfile(mp4):
            os.startfile(mp4)
        else:
            messagebox.showinfo("Non disponible", "Le MP4 n'existe pas encore.")

    # --- Logs ---
    def open_log_folder(self):
        logs_dir = os.path.join(_get_bundle_dir(), "logs")
        if not os.path.isdir(logs_dir):
            os.makedirs(logs_dir, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(logs_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", logs_dir])
        else:
            subprocess.Popen(["xdg-open", logs_dir])

    # --- Queue polling ---
    def _poll_queue(self):
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[1])
                elif kind == "progress_pct":
                    pct, speed = msg[1], msg[2] if len(msg) > 2 else None
                    self.prog_bar["value"] = pct
                    txt = f"{pct}%"
                    if self.current_duration:
                        elapsed = self.current_duration * pct / 100
                        txt += f"  {_fmt_duration(elapsed)} / {_fmt_duration(self.current_duration)}"
                    if speed:
                        txt += f"  ({speed}x)"
                    self.prog_pct.configure(text=txt)
                elif kind == "set_duration":
                    self.current_duration = msg[1]
                elif kind == "progress_text":
                    self.prog_file.configure(text=msg[1])
                elif kind == "progress_value":
                    idx = msg[1]
                    total = len(self.files)
                    self.prog_counter.configure(text=f"{idx}/{total}")
                    self.prog_bar["value"] = 0
                elif kind == "done":
                    success, total = msg[1]
                    self._on_done(success, total)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _append_log(self, text):
        self._write_log(text)
        self.log_text.configure(state="normal")
        tag = None
        low = text.lower()
        if "succes" in low or "termine avec succes" in low or "remux" in low:
            tag = "success"
        elif "error" in low or "echec" in low or "impossible" in low:
            tag = "error"
        elif "warning" in low or "avertissement" in low:
            tag = "warning"
        elif "detect" in low or "gpu" in low or "mode" in low:
            tag = "info"
        elif text.startswith("  ") or "---" in text:
            tag = "dim"
        if tag:
            self.log_text.insert("end", text + "\n", tag)
        else:
            self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_done(self, success, total):
        self.start_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        elapsed = ""
        if self.start_time:
            secs = (datetime.datetime.now() - self.start_time).total_seconds()
            elapsed = f" en {_fmt_duration(secs)}"
        if self.cancel_event.is_set():
            self.prog_file.configure(text="Annule.")
            self._append_log("\nAnnule par l'utilisateur.")
        elif success == total:
            self.prog_file.configure(text=f"Termine : {success}/{total} converti(s) avec succes !{elapsed}")
            self.prog_pct.configure(text="100%")
            self.prog_bar["value"] = 100
            self.play_btn.configure(state="normal")
            self._append_log(f"\n=== Termine : {success}/{total} avec succes !{elapsed} ===")
        else:
            self.prog_file.configure(text=f"Termine : {success}/{total} reussi(s){elapsed}")
            self._append_log(f"\n=== Termine : {success}/{total} reussi(s){elapsed} ===")
            if success > 0:
                self.play_btn.configure(state="normal")
        self.start_time = None


# ======================================================================
# 10. POINT D'ENTREE
# ======================================================================

if __name__ == "__main__":
    q = queue.Queue()
    app = DavConverterApp()
    app.mainloop()
