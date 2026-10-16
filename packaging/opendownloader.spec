# PyInstaller spec: builds dist/OpenDownloader/OpenDownloader.exe - a single executable that is
# both the app (windowed) and the browser native-messaging host (auto-detected from the
# arguments the browser passes; it talks over the stdio pipes the browser provides).
# Usage:  pyinstaller packaging/opendownloader.spec --noconfirm
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "packaging", "opendownloader.ico")

# Explicitly collect the Qt plugins the app needs. PyInstaller's PySide6 hook normally does this, but
# if it can't start Qt during the build it silently skips them - and without platforms/qwindows.dll
# the app can't open a window. The runtime hook points QT_PLUGIN_PATH at PySide6/plugins.
import glob
import importlib.util

QT_PLUGINS = {
    "platforms": ["qwindows", "qoffscreen", "qminimal", "qcocoa", "qxcb"],
    "styles": ["*"],
    "imageformats": ["qico", "qjpeg", "qgif"],
}
plugin_bins = []
_spec = importlib.util.find_spec("PySide6")
if _spec and _spec.submodule_search_locations:
    _pyside = list(_spec.submodule_search_locations)[0]
    for _base in (os.path.join(_pyside, "plugins"), os.path.join(_pyside, "Qt", "plugins")):
        for _sub, _names in QT_PLUGINS.items():
            for _n in _names:
                for _f in glob.glob(os.path.join(_base, _sub, _n + ".*")) + glob.glob(os.path.join(_base, _sub, "lib" + _n + ".*")):
                    if _f.endswith((".dll", ".so", ".dylib")):
                        plugin_bins.append((_f, os.path.join("PySide6", "plugins", _sub)))
if not any("qwindows" in b[0] or "qxcb" in b[0] or "qcocoa" in b[0] for b in plugin_bins):
    raise SystemExit("ERROR: Qt platform plugin not found - is PySide6 installed correctly?")

app_a = Analysis([os.path.join(ROOT, "packaging", "app_entry.py")], pathex=[ROOT],
                 binaries=plugin_bins,
                 hiddenimports=["libtorrent"] if importlib.util.find_spec("libtorrent") else [],
                 excludes=["tkinter", "setuptools", "pkg_resources", "distutils", "unittest", "pydoc", "doctest",
                           "lib2to3", "xmlrpc", "pip", "wheel", "cryptography", "PySide6.QtNetwork",
                           "PySide6.QtWebEngineCore", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml",
                           "PySide6.QtPdf", "PySide6.QtMultimedia"])
# PyInstaller's PySide6 hook also collects every Qt plugin plus what those plugins need
# (Qt Quick, QML, PDF, SVG, virtual keyboard, software OpenGL, translations...). The app
# only uses Qt Core/Gui/Widgets, so drop the rest - roughly halves the app folder.
import re

_KEEP_PLUGINS = {("platforms", "qwindows"), ("platforms", "qoffscreen"), ("platforms", "qminimal"),
                 ("platforms", "qxcb"), ("platforms", "qcocoa"), ("imageformats", "qico"),
                 ("imageformats", "qjpeg"), ("imageformats", "qgif")}
_DROP_LIBS = re.compile(r"(^|[\\/])(lib)?(Qt6(Pdf|Quick\w*|Qml\w*|VirtualKeyboard\w*|OpenGL\w*|Network|Svg\w*|"
                        r"Multimedia\w*|WebEngine\w*|Positioning|Sensors|SerialPort)|opengl32sw|d3dcompiler_47)"
                        r"(\.dll|\.so[.0-9]*|\.dylib)?$", re.I)


def _keep(entry):
    dest = entry[0].replace("\\", "/")
    name = dest.rsplit("/", 1)[-1].lower()
    # Qt uses Windows' unversioned ICU API. PATH can contain an unrelated ICU
    # (for example Poppler's build with suffixed exports), which PyInstaller
    # otherwise bundles ahead of System32 and causes WinError 127 at startup.
    # Windows 10+ also supplies its own API-set forwarders.
    if sys.platform == "win32" and (
        name in {"icuuc.dll", "icuin.dll", "icudt.dll"}
        or name.startswith(("api-ms-win-", "ext-ms-win-"))
    ):
        return False
    if "/plugins/" in dest:
        parts = dest.split("/plugins/", 1)[1].split("/")
        if len(parts) >= 2:
            if parts[0] == "styles":
                return True
            name = re.sub(r"^lib|\.(dll|so|dylib)$", "", parts[-1])
            return (parts[0], name) in _KEEP_PLUGINS
    if "/translations/" in dest:
        return False
    return not _DROP_LIBS.search(dest)


app_a.binaries = [b for b in app_a.binaries if _keep(b)]

# libtorrent's .pyd needs the C++ runtime (MSVCP140.dll). PySide6 ships one inside its own folder;
# put a copy next to python3xx.dll too, so torrent support also works on PCs without the
# Visual C++ Redistributable installed.
if not any(b[0].replace("\\", "/").lower() == "msvcp140.dll" for b in app_a.binaries):
    for _b in app_a.binaries:
        if _b[0].replace("\\", "/").lower().endswith("/msvcp140.dll"):
            app_a.binaries.append(("MSVCP140.dll", _b[1], _b[2]))
            break
app_a.datas = [d for d in app_a.datas if _keep(d)]

app_pyz = PYZ(app_a.pure)
app_exe = EXE(app_pyz, app_a.scripts, [], exclude_binaries=True, name="OpenDownloader",
              console=False, icon=ICON)
coll = COLLECT(app_exe, app_a.binaries, app_a.datas, name="OpenDownloader")
