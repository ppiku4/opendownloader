"""Vector icons drawn at runtime (no image assets needed)."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

ACCENT = QColor("#1f7ae0")
GREEN = QColor("#20a35a")
RED = QColor("#d64545")
ORANGE = QColor("#e08a1f")
GREY = QColor("#6b7280")
PURPLE = QColor("#7c4ddb")


def _canvas(size: int) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 32.0, size / 32.0)
    return pm, p


def _circle_bg(p: QPainter, color: QColor) -> None:
    g = QLinearGradient(0, 0, 0, 32)
    g.setColorAt(0, color.lighter(125))
    g.setColorAt(1, color.darker(110))
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(2, 2, 28, 28), 7, 7)


def _white_pen(w: float = 3.0) -> QPen:
    pen = QPen(QColor("white"), w)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _draw(kind: str, p: QPainter) -> None:
    if kind == "app":
        _circle_bg(p, ACCENT)
        p.setPen(_white_pen(3.2))
        p.drawLine(QPointF(16, 7), QPointF(16, 19))
        p.drawPolyline(QPolygonF([QPointF(10, 14), QPointF(16, 20), QPointF(22, 14)]))
        p.drawLine(QPointF(9, 25), QPointF(23, 25))
    elif kind == "add":
        _circle_bg(p, GREEN)
        p.setPen(_white_pen(3.4))
        p.drawLine(QPointF(16, 9), QPointF(16, 23))
        p.drawLine(QPointF(9, 16), QPointF(23, 16))
    elif kind == "resume":
        _circle_bg(p, GREEN)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawPolygon(QPolygonF([QPointF(12, 9), QPointF(24, 16), QPointF(12, 23)]))
    elif kind == "pause":
        _circle_bg(p, ORANGE)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawRoundedRect(QRectF(10, 9, 4.5, 14), 1, 1)
        p.drawRoundedRect(QRectF(17.5, 9, 4.5, 14), 1, 1)
    elif kind == "stopall":
        _circle_bg(p, RED)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawRoundedRect(QRectF(10, 10, 12, 12), 2, 2)
    elif kind == "delete":
        _circle_bg(p, RED)
        p.setPen(_white_pen(3.2))
        p.drawLine(QPointF(11, 11), QPointF(21, 21))
        p.drawLine(QPointF(21, 11), QPointF(11, 21))
    elif kind == "clean":
        _circle_bg(p, GREY)
        p.setPen(_white_pen(2.6))
        p.drawPolyline(QPolygonF([QPointF(9, 16), QPointF(14, 21), QPointF(23, 11)]))
    elif kind == "options":
        _circle_bg(p, GREY)
        p.setPen(_white_pen(2.4))
        for y, x in ((11, 20), (16, 12), (21, 18)):
            p.drawLine(QPointF(8, y), QPointF(24, y))
            p.setBrush(QColor("white"))
            p.drawEllipse(QPointF(x, y), 2.2, 2.2)
    elif kind == "scheduler":
        _circle_bg(p, PURPLE)
        p.setPen(_white_pen(2.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(16, 16), 8.5, 8.5)
        p.drawPolyline(QPolygonF([QPointF(16, 11), QPointF(16, 16), QPointF(20, 18)]))
    elif kind == "startq":
        _circle_bg(p, ACCENT)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawPolygon(QPolygonF([QPointF(9, 9), QPointF(17, 16), QPointF(9, 23)]))
        p.drawPolygon(QPolygonF([QPointF(16, 9), QPointF(24, 16), QPointF(16, 23)]))
    elif kind == "stopq":
        _circle_bg(p, ACCENT.darker(130))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawRect(QRectF(9, 10, 5, 12))
        p.drawRect(QRectF(18, 10, 5, 12))
    elif kind == "grabber":
        _circle_bg(p, QColor("#0e9aa7"))
        p.setPen(_white_pen(2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(16, 16), 8.5, 8.5)
        p.drawEllipse(QRectF(12, 7.5, 8, 17))
        p.drawLine(QPointF(7.5, 16), QPointF(24.5, 16))
    elif kind == "batch":
        _circle_bg(p, QColor("#8a6d3b"))
        p.setPen(_white_pen(2.4))
        for y in (11, 16, 21):
            p.drawLine(QPointF(9, y), QPointF(23, y))
    elif kind == "video":
        _circle_bg(p, RED)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawRoundedRect(QRectF(7, 10, 13, 12), 2, 2)
        p.drawPolygon(QPolygonF([QPointF(20, 16), QPointF(26, 11), QPointF(26, 21)]))
    elif kind.startswith("folder"):
        color = {"folder": QColor("#e8b04a"), "folder-done": GREEN, "folder-unfinished": ORANGE,
                 "folder-queue": ACCENT}.get(kind, QColor("#e8b04a"))
        path = QPainterPath()
        path.moveTo(3, 8)
        path.lineTo(12, 8)
        path.lineTo(15, 11)
        path.lineTo(29, 11)
        path.lineTo(29, 26)
        path.lineTo(3, 26)
        path.closeSubpath()
        p.setPen(QPen(color.darker(130), 1))
        p.setBrush(color)
        p.drawPath(path)
    else:
        _circle_bg(p, GREY)


@lru_cache(maxsize=None)
def icon(kind: str) -> QIcon:
    ic = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm, p = _canvas(size)
        _draw(kind, p)
        p.end()
        ic.addPixmap(pm)
    return ic


def pixmap(kind: str, size: int) -> QPixmap:
    pm, p = _canvas(size)
    _draw(kind, p)
    p.end()
    return pm
