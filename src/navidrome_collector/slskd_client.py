"""REST API client for slskd (headless Soulseek daemon).

API docs: https://github.com/slskd/slskd/blob/master/docs/api.md
"""

import time
import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)

API_PREFIX = "/api/v0"


@dataclass
class SlskdFile:
    """Represents a file returned from slskd search."""
    filename: str
    size: int
    bitrate: int = 0
    duration: int = 0
    sample_rate: int = 0
    username: str = ""
    slot_free: bool = True
    queue_length: int = 0
    upload_speed: int = 0


@dataclass
class SlskdDownload:
    """Represents a download task in slskd."""
    id: str
    filename: str
    size: int
    bytes_downloaded: int
    state: str
    error: Optional[str] = None
    username: str = ""


class SlskdClient:
    """Thin client for slskd REST API."""

    def __init__(self, base_url: str = "http://127.0.0.1:5030", api_key: str | None = None, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if api_key:
            self._session.headers["X-API-Key"] = api_key

    # ── Status ─────────────────────────────────────────────

    def ping(self) -> bool:
        """Check if slskd is reachable."""
        try:
            resp = self._get("/server")
            return resp.ok
        except requests.RequestException:
            return False

    # ── Search ──────────────────────────────────────────────

    def search(self, query: str) -> list[SlskdFile]:
        """Search Soulseek network. Returns list of matching files."""
        resp = self._post("/searches", {"searchText": query})
        data = resp.json() if resp.content else {}
        search_id = data.get("id") if isinstance(data, dict) else None
        if not search_id:
            log.warning("Search returned no id")
            return []

        # Poll for results (search may be queued then complete)
        for i in range(30):  # wait up to ~60s
            time.sleep(2)
            # Check search state
            resp = self._get(f"/searches/{search_id}")
            if not resp.ok:
                continue
            state = resp.json().get("state", "")
            # Completed or ResponseLimitReached = results ready
            if "Completed" in state or "ResponseLimit" in state:
                resp = self._get(f"/searches/{search_id}/responses")
                if resp.ok:
                    results = resp.json()
                    if results:
                        return self._parse_files(results)
                return []  # no results
            # InProgress = still waiting
            if state == "InProgress" or state == "Queued":
                continue
            # Unknown state, try responses anyway
            resp = self._get(f"/searches/{search_id}/responses")
            if resp.ok and resp.json():
                return self._parse_files(resp.json())
            break

        log.warning("Search timed out: %s", query)
        return []

    # ── Downloads ───────────────────────────────────────────

    def enqueue(self, username: str, filename: str) -> str | None:
        """Enqueue a file for download. Returns download id or None."""
        payload = [{"filename": filename}]
        resp = self._post(f"/transfers/downloads/{username}", payload)
        if resp.status_code in (200, 201):
            data = resp.json() if resp.content else {}
            if isinstance(data, list) and data:
                return data[0].get("id") if isinstance(data[0], dict) else None
            if isinstance(data, dict):
                return data.get("id")
            return None
        if resp.status_code == 409:
            log.info("Download already queued/completed: %s", filename)
            return None
        log.warning("Enqueue failed (%d): %s", resp.status_code, resp.text[:200])
        return None

    def get_downloads(self, state: str | None = None) -> list[SlskdDownload]:
        """List downloads, optionally filtered by state."""
        resp = self._get("/transfers/downloads")
        data = resp.json() if resp.ok and resp.content else []
        if not isinstance(data, list):
            return []
        # Flatten the nested structure: username → directories → files
        downloads = []
        for user_entry in data:
            username = user_entry.get("username", "")
            for dir_entry in user_entry.get("directories", []):
                for f in dir_entry.get("files", []):
                    d = SlskdDownload(
                        id=f.get("id", ""),
                        filename=f.get("filename", ""),
                        size=f.get("size", 0),
                        bytes_downloaded=f.get("bytesTransferred", 0),
                        state=f.get("state", "Unknown"),
                        error=f.get("exception"),
                        username=username,
                    )
                    downloads.append(d)
        if state:
            downloads = [d for d in downloads if d.state.lower() == state.lower()]
        return downloads

    def wait_for_download(
        self, username: str, filename: str, poll_interval: float = 2.0, timeout: float = 300
    ) -> SlskdDownload:
        """Block until a download completes (or errors)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            downloads = self.get_downloads()
            for d in downloads:
                if d.username == username and d.filename == filename:
                    if d.state in ("Completed", "Errored", "Cancelled") or "Aborted" in d.state or "Completed" in d.state:
                        return d
                    break
            time.sleep(poll_interval)
        raise TimeoutError(f"Download timed out after {timeout}s: {filename}")

    # ── Internal ────────────────────────────────────────────

    def _post(self, path: str, json: Any) -> requests.Response:
        url = urljoin(self.base_url, API_PREFIX + path)
        return self._session.post(url, json=json, timeout=self.timeout)

    def _get(self, path: str) -> requests.Response:
        url = urljoin(self.base_url, API_PREFIX + path)
        return self._session.get(url, timeout=self.timeout)

    def _parse_files(self, data: Any) -> list[SlskdFile]:
        files: list[SlskdFile] = []
        if not data or not isinstance(data, list):
            return files
        for entry in data:
            if isinstance(entry, dict) and "files" in entry:
                for f in entry.get("files", []):
                    files.append(SlskdFile(
                        filename=f.get("filename", ""),
                        size=f.get("size", 0),
                        bitrate=f.get("bitRate", f.get("bitrate", 0)),
                        duration=f.get("length", f.get("duration", 0)),
                        sample_rate=f.get("sampleRate", 0),
                        username=entry.get("username", ""),
                        slot_free=entry.get("hasFreeUploadSlot", True),
                        queue_length=entry.get("queueLength", 0),
                        upload_speed=entry.get("uploadSpeed", 0),
                    ))
        return files

    @staticmethod
    def _as_download(d: dict) -> SlskdDownload:
        return SlskdDownload(
            id=d.get("id", ""),
            filename=d.get("filename", ""),
            size=d.get("size", 0),
            bytes_downloaded=d.get("bytesDownloaded", 0),
            state=d.get("state", "Unknown"),
            error=d.get("error"),
            username=d.get("username", ""),
        )
