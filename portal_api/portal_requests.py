from __future__ import annotations

from typing import Any

import requests
from bs4 import BeautifulSoup
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .portal import (
    BASE_URL,
    is_logged_in,
    portal_session,
    require_portal_credentials,
    require_unio_portal_user,
    csrf_from_page,
    clean_text,
    request_id_from_url,
)


router = APIRouter(prefix="/requests", tags=["portal:requests"])

REQUESTS_URL = f"{BASE_URL}/request/index"
REQUEST_CREATE_URL = f"{BASE_URL}/request/create"


# -------- models --------

class CreateRequest(BaseModel):
    category_id: str
    type_id: str


class SubmitRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


# -------- parsers --------

def _parse_categories_and_types(soup: BeautifulSoup) -> dict[str, list[dict[str, str]]]:
    categories: list[dict[str, str]] = []
    types: list[dict[str, str]] = []

    category_select = soup.select_one("#category-selector")
    if category_select:
        for option in category_select.find_all("option"):
            value = option.get("value", "").strip()
            if value:
                categories.append({"id": value, "name": clean_text(option.get_text())})

    type_select = soup.select_one("#type-selector")
    if type_select:
        for option in type_select.find_all("option"):
            value = option.get("value", "").strip()
            if value:
                types.append({"id": value, "name": clean_text(option.get_text())})

    return {"categories": categories, "types": types}


def _parse_request_list(soup: BeautifulSoup) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for item in soup.select(".requestItem"):
        link = item.select_one("a.requestItem-Info")
        edit_link = item.select_one('a[href*="/request/"][href*="/edit"]')

        href = link.get("href", "") if link else ""
        edit_href = edit_link.get("href", "") if edit_link else href
        request_id = item.get("data-key") or request_id_from_url(edit_href)

        title_node = item.select_one(".requestItem-title")
        cat_node = item.select_one(".requestItem-Info-Descr h5")
        status_node = item.select_one(".Ach-Status")
        upd_node = item.select_one(".requestItem-Info-Descr-Status p")

        result.append({
            "id": int(request_id) if str(request_id).isdigit() else request_id,
            "title": clean_text(title_node.get_text() if title_node else ""),
            "category": clean_text(cat_node.get_text() if cat_node else ""),
            "status": clean_text(status_node.get_text() if status_node else ""),
            "updated": clean_text(upd_node.get_text() if upd_node else ""),
            "edit_url": edit_href,
        })

    return result


def _field_label(soup: BeautifulSoup, element) -> str:
    element_id = element.get("id")
    if element_id:
        label = soup.find("label", attrs={"for": element_id})
        if label:
            text = clean_text(label.get_text(" ", strip=True))
            if text:
                return text

    parent = element.parent
    if parent:
        label = parent.find("label")
        if label:
            text = clean_text(label.get_text(" ", strip=True))
            if text:
                return text

    previous = element.find_previous(["label", "h3", "h4", "p"])
    if previous:
        return clean_text(previous.get_text(" ", strip=True))

    return element.get("name", "")


def _parse_edit_form(soup: BeautifulSoup) -> dict[str, Any]:
    form = soup.select_one("#approve-request-form")
    if not form:
        form = soup.find("form", method=lambda x: x and x.lower() == "post")
    if not form:
        raise ValueError("Форма заявления не найдена")

    fields: list[dict[str, Any]] = []
    hidden: dict[str, str] = {}

    for element in form.select('input[type="hidden"][name]'):
        hidden[element["name"]] = element.get("value", "")

    for element in form.select("input[name], select[name], textarea[name]"):
        name = element.get("name")
        if not name or element.get("type") == "hidden":
            continue

        tag = element.name
        field_type = element.get("type", "text") if tag == "input" else tag

        field: dict[str, Any] = {
            "name": name,
            "label": _field_label(soup, element),
            "type": field_type,
            "required": element.has_attr("required"),
        }

        if tag == "select":
            options = [
                {"value": o.get("value", ""), "label": clean_text(o.get_text())}
                for o in element.find_all("option")
            ]
            field["options"] = options
            selected = element.find("option", selected=True)
            field["value"] = selected.get("value", "") if selected else ""

        elif tag == "textarea":
            field["value"] = element.get_text()

        else:
            field["value"] = element.get("value", "")

        fields.append(field)

    action = form.get("action") or ""
    if action.startswith("/"):
        action = BASE_URL + action

    return {
        "action": action,
        "method": (form.get("method") or "post").upper(),
        "hidden": hidden,
        "fields": fields,
    }


# -------- routes --------

@router.get("")
async def list_requests(
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
            response = session.get(REQUESTS_URL, timeout=20, allow_redirects=True)
            response.raise_for_status()

            if not is_logged_in(response):
                raise HTTPException(401, "Авторизация ВГСПУ не прошла")

            soup = BeautifulSoup(response.text, "html.parser")
            choices = _parse_categories_and_types(soup)

            return {
                "success": True,
                "requests": _parse_request_list(soup),
                "available_categories": choices["categories"],
                "available_types": choices["types"],
            }

    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(502, f"Ошибка получения заявлений: {exc}") from exc


@router.get("/{request_id}")
async def get_request(
    request_id: int,
    x_unio_token: str | None = Header(default=None),
    x_portal_login: str | None = Header(default=None),
    x_portal_password: str | None = Header(default=None),
):
    await require_unio_portal_user(x_unio_token)
    login, password = require_portal_credentials(
        x_portal_login,
        x_portal_password,
    )
    url = f"{BASE_URL}/request/{request_id}/edit"

    try:
        with portal_session(login, password) as session:
            response = session.get(url, timeout=20, allow_redirects=True)
            response.raise_for_status()

            if not is_logged_in(response):
                raise HTTPException(401, "Авторизация ВГСПУ не прошла")

            soup = BeautifulSoup(response.text, "html.parser")
            return {
                "success": True,
                "request_id": request_id,
                "form": _parse_edit_form(soup),
            }

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(502, "Request unavailable") from exc
    except requests.RequestException as exc:
        raise HTTPException(502, f"Ошибка получения заявления: {exc}") from exc


@router.post("")
async def create_request(
    data: CreateRequest,
    x_unio_token: str | None = Header(default=None),
    x_portal_login: str | None = Header(default=None),
    x_portal_password: str | None = Header(default=None),
):
    await require_unio_portal_user(x_unio_token)
    login, password = require_portal_credentials(
        x_portal_login,
        x_portal_password,
    )

    payload = {
        "CreateRequestForm[category_id]": data.category_id,
        "CreateRequestForm[type_id]": data.type_id,
    }

    try:
        with portal_session(login, password) as session:
            page = session.get(REQUESTS_URL, timeout=20)
            page.raise_for_status()

            csrf = csrf_from_page(BeautifulSoup(page.text, "html.parser"))
            if csrf:
                payload["_csrf"] = csrf

            response = session.post(
                REQUEST_CREATE_URL,
                data=payload,
                timeout=20,
                allow_redirects=True,
            )
            response.raise_for_status()

            if not is_logged_in(response):
                raise HTTPException(401, "Авторизация ВГСПУ не прошла")

            request_id = request_id_from_url(response.url)
            if request_id is None:
                soup = BeautifulSoup(response.text, "html.parser")
                edit_link = soup.select_one('a[href*="/request/"][href*="/edit"]')
                if edit_link:
                    request_id = request_id_from_url(edit_link.get("href", ""))

            if request_id is None:
                raise HTTPException(502, "Портал не вернул ID созданного заявления")

            return {
                "success": True,
                "message": "Заявление создано",
                "request_id": request_id,
                "edit_url": f"{BASE_URL}/request/{request_id}/edit",
            }

    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(502, f"Ошибка создания заявления: {exc}") from exc


@router.post("/{request_id}/submit")
async def submit_request(
    request_id: int,
    data: SubmitRequest,
    x_unio_token: str | None = Header(default=None),
    x_portal_login: str | None = Header(default=None),
    x_portal_password: str | None = Header(default=None),
):
    await require_unio_portal_user(x_unio_token)
    login, password = require_portal_credentials(
        x_portal_login,
        x_portal_password,
    )
    url = f"{BASE_URL}/request/{request_id}/edit"

    try:
        with portal_session(login, password) as session:
            page = session.get(url, timeout=20, allow_redirects=True)
            page.raise_for_status()

            if not is_logged_in(page):
                raise HTTPException(401, "Авторизация ВГСПУ не прошла")

            soup = BeautifulSoup(page.text, "html.parser")
            form = _parse_edit_form(soup)

            payload = dict(form["hidden"])
            for field in form["fields"]:
                name = field["name"]
                value = data.values.get(name, field.get("value", ""))

                if value is None:
                    value = ""
                if isinstance(value, bool):
                    value = "1" if value else "0"

                payload[name] = str(value)

            response = session.post(
                form["action"] or url,
                data=payload,
                timeout=20,
                allow_redirects=True,
            )
            response.raise_for_status()

            if not is_logged_in(response):
                raise HTTPException(401, "Авторизация ВГСПУ не прошла")

            result_soup = BeautifulSoup(response.text, "html.parser")
            result_form = result_soup.select_one("#approve-request-form")

            errors = [
                clean_text(node.get_text(" ", strip=True))
                for node in result_soup.select(".help-block")
                if clean_text(node.get_text(" ", strip=True))
            ]

            if result_form and errors:
                return {
                    "success": False,
                    "message": "Портал не принял заявление",
                    "errors": errors,
                }

            return {
                "success": True,
                "message": "Заявление отправлено на обработку",
                "request_id": request_id,
                "url": response.url,
            }

    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(502, f"Ошибка отправки заявления: {exc}") from exc

