#!/usr/bin/env python3
"""Circle to Search for desktop.

Hotkey -> the screen freezes with a Google-coloured glow, you circle / scribble /
tap anything, and Google Lens results slide up in a bottom sheet — like
Circle to Search on Android.

Usage:
    circle_search.py            trigger (starts the app if it is not running)
    circle_search.py --daemon   stay resident so later triggers open instantly
    circle_search.py --hotkey "<ctrl>+<alt>+s"
                                resident + global hotkey (X11 / Windows / macOS,
                                needs `pip install pynput`; on Wayland bind the
                                plain command to a shortcut in system settings)
    circle_search.py --import-cookies chromium [--browser-profile Default]
                                copy Google cookies (login, consent, captcha pass)
                                from chromium / chrome / brave into the app
"""

import argparse
import base64
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse

from PySide6.QtCore import (QBuffer, QDateTime, QEasingCurve, QIODevice, QLocale, QObject, QPointF,
                            QSettings,
                            QPropertyAnimation, QRect, QRectF, QSize, Qt,
                            QTimer, QUrl, Signal, Property, QStandardPaths)
from PySide6.QtGui import (QColor, QConicalGradient, QCursor, QDesktopServices, QIcon,
                           QGuiApplication, QImage, QKeySequence, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QShortcut)
from PySide6.QtNetwork import QLocalServer, QLocalSocket, QNetworkCookie
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QToolButton, QVBoxLayout, QWidget)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView

APP_ID = "circle-search"
GOOGLE_COLORS = ["#4285F4", "#EA4335", "#FBBC05", "#34A853"]
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36")
LENS_UPLOAD = "https://lens.google.com/v3/upload"
TAP_BOX = 220          # side of the box selected by a simple tap (logical px)
TAP_MAX_TRAVEL = 12    # a press that moved less than this is a tap
HANDLE_HIT = 30        # grab radius around a corner bracket
MAX_UPLOAD_SIDE = 2400 # Lens downsizes anyway; keeps the upload small
ICONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
TRANSLATE_QUERY = "переведи на русский"
DEFAULT_LANG = "ru"    # used when the system locale is C/POSIX or unset


def resolve_lang(override=None):
    """Google's interface language: --lang (remembered) > system locale > DEFAULT_LANG.

    Always sent explicitly, otherwise Google picks the language from the IP address.
    """
    settings = QSettings(APP_ID, APP_ID)
    if override:
        settings.setValue("lang", override)
        return override
    saved = settings.value("lang")
    if saved:
        return saved
    system = QLocale.system().name().split("_")[0]
    return system if system not in ("", "C", "POSIX") else DEFAULT_LANG


SEARCH_LANG = DEFAULT_LANG   # set in main()


def google_icon(name, color=None):
    """Icon from icons/: Google logos as is, Material Symbols tinted with `color`."""
    path = os.path.join(ICONS, name + ".svg")
    if color is None:
        # Qt's SVG renderer skips blur filters (the 2025 "G" gradient), so prefer a PNG render.
        png = os.path.join(ICONS, name + ".png")
        return QIcon(png if os.path.exists(png) else path)
    with open(path, encoding="utf-8") as f:
        svg = f.read().replace("<svg ", f'<svg fill="{color}" ', 1)
    pix = QPixmap()
    pix.loadFromData(svg.encode(), "SVG")
    return QIcon(pix)


# --------------------------------------------------------------------------- capture

def capture_screens(screens):
    """Return a QPixmap per screen (device pixel ratio set so each paints at logical size)."""
    if QGuiApplication.platformName() != "wayland":
        return [screen.grabWindow(0) for screen in screens]

    # Wayland forbids clients from reading the screen, so ask a compositor tool.
    path = os.path.join(tempfile.gettempdir(), f"{APP_ID}-{os.getpid()}.png")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    commands = []
    if shutil.which("spectacle"):
        commands.append(["spectacle", "-b", "-n", "-f", "-o", path])
    if shutil.which("grim"):
        commands.append(["grim", path])
    if shutil.which("gnome-screenshot"):
        commands.append(["gnome-screenshot", "-f", path])
    if "KDE" not in desktop:
        commands.sort(key=lambda c: c[0] == "spectacle")
    for cmd in commands:
        try:
            if os.path.exists(path):
                os.remove(path)
            subprocess.run(cmd, check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            full = QImage(path)
            if not full.isNull():
                break
        except (subprocess.SubprocessError, OSError):
            continue
    else:
        raise RuntimeError("No screenshot tool worked (install spectacle or grim)")
    os.remove(path)

    # The tools capture the whole desktop; cut out each screen.
    virtual = screens[0].virtualGeometry()
    scale = full.width() / virtual.width()
    shots = []
    for screen in screens:
        geo = screen.geometry().translated(-virtual.topLeft())
        crop = QRect(round(geo.x() * scale), round(geo.y() * scale),
                     round(geo.width() * scale), round(geo.height() * scale))
        pix = QPixmap.fromImage(full.copy(crop))
        pix.setDevicePixelRatio(scale)
        shots.append(pix)
    return shots


def encode_image(image):
    if max(image.width(), image.height()) > MAX_UPLOAD_SIDE:
        image = image.scaled(MAX_UPLOAD_SIDE, MAX_UPLOAD_SIDE, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


# The upload is a real form POST from inside the web view, so it carries the
# view's Google cookies exactly like a browser would (no captcha for "bot" uploads).
LENS_FORM_JS = """
(function (b64, action) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const files = new DataTransfer();
  files.items.add(new File([bytes], "screen.png", {type: "image/png"}));
  const form = document.createElement("form");
  form.method = "POST";
  form.enctype = "multipart/form-data";
  form.action = action;
  const input = document.createElement("input");
  input.type = "file";
  input.name = "encoded_image";
  input.files = files.files;
  form.appendChild(input);
  document.body.appendChild(form);
  form.submit();
})(__IMAGE__, __ACTION__);
"""

SEARCHING_HTML = """<!doctype html><meta name="viewport" content="width=device-width">
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#1f1f1f;color:#c4c7c5;font:15px sans-serif">Поиск…</body>"""


def lens_form_js(png):
    action = LENS_UPLOAD + "?" + urllib.parse.urlencode(
        {"ep": "ccm", "re": "dcsp", "s": "4", "st": str(int(time.time() * 1000)), "hl": SEARCH_LANG})
    return (LENS_FORM_JS.replace("__IMAGE__", json.dumps(base64.b64encode(png).decode()))
            .replace("__ACTION__", json.dumps(action)))


# --------------------------------------------------------------------------- web

class WebPage(QWebEnginePage):
    """Google pages stay in the sheet; everything else opens in the real browser."""

    externalOpened = Signal()

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if (is_main_frame and nav_type == QWebEnginePage.NavigationTypeLinkClicked
                and not is_google(url)):
            self.open_external(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def createWindow(self, _type):
        # target=_blank links: catch the first URL of a throwaway page.
        page = QWebEnginePage(self.profile(), self)
        def opened(url):
            if url.isValid() and not url.isEmpty():
                self.open_external(url)
                page.deleteLater()
        page.urlChanged.connect(opened)
        return page

    def open_external(self, url):
        QDesktopServices.openUrl(url)
        self.externalOpened.emit()


def is_google(url):
    host = url.host()
    return host == "google.com" or host.endswith(".google.com") or host.startswith("google.") \
        or ".google." in host or host.endswith("gstatic.com") or host.endswith("googleusercontent.com")


def make_profile(parent):
    data = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
    profile = QWebEngineProfile(APP_ID, parent)
    profile.setPersistentStoragePath(os.path.join(data, "web"))
    profile.setCachePath(os.path.join(data, "cache"))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
    profile.setHttpUserAgent(MOBILE_UA)  # Lens' mobile layout is the Circle to Search look
    profile.setHttpAcceptLanguage(f"{SEARCH_LANG},en;q=0.5")
    return profile


# --------------------------------------------------------------------------- UI pieces

class ResultSheet(QFrame):
    """Bottom sheet with a drag handle and the Lens page."""

    closeRequested = Signal()

    def __init__(self, profile, parent):
        super().__init__(parent)
        self.setObjectName("sheet")
        self.setStyleSheet("""
            #sheet { background: #1f1f1f; border-top-left-radius: 28px; border-top-right-radius: 28px; }
            QToolButton { color: #e3e3e3; background: transparent; border: none;
                          font-size: 18px; padding: 6px 10px; border-radius: 16px; }
            QToolButton:hover { background: #3a3a3a; }
        """)
        self.drag_origin = None
        self.expanded = False

        self.handle = QWidget(self)
        self.handle.setFixedHeight(28)
        self.handle.setCursor(Qt.OpenHandCursor)
        self.handle.paintEvent = self._paint_handle
        self.handle.mousePressEvent = self._handle_press
        self.handle.mouseMoveEvent = self._handle_move
        self.handle.mouseReleaseEvent = self._handle_release

        open_btn = QToolButton(self, icon=google_icon("open_in_new", "#e3e3e3"),
                               iconSize=QSize(22, 22), toolTip="Открыть в браузере")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(self.view.url()))
        close_btn = QToolButton(self, icon=google_icon("close", "#e3e3e3"),
                                iconSize=QSize(22, 22), toolTip="Закрыть (Esc)")
        close_btn.clicked.connect(self.closeRequested)

        top = QHBoxLayout()
        top.setContentsMargins(12, 0, 12, 0)
        top.addWidget(open_btn)
        top.addWidget(self.handle, 1)
        top.addWidget(close_btn)

        self.view = QWebEngineView(self)
        self.page = WebPage(profile, self.view)
        self.page.setBackgroundColor(QColor("#1f1f1f"))
        self.view.setPage(self.page)

        self.pending_js = None      # form upload to run once the "Поиск…" page is up
        self.pending_query = ""     # text to add to the Lens results (multisearch)
        self.page.loadFinished.connect(self.on_load_finished)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(top)
        lay.addWidget(self.view, 1)

        self.anim = QPropertyAnimation(self, b"geometry", self)
        self.anim.setDuration(320)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)

    def _paint_handle(self, _event):
        p = QPainter(self.handle)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#8e918f"))
        p.setPen(Qt.NoPen)
        w = 36
        p.drawRoundedRect(QRectF((self.handle.width() - w) / 2, 10, w, 4), 2, 2)

    def search_image(self, png, query=""):
        self.pending_js = lens_form_js(png)
        self.pending_query = query
        self.page.setHtml(SEARCHING_HTML, QUrl("https://lens.google.com/"))

    def search_text(self, text):
        self.pending_js, self.pending_query = None, ""
        self.view.load(QUrl("https://www.google.com/search?" +
                            urllib.parse.urlencode({"q": text, "hl": SEARCH_LANG})))

    def on_load_finished(self, ok):
        if not ok:
            return
        if self.pending_js:
            js, self.pending_js = self.pending_js, None
            self.page.runJavaScript(js)
            return
        url = self.page.url()
        if self.pending_query and url.path() == "/search" and "vsrid" in url.query():
            query, self.pending_query = self.pending_query, ""
            self.view.load(QUrl(url.toString() + "&q=" + urllib.parse.quote(query)))

    # geometry ---------------------------------------------------------------
    def target_rect(self, expanded=None, hidden=False):
        expanded = self.expanded if expanded is None else expanded
        area = self.parentWidget().rect()
        width = min(area.width(), max(560, round(area.width() * 0.42)))
        height = round(area.height() * (0.93 if expanded else 0.58))
        y = area.height() if hidden else area.height() - height
        return QRect((area.width() - width) // 2, y, width, height)

    def slide_to(self, rect):
        self.anim.stop()
        self.anim.setStartValue(self.geometry())
        self.anim.setEndValue(rect)
        self.anim.start()

    def present(self):
        if not self.isVisible():
            self.setGeometry(self.target_rect(hidden=True))
            self.show()
            self.raise_()
        self.slide_to(self.target_rect())

    def dismiss(self):
        if self.isVisible():
            self.slide_to(self.target_rect(hidden=True))
            QTimer.singleShot(self.anim.duration(), self.hide)

    # dragging the handle ----------------------------------------------------
    def _handle_press(self, e):
        self.drag_origin = (e.globalPosition().y(), self.geometry())
        self.handle.setCursor(Qt.ClosedHandCursor)

    def _handle_move(self, e):
        if not self.drag_origin:
            return
        start_y, geo = self.drag_origin
        dy = e.globalPosition().y() - start_y
        area = self.parentWidget().rect()
        top = max(round(area.height() * 0.07), min(area.height() - 80, geo.y() + dy))
        self.setGeometry(geo.x(), top, geo.width(), area.height() - top)

    def _handle_release(self, e):
        self.handle.setCursor(Qt.OpenHandCursor)
        start_y, geo = self.drag_origin
        self.drag_origin = None
        dy = e.globalPosition().y() - start_y
        area_h = self.parentWidget().height()
        if dy > 0 and self.y() > area_h * 0.7:
            self.closeRequested.emit()
            return
        if abs(dy) > 40:
            self.expanded = dy < 0
        self.slide_to(self.target_rect())


class SearchPill(QFrame):
    """The "G | Search…" bar shown at the bottom before anything is selected."""

    submitted = Signal(str)
    wholeScreen = Signal()
    translate = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("pill")
        self.setStyleSheet("""
            #pill { background: #1f1f1f; border-radius: 28px; }
            QLineEdit { background: transparent; border: none; color: #e3e3e3;
                        font-size: 17px; selection-background-color: #4285F4; }
            QToolButton { background: transparent; border: none; color: #c4c7c5;
                          font-size: 20px; padding: 6px; border-radius: 18px; }
            QToolButton:hover { background: #333537; }
        """)
        g = QLabel(self)
        g.setPixmap(google_icon("google_g").pixmap(QSize(26, 26), self.devicePixelRatioF()))
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("Поиск")
        self.edit.returnPressed.connect(lambda: self.submitted.emit(self.edit.text().strip()))
        translate = QToolButton(self, icon=google_icon("g_translate", "#c4c7c5"),
                                iconSize=QSize(26, 26), toolTip="Перевести экран")
        translate.clicked.connect(self.translate)
        lens = QToolButton(self, icon=google_icon("google_lens"), iconSize=QSize(26, 26),
                           toolTip="Искать по всему экрану")
        lens.clicked.connect(self.wholeScreen)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 6, 10, 6)
        lay.setSpacing(10)
        lay.addWidget(g)
        lay.addWidget(self.edit, 1)
        lay.addWidget(translate)
        lay.addWidget(lens)
        self.setFixedSize(QSize(560, 56))


# --------------------------------------------------------------------------- overlay

class Overlay(QWidget):
    closed = Signal()
    activated = Signal()          # the user started selecting / searching on this screen

    def __init__(self, profile):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.shot = None
        self.started = time.monotonic()

        self.stroke = []            # list of QPointF while drawing
        self.fading_stroke = []     # finished stroke fading out
        self.fade_start = 0.0
        self.selection = None       # QRectF
        self._sel_anim_value = 0.0
        self.sel_from = self.sel_to = None
        self.drag = None            # ("draw"|"move"|corner index, start pos, start rect)
        self.query = ""
        self._pan = 0.0            # screenshot slides up so the selection stays above the sheet

        self.sheet = ResultSheet(profile, self)
        self.sheet.hide()
        self.sheet.closeRequested.connect(self.close)
        self.sheet.page.externalOpened.connect(self.close)
        self.pill = SearchPill(self)
        self.pill.submitted.connect(self.on_text_search)
        self.pill.wholeScreen.connect(self.search_whole_screen)
        self.pill.translate.connect(self.translate_screen)

        self.sel_anim = QPropertyAnimation(self, b"selAnim", self)
        self.sel_anim.setDuration(380)
        self.sel_anim.setEasingCurve(QEasingCurve.OutBack)

        self.pan_anim = QPropertyAnimation(self, b"pan", self)
        self.pan_anim.setDuration(320)
        self.pan_anim.setEasingCurve(QEasingCurve.OutCubic)

        self.ticker = QTimer(self, interval=16, timeout=self.update)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.on_escape)
        QShortcut(QKeySequence.Copy, self, activated=self.copy_selection)

    # animated property ------------------------------------------------------
    def get_sel_anim(self):
        return self._sel_anim_value

    def set_sel_anim(self, v):
        self._sel_anim_value = v
        if self.sel_from is not None:
            a, b = self.sel_from, self.sel_to
            self.selection = QRectF(a.x() + (b.x() - a.x()) * v, a.y() + (b.y() - a.y()) * v,
                                    a.width() + (b.width() - a.width()) * v,
                                    a.height() + (b.height() - a.height()) * v)
        self.update()

    selAnim = Property(float, get_sel_anim, set_sel_anim)

    def get_pan(self):
        return self._pan

    def set_pan(self, v):
        self._pan = v
        self.update()

    pan = Property(float, get_pan, set_pan)

    def pan_to(self, value):
        self.pan_anim.stop()
        self.pan_anim.setStartValue(self._pan)
        self.pan_anim.setEndValue(value)
        self.pan_anim.start()

    def pan_for_sheet(self):
        """Like on Android: lift the screenshot so the selection sits above the sheet."""
        if not self.selection:
            return 0.0
        room = self.sheet.target_rect(expanded=False).top() - 24
        lift = max(0.0, self.selection.bottom() - room)
        return -min(lift, max(0.0, self.selection.top() - 24))

    # lifecycle --------------------------------------------------------------
    def start(self, screen, shot):
        self.shot = shot
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.started = time.monotonic()
        self.ticker.start()
        self.showFullScreen()
        self.layout_children()
        self.raise_()
        self.activateWindow()

    def resizeEvent(self, _e):
        self.layout_children()

    def layout_children(self):
        self.pill.move((self.width() - self.pill.width()) // 2, self.height() - self.pill.height() - 36)
        if self.sheet.isVisible() and self.sheet.anim.state() != QPropertyAnimation.Running:
            self.sheet.setGeometry(self.sheet.target_rect())

    def reset(self):
        """Another screen took over: drop this screen's selection and results."""
        self.selection = self.sel_from = None
        self.sel_anim.stop()
        self.stroke, self.fading_stroke = [], []
        self.sheet.dismiss()
        self.pan_to(0.0)
        self.pill.show()
        self.started = time.monotonic()

    def on_escape(self):
        if self.sheet.isVisible() and self.sheet.expanded:
            self.sheet.expanded = False
            self.sheet.slide_to(self.sheet.target_rect())
        else:
            self.close()

    def closeEvent(self, e):
        self.ticker.stop()
        self.closed.emit()
        super().closeEvent(e)

    # painting ---------------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = time.monotonic()
        intro = min(1.0, (now - self.started) / 0.35)
        r = QRectF(self.rect())

        if self._pan:
            p.fillRect(r, QColor("black"))
            p.translate(0, self._pan)
        p.drawPixmap(0, 0, self.shot)

        # Dim: light while choosing, darker outside the selection once chosen.
        if self.selection:
            outside = QPainterPath()
            outside.addRect(r)
            hole = QPainterPath()
            hole.addRoundedRect(self.selection, 18, 18)
            p.fillPath(outside.subtracted(hole), QColor(0, 0, 0, 120))
        else:
            p.fillRect(r, QColor(0, 0, 0, round(70 * intro)))

        drawing = bool(self.stroke)
        if not self.selection and not drawing:
            self.paint_edge_glow(p, r, now, intro)
            self.paint_hint(p, intro)
        if self.fading_stroke:
            t = (now - self.fade_start) / 0.35
            if t >= 1:
                self.fading_stroke = []
            else:
                self.paint_stroke(p, self.fading_stroke, 1 - t)
        if drawing:
            self.paint_stroke(p, self.stroke, 1.0)
        if self.selection and not drawing:
            self.paint_brackets(p, self.selection)
        p.resetTransform()

    def paint_edge_glow(self, p, r, now, intro):
        grad = QConicalGradient(r.center(), (now * 60) % 360)
        for i, c in enumerate(GOOGLE_COLORS + GOOGLE_COLORS[:1]):
            grad.setColorAt(i / 4, QColor(c))
        breathe = 0.85 + 0.15 * math.sin(now * 2.4)
        p.setBrush(Qt.NoBrush)
        for width, alpha in ((44, 0.06), (30, 0.10), (18, 0.18), (9, 0.35), (4, 0.9)):
            p.setOpacity(alpha * intro * breathe)
            p.setPen(QPen(grad, width))
            p.drawRoundedRect(r.adjusted(-2, -2, 2, 2), 24, 24)
        p.setOpacity(1)

    def paint_hint(self, p, intro):
        text = "Обведите или коснитесь, чтобы найти"
        font = p.font()
        font.setPixelSize(17)
        p.setFont(font)
        w = p.fontMetrics().horizontalAdvance(text) + 44
        box = QRectF((self.width() - w) / 2, 34 - (1 - intro) * 20, w, 44)
        p.setOpacity(intro)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(31, 31, 31, 225))
        p.drawRoundedRect(box, 22, 22)
        p.setPen(QColor("#e3e3e3"))
        p.drawText(box, Qt.AlignCenter, text)
        p.setOpacity(1)

    def paint_stroke(self, p, points, opacity):
        if len(points) < 2:
            return
        path = QPainterPath(points[0])
        for a, b in zip(points[1:], points[2:]):
            path.quadTo(a, (a + b) / 2)
        path.lineTo(points[-1])
        box = path.boundingRect()
        grad = QLinearGradient(box.topLeft(), box.bottomRight())
        for i, c in enumerate(GOOGLE_COLORS):
            grad.setColorAt(i / 3, QColor(c))
        p.setBrush(Qt.NoBrush)
        for width, alpha, brush in ((26, 0.18, grad), (14, 0.45, grad), (6, 0.95, QColor("white"))):
            p.setOpacity(alpha * opacity)
            p.setPen(QPen(brush, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
        p.setOpacity(1)

    def paint_brackets(self, p, rect):
        radius, leg = 18, 30
        leg_x = min(leg, rect.width() / 2 - radius)
        leg_y = min(leg, rect.height() / 2 - radius)
        for cx, cy, sx, sy in self.corner_points(rect):
            path = QPainterPath(QPointF(cx, cy + sy * (radius + leg_y)))
            path.lineTo(cx, cy + sy * radius)
            path.quadTo(QPointF(cx, cy), QPointF(cx + sx * radius, cy))
            path.lineTo(cx + sx * (radius + leg_x), cy)
            p.setPen(QPen(QColor(0, 0, 0, 70), 9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(QPen(QColor("white"), 5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)

    @staticmethod
    def corner_points(rect):
        # (x, y, direction the legs go in x, in y)
        return [(rect.left(), rect.top(), 1, 1), (rect.right(), rect.top(), -1, 1),
                (rect.right(), rect.bottom(), -1, -1), (rect.left(), rect.bottom(), 1, -1)]

    # input ------------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.close()
            return
        if e.button() != Qt.LeftButton:
            return
        pos = self.to_image(e.position())
        if self.selection:
            for i, (cx, cy, _, _) in enumerate(self.corner_points(self.selection)):
                if math.hypot(pos.x() - cx, pos.y() - cy) < HANDLE_HIT:
                    self.drag = (i, pos, QRectF(self.selection))
                    return
            if self.selection.contains(pos):
                self.drag = ("move", pos, QRectF(self.selection))
                return
        self.drag = ("draw", pos, None)
        self.stroke = [pos]
        self.activated.emit()
        self.update()

    def to_image(self, pos):
        return pos - QPointF(0, self._pan)

    def mouseMoveEvent(self, e):
        pos = self.to_image(e.position())
        if not self.drag:
            self.update_cursor(pos)
            return
        kind, start, rect = self.drag
        if kind == "draw":
            if (pos - self.stroke[-1]).manhattanLength() > 2:
                self.stroke.append(pos)
                if len(self.stroke) == 3 and self.selection:
                    self.selection = None
                    self.sheet.dismiss()
                    self.pan_to(0.0)
        elif kind == "move":
            self.selection = self.clamp(rect.translated(pos - start))
        else:
            self.selection = self.resize_rect(rect, kind, pos)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self.drag:
            return
        kind, start, rect = self.drag
        self.drag = None
        if kind == "draw":
            points, self.stroke = self.stroke, []
            travel = max(math.hypot(p.x() - start.x(), p.y() - start.y()) for p in points)
            if travel < TAP_MAX_TRAVEL:
                target = QRectF(start.x() - TAP_BOX / 2, start.y() - TAP_BOX / 2, TAP_BOX, TAP_BOX)
                origin = QRectF(start.x() - 4, start.y() - 4, 8, 8)
            else:
                xs, ys = [p.x() for p in points], [p.y() for p in points]
                origin = QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
                target = origin.adjusted(-10, -10, 10, 10)
                if target.width() < 48 or target.height() < 48:
                    target = QRectF(target.center().x() - max(24, target.width() / 2),
                                    target.center().y() - max(24, target.height() / 2),
                                    max(48, target.width()), max(48, target.height()))
                self.fading_stroke, self.fade_start = points, time.monotonic()
            self.animate_selection(origin, self.clamp(target))
            QTimer.singleShot(self.sel_anim.duration(), self.search_selection)
        elif self.selection != rect:
            self.search_selection()

    def update_cursor(self, pos):
        if self.selection:
            for i, (cx, cy, _, _) in enumerate(self.corner_points(self.selection)):
                if math.hypot(pos.x() - cx, pos.y() - cy) < HANDLE_HIT:
                    self.setCursor(Qt.SizeFDiagCursor if i % 2 == 0 else Qt.SizeBDiagCursor)
                    return
            if self.selection.contains(pos):
                self.setCursor(Qt.SizeAllCursor)
                return
        self.setCursor(Qt.CrossCursor)

    def animate_selection(self, origin, target):
        self.sel_from, self.sel_to = origin, target
        self.sel_anim.stop()
        self.sel_anim.setStartValue(0.0)
        self.sel_anim.setEndValue(1.0)
        self.sel_anim.start()

    def clamp(self, rect):
        bounds = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        rect = QRectF(rect)
        rect.setWidth(min(rect.width(), bounds.width()))
        rect.setHeight(min(rect.height(), bounds.height()))
        rect.moveLeft(max(bounds.left(), min(rect.left(), bounds.right() - rect.width())))
        rect.moveTop(max(bounds.top(), min(rect.top(), bounds.bottom() - rect.height())))
        return rect

    def resize_rect(self, rect, corner, pos):
        x1, y1, x2, y2 = rect.left(), rect.top(), rect.right(), rect.bottom()
        if corner in (0, 3):
            x1 = min(pos.x(), x2 - 48)
        else:
            x2 = max(pos.x(), x1 + 48)
        if corner in (0, 1):
            y1 = min(pos.y(), y2 - 48)
        else:
            y2 = max(pos.y(), y1 + 48)
        return self.clamp(QRectF(QPointF(x1, y1), QPointF(x2, y2)))

    # search -----------------------------------------------------------------
    def selection_image(self, rect=None):
        rect = rect or self.selection
        dpr = self.shot.devicePixelRatio()
        px = QRect(round(rect.x() * dpr), round(rect.y() * dpr),
                   round(rect.width() * dpr), round(rect.height() * dpr))
        return self.shot.copy(px).toImage()

    def copy_selection(self):
        if self.sheet.view.hasFocus():
            self.sheet.page.triggerAction(QWebEnginePage.Copy)
        elif self.selection:
            QGuiApplication.clipboard().setImage(self.selection_image())

    def search_selection(self):
        if self.selection:
            self.run_lens(self.selection_image())

    def search_whole_screen(self):
        self.query = self.pill.edit.text().strip()
        self.run_lens(self.shot.toImage())

    def translate_screen(self):
        self.query = TRANSLATE_QUERY
        self.run_lens(self.shot.toImage())

    def on_text_search(self, text):
        if not text:
            return
        self.query = text
        if self.selection:
            self.search_selection()
            return
        self.activated.emit()
        self.pill.hide()
        self.sheet.search_text(text)
        self.sheet.present()

    def run_lens(self, image):
        self.activated.emit()
        self.pill.hide()
        self.sheet.present()
        self.pan_to(self.pan_for_sheet())
        self.sheet.search_image(encode_image(image), self.query)


# --------------------------------------------------------------------------- app

class Controller(QObject):
    triggered = Signal()

    def __init__(self, app, resident):
        super().__init__()
        self.app = app
        self.resident = resident
        self.profile = make_profile(self)
        self.overlays = []
        self.triggered.connect(self.trigger)

        self.server = QLocalServer(self)
        QLocalServer.removeServer(APP_ID)
        self.server.listen(APP_ID)
        self.server.newConnection.connect(self.on_connection)

    def on_connection(self):
        sock = self.server.nextPendingConnection()
        sock.readyRead.connect(lambda: self.handle_message(sock, bytes(sock.readAll()).decode()))

    def handle_message(self, sock, message):
        if message.startswith("import-cookies "):
            _, browser, profile = message.split(" ", 2)
            sock.write(import_cookies(self.profile, browser, profile).encode())
            sock.flush()
        else:
            self.trigger()

    def trigger(self):
        if self.overlays:
            self.close_all()
            return
        screens = QGuiApplication.screens()
        try:
            shots = capture_screens(screens)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            if not self.resident:
                self.app.quit()
            return
        for screen, shot in zip(screens, shots):
            overlay = Overlay(self.profile)
            overlay.closed.connect(self.close_all)
            overlay.activated.connect(lambda o=overlay: self.on_activated(o))
            self.overlays.append(overlay)
            overlay.start(screen, shot)
        # Keyboard focus goes to the screen with the mouse (if the platform knows it).
        current = QGuiApplication.screenAt(QCursor.pos())
        for overlay in self.overlays:
            if overlay.screen() == current:
                overlay.activateWindow()

    def on_activated(self, active):
        for overlay in self.overlays:
            if overlay is not active and (overlay.selection or overlay.sheet.isVisible()):
                overlay.reset()

    def close_all(self):
        overlays, self.overlays = self.overlays, []
        for overlay in overlays:
            overlay.close()
        if overlays and not self.resident:
            QTimer.singleShot(0, self.app.quit)


# --------------------------------------------------------------------------- cookies

BROWSERS = {   # name: (Linux config dir, keyring application, KWallet name)
    "chromium": ("~/.config/chromium", "chromium", "Chromium"),
    "chrome": ("~/.config/google-chrome", "chrome", "Chrome"),
    "brave": ("~/.config/BraveSoftware/Brave-Browser", "brave", "Brave"),
}
CHROME_EPOCH = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def keyring_password(app_name, wallet_name):
    """Chromium's "Safe Storage" password from Secret Service or KWallet ("" if none)."""
    tries = [["secret-tool", "lookup", "application", app_name],
             ["kwallet-query", "-r", f"{wallet_name} Safe Storage", "-f", f"{wallet_name} Keys",
              "kdewallet"]]
    for cmd in tries:
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
        except subprocess.SubprocessError:
            continue
        if out and "failed" not in out.lower():
            return out
    return ""


def import_cookies(profile, browser, browser_profile="Default"):
    """Copy Google cookies from a Chromium-based browser into the app's web profile."""
    if browser not in BROWSERS:
        return f"Unknown browser {browser!r}, choose from: {', '.join(BROWSERS)}"
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return "Cookie import needs: pip install cryptography"
    base, app_name, wallet_name = BROWSERS[browser]
    db_path = os.path.join(os.path.expanduser(base), browser_profile, "Cookies")
    if not os.path.exists(db_path):
        return f"No cookie database at {db_path}"

    # The browser keeps the database locked while running, so read a copy.
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "Cookies")
        shutil.copy2(db_path, copy)
        db = sqlite3.connect(copy)
        version = int((db.execute("SELECT value FROM meta WHERE key='version'").fetchone() or [0])[0])
        rows = db.execute(
            "SELECT host_key, name, value, encrypted_value, path, expires_utc, is_secure,"
            " is_httponly, samesite FROM cookies WHERE host_key LIKE '%google.%'").fetchall()
        db.close()

    keys = {b"v10": hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)}
    password = keyring_password(app_name, wallet_name)
    if password:
        keys[b"v11"] = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1, 16)

    def decrypt(blob):
        key = keys.get(blob[:3])
        if not key:
            return None
        dec = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        raw = dec.update(blob[3:]) + dec.finalize()
        raw = raw[:-raw[-1]]
        return raw[32:] if version >= 24 else raw  # newer DBs prefix a domain hash

    same_site = {0: QNetworkCookie.SameSite.None_, 1: QNetworkCookie.SameSite.Lax,
                 2: QNetworkCookie.SameSite.Strict}
    store = profile.cookieStore()
    imported = skipped = 0
    for host, name, value, encrypted, path, expires, secure, httponly, samesite in rows:
        if not value and encrypted:
            try:
                value = decrypt(encrypted)
            except (ValueError, IndexError):
                value = None
            if value is None:
                skipped += 1
                continue
        cookie = QNetworkCookie(name.encode(), value if isinstance(value, bytes) else value.encode())
        cookie.setDomain(host)
        cookie.setPath(path)
        cookie.setSecure(bool(secure))
        cookie.setHttpOnly(bool(httponly))
        if samesite in same_site:
            cookie.setSameSitePolicy(same_site[samesite])
        if expires:
            cookie.setExpirationDate(QDateTime.fromSecsSinceEpoch(expires // 1_000_000 - CHROME_EPOCH))
        store.setCookie(cookie, QUrl("https://" + host.lstrip(".")))
        imported += 1
    result = f"Imported {imported} Google cookies from {browser}"
    if skipped:
        result += f" ({skipped} could not be decrypted — keyring locked or unavailable)"
    return result


def send_to_running_instance(message, reply_timeout=0):
    """Hand `message` to an already running instance; returns None if there is none."""
    sock = QLocalSocket()
    sock.connectToServer(APP_ID)
    if not sock.waitForConnected(300):
        return None
    sock.write(message.encode())
    sock.waitForBytesWritten(300)
    reply = ""
    if reply_timeout and sock.waitForReadyRead(reply_timeout):
        reply = bytes(sock.readAll()).decode()
    sock.disconnectFromServer()
    return reply


def start_hotkey(combo, controller):
    try:
        from pynput import keyboard
    except ImportError:
        sys.exit("--hotkey needs pynput: pip install pynput")
    listener = keyboard.GlobalHotKeys({combo: controller.triggered.emit})
    listener.daemon = True
    listener.start()


def main():
    ap = argparse.ArgumentParser(description="Circle to Search for desktop")
    ap.add_argument("--daemon", action="store_true", help="stay running in the background")
    ap.add_argument("--hotkey", help='global hotkey for X11/Windows/macOS, e.g. "<ctrl>+<alt>+s"')
    ap.add_argument("--import-cookies", metavar="BROWSER", choices=sorted(BROWSERS),
                    help="copy Google cookies from this browser (close it first for best results)")
    ap.add_argument("--browser-profile", default="Default", help="browser profile folder name")
    ap.add_argument("--lang", help="Google language, e.g. ru, en, kk (remembered); default: system or ru")
    args = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    # Only claim a desktop file once install.sh created it, or the portal warns about it.
    apps_dir = QStandardPaths.writableLocation(QStandardPaths.ApplicationsLocation)
    if os.path.exists(os.path.join(apps_dir, APP_ID + ".desktop")):
        app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(google_icon("google_lens"))
    global SEARCH_LANG
    SEARCH_LANG = resolve_lang(args.lang)

    message = (f"import-cookies {args.import_cookies} {args.browser_profile}"
               if args.import_cookies else "trigger")
    reply = send_to_running_instance(message, 30000 if args.import_cookies else 0)
    if reply is not None:
        if reply:
            print(reply)
        return
    if args.import_cookies:
        profile = make_profile(app)
        print(import_cookies(profile, args.import_cookies, args.browser_profile))
        # The cookie store only commits once the browser context is up: load a blank page,
        # then give it a moment to write to disk.
        page = QWebEnginePage(profile)
        page.loadFinished.connect(lambda _ok: QTimer.singleShot(1500, app.quit))
        page.setHtml("", QUrl("https://www.google.com/"))
        app.exec()
        return
    resident = args.daemon or bool(args.hotkey)
    controller = Controller(app, resident)
    if args.hotkey:
        start_hotkey(args.hotkey, controller)
    if not resident:
        QTimer.singleShot(0, controller.trigger)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
