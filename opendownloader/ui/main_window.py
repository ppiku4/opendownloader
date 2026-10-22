"""Main window: categories tree, downloads table, toolbar, menus."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QAbstractTableModel, QByteArray, QFileInfo, QModelIndex, QSize, QSortFilterProxyModel, Qt
from PySide6.QtGui import QAction, QCloseEvent, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFileIconProvider, QHeaderView, QLabel, QMainWindow, QMenu, QMessageBox,
    QSplitter, QTableView, QToolBar, QTreeWidget, QTreeWidgetItem,
)

from .. import APP_NAME, __version__
from ..core.manager import open_path, reveal_in_folder
from ..core.models import DownloadItem, Status
from ..util import fmt_time, human_eta, human_size, human_speed
from . import icons
from .update_ui import check_for_updates
from .widgets import ProgressDelegate

if TYPE_CHECKING:
    from .app import Controller

COLS = ["File Name", "Size", "Status", "Time left", "Transfer rate", "Last try", "Description", "Save to"]
SORT_ROLE = Qt.UserRole + 1


@lru_cache(maxsize=256)
def _file_icon(ext: str) -> QIcon:
    return QFileIconProvider().icon(QFileInfo(f"dummy.{ext}")) if ext else QFileIconProvider().icon(QFileIconProvider.File)


def status_text(it: DownloadItem) -> str:
    if it.status == Status.COMPLETE:
        return it.stage if it.stage.startswith("Seeding") else "Complete"
    if it.status == Status.ERROR:
        return "Error"
    if it.awaiting_refresh:
        return "Waiting for new address"
    pct = f"{it.progress * 100:.1f}%" if it.size > 0 else ""
    if it.status == Status.DOWNLOADING:
        return pct or human_size(it.downloaded)
    if it.status == Status.PROCESSING:
        return "Merging…"
    if it.status == Status.CONNECTING:
        return "Connecting…"
    if it.status == Status.PAUSED:
        return f"Paused {pct}".strip()
    return f"Queued {pct}".strip()


class DownloadsModel(QAbstractTableModel):
    def __init__(self, ctl: "Controller"):
        super().__init__()
        self.ctl = ctl
        self.ids: list[int] = []
        self.filter: tuple[str, Any] = ("all", None)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.ids)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLS[section]
        return None

    def item(self, row: int) -> DownloadItem | None:
        if 0 <= row < len(self.ids):
            return self.ctl.manager.items.get(self.ids[row])
        return None

    def _match(self, it: DownloadItem) -> bool:
        kind, val = self.filter
        if kind == "all":
            return True
        if kind == "cat":
            return it.category == val
        if kind == "unfinished":
            return it.status != Status.COMPLETE
        if kind == "finished":
            return it.status == Status.COMPLETE
        if kind == "queue":
            return it.queue_id == val
        return True

    def refresh(self) -> bool:
        m = self.ctl.manager
        if self.filter[0] == "queue" and self.filter[1] in m.queues:
            new = [i for i in m.queues[self.filter[1]].order if i in m.items]
        else:
            new = [i for i, it in list(m.items.items()) if self._match(it)]
        if new != self.ids:
            self.beginResetModel()
            self.ids = new
            self.endResetModel()
            return True
        if self.ids:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.ids) - 1, len(COLS) - 1))
        return False

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        it = self.item(index.row())
        if it is None:
            return None
        c = index.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return it.filename
            if c == 1:
                return human_size(it.size) if it.size > 0 else ""
            if c == 2:
                return status_text(it)
            if c == 3:
                return human_eta(it.eta) if it.status.active else ""
            if c == 4:
                return human_speed(it.speed) if it.status.active else ""
            if c == 5:
                return fmt_time(it.last_try or it.added_at)
            if c == 6:
                return it.error if it.status == Status.ERROR else it.description
            if c == 7:
                return it.save_dir
        elif role == SORT_ROLE:
            return (it.filename.lower(), it.size, it.progress, it.eta or 0, it.speed,
                    it.last_try or it.added_at, it.description, it.save_dir)[c]
        elif role == Qt.DecorationRole and c == 0:
            if it.kind in ("hls", "dash", "ytdlp"):
                return icons.icon("video")
            return _file_icon(Path(it.filename).suffix.lstrip(".").lower())
        elif role == Qt.ToolTipRole:
            tip = f"{it.filename}\n{it.url}"
            if it.error:
                tip += f"\n\nError: {it.error}"
            return tip
        elif role == Qt.UserRole:
            return it.id
        return None


class MainWindow(QMainWindow):
    def __init__(self, ctl: "Controller"):
        super().__init__()
        self.ctl = ctl
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.icon("app"))
        self.resize(1100, 620)

        self.model = DownloadsModel(ctl)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(5, Qt.DescendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setIconSize(QSize(16, 16))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        for i, w in enumerate((300, 90, 130, 80, 100, 120, 200)):
            self.table.setColumnWidth(i, w)
        self.table.setItemDelegateForColumn(
            2, ProgressDelegate(lambda r: self.model.item(self.proxy.mapToSource(self.proxy.index(r, 0)).row()), self))
        self.table.doubleClicked.connect(self._double_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(170)
        self.tree.itemSelectionChanged.connect(self._tree_changed)
        self.refresh_tree()

        split = QSplitter()
        split.addWidget(self.tree)
        split.addWidget(self.table)
        split.setStretchFactor(1, 1)
        split.setSizes([200, 900])
        self.setCentralWidget(split)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self.speed_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.speed_lbl)
        geo = ctl.settings.get("window_geometry")
        if geo:
            self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
        self._update_actions()

    # ---------------------------------------------------------------- actions
    def _act(self, text: str, icon: str | None, slot, shortcut: str | None = None, tip: str = "") -> QAction:
        a = QAction(icons.icon(icon) if icon else QIcon(), text, self)
        a.triggered.connect(slot)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
        return a

    def _build_actions(self) -> None:
        c = self.ctl
        self.a_add = self._act("Add URL", "add", c.add_url_dialog, "Ctrl+N", "Add a new download")
        self.a_resume = self._act("Resume", "resume", lambda: self._each(c.manager.start), "Ctrl+R")
        self.a_stop = self._act("Stop", "pause", lambda: self._each(c.manager.pause), "Ctrl+P")
        self.a_stopall = self._act("Stop All", "stopall", c.manager.pause_all)
        self.a_delete = self._act("Delete", "delete", self._delete, "Del")
        self.a_delcomplete = self._act("Delete Completed", "clean", self._delete_completed)
        self.a_options = self._act("Options", "options", c.options_dialog)
        self.a_sched = self._act("Scheduler", "scheduler", c.scheduler_dialog)
        self.a_startq = self._act("Start Queue", "startq", lambda: c.manager.start_queue(self._current_queue()))
        self.a_stopq = self._act("Stop Queue", "stopq", lambda: c.manager.stop_queue(self._current_queue()))
        self.a_grab = self._act("Grabber", "grabber", c.grabber_dialog)
        self.a_batch = self._act("Add batch download…", "batch", c.batch_dialog)
        self.a_page = self._act("Download video from web page…", "video", c.page_video_dialog)
        self.a_torrent = self._act("Open torrent file…", None, c.add_torrent_file_dialog, "Ctrl+T")
        self.a_stopseed = self._act("Stop seeding", None, lambda: c.stop_seeding(self.selected_ids()))
        self.a_tpause = self._act("Pause torrent", "pause", lambda: self._torrent("pause"),
                                  tip="Pause in place: peers are disconnected, Resume continues instantly")
        self.a_tstop = self._act("Stop torrent", "stopall", lambda: self._torrent("stop"),
                                 tip="Stop completely: the torrent is unloaded and its progress saved")
        self.a_trecheck = self._act("Force recheck", None, lambda: self._torrent("recheck"),
                                    tip="Verify the downloaded data against the torrent's hashes")
        self.a_treannounce = self._act("Force reannounce", None, lambda: self._torrent("reannounce"),
                                       tip="Ask the trackers and DHT for peers now")
        self.a_refresh = self._act("Refresh download address…", None, self._refresh_addr)
        self.a_restart = self._act("Restart download", None, lambda: self._each(c.manager.restart))
        self.a_props = self._act("Properties", None, self._properties, "Alt+Return")
        self.a_open = self._act("Open", None, self._open)
        self.a_folder = self._act("Open folder", None, self._open_folder)
        self.a_copy = self._act("Copy address", None, self._copy_url, "Ctrl+Shift+C")
        self.a_limiter = QAction("Speed limiter", self, checkable=True)
        self.a_limiter.setChecked(bool(c.settings.get("speed_limit_enabled")))
        self.a_limiter.toggled.connect(c.toggle_limiter)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&Tasks")
        m.addAction(self.a_add)
        m.addAction(self.a_batch)
        m.addAction(self.a_page)
        m.addAction(self.a_torrent)
        m.addSeparator()
        m.addAction("Import download list…", self._import)
        m.addAction("Export download list…", self._export)
        m.addSeparator()
        m.addAction("Exit", self.ctl.quit, QKeySequence("Ctrl+Q"))
        m = mb.addMenu("&Downloads")
        for a in (self.a_resume, self.a_stop, self.a_stopall, None, self.a_delete, self.a_delcomplete, None,
                  self.a_restart, self.a_refresh, self.a_props):
            m.addSeparator() if a is None else m.addAction(a)
        self.torrent_menu = self._torrent_menu(m)
        m = mb.addMenu("&Queues")
        for a in (self.a_startq, self.a_stopq, None, self.a_sched):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Tools")
        m.addAction(self.a_options)
        m.addAction(self.a_grab)
        m.addAction(self.a_limiter)
        m = mb.addMenu("&Help")
        m.addAction("Browser integration…", lambda: self.ctl.options_dialog(tab="Browser"))
        m.addAction("Check for updates…", lambda: check_for_updates(self.ctl, self))
        m.addAction("About", self._about)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("main-toolbar")
        tb.setIconSize(QSize(32, 32))
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setMovable(False)
        for a in (self.a_add, self.a_resume, self.a_stop, self.a_stopall, self.a_delete, self.a_delcomplete, None,
                  self.a_options, self.a_sched, self.a_startq, self.a_stopq, None, self.a_grab):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.addToolBar(tb)

    # ---------------------------------------------------------------- tree
    def refresh_tree(self) -> None:
        m = self.ctl.manager
        cur = self.model.filter
        self.tree.blockSignals(True)
        self.tree.clear()
        root = QTreeWidgetItem(["All Downloads"])
        root.setIcon(0, icons.icon("folder"))
        root.setData(0, Qt.UserRole, ("all", None))
        for cat in ["General", *self.ctl.settings.get("categories").keys()]:
            ch = QTreeWidgetItem([cat])
            ch.setIcon(0, icons.icon("folder"))
            ch.setData(0, Qt.UserRole, ("cat", cat))
            root.addChild(ch)
        unf = QTreeWidgetItem(["Unfinished"])
        unf.setIcon(0, icons.icon("folder-unfinished"))
        unf.setData(0, Qt.UserRole, ("unfinished", None))
        fin = QTreeWidgetItem(["Finished"])
        fin.setIcon(0, icons.icon("folder-done"))
        fin.setData(0, Qt.UserRole, ("finished", None))
        qs = QTreeWidgetItem(["Queues"])
        qs.setIcon(0, icons.icon("folder-queue"))
        qs.setData(0, Qt.UserRole, ("all", None))
        for q in m.queues.values():
            ch = QTreeWidgetItem([q.name])
            ch.setIcon(0, icons.icon("startq" if q.running else "folder-queue"))
            ch.setData(0, Qt.UserRole, ("queue", q.id))
            qs.addChild(ch)
        self.tree.addTopLevelItems([root, unf, fin, qs])
        self.tree.expandAll()
        # restore selection
        sel = root
        it = self.tree.invisibleRootItem()
        stack = [it.child(i) for i in range(it.childCount())]
        while stack:
            node = stack.pop()
            if node.data(0, Qt.UserRole) == cur and node is not qs:
                sel = node
            stack.extend(node.child(i) for i in range(node.childCount()))
        self.tree.setCurrentItem(sel)
        self.tree.blockSignals(False)

    def _tree_changed(self) -> None:
        it = self.tree.currentItem()
        if it is None:
            return
        self.model.filter = it.data(0, Qt.UserRole) or ("all", None)
        self.model.refresh()
        self._update_actions()

    def _current_queue(self) -> int:
        kind, val = self.model.filter
        if kind == "queue":
            return val
        return self.ctl.manager.main_queue.id  # type: ignore[return-value]

    # ---------------------------------------------------------------- selection
    def selected_ids(self) -> list[int]:
        rows = {self.proxy.mapToSource(i).row() for i in self.table.selectionModel().selectedRows()}
        return [self.model.ids[r] for r in sorted(rows) if r < len(self.model.ids)]

    def _each(self, fn) -> None:
        for iid in self.selected_ids():
            fn(iid)

    def _update_actions(self) -> None:
        ids = self.selected_ids()
        items = [self.ctl.manager.items[i] for i in ids if i in self.ctl.manager.items]
        self.a_resume.setEnabled(any(not i.status.active and i.status != Status.COMPLETE for i in items))
        self.a_stop.setEnabled(any(i.status.active for i in items))
        self.a_delete.setEnabled(bool(items))
        one = len(items) == 1
        self.a_props.setEnabled(one)
        self.a_refresh.setEnabled(one and items[0].status != Status.COMPLETE and items[0].kind == "http")
        self.a_open.setEnabled(one and items[0].status == Status.COMPLETE)
        self.a_stopseed.setVisible(any(i.status == Status.COMPLETE and i.stage.startswith("Seeding") for i in items))
        m = self.ctl.manager
        states = [(i, m.torrent_state(i.id)) for i in items if i.kind == "torrent"]
        self.torrent_menu.menuAction().setVisible(bool(states))
        self.a_tpause.setEnabled(any(s == "running" for _, s in states))
        self.a_tstop.setEnabled(any(s in ("running", "paused") for _, s in states))
        self.a_trecheck.setEnabled(any(s in ("running", "paused") or (s == "stopped" and i.status != Status.COMPLETE)
                                       for i, s in states))
        self.a_treannounce.setEnabled(any(s in ("running", "seeding") for _, s in states))

    def _torrent_menu(self, parent: QMenu) -> QMenu:
        tm = parent.addMenu("Torrent")
        for a in (self.a_tpause, self.a_tstop, None, self.a_trecheck, self.a_treannounce):
            tm.addSeparator() if a is None else tm.addAction(a)
        return tm

    def _torrent(self, action: str) -> None:
        for iid in self.selected_ids():
            self.ctl.manager.torrent_action(iid, action)
        self._update_actions()

    def refresh(self) -> None:
        sel = set(self.selected_ids())
        if self.model.refresh() and sel:
            sm = self.table.selectionModel()
            for r, iid in enumerate(self.model.ids):
                if iid in sel:
                    idx = self.proxy.mapFromSource(self.model.index(r, 0))
                    sm.select(idx, sm.Select | sm.Rows)
        self._update_actions()
        m = self.ctl.manager
        active = len(m.workers)
        spd = m.total_speed()
        self.speed_lbl.setText(f"Active: {active}   {human_speed(spd) or '0 B/s'}"
                               + ("   [limiter on]" if self.ctl.settings.get("speed_limit_enabled") else ""))

    # ---------------------------------------------------------------- handlers
    def _double_click(self, index: QModelIndex) -> None:
        row = self.proxy.mapToSource(index).row()
        it = self.model.item(row)
        if it is None:
            return
        if it.status == Status.COMPLETE:
            if it.path.exists():
                open_path(it.path)
            else:
                QMessageBox.warning(self, APP_NAME, f"File not found:\n{it.path}")
        else:
            self.ctl.show_progress(it.id)

    def _context_menu(self, pos) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        m = QMenu(self)
        m.addAction(self.a_open)
        m.addAction(self.a_folder)
        m.addSeparator()
        m.addAction(self.a_resume)
        m.addAction(self.a_stop)
        m.addAction(self.a_restart)
        m.addAction(self.a_refresh)
        m.addAction(self.a_stopseed)
        self._torrent_menu(m).menuAction().setVisible(self.torrent_menu.menuAction().isVisible())
        m.addSeparator()
        qm = m.addMenu("Move to queue")
        for q in self.ctl.manager.queues.values():
            qm.addAction(q.name, lambda qid=q.id: self.ctl.manager.add_to_queue(ids, qid))
        m.addAction("Remove from queue", lambda: self.ctl.manager.remove_from_queue(ids))
        m.addAction("Move up in queue", lambda: [self.ctl.manager.move_in_queue(i, -1) for i in ids])
        m.addAction("Move down in queue", lambda: [self.ctl.manager.move_in_queue(i, 1) for i in ids])
        m.addSeparator()
        m.addAction(self.a_copy)
        m.addAction(self.a_delete)
        m.addAction(self.a_props)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _open(self) -> None:
        for iid in self.selected_ids():
            it = self.ctl.manager.items[iid]
            if it.status == Status.COMPLETE:
                open_path(it.path)

    def _open_folder(self) -> None:
        ids = self.selected_ids()
        if ids:
            it = self.ctl.manager.items[ids[0]]
            reveal_in_folder(it.path if it.status == Status.COMPLETE else it.part_path)

    def _copy_url(self) -> None:
        QGuiApplication.clipboard().setText("\n".join(self.ctl.manager.items[i].url for i in self.selected_ids()))

    def _delete(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Delete")
        box.setText(f"Remove {len(ids)} download(s) from the list?")
        box.setIcon(QMessageBox.Question)
        also = box.addButton("Remove and delete files", QMessageBox.DestructiveRole)
        box.addButton("Remove from list", QMessageBox.AcceptRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is cancel:
            return
        self.ctl.manager.remove(ids, delete_files=box.clickedButton() is also)

    def _delete_completed(self) -> None:
        ids = [i for i, it in self.ctl.manager.items.items() if it.status == Status.COMPLETE]
        if ids and QMessageBox.question(self, "Delete completed",
                                        f"Remove {len(ids)} completed download(s) from the list?\n"
                                        "(Files stay on disk.)") == QMessageBox.Yes:
            self.ctl.manager.remove(ids)

    def _refresh_addr(self) -> None:
        ids = self.selected_ids()
        if ids:
            from .dialogs_download import ask_refresh_address

            ask_refresh_address(self.ctl, ids[0], self)

    def _properties(self) -> None:
        ids = self.selected_ids()
        if ids:
            from .dialogs_download import PropertiesDialog

            PropertiesDialog(self.ctl, ids[0], self).exec()

    def _import(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Import", "", "Download list (*.json);;Text file with URLs (*.txt)")
        if not f:
            return
        if f.endswith(".txt"):
            from .dialogs_download import LinksDialog

            urls = [ln.strip() for ln in Path(f).read_text("utf-8", "replace").splitlines() if ln.strip()]
            LinksDialog(self.ctl, urls, parent=self, title="Import URLs").exec()
        else:
            n = self.ctl.manager.import_list(Path(f))
            self.statusBar().showMessage(f"Imported {n} downloads", 5000)

    def _export(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "Export", "downloads.json", "Download list (*.json)")
        if f:
            self.ctl.manager.export_list(Path(f))

    def _about(self) -> None:
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<h3>{APP_NAME} {__version__}</h3>"
                          "<p>Open-source multi-connection download manager with browser integration "
                          "and HLS/DASH video capture.</p><p>Public domain software (no copyright). Provided as is, without warranty. You are solely responsible for what you download and for respecting copyright and each site's terms.</p>")

    def closeEvent(self, e: QCloseEvent) -> None:  # noqa: N802
        self.ctl.settings["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.ctl.settings.save()
        if self.ctl.settings.get("minimize_to_tray") and self.ctl.tray is not None and not self.ctl.quitting:
            e.ignore()
            self.hide()
            self.ctl.notify(APP_NAME, "Still running in the tray. Right-click the tray icon to exit.", once=True)
            return
        e.accept()
        self.ctl.quit()
