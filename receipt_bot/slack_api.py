"""Minimal Slack Web API client (bot token)."""

import requests

API = "https://slack.com/api/"


class SlackError(RuntimeError):
    pass


class Slack:
    def __init__(self, token: str, dry_run: bool = False):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.dry_run = dry_run
        self._user_names: dict[str, str] = {}
        self.bot_user_id = self._call("auth.test")["user_id"]

    def _call(self, method: str, **params) -> dict:
        if method in ("chat.postMessage", "reactions.add", "reactions.remove"):
            resp = self.session.post(API + method, json=params, timeout=30)
        else:
            resp = self.session.get(API + method, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise SlackError(f"{method}: {data.get('error')}")
        return data

    def history(self, channel: str, oldest: float) -> list[dict]:
        messages, cursor = [], None
        while True:
            params = {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._call("conversations.history", **params)
            messages += data.get("messages", [])
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return sorted(messages, key=lambda m: float(m["ts"]))

    def replies(self, channel: str, ts: str) -> list[dict]:
        return self._call("conversations.replies", channel=channel, ts=ts, limit=200)["messages"][1:]

    def user_name(self, user_id: str) -> str:
        if user_id not in self._user_names:
            try:
                profile = self._call("users.info", user=user_id)["user"]
                self._user_names[user_id] = profile.get("real_name") or profile.get("name") or user_id
            except SlackError:
                self._user_names[user_id] = user_id
        return self._user_names[user_id]

    def permalink(self, channel: str, ts: str) -> str:
        try:
            return self._call("chat.getPermalink", channel=channel, message_ts=ts)["permalink"]
        except SlackError:
            return ""

    def download(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=60)
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("text/html"):
            raise SlackError("File download returned HTML (is the files:read scope granted?)")
        return resp.content

    # --- writes (skipped in dry run) ---

    def reply(self, channel: str, thread_ts: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] reply in {thread_ts}: {text}")
            return
        self._call("chat.postMessage", channel=channel, thread_ts=thread_ts, text=text, unfurl_links=False)

    def post(self, channel: str, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] post: {text}")
            return
        self._call("chat.postMessage", channel=channel, text=text, unfurl_links=False)

    def react(self, channel: str, ts: str, name: str) -> None:
        if self.dry_run:
            return
        try:
            self._call("reactions.add", channel=channel, timestamp=ts, name=name)
        except SlackError as e:
            if "already_reacted" not in str(e):
                raise

    def unreact(self, channel: str, ts: str, name: str) -> None:
        if self.dry_run:
            return
        try:
            self._call("reactions.remove", channel=channel, timestamp=ts, name=name)
        except SlackError as e:
            if "no_reaction" not in str(e):
                raise
