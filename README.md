<div align="center">

<img src="packaging/opendownloader.png" width="96" alt="OpenDownloader icon">

# OpenDownloader

**A free, open-source download manager for Windows, with browser integration and video capture.**

Faster downloads with up to 32 connections · Pause and resume · Catches downloads from your browser ·
Saves videos from web pages, including YouTube

[**⬇ Download for Windows**](https://github.com/ppiku4/opendownloader/releases/latest/download/OpenDownloader-Setup.exe)
&nbsp;·&nbsp; [All releases](https://github.com/ppiku4/opendownloader/releases)
&nbsp;·&nbsp; [Report a problem](https://github.com/ppiku4/opendownloader/issues)

</div>

---

## Install

1. Download **[OpenDownloader-Setup.exe](https://github.com/ppiku4/opendownloader/releases/latest/download/OpenDownloader-Setup.exe)** and run it.
   You don't need admin rights.
2. If Windows shows **"Windows protected your PC"**, click **More info → Run anyway**. The installer
   isn't code-signed yet, so Windows doesn't recognize it.
3. On the last page, leave **"Show how to add the browser extension"** ticked and follow the steps
   below.

Requires Windows 10 (version 1903) or newer, 64-bit.

## Add the browser extension

The extension is what lets OpenDownloader catch downloads and videos from your browser.

**Chrome, Edge, Brave, Opera, Vivaldi**

1. Open `chrome://extensions` (in Edge: `edge://extensions`).
2. Turn on **Developer mode** (top right in Chrome, left side in Edge).
3. Click **Load unpacked** and choose the folder
   `%LOCALAPPDATA%\Programs\OpenDownloader\extension\chromium`.
   Paste that path into the folder dialog's address bar, or use the Start menu shortcut
   *OpenDownloader → Browser extension*.

**Firefox**

Download **opendownloader-firefox.xpi** from the
[latest release](https://github.com/ppiku4/opendownloader/releases/latest), open it in
Firefox and click **Add**.

When the extension is connected to the app, its toolbar icon shows a **green dot**.

> **After an update:** installing a newer version replaces the extension files. If the extension
> says *"OpenDownloader was updated"*, click **Reload ↻** on the extensions page.

## Features

| | |
|---|---|
| **Faster downloads** | Up to 32 connections per file with dynamic segmentation. When one connection finishes, the largest remaining piece is split and handed to a new connection, so no connection sits idle. |
| **Pause & resume** | Continue after pauses, crashes, reboots or network drops. A file that changed on the server is detected instead of being silently corrupted. |
| **Browser takeover** | Downloads of the file types you choose are sent to OpenDownloader automatically, with cookies and referrer, so downloads from sites you're signed in to still work. Hold **Alt** while clicking to let the browser handle a link itself. |
| **Video capture** | A **"Download this video"** button appears over video players. HLS (`.m3u8`), DASH (`.mpd`) and direct video files are detected. You pick the quality (1080p, 720p, …), and separate audio and video are merged into one MP4 without re-encoding. Live streams can be recorded. On pages with several players (preview grids on stock-media sites) the button follows your mouse and offers *that* video's own stream first; hidden music-preview players get a **"Download playing audio"** button. |
| **YouTube & site players** | On YouTube and other sites whose player hides the video file, the button passes the page to [yt-dlp](https://github.com/yt-dlp/yt-dlp). You then choose a quality or *audio only*. |
| **Progress window** | Shows speed, time left and progress for every download. Video downloads show their stages: video, then audio, then merging. |
| **Expired links** | *Refresh download address*: pick up a fresh link from the browser and continue where the download stopped. |
| **Queues & scheduler** | Several queues, with a set number of downloads running at once. Start and stop at set times on chosen days. Retry failed files. When finished: do nothing, exit, sleep, hibernate or shut down. |
| **Speed limiter** | A global limit and per-download limits. |
| **Updates** | *Help → Check for updates* compares with the latest GitHub release. Installed copies download the new installer, verify its SHA-256 and update in place; downloads in progress resume afterwards. |
| **Categories** | Compressed, Documents, Music, Programs, Video, each saved to its own folder (all editable). |
| **Torrents** | Magnet links and `.torrent` files (built on libtorrent): choose the files you want, pause/resume, per-download speed limit. Seeding stops when the download finishes unless you choose a ratio or time in *Options → Torrents*. The extension opens magnet links in the app; Setup can make OpenDownloader the handler for `.torrent` files and magnet links. |
| **More tools** | Download all links on a page, Site Grabber, batch downloads with wildcards (`img_*.jpg` 001–100), clipboard monitoring, checksum check (MD5/SHA-1/SHA-256/SHA-512), import/export lists, HTTP and SOCKS proxy, FTP, tray icon, dark mode, start with Windows, command line. |

**Not supported, by design:** DRM-protected streams (Netflix, Prime Video, Disney+, Spotify and
similar) are detected and refused. OpenDownloader never circumvents copy protection.

### Helper tools (downloaded only when needed)

Two optional open-source tools aren't included in the installer. The app offers to download them
the first time a feature needs them:

| Tool | Needed for | Size |
|---|---|---|
| **ffmpeg** | Merging separate video and audio into one file (DASH, some HLS, YouTube) | ~80 MB |
| **yt-dlp** | YouTube and other site-player downloads | ~18 MB |

Both come from their official GitHub releases, are **checked against published SHA-256 checksums**,
and are stored in `%APPDATA%\OpenDownloader\tools`. You can update or remove them in
*Options → Video → Helper tools*.

## FAQ

**The video button shows on YouTube, but the quality list is empty or the download fails.**
Open *Options → Video → Helper tools* and click **Update** for yt-dlp. YouTube changes often, and
new yt-dlp versions keep up with it.

**Downloads aren't being caught.**
Check that the extension's icon shows a green dot. If it doesn't, start OpenDownloader once and
reload the extension. Also check that the file type is in the extension's options list.
The extension's popup has a **Recent downloads** list that says why each download was or wasn't
sent to OpenDownloader. Files a page builds itself (`blob:` links) can only be saved by the browser.

**Where are my settings?**
In `%APPDATA%\OpenDownloader`. Uninstalling asks whether to delete them. Files you downloaded are
never deleted.

**Can I use it without installing?**
Yes. Download the portable zip from a release, unzip it to a permanent folder and run
`OpenDownloader.exe`. Keep the `_internal` folder next to the exe.

---

## For developers

Written from scratch in **Python + Qt (PySide6)**. The browser extension is Manifest V3 for
Chromium browsers and Firefox. No code from any proprietary download manager is used.

### Run from source

1. Install **Python 3.10+** from python.org (tick *Add python.exe to PATH*).
2. Double-click **`install.bat`**. It installs the dependencies, builds the extension into
   `extension\dist\` and registers the browser integration.
3. Start the app with **`run.bat`** and load `extension\dist\chromium` as an unpacked extension.

The app also runs on Linux and macOS from source.

### Publish a release

Releases are built by GitHub Actions ([`.github/workflows/release.yml`](.github/workflows/release.yml)), never
from a local machine, and no executables are committed to the repository.

1. Set the new version in `opendownloader/__init__.py` and `pyproject.toml`, and commit.
2. Tag it and push the tag: `git tag -a v1.2.1 -m "OpenDownloader 1.2.1"` then `git push origin v1.2.1`.

The workflow checks that the tag matches the version, runs the tests, builds the exe, installer, portable zip
and extensions, and publishes the release with `SHA256SUMS.txt`. Installed copies then find it through
*Help → Check for updates*. To build without releasing, run the workflow by hand from the Actions tab.

### Build the installer locally

Double-click **`packaging\build_windows.bat`**.

**Torrent support needs Python 3.13** (libtorrent has no package for Python 3.14 yet). The script looks for
Python 3.13 in its standard folder `%LOCALAPPDATA%\Programs\Python\Python313` (or via the `py` launcher). If it
only finds a newer Python, it explains where to download 3.13 and offers to install it for you: it downloads the
official installer from python.org, checks its signature and installs it for your user only, next to your
existing Python. Without 3.13 the build still works, just without torrents.

The script builds the extension, builds `dist\OpenDownloader\OpenDownloader.exe` with PyInstaller, and then
builds **`dist\OpenDownloader-Setup.exe`** with [Inno Setup 6](https://jrsoftware.org/isinfo.php). If
Inno Setup is missing, the script downloads the official release, checks it and installs it for
you. The full log is in `build_log.txt`.

### How it fits together

```
 Browser                                 Your PC
┌──────────────────────────┐   native   ┌───────────────────────┐  local IPC  ┌──────────────────────────┐
│ Extension (MV3)          │ messaging  │ OpenDownloader.exe    │ (127.0.0.1  │ OpenDownloader app       │
│  • download takeover     │──stdio────▶│ (bridge mode; starts  │  + secret   │  • download manager      │
│  • media sniffing        │            │  the app if needed)   │──token)────▶│  • HTTP/FTP/HLS/DASH     │
│  • in-page video button  │            └───────────────────────┘             │  • queues, scheduler     │
│  • context menus / popup │                                                  │  • Qt UI, tray           │
└──────────────────────────┘                                                  └──────────────────────────┘
```

* The browser can only reach the app through the registered native-messaging host, and only from
  the OpenDownloader extension IDs. The local IPC port needs a random secret stored in your profile,
  so web pages can't send the app commands.
* The Chromium extension ID (`hkhijccfgjfdcidbdfgfbmghhjfnnldj`) is fixed by the public key in
  `extension/manifest.chromium.json`. A store build gets a different ID. Add that ID under
  *Options → Browser → Extra extension IDs* and click *Register*.

### Project layout

```
opendownloader/
  core/          download engines (HTTP, FTP, HLS, DASH, yt-dlp), manager, queues, SQLite store
  ui/            PySide6 main window, dialogs, tray
  native_host/   browser native-messaging bridge
  integration.py browser registration, autostart
extension/       browser extension source (src/) and build script
packaging/       PyInstaller spec, Windows build script, Inno Setup installer
tests/           unit tests and browser end-to-end tests
```

### Command line

```
OpenDownloader.exe [URL] [-o FILENAME] [-d FOLDER] [-r REFERER] [--silent] [--queue] [--minimized]
OpenDownloader.exe --register-host | --unregister-host
```

### Tests

```
pip install -r requirements-dev.txt
pytest
python tests/e2e/run_browser_e2e.py      # real Chromium + extension + app
python tests/e2e/run_page_video_e2e.py   # page-video button -> app
python tests/e2e/run_spa_grid_e2e.py     # preview grid, hidden audio, in-page navigation, activity list
```

### Contributing

Bug reports and pull requests are welcome. Please open an issue first for larger changes.

---

## License

**Public domain: no copyright.** Anyone may use, copy, modify and distribute this software for any
purpose. It comes with terms of use and a disclaimer:

- OpenDownloader is a general-purpose tool.
- **You alone are responsible for what you download** and for respecting copyright and each website's
  terms.
- The software is provided *as is*, without warranty.

See **[LICENSE](LICENSE)** for the full text. Third-party components (Qt/PySide6, Python, yt-dlp,
FFmpeg) keep their own licenses.
