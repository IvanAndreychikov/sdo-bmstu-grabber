"""Authenticated HTTP access to the Moodle instance."""
from __future__ import annotations

import logging

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class LoginError(RuntimeError):
    """Raised when authentication against Moodle fails."""


class MoodleClient:
    """Wraps a logged-in :class:`requests.Session` for the SDO Moodle site."""

    def __init__(self, base_url: str, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})

    # -- public API ------------------------------------------------------------
    def login(self, username: str, password: str) -> None:
        """Authenticate; raises :class:`LoginError` on failure."""
        login_url = f"{self.base_url}/login/index.php"
        page = self._get(login_url)
        token = self._extract_login_token(page.text)

        resp = self.session.post(
            login_url,
            data={"username": username, "password": password, "logintoken": token},
            timeout=self.timeout,
        )
        resp.raise_for_status()

        # Moodle keeps you on /login/index.php (with an error notice) when the
        # credentials are wrong; a success redirects elsewhere.
        if "login/index.php" in resp.url or "loginerrors" in resp.text:
            raise LoginError(
                "Login failed — check username/password. "
                f"Landed on: {resp.url}"
            )
        log.info("Logged in as %s", username)

    def get(self, url: str, *, allow_redirects: bool = True) -> requests.Response:
        """GET an absolute or site-relative URL within the session."""
        return self._get(self._absolute(url), allow_redirects=allow_redirects)

    def get_soup(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.get(url).text, "lxml")

    # -- internals -------------------------------------------------------------
    def _get(self, url: str, *, allow_redirects: bool = True) -> requests.Response:
        resp = self.session.get(
            url, timeout=self.timeout, allow_redirects=allow_redirects
        )
        resp.raise_for_status()
        return resp

    def _absolute(self, url: str) -> str:
        if url.startswith("http"):
            return url
        return f"{self.base_url}/{url.lstrip('/')}"

    @staticmethod
    def _extract_login_token(html: str) -> str:
        el = BeautifulSoup(html, "lxml").find("input", {"name": "logintoken"})
        # Older Moodle builds omit the token; an empty value is then accepted.
        return el["value"] if el and el.has_attr("value") else ""
