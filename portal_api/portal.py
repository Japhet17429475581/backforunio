from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from typing import Iterator

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Header, HTTPException

from database.users import get_user_by_token_hash
from pydantic import BaseModel


BASE_URL = "https://portal.vspu.ru"
LOGIN_URL = f"{BASE_URL}/login"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0 Safari/537.36"
)


# -------- temporary portal sessions --------
#
# Важно: это НЕ серверная сессия пользователя Unio.
# requests.Session существует только внутри одного backend-запроса и
# закрывается сразу после выполнения операции.


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    return session


@contextmanager
def portal_session(login: str, password: str) -> Iterator[requests.Session]:
    session = new_session()
    try:
        _login_session(session, login, password)
        yield session
    finally:
        session.close()


# -------- html helpers --------


def csrf_from_page(soup: BeautifulSoup) -> str | None:
    csrf = soup.find("input", {"name": "_csrf"})
    if csrf and csrf.get("value"):
        return csrf["value"]
    return None


def is_logged_in(response: requests.Response) -> bool:
    soup = BeautifulSoup(response.text, "html.parser")

    if soup.select_one("#login-form"):
        return False

    if soup.select_one('form[action*="/auth/logout"]'):
        return True

    if "/request/" in response.url or soup.select_one('a[href*="/request/index"]'):
        return True

    return False


def request_id_from_url(url: str) -> int | None:
    match = re.search(r"/request/(\d+)/edit", url)
    return int(match.group(1)) if match else None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _login_session(
    session: requests.Session,
    login: str,
    password: str,
) -> dict[str, str | bool | None]:
    try:
        page = session.get(LOGIN_URL, timeout=20)
        page.raise_for_status()

        soup = BeautifulSoup(page.text, "html.parser")
        form_data = {
            "LoginForm[username]": login,
            "LoginForm[password]": password,
            "RememberMe": "1",
        }
        csrf = csrf_from_page(soup)
        if csrf:
            form_data["_csrf"] = csrf

        response = session.post(
            LOGIN_URL,
            data=form_data,
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()

        if not is_logged_in(response):
            raise HTTPException(401, "Неверный логин или пароль ВГСПУ")

        return {
            "success": True,
            "message": "Вход выполнен",
            "full_name": _extract_full_name(response.text),
        }

    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ошибка подключения к порталу: {exc}",
        ) from exc


def _extract_full_name(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    selectors = [
        ".user-name",
        ".username",
        ".profile-name",
        '[class*="user-name"]',
        '[class*="profile-name"]',
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            value = clean_text(node.get_text(" ", strip=True))
            if value:
                return value

    return None


def check_portal_credentials(login: str, password: str) -> dict[str, str | bool | None]:
    """Проверяет credentials и сразу закрывает временную portal-сессию."""
    session = new_session()
    try:
        return _login_session(session, login, password)
    finally:
        session.close()


async def require_unio_portal_user(x_unio_token: str | None):
    if not x_unio_token:
        raise HTTPException(401, "Требуется авторизация Unio")

    token_hash = hashlib.sha256(x_unio_token.encode("utf-8")).hexdigest()
    user = await get_user_by_token_hash(token_hash)

    if not user:
        raise HTTPException(401, "Токен Unio недействителен или был отозван")

    if user["auth_mode"] != "portal":
        raise HTTPException(403, "Эта функция доступна только после входа в личный кабинет")

    return user


def require_portal_credentials(
    x_portal_login: str | None,
    x_portal_password: str | None,
) -> tuple[str, str]:
    if not x_portal_login or not x_portal_password:
        raise HTTPException(
            401,
            "Для этой функции требуется логин и пароль ВГСПУ",
        )
    return x_portal_login, x_portal_password


# -------- router + compatibility login --------

router = APIRouter(prefix="/api/v1/portal", tags=["portal"])


class PortalLoginRequest(BaseModel):
    login: str
    password: str


@router.post("/login")
def portal_login(data: PortalLoginRequest):
    """Проверка credentials без создания серверной session_id."""
    result = check_portal_credentials(data.login, data.password)
    return result



# Подроутеры импортируются снизу, чтобы избежать circular import.
from .portal_grades import router as grades_router  # noqa: E402
from .portal_requests import router as requests_router  # noqa: E402

router.include_router(grades_router)
router.include_router(requests_router)
