#!/usr/bin/env python3
"""droid-deck — browse and transfer files on ADB-connected Android devices
from a local web GUI. Stdlib only; requires `adb` on PATH.

Usage:  droid-deck [--port 8642] [--no-open]
"""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8642
ADB = shutil.which("adb") or "/opt/homebrew/bin/adb"

# toybox `ls -lA` line, e.g.
# -rw-rw---- 1 root everybody 12345 2026-08-01 10:00 name with spaces
LS_RE = re.compile(
    r"^([\-dlbcpsD])([rwxsStT\-]{9})\s+\d+\s+\S+\s+\S+\s+"
    r"(\d+(?:,\s*\d+)?)\s+(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2})\s+(.*)$"
)


THUMB_DIR = os.path.expanduser("~/.cache/droid-deck/thumbs")
THUMB_SEM = threading.Semaphore(3)   # limit concurrent adb pulls for thumbs
THUMB_MAX_BYTES = 80 * 1024 * 1024


def sh_quote(s: str) -> str:
    """Quote a string for the device-side POSIX shell."""
    return "'" + s.replace("'", "'\\''") + "'"


def adb_run(serial, *args, binary=False, timeout=120):
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, b"" if binary else "", "adb binary not found"
    except subprocess.TimeoutExpired:
        return 124, b"" if binary else "", "adb command timed out"
    out = p.stdout if binary else p.stdout.decode("utf-8", "replace")
    return p.returncode, out, p.stderr.decode("utf-8", "replace")


DEVNAME_CACHE = {}


def device_info(serial):
    """Friendly name, marketing name first (e.g. 'Xiaomi Mi 11i'), cached."""
    if serial in DEVNAME_CACHE:
        return DEVNAME_CACHE[serial]
    rc, out, err = adb_run(
        serial, "shell",
        "getprop ro.product.marketname; getprop ro.config.marketing_name; "
        "settings get global device_name; getprop ro.product.manufacturer; "
        "getprop ro.product.model; getprop ro.build.version.release",
        timeout=10)
    lines = [l.strip() for l in out.splitlines()] + [""] * 6
    market, market2, devname, manuf, model, release = lines[:6]
    name = next((c for c in (market, market2, devname)
                 if c and c.lower() != "null"), "") or model or serial
    if manuf and manuf.lower() not in name.lower():
        name = f"{manuf} {name}"
    info = {"name": name, "android": release}
    if rc == 0 and name:
        DEVNAME_CACHE[serial] = info
    return info


def list_devices():
    rc, out, err = adb_run(None, "devices", "-l")
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial, state = parts[0], parts[1]
        props = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append({
            "serial": serial,
            "state": state,
            "model": props.get("model", "").replace("_", " "),
            "product": props.get("product", ""),
            "transport": props.get("usb", props.get("transport_id", "")),
        })
    for d in devices:
        if d["state"] == "device":
            d.update(device_info(d["serial"]))
    return devices


def list_dir(serial, path):
    rc, out, err = adb_run(serial, "shell", f"ls -lAH -- {sh_quote(path)}")
    entries = []
    for line in out.splitlines():
        m = LS_RE.match(line.rstrip())
        if not m:
            continue
        kind_ch, _perms, size, mtime, name = m.groups()
        target = None
        if kind_ch == "l" and " -> " in name:
            name, target = name.split(" -> ", 1)
        kind = {"d": "dir", "l": "link"}.get(kind_ch, "file")
        entries.append({
            "name": name,
            "type": kind,
            "size": int(size.split(",")[0]) if kind == "file" else 0,
            "mtime": mtime,
            "target": target,
        })
    entries.sort(key=lambda e: (e["type"] == "file", e["name"].lower()))
    return rc, entries, err.strip()


def make_thumb(serial, path, mtime, size):
    """Pull an image once, downscale with macOS `sips`, cache by mtime+size.
    Returns the cached jpeg path, or None if a thumb can't be made."""
    os.makedirs(THUMB_DIR, exist_ok=True)
    key = hashlib.sha1(f"{serial}|{path}|{mtime}|{size}".encode()).hexdigest()
    out = os.path.join(THUMB_DIR, key + ".jpg")
    if os.path.exists(out):
        return out
    if size and size > THUMB_MAX_BYTES:
        return None
    ext = os.path.splitext(path)[1].lower() or ".img"
    with THUMB_SEM:
        if os.path.exists(out):  # another thread beat us to it
            return out
        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        try:
            proc = subprocess.run(
                [ADB, "-s", serial, "exec-out", f"cat {sh_quote(path)}"],
                stdout=tmp, stderr=subprocess.DEVNULL, timeout=120)
            tmp.close()
            if proc.returncode != 0 or os.path.getsize(tmp.name) == 0:
                return None
            r = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "70",
                 "-Z", "384", tmp.name, "--out", out],
                capture_output=True, timeout=60)
            if r.returncode != 0 or not os.path.exists(out):
                return None
            return out
        except subprocess.TimeoutExpired:
            return None
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def disk_free(serial, path):
    rc, out, err = adb_run(serial, "shell", f"df -k -- {sh_quote(path)}")
    lines = [l for l in out.splitlines() if l.strip()]
    if rc == 0 and len(lines) >= 2:
        parts = lines[-1].split()
        if len(parts) >= 4:
            try:
                return {"total_kb": int(parts[1]), "used_kb": int(parts[2]),
                        "free_kb": int(parts[3])}
            except ValueError:
                pass
    return None


# ---------- storage clone (backup / restore) ----------
# One job at a time. Backup streams a device-side `tar -c` over `adb shell -T`
# (shell v2 keeps stdout binary-safe and stderr separate) into a local `tar -x`;
# restore is the same pipe reversed. Unreadable files are logged and skipped.
CLONE_LOCK = threading.Lock()
CLONE_JOB = None
CLONE_EXCLUDES = ("./Android/data", "./Android/data/*",
                  "./Android/obb", "./Android/obb/*")


def new_clone_job(kind, serial, root):
    return {"kind": kind, "serial": serial, "root": root, "state": "running",
            "phase": "starting", "dest": None, "src": None,
            "bytes_done": 0, "bytes_total": 0, "files_done": 0, "files_total": 0,
            "current": "", "errors": [], "errors_total": 0, "error": None,
            "note": "", "started": time.time(), "finished": None,
            "_cancel": False, "_procs": []}


def job_public(job):
    if not job:
        return None
    d = {k: job[k] for k in (
        "kind", "state", "phase", "serial", "root", "dest", "src",
        "bytes_done", "bytes_total", "files_done", "files_total",
        "current", "errors_total", "error", "note", "started", "finished")}
    d["errors_tail"] = job["errors"][-6:]
    return d


def clone_finish(job, state):
    job["state"] = state
    job["phase"] = ""
    job["current"] = ""
    job["finished"] = time.time()
    for p in job["_procs"]:
        if p.poll() is None:
            p.kill()


def _tar_stderr_reader(pipe, job, names, count=True):
    """Drain a tar stderr. With names=True (the verbose `-cv` side), non-error
    lines are the files being copied; everything tar-flagged is an error.
    count=False shows names without counting them (bsdtar lists directories
    indistinguishably from files, so its counts would be wrong)."""
    for raw in iter(pipe.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        if names and not line.startswith("tar:"):
            n = line[2:] if line.startswith("a ") else line   # bsdtar prefixes 'a '
            n = n[2:] if n.startswith("./") else n
            if n and not n.endswith("/"):
                if count:
                    job["files_done"] += 1
                job["current"] = n
        else:
            job["errors_total"] += 1
            if len(job["errors"]) < 400:
                job["errors"].append(line)
    try:
        pipe.close()
    except OSError:
        pass


def _pump(src, dst, job):
    """Move bytes src→dst counting progress. False if cancelled."""
    while True:
        if job["_cancel"]:
            return False
        chunk = src.read(1 << 20)
        if not chunk:
            return True
        dst.write(chunk)
        job["bytes_done"] += len(chunk)


def clone_measure_device(serial, root, include_android_data):
    """Bytes + file count under root, honouring the Android/data exclusion."""
    r = root.rstrip("/")
    rc, out, _ = adb_run(serial, "shell",
                         f"du -sk {sh_quote(r)} 2>/dev/null | tail -n1",
                         timeout=1800)
    try:
        total_kb = int(out.split()[0])
    except (ValueError, IndexError):
        total_kb = 0
    if include_android_data:
        find_cmd = f"find {sh_quote(r)}/ -type f 2>/dev/null | wc -l"
    else:
        rc, out2, _ = adb_run(
            serial, "shell",
            f"du -sk {sh_quote(r + '/Android/data')} "
            f"{sh_quote(r + '/Android/obb')} 2>/dev/null", timeout=900)
        for line in out2.splitlines():
            try:
                total_kb -= int(line.split()[0])
            except (ValueError, IndexError):
                pass
        find_cmd = (f"find {sh_quote(r)}/ \\( -path {sh_quote(r + '/Android/data')} "
                    f"-o -path {sh_quote(r + '/Android/obb')} \\) -prune "
                    f"-o -type f -print 2>/dev/null | wc -l")
    rc, out3, _ = adb_run(serial, "shell", find_cmd, timeout=1800)
    try:
        files = int(out3.split()[0])
    except (ValueError, IndexError):
        files = 0
    return max(total_kb, 0) * 1024, files


def _local_tree_size(top):
    files = size = 0
    for dirpath, _dirs, names in os.walk(top):
        for n in names:
            try:
                size += os.path.getsize(os.path.join(dirpath, n))
                files += 1
            except OSError:
                pass
    return size, files


def clone_backup_worker(job):
    serial, root = job["serial"], job["root"]
    try:
        job["phase"] = "measuring phone storage (takes a minute on big phones)"
        b, f = clone_measure_device(serial, root, job["include_android_data"])
        job["bytes_total"], job["files_total"] = b, f
        if job["_cancel"]:
            return clone_finish(job, "cancelled")
        free = shutil.disk_usage(os.path.dirname(job["backup_dir"])).free
        if b and free < b * 1.02:
            job["error"] = (f"not enough space on the Mac: backup needs about "
                            f"{b // 2**30} GB, destination has {free // 2**30} GB free")
            return clone_finish(job, "error")
        os.makedirs(job["storage_dir"], exist_ok=True)

        job["phase"] = "copying from phone"
        ex = "" if job["include_android_data"] else \
            " ".join("--exclude " + sh_quote(p) for p in CLONE_EXCLUDES) + " "
        remote = f"tar -cvf - {ex}-C {sh_quote(root)} ."
        p_dev = subprocess.Popen([ADB, "-s", serial, "shell", "-T", remote],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p_tar = subprocess.Popen(["/usr/bin/tar", "-xpf", "-", "-C", job["storage_dir"]],
                                 stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        job["_procs"] = [p_dev, p_tar]
        threading.Thread(target=_tar_stderr_reader,
                         args=(p_dev.stderr, job, True), daemon=True).start()
        threading.Thread(target=_tar_stderr_reader,
                         args=(p_tar.stderr, job, False), daemon=True).start()
        ok = True
        try:
            ok = _pump(p_dev.stdout, p_tar.stdin, job)
        except (BrokenPipeError, OSError):
            ok = False
        try:
            p_tar.stdin.close()
        except OSError:
            pass
        p_dev.wait()
        tar_rc = p_tar.wait()
        if job["_cancel"] or not ok:
            if not job["_cancel"]:
                job["error"] = "extraction stopped early — " + \
                    ("; ".join(job["errors"][-2:]) or "is the Mac disk full?")
            return clone_finish(job, "cancelled" if job["_cancel"] else "error")
        if tar_rc != 0 and not os.listdir(job["storage_dir"]):
            job["error"] = "nothing was extracted — " + \
                ("; ".join(job["errors"][-2:]) or "device tar produced no data")
            return clone_finish(job, "error")

        job["phase"] = "verifying + writing manifest"
        size, files = _local_tree_size(job["storage_dir"])
        job["files_done"] = files
        job["bytes_total"] = job["bytes_done"]   # snap the bar to 100%
        manifest = {
            "app": "droid-deck", "kind": "phone-storage-backup",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "device": {"serial": serial, **device_info(serial)},
            "root": root,
            "include_android_data": job["include_android_data"],
            "files": files, "bytes": size,
            "files_expected": job["files_total"] or None,
            "skipped_or_errors": job["errors_total"],
            "duration_s": round(time.time() - job["started"]),
        }
        with open(os.path.join(job["backup_dir"], "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False)
        if job["errors"]:
            with open(os.path.join(job["backup_dir"], "errors.log"), "w",
                      encoding="utf-8") as fh:
                fh.write("\n".join(job["errors"]) + "\n")
                if job["errors_total"] > len(job["errors"]):
                    fh.write(f"... and {job['errors_total'] - len(job['errors'])} more\n")
        job["note"] = f"{files:,} files saved"
        if job["errors_total"]:
            job["note"] += f", {job['errors_total']:,} skipped (see errors.log)"
        clone_finish(job, "done")
    except Exception as e:                          # noqa: BLE001 — job must not die silently
        job["error"] = f"{type(e).__name__}: {e}"
        clone_finish(job, "error")


def clone_restore_worker(job):
    serial, root = job["serial"], job["root"]
    try:
        job["phase"] = "checking free space on phone"
        df = disk_free(serial, root)
        if df and df["free_kb"] * 1024 < job["bytes_total"] * 1.02:
            job["error"] = (f"not enough free space on the phone: needs about "
                            f"{job['bytes_total'] // 2**30} GB, "
                            f"{df['free_kb'] * 1024 // 2**30} GB free")
            return clone_finish(job, "error")
        if job["_cancel"]:
            return clone_finish(job, "cancelled")

        job["phase"] = "copying to phone"
        # COPYFILE_DISABLE stops bsdtar smuggling AppleDouble ._* metadata
        # files into the archive (they'd land on the phone as junk).
        p_tar = subprocess.Popen(
            ["/usr/bin/tar", "--exclude", "._*", "--exclude", "*/._*",
             "-cvf", "-", "-C", job["storage_dir"], "."],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "COPYFILE_DISABLE": "1"})
        p_dev = subprocess.Popen(
            [ADB, "-s", serial, "shell", "-T", f"tar -xf - -C {sh_quote(root)}"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        job["_procs"] = [p_tar, p_dev]
        threading.Thread(target=_tar_stderr_reader,
                         args=(p_tar.stderr, job, True, False), daemon=True).start()
        threading.Thread(target=_tar_stderr_reader,
                         args=(p_dev.stderr, job, False), daemon=True).start()
        ok = True
        try:
            ok = _pump(p_tar.stdout, p_dev.stdin, job)
        except (BrokenPipeError, OSError):
            ok = False
        try:
            p_dev.stdin.close()
        except OSError:
            pass
        p_tar.wait()
        dev_rc = p_dev.wait()
        if job["_cancel"] or not ok:
            if not job["_cancel"]:
                job["error"] = "the phone stopped accepting data — " + \
                    ("; ".join(job["errors"][-2:]) or "cable unplugged?")
            return clone_finish(job, "cancelled" if job["_cancel"] else "error")
        if dev_rc != 0 and job["bytes_done"] == 0:
            job["error"] = "; ".join(job["errors"][-2:]) or "device tar failed"
            return clone_finish(job, "error")

        job["phase"] = "asking the phone to re-index media"
        adb_run(serial, "shell",
                f"cmd media scan {sh_quote(root)} >/dev/null 2>&1", timeout=30)
        job["bytes_total"] = job["bytes_done"]
        job["files_done"] = job["files_total"]
        job["note"] = ("restore complete — reboot the phone so Gallery/Music "
                       "pick up everything")
        clone_finish(job, "done")
    except Exception as e:                          # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"
        clone_finish(job, "error")


def choose_folder(prompt):
    """Native macOS folder picker. Returns (path|None, error|None);
    (None, None) means the user cancelled."""
    script = f"POSIX path of (choose folder with prompt {json.dumps(prompt)})"
    try:
        p = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None, "folder dialog timed out"
    if p.returncode != 0:
        if "-128" in p.stderr:                      # user pressed Cancel
            return None, None
        return None, p.stderr.strip() or "folder dialog failed"
    return p.stdout.strip(), None


def inspect_backup(path):
    """Validate a backup folder for restore. Returns (info, error)."""
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isdir(path):
        return None, "folder not found: " + path
    manifest = None
    mf = os.path.join(path, "manifest.json")
    if os.path.isfile(mf):
        try:
            with open(mf, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            manifest = None
    storage = os.path.join(path, "storage")
    if not os.path.isdir(storage):
        storage = path
    size, files = _local_tree_size(storage)
    if files == 0:
        return None, "that folder is empty — pick the dated backup folder"
    return {"path": path, "storage": storage, "manifest": manifest,
            "bytes": size, "files": files}, None


class Handler(BaseHTTPRequestHandler):
    server_version = "droid-deck/1.0"

    # ---------- helpers ----------
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def fail(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def query(self):
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def stream_exec_out(self, serial, cmd_str, mime, disposition=None):
        proc = subprocess.Popen(
            [ADB, "-s", serial, "exec-out", cmd_str],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            proc.kill()
        finally:
            proc.stdout.close()
            proc.wait()

    # ---------- routes ----------
    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        q = self.query()
        try:
            if route == "/":
                body = HTML_PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif route == "/api/devices":
                self.send_json({"devices": list_devices()})

            elif route == "/api/ls":
                serial, path = q.get("serial"), q.get("path", "/sdcard")
                if not serial:
                    return self.fail("serial required")
                rc, entries, err = list_dir(serial, path)
                if rc != 0 and not entries:
                    return self.fail(err or "ls failed", 500)
                self.send_json({"path": path, "entries": entries,
                                "warning": err or None,
                                "df": disk_free(serial, path)})

            elif route in ("/api/file", "/api/download"):
                serial, path = q.get("serial"), q.get("path")
                if not serial or not path:
                    return self.fail("serial and path required")
                name = os.path.basename(path)
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                disp = None
                if route == "/api/download":
                    disp = "attachment; filename*=UTF-8''" + urllib.parse.quote(name)
                self.stream_exec_out(serial, f"cat {sh_quote(path)}", mime, disp)

            elif route == "/api/thumb":
                serial, path = q.get("serial"), q.get("path")
                if not serial or not path:
                    return self.fail("serial and path required")
                f = make_thumb(serial, path, q.get("mtime", ""),
                               int(q.get("size") or 0))
                if not f:
                    return self.fail("no thumbnail", 404)
                with open(f, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control",
                                 "max-age=604800, immutable")
                self.end_headers()
                self.wfile.write(data)

            elif route == "/api/screenshot":
                serial = q.get("serial")
                if not serial:
                    return self.fail("serial required")
                self.stream_exec_out(serial, "screencap -p", "image/png")

            elif route == "/api/clone/status":
                with CLONE_LOCK:
                    j = job_public(CLONE_JOB)
                self.send_json({"job": j})

            elif route == "/api/clone/df":
                serial = q.get("serial")
                if not serial:
                    return self.fail("serial required")
                self.send_json({"df": disk_free(serial, q.get("path", "/sdcard"))})

            else:
                self.fail("not found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_clone(self, route):
        global CLONE_JOB
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode()) if length else {}
        except (ValueError, UnicodeDecodeError):
            return self.fail("bad json")

        if route == "/api/clone/choose":
            path, err = choose_folder(payload.get("prompt") or "Choose a folder")
            if err:
                return self.fail(err, 500)
            return self.send_json({"path": path})   # path=None → user cancelled

        if route == "/api/clone/inspect":
            info, err = inspect_backup(payload.get("path"))
            if err:
                return self.fail(err)
            serial = payload.get("serial")
            info["df"] = disk_free(serial, payload.get("root") or "/sdcard") \
                if serial else None
            return self.send_json(info)

        if route == "/api/clone/cancel":
            with CLONE_LOCK:
                job = CLONE_JOB
            if not job or job["state"] != "running":
                return self.fail("no running clone job")
            job["_cancel"] = True
            for p in job["_procs"]:
                if p.poll() is None:
                    p.kill()
            return self.send_json({"ok": True})

        if route == "/api/clone/reveal":
            p = payload.get("path")
            if not p or not os.path.exists(p):
                return self.fail("path missing")
            subprocess.Popen(["open", p])
            return self.send_json({"ok": True})

        if route == "/api/clone/start":
            mode, serial = payload.get("mode"), payload.get("serial")
            root = payload.get("root") or "/sdcard"
            if mode not in ("backup", "restore") or not serial:
                return self.fail("mode and serial required")
            with CLONE_LOCK:
                if CLONE_JOB and CLONE_JOB["state"] == "running":
                    return self.fail("a backup/restore is already running", 409)
                job = new_clone_job(mode, serial, root)
                if mode == "backup":
                    dest = os.path.abspath(os.path.expanduser(payload.get("dest") or ""))
                    if not os.path.isdir(dest):
                        return self.fail("destination folder not found")
                    name = device_info(serial)["name"]
                    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or serial
                    bdir = os.path.join(
                        dest, f"{safe}-backup-{time.strftime('%Y%m%d-%H%M')}")
                    n = 2
                    while os.path.exists(bdir):
                        bdir = f"{bdir}-{n}"
                        n += 1
                    job.update(backup_dir=bdir,
                               storage_dir=os.path.join(bdir, "storage"),
                               dest=bdir,
                               include_android_data=bool(
                                   payload.get("include_android_data")))
                    worker = clone_backup_worker
                else:
                    info, err = inspect_backup(payload.get("src"))
                    if err:
                        return self.fail(err)
                    job.update(storage_dir=info["storage"], src=info["path"],
                               bytes_total=info["bytes"],
                               files_total=info["files"])
                    worker = clone_restore_worker
                CLONE_JOB = job
            threading.Thread(target=worker, args=(job,), daemon=True).start()
            return self.send_json({"ok": True, "job": job_public(job)})

        self.fail("not found", 404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        q = self.query()

        if route == "/api/reconnect":
            # nudge adb: revive offline transports, then rescan
            adb_run(None, "reconnect", "offline", timeout=10)
            return self.send_json({"ok": True, "devices": list_devices()})

        if route.startswith("/api/clone/"):
            return self.handle_clone(route)

        serial = q.get("serial")
        if not serial:
            return self.fail("serial required")

        length = int(self.headers.get("Content-Length") or 0)

        if route == "/api/upload":
            dirname, name = q.get("dir"), q.get("name")
            if not dirname or not name:
                return self.fail("dir and name required")
            tmp = tempfile.NamedTemporaryFile(delete=False)
            try:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    tmp.write(chunk)
                    remaining -= len(chunk)
                tmp.close()
                target = dirname.rstrip("/") + "/" + name
                rc, out, err = adb_run(serial, "push", tmp.name, target,
                                       timeout=600)
                if rc != 0:
                    return self.fail(err or out or "push failed", 500)
                self.send_json({"ok": True, "path": target})
            finally:
                os.unlink(tmp.name)
            return

        body = self.rfile.read(length) if length else b""

        if route == "/api/mkdir":
            path = q.get("path")
            rc, out, err = adb_run(serial, "shell",
                                   f"mkdir -p -- {sh_quote(path)}")
            return self.send_json({"ok": True}) if rc == 0 else \
                self.fail(err or "mkdir failed", 500)

        if route == "/api/delete":
            path = (q.get("path") or "").rstrip("/")
            protected = {"", "/", "/sdcard", "/storage",
                         "/storage/emulated/0", "/storage/emulated"}
            if path in protected:
                return self.fail("refusing to delete protected path")
            rc, out, err = adb_run(serial, "shell",
                                   f"rm -rf -- {sh_quote(path)}")
            return self.send_json({"ok": True}) if rc == 0 else \
                self.fail(err or "delete failed", 500)

        if route == "/api/rename":
            src, dst = q.get("src"), q.get("dst")
            rc, out, err = adb_run(
                serial, "shell", f"mv -- {sh_quote(src)} {sh_quote(dst)}")
            return self.send_json({"ok": True}) if rc == 0 else \
                self.fail(err or "rename failed", 500)

        if route == "/api/text":
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return self.fail("bad json")
            text = payload.get("text", "")
            mode = payload.get("mode", "type")
            if not text:
                return self.fail("empty text")

            if mode == "file":
                fname = payload.get("filename") or \
                    ("droid-deck-" + time.strftime("%Y%m%d-%H%M%S") + ".txt")
                target = "/sdcard/Download/" + fname
                tmp = tempfile.NamedTemporaryFile(delete=False, mode="w",
                                                  encoding="utf-8")
                tmp.write(text)
                tmp.close()
                try:
                    rc, out, err = adb_run(serial, "push", tmp.name, target)
                finally:
                    os.unlink(tmp.name)
                return self.send_json({"ok": True, "path": target}) \
                    if rc == 0 else self.fail(err or "push failed", 500)

            # mode == "type": simulate keystrokes into the focused field.
            # `input text` is ASCII-only; %s encodes a space.
            if not all(32 <= ord(c) < 127 for c in text):
                return self.fail(
                    "typing supports ASCII only — use 'Save as file' for "
                    "unicode or multi-line text")
            escaped = text.replace(" ", "%s")
            rc, out, err = adb_run(serial, "shell",
                                   f"input text {sh_quote(escaped)}")
            if rc == 0 and payload.get("enter"):
                adb_run(serial, "shell", "input keyevent 66")
            return self.send_json({"ok": True}) if rc == 0 else \
                self.fail(err or "input failed", 500)

        self.fail("not found", 404)


HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>droid-deck</title>
<style>
:root {
  --bg: #0d1117; --panel: #161b22; --panel2: #1c2330; --line: #2a3240;
  --text: #e6edf3; --dim: #8b98a9; --accent: #4ade80; --accent2: #38bdf8;
  --danger: #f87171; --radius: 12px;
  font-size: 15px;
}
* { box-sizing: border-box; margin: 0; }
body {
  background: var(--bg); color: var(--text); min-height: 100vh;
  font-family: -apple-system, "SF Pro Text", "Segoe UI", sans-serif;
  display: grid; grid-template-columns: 270px 1fr;
}
code, .mono { font-family: "SF Mono", ui-monospace, Menlo, monospace; }

/* ---------- sidebar ---------- */
aside {
  background: var(--panel); border-right: 1px solid var(--line);
  padding: 18px 16px; display: flex; flex-direction: column; gap: 18px;
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand .logo {
  width: 36px; height: 36px; border-radius: 10px; display: grid;
  place-items: center; font-size: 20px;
  background: linear-gradient(135deg, #14532d, #052e16);
  border: 1px solid #1f7a3f;
}
.brand h1 { font-size: 1.05rem; font-weight: 700; letter-spacing: .3px; }
.brand small { color: var(--dim); display: block; font-weight: 400; }

.card {
  background: var(--panel2); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 12px;
}
.card h2 {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .12em;
  color: var(--dim); margin-bottom: 10px;
}
select, input[type=text], textarea {
  width: 100%; background: var(--bg); color: var(--text);
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
  font: inherit; outline: none;
}
select:focus, input:focus, textarea:focus { border-color: var(--accent2); }
.devmeta { margin-top: 8px; font-size: .8rem; color: var(--dim); line-height: 1.5; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); margin-right: 6px; }
.dot.off { background: var(--danger); }

.quick { display: flex; flex-direction: column; gap: 2px; }
.quick button {
  all: unset; cursor: pointer; padding: 7px 10px; border-radius: 8px;
  color: var(--text); font-size: .88rem; display: flex; gap: 9px;
}
.quick button:hover { background: var(--panel); }

textarea { resize: vertical; min-height: 74px; font-size: .85rem; }
.row { display: flex; gap: 8px; margin-top: 8px; align-items: center; }
.row label { font-size: .78rem; color: var(--dim); display: flex; gap: 5px;
  align-items: center; }

button.btn {
  background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; padding: 7px 12px; font: inherit; font-size: .85rem;
  cursor: pointer; white-space: nowrap;
}
button.btn:hover { border-color: var(--accent2); color: var(--accent2); }
button.btn.primary {
  background: #14532d; border-color: #1f7a3f; color: #d1fae5;
}
button.btn.primary:hover { background: #166534; color: #fff; }
button.btn.danger:hover { border-color: var(--danger); color: var(--danger); }

/* ---------- main ---------- */
main { padding: 20px 26px; min-width: 0; }
.topbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.crumbs {
  flex: 1; display: flex; align-items: center; gap: 2px; flex-wrap: wrap;
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 8px 12px; min-height: 40px; min-width: 220px;
}
.crumbs a { color: var(--accent2); text-decoration: none; cursor: pointer;
  padding: 1px 4px; border-radius: 5px; }
.crumbs a:hover { background: var(--panel2); }
.crumbs .sep { color: var(--dim); }
#filter { max-width: 200px; }

.statusline { color: var(--dim); font-size: .8rem; margin: 10px 2px; }

table { width: 100%; border-collapse: collapse; margin-top: 4px; }
th {
  text-align: left; font-size: .72rem; color: var(--dim);
  text-transform: uppercase; letter-spacing: .1em; font-weight: 600;
  padding: 8px 10px; border-bottom: 1px solid var(--line);
}
td { padding: 8px 10px; border-bottom: 1px solid #1a2230; font-size: .92rem; }
tr.entry { cursor: pointer; }
tr.entry:hover td { background: var(--panel); }
td.name { display: flex; gap: 10px; align-items: center; word-break: break-all; }
td .icon { width: 22px; text-align: center; flex: none; }
td.size, td.date { color: var(--dim); white-space: nowrap; font-size: .82rem; }
td.actions { white-space: nowrap; text-align: right; }
td.actions button {
  all: unset; cursor: pointer; padding: 4px 7px; border-radius: 6px;
  font-size: .85rem; opacity: 0; transition: opacity .1s;
}
tr.entry:hover td.actions button { opacity: .85; }
td.actions button:hover { background: var(--panel2); opacity: 1; }
.linkhint { color: var(--dim); font-size: .78rem; }

.empty { text-align: center; color: var(--dim); padding: 70px 0; }
.empty .big { font-size: 2.6rem; margin-bottom: 12px; }

/* ---------- sorting ---------- */
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { color: var(--text); }
th .arrow { font-size: .75em; color: var(--accent2); }
#gridsort { display: none; gap: 6px; align-items: center; }
body.view-grid #gridsort { display: flex; }
#sortsel { width: auto; padding: 7px 8px; font-size: .85rem; }

/* ---------- selection ---------- */
.check { accent-color: var(--accent); cursor: pointer; }
tr.entry .check { transform: scale(1.15); opacity: 0; flex: none; }
tr.entry:hover .check, tr.entry .check:checked,
body.selecting tr.entry .check { opacity: 1; }
tr.entry.sel td { background: rgba(74, 222, 128, .07); }
#selbar {
  position: fixed; left: 50%; transform: translateX(-50%); bottom: 22px;
  background: var(--panel2); border: 1px solid var(--accent2);
  border-radius: 12px; padding: 9px 14px; display: none; gap: 8px;
  align-items: center; z-index: 40; box-shadow: 0 8px 30px rgba(0,0,0,.5);
}
#selbar.on { display: flex; }
#selcount { font-size: .88rem; margin-right: 4px; white-space: nowrap; }

/* ---------- grid view ---------- */
body.view-grid table { display: none; }
#grid { display: none; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px; margin-top: 14px; }
body.view-grid #grid { display: grid; }
.gcard {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  overflow: hidden; cursor: pointer; position: relative;
}
.gcard:hover { border-color: var(--accent2); }
.gcard.sel { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.gcard .thumb {
  height: 118px; display: grid; place-items: center; font-size: 2.5rem;
  background: var(--panel2); overflow: hidden;
}
.gcard .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.gcard .label {
  padding: 7px 9px 2px; font-size: .78rem; line-height: 1.3;
  word-break: break-all; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; min-height: 2em;
}
.gcard .sub { padding: 0 9px 8px; color: var(--dim); font-size: .7rem; }
.gcard .check {
  position: absolute; top: 8px; left: 8px; transform: scale(1.3);
  opacity: 0; z-index: 2;
}
.gcard:hover .check, .gcard .check:checked,
body.selecting .gcard .check { opacity: 1; }

/* ---------- preview nav ---------- */
#modal .nav { display: flex; align-items: center; gap: 4px; }
#mcount { color: var(--dim); font-size: .8rem; white-space: nowrap; }

/* ---------- drop overlay ---------- */
#drop {
  position: fixed; inset: 0; background: rgba(13,17,23,.85); z-index: 50;
  display: none; place-items: center; border: 3px dashed var(--accent);
  border-radius: 18px; margin: 14px; pointer-events: none;
}
#drop.on { display: grid; }
#drop div { font-size: 1.3rem; color: var(--accent); }

/* ---------- modal ---------- */
#modal {
  position: fixed; inset: 0; background: rgba(0,0,0,.65); z-index: 60;
  display: none; place-items: center; padding: 30px;
}
#modal.on { display: grid; }
#modal .box {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); max-width: min(960px, 92vw);
  max-height: 88vh; display: flex; flex-direction: column; overflow: hidden;
  min-width: 340px;
}
#modal header {
  display: flex; align-items: center; gap: 10px; padding: 12px 16px;
  border-bottom: 1px solid var(--line);
}
#modal header .title { flex: 1; font-weight: 600; word-break: break-all; }
#modal .body { overflow: auto; padding: 16px; }
#modal img, #modal video { max-width: 100%; max-height: 68vh; border-radius: 8px; }
#modal pre {
  white-space: pre-wrap; word-break: break-word; font-size: .82rem;
  font-family: "SF Mono", ui-monospace, Menlo, monospace; line-height: 1.5;
}

/* ---------- connection lost ---------- */
#lost {
  position: fixed; inset: 0; z-index: 80; display: none; place-items: center;
  background: rgba(13,17,23,.55);
  backdrop-filter: blur(7px); -webkit-backdrop-filter: blur(7px);
}
#lost.on { display: grid; }
.lostcard {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 16px; padding: 34px 46px; text-align: center;
  box-shadow: 0 20px 60px rgba(0,0,0,.6); animation: pop .18s ease-out;
}
.lostcard .big { font-size: 3rem; margin-bottom: 10px; }
.lostcard h3 { margin-bottom: 6px; font-size: 1.15rem; }
.lostcard p { color: var(--dim); margin-bottom: 20px; font-size: .9rem; }
.lostcard .hint { color: var(--dim); font-size: .75rem; margin-top: 16px; }
.lostcard .btn { font-size: 1rem; padding: 10px 22px; }
@keyframes spin { to { transform: rotate(360deg); } }
.spinning { display: inline-block; animation: spin 1s linear infinite; }

/* ---------- clone overlay ---------- */
#clone {
  position: fixed; inset: 0; z-index: 75; display: none; place-items: center;
  background: rgba(13,17,23,.6);
  backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
}
#clone.on { display: grid; }
.clonecard {
  background: var(--panel); border: 1px solid var(--line); border-radius: 16px;
  padding: 24px 28px; width: min(620px, 94vw);
  box-shadow: 0 20px 60px rgba(0,0,0,.6); animation: pop .18s ease-out;
}
.clonecard h3 { margin-bottom: 4px; }
.clonecard .sub { color: var(--dim); font-size: .85rem; margin-bottom: 14px;
  line-height: 1.45; }
.ctbl { width: 100%; font-size: .85rem; margin-bottom: 12px; border-collapse: collapse; }
.ctbl td { padding: 5px 8px 5px 0; border: 0; vertical-align: top;
  word-break: break-all; }
.ctbl td:first-child { color: var(--dim); white-space: nowrap; width: 96px; }
.bar { height: 12px; background: var(--bg); border: 1px solid var(--line);
  border-radius: 7px; overflow: hidden; margin: 12px 0 4px; }
.bar div { height: 100%; width: 0%; border-radius: 6px;
  background: linear-gradient(90deg, #1f7a3f, var(--accent)); transition: width .5s; }
.bar.busy div { width: 30%; animation: slide 1.2s ease-in-out infinite alternate; }
@keyframes slide { from { margin-left: 0 } to { margin-left: 70% } }
.cstats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;
  margin: 10px 0; font-size: .72rem; color: var(--dim); text-align: center; }
.cstats b { display: block; color: var(--text); font-size: .95rem; margin-bottom: 2px;
  font-variant-numeric: tabular-nums; }
.ccur { font-size: .74rem; color: var(--dim); word-break: break-all; min-height: 2.1em; }
#cerr { max-height: 130px; overflow: auto; font-size: .72rem; color: var(--danger);
  white-space: pre-wrap; margin-top: 8px; display: none;
  font-family: "SF Mono", ui-monospace, Menlo, monospace; }
.cbtns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
.cwarn { color: var(--danger); font-size: .8rem; margin: 6px 0; }

/* ---------- toasts ---------- */
#toasts { position: fixed; right: 18px; bottom: 18px; z-index: 70;
  display: flex; flex-direction: column; gap: 8px; }
.toast {
  background: var(--panel2); border: 1px solid var(--line); color: var(--text);
  border-radius: 10px; padding: 10px 14px; font-size: .88rem; max-width: 380px;
  box-shadow: 0 6px 24px rgba(0,0,0,.4); animation: pop .15s ease-out;
}
.toast.err { border-color: var(--danger); }
.toast.ok { border-color: #1f7a3f; }
@keyframes pop { from { transform: translateY(8px); opacity: 0; } }

@media (max-width: 860px) {
  body { grid-template-columns: 1fr; }
  aside { position: static; height: auto; }
}
</style>
</head>
<body>

<aside>
  <div class="brand">
    <div class="logo">🤖</div>
    <div><h1>droid-deck</h1><small>ADB file bridge</small></div>
  </div>

  <div class="card">
    <h2>Device</h2>
    <select id="devsel"></select>
    <div class="devmeta" id="devmeta">scanning…</div>
    <div class="row">
      <button class="btn" id="refreshdev">↻ Rescan</button>
      <button class="btn" id="shot">📸 Screenshot</button>
    </div>
  </div>

  <div class="card">
    <h2>Quick locations</h2>
    <div class="quick" id="quick"></div>
  </div>

  <div class="card">
    <h2>Clone storage</h2>
    <div class="row" style="margin-top:0">
      <button class="btn primary" id="bkbtn">💾 Backup</button>
      <button class="btn" id="rsbtn">📲 Restore</button>
    </div>
    <div class="devmeta">Copy the phone's entire shared storage into a Mac
    folder, or push a saved backup onto a new phone.</div>
  </div>

  <div class="card">
    <h2>Send text to device</h2>
    <textarea id="sendtext" placeholder="Text to send…"></textarea>
    <div class="row">
      <button class="btn primary" id="typebtn" title="Types into the focused input field on the phone">⌨️ Type</button>
      <button class="btn" id="filebtn" title="Saves as a .txt in /sdcard/Download">📄 As file</button>
      <label><input type="checkbox" id="enterchk"> + Enter</label>
    </div>
    <div class="devmeta">Typing needs a focused text field on the phone (ASCII only). “As file” handles anything.</div>
  </div>
</aside>

<main>
  <div class="topbar">
    <button class="btn" id="upbtn" title="Parent folder">←</button>
    <div class="crumbs" id="crumbs"></div>
    <input type="text" id="filter" placeholder="Filter…">
    <span id="gridsort">
      <select id="sortsel">
        <option value="name">Name</option>
        <option value="size">Size</option>
        <option value="mtime">Modified</option>
      </select>
      <button class="btn" id="dirbtn" title="Sort direction">↓</button>
    </span>
    <button class="btn" id="viewbtn" title="Toggle list/grid view">▦</button>
    <button class="btn" id="newdir">＋ Folder</button>
    <button class="btn primary" id="uploadbtn">⬆ Upload</button>
    <input type="file" id="filepick" multiple hidden>
  </div>
  <div class="statusline" id="status"></div>
  <table>
    <thead><tr>
      <th class="sortable" data-key="name">Name <span class="arrow"></span></th>
      <th class="sortable" data-key="size" style="width:90px">Size <span class="arrow"></span></th>
      <th class="sortable" data-key="mtime" style="width:140px">Modified <span class="arrow"></span></th>
      <th style="width:110px"></th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="grid"></div>
  <div class="empty" id="empty" style="display:none"></div>
</main>

<div id="selbar">
  <span id="selcount"></span>
  <button class="btn" id="selall">Select all</button>
  <button class="btn primary" id="seldl">⬇ Download</button>
  <button class="btn danger" id="selrm">🗑 Delete</button>
  <button class="btn" id="selclear">✕</button>
</div>

<div id="drop"><div>⬆ &nbsp;Drop files to upload to <span id="droppath"></span></div></div>

<div id="modal">
  <div class="box">
    <header>
      <span class="title" id="mtitle"></span>
      <span class="nav" id="mnav" style="display:none">
        <button class="btn" id="mprev" title="Previous (←)">‹</button>
        <span id="mcount"></span>
        <button class="btn" id="mnext" title="Next (→)">›</button>
      </span>
      <button class="btn" id="mcopy" style="display:none">📋 Copy</button>
      <button class="btn" id="mdl">⬇ Download</button>
      <button class="btn" id="mclose">✕</button>
    </header>
    <div class="body" id="mbody"></div>
  </div>
</div>

<div id="lost">
  <div class="lostcard">
    <div class="big">📵</div>
    <h3>Device disconnected</h3>
    <p id="lostname">The ADB connection was lost.</p>
    <button class="btn primary" id="reconnect"><span id="reconicon">↻</span> Reconnect</button>
    <div class="hint">Auto-retrying every 3 s — check the USB cable / debugging prompt</div>
  </div>
</div>

<div id="clone"><div class="clonecard" id="cbody"></div></div>

<div id="toasts"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const state = {
  serial: null, path: "/sdcard", entries: [], visible: [],
  view: localStorage.getItem("dd.view") || "list",
  sortKey: localStorage.getItem("dd.sortKey") || "name",
  sortDir: +(localStorage.getItem("dd.sortDir") || 1),
  selected: new Set(),
};

const QUICK = [
  ["🏠", "Internal storage", "/sdcard"],
  ["⬇️", "Download", "/sdcard/Download"],
  ["📷", "DCIM / Camera", "/sdcard/DCIM"],
  ["🖼", "Pictures", "/sdcard/Pictures"],
  ["🎬", "Movies", "/sdcard/Movies"],
  ["🎵", "Music", "/sdcard/Music"],
  ["📄", "Documents", "/sdcard/Documents"],
  ["💬", "WhatsApp media", "/sdcard/Android/media/com.whatsapp/WhatsApp/Media"],
];
const TEXT_EXT = new Set(("txt md json xml log csv tsv html htm css js ts jsx tsx java kt kts py sh zsh yaml yml conf ini prop properties gradle c cpp h hpp cfg toml sql srt vtt smali mk env gitignore").split(" "));
const IMG_EXT  = new Set("jpg jpeg png gif webp bmp svg ico heic avif".split(" "));
const VID_EXT  = new Set("mp4 webm mov 3gp mkv".split(" "));
const AUD_EXT  = new Set("mp3 m4a ogg wav flac opus aac amr".split(" "));

const ext = n => (n.includes(".") ? n.split(".").pop().toLowerCase() : "");
const esc = s => s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function icon(e) {
  if (e.type === "dir") return "📁";
  if (e.type === "link") return "🔗";
  const x = ext(e.name);
  if (IMG_EXT.has(x)) return "🖼";
  if (VID_EXT.has(x)) return "🎬";
  if (AUD_EXT.has(x)) return "🎵";
  if (x === "apk") return "📦";
  if (x === "pdf") return "📕";
  if (["zip","tar","gz","7z","rar"].includes(x)) return "🗜";
  if (TEXT_EXT.has(x)) return "📝";
  return "📄";
}
function human(n) {
  if (!n) return "";
  const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + u[i];
}
function toast(msg, cls) {
  const el = document.createElement("div");
  el.className = "toast " + (cls || "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
async function api(url, opts) {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const fileUrl = (p, dl) =>
  `/api/${dl ? "download" : "file"}?serial=${encodeURIComponent(state.serial)}&path=${encodeURIComponent(p)}`;
const thumbUrl = (p, e) =>
  `/api/thumb?serial=${encodeURIComponent(state.serial)}&path=${encodeURIComponent(p)}` +
  `&mtime=${encodeURIComponent(e.mtime)}&size=${e.size}`;
const fullPath = name => state.path.replace(/\/$/, "") + "/" + name;

/* ---------- devices ---------- */
const devLabel = d => d.name || d.model || d.serial;

async function loadDevices(keep) {
  const j = await api("/api/devices");
  state.devices = j.devices;
  const sel = $("devsel");
  const prev = keep ? state.serial : null;
  sel.innerHTML = "";
  for (const d of j.devices) {
    const o = document.createElement("option");
    o.value = d.serial;
    o.textContent = devLabel(d);
    sel.appendChild(o);
  }
  if (!j.devices.length) {
    state.serial = null;
    $("devmeta").innerHTML = '<span class="dot off"></span>no device — plug one in & allow USB debugging';
    state.entries = [];
    render();
    showEmpty("🔌", "No ADB device detected");
    return;
  }
  const found = j.devices.find(d => d.serial === prev);
  sel.value = found ? found.serial : j.devices[0].serial;
  pickDevice();
}
function pickDevice() {
  state.serial = $("devsel").value;
  const d = (state.devices || []).find(x => x.serial === state.serial);
  if (d) {
    const ok = d.state === "device";
    $("devmeta").innerHTML =
      `<span class="dot${ok ? "" : " off"}"></span>` +
      esc(ok ? "connected" : d.state) +
      (d.android ? ` · Android ${esc(d.android)}` : "") +
      ` · <span class="mono">${esc(d.serial)}</span>`;
    $("lostname").textContent = `${devLabel(d)} stopped responding — reconnect it via USB.`;
  }
  ls(state.path);
}

/* ---------- connection watchdog ---------- */
let lost = false;
function setLost(on) {
  if (on === lost) return;
  lost = on;
  $("lost").classList.toggle("on", on);
  if (on) $("devmeta").innerHTML = '<span class="dot off"></span>disconnected';
}
async function checkAlive() {
  let j;
  try { j = await api("/api/devices"); } catch (e) { return; }  // server hiccup
  state.devices = j.devices;
  const cur = j.devices.find(d => d.serial === state.serial && d.state === "device");
  if (state.serial && !cur) {
    setLost(true);
  } else if (lost && cur) {
    setLost(false);
    toast("Device reconnected ✓", "ok");
    loadDevices(true);
  } else if (!state.serial && j.devices.length) {
    loadDevices();
  }
}
async function reconnect() {
  const ic = $("reconicon");
  ic.classList.add("spinning");
  try {
    await api("/api/reconnect", { method: "POST" });
    await checkAlive();
    if (lost) toast("Still no device — check the cable", "err");
  } catch (e) { toast(e.message, "err"); }
  ic.classList.remove("spinning");
}

/* ---------- listing ---------- */
function showEmpty(big, msg) {
  $("empty").style.display = "block";
  $("empty").innerHTML = `<div class="big">${big}</div>${esc(msg)}`;
}
async function ls(path) {
  if (!state.serial) return;
  try {
    const j = await api(`/api/ls?serial=${encodeURIComponent(state.serial)}&path=${encodeURIComponent(path)}`);
    if (j.path !== state.path) clearSel();
    state.path = j.path;
    state.entries = j.entries;
    if (j.warning) toast(j.warning, "err");
    renderCrumbs();
    render();
    let st = `${j.entries.length} item${j.entries.length === 1 ? "" : "s"}`;
    if (j.df) st += ` · ${human(j.df.free_kb * 1024)} free of ${human(j.df.total_kb * 1024)}`;
    $("status").textContent = st;
  } catch (e) {
    if (/not found|offline|no devices|closed/i.test(e.message)) setLost(true);
    else toast(e.message, "err");
  }
}
function renderCrumbs() {
  const parts = state.path.split("/").filter(Boolean);
  let acc = "", html = `<a data-p="/">/</a>`;
  for (const p of parts) {
    acc += "/" + p;
    html += `<span class="sep">›</span><a data-p="${esc(acc)}">${esc(p)}</a>`;
  }
  $("crumbs").innerHTML = html;
  $("crumbs").querySelectorAll("a").forEach(a =>
    a.onclick = () => ls(a.dataset.p));
  $("droppath").textContent = state.path;
}
function cmp(a, b) {
  if ((a.type === "dir") !== (b.type === "dir")) return a.type === "dir" ? -1 : 1;
  let r;
  if (state.sortKey === "size") r = a.size - b.size;
  else if (state.sortKey === "mtime") r = a.mtime < b.mtime ? -1 : a.mtime > b.mtime ? 1 : 0;
  else r = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });
  return r * state.sortDir ||
    a.name.localeCompare(b.name, undefined, { numeric: true });
}
function setSort(key, dir) {
  if (dir !== undefined) state.sortDir = dir;
  else if (state.sortKey === key) state.sortDir *= -1;
  else state.sortDir = key === "name" ? 1 : -1;   // size/date: biggest/newest first
  state.sortKey = key;
  localStorage.setItem("dd.sortKey", state.sortKey);
  localStorage.setItem("dd.sortDir", state.sortDir);
  render();
}
function open_(e) {
  if (e.type === "dir" || e.type === "link") return ls(fullPath(e.name));
  openPreview(e);
}
function toggleSel(full, on) {
  on ? state.selected.add(full) : state.selected.delete(full);
  document.body.classList.toggle("selecting", state.selected.size > 0);
  updateSelbar();
}
function updateSelbar() {
  $("selbar").classList.toggle("on", state.selected.size > 0);
  $("selcount").textContent = state.selected.size + " selected";
}
function clearSel() {
  state.selected.clear();
  document.body.classList.remove("selecting");
  updateSelbar();
}

function render() {
  const f = $("filter").value.toLowerCase();
  const shown = state.entries
    .filter(e => !f || e.name.toLowerCase().includes(f)).sort(cmp);
  state.visible = shown;
  document.body.classList.toggle("view-grid", state.view === "grid");
  $("viewbtn").textContent = state.view === "grid" ? "☰" : "▦";
  $("viewbtn").title = state.view === "grid" ? "List view" : "Grid view (thumbnails)";
  $("sortsel").value = state.sortKey;
  $("dirbtn").textContent = state.sortDir === 1 ? "↓" : "↑";
  document.querySelectorAll("th.sortable .arrow").forEach(s => {
    s.textContent = s.parentElement.dataset.key === state.sortKey
      ? (state.sortDir === 1 ? "▲" : "▼") : "";
  });
  $("empty").style.display = shown.length ? "none" : "block";
  if (!shown.length && state.serial)
    showEmpty("🗂", f ? "Nothing matches the filter" : "Empty folder");
  state.view === "grid" ? renderGrid(shown) : renderList(shown);
  updateSelbar();
}

function checkbox(full) {
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "check";
  cb.checked = state.selected.has(full);
  cb.onclick = ev => { ev.stopPropagation(); toggleSel(full, cb.checked); markSel(); };
  return cb;
}
function markSel() {   // refresh row/card highlight without a full re-render
  document.querySelectorAll("[data-full]").forEach(el =>
    el.classList.toggle("sel", state.selected.has(el.dataset.full)));
}

function renderList(shown) {
  const rows = $("rows");
  rows.innerHTML = "";
  $("grid").innerHTML = "";
  for (const e of shown) {
    const full = fullPath(e.name);
    const tr = document.createElement("tr");
    tr.className = "entry" + (state.selected.has(full) ? " sel" : "");
    tr.dataset.full = full;
    const link = e.target ? ` <span class="linkhint">→ ${esc(e.target)}</span>` : "";
    tr.innerHTML =
      `<td class="name"><span class="icon">${icon(e)}</span><span>${esc(e.name)}${link}</span></td>` +
      `<td class="size">${e.type === "file" ? human(e.size) : ""}</td>` +
      `<td class="date">${esc(e.mtime)}</td>` +
      `<td class="actions">` +
      (e.type !== "dir" ? `<button title="Download" data-a="dl">⬇</button>` : "") +
      `<button title="Rename" data-a="mv">✏️</button>` +
      `<button title="Delete" data-a="rm">🗑</button></td>`;
    tr.querySelector("td.name").prepend(checkbox(full));
    tr.onclick = ev => {
      const a = ev.target.closest("button")?.dataset.a;
      if (a === "dl") return download(full);
      if (a === "mv") return renameEntry(full, e.name);
      if (a === "rm") return del(full, e.name);
      open_(e);
    };
    rows.appendChild(tr);
  }
}

function renderGrid(shown) {
  const grid = $("grid");
  grid.innerHTML = "";
  $("rows").innerHTML = "";
  for (const e of shown) {
    const full = fullPath(e.name);
    const card = document.createElement("div");
    card.className = "gcard" + (state.selected.has(full) ? " sel" : "");
    card.dataset.full = full;
    const th = document.createElement("div");
    th.className = "thumb";
    if (e.type === "file" && IMG_EXT.has(ext(e.name))) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.src = thumbUrl(full, e);
      img.addEventListener("error", () => { th.textContent = icon(e); });
      th.appendChild(img);
    } else {
      th.textContent = icon(e);
    }
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = e.name;
    const sub = document.createElement("div");
    sub.className = "sub";
    sub.textContent = e.type === "file"
      ? `${human(e.size)} · ${e.mtime}` : e.mtime;
    card.append(checkbox(full), th, label, sub);
    card.onclick = () => open_(e);
    grid.appendChild(card);
  }
}

/* ---------- multi-select actions ---------- */
async function downloadSelected() {
  const dirs = state.visible.filter(e => e.type === "dir" && state.selected.has(fullPath(e.name)));
  if (dirs.length) toast(`Skipping ${dirs.length} folder(s) — files only`, "err");
  const files = [...state.selected].filter(p =>
    !dirs.some(d => fullPath(d.name) === p));
  for (const p of files) {
    download(p);
    await new Promise(r => setTimeout(r, 500));  // let the browser keep up
  }
}
async function deleteSelected() {
  const n = state.selected.size;
  if (!confirm(`Delete ${n} item(s) from the device?`)) return;
  for (const p of [...state.selected]) {
    try {
      await api(`/api/delete?serial=${encodeURIComponent(state.serial)}&path=${encodeURIComponent(p)}`, { method: "POST" });
    } catch (e) { toast(`${p.split("/").pop()}: ${e.message}`, "err"); }
  }
  toast(`Deleted ${n} item(s)`, "ok");
  clearSel();
  ls(state.path);
}

/* ---------- actions ---------- */
function download(p) {
  const a = document.createElement("a");
  a.href = fileUrl(p, true);
  a.download = "";
  a.click();
  toast("Downloading " + p.split("/").pop(), "ok");
}
async function del(p, name) {
  if (!confirm(`Delete “${name}” from the device?\n\n${p}`)) return;
  try {
    await api(`/api/delete?serial=${encodeURIComponent(state.serial)}&path=${encodeURIComponent(p)}`, { method: "POST" });
    state.selected.delete(p);
    toast("Deleted " + name, "ok");
    ls(state.path);
  } catch (e) { toast(e.message, "err"); }
}
async function renameEntry(p, name) {
  const nn = prompt("New name:", name);
  if (!nn || nn === name) return;
  const dst = p.slice(0, p.lastIndexOf("/") + 1) + nn;
  try {
    await api(`/api/rename?serial=${encodeURIComponent(state.serial)}&src=${encodeURIComponent(p)}&dst=${encodeURIComponent(dst)}`, { method: "POST" });
    toast("Renamed to " + nn, "ok");
    ls(state.path);
  } catch (e) { toast(e.message, "err"); }
}
async function mkdir() {
  const n = prompt("New folder name:");
  if (!n) return;
  const p = state.path.replace(/\/$/, "") + "/" + n;
  try {
    await api(`/api/mkdir?serial=${encodeURIComponent(state.serial)}&path=${encodeURIComponent(p)}`, { method: "POST" });
    toast("Created " + n, "ok");
    ls(state.path);
  } catch (e) { toast(e.message, "err"); }
}
async function uploadFiles(files) {
  if (!state.serial) return toast("No device", "err");
  for (const f of files) {
    toast(`Uploading ${f.name} (${human(f.size)})…`);
    try {
      await api(`/api/upload?serial=${encodeURIComponent(state.serial)}` +
        `&dir=${encodeURIComponent(state.path)}&name=${encodeURIComponent(f.name)}`,
        { method: "POST", body: f });
      toast(`✓ ${f.name} → ${state.path}`, "ok");
    } catch (e) { toast(`${f.name}: ${e.message}`, "err"); }
  }
  ls(state.path);
}

/* ---------- preview ---------- */
let currentPreview = null;
let pvList = [], pvIdx = -1;
function openModal(title) {
  $("mtitle").textContent = title;
  $("mbody").innerHTML = "";
  $("mcopy").style.display = "none";
  $("mnav").style.display = "none";
  $("modal").classList.add("on");
}
function closeModal() {
  $("modal").classList.remove("on");
  $("mbody").innerHTML = "";
  currentPreview = null;
  pvIdx = -1;
}
function openPreview(e) {
  pvList = state.visible.filter(x => x.type === "file");
  pvIdx = pvList.indexOf(e);
  showPv();
}
function showPv() {
  const e = pvList[pvIdx];
  preview(fullPath(e.name), e);
  if (pvList.length > 1) {
    $("mnav").style.display = "";
    $("mcount").textContent = `${pvIdx + 1} / ${pvList.length}`;
  }
}
function pvStep(d) {
  if (pvIdx < 0 || !$("modal").classList.contains("on")) return;
  pvIdx = (pvIdx + d + pvList.length) % pvList.length;
  showPv();
}
async function preview(p, e) {
  currentPreview = p;
  const x = ext(e.name);
  openModal(e.name);
  $("mdl").onclick = () => download(p);
  const body = $("mbody");
  if (IMG_EXT.has(x)) {
    body.innerHTML = `<img src="${fileUrl(p)}" alt="">`;
    setupImgCopy(p);
  } else if (VID_EXT.has(x)) {
    body.innerHTML = `<video src="${fileUrl(p)}" controls autoplay></video>`;
  } else if (AUD_EXT.has(x)) {
    body.innerHTML = `<audio src="${fileUrl(p)}" controls autoplay style="width:100%"></audio>`;
  } else if (TEXT_EXT.has(x) || e.size < 262144) {
    body.innerHTML = "<pre>loading…</pre>";
    try {
      const r = await fetch(fileUrl(p));
      const buf = await r.arrayBuffer();
      const bytes = new Uint8Array(buf.slice(0, 512));
      if (bytes.includes(0)) {
        body.innerHTML = `<div class="empty"><div class="big">📄</div>Binary file (${human(e.size)}) — use Download</div>`;
        return;
      }
      const text = new TextDecoder().decode(buf.slice(0, 1048576));
      body.innerHTML = "";
      const pre = document.createElement("pre");
      pre.textContent = text + (buf.byteLength > 1048576 ? "\n\n… truncated (1 MB shown)" : "");
      body.appendChild(pre);
      $("mcopy").style.display = "";
      $("mcopy").onclick = async () => {
        await navigator.clipboard.writeText(text);
        toast("Copied to Mac clipboard ✓", "ok");
      };
    } catch (err) { body.innerHTML = `<pre>${esc(err.message)}</pre>`; }
  } else {
    body.innerHTML = `<div class="empty"><div class="big">${icon(e)}</div>${human(e.size)} — no preview, use Download</div>`;
  }
}
function setupImgCopy(p) {
  $("mcopy").style.display = "";
  $("mcopy").onclick = async () => {
    try {
      const blob = await (await fetch(fileUrl(p))).blob();
      // clipboard API wants PNG; convert via canvas
      const bmp = await createImageBitmap(blob);
      const cv = document.createElement("canvas");
      cv.width = bmp.width; cv.height = bmp.height;
      cv.getContext("2d").drawImage(bmp, 0, 0);
      const png = await new Promise(res => cv.toBlob(res, "image/png"));
      await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
      toast("Image copied to Mac clipboard ✓", "ok");
    } catch (e) { toast("Copy failed: " + e.message, "err"); }
  };
}

/* ---------- screenshot ---------- */
async function screenshot() {
  if (!state.serial) return toast("No device", "err");
  openModal("Screenshot");
  const url = `/api/screenshot?serial=${encodeURIComponent(state.serial)}&t=${Math.floor(performance.timeOrigin + performance.now())}`;
  $("mbody").innerHTML = `<img src="${url}" alt="screenshot">`;
  $("mcopy").style.display = "";
  $("mcopy").onclick = async () => {
    try {
      const blob = await (await fetch(url)).blob();
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      toast("Screenshot copied ✓", "ok");
    } catch (e) { toast("Copy failed: " + e.message, "err"); }
  };
  $("mdl").onclick = () => {
    const a = document.createElement("a");
    a.href = url; a.download = "screenshot.png"; a.click();
  };
}

/* ---------- storage clone (backup / restore) ---------- */
let lastCloneJob = null, clonePrev = null, cloneSpeed = 0;

const jpost = (url, obj) => api(url, { method: "POST",
  headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) });
const fmtDur = s => {
  s = Math.max(0, Math.round(s));
  const h = s / 3600 | 0, m = (s % 3600) / 60 | 0;
  return h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
};
function cloneOpen(html) { $("cbody").innerHTML = html; $("clone").classList.add("on"); }
function cloneClose() { $("clone").classList.remove("on"); }

async function chooseFolder(prompt) {
  toast("Check the folder dialog — it may open behind this window…");
  const j = await jpost("/api/clone/choose", { prompt });
  return j.path;
}

async function beginBackup() {
  if (!state.serial) return toast("No device", "err");
  let dest;
  try { dest = await chooseFolder("Where should the phone backup be saved?"); }
  catch (e) { return toast(e.message, "err"); }
  if (!dest) return;
  const d = (state.devices || []).find(x => x.serial === state.serial) || {};
  cloneOpen(`
    <h3>💾 Backup phone → Mac</h3>
    <div class="sub">Copies the phone's entire shared storage — photos, videos,
      downloads, WhatsApp media, documents — into a new dated folder.
      Apps themselves are not included, storage only.</div>
    <table class="ctbl">
      <tr><td>Phone</td><td><b>${esc(devLabel(d) || state.serial)}</b> · <span class="mono">/sdcard</span></td></tr>
      <tr><td>Save into</td><td class="mono">${esc(dest)}</td></tr>
      <tr><td>Size</td><td id="cdf">measuring…</td></tr>
    </table>
    <label style="font-size:.8rem;color:var(--dim);display:flex;gap:7px;align-items:flex-start">
      <input type="checkbox" id="incdata" class="check" style="margin-top:2px">
      <span>Also try <span class="mono">Android/data</span> + <span class="mono">obb</span>
      (app-private files — many are locked by Android and will be skipped with errors)</span>
    </label>
    <div class="cbtns">
      <button class="btn" id="cno">Cancel</button>
      <button class="btn primary" id="cgo">Start backup</button>
    </div>`);
  api(`/api/clone/df?serial=${encodeURIComponent(state.serial)}&path=/sdcard`)
    .then(j => { if (j.df && $("cdf")) $("cdf").textContent =
      `up to ${human(j.df.used_kb * 1024)} — exact size is measured when it starts`; })
    .catch(() => { if ($("cdf")) $("cdf").textContent = "unknown"; });
  $("cno").onclick = cloneClose;
  $("cgo").onclick = () => startClone({ mode: "backup", serial: state.serial,
    dest, include_android_data: $("incdata").checked });
}

async function beginRestore() {
  if (!state.serial) return toast("No device", "err");
  let src;
  try { src = await chooseFolder("Pick the backup folder (the dated one that contains manifest.json)"); }
  catch (e) { return toast(e.message, "err"); }
  if (!src) return;
  let ins;
  try { ins = await jpost("/api/clone/inspect", { path: src, serial: state.serial }); }
  catch (e) { return toast(e.message, "err"); }
  const d = (state.devices || []).find(x => x.serial === state.serial) || {};
  const m = ins.manifest;
  const free = ins.df ? ins.df.free_kb * 1024 : null;
  const tooSmall = free !== null && free < ins.bytes * 1.02;
  cloneOpen(`
    <h3>📲 Restore backup → phone</h3>
    <div class="sub">Pushes the backup into the phone's storage. Files that
      already exist under the same names are overwritten.</div>
    <table class="ctbl">
      <tr><td>Backup</td><td class="mono">${esc(ins.path)}</td></tr>
      ${m ? `<tr><td>Made from</td><td><b>${esc(m.device && (m.device.name || m.device.serial) || "?")}</b> on ${esc(m.created || "?")}</td></tr>`
          : `<tr><td></td><td class="cwarn">⚠ No manifest.json — not made by droid-deck; the folder's contents will be pushed as-is into /sdcard.</td></tr>`}
      <tr><td>Contents</td><td>${ins.files.toLocaleString()} files · ${human(ins.bytes)}</td></tr>
      <tr><td>Onto phone</td><td><b>${esc(devLabel(d) || state.serial)}</b>${free !== null ? ` · ${human(free)} free` : ""}</td></tr>
    </table>
    ${tooSmall ? `<div class="cwarn">⚠ Not enough free space on the phone for this backup.</div>` : ""}
    <div class="cbtns">
      <button class="btn" id="cno">Cancel</button>
      <button class="btn primary" id="cgo" ${tooSmall ? "disabled" : ""}>Start restore</button>
    </div>`);
  $("cno").onclick = cloneClose;
  $("cgo").onclick = () => startClone({ mode: "restore", serial: state.serial, src });
}

async function startClone(payload) {
  try {
    const j = await jpost("/api/clone/start", payload);
    clonePrev = null; cloneSpeed = 0;
    renderCloneRun(j.job, true);
  } catch (e) { toast(e.message, "err"); }
}

function renderCloneRun(job, fresh) {
  if (fresh || !$("cbar")) {
    cloneOpen(`
      <h3 id="chead"></h3>
      <div class="sub" id="cphase"></div>
      <div class="bar" id="cbarw"><div id="cbar"></div></div>
      <div class="cstats">
        <div><b id="cbytes">—</b>copied</div>
        <div><b id="cspeed">—</b>speed</div>
        <div><b id="ceta">—</b>time left</div>
        <div><b id="cfiles">—</b>files</div>
      </div>
      <div class="ccur" id="ccur"></div>
      <div style="font-size:.78rem" id="cerrline" hidden>⚠ <span id="cerrn"></span>
        file(s) skipped — <a href="#" id="cerrshow" style="color:var(--accent2)">details</a></div>
      <pre id="cerr"></pre>
      <div class="cbtns" id="cctl"></div>`);
    $("cerrshow").onclick = e => { e.preventDefault();
      $("cerr").style.display = $("cerr").style.display === "block" ? "none" : "block"; };
  }
  updateCloneRun(job);
}

function updateCloneRun(job) {
  if (!job || !$("cbar")) return;
  const running = job.state === "running";
  const pct = job.bytes_total
    ? Math.min(100, job.bytes_done / job.bytes_total * 100) : 0;
  $("chead").textContent =
    (job.kind === "backup" ? "💾 Backing up" : "📲 Restoring") +
    (running ? "…" : "");
  $("cbarw").classList.toggle("busy", running && !job.bytes_total);
  $("cbar").style.width = (job.state === "done" ? 100 : pct) + "%";

  const now = performance.now() / 1000;
  if (clonePrev && job.bytes_done > clonePrev.bytes && now > clonePrev.t)
    cloneSpeed = cloneSpeed * .6 +
      ((job.bytes_done - clonePrev.bytes) / (now - clonePrev.t)) * .4;
  clonePrev = { t: now, bytes: job.bytes_done };
  const left = cloneSpeed > 1 && job.bytes_total > job.bytes_done
    ? (job.bytes_total - job.bytes_done) / cloneSpeed : null;

  $("cbytes").textContent = human(job.bytes_done) +
    (job.bytes_total ? " / " + human(job.bytes_total) : "");
  $("cspeed").textContent = running && cloneSpeed > 1 ? human(cloneSpeed) + "/s" : "—";
  $("ceta").textContent = running ? (left ? "~" + fmtDur(left) : "…")
    : fmtDur((job.finished || job.started) - job.started);
  const fd = job.files_done ||    // restore counts by bytes, so estimate
    (job.kind === "restore" && job.bytes_total
      ? Math.round(job.files_total * job.bytes_done / job.bytes_total) : 0);
  $("cfiles").textContent = fd.toLocaleString() +
    (job.files_total ? " / " + job.files_total.toLocaleString() : "");
  $("ccur").textContent = running ? (job.current || "") : "";
  $("cphase").textContent =
    running ? (job.phase || "working") + (pct ? ` · ${pct.toFixed(1)}%` : "")
    : job.state === "done" ? "✓ Finished" + (job.note ? " — " + job.note : "")
    : job.state === "cancelled" ? "Stopped — everything copied so far was kept"
    : "✗ Failed — " + (job.error || "unknown error");

  if (job.errors_total) {
    $("cerrline").hidden = false;
    $("cerrn").textContent = job.errors_total.toLocaleString();
    $("cerr").textContent = (job.errors_tail || []).join("\n") +
      (job.errors_total > (job.errors_tail || []).length
        ? "\n… full list saved to errors.log in the backup folder" : "");
  }

  const ctl = $("cctl");
  if (running) {
    if (!$("cstop")) {
      ctl.innerHTML = `<button class="btn danger" id="cstop">■ Stop</button>
        <button class="btn" id="chide">Hide</button>`;
      $("cstop").onclick = async () => {
        if (!confirm("Stop the copy? Everything copied so far is kept.")) return;
        try { await jpost("/api/clone/cancel", {}); } catch (e) { toast(e.message, "err"); }
      };
      $("chide").onclick = cloneClose;
    }
  } else if (!$("cok")) {
    ctl.innerHTML =
      (job.kind === "backup" && job.state === "done"
        ? `<button class="btn" id="creveal">📂 Show in Finder</button>` : "") +
      `<button class="btn primary" id="cok">Close</button>`;
    if ($("creveal")) $("creveal").onclick = () =>
      jpost("/api/clone/reveal", { path: job.dest }).catch(e => toast(e.message, "err"));
    $("cok").onclick = cloneClose;
  }
}

async function pollClone() {
  let j;
  try { j = await api("/api/clone/status"); } catch (e) { return; }
  const job = j.job;
  const wasRunning = lastCloneJob && lastCloneJob.state === "running";
  lastCloneJob = job;
  const running = job && job.state === "running";
  const pctTxt = running && job.bytes_total
    ? Math.round(job.bytes_done / job.bytes_total * 100) + "%" : "…";
  $("bkbtn").textContent = running && job.kind === "backup" ? "💾 " + pctTxt : "💾 Backup";
  $("rsbtn").textContent = running && job.kind === "restore" ? "📲 " + pctTxt : "📲 Restore";
  document.title = running ? `droid-deck — ${job.kind} ${pctTxt}` : "droid-deck";
  if ($("clone").classList.contains("on")) updateCloneRun(job);
  if (wasRunning && job && !running && !$("clone").classList.contains("on"))
    toast(job.state === "done" ? `✓ ${job.kind} finished — ${job.note || ""}`
      : `${job.kind} ${job.state}: ${job.error || ""}`,
      job.state === "done" ? "ok" : "err");
}

/* ---------- send text ---------- */
async function sendText(mode) {
  const text = $("sendtext").value;
  if (!text) return toast("Nothing to send", "err");
  try {
    const j = await api(`/api/text?serial=${encodeURIComponent(state.serial)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode, enter: $("enterchk").checked }),
    });
    toast(mode === "file" ? `Saved to ${j.path} ✓` : "Typed on device ✓", "ok");
  } catch (e) { toast(e.message, "err"); }
}

/* ---------- wiring ---------- */
$("devsel").onchange = () => pickDevice();
$("refreshdev").onclick = () => loadDevices(true);
$("shot").onclick = screenshot;
$("upbtn").onclick = () => {
  const parent = state.path.replace(/\/[^/]+\/?$/, "") || "/";
  ls(parent);
};
$("filter").oninput = () => render();
$("viewbtn").onclick = () => {
  state.view = state.view === "grid" ? "list" : "grid";
  localStorage.setItem("dd.view", state.view);
  render();
};
$("sortsel").onchange = () => setSort($("sortsel").value, state.sortDir);
$("dirbtn").onclick = () => setSort(state.sortKey);
document.querySelectorAll("th.sortable").forEach(th =>
  th.onclick = () => setSort(th.dataset.key));
$("selall").onclick = () => {
  state.visible.forEach(e => state.selected.add(fullPath(e.name)));
  document.body.classList.add("selecting");
  render();
};
$("seldl").onclick = downloadSelected;
$("selrm").onclick = deleteSelected;
$("selclear").onclick = () => { clearSel(); markSel(); render(); };
$("mprev").onclick = () => pvStep(-1);
$("mnext").onclick = () => pvStep(1);
$("newdir").onclick = mkdir;
$("uploadbtn").onclick = () => $("filepick").click();
$("filepick").onchange = () => { uploadFiles([...$("filepick").files]); $("filepick").value = ""; };
$("typebtn").onclick = () => sendText("type");
$("filebtn").onclick = () => sendText("file");
$("mclose").onclick = closeModal;
$("mdl").onclick = () => currentPreview && download(currentPreview);
$("modal").onclick = e => { if (e.target === $("modal")) closeModal(); };
document.addEventListener("keydown", e => {
  if (e.key === "Escape") return closeModal();
  if (!$("modal").classList.contains("on")) return;
  if (e.key === "ArrowLeft") pvStep(-1);
  if (e.key === "ArrowRight") pvStep(1);
});

const quick = $("quick");
for (const [ic, label, p] of QUICK) {
  const b = document.createElement("button");
  b.innerHTML = `<span>${ic}</span><span>${esc(label)}</span>`;
  b.onclick = () => ls(p);
  quick.appendChild(b);
}

let dragDepth = 0;
window.addEventListener("dragenter", e => { e.preventDefault(); if (++dragDepth) $("drop").classList.add("on"); });
window.addEventListener("dragleave", e => { if (--dragDepth <= 0) { dragDepth = 0; $("drop").classList.remove("on"); } });
window.addEventListener("dragover", e => e.preventDefault());
window.addEventListener("drop", e => {
  e.preventDefault(); dragDepth = 0; $("drop").classList.remove("on");
  if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]);
});

$("reconnect").onclick = reconnect;
$("bkbtn").onclick = () =>
  lastCloneJob && lastCloneJob.state === "running"
    ? renderCloneRun(lastCloneJob, !$("cbar")) : beginBackup();
$("rsbtn").onclick = () =>
  lastCloneJob && lastCloneJob.state === "running"
    ? renderCloneRun(lastCloneJob, !$("cbar")) : beginRestore();

loadDevices();
setInterval(checkAlive, 3000);
setInterval(pollClone, 900);
pollClone();
</script>
</body>
</html>
"""


def main():
    port = PORT
    auto_open = True
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--port" and args:
            port = int(args.pop(0))
        elif a == "--no-open":
            auto_open = False
        elif a in ("-h", "--help"):
            print(__doc__)
            return

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"""
  🤖 droid-deck
  ─────────────
  serving on {url}  (local only)
  adb: {ADB}
  Ctrl-C to stop
""")
    if auto_open:
        subprocess.Popen(["open", url])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye 👋")


if __name__ == "__main__":
    main()
