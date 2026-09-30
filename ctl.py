"""Command line client for the Screen Recorder control API (stdlib only).

  python ctl.py launch                 start the app if it is not running, wait until it answers
  python ctl.py status                 state, config, last file, last error
  python ctl.py devices                valid values for every setting
  python ctl.py config [key=value ..]  show or change settings (only while idle)
  python ctl.py start [key=value ..]   optional settings are applied first; returns once recording
  python ctl.py stop                   returns once the MP4 is written
  python ctl.py quit                   close the app (only while idle)

Values are parsed as JSON where possible (fps=30, mic=true), otherwise taken as text (folder=D:\\Caps).
Prints the JSON reply. Exit code 0 on success, 1 on an error reply, 2 if the app is unreachable.
Set RECORDER_PORT to use a port other than 8765.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("RECORDER_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"
APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recorder.py")


def call(method, path, body=None, timeout=1200):
    data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def parse_pairs(args):
    d = {}
    for a in args:
        if "=" not in a:
            sys.exit(f"expected key=value, got {a!r}")
        k, v = a.split("=", 1)
        try:
            d[k] = json.loads(v)
        except ValueError:
            d[k] = v
    return d


def launch():
    try:
        return call("GET", "/status", timeout=5)
    except OSError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    exe = os.path.join(here, "ScreenRecorder.exe")
    if os.path.exists(exe):  # packaged build: ctl.py sits beside the exe
        cmd = [exe]
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            sys.exit(f"pythonw.exe not found beside {sys.executable}")
        cmd = [pythonw, APP]
    cmd += ["--port", str(PORT)] if PORT != 8765 else []
    subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, close_fds=True)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    # Startup tests three encoders and probes displays, which takes several seconds.
    end = time.time() + 120
    while time.time() < end:
        time.sleep(1)
        try:
            return call("GET", "/status", timeout=5)
        except OSError:
            pass
    sys.exit("app did not start answering on the control port within 120 s (check logs and the app window)")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "launch":
            code, out = launch()
        elif cmd in ("status", "devices"):
            code, out = call("GET", "/" + cmd)
        elif cmd == "config":
            code, out = call("POST", "/config", parse_pairs(rest)) if rest else call("GET", "/config")
        elif cmd == "start":
            code, out = call("POST", "/start", parse_pairs(rest))
        elif cmd in ("stop", "quit"):
            code, out = call("POST", "/" + cmd)
        else:
            sys.exit(f"unknown command {cmd!r}")
    except OSError as e:
        print(f"cannot reach the recorder on {URL}: {e}. Run: python ctl.py launch", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0 if code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
