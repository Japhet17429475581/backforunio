from __future__ import annotations

from typing import Any

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Header, HTTPException

from .portal import (
    BASE_URL,
    clean_text,
    is_logged_in,
    portal_session,
    require_portal_credentials,
    require_unio_portal_user,
)

router = APIRouter(prefix="/grades", tags=["portal:grades"])

GRADES_URL = f"{BASE_URL}/progress/index"


def _parse_grades(soup: BeautifulSoup) -> dict[str, Any]:
    summary: dict[str, Any] = {}

    summary_block = soup.select_one(".progress-summary")
    if summary_block:
        for row in summary_block.select(".progress-summary-row"):
            key_node = row.select_one(".progress-summary-key")
            val_node = row.select_one(".progress-summary-value")
            if key_node and val_node:
                summary[clean_text(key_node.get_text())] = clean_text(
                    val_node.get_text()
                )

    terms: list[dict[str, Any]] = []

    for term_block in soup.select(".progress-term"):
        title_node = term_block.select_one(".progress-term-title")
        title = clean_text(title_node.get_text() if title_node else "")

        subjects: list[dict[str, Any]] = []
        for row in term_block.select("table.progress-table tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            subjects.append({
                "subject": clean_text(cells[0].get_text()),
                "type": clean_text(cells[1].get_text()),
                "grade": clean_text(cells[2].get_text()),
                "teacher": clean_text(cells[3].get_text()) if len(cells) > 3 else "",
            })

        terms.append({"title": title, "subjects": subjects})

    return {"summary": summary, "terms": terms}


@router.get("")
async def get_grades(
    x_unio_token: str | None = Header(default=None),
    x_portal_login: str | None = Header(default=None),
    x_portal_password: str | None = Header(default=None),
):
    await require_unio_portal_user(x_unio_token)
    login, password = require_portal_credentials(
        x_portal_login,
        x_portal_password,
    )

    try:
        with portal_session(login, password) as session:
            response = session.get(GRADES_URL, timeout=20, allow_redirects=True)
            response.raise_for_status()

            if not is_logged_in(response):
                raise HTTPException(401, "Авторизация ВГСПУ не прошла")

            soup = BeautifulSoup(response.text, "html.parser")
            return {
                "success": True,
                "grades": _parse_grades(soup),
            }

    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(502, f"Ошибка получения успеваемости: {exc}") from exc
