<div align="center">

# OpenDownloader

**A free download manager for Windows, with browser integration and video capture.**

Faster downloads with up to 32 connections · Pause and resume anytime · Catches downloads from your
browser automatically · Saves videos from web pages, including YouTube

### [⬇ Download OpenDownloader for Windows](https://github.com/ppiku4/opendownloader/releases/latest/download/OpenDownloader-Setup.exe)

[All versions](https://github.com/ppiku4/opendownloader/releases) ·
[Report a problem](https://github.com/ppiku4/opendownloader/issues)

</div>

---

## Features

| | |
|---|---|
| **Faster downloads** | Splits each file into parts and downloads them over up to 32 connections at once. When one part finishes, the largest remaining part is split again so no connection sits idle. |
| **Pause & resume** | Pause and continue any time, even after a crash, restart or lost connection. If the file changed on the server, OpenDownloader notices instead of saving a broken file. |
| **Automatic error recovery** | Retries failed connections by itself. If a download link expires, *Refresh download address* picks up a fresh link from your browser and continues where it stopped. |
| **Browser integration** | Works with Chrome, Edge, Brave, Opera, Vivaldi and Firefox. Downloads of the file types you choose are taken over automatically, including from sites you're signed in to. Hold **Alt** while clicking a link to let the browser download it instead. |
| **Video capture** | A **"Download this video"** button appears over video players. Choose the quality (1080p, 720p, …) and get one MP4 file with picture and sound, without any quality loss. Live streams can be recorded. On pages with several players (preview grids on stock-media sites) the button follows your mouse and offers *that* video's own stream first; hidden music-preview players get a **"Download playing audio"** button. |
| **YouTube & other sites** | Works on YouTube videos and Shorts and on many other video sites. Choose a quality, or *audio only*. |
| **Progress window** | Shows speed, time left and progress. Video downloads show their stages: video, then audio, then merging. |
| **Torrents** | Open magnet links and `.torrent` files, choose which files you want, and follow the download like any other. By default a torrent stops sharing as soon as it finishes; you can change that in Options. |
| **Right-click menu** | Download a link, download a video, or **download all links** on a page or in a selection. |
| **Categories** | Files are sorted into Compressed, Documents, Music, Programs and Video, each in its own folder. You can edit the categories. |
| **Queues & scheduler** | Several download queues, each with a set number of downloads at once. Start and stop at set times on chosen days. When everything is finished: do nothing, exit, sleep, hibernate or shut down. |
| **Speed limiter** | Limit all downloads or just one, so the rest of your internet stays usable. |
| **More tools** | Site Grabber (collect all files of a type from a website), batch downloads (`img_001.jpg` to `img_100.jpg`), clipboard monitoring, checksum check (MD5/SHA-1/SHA-256/SHA-512), import and export of lists, HTTP and SOCKS proxy, FTP, password-protected downloads, tray icon, dark mode, start with Windows. |

**Not supported, by design:** DRM-protected streams (Netflix, Prime Video, Disney+, Spotify and
similar) are detected and refused. OpenDownloader never bypasses copy protection.

---

## Install

**Requirements:** Windows 10 (version 1903) or newer, 64-bit.

1. Download **[OpenDownloader-Setup.exe](https://github.com/ppiku4/opendownloader/releases/latest/download/OpenDownloader-Setup.exe)**
   and run it. No admin rights are needed.
2. If Windows shows **"Windows protected your PC"**, click **More info → Run anyway**. The installer
   isn't code-signed, so Windows doesn't recognize it yet.
3. Accept the license, choose your options and click **Install**.
4. On the last page, leave **"Show how to add the browser extension"** ticked, then click **Finish**.

The installer:

- installs to your user folder;
- adds Start menu shortcuts, plus an optional desktop icon and an optional *start with Windows*;
- connects OpenDownloader to your browsers;
- optionally opens `.torrent` files and magnet links with OpenDownloader.

To update, run the newer Setup. It closes the running copy, installs the new version and resumes your
downloads.

## Add the browser extension

The extension lets OpenDownloader catch downloads and videos from your browser. You add it once.

### Chrome, Edge, Brave, Opera, Vivaldi

1. Open the extensions page: type `chrome://extensions` in the address bar (Edge: `edge://extensions`).
2. Turn on **Developer mode** (top right in Chrome, left side in Edge).
3. Click **Load unpacked**.
4. Paste this folder path into the address bar of the folder window and click **Select Folder**:
   ```
   %LOCALAPPDATA%\Programs\OpenDownloader\extension\chromium
   ```
   You can also open that folder from the Start menu: *OpenDownloader → Browser extension*.

### Firefox

Download **opendownloader-firefox.xpi** from the
[latest release](https://github.com/ppiku4/opendownloader/releases/latest). Open it in Firefox,
or drag it onto a Firefox window, and click **Add**.

### Check that it works

The OpenDownloader icon in the browser toolbar shows a **green dot** when it's connected to the app.
If the app isn't running, the extension starts it automatically when it catches a download.

> **After updating OpenDownloader:** if the extension says *"OpenDownloader was updated"*, open the
> extensions page and click **Reload ↻** on OpenDownloader.

---

## How to use

**Download a file:** click a download link in your browser as usual. OpenDownloader takes it over and
shows where it will be saved. Click **Start**.

**Download a video:**

1. Play the video.
2. Click the **Download this video** button that appears over the player.
3. Choose the quality.

You can also click the OpenDownloader icon in the toolbar to see all videos found on the page.

**Add a link yourself:** copy a link and OpenDownloader offers to download it. You can also click
**Add URL** in the app.

**Download many files:** right-click a page and choose **Download all links with OpenDownloader**, then tick the files you
want.

**Download a torrent:** click a magnet link in your browser, open a `.torrent` file, or use
**Tasks → Open torrent file…**. Choose the files and folder, then click **Start Download**.

**Schedule downloads:** add downloads to a queue and set the start time under **Scheduler**.

---

## How it works

```
   Your browser                                   Your PC
 ┌──────────────────────┐               ┌───────────────────────────┐
 │ OpenDownloader       │  secure local │  OpenDownloader app       │
 │ extension            │ ────────────▶ │  • splits the file into   │
 │ • spots downloads    │   connection  │    parts, downloads them  │
 │ • finds videos on    │               │    in parallel            │
 │   the page           │               │  • joins the parts into   │
 │ • shows the video    │               │    the finished file      │
 │   button             │               │  • queues, scheduler      │
 └──────────────────────┘               └───────────────────────────┘
```

1. **The extension watches your browser.** When you start a download of a file type you've chosen, it
   cancels the browser's own download. It then hands the link to the app, with the cookies and page
   address the site needs.
2. **It also looks for videos.** It notices video streams while a page plays them and shows the
   *Download this video* button. For sites like YouTube, which hide the video file, it passes the page
   address to the app instead.
3. **The app downloads the file in parts.** It asks the server how big the file is, opens several
   connections and downloads different parts at the same time. It then puts the parts together
   into the finished file. Progress is saved all the time, which is why downloads can resume.
4. **Videos are merged.** Many sites send picture and sound separately. The app downloads both and
   joins them into one MP4 without re-encoding, so there's no quality loss.

**Privacy and safety:**

- The extension can only talk to the OpenDownloader app on your own PC.
- The app accepts commands only from the extension, protected by a secret key stored on your PC.
  Websites can't control it.
- Nothing is sent anywhere else.

---

## Helper tools

Two free tools aren't included in the installer. OpenDownloader offers to download them the first
time you use a feature that needs them:

| Tool | Needed for | Size |
|---|---|---|
| **ffmpeg** | Joining separate video and audio into one file | ~80 MB |
| **yt-dlp** | YouTube and other video sites | ~18 MB |

They're downloaded from their official sources and **checked against published checksums** before
use. You can update or remove them any time in **Options → Video → Helper tools**.

---

## FAQ

**The video button shows on YouTube, but the quality list is empty or the download fails.**
Go to **Options → Video → Helper tools** and click **Update** for yt-dlp. YouTube changes often, and
updates keep up with it.

**Downloads aren't being caught by OpenDownloader.**
Check that the extension icon shows a green dot. If not, start OpenDownloader and reload the extension
on the extensions page. Also check that the file type is in the list in the extension's options.
The extension's popup has a **Recent downloads** list that says why each download was or wasn't
sent to OpenDownloader. Files a page builds itself (`blob:` links) can only be saved by the browser.

**Some video sites don't work.**
Videos protected by DRM (paid streaming services) can't be downloaded, by design.

**Does OpenDownloader share (seed) torrents?**
While a torrent downloads, pieces are also shared with other people in the swarm. That's how
BitTorrent works, and everyone in the swarm can see your IP address. When the download finishes,
sharing stops right away unless you choose otherwise in **Options → Torrents**.

**Where are my settings stored?**
In `%APPDATA%\OpenDownloader`. When you uninstall, you're asked whether to delete them. Files you
downloaded are never deleted.

**How do I uninstall?**
Go to Windows **Settings → Apps**, find OpenDownloader and click **Uninstall**. Then remove the
extension from your browser's extensions page.

---

## License

**Public domain: no copyright.** OpenDownloader is free to use, copy and share for any purpose. It
comes with terms of use and a disclaimer:

- OpenDownloader is a general-purpose tool.
- **You alone are responsible for what you download** and for respecting copyright and each website's
  terms.
- The software is provided *as is*, without warranty.

See **[LICENSE](LICENSE)** for the full text. Third-party components (Qt/PySide6, Python, libtorrent,
yt-dlp, FFmpeg) keep their own licenses.
