"""토스증권 Open API — OAuth 2.0 토큰 발급·캐싱·자동갱신."""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://openapi.tossinvest.com"
_REFRESH_MARGIN = 300  # 만료 5분 전에 미리 갱신
_DEFAULT_TTL = 3600


class TokenManager:
    """액세스 토큰을 발급하고 만료 전 자동 갱신한다."""

    def __init__(self) -> None:
        self.client_id = os.getenv("TOSS_CLIENT_ID", "")
        self.client_secret = os.getenv("TOSS_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                ".env 파일에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 설정하세요. "
                "(.env.example 참고)"
            )
        self._token: str = ""
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        """유효한 토큰 반환. 만료 임박 시 자동 재발급."""
        if not self._token or time.time() >= self._expires_at - _REFRESH_MARGIN:
            self._issue()
        return self._token

    def invalidate(self) -> None:
        """401 expired-token 수신 시 호출 → 다음 get_token()에서 재발급."""
        self._token = ""

    def _issue(self) -> None:
        resp = requests.post(
            BASE_URL + "/oauth2/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        ttl = int(body.get("expires_in", _DEFAULT_TTL))
        self._expires_at = time.time() + ttl
