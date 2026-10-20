from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from curl_cffi import requests

_PROFILE_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

TIKREC_SIGN_URL = "https://tikrec.com/tiktok/room/api/sign"


class TikTokError(Exception):
    pass


@dataclass
class TikTokPost:
    aweme_id: str
    desc: str
    create_time: int
    cover_url: str
    username: str

    @property
    def url(self) -> str:
        return f"https://www.tiktok.com/@{self.username}/video/{self.aweme_id}"


@dataclass
class LiveStream:
    room_id: str
    title: str
    viewer_count: Optional[int]
    username: str

    @property
    def url(self) -> str:
        return f"https://www.tiktok.com/@{self.username}/live"


def _first_cover(item: dict[str, Any]) -> str:
    video = item.get("video") or {}
    for key in ("cover", "originCover", "dynamicCover"):
        val = video.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, list) and val and isinstance(val[0], str):
            return val[0]
    for key in ("covers", "coverUrl"):
        val = item.get(key)
        if isinstance(val, list) and val and isinstance(val[0], str):
            return val[0]
    author = item.get("author") or {}
    return author.get("avatarThumb") or ""


def _post_from_item(item: dict[str, Any], username: str) -> TikTokPost:
    aweme_id = str(item.get("aweme_id") or item.get("id") or "")
    if not aweme_id:
        raise TikTokError("Post item is missing an id")
    return TikTokPost(
        aweme_id=aweme_id,
        desc=(item.get("desc") or "").strip(),
        create_time=int(item.get("create_time") or 0),
        cover_url=_first_cover(item),
        username=username,
    )


def _live_from_api_live(data: Any, username: str) -> Optional[LiveStream]:
    if not isinstance(data, dict) or data.get("statusCode") not in (0, None):
        return None
    room = (data.get("data") or {}).get("liveRoom") or {}
    if not isinstance(room, dict) or room.get("status") == 2:
        return None
    room_id = room.get("roomId") or room.get("idStr") or room.get("id")
    if not room_id:
        return None
    stats = room.get("liveRoomStats") or {}
    viewers = stats.get("userCount")
    try:
        viewers = int(viewers) if viewers is not None else None
    except (TypeError, ValueError):
        viewers = None
    return LiveStream(
        room_id=str(room_id),
        title=(room.get("title") or "").strip(),
        viewer_count=viewers,
        username=username,
    )


def _live_from_webcast(data: Any, username: str) -> Optional[LiveStream]:
    if not isinstance(data, dict):
        return None
    room = (data.get("data") or {}).get("room")
    if not isinstance(room, dict):
        return None
    if room.get("status") not in (None, 2):
        return None
    room_id = room.get("id_str") or room.get("room_id")
    if not room_id:
        return None
    viewers = room.get("user_count")
    try:
        viewers = int(viewers) if viewers is not None else None
    except (TypeError, ValueError):
        viewers = None
    return LiveStream(
        room_id=str(room_id),
        title=(room.get("title") or "").strip(),
        viewer_count=viewers,
        username=username,
    )


class TikTokClient:
    def __init__(self, proxy: Optional[str] = None, timeout: float = 20.0):
        self._timeout = timeout
        self._session_kwargs: dict[str, Any] = {"impersonate": "chrome"}
        if proxy:
            self._session_kwargs["proxies"] = {"http": proxy, "https": proxy}
        self._session = requests.Session(**self._session_kwargs)

    def _get(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        referer: Optional[str] = None,
    ) -> requests.Response:
        headers = {"Accept": "text/html,application/json,*/*"}
        if referer:
            headers["Referer"] = referer
        try:
            resp = self._session.get(
                url, params=params, headers=headers, timeout=self._timeout
            )
        except Exception as exc:
            raise TikTokError(f"Request to TikTok failed: {exc}") from exc
        return resp

    def _get_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        referer: Optional[str] = None,
    ) -> Any:
        resp = self._get(url, params=params, referer=referer)
        if resp.status_code != 200:
            raise TikTokError(f"TikTok returned HTTP {resp.status_code} for {url}")
        try:
            return resp.json()
        except Exception as exc:
            raise TikTokError(
                "TikTok returned a non-JSON response (blocked, wrong region, or bad proxy)"
            ) from exc

    def get_profile(self, username: str) -> dict[str, Any]:
        username = username.strip().lstrip("@")
        url = f"https://www.tiktok.com/@{username}"
        resp = self._get(url)
        if resp.status_code == 404:
            raise TikTokError(f"TikTok user @{username} does not exist")
        if resp.status_code != 200:
            raise TikTokError(f"TikTok returned HTTP {resp.status_code} for @{username}")
        match = _PROFILE_RE.search(resp.text)
        if not match:
            raise TikTokError(
                f"Could not read profile page for @{username} "
                "(blocked, private, or the proxy/VPN region can't reach TikTok)"
            )
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise TikTokError(f"Could not parse profile data for @{username}") from exc
        scope = data.get("__DEFAULT_SCOPE__") or {}
        detail = scope.get("webapp.user-detail")
        if not detail or not (detail.get("userInfo") or {}).get("user"):
            raise TikTokError(f"@{username} profile not found or not public")
        return detail

    def _user_api_params(self, sec_uid: str) -> dict[str, Any]:
        return {
            "aid": "1988",
            "app_language": "en",
            "app_name": "tiktok_web",
            "channel": "tiktok_web",
            "cookie_enabled": "true",
            "count": "12",
            "cursor": "0",
            "device_platform": "web",
            "focus_state": "true",
            "from_page": "user",
            "history_len": "3",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "language": "en",
            "os": "windows",
            "priority_region": "US",
            "region": "US",
            "screen_height": "1080",
            "screen_width": "1920",
            "sec_user_id": sec_uid,
            "tz_name": "America/New_York",
            "webcast_language": "en",
        }

    def get_recent_posts(self, username: str, count: int = 12) -> list[TikTokPost]:
        username = username.strip().lstrip("@")
        detail = self.get_profile(username)
        user = detail["userInfo"]["user"]
        sec_uid = user.get("secUid") or ""
        items = detail.get("itemList") or detail["userInfo"].get("itemList") or []

        if sec_uid:
            try:
                data = self._get_json(
                    "https://www.tiktok.com/api/aweme/post/",
                    params=self._user_api_params(sec_uid),
                    referer=f"https://www.tiktok.com/@{username}",
                )
                api_items = data.get("item_list") or data.get("aweme_list") or []
                if api_items:
                    items = api_items
            except TikTokError:
                pass

        posts: list[TikTokPost] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                posts.append(_post_from_item(item, username))
            except TikTokError:
                continue
            if len(posts) >= count:
                break
        return posts

    def _live_via_tikrec(self, username: str) -> Optional[LiveStream]:
        sign_data = self._get_json(TIKREC_SIGN_URL, params={"unique_id": username})
        signed_url = sign_data.get("signed_url") if isinstance(sign_data, dict) else None
        if not signed_url:
            return None
        data = self._get_json(
            signed_url, referer=f"https://www.tiktok.com/@{username}/live"
        )
        return _live_from_api_live(data, username)

    def _live_via_webcast(self, username: str) -> Optional[LiveStream]:
        params = {
            "aid": "1988",
            "app_language": "en",
            "app_name": "tiktok_web",
            "browser_language": "en-US",
            "browser_name": "Mozilla",
            "browser_online": "true",
            "browser_platform": "Win32",
            "channel": "tiktok_web",
            "device_platform": "web",
            "focus_state": "true",
            "from_page": "user",
            "identity": "audience",
            "live_id": "1",
            "need_persist_device_param": "false",
            "priority_region": "US",
            "region": "US",
            "screen_height": "1080",
            "screen_width": "1920",
            "sourceType": "54",
            "tz_name": "America/New_York",
            "unique_id": username,
            "user_is_login": "false",
            "webcast_language": "en",
        }
        data = self._get_json(
            "https://webcast.tiktok.com/webcast/room/get_by_user/",
            params=params,
            referer=f"https://www.tiktok.com/@{username}",
        )
        return _live_from_webcast(data, username)

    def get_live_stream(self, username: str) -> Optional[LiveStream]:
        username = username.strip().lstrip("@")
        errors: list[str] = []
        for check in (self._live_via_tikrec, self._live_via_webcast):
            try:
                return check(username)
            except TikTokError as exc:
                errors.append(str(exc))
        raise TikTokError("; ".join(errors) or "Live check failed")
