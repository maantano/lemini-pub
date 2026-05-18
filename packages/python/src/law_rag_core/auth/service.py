from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import jwt

from ..repository import Repository
from ..settings import get_settings
from ..types import AuthResult

logger = logging.getLogger(__name__)


class AuthService:
    """인증 서비스 — 카카오 OAuth + JWT 토큰 발급/검증을 담당한다."""

    def __init__(self, repository: Repository | None = None) -> None:
        self.settings = get_settings()
        self.repository = repository or Repository()

    # ── Kakao OAuth ──────────────────────────────────────────

    def exchange_kakao_code(self, code: str) -> dict:
        """카카오 인가 코드를 access_token으로 교환한다."""
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": self.settings.kakao_client_id,
            "redirect_uri": self.settings.kakao_redirect_uri,
            "code": code,
            "client_secret": self.settings.kakao_client_secret,
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://kauth.kakao.com/oauth/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("Kakao token exchange failed: %s %s", exc.code, body)
            raise RuntimeError(f"Kakao token exchange failed ({exc.code}): {body}") from exc

    def get_kakao_user(self, access_token: str) -> dict:
        """카카오 access_token으로 사용자 프로필(닉네임, 프로필 이미지)을 조회한다."""
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("Kakao user fetch failed: %s %s", exc.code, body)
            raise RuntimeError(f"Kakao user fetch failed ({exc.code}): {body}") from exc

    # ── Full login / register flow ───────────────────────────

    def login_or_register(self, code: str) -> AuthResult:
        """카카오 로그인 전체 흐름: 코드 교환 → 프로필 조회 → 사용자 생성/갱신 → JWT 발급."""
        token_resp = self.exchange_kakao_code(code)
        kakao_access_token = token_resp["access_token"]

        profile = self.get_kakao_user(kakao_access_token)
        kakao_id = str(profile["id"])

        kakao_account = profile.get("kakao_account", {})
        kakao_profile = kakao_account.get("profile", {})
        nickname = kakao_profile.get("nickname", f"user_{kakao_id}")
        profile_image = kakao_profile.get("profile_image_url")

        user, is_new = self.repository.upsert_user(
            kakao_id=kakao_id,
            nickname=nickname,
            profile_image=profile_image,
        )

        jwt_token = self.create_jwt(user.id, user.nickname)

        return AuthResult(
            token=jwt_token,
            user_id=user.id,
            nickname=user.nickname,
            profile_image=user.profile_image,
            is_new=is_new,
        )

    # ── JWT (PyJWT) ───────────────────────────────────────────

    def create_jwt(self, user_id: str, nickname: str) -> str:
        """JWT 토큰 생성 (HS256, 기본 72시간 만료)."""
        payload = {
            "sub": user_id,
            "nickname": nickname,
            "exp": datetime.now(timezone.utc) + timedelta(hours=self.settings.jwt_expire_hours),
        }
        return jwt.encode(payload, self.settings.jwt_secret, algorithm="HS256")

    def verify_jwt(self, token: str) -> dict | None:
        """JWT 서명 검증 + 만료 확인. 유효하면 payload 반환, 아니면 None."""
        try:
            return jwt.decode(token, self.settings.jwt_secret, algorithms=["HS256"])
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return None

    def get_current_user(self, authorization: str | None) -> dict | None:  # Authorization 헤더에서 Bearer 토큰 추출 후 검증
        if not authorization:
            return None
        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return self.verify_jwt(parts[1])
