"""'Add torrent' dialog: choose the save folder and which files of a torrent to download."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import torrent_engine as te
from ..core.models import Kind
from ..util import human_size, sanitize_filename
from . import icons
from .threads import run_async

if TYPE_CHECKING:
    from .app import Controller

INDEX_ROLE = Qt.UserRole + 1
SIZE_ROLE = Qt.UserRole + 2


class TorrentDialog(QDialog):
    """source = {"magnet": url} or {"data": .torrent bytes, "url": where it came from}."""

    def __init__(self, ctl: "Controller", source: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.ctl = ctl
        self.source = source
        self.meta: te.TorrentMeta | None = None
        self.cancel = threading.Event()
        self._folder_edited = False
        self.setWindowTitle("Add torrent")
        self.setWindowIcon(icons.icon("app"))
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.name = QLabel()
        self.name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.name.setWordWrap(True)
        f = self.name.font()
        f.setBold(True)
        self.name.setFont(f)
        self.info = QLabel()
        self.folder = QLineEdit()
        self.folder.textEdited.connect(lambda _t: setattr(self, "_folder_edited", True))
        browse = QToolButton()
        browse.setText("…")
        browse.clicked.connect(self._browse)
        frow = QHBoxLayout()
        frow.addWidget(self.folder, 1)
        frow.addWidget(browse)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.itemChanged.connect(self._update_selected)
        self.sel_lbl = QLabel()
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._check_all(True))
        none_btn = QPushButton("Select none")
        none_btn.clicked.connect(lambda: self._check_all(False))
        srow = QHBoxLayout()
        srow.addWidget(all_btn)
        srow.addWidget(none_btn)
        srow.addStretch(1)
        srow.addWidget(self.sel_lbl)

        form = QFormLayout()
        form.addRow("Torrent:", self.name)
        form.addRow("", self.info)
        form.addRow("Save to:", frow)

        self.later = QPushButton("Download Later")
        menu = QMenu(self.later)
        for q in ctl.manager.queues.values():
            menu.addAction(f"Add to '{q.name}'", lambda qid=q.id: self._go(start=False, queue_id=qid))
        menu.addAction("Just add to list", lambda: self._go(start=False))
        self.later.setMenu(menu)
        self.start_btn = QPushButton("Start Download")
        self.start_btn.setDefault(True)
        self.start_btn.setIcon(icons.icon("resume"))
        self.start_btn.clicked.connect(lambda: self._go(start=True))
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns = QHBoxLayout()
        btns.addStretch(1)
        for b in (self.later, self.start_btn, cancel):
            btns.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.tree, 1)
        lay.addLayout(srow)
        lay.addLayout(btns)
        self.resize(640, 460)

        if "data" in source:
            self._set_meta(te.parse_torrent(source["data"]))
        else:
            m = te.parse_magnet(source["magnet"])
            self.name.setText(m.name or m.info_hash)
            self._set_folder(m.name or "torrent")
            self.info.setText("Getting the file list from peers… (you can start now to download everything)")
            self.tree.setEnabled(False)
            self.meta_keys = m.keys
            self._fetch()

    # ---------------------------------------------------------------- metadata
    def _fetch(self) -> None:
        settings, magnet, cancel = self.ctl.settings, self.source["magnet"], self.cancel

        def done(meta, exc) -> None:
            if self.cancel.is_set():
                return
            if exc is not None:
                if not isinstance(exc, InterruptedError):
                    self.info.setText(f"Couldn't get the file list: {exc}")
                return
            self._set_meta(meta)

        run_async(lambda: te.fetch_metadata(settings, magnet, cancel), done)

    def _set_meta(self, meta: te.TorrentMeta) -> None:
        self.meta = meta
        self.meta_keys = meta.keys
        self.name.setText(meta.name)
        n = len(meta.files)
        self.info.setText(f"{n} file{'s' if n != 1 else ''}, {human_size(meta.total_size)}")
        if not self._folder_edited:
            self._set_folder(meta.root)
        self.tree.blockSignals(True)
        self.tree.clear()
        dirs: dict[tuple[str, ...], QTreeWidgetItem] = {}
        for idx, rel, size in meta.files:
            parts = rel.replace("\\", "/").split("/")
            if len(meta.files) > 1 and len(parts) > 1:
                parts = parts[1:]           # the torrent's own folder is shown as "Save to"
            parent: QTreeWidget | QTreeWidgetItem = self.tree
            for depth in range(len(parts) - 1):
                key = tuple(parts[:depth + 1])
                node = dirs.get(key)
                if node is None:
                    node = QTreeWidgetItem(parent, [parts[depth], ""])
                    node.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
                    node.setFlags(node.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
                    node.setCheckState(0, Qt.Checked)
                    node.setExpanded(True)
                    dirs[key] = node
                parent = node
            leaf = QTreeWidgetItem(parent, [parts[-1], human_size(size)])
            leaf.setFlags(leaf.flags() | Qt.ItemIsUserCheckable)
            leaf.setCheckState(0, Qt.Checked)
            leaf.setData(0, INDEX_ROLE, idx)
            leaf.setData(0, SIZE_ROLE, size)
            leaf.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        self.tree.blockSignals(False)
        self.tree.setEnabled(True)
        self._update_selected()
        dup = self._duplicate()
        if dup is not None:
            self.info.setText(self.info.text() + f"  —  <b>already in your download list</b> ({dup.filename})")

    def _duplicate(self):
        keys = getattr(self, "meta_keys", [])
        for it in self.ctl.manager.items.values():
            if it.kind != Kind.TORRENT.value:
                continue
            theirs = list((it.media or {}).get("info_hashes") or [])
            if (it.media or {}).get("info_hash"):
                theirs.append(it.media["info_hash"])
            if te.is_magnet(it.url):
                try:
                    theirs += te.parse_magnet(it.url).keys
                except ValueError:
                    pass
            if any(te.same_torrent(a, b) for a in keys for b in theirs):
                return it
        return None

    # ---------------------------------------------------------------- selection
    def _leaves(self) -> list[QTreeWidgetItem]:
        out = []
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            node = stack.pop()
            if node.data(0, INDEX_ROLE) is not None:
                out.append(node)
            stack.extend(node.child(i) for i in range(node.childCount()))
        return out

    def _check_all(self, on: bool) -> None:
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setCheckState(0, Qt.Checked if on else Qt.Unchecked)

    def _update_selected(self, *_a) -> None:
        leaves = self._leaves()
        chosen = [x for x in leaves if x.checkState(0) == Qt.Checked]
        size = sum(int(x.data(0, SIZE_ROLE)) for x in chosen)
        self.sel_lbl.setText(f"Selected: {len(chosen)} of {len(leaves)} files, {human_size(size)}")
        ok = bool(chosen) or self.meta is None
        self.start_btn.setEnabled(ok)
        self.later.setEnabled(ok)

    # ---------------------------------------------------------------- folder
    def _set_folder(self, root: str) -> None:
        st = self.ctl.settings
        name = sanitize_filename(root or "torrent")
        self.folder.setText(str(st.folder_for(name, st.category_for(name))))

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Save torrent into", self.folder.text())
        if d:
            self.folder.setText(d)
            self._folder_edited = True

    # ---------------------------------------------------------------- go
    def _go(self, start: bool, queue_id: int | None = None) -> None:
        folder = self.folder.text().strip()
        if not folder:
            return
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.warning(self, "Add torrent", f"Can't use this folder:\n{e}")
            return
        meta = self.meta
        prios = None
        size = -1
        if meta is not None:
            prios = [0] * meta.num_files
            size = 0
            for leaf in self._leaves():
                if leaf.checkState(0) == Qt.Checked:
                    prios[int(leaf.data(0, INDEX_ROLE))] = 4
                    size += int(leaf.data(0, SIZE_ROLE))
            if not any(prios):
                return
            if all(prios[i] for i, _p, _s in meta.files):
                prios = None                # everything: no need to store a selection
        self.cancel.set()
        self.ctl.add_torrent(
            url=self.source.get("magnet") or self.source.get("url") or "",
            data=meta.data if meta is not None else b"",
            name=meta.root if meta is not None else (self.name.text() or "torrent"),
            save_dir=folder, priorities=prios, size=size, keys=getattr(self, "meta_keys", []),
            start=start, queue_id=queue_id)
        self.accept()

    def done(self, r: int) -> None:  # noqa: D401 - Qt override
        self.cancel.set()
        super().done(r)
