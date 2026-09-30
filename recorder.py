"""Windows screen recorder.

Video: ffmpeg ddagrab (Desktop Duplication, GPU capture) -> hardware H.264 encoder
(NVENC / AMF / QSV). Frames stay on the GPU; the CPU only muxes.
Audio: WASAPI loopback (system) and microphone, written to WAV by Python, mixed and
encoded to AAC when recording stops. Video is copied, not re-encoded, at that step.
"""
import ctypes
import ctypes.wintypes as wt
import datetime
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
from tkinter import filedialog, messagebox, ttk

import pyaudiowpatch as pyaudio

HOTKEY_MOD = 0x0002 | 0x0001 | 0x4000  # MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
HOTKEY_VK = 0x52  # R  -> Ctrl+Alt+R
HOTKEY_TEXT = "Ctrl+Alt+R"
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Videos", "Captures")
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(APP_DIR, "logs")
NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000
AUDIO_KBPS = 192

# Packaged build uses the ffmpeg.exe / ffprobe.exe shipped beside the exe; script mode uses PATH.
FFMPEG = os.path.join(APP_DIR, "ffmpeg.exe") if getattr(sys, "frozen", False) else shutil.which("ffmpeg")
if not FFMPEG or not os.path.exists(FFMPEG):
    raise RuntimeError(f"ffmpeg.exe not found ({FFMPEG})")


# ---------------------------------------------------------------- video

def source_chain(encoder, output_idx, fps):
    src = f"ddagrab=output_idx={output_idx}:framerate={fps}:draw_mouse=1"
    if encoder == "h264_qsv":
        src += ",hwmap=derive_device=qsv,format=qsv"
    return src


def encoder_args(encoder, bitrate, fps):
    b, mx, buf = int(bitrate), int(bitrate * 1.4), int(bitrate * 2)
    gop = ["-g", str(fps * 2)]
    if encoder == "h264_nvenc":
        return ["-c:v", encoder, "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-b:v", str(b),
                "-maxrate", str(mx), "-bufsize", str(buf), "-profile:v", "high", "-spatial-aq", "1"] + gop
    if encoder == "h264_amf":
        return ["-c:v", encoder, "-quality", "balanced", "-rc", "vbr_peak", "-b:v", str(b),
                "-maxrate", str(mx), "-bufsize", str(buf), "-profile:v", "high"] + gop
    if encoder == "h264_qsv":
        return ["-c:v", encoder, "-preset", "medium", "-b:v", str(b), "-maxrate", str(mx),
                "-bufsize", str(buf), "-profile:v", "high"] + gop
    raise ValueError(f"unknown encoder {encoder}")


def detect_encoder():
    errors = []
    for enc in ("h264_nvenc", "h264_amf", "h264_qsv"):
        cmd = [FFMPEG, "-hide_banner", "-f", "lavfi", "-i", source_chain(enc, 0, 30),
               "-frames:v", "10", "-c:v", enc, "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        if r.returncode == 0:
            return enc
        errors.append(f"{enc}: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'no output'}")
    raise RuntimeError("No working GPU H.264 encoder found:\n" + "\n".join(errors))


def probe_outputs():
    """Return [(output_idx, width, height)] for each display ddagrab can open."""
    found = []
    for idx in range(8):
        cmd = [FFMPEG, "-hide_banner", "-f", "lavfi", "-i", f"ddagrab=output_idx={idx}",
               "-vf", "hwdownload,format=bgra", "-frames:v", "1", "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        if r.returncode != 0:
            break
        m = re.search(r"Video:.*?(\d{3,5})x(\d{3,5})", r.stderr)
        if not m:
            raise RuntimeError(f"Could not read resolution for output {idx}:\n{r.stderr}")
        found.append((idx, int(m.group(1)), int(m.group(2))))
    if not found:
        raise RuntimeError("ddagrab could not open any display")
    return found


def target_bitrate(w, h, fps, high):
    """Xbox Game Bar 'standard' is about 9 Mbps at 1080p60; scale with pixel count."""
    bps = 9e6 * ((w * h) / (1920 * 1080)) ** 0.9
    if fps <= 30:
        bps *= 0.65
    if high:
        bps *= 1.5
    return bps


# ---------------------------------------------------------------- audio

class Track:
    """One device's WAV output. Pads silence so the file tracks wall-clock time (loopback devices
    deliver no data while nothing is playing)."""

    def __init__(self, device, path):
        self.device, self.path = device, path
        self.rate = int(device["defaultSampleRate"])
        self.ch = int(device["maxInputChannels"])
        self.q = queue.Queue()
        self.written = 0
        self.wav = wave.open(path, "wb")
        self.wav.setnchannels(self.ch)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.rate)

    def callback(self, data, frames, t, status):
        self.q.put(data)
        return (None, pyaudio.paContinue)

    def drain(self):
        while True:
            try:
                data = self.q.get_nowait()
            except queue.Empty:
                return
            self.wav.writeframes(data)
            self.written += len(data) // (2 * self.ch)

    def pad_to_now(self, t0, min_lag):
        lag = int((time.perf_counter() - t0) * self.rate) - self.written
        if lag > min_lag:
            self.wav.writeframes(bytes(lag * self.ch * 2))
            self.written += lag


class AudioRecorder:
    """Records several WASAPI devices on one thread with one PyAudio instance. WASAPI streams must be
    opened and closed on the same thread, and a second live PyAudio instance makes opening fail (-9999)."""

    def __init__(self, tracks):
        self.tracks = tracks  # list of Track
        self.stop_flag = threading.Event()
        self.opened = threading.Event()
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.opened.wait()
        if self.error:
            self.thread.join()
            raise RuntimeError(f"audio failed to start: {self.error!r}")

    def _run(self):
        pa = pyaudio.PyAudio()
        streams = []
        try:
            for t in self.tracks:
                streams.append(pa.open(format=pyaudio.paInt16, channels=t.ch, rate=t.rate, input=True,
                                       input_device_index=t.device["index"], frames_per_buffer=t.rate // 50,
                                       stream_callback=t.callback))
            self.t0 = time.perf_counter()
        except Exception as e:
            self.error = RuntimeError(f"{t.device['name']}: {e!r}")
            for st in streams:
                st.close()
            pa.terminate()
            for t in self.tracks:
                t.wav.close()
            self.opened.set()
            return
        self.opened.set()
        try:
            while not self.stop_flag.is_set():
                time.sleep(0.05)
                for t in self.tracks:
                    t.drain()
                    t.pad_to_now(self.t0, t.rate // 4)
            for st in streams:
                st.stop_stream()
                st.close()
            for t in self.tracks:
                t.drain()
                t.pad_to_now(self.t0, 0)
        except Exception as e:  # reported by stop()
            self.error = e
        finally:
            pa.terminate()
            for t in self.tracks:
                t.wav.close()

    def stop(self):
        self.stop_flag.set()
        self.thread.join()
        if self.error:
            raise RuntimeError(f"audio writer failed: {self.error!r}")


def list_audio(pa):
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    loopbacks = list(pa.get_loopback_device_info_generator())
    mics = []
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
            mics.append(d)
    default_loop = pa.get_default_wasapi_loopback()
    default_mic = pa.get_device_info_by_index(wasapi["defaultInputDevice"]) if wasapi["defaultInputDevice"] >= 0 else None
    return loopbacks, default_loop, mics, default_mic


# ---------------------------------------------------------------- recording session

class Session:
    def __init__(self, cfg):
        self.cfg = cfg
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        os.makedirs(cfg["folder"], exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        self.base = os.path.join(cfg["folder"], f"Recording {stamp}")
        self.final = self.base + ".mp4"
        self.video = self.base + ".video.mkv"
        self.audio = None
        self.tracks = []
        self.record_log = os.path.join(LOG_DIR, "record.log")
        self.mux_log = os.path.join(LOG_DIR, "mux.log")
        self.started = None

    def start(self):
        c = self.cfg
        cmd = [FFMPEG, "-hide_banner", "-y", "-f", "lavfi", "-i", source_chain(c["encoder"], c["output_idx"], c["fps"])]
        cmd += encoder_args(c["encoder"], c["bitrate"], c["fps"])
        cmd += ["-fps_mode", "cfr", "-progress", "pipe:1", "-stats_period", "0.1", "-nostats", self.video]
        self.log_f = open(self.record_log, "w")
        self.log_f.write(" ".join(cmd) + "\n")
        self.log_f.flush()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log_f,
                                     creationflags=NO_WINDOW | BELOW_NORMAL)
        self.first_frame = threading.Event()
        threading.Thread(target=self._read_progress, daemon=True).start()
        deadline = time.time() + 60
        while not self.first_frame.is_set():
            if self.proc.poll() is not None:
                msg = self.log_tail()
                self.abort()
                raise RuntimeError("ffmpeg exited before the first frame:\n" + msg)
            if time.time() > deadline:
                msg = self.log_tail()
                self.abort()
                raise RuntimeError("Timed out waiting for the first video frame:\n" + msg)
            time.sleep(0.02)
        self.started = time.perf_counter()
        tracks = []
        if c["sys_dev"]:
            tracks.append(Track(c["sys_dev"], f"{self.base}.sys.wav"))
        if c["mic_dev"]:
            tracks.append(Track(c["mic_dev"], f"{self.base}.mic.wav"))
        self.tracks = tracks
        if tracks:
            self.audio = AudioRecorder(tracks)
            try:
                self.audio.start()
            except Exception:
                self.abort()
                raise

    def _read_progress(self):
        for line in self.proc.stdout:
            if line.startswith(b"frame=") and int(line[6:].strip() or 0) > 0:
                self.first_frame.set()

    def log_tail(self, n=15):
        self.log_f.flush()
        with open(self.record_log) as f:
            return "".join(f.readlines()[-n:])

    def alive(self):
        return self.proc.poll() is None

    def video_size(self):
        return os.path.getsize(self.video) if os.path.exists(self.video) else 0

    def abort(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log_f.close()
        # Files from a failed start are plain files created by this session and hold no usable data.
        for f in [self.video] + [t.path for t in self.tracks]:
            if os.path.exists(f):
                os.remove(f)

    def stop(self):
        errors = []
        if self.proc.poll() is None:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        if self.audio:
            try:
                self.audio.stop()
            except Exception as e:
                errors.append(str(e))
        try:
            self.proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise RuntimeError("ffmpeg did not exit after 'q'")
        self.log_f.close()
        if self.proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited with code {self.proc.returncode}:\n" + self.log_tail())
        if errors:
            raise RuntimeError("\n".join(errors))
        self._mux()
        return self.final

    def _video_duration(self):
        out = subprocess.run([os.path.join(os.path.dirname(FFMPEG), "ffprobe.exe"), "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", self.video], capture_output=True, text=True,
                             check=True, creationflags=NO_WINDOW).stdout
        return float(out)

    def _audio_duration(self):
        t = self.tracks[0]
        return t.written / t.rate

    def _mux(self):
        cmd = [FFMPEG, "-hide_banner", "-y", "-i", self.video]
        for t in self.tracks:
            cmd += ["-i", t.path]
        if len(self.tracks) == 0:
            cmd += ["-map", "0:v", "-c:v", "copy"]
        else:
            # Video starts recording before the first-frame signal reaches us, so the audio files are
            # shorter than the video by the start lag. Delay audio by that difference.
            lag_ms = max(0, int((self._video_duration() - self._audio_duration()) * 1000))
            norm = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
            parts, labels = [], []
            for i, t in enumerate(self.tracks, start=1):
                gain = f",volume={self.cfg['mic_gain_db']}dB" if t.path.endswith(".mic.wav") else ""
                parts.append(f"[{i}:a]{norm}{gain},adelay={lag_ms}:all=1[a{i}]")
                labels.append(f"[a{i}]")
            if len(self.tracks) == 1:
                parts.append(f"{labels[0]}alimiter=limit=0.95[a]")
            else:
                parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,alimiter=limit=0.95[a]")
            fg = ";".join(parts)
            cmd += ["-filter_complex", fg, "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k"]
        cmd += ["-movflags", "+faststart", self.final]
        with open(self.mux_log, "w") as lf:
            lf.write(" ".join(cmd) + "\n")
            lf.flush()
            r = subprocess.run(cmd, stderr=lf, creationflags=NO_WINDOW, timeout=3600)
        if r.returncode != 0:
            with open(self.mux_log) as f:
                tail = "".join(f.readlines()[-15:])
            raise RuntimeError(f"Mux failed, raw files kept next to {self.final}:\n{tail}")
        # Temp files are plain files created by this session.
        os.remove(self.video)
        for t in self.tracks:
            os.remove(t.path)


# ---------------------------------------------------------------- GUI

def start_hotkey_thread(q):
    def run():
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, 1, HOTKEY_MOD, HOTKEY_VK):
            q.put(("hotkey_error", None))
            return
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                q.put(("toggle", None))
    threading.Thread(target=run, daemon=True).start()


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.session = None
        self.state = "idle"  # idle | starting | recording | finalizing
        root.title("Screen Recorder")
        root.resizable(False, False)

        # A live PyAudio instance in this thread makes opening loopback streams elsewhere fail (-9999),
        # so it is used only to list devices and then terminated.
        pa = pyaudio.PyAudio()
        self.loopbacks, self.def_loop, self.mics, self.def_mic = list_audio(pa)
        pa.terminate()
        self.encoder = detect_encoder()
        self.outputs = probe_outputs()

        f = ttk.Frame(root, padding=10)
        f.grid()
        r = 0
        ttk.Label(f, text="Display").grid(row=r, column=0, sticky="w")
        self.disp = ttk.Combobox(f, state="readonly", width=38,
                                 values=[f"Display {i}  ({w}x{h})" for i, w, h in self.outputs])
        self.disp.current(0)
        self.disp.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)

        r += 1
        ttk.Label(f, text="Frame rate").grid(row=r, column=0, sticky="w")
        self.fps = ttk.Combobox(f, state="readonly", width=8, values=["30", "60"])
        self.fps.set("60")
        self.fps.grid(row=r, column=1, sticky="w", pady=2)
        self.quality = ttk.Combobox(f, state="readonly", width=18, values=["Standard (Xbox)", "High (1.5x)"])
        self.quality.current(0)
        self.quality.grid(row=r, column=2, sticky="w", pady=2)

        r += 1
        self.sys_on = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="System audio", variable=self.sys_on).grid(row=r, column=0, sticky="w")
        self.sys_dev = ttk.Combobox(f, state="readonly", width=45, values=[d["name"] for d in self.loopbacks])
        self.sys_dev.set(self.def_loop["name"])
        self.sys_dev.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)

        r += 1
        self.mic_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Microphone", variable=self.mic_on).grid(row=r, column=0, sticky="w")
        self.mic_dev = ttk.Combobox(f, state="readonly", width=45, values=[d["name"] for d in self.mics])
        if self.def_mic:
            self.mic_dev.set(self.def_mic["name"])
        self.mic_dev.grid(row=r, column=1, columnspan=2, sticky="w", pady=2)

        r += 1
        ttk.Label(f, text="Mic gain (dB)").grid(row=r, column=0, sticky="w")
        self.mic_gain = ttk.Combobox(f, state="readonly", width=8, values=["0", "6", "12", "18", "24", "30"])
        self.mic_gain.set("0")
        self.mic_gain.grid(row=r, column=1, sticky="w", pady=2)

        r += 1
        ttk.Label(f, text="Save to").grid(row=r, column=0, sticky="w")
        self.folder = tk.StringVar(value=DEFAULT_DIR)
        ttk.Entry(f, textvariable=self.folder, width=40).grid(row=r, column=1, sticky="w", pady=2)
        ttk.Button(f, text="Browse", command=self.browse).grid(row=r, column=2, sticky="w")

        r += 1
        self.btn = ttk.Button(f, text=f"Start ({HOTKEY_TEXT})", command=self.toggle)
        self.btn.grid(row=r, column=0, columnspan=3, sticky="ew", pady=6)

        r += 1
        self.status = tk.StringVar(value=f"Ready. Encoder: {self.encoder}")
        ttk.Label(f, textvariable=self.status, wraplength=470, justify="left").grid(row=r, column=0, columnspan=3, sticky="w")

        start_hotkey_thread(self.q)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.folder.get())
        if d:
            self.folder.set(d)

    def config(self):
        idx, w, h = self.outputs[self.disp.current()]
        fps = int(self.fps.get())
        cfg = {"encoder": self.encoder, "output_idx": idx, "fps": fps, "folder": self.folder.get(),
               "bitrate": target_bitrate(w, h, fps, self.quality.current() == 1),
               "sys_dev": None, "mic_dev": None,
               "mic_gain_db": int(self.mic_gain.get())}
        if self.sys_on.get():
            cfg["sys_dev"] = next(d for d in self.loopbacks if d["name"] == self.sys_dev.get())
        if self.mic_on.get():
            if not self.mics:
                raise RuntimeError("No microphone found")
            cfg["mic_dev"] = next(d for d in self.mics if d["name"] == self.mic_dev.get())
        return cfg

    def toggle(self):
        if self.state == "idle":
            try:
                cfg = self.config()
            except Exception as e:
                messagebox.showerror("Screen Recorder", str(e))
                return
            self.state = "starting"
            self.btn.state(["disabled"])
            self.status.set("Starting...")
            self.session = Session(cfg)
            self.bg(self.session.start, "started")
        elif self.state == "recording":
            self.state = "finalizing"
            self.btn.state(["disabled"])
            self.status.set("Finalizing (mixing audio, writing MP4)...")
            self.bg(self.session.stop, "stopped")

    def bg(self, fn, tag):
        def run():
            try:
                self.q.put((tag, fn()))
            except Exception as e:
                self.q.put(("error", e))
        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                tag, val = self.q.get_nowait()
                if tag == "toggle":
                    self.toggle()
                elif tag == "hotkey_error":
                    self.status.set(f"Could not register {HOTKEY_TEXT} (already in use). Use the button.")
                elif tag == "started":
                    self.state = "recording"
                    self.btn.config(text=f"Stop ({HOTKEY_TEXT})")
                    self.btn.state(["!disabled"])
                elif tag == "stopped":
                    self.state = "idle"
                    self.btn.config(text=f"Start ({HOTKEY_TEXT})")
                    self.btn.state(["!disabled"])
                    self.status.set(f"Saved: {val}")
                elif tag == "error":
                    self.state = "idle"
                    self.btn.config(text=f"Start ({HOTKEY_TEXT})")
                    self.btn.state(["!disabled"])
                    self.status.set("Error")
                    messagebox.showerror("Screen Recorder", str(val))
        except queue.Empty:
            pass
        if self.state == "recording":
            if not self.session.alive():
                self.state = "finalizing"
                self.status.set("ffmpeg stopped unexpectedly, finalizing...")
                self.bg(self.session.stop, "stopped")
            else:
                el = int(time.perf_counter() - self.session.started)
                mb = self.session.video_size() / 1e6
                self.status.set(f"Recording {el // 3600:02d}:{el // 60 % 60:02d}:{el % 60:02d}   video {mb:.0f} MB")
        self.root.after(250, self.poll)

    def close(self):
        if self.state in ("recording", "starting", "finalizing"):
            messagebox.showwarning("Screen Recorder", "Stop the recording before closing.")
            return
        self.root.destroy()


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
