# Screen Recorder for Windows

A lightweight Windows screen recorder that keeps the CPU load low while recording games or other heavy applications.

- Video: screen capture and H.264 encoding both run on the GPU (Desktop Duplication API via ffmpeg `ddagrab`, then NVENC, AMF or Quick Sync). Frames never pass through the CPU.
- Audio: system audio (WASAPI loopback) and/or microphone, mixed into one AAC track.
- Quality: about the same bitrate as Xbox Game Bar (9 Mbps at 1080p60, scaled by resolution), output as MP4.
- Works with any display, full-screen or windowed. A global hotkey (Ctrl+Alt+R) starts and stops recording while another app has focus.

## Requirements

- Windows 10 or 11, 64-bit
- A GPU with a hardware H.264 encoder: NVIDIA GTX 600 series or newer (NVENC), AMD with AMF, or Intel with Quick Sync. Up to date graphics drivers are required.
- ffmpeg 6.0 or newer, built with `ddagrab` and the hardware encoders (the "essentials" or "full" builds from gyan.dev include them)

Running from source additionally needs Python 3.10+ (developed on 3.13).

## Option A: Run the packaged build

1. Build or download the `ScreenRecorder` folder (see "Building the exe" below).
2. Confirm `ffmpeg.exe` and `ffprobe.exe` are in the same folder as `ScreenRecorder.exe`. The packaged build uses only these copies, not anything on PATH.
3. Run `ScreenRecorder.exe`. Keep the whole folder together; the exe needs the `_internal` folder beside it.

## Option B: Run from source

1. Install ffmpeg and put `ffmpeg.exe` and `ffprobe.exe` on PATH:
   - Download `ffmpeg-release-essentials.zip` from https://www.gyan.dev/ffmpeg/builds/
   - Extract it, for example to `C:\ffmpeg`, and add `C:\ffmpeg\bin` to the system PATH.
   - Check: open a new terminal and run `ffmpeg -hide_banner -filters | findstr ddagrab`. It must print a line. If not, the build is too old.
2. Install the Python dependency:
   ```
   pip install -r requirements.txt
   ```
3. Start it:
   ```
   python recorder.py
   ```
   or double-click `run.bat` (no console window).

## Building the exe

```
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onedir --windowed --name ScreenRecorder recorder.py
copy C:\ffmpeg\bin\ffmpeg.exe dist\ScreenRecorder\
copy C:\ffmpeg\bin\ffprobe.exe dist\ScreenRecorder\
```

The result is `dist\ScreenRecorder\ScreenRecorder.exe`. Do not commit `dist`, `build` or the ffmpeg binaries to git.

## First-time preparation

1. Update your graphics driver. Encoder detection at startup fails if the driver is too old.
2. Set Windows to use your main GPU for the recorder if you have more than one GPU (laptops especially): Settings > System > Display > Graphics, add `ScreenRecorder.exe` (or `python.exe`), and choose the GPU that drives the monitor you record.
3. Check Windows microphone privacy: Settings > Privacy & security > Microphone, allow desktop apps to access the microphone. Without this the mic records silence.
4. Start the app once. Startup takes a few seconds while it tests the encoders and probes the displays. The status line shows the encoder in use, for example `Ready. Encoder: h264_nvenc`.

## Using it

| Control | Meaning |
|---|---|
| Display | Which monitor to record, with its resolution. Display numbers follow ffmpeg/DXGI order and may not match Windows' numbering. |
| Frame rate | 30 or 60 fps, constant. |
| Quality | Standard is about 9 Mbps at 1080p60. High is 1.5 times that. |
| System audio | Records what plays on the chosen output device (loopback). Pick the device you actually listen on. |
| Microphone | Optional. Mixed into the same audio track as system audio. |
| Save to | Output folder. Default `Videos\Captures`. |

Press Start or Ctrl+Alt+R. Press Stop or Ctrl+Alt+R again to finish. After stopping, it takes a few seconds to mix audio and write the final MP4 (the video is copied, not re-encoded). Files are named `Recording YYYY-MM-DD HH-MM-SS.mp4`.

While recording, the raw video is written to `Recording ....video.mkv` beside the final file and audio to `.sys.wav` and `.mic.wav`. These are deleted after a successful save. If a recording is interrupted by a crash, the `.mkv` is still playable.

The hotkey is set by `HOTKEY_MOD` and `HOTKEY_VK` at the top of `recorder.py`. If Ctrl+Alt+R is used by another program, the status line says so and the button still works.

## How it works

- `ddagrab` captures the desktop into GPU memory; the encoder reads those frames directly.
- ffmpeg and the app run at below-normal priority so the game keeps precedence.
- Audio is recorded by Python (PyAudioWPatch, WASAPI) to WAV, padded with silence when a loopback device is idle so it stays in sync. At stop, ffmpeg mixes the tracks, encodes AAC 192 kbps, and remuxes with the video into MP4.
- Audio is delayed by the measured start lag between video and audio. Expect sync to be within roughly 0.1 seconds.

## Troubleshooting

Logs for the last recording are in the `logs` folder next to the app: `record.log` (capture and encode) and `mux.log` (final MP4 step). Error dialogs show the tail of the relevant log.

- `No working GPU H.264 encoder found`: update the graphics driver, and check that your ffmpeg build lists `h264_nvenc`, `h264_amf` or `h264_qsv` in `ffmpeg -encoders`.
- `ddagrab could not open any display`: the ffmpeg build lacks `ddagrab` (too old), or the session is a remote desktop session, which does not support Desktop Duplication.
- `Unanticipated host error (-9999)` on start: an audio device could not be opened. Close programs that hold the device in exclusive mode, and re-select the device in the dropdown.
- Black video for a specific game: some exclusive-fullscreen or protected-content windows cannot be duplicated. Switch the game to borderless windowed.
- Microphone silent: see the privacy setting above.
- Video and audio drift over a long recording: report it with the `mux.log` file.

## Limitations

- Windows only. Whole-display capture only (no window or region capture).
- Only the encoder that passes the startup test is used; there is no software fallback.
- Not all combinations have been tested. Development testing used an NVIDIA RTX 4070 Ti Super with NVENC; the AMD (AMF) and Intel (Quick Sync) paths are written but untested.
