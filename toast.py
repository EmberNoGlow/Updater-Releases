from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout, QLabel, QStyle


class Toast(QWidget):
    def __init__(self, parent=None, message="", message_type="info", duration=3000):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.duration = duration

        accents = {
            "info": "#2563EB",
            "success": "#16A34A",
            "error": "#DC2626",
        }
        accent = accents.get(message_type, accents["info"])

        palette = QApplication.palette()
        bg = palette.color(QPalette.Window).name()
        fg = palette.color(QPalette.WindowText).name()

        icons = {
            "info": QStyle.SP_MessageBoxInformation,
            "success": QStyle.SP_DialogApplyButton,
            "error": QStyle.SP_MessageBoxCritical,
        }
        icon_label = QLabel()
        icon_label.setPixmap(QApplication.style().standardIcon(icons.get(message_type, icons["info"])).pixmap(36, 36))
        icon_label.setFixedSize(36, 36)
        icon_label.setAlignment(Qt.AlignVCenter | Qt.AlignHCenter)

        label = QLabel(message)
        label.setWordWrap(True)
        label.setMinimumWidth(300)
        label.setMaximumWidth(500)

        # Content area sits inside the outer widget, offset 4px from the left.
        # The outer widget's background (accent color) bleeds through that 4px gap,
        # creating an outside left border without any QSS border tricks.
        self.setObjectName("ToastOuter")
        self.setStyleSheet(f"""
            #ToastOuter  {{ background-color: {accent}; border: none; }}
            #ToastContent {{ background-color: {bg};    border: none; }}
            #ToastContent QLabel {{ background: transparent; color: {fg};
                                    font-size: 18px; font-weight: bold; }}
        """)

        content = QWidget()
        content.setObjectName("ToastContent")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(20, 14, 20, 14)
        content_layout.setSpacing(16)
        content_layout.addWidget(icon_label)
        content_layout.addWidget(label)

        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(4, 0, 0, 0)  # 4px left = accent strip
        outer_layout.setSpacing(0)
        outer_layout.addWidget(content)

        self.setMinimumHeight(100)
        self.adjustSize()

        self._fading_out = False
        self._fade = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(250)
        self._fade.setEasingCurve(QEasingCurve.InOutQuad)
        self._fade.finished.connect(self._on_fade_finished)

    def show_toast(self):
        if self.parent():
            geo = self.parent().geometry()
            x = geo.x() + (geo.width() - self.width()) // 2
            y = geo.y() + geo.height() - self.height() - 60
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            x = (screen.width() - self.width()) // 2
            y = screen.height() - self.height() - 60

        self.move(x, y)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()

        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

        QTimer.singleShot(self.duration, self._dismiss)

    def _dismiss(self):
        self._fade.stop()
        self._fading_out = True
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_finished(self):
        if self._fading_out:
            self.close()
            self.deleteLater()
