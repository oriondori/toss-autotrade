"""공통 HTTP 계층 — 인증 헤더, rate limit, 401/429 자동 처리."""
import time
from typing import Any

import requests

from .auth import BASE_URL, TokenManager
from .ratelimit import RateLimiter


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str = ""):
        self.status, self.code, self.request_id = status, code, request_id
        super().__init__(f"[{status}] {code}: {message} (requestId={request_id})")


class TossClient:
    """토스증권 Open API 공통 클라이언트."""

    def __init__(self, account_seq: str | None = None) -> None:
        self.tm = TokenManager()
        self.rl = RateLimiter()
        self.account_seq = account_seq  # 계좌/자산/주문 API에 필요
        self._http = requests.Session()

    def get(self, path: str, group: str, params: dict | None = None,
            need_account: bool = False) -> Any:
        return self._request("GET", path, group, params=params, need_account=need_account)

    def post(self, path: str, group: str, json: dict | None = None,
             need_account: bool = False) -> Any:
        return self._request("POST", path, group, json=json, need_account=need_account)

    def _request(self, method: str, path: str, group: str, *,
                 params: dict | None = None, json: dict | None = None,
                 need_account: bool = False, _retried: bool = False) -> Any:
        self.rl.wait(group)
        headers = {"Authorization": f"Bearer {self.tm.get_token()}"}
        if need_account:
            if not self.account_seq:
                raise RuntimeError("account_seq 미설정 — /api/v1/accounts 로 먼저 확인하세요.")
            headers["X-Tossinvest-Account"] = str(self.account_seq)

        resp = self._http.request(method, BASE_URL + path, params=params,
                                  json=json, headers=headers, timeout=10)

        # 429 → Retry-After 만큼 대기 후 1회 재시도
        if resp.status_code == 429 and not _retried:
            time.sleep(float(resp.headers.get("Retry-After", 1)))
            return self._request(method, path, group, params=params, json=json,
                                 need_account=need_account, _retried=True)

        if resp.status_code >= 400:
            err = self._parse_error(resp)
            # 토큰 만료·무효 → 재발급 후 1회 재시도
            _reauth_codes = ("expired-token", "invalid-token", "login-user-not-found")
            if resp.status_code == 401 and err.code in _reauth_codes and not _retried:
                self.tm.invalidate()
                return self._request(method, path, group, params=params, json=json,
                                     need_account=need_account, _retried=True)
            raise err

        return resp.json()

    @staticmethod
    def _parse_error(resp: requests.Response) -> ApiError:
        try:
            e = resp.json().get("error", {})
            return ApiError(resp.status_code, e.get("code", "unknown"),
                            e.get("message", resp.text[:200]), e.get("requestId", ""))
        except Exception:
            return ApiError(resp.status_code, "unknown", resp.text[:200])
