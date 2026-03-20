import re
import markdown as md_lib
from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QComboBox,
    QLabel,
    QTextBrowser,
    QWidget,
)

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)]) ")
_FENCE_RE = re.compile(r"^(`{3}|~{3})")
_DETAILS_TAG_RE = re.compile(r"<(details)(\s[^>]*)?>", re.IGNORECASE)
# article, aside, blockquote, body, colgroup, details, div, dl, fieldset, figcaption, figure, footer, form, group,
# header, hgroup, hr, iframe, main, map, menu, nav, noscript, object, ol, output, progress, section, table, tbody, tfoot, thead, tr, ul and video,


def _inject_markdown_attr(text: str) -> str:
    """Add markdown="1" to <details> tags so md_in_html processes their content."""

    def _add_attr(m):
        attrs = m.group(2) or ""
        if "markdown=" in attrs.lower():
            return m.group(0)
        return f'<details{attrs} markdown="1">'

    return _DETAILS_TAG_RE.sub(_add_attr, text)


def _normalize_md_for_parser(text: str) -> str:
    """Ensure blank lines around list blocks so Python's markdown parser
    treats them as lists rather than paragraph text.

    GitHub's renderer is lenient; Python's markdown library requires:
      - a blank line before a list that follows non-blank, non-list content
      - a blank line after a list that is followed by non-blank, non-list content
        (otherwise the trailing text is absorbed into the last list item)

    Fenced code blocks are passed through unchanged.
    """
    lines = text.split("\n")
    out = []
    in_fence = False

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        if out:
            prev = out[-1]
            prev_blank = not prev.strip()
            cur_blank = not line.strip()
            cur_is_list = bool(_LIST_ITEM_RE.match(line))
            prev_is_list = bool(_LIST_ITEM_RE.match(prev))

            # blank line needed before a list that follows non-blank, non-list content
            if not prev_blank and cur_is_list and not prev_is_list:
                out.append("")
            # blank line needed after a list when followed by non-blank, non-list content
            elif not prev_blank and not cur_blank and prev_is_list and not cur_is_list:
                out.append("")

        out.append(line)

    return "\n".join(out)


def _make_changelog_css(bg: str, fg: str, border: str) -> str:
    """Generate HTML/CSS adapted to the given tooltip palette colours."""
    r, g, b = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
    is_dark = (r * 0.299 + g * 0.587 + b * 0.114) < 128
    code_bg = "#3a3a6b" if is_dark else "#f0f0f0"
    code_border = "#5a5a9b" if is_dark else "#e0e0e0"
    secondary = "#b0b0d0" if is_dark else "#57606a"
    hr_color = "#4a4a7b" if is_dark else "#d0d7de"
    link_color = "#8ab4f8" if is_dark else "#0969da"
    return f"""
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 20px; color: {fg}; background-color: {bg}; line-height: 1.0; margin: 10px 16px;
}}
h2 {{
    font-size: 22px; font-weight: 600; color: {fg};
    border-bottom: 1px solid {hr_color}; padding-bottom: 8px; margin: 12px 0 6px;
}}
h3 {{ font-size: 20px; font-weight: 600; margin: 10px 0 5px; }}
h4 {{ font-size: 19px; font-weight: 600; margin: 8px 0 4px; color: {secondary}; }}
p  {{ margin: 6px 0; }}
code {{
    background-color: {code_bg}; border: 1px solid {code_border};
    padding: 1px 5px; font-family: Consolas, 'Courier New', monospace; font-size: 18px;
}}
pre {{
    background-color: {code_bg}; border: 1px solid {code_border};
    padding: 10px 14px; font-family: Consolas, 'Courier New', monospace; font-size: 18px;
}}
pre code {{ background: none; border: none; padding: 0; }}
ul {{ padding-left: 24px; margin: 5px 0; }}
ol  {{ padding-left: 24px; margin: 5px 0; }}
li  {{ margin: 3px 0; }}
hr  {{ border-top: 1px solid {hr_color}; margin: 16px 0; }}
a   {{ color: {link_color}; }}
blockquote {{
    border-left: 3px solid {hr_color}; padding: 4px 14px; color: {secondary}; margin: 8px 0;
}}
"""



class AssetSelectionDialog(QDialog):
    def __init__(self, assets, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Asset")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        self.asset_combo = QComboBox()
        for asset in assets:
            self.asset_combo.addItem(f"{asset['name']} ({asset['size'] / (1024 * 1024):.2f} MB)", asset)
        layout.addWidget(QLabel("Select an asset to download:"))
        layout.addWidget(self.asset_combo)

        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)

    def get_selected_asset(self):
        return self.asset_combo.currentData()


class ChangelogPanel(QWidget):
    def __init__(self, repo_url, changelogs, parent=None):
        """changelogs: list of (tag, title, body) tuples, newest-first"""
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)

        repo_name = repo_url.rstrip("/").split("/")[-1]
        screen = QApplication.primaryScreen().availableGeometry()
        self._max_height = int(screen.height() * 2 / 3)

        palette = QApplication.palette()
        bg = palette.color(QPalette.Window).name()
        fg = palette.color(QPalette.WindowText).name()
        border = palette.color(QPalette.Mid).name()

        # Build HTML content
        html_sections = []
        for tag, title, body in changelogs:
            heading = title if title and title != tag else tag
            clean_body = body.replace("\r\n", "\n").replace("\r", "\n")
            clean_body = _inject_markdown_attr(clean_body)
            raw = f"## {heading}\n\n{clean_body.strip() or '*No changelog provided.*'}"
            normalized = _normalize_md_for_parser(raw)
            html_sections.append(md_lib.markdown(normalized, extensions=["md_in_html", "sane_lists", "tables", "fenced_code", "nl2br"]))

        css = _make_changelog_css(bg, fg, border)
        full_html = f"<html><head><style>{css}</style></head><body>{'<hr>'.join(html_sections)}</body></html>"

        # Accent strip styling (same pattern as Toast)
        accent = "#2563EB"
        self.setObjectName("ChangelogOuter")
        self.setStyleSheet(f"""
            #ChangelogOuter  {{ background-color: {accent}; border: none; }}
            #ChangelogContent {{ background-color: {bg}; border: none; }}
            #ChangelogTitle  {{ background: transparent; color: {fg};
                                font-size: 15px; font-weight: bold; }}
            #CloseBtn {{ background: transparent; color: {fg}; border: none;
                         font-size: 16px; font-weight: bold; padding: 0 6px; }}
            #CloseBtn:hover {{ background-color: rgba(220,38,38,160); color: white; }}
        """)

        # Title bar
        title_label = QLabel(f"Changelog — {repo_name}")
        title_label.setObjectName("ChangelogTitle")
        close_btn = QPushButton("✕")
        close_btn.setObjectName("CloseBtn")
        close_btn.setFixedSize(32, 32)
        close_btn.clicked.connect(self._dismiss)

        title_bar = QHBoxLayout()
        title_bar.setContentsMargins(0, 0, 0, 0)
        title_bar.addWidget(title_label)
        title_bar.addStretch()
        title_bar.addWidget(close_btn)

        # Browser
        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(True)
        self._browser.setStyleSheet(f"QTextBrowser {{ background-color: {bg}; color: {fg}; border: none; }}")
        self._browser.setHtml(full_html)

        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(12, 10, 12, 12)
        content_layout.setSpacing(8)
        content_layout.addLayout(title_bar)
        content_layout.addWidget(self._browser)

        content = QWidget()
        content.setObjectName("ChangelogContent")
        content.setLayout(content_layout)

        outer_layout = QHBoxLayout(self)
        outer_layout.setContentsMargins(4, 0, 0, 0)
        outer_layout.setSpacing(0)
        outer_layout.addWidget(content)

        # Fade animation
        self._fading_out = False
        self._fade = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(250)
        self._fade.setEasingCurve(QEasingCurve.InOutQuad)
        self._fade.finished.connect(self._on_fade_finished)

    def show_panel(self):
        self.setWindowOpacity(0.0)
        self.show()
        self._fit_and_position()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def _fit_and_position(self):
        screen = QApplication.primaryScreen().availableGeometry()

        # Width = half the parent app window, falling back to half the screen
        if self.parent():
            panel_width = self.parent().width() // 2
        else:
            panel_width = screen.width() // 2
        self.setFixedWidth(panel_width)

        # Force document layout at the now-known viewport width, then measure height
        doc = self._browser.document()
        doc.setTextWidth(self._browser.viewport().width())
        content_h = int(doc.size().height())

        extra_h = self.height() - self._browser.height()
        total_h = min(content_h + extra_h + 4, self._max_height)
        self.setFixedHeight(total_h)

        # X: centered over parent window (or screen); Y: 10% from screen top
        if self.parent():
            geo = self.parent().geometry()
            cx = geo.x() + geo.width() // 2
        else:
            cx = screen.x() + screen.width() // 2

        y = screen.y() + int(screen.height() * 0.1)
        self.move(cx - self.width() // 2, y)
        self.raise_()

    def _dismiss(self):
        self._fade.stop()
        self._fading_out = True
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._dismiss()
        else:
            super().keyPressEvent(event)

    def _on_fade_finished(self):
        if self._fading_out:
            self.close()
