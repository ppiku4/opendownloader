"""Custom widgets: connection segment bar and progress delegate."""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionProgressBar, QWidget

from ..core.models import DownloadItem, Status


class SegmentBar(QWidget):
    """Shows which byte ranges of the file are downloaded, per connection -
    the classic multi-connection visual."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.item: DownloadItem | None = None
        self.setFixedHeight(20)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(400, 22)

    def set_item(self, item: DownloadItem) -> None:
        self.item = item
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        r = self.rect().adjusted(0, 0, -1, -1)
        pal = self.palette()
        p.fillRect(r, pal.color(QPalette.Base))
        p.setPen(pal.color(QPalette.Mid))
        p.drawRect(r)
        it = self.item
        if it is None:
            return
        inner = QRectF(r.adjusted(1, 1, 0, 0))
        done = QColor("#1f7ae0")
        edge = QColor("#0b4f9c")
        if it.size and it.size > 0 and it.segments:
            scale = inner.width() / it.size
            for seg in list(it.segments):
                x = inner.left() + seg.start * scale
                w = max(0.0, seg.downloaded * scale)
                p.fillRect(QRectF(x, inner.top(), w, inner.height()), done)
                p.fillRect(QRectF(x, inner.top(), 1, inner.height()), edge)
        else:
            frac = it.progress
            if it.status == Status.COMPLETE:
                frac = 1.0
            p.fillRect(QRectF(inner.left(), inner.top(), inner.width() * frac, inner.height()), done)


class ProgressDelegate(QStyledItemDelegate):
    """Draws a progress bar in the Status column."""

    def __init__(self, get_item, parent=None):
        super().__init__(parent)
        self.get_item = get_item

    def paint(self, painter, option, index) -> None:
        item: DownloadItem | None = self.get_item(index.row())
        if item is None or item.status == Status.COMPLETE or item.size <= 0 and not item.status.active:
            super().paint(painter, option, index)
            return
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        bar = QStyleOptionProgressBar()
        bar.rect = QRect(option.rect.adjusted(2, 3, -2, -3))
        bar.minimum, bar.maximum = 0, 1000
        bar.progress = int(item.progress * 1000) if item.size > 0 else 0
        bar.text = index.data() or ""
        bar.textVisible = True
        bar.textAlignment = Qt.AlignCenter
        QApplication.style().drawControl(QStyle.CE_ProgressBar, bar, painter)
