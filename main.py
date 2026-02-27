import os
import json
import datetime
import re
import uuid
from enum import IntEnum

import requests
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QComboBox,
    QLabel,
    QInputDialog,
    QFileDialog,
    QProgressDialog,
    QHBoxLayout,
    QStyleFactory,
    QHeaderView,
    QStyle,
)
from PySide6.QtCore import Qt, QSize, QTimer

from toast import Toast
from threads import DownloadThread, FetchLatestThread, FetchChangelogThread
from dialogs import ChangelogPanel


CACHE_TIMEOUT_SECONDS = 300  # How long release/asset lists are considered fresh
AUTOSAVE_DEBOUNCE_SECONDS = 5  # Idle time after a change before auto-saving
MAX_CHANGELOG_RELEASES = 10  # Max releases to fetch and display in changelog popup


class Col(IntEnum):
    URL = 0
    FOLDER = 1
    REGEX = 2
    UNPACK = 3
    LATEST_RELEASE = 4
    DOWNLOADED_VERSION = 5
    DOWNLOADED_RELEASE = 6
    LAST_UPDATED = 7
    ACTION = 8


def show_toast_message(parent, message, message_type="info"):
    """Helper function to show toast messages"""
    toast = Toast(parent, message, message_type)
    toast.show_toast()


def _is_rate_limited(exc):
    """Return True if the requests exception is a GitHub 403/429 rate limit response."""
    return isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in (403, 429)


class AutoUpdater(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GitHub Auto Updater")
        self.setGeometry(100, 100, 1800, 700)

        # Main widget and layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Top bar: Add Repository on the left, Save/Load + theme toggles on the right
        top_bar = QHBoxLayout()

        self.add_repo_btn = QPushButton(" Add Repository")
        self.add_repo_btn.clicked.connect(self.add_repository)
        top_bar.addWidget(self.add_repo_btn)

        top_bar.addStretch()

        self.save_btn = QPushButton(" Save List")
        self.save_btn.clicked.connect(self.save_repositories)
        top_bar.addWidget(self.save_btn)

        self.load_btn = QPushButton(" Load List")
        self.load_btn.clicked.connect(self.load_repositories)
        top_bar.addWidget(self.load_btn)

        self.light_btn = QPushButton("🔆  Light")
        self.light_btn.setCheckable(True)
        self.light_btn.clicked.connect(lambda: self.change_theme("Light"))
        top_bar.addWidget(self.light_btn)

        self.dark_btn = QPushButton("⏾  Dark")
        self.dark_btn.setCheckable(True)
        self.dark_btn.clicked.connect(lambda: self.change_theme("Dark"))
        top_bar.addWidget(self.dark_btn)

        layout.addLayout(top_bar)

        # Table for repositories
        _col_labels = [
            "Repository URL",
            "Download Folder",
            "Release Asset Regex",
            "Subfolder Unpack",
            "Latest Release",
            "Downloaded Version",
            "Downloaded Release Asset",
            "Last Updated",
            "Action",
        ]
        self.table = QTableWidget(0, len(Col))
        self.table.setHorizontalHeaderLabels(_col_labels)

        # URL stretches to fill spare space; Action is fixed; rest are user-resizable
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(Col.URL, QHeaderView.Stretch)
        header.setSectionResizeMode(Col.ACTION, QHeaderView.Fixed)
        _col_widths = {
            Col.FOLDER: 300,
            Col.REGEX: 200,
            Col.UNPACK: 60,
            Col.LATEST_RELEASE: 120,
            Col.DOWNLOADED_VERSION: 140,
            Col.DOWNLOADED_RELEASE: 210,
            Col.LAST_UPDATED: 165,
            Col.ACTION: 430,
        }
        for col, w in _col_widths.items():
            self.table.setColumnWidth(col, w)

        # Tooltips and left-alignment on every header cell
        for col, label in enumerate(_col_labels):
            item = self.table.horizontalHeaderItem(col)
            item.setToolTip(label)
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self.on_row_selected)
        layout.addWidget(self.table)

        # Release and asset dropdowns
        dropdowns_layout = QHBoxLayout()

        dropdowns_layout.addWidget(QLabel("Release:"))
        self.release_dropdown = QComboBox()
        self.release_dropdown.currentTextChanged.connect(self.on_release_changed)
        dropdowns_layout.addWidget(self.release_dropdown, stretch=1)

        dropdowns_layout.addWidget(QLabel("Asset:"))
        self.asset_dropdown = QComboBox()
        dropdowns_layout.addWidget(self.asset_dropdown, stretch=2)

        layout.addLayout(dropdowns_layout)

        action_btn_layout = QHBoxLayout()
        self.update_all_btn = QPushButton("📚 Update All")
        self.update_all_btn.clicked.connect(self.update_all)
        action_btn_layout.addWidget(self.update_all_btn)

        self.update_btn = QPushButton("⬇️ Update Selected")
        self.update_btn.clicked.connect(self.update_selected)
        action_btn_layout.addWidget(self.update_btn)

        self.fetch_latest_btn = QPushButton(" Fetch Latest Releases")
        self.fetch_latest_btn.clicked.connect(self.fetch_latest_releases)
        action_btn_layout.addWidget(self.fetch_latest_btn)
        layout.addLayout(action_btn_layout)

        # Button icons at 24×24
        _s = QApplication.style()
        _icon_size = QSize(24, 24)
        for btn, sp in [
            (self.add_repo_btn, QStyle.SP_FileDialogNewFolder),
            (self.save_btn, QStyle.SP_DialogSaveButton),
            (self.load_btn, QStyle.SP_DialogOpenButton),
            (self.fetch_latest_btn, QStyle.SP_BrowserReload),
        ]:
            btn.setIcon(_s.standardIcon(sp))
            btn.setIconSize(_icon_size)

        # Download thread and progress dialog
        self.download_thread = None
        self.fetch_latest_thread = None
        self.changelog_thread = None
        self._changelog_panel = None
        self.progress_dialog = None
        self.selected_row = None
        self._pending_release_name = ""
        self._pending_release_tag = ""
        self._pending_row_items = None  # direct item refs, immune to sort reordering
        self._cache = {}  # {repo_url: {"releases": [...], "releases_ts": datetime, "assets": {tag: {"list": [...], "ts": datetime}}}}
        self._update_all_mode = False
        self._update_queue = []  # rows waiting to be downloaded in update-all run
        self._loading_repositories = False

        # Load saved repositories (if any)
        if os.path.exists("repositories.json"):
            self.load_repositories()

        # Load default theme
        self.change_theme("Light")

        # Auto-save wiring (connected after initial load so startup doesn't trigger a save)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self.save_repositories)
        self.table.itemChanged.connect(self._on_saveable_item_changed)
        self.table.model().rowsRemoved.connect(lambda *_: self._schedule_autosave())

    def on_row_selected(self):
        if self.download_thread is not None or self._loading_repositories:
            return
        selected = self.table.selectedItems()
        if not selected:
            return
        self.update_repository(selected[0].row())

    def change_theme(self, theme_name):
        self.light_btn.setChecked(theme_name == "Light")
        self.dark_btn.setChecked(theme_name == "Dark")
        qss_file = f"{theme_name.lower()}.qss"
        if os.path.exists(qss_file):
            with open(qss_file, "r", encoding="utf-8") as f:
                self.setStyleSheet(f.read())
        else:
            # Fallback to default theme if QSS file not found
            QApplication.setStyle(QStyleFactory.create("Fusion"))

    def _row_of_widget(self, w):
        """Find the table row containing widget w using its visual position."""
        vp = self.table.viewport()
        pos = vp.mapFromGlobal(w.mapToGlobal(w.rect().center()))
        return self.table.rowAt(pos.y())

    def _make_action_widget(self):
        action_widget = QWidget()
        action_layout = QHBoxLayout()
        action_layout.setContentsMargins(8, 0, 8, 0)
        update_btn = QPushButton(" ➡️ Fetch ")
        update_btn.clicked.connect(lambda: self.update_repository(self._row_of_widget(update_btn)))
        action_layout.addWidget(update_btn)
        changelog_btn = QPushButton(" 📄 Changelog ")
        changelog_btn.setToolTip("Show changelogs between downloaded version and latest")
        changelog_btn.clicked.connect(lambda: self._show_changelog(self._row_of_widget(changelog_btn)))
        action_layout.addWidget(changelog_btn)
        delete_btn = QPushButton(" 🗑️ Delete ")
        delete_btn.clicked.connect(lambda: self.table.removeRow(self._row_of_widget(delete_btn)))
        action_layout.addWidget(delete_btn)
        action_layout.addStretch()
        action_widget.setLayout(action_layout)
        return action_widget

    def _schedule_autosave(self):
        self._autosave_timer.start(AUTOSAVE_DEBOUNCE_SECONDS * 1000)

    def _on_saveable_item_changed(self, item):
        if item.column() != Col.LATEST_RELEASE:
            self._schedule_autosave()
        if item.column() == Col.REGEX and item.row() == self.selected_row:
            self.on_release_changed(self.release_dropdown.currentText())

    def _fit_content_columns(self):
        for col in (Col.FOLDER, Col.REGEX, Col.DOWNLOADED_RELEASE, Col.LAST_UPDATED):
            self.table.resizeColumnToContents(col)

    def _make_readonly_item(self, text=""):
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    def add_repository(self):
        repo_url, ok1 = QInputDialog.getText(self, "Repository URL", "Enter GitHub repo URL (e.g., https://github.com/godotengine/godot):")
        if not ok1:
            return

        # Validate URL
        if "github.com" not in repo_url:
            show_toast_message(self, "Invalid GitHub URL.", "error")
            return

        download_folder = QFileDialog.getExistingDirectory(self, "Select Download Folder")
        if not download_folder:
            return

        # Add to table (disable sorting during insertion to avoid mid-insert reorder)
        self.table.setSortingEnabled(False)
        row = self.table.rowCount()
        self.table.insertRow(row)

        url_item = QTableWidgetItem(repo_url)
        url_item.setData(Qt.UserRole, str(uuid.uuid4()))
        self.table.setItem(row, Col.URL, url_item)
        self.table.setItem(row, Col.FOLDER, QTableWidgetItem(download_folder))
        self.table.setItem(row, Col.REGEX, QTableWidgetItem(""))

        unpack_item = QTableWidgetItem()
        unpack_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        unpack_item.setCheckState(Qt.Unchecked)
        self.table.setItem(row, Col.UNPACK, unpack_item)

        for col in (Col.LAST_UPDATED, Col.DOWNLOADED_RELEASE, Col.DOWNLOADED_VERSION, Col.LATEST_RELEASE):
            self.table.setItem(row, col, self._make_readonly_item())

        self.table.setCellWidget(row, Col.ACTION, self._make_action_widget())
        self.table.setSortingEnabled(True)

    def _cache_age(self, ts):
        return (datetime.datetime.now() - ts).total_seconds()

    def fetch_releases(self, repo_url):
        entry = self._cache.get(repo_url, {})
        if "releases" in entry and self._cache_age(entry["releases_ts"]) < CACHE_TIMEOUT_SECONDS:
            return entry["releases"]

        parts = repo_url.strip("/").split("/")
        owner, repo = parts[-2], parts[-1]
        api_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
        try:
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            releases = ["latest"] + [r["tag_name"] for r in response.json()]
            self._cache.setdefault(repo_url, {}).update({"releases": releases, "releases_ts": datetime.datetime.now()})
            return releases
        except requests.RequestException as e:
            msg = (
                "GitHub API rate limit exceeded — wait a minute or add a token."
                if _is_rate_limited(e)
                else f"Failed to fetch releases: {e}"
            )
            show_toast_message(self, msg, "error")
            return entry.get("releases", ["latest"])  # serve stale cache on error

    def fetch_assets(self, repo_url, release_tag):
        asset_entry = self._cache.get(repo_url, {}).get("assets", {}).get(release_tag, {})
        if asset_entry and self._cache_age(asset_entry["ts"]) < CACHE_TIMEOUT_SECONDS:
            return asset_entry["list"]

        parts = repo_url.strip("/").split("/")
        owner, repo = parts[-2], parts[-1]
        if release_tag == "latest":
            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        else:
            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{release_tag}"
        try:
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()
            data = response.json()
            assets = data.get("assets", [])
            self._cache.setdefault(repo_url, {}).setdefault("assets", {})[release_tag] = {
                "list": assets,
                "resolved_tag": data.get("tag_name", release_tag),
                "ts": datetime.datetime.now(),
            }
            return assets
        except requests.RequestException as e:
            msg = (
                "GitHub API rate limit exceeded — wait a minute or add a token." if _is_rate_limited(e) else f"Failed to fetch assets: {e}"
            )
            show_toast_message(self, msg, "error")
            return asset_entry.get("list", [])  # serve stale cache on error

    def update_repository(self, row):
        if row < 0:
            return
        self.selected_row = row
        repo_url = self.table.item(row, Col.URL).text()
        releases = self.fetch_releases(repo_url)

        self.release_dropdown.clear()
        self.release_dropdown.addItems(releases)

    def on_release_changed(self, release_tag):
        self.asset_dropdown.clear()
        if not release_tag or self.selected_row is None:
            return
        repo_url = self.table.item(self.selected_row, Col.URL).text()
        assets = self.fetch_assets(repo_url, release_tag)

        regex = self.table.item(self.selected_row, Col.REGEX).text().strip()
        if regex:
            try:
                matched = [a for a in assets if re.search(regex, a["name"])]
            except re.error:
                matched = []
            display_assets = matched if matched else assets
        else:
            display_assets = assets

        for asset in display_assets:
            label = f"{asset['name']} ({asset['size'] / (1024 * 1024):.2f} MB)"
            self.asset_dropdown.addItem(label, asset)

    def update_selected(self):
        selected_rows = sorted(set(idx.row() for idx in self.table.selectedIndexes()))
        if not selected_rows:
            return

        if len(selected_rows) > 1:
            # Multiple rows: queue all using auto-pick (same logic as Update All)
            self._update_all_mode = True
            self.update_all_btn.setEnabled(False)
            self._update_queue = selected_rows
            self._process_update_queue()
            return

        # Single row: use the exact release/asset shown in the dropdowns
        row = selected_rows[0]
        download_folder = self.table.item(row, Col.FOLDER).text()

        selected_asset = self.asset_dropdown.currentData()
        if not selected_asset:
            show_toast_message(self, "No asset selected.", "error")
            return

        download_url = selected_asset["browser_download_url"]
        file_name = selected_asset["name"]
        file_type = os.path.splitext(file_name)[1][1:]  # e.g., "zip", "exe"
        self._pending_release_name = file_name
        release_tag = self.release_dropdown.currentText()
        repo_url = self.table.item(row, Col.URL).text()
        resolved = self._cache.get(repo_url, {}).get("assets", {}).get(release_tag, {}).get("resolved_tag", release_tag)
        self._pending_release_tag = resolved
        self._pending_row_items = {
            "last_updated": self.table.item(row, Col.LAST_UPDATED),
            "downloaded_release": self.table.item(row, Col.DOWNLOADED_RELEASE),
            "downloaded_version": self.table.item(row, Col.DOWNLOADED_VERSION),
            "regex": self.table.item(row, Col.REGEX),
        }

        # Show progress dialog
        self.progress_dialog = QProgressDialog("Starting download...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowTitle("Downloading...")
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setValue(0)
        self.progress_dialog.show()

        # Start download in a thread
        unpack_subfolder = self.table.item(row, Col.UNPACK).checkState() == Qt.Checked
        self.download_thread = DownloadThread(download_url, file_type, download_folder, unpack_subfolder)
        self.download_thread.progress.connect(self.update_progress)
        self.download_thread.download_done.connect(self.download_finished)
        self.download_thread.error.connect(self.download_error)
        _t = self.download_thread
        self.download_thread.finished.connect(lambda: self._cleanup_thread(_t))
        self.progress_dialog.canceled.connect(self._on_download_canceled)
        self.download_thread.start()

    def _cleanup_thread(self, thread):
        if thread is self.download_thread:
            self.download_thread = None
        thread.deleteLater()

    def _on_download_canceled(self):
        if self.download_thread:
            self.download_thread.stop()
        self._update_all_mode = False
        self._update_queue.clear()
        self.update_all_btn.setEnabled(True)

    def update_progress(self, percent, downloaded_total, speed):
        if self.progress_dialog is not None:
            self.progress_dialog.setValue(percent)
            self.progress_dialog.setLabelText(f"Downloading... {downloaded_total} | Speed: {speed}")

    def _dismiss_progress_dialog(self):
        if self.progress_dialog is not None:
            self.progress_dialog.hide()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None

    def download_finished(self, success, message):
        self._dismiss_progress_dialog()
        if success:
            if not self._update_all_mode:
                show_toast_message(self, message, "success")
            if self._pending_row_items is not None:
                now = datetime.datetime.now().astimezone()
                self._pending_row_items["last_updated"].setText(now.strftime("%Y-%m-%d %H:%M:%S"))
                self._pending_row_items["last_updated"].setData(Qt.UserRole, now.isoformat())
                self._pending_row_items["downloaded_release"].setText(self._pending_release_name)
                self._pending_row_items["downloaded_version"].setText(self._pending_release_tag)
                if not self._pending_row_items["regex"].text().strip():
                    self._pending_row_items["regex"].setText(re.escape(self._pending_release_name))
        else:
            show_toast_message(self, message, "error")
        if self._update_all_mode:
            self._process_update_queue()

    def download_error(self, message):
        self._dismiss_progress_dialog()
        show_toast_message(self, message, "error")

    def fetch_latest_releases(self):
        if self.table.rowCount() == 0:
            return
        repos = [(row, self.table.item(row, Col.URL).text()) for row in range(self.table.rowCount())]
        # Clear current values and disable buttons while fetching
        for row, _ in repos:
            self.table.item(row, Col.LATEST_RELEASE).setText("…")
        self.fetch_latest_btn.setEnabled(False)
        self.update_all_btn.setEnabled(False)

        self.fetch_latest_thread = FetchLatestThread(repos)
        self.fetch_latest_thread.result.connect(self.on_fetch_latest_result)
        self.fetch_latest_thread.fetch_done.connect(self.on_fetch_latest_finished)
        self.fetch_latest_thread.error.connect(lambda msg: show_toast_message(self, msg, "error"))
        _ft = self.fetch_latest_thread
        self.fetch_latest_thread.finished.connect(lambda: self._cleanup_fetch_thread(_ft))
        self.fetch_latest_thread.start()

    def _cleanup_fetch_thread(self, thread):
        if thread is self.fetch_latest_thread:
            self.fetch_latest_thread = None
        thread.deleteLater()

    def on_fetch_latest_result(self, row, tag):
        if row < self.table.rowCount():
            self.table.item(row, Col.LATEST_RELEASE).setText(tag)

    def on_fetch_latest_finished(self):
        self.fetch_latest_btn.setEnabled(True)
        self.update_all_btn.setEnabled(True)
        if self._update_all_mode:
            self._start_update_all_queue()

    def update_all(self):
        if self.table.rowCount() == 0:
            return
        self._update_all_mode = True
        self.update_all_btn.setEnabled(False)
        self.fetch_latest_releases()  # on_fetch_latest_finished will call _start_update_all_queue

    def _start_update_all_queue(self):
        self._update_queue = []
        for row in range(self.table.rowCount()):
            downloaded = self.table.item(row, Col.DOWNLOADED_VERSION).text().strip()
            latest = self.table.item(row, Col.LATEST_RELEASE).text().strip()
            if not downloaded or not latest or downloaded != latest:
                self._update_queue.append(row)
        if not self._update_queue:
            self._update_all_mode = False
            show_toast_message(self, "All repositories are up to date.", "info")
            return
        self._process_update_queue()

    def _pick_asset(self, row):
        """Return the best asset for a row using regex, or first asset, or None."""
        repo_url = self.table.item(row, Col.URL).text()
        assets = self.fetch_assets(repo_url, "latest")
        if not assets:
            return None
        regex = self.table.item(row, Col.REGEX).text().strip()
        if regex:
            try:
                matched = [a for a in assets if re.search(regex, a["name"])]
            except re.error:
                matched = []
            return matched[0] if matched else assets[0]
        return assets[0]

    def _process_update_queue(self):
        """Start the download for the next row in the update queue."""
        if not self._update_queue:
            self._update_all_mode = False
            self.update_all_btn.setEnabled(True)
            show_toast_message(self, "Update All finished.", "success")
            return

        row = self._update_queue.pop(0)
        self.selected_row = row
        repo_url = self.table.item(row, Col.URL).text()
        download_folder = self.table.item(row, Col.FOLDER).text()
        unpack_subfolder = self.table.item(row, Col.UNPACK).checkState() == Qt.Checked

        asset = self._pick_asset(row)
        if asset is None:
            show_toast_message(self, f"No asset found for row {row + 1}, skipping.", "error")
            self._process_update_queue()
            return

        file_name = asset["name"]
        file_type = os.path.splitext(file_name)[1][1:]
        self._pending_release_name = file_name
        release_tag = "latest"
        resolved = self._cache.get(repo_url, {}).get("assets", {}).get(release_tag, {}).get("resolved_tag", release_tag)
        self._pending_release_tag = resolved
        self._pending_row_items = {
            "last_updated": self.table.item(row, Col.LAST_UPDATED),
            "downloaded_release": self.table.item(row, Col.DOWNLOADED_RELEASE),
            "downloaded_version": self.table.item(row, Col.DOWNLOADED_VERSION),
            "regex": self.table.item(row, Col.REGEX),
        }

        self.progress_dialog = QProgressDialog(f"Updating {repo_url.rstrip('/').split('/')[-1]}…", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowTitle("Downloading...")
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setValue(0)
        self.progress_dialog.show()

        self.download_thread = DownloadThread(asset["browser_download_url"], file_type, download_folder, unpack_subfolder)
        self.download_thread.progress.connect(self.update_progress)
        self.download_thread.download_done.connect(self.download_finished)
        self.download_thread.error.connect(self.download_error)
        _t = self.download_thread
        self.download_thread.finished.connect(lambda: self._cleanup_thread(_t))
        self.progress_dialog.canceled.connect(self._on_download_canceled)
        self.download_thread.start()

    def _show_changelog(self, row):
        """Fetch and display release changelogs between downloaded and latest versions."""
        repo_url = self.table.item(row, Col.URL).text()
        downloaded = self.table.item(row, Col.DOWNLOADED_VERSION).text().strip()

        releases = self.fetch_releases(repo_url)
        # releases[0] is the sentinel "latest"; the rest are real tags, newest-first
        tag_list = [r for r in releases if r != "latest"]

        if downloaded and downloaded in tag_list:
            # Only show releases newer than the downloaded version
            tags_to_show = tag_list[: tag_list.index(downloaded)]
        else:
            tags_to_show = tag_list

        tags_to_show = tags_to_show[:MAX_CHANGELOG_RELEASES]

        if not tags_to_show:
            # Already on the latest — show the changelog for the current release
            if tag_list:
                tags_to_show = [tag_list[0]]
            else:
                show_toast_message(self, "No changelog available.", "info")
                return

        changelog_cache = self._cache.setdefault(repo_url, {}).setdefault("changelogs", {})
        missing = [t for t in tags_to_show if t not in changelog_cache]

        if missing:
            if self.changelog_thread is not None:
                return  # already fetching
            self.changelog_thread = FetchChangelogThread(repo_url, missing)
            self.changelog_thread.result.connect(lambda tag, title, body: self._on_changelog_result(repo_url, tag, title, body))
            self.changelog_thread.fetch_done.connect(lambda: self._on_changelog_fetch_done(repo_url, tags_to_show))
            _ct = self.changelog_thread
            self.changelog_thread.finished.connect(lambda: self._cleanup_changelog_thread(_ct))
            self.changelog_thread.start()
        else:
            self._display_changelog(repo_url, tags_to_show)

    def _on_changelog_result(self, repo_url, tag, title, body):
        self._cache.setdefault(repo_url, {}).setdefault("changelogs", {})[tag] = {
            "title": title,
            "body": body,
            "ts": datetime.datetime.now(),
        }

    def _on_changelog_fetch_done(self, repo_url, tags_to_show):
        self._display_changelog(repo_url, tags_to_show)

    def _cleanup_changelog_thread(self, thread):
        if thread is self.changelog_thread:
            self.changelog_thread = None
        thread.deleteLater()

    def _display_changelog(self, repo_url, tags_to_show):
        changelog_cache = self._cache.get(repo_url, {}).get("changelogs", {})
        changelogs = []
        for tag in tags_to_show:
            entry = changelog_cache.get(tag, {})
            changelogs.append((tag, entry.get("title", tag), entry.get("body", "")))
        if self._changelog_panel is not None:
            try:
                self._changelog_panel.close()
            except RuntimeError:
                pass
            self._changelog_panel = None
        self._changelog_panel = ChangelogPanel(repo_url, changelogs, self)
        self._changelog_panel.destroyed.connect(lambda _=None: setattr(self, "_changelog_panel", None))
        self._changelog_panel.show_panel()

    def save_repositories(self):
        repositories = []
        for row in range(self.table.rowCount()):
            repositories.append(
                {
                    "id": self.table.item(row, Col.URL).data(Qt.UserRole) or str(uuid.uuid4()),
                    "repo_url": self.table.item(row, Col.URL).text(),
                    "download_folder": self.table.item(row, Col.FOLDER).text(),
                    "release_regex": self.table.item(row, Col.REGEX).text(),
                    "unpack_subfolder": self.table.item(row, Col.UNPACK).checkState() == Qt.Checked,
                    "last_updated": self.table.item(row, Col.LAST_UPDATED).data(Qt.UserRole) or "",
                    "downloaded_release": self.table.item(row, Col.DOWNLOADED_RELEASE).text(),
                    "downloaded_version": self.table.item(row, Col.DOWNLOADED_VERSION).text(),
                }
            )

        with open("repositories.json", "w", encoding="utf-8") as f:
            json.dump(repositories, f, indent=2)

        show_toast_message(self, "Repositories saved to repositories.json", "success")

    def load_repositories(self):
        try:
            with open("repositories.json", "r", encoding="utf-8") as f:
                repositories = json.load(f)

            self._loading_repositories = True
            self.table.blockSignals(True)
            self.table.setSortingEnabled(False)
            self.table.setRowCount(0)

            for repo in repositories:
                row = self.table.rowCount()
                self.table.insertRow(row)

                url_item = QTableWidgetItem(repo["repo_url"])
                url_item.setData(Qt.UserRole, repo.get("id") or str(uuid.uuid4()))
                self.table.setItem(row, Col.URL, url_item)
                self.table.setItem(row, Col.FOLDER, QTableWidgetItem(repo["download_folder"]))
                self.table.setItem(row, Col.REGEX, QTableWidgetItem(repo.get("release_regex", "")))

                unpack_item = QTableWidgetItem()
                unpack_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                unpack_item.setCheckState(Qt.Checked if repo.get("unpack_subfolder", False) else Qt.Unchecked)
                self.table.setItem(row, Col.UNPACK, unpack_item)

                last_updated_iso = repo.get("last_updated", "")
                last_updated_item = self._make_readonly_item()
                if last_updated_iso:
                    dt = datetime.datetime.fromisoformat(last_updated_iso).astimezone()
                    last_updated_item.setText(dt.strftime("%Y-%m-%d %H:%M:%S"))
                    last_updated_item.setData(Qt.UserRole, last_updated_iso)
                self.table.setItem(row, Col.LAST_UPDATED, last_updated_item)

                self.table.setItem(row, Col.DOWNLOADED_RELEASE, self._make_readonly_item(repo.get("downloaded_release", "")))
                self.table.setItem(row, Col.DOWNLOADED_VERSION, self._make_readonly_item(repo.get("downloaded_version", "")))
                self.table.setItem(row, Col.LATEST_RELEASE, self._make_readonly_item())
                self.table.setCellWidget(row, Col.ACTION, self._make_action_widget())

            self.table.setSortingEnabled(True)
            self.table.blockSignals(False)
            self._loading_repositories = False

            self._fit_content_columns()
            show_toast_message(self, "Repositories loaded from repositories.json", "success")
            self.fetch_latest_releases()
        except Exception as e:
            self.table.blockSignals(False)
            self._loading_repositories = False
            show_toast_message(self, f"Failed to load repositories: {e}", "error")


if __name__ == "__main__":
    import sys

    app = QApplication(sys.argv)
    window = AutoUpdater()
    window.show()
    sys.exit(app.exec())
