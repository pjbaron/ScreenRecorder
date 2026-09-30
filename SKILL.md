---
name: screenrecorder
description: Record the screen (video plus system audio and/or mic) on this Windows machine by driving the Screen Recorder app through ctl.py. Use when asked to record, capture or film the screen, a demo, a game or a test run, or to stop, check or configure a recording.
---

# Screen Recorder control

`ctl.py` (in this folder, stdlib only) drives the Screen Recorder app over a local API on 127.0.0.1:8765. It prints JSON. Exit code 0 = ok, 1 = error reply (read the `error` field), 2 = app not running.

## Workflow

```
python ctl.py launch                      # starts the app if needed, waits until ready (about 10 s)
python ctl.py devices                     # valid values for every setting
python ctl.py start fps=30 mic=true folder=D:\Caps   # settings are optional; returns once recording
python ctl.py status                      # state, elapsed_s, video_mb, current_file
python ctl.py stop                        # returns once the MP4 is written; prints its path
python ctl.py quit                        # close the app (idle only)
```

Always `stop` what you `start`, and report the file path from the `stop` reply to the user.

## Settings (start or config, key=value)

`display` (index from devices), `fps` (30|60), `quality` (standard|high), `system_audio` (true|false), `system_device`, `mic` (true|false), `mic_device`, `mic_gain_db` (0,6,12,18,24,30), `folder`.

Device names must match `devices` exactly. Settings persist between recordings while the app runs, and can only be changed while idle. Defaults: display 0, 60 fps, standard, system audio on, mic off, folder `C:\Users\Pete\Videos\Captures`.

## Gotchas

- State is one of idle, starting, recording, finalizing. Wrong-state calls return an error (for example start while recording); check `status` instead of retrying blindly.
- `stop` can take a few seconds to mux. If it returns `done: false` (HTTP 202), poll `status` until idle, then read `last_file`.
- A failed call leaves the reason in `last_error` in `status`. Logs are in `logs\record.log` and `logs\mux.log` beside the app.
- Whole-display capture only. Black video for a game means exclusive fullscreen; switch it to borderless.
- Ctrl+Alt+R also toggles recording, so a person can stop it out from under you; check `status` if unsure.
- Port taken: set `RECORDER_PORT` for ctl.py and start the app with `--port N`.
- Packaged build: `build.bat` makes `dist\ScreenRecorder` with its own copy of ctl.py, which launches the exe.
