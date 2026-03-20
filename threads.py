import os
import tempfile
import shutil
import zipfile
import tarfile
import time
import uuid

import requests
from PySide6.QtCore import QThread, Signal


class DownloadThread(QThread):
    progress = Signal(int, str, str)  # percent, downloaded/total, speed
    download_done = Signal(bool, str)  # renamed: QThread already defines finished()
    error = Signal(str)

    def __init__(self, url, file_type, download_folder, unpack_subfolder=False):
        super().__init__()
        self.url = url
        self.file_type = file_type
        self.download_folder = download_folder
        self.unpack_subfolder = unpack_subfolder
        self._is_running = True

    def run(self):
        temp_file = os.path.join(tempfile.gettempdir(), f"release.{self.file_type}")
        staging_dir = os.path.join(tempfile.gettempdir(), f"gh-updater-{uuid.uuid4().hex[:8]}")
        try:
            response = requests.get(self.url, stream=True, timeout=30)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0
            start_time = time.time()
            last_update = 0

            with open(temp_file, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if not self._is_running:
                        raise RuntimeError("Download canceled by user.")
                    f.write(chunk)
                    downloaded += len(chunk)

                    # Update progress every 0.1s
                    current_time = time.time()
                    if current_time - last_update > 0.1:
                        speed = downloaded / (current_time - start_time) if (current_time - start_time) > 0 else 0
                        percent = int((downloaded / total_size) * 100) if total_size > 0 else 0
                        self.progress.emit(
                            percent,
                            f"{downloaded / (1024 * 1024):.2f} / {total_size / (1024 * 1024):.2f} MB" if total_size > 0 else "Unknown size",
                            f"{speed / (1024 * 1024):.2f} MB/s",
                        )
                        last_update = current_time

            is_archive = self.file_type in ("zip", "tar.gz", "gz", "7z")
            if is_archive:
                # Extract into isolated staging dir
                os.makedirs(staging_dir, exist_ok=True)
                if self.file_type == "zip":
                    with zipfile.ZipFile(temp_file) as zip_ref:
                        zip_ref.extractall(staging_dir)
                elif self.file_type in ("tar.gz", "gz"):
                    with tarfile.open(temp_file, "r:gz") as tar_ref:
                        tar_ref.extractall(staging_dir)

                # If "Unpack Subfolder" is set and the archive unpacks to a single folder,
                # treat that folder's contents as the source instead of the archive root.
                top_level = os.listdir(staging_dir)
                if self.unpack_subfolder and len(top_level) == 1 and os.path.isdir(os.path.join(staging_dir, top_level[0])):
                    src_dir = os.path.join(staging_dir, top_level[0])
                else:
                    src_dir = staging_dir

                os.makedirs(self.download_folder, exist_ok=True)
                for item in os.listdir(src_dir):
                    src = os.path.join(src_dir, item)
                    dst = os.path.join(self.download_folder, item)
                    if os.path.isdir(src):
                        if os.path.exists(dst):
                            shutil.rmtree(dst)
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
            else:
                # Non-archive (exe, dmg, …): move directly to destination
                os.makedirs(self.download_folder, exist_ok=True)
                shutil.move(temp_file, os.path.join(self.download_folder, os.path.basename(temp_file)))

            self.download_done.emit(True, "Update completed successfully!")
        except Exception as e:
            self.download_done.emit(False, f"Update failed: {str(e)}")
        finally:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            shutil.rmtree(staging_dir, ignore_errors=True)

    def stop(self):
        self._is_running = False


class FetchLatestThread(QThread):
    result = Signal(int, str)  # row index, latest release tag
    fetch_done = Signal()  # renamed: QThread already defines finished()
    error = Signal(str)  # human-readable error message

    def __init__(self, repos):
        super().__init__()
        self.repos = repos  # list of (row, repo_url)

    def run(self):
        for row, repo_url in self.repos:
            parts = repo_url.strip("/").split("/")
            if len(parts) < 2:
                self.result.emit(row, "")
                continue
            owner, repo = parts[-2], parts[-1]
            api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
            try:
                response = requests.get(api_url, timeout=10)
                response.raise_for_status()
                self.result.emit(row, response.json().get("tag_name", ""))
            except requests.HTTPError as e:
                self.result.emit(row, "")
                if e.response is not None and e.response.status_code in (403, 429):
                    self.error.emit("GitHub API rate limit exceeded — wait a minute or add a token.")
                    break  # no point hitting the API for remaining repos
            except Exception:
                self.result.emit(row, "")
        self.fetch_done.emit()


class FetchChangelogThread(QThread):
    result = Signal(str, str, str)  # tag, release title, body
    fetch_done = Signal()

    def __init__(self, repo_url, tags):
        super().__init__()
        self.repo_url = repo_url
        self.tags = tags  # list of tag strings to fetch, newest-first

    def run(self):
        parts = self.repo_url.strip("/").split("/")
        if len(parts) < 2:
            self.fetch_done.emit()
            return
        owner, repo = parts[-2], parts[-1]
        for tag in self.tags:
            if tag == "latest":
                url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
            else:
                url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                data = r.json()
                self.result.emit(
                    data.get("tag_name", tag),
                    data.get("name", tag),
                    data.get("body", ""),
                )
            except Exception:
                self.result.emit(tag, tag, "")
        self.fetch_done.emit()
