"""
GitHub REST API client — wraps requests, no extra deps.
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

import requests

GITHUB_API = "https://api.github.com"
MAX_DL = int(os.environ.get("MAX_DL_MB", "200")) << 20


class GitHubError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        self.status = status
        super().__init__(msg)


class GitHubClient:
    """Thin wrapper around GitHub REST API. One instance per token."""

    def __init__(self, token: str):
        self.token = token
        self.sess = requests.Session()
        self.sess.headers.update({
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "CubaExchangeBot/1.0",
        })

    # ── Internals ──────────────────────────────────────────────────────────

    def _get(self, path: str, **kw: Any) -> Any:
        r = self.sess.get(f"{GITHUB_API}{path}", timeout=20, **kw)
        return self._raise(r)

    def _post(self, path: str, json: dict | None = None) -> Any:
        r = self.sess.post(f"{GITHUB_API}{path}", json=json or {}, timeout=20)
        return self._raise(r)

    def _put(self, path: str, json: dict | None = None) -> Any:
        r = self.sess.put(f"{GITHUB_API}{path}", json=json or {}, timeout=20)
        return self._raise(r)

    def _delete(self, path: str) -> bool:
        r = self.sess.delete(f"{GITHUB_API}{path}", timeout=20)
        if r.status_code == 204:
            return True
        self._raise(r)
        return True

    @staticmethod
    def _raise(r: requests.Response) -> Any:
        if r.ok:
            return r.json() if r.text and r.text != "true" else True
        msgs = {
            401: "🔑 Token inválido o expirado. Usá /settoken para actualizarlo.",
            403: "⛔ Límite de API excedido o no tenés permiso.",
            404: "🔍 No encontrado.",
        }
        msg = msgs.get(r.status_code, f"GitHub error {r.status_code}")
        try:
            body = r.json().get("message", "")
            if body:
                msg += f": {body}"
        except Exception:
            pass
        raise GitHubError(msg, r.status_code)

    # ── User ───────────────────────────────────────────────────────────────

    def get_user(self) -> dict[str, Any]:
        return self._get("/user")

    # ── Repos ──────────────────────────────────────────────────────────────

    def list_repos(self, username: str | None = None, per_page: int = 50) -> list[dict]:
        path = f"/users/{username}/repos" if username else "/user/repos"
        return self._get(f"{path}?per_page={per_page}&sort=updated&direction=desc")

    def get_repo(self, full_name: str) -> dict[str, Any]:
        return self._get(f"/repos/{full_name}")

    def create_repo(self, name: str, private: bool = False, description: str = "") -> dict[str, Any]:
        return self._post("/user/repos", {
            "name": name,
            "private": private,
            "description": description,
            "auto_init": True,
        })

    def delete_repo(self, full_name: str) -> bool:
        return self._delete(f"/repos/{full_name}")

    def fork_repo(self, full_name: str) -> dict[str, Any]:
        return self._post(f"/repos/{full_name}/forks")

    # ── Contents ───────────────────────────────────────────────────────────

    def get_contents(self, full_name: str, path: str = "") -> Any:
        return self._get(f"/repos/{full_name}/contents/{path}")

    def create_or_update_file(
        self, full_name: str, path: str, content_b64: str,
        message: str, branch: str | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"message": message, "content": content_b64}
        if branch:
            data["branch"] = branch
        # Get sha if file already exists
        try:
            existing = self.get_contents(full_name, path)
            if isinstance(existing, dict) and "sha" in existing:
                data["sha"] = existing["sha"]
        except GitHubError:
            pass
        return self._put(f"/repos/{full_name}/contents/{path}", data)

    # ── Branches ───────────────────────────────────────────────────────────

    def list_branches(self, full_name: str) -> list[dict]:
        return self._get(f"/repos/{full_name}/branches")

    # ── Commits ────────────────────────────────────────────────────────────

    def list_commits(self, full_name: str, per_page: int = 10) -> list[dict]:
        return self._get(f"/repos/{full_name}/commits?per_page={per_page}")

    # ── Search ─────────────────────────────────────────────────────────────

    def search_repos(self, query: str, per_page: int = 10) -> list[dict]:
        r = self.sess.get(
            f"{GITHUB_API}/search/repositories",
            params={"q": query, "per_page": per_page},
            timeout=15,
        )
        return self._raise(r).get("items", [])

    # ── Download ───────────────────────────────────────────────────────────

    def download_repo(self, full_name: str, branch: str | None = None) -> tuple[str, str]:
        """Download repo as ZIP → (tmp_path, filename). Caller must unlink tmp_path."""
        repo = self.get_repo(full_name)
        branch = branch or repo.get("default_branch", "main")
        dl_url = f"https://github.com/{full_name}/archive/refs/heads/{branch}.zip"

        r = self.sess.get(dl_url, stream=True, timeout=300)
        r.raise_for_status()

        if int(r.headers.get("Content-Length", 0)) > MAX_DL:
            raise GitHubError(f"📦 Muy grande (> {MAX_DL >> 20}MB)")

        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        for chunk in r.iter_content(1 << 14):
            if chunk:
                tmp.write(chunk)
        tmp.close()
        return tmp.name, f"{full_name.replace('/', '_')}_{branch}.zip"

    def download_all_user_repos(self, username: str) -> list[tuple[str, str]]:
        """Download all repos of a user. Returns list of (tmp_path, filename)."""
        repos = self.list_repos(username, per_page=100)
        results: list[tuple[str, str]] = []
        for repo in repos:
            try:
                path, name = self.download_repo(repo["full_name"])
                results.append((path, name))
            except Exception as e:
                # ponytail: skip failures, log would be better. Add logging if debugging.
                pass
        return results

    # ── Token validation ───────────────────────────────────────────────────

    def validate_token(self) -> dict[str, Any] | None:
        """Test token. Returns user dict on success, None on failure."""
        try:
            return self.get_user()
        except GitHubError:
            return None
