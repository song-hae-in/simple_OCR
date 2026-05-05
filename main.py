"""
PyQt6 OCR 번역기: A/B 각각 영역 캡처 → Tesseract OCR → Google 번역 → 해당 반투명 창.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple

from PIL import ImageGrab
from PyQt6.QtCore import QObject, QPoint, QRect, Qt, QRunnable, QThreadPool, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QCursor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from deep_translator import GoogleTranslator
import pytesseract


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        out.append(p)
    return out


def _tesseract_candidates() -> list[Path]:
    """실행 권한 문제가 적은 경로를 먼저 두고, Program Files는 뒤로 둡니다."""
    here = Path(__file__).resolve().parent
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    items: list[Path] = []
    cmd = os.environ.get("TESSERACT_CMD", "").strip()
    if cmd:
        items.append(Path(cmd))
    rest: list[Path] = [
        here / "tesseract" / "tesseract.exe",
        Path(r"C:\Tesseract-OCR\tesseract.exe"),
        Path.home() / "scoop" / "shims" / "tesseract.exe",
        Path(pf) / "Tesseract-OCR" / "tesseract.exe",
        Path(pf86) / "Tesseract-OCR" / "tesseract.exe",
    ]
    if local:
        rest.insert(1, Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
    items.extend(rest)
    return _unique_paths(items)


def configure_tesseract() -> Optional[str]:
    """첫 번째로 존재하는 tesseract.exe로 pytesseract를 설정. 없으면 PATH 기본 동작 유지."""
    for cand in _tesseract_candidates():
        if not cand or not cand.is_file():
            continue
        resolved = str(cand.resolve())
        pytesseract.pytesseract.tesseract_cmd = resolved
        return resolved
    return None


def _ocr_access_denied_hint(exc: BaseException) -> str:
    w = getattr(exc, "winerror", None)
    if isinstance(exc, PermissionError) or w == 5:
        return (
            "\n\n[WinError 5 · 접근 거부]\n"
            "• 관리자 권한이 아닌 사용자 폴더에 Tesseract를 두는 방법을 권장합니다.\n"
            "  예: 설치 경로를 "
            r"%LOCALAPPDATA%\Programs\Tesseract-OCR"
            " 로 지정하거나, "
            r"C:\Tesseract-OCR"
            " 등에 복사합니다.\n"
            "• 환경 변수 "
            "TESSERACT_CMD"
            " 에 "
            "tesseract.exe"
            " 의 전체 경로를 넣고 프로그램을 다시 실행해 보세요.\n"
            "• 백신/Windows 보안에서 Python 또는 tesseract.exe 실행이 차단되지 않았는지 확인하세요."
        )
    return ""


# (표시명, Google 번역 언어 코드)
LANG_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("English", "en"),
    ("한국어", "ko"),
    ("日本語", "ja"),
    ("中文(简体)", "zh-CN"),
    ("中文(繁體)", "zh-TW"),
    ("Español", "es"),
    ("Français", "fr"),
    ("Deutsch", "de"),
    ("Русский", "ru"),
    ("Português", "pt"),
    ("Italiano", "it"),
    ("العربية", "ar"),
    ("हिन्दी", "hi"),
    ("Tiếng Việt", "vi"),
    ("ไทย", "th"),
)


class TranslatorSignals(QObject):
    """QThreadPool Runnable에서 메인 스레드로 결과 전달."""

    finished = pyqtSignal(str, str)  # window_key ('a'|'b'), text


class TranslateRunnable(QRunnable):
    def __init__(
        self,
        text: str,
        target_lang: str,
        window_key: str,
        signals: TranslatorSignals,
    ) -> None:
        super().__init__()
        self._text = text
        self._target_lang = target_lang
        self._window_key = window_key
        self._signals = signals

    def run(self) -> None:
        try:
            translator = GoogleTranslator(source="auto", target=self._target_lang)
            out = translator.translate(self._text)
        except Exception as exc:  # noqa: BLE001 — 사용자에게 표시
            out = f"[번역 오류] {exc}"
        self._signals.finished.emit(self._window_key, out)


class TranslationWindow(QWidget):
    """반투명, 항상 위, 드래그로 이동 가능한 번역 결과 창."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._drag_anchor: Optional[QPoint] = None
        self.setWindowTitle(title)
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowOpacity(0.88)

        self._label = QLabel("")
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setStyleSheet(
            "background-color: rgba(30, 30, 40, 220); color: #f0f0f0; "
            "padding: 12px; border-radius: 8px;"
        )
        self._label.setFont(QFont("Segoe UI", 11))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.addWidget(self._label)

        self.resize(420, 220)

    def setText(self, text: str) -> None:
        self._label.setText(text)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_anchor = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_anchor is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_anchor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_anchor = None
        super().mouseReleaseEvent(event)


class RegionSelector(QWidget):
    """전체 화면에서 드래그로 사각형 영역 지정."""

    region_selected = pyqtSignal(int, int, int, int)  # left, top, right, bottom

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None

    def showEvent(self, event) -> None:
        screen = QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.geometry())
        super().showEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._origin is not None:
            end = event.position().toPoint()
            x1, x2 = sorted((self._origin.x(), end.x()))
            y1, y2 = sorted((self._origin.y(), end.y()))
            top_left = self.mapToGlobal(QPoint(x1, y1))
            bottom_right = self.mapToGlobal(QPoint(x2, y2))
            left, top = top_left.x(), top_left.y()
            right, bottom = bottom_right.x(), bottom_right.y()
            if right - left >= 2 and bottom - top >= 2:
                self.region_selected.emit(left, top, right, bottom)
            self.close()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 1. 전체 화면을 어두운 반투명으로 채웁니다.
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

        if self._origin and self._current:
            # 드래그한 사각형 계산
            rect = QRect(self._origin, self._current).normalized()

            # 2. 선택 영역을 투명하게 '뚫어버림' (선택 영역이 강조됨)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, Qt.GlobalColor.transparent)
            
            # 3. 테두리 그리기 (다시 기본 모드로 복구)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QPen(QColor(80, 160, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect)

class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.capture_coords_a: Optional[Tuple[int, int, int, int]] = None
        self.capture_coords_b: Optional[Tuple[int, int, int, int]] = None
        self._region_target_key: str = "a"
        self._pool = QThreadPool.globalInstance()
        self._translator_signals = TranslatorSignals()
        self._translator_signals.finished.connect(self._on_translation_finished)

        self._win_a = TranslationWindow("번역 A (Google)")
        self._win_b = TranslationWindow("번역 B (Google)")
        self._pending_keys: set[str] = set()

        self.setWindowTitle("OCR 번역기")
        self.setWindowFlags(
            self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        )

        self._btn_region_a = QPushButton("영역 지정 A")
        self._btn_region_a.clicked.connect(lambda: self._open_region_selector("a"))
        self._btn_region_b = QPushButton("영역 지정 B")
        self._btn_region_b.clicked.connect(lambda: self._open_region_selector("b"))

        self._btn_translate_a = QPushButton("번역 실행 A")
        self._btn_translate_a.clicked.connect(lambda: self._run_translation_for("a"))
        self._btn_translate_b = QPushButton("번역 실행 B")
        self._btn_translate_b.clicked.connect(lambda: self._run_translation_for("b"))

        self._shortcut_translate_a = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._shortcut_translate_a.activated.connect(lambda: self._run_translation_for("a"))
        self._shortcut_translate_b = QShortcut(QKeySequence("Ctrl+Shift+Return"), self)
        self._shortcut_translate_b.activated.connect(lambda: self._run_translation_for("b"))

        self._combo_a = QComboBox()
        self._combo_b = QComboBox()
        for label, code in LANG_OPTIONS:
            self._combo_a.addItem(label, code)
            self._combo_b.addItem(label, code)
        self._combo_a.setCurrentIndex(1)  # ko
        self._combo_b.setCurrentIndex(0)  # en

        row_langs = QHBoxLayout()
        row_langs.addWidget(QLabel("A 목표:"))
        row_langs.addWidget(self._combo_a)
        row_langs.addWidget(QLabel("B 목표:"))
        row_langs.addWidget(self._combo_b)

        row_region = QHBoxLayout()
        row_region.addWidget(self._btn_region_a)
        row_region.addWidget(self._btn_region_b)

        row_translate = QHBoxLayout()
        row_translate.addWidget(self._btn_translate_a)
        row_translate.addWidget(self._btn_translate_b)

        layout = QVBoxLayout(self)
        layout.addLayout(row_region)
        layout.addLayout(row_translate)
        layout.addLayout(row_langs)

        self._status = QLabel(
            "A/B 각각 영역 지정 후 해당「번역 실행」을 누르세요. "
            "단축키: A → Ctrl+Enter, B → Ctrl+Shift+Enter"
        )
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self.resize(480, 200)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._win_a.show()
        self._win_b.show()

    def _open_region_selector(self, key: str) -> None:
        self._region_target_key = key
        self.selector = RegionSelector()
        self.selector.region_selected.connect(self._on_region_selected)
        self.selector.showFullScreen()

    def _on_region_selected(self, left: int, top: int, right: int, bottom: int) -> None:
        tag = self._region_target_key
        coords = (left, top, right, bottom)
        if tag == "a":
            self.capture_coords_a = coords
            label = "A"
        else:
            self.capture_coords_b = coords
            label = "B"
        self._status.setText(
            f"{label} 캡처 영역: ({left}, {top}) — ({right}, {bottom})"
        )

    def _run_translation_for(self, window_key: str) -> None:
        if window_key == "a":
            coords = self.capture_coords_a
            lang = self._combo_a.currentData()
            win = self._win_a
            side = "A"
        else:
            coords = self.capture_coords_b
            lang = self._combo_b.currentData()
            win = self._win_b
            side = "B"

        if coords is None:
            QMessageBox.warning(
                self,
                "영역 없음",
                f"먼저「영역 지정 {side}」으로 해당 캡처 영역을 설정하세요.",
            )
            return
        if window_key in self._pending_keys:
            self._status.setText(f"{side} 번역이 이미 진행 중입니다.")
            return

        left, top, right, bottom = coords
        try:
            shot = ImageGrab.grab(bbox=(left, top, right, bottom))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "캡처 실패", str(exc))
            return

        try:
            ocr_text = pytesseract.image_to_string(shot).strip()
        except Exception as exc:  # noqa: BLE001
            extra = _ocr_access_denied_hint(exc)
            QMessageBox.critical(
                self,
                "OCR 실패",
                f"Tesseract 실행 오류: {exc}\n\n"
                "설치 및 PATH 등록을 확인하거나, 환경 변수 TESSERACT_CMD 로 "
                "tesseract.exe 전체 경로를 지정하세요."
                + extra,
            )
            return

        if not ocr_text:
            self._status.setText(
                f"{side}: OCR 결과가 비었습니다. 영역·언어·해상도를 확인하세요."
            )
            win.setText("")
            return

        self._pending_keys.add(window_key)
        win.setText("번역 중…")
        self._status.setText(f"{side} 번역 요청 중…")

        self._pool.start(
            TranslateRunnable(ocr_text, lang, window_key, self._translator_signals)
        )

    def _on_translation_finished(self, window_key: str, text: str) -> None:
        if window_key == "a":
            self._win_a.setText(text)
            side = "A"
        else:
            self._win_b.setText(text)
            side = "B"
        self._pending_keys.discard(window_key)
        self._status.setText(f"{side} 번역 완료.")


def main() -> None:
    app = QApplication(sys.argv)
    configure_tesseract()
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
