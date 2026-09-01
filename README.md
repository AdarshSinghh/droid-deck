# 🤖 droid-deck

A tiny, dependency-free web GUI for browsing and transferring files on
ADB-connected Android devices — from your Mac's browser.

One Python file. No pip installs. No Electron. Just `adb` + the standard library.

```
┌──────────────┬────────────────────────────────────────────┐
│  Device      │  ← /sdcard › Download          [Filter…]   │
│  Mi 11 ●     │  ──────────────────────────────────────    │
│              │  📁 DCIM                                    │
│  Quick locs  │  📁 Documents                               │
│  🏠 Internal │  🖼 IMG_2026.jpg      2.1 MB   2026-08-01  │
│  ⬇️ Download │  📝 notes.txt          1 KB    2026-08-12  │
│  📷 DCIM     │  🎬 clip.mp4          48 MB    2026-08-20  │
│              │                                            │
│  Send text ⌨️ │        (drag & drop files to upload)       │
└──────────────┴────────────────────────────────────────────┘
```

## Features

**Android → Mac**
- Browse device storage with quick links (Download, DCIM, WhatsApp media, …)
- Preview images, video, audio, and text files in the browser
- **Copy** text-file contents or images straight to the Mac clipboard
- Download any file
- Live screenshots of the phone screen — copy or download

**Mac → Android**
- Drag & drop files anywhere on the page → pushed to the current folder
- Send text to the device: *type* it into the focused field on the phone
  (handy for passwords/URLs), or *save* it as a `.txt` in `/sdcard/Download`
  (handles unicode & multi-line)

**Browsing**
- List ⇄ grid view toggle; grid shows photo **thumbnails** (downscaled once via
  macOS `sips`, cached in `~/.cache/droid-deck` — instant on revisit)
- Click column headers to **sort** by name / size / modified (asc/desc)
- Preview has **‹ ›** navigation (and ← → keys) to flip through a folder's files
- **Multi-select** with checkboxes → download or delete a batch
- View & sort preferences remembered across sessions

**Clone storage (backup / restore)**
- **💾 Backup**: copy the phone's *entire* shared storage (photos, videos,
  downloads, WhatsApp media, documents — everything under `/sdcard`) into a
  dated folder on the Mac, picked with the native folder dialog
- **📲 Restore**: push a saved backup onto a (new) phone — perfect for
  phone-to-phone migration; apps are easy to reinstall, storage is not
- Live progress the whole way: bytes copied / total, transfer speed, ETA,
  file counter, and the file currently being copied — plus a running badge on
  the sidebar button and in the tab title if you hide the dialog
- Streams a single device-side `tar` over ADB (no per-file overhead, ~30 MB/s
  on USB), preserving folder structure and file timestamps
- Locked files (e.g. under `Android/data`, skipped by default) are logged to
  `errors.log` and skipped instead of aborting; every backup gets a
  `manifest.json` (source device, date, counts) that the restore screen shows
  before pushing anything
- Stop button any time — everything copied so far is kept

**Housekeeping**
- Rename, delete, new folder, free-space display
- Protected-path guard (won't let you `rm -rf /sdcard`)

## Prerequisites

| What | Why | Install (macOS) |
|---|---|---|
| Python 3.8+ | runs the server (stdlib only) | ships with macOS, or `brew install python` |
| adb (platform-tools) | talks to the device | `brew install --cask android-platform-tools` |
| USB debugging on the phone | lets adb in | see below |

**Enable USB debugging on the phone:**
1. Settings → About phone → tap **Build number** 7 times (unlocks Developer options)
2. Settings → Additional settings → Developer options → enable **USB debugging**
3. Plug in via USB, tap **Allow** on the "Allow USB debugging?" prompt
   (tick *Always allow from this computer*)
4. On Xiaomi/MIUI also enable **USB debugging (Security settings)** if you want
   the "Type text" feature to simulate input

Verify: `adb devices` should list your device as `device` (not `unauthorized`).

## Install

```sh
git clone <this-repo> && cd droid-deck
./install.sh
```

`install.sh` checks for Python 3 and adb (offers the brew command if missing)
and drops a `droid-deck` launcher into `~/.local/bin`.

Or skip the install and run it directly:

```sh
python3 droid-deck.py
```

## Usage

```sh
droid-deck                # serve http://127.0.0.1:8642 and open the browser
droid-deck --port 9000    # different port
droid-deck --no-open      # don't auto-open the browser
```

The server binds to `127.0.0.1` only — nothing is exposed to your network.

## Troubleshooting

- **"no device"** — check the USB cable (some are charge-only), and that the
  phone shows `device` in `adb devices`. `unauthorized` means the debugging
  prompt on the phone is waiting for you.
- **"Type" does nothing** — a text field must be focused on the phone, and
  `input text` is ASCII-only. Use **As file** for anything else. On MIUI,
  "USB debugging (Security settings)" must be on.
- **Video preview won't play** — the browser can only play formats it supports
  (mp4/webm mostly; mkv usually won't). Download instead.
- **Port already in use** — another droid-deck is running; reuse that tab or
  pass `--port`.

## How it works

`droid-deck.py` is an `http.server` that shells out to `adb`:
listings via `ls -lAH`, downloads streamed through `adb exec-out cat`,
uploads via `adb push`, screenshots via `screencap -p`, typed text via
`input text`. Thumbnails are made by pulling the image once, downscaling
it with macOS's built-in `sips`, and caching the result in
`~/.cache/droid-deck/thumbs` keyed by path+mtime+size (so an edited photo
gets a fresh thumb, an unchanged one never transfers twice).
The GUI is a single embedded HTML page — no build step.
