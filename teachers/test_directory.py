from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import httpx

from .directory import TeacherDirectoryService, parse_teacher_directory


def link(fio: str, label: str | None = None) -> str:
    return f'<p class="schedule-filter-list__item"><a href="/schedule/lecturers?{urlencode({"fio": fio})}">{label or fio}</a></p>'


FIRST = "Ёршов Иван Иванович"
SECOND = "Иванова Анна Петровна"
JOINT = f"{FIRST}, {SECOND}"
PAGE = link(FIRST) + link(FIRST) + link(SECOND) + link(JOINT)


class DirectoryParserTests(unittest.TestCase):
    def test_initials_merge_into_unique_full_name_and_keep_joint_keys(self):
        short = "Багаева З.Г."
        full = "Багаева Зарина Геннадьевна"
        joint = f"{short}, {SECOND}"
        records = parse_teacher_directory(link(short) + link(full) + link(joint))
        self.assertEqual(len(records), 2)
        teacher = next(record for record in records if record["fio"] == full)
        self.assertEqual(set(teacher["schedule_fios"]), {short, full, joint})

    def test_ambiguous_initials_do_not_merge_namesakes(self):
        names = ["Иванова А.П.", "Иванова Анна Петровна", "Иванова Алла Павловна"]
        records = parse_teacher_directory("".join(link(name) for name in names))
        self.assertEqual(len(records), 3)

    def test_initial_spacing_and_full_namesakes(self):
        names = ["Ёршов И.И.", "ершов И. И.", FIRST, "Ёршов Игорь Петрович"]
        records = parse_teacher_directory("".join(link(name) for name in names))
        self.assertEqual(len(records), 2)
        teacher = next(record for record in records if record["fio"] == FIRST)
        self.assertEqual(len(teacher["schedule_fios"]), 3)

    def test_duplicates_and_joint_names_keep_all_schedule_keys(self):
        records = parse_teacher_directory(PAGE)
        self.assertEqual(len(records), 2)
        by_name = {record["fio"]: record["schedule_fios"] for record in records}
        self.assertEqual(by_name[FIRST], [FIRST, JOINT])
        self.assertEqual(by_name[SECOND], [SECOND, JOINT])

    def test_exact_query_key_is_used_even_if_label_is_shortened(self):
        records = parse_teacher_directory(link(FIRST, "Ёршов И. И."))
        self.assertEqual(records[0]["fio"], FIRST)

    def test_foreign_links_and_empty_page_are_not_valid_directories(self):
        for page in ["<html>Ошибка портала</html>", '<p class="schedule-filter-list__item"><a href="https://other.example/schedule/lecturers?fio=Test">Test</a></p>']:
            with self.assertRaises(ValueError):
                parse_teacher_directory(page)

    def test_e_yo_and_case_duplicates_are_normalized(self):
        records = parse_teacher_directory(link(FIRST) + link(FIRST.lower().replace("ё", "е")))
        self.assertEqual(len(records), 1)
        self.assertEqual(len(records[0]["schedule_fios"]), 2)


class DirectoryCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_calls_fetch_once_and_reuse_cache(self):
        service = TeacherDirectoryService()
        with patch.object(service, "_fetch", AsyncMock(return_value=PAGE)) as fetch:
            results = await asyncio.gather(*(service.get() for _ in range(8)))
            self.assertEqual(fetch.await_count, 1)
            self.assertTrue(all(result["success"] for result in results))
            self.assertFalse(results[0]["stale"])

    async def test_failed_update_preserves_directory_and_original_timestamp(self):
        service = TeacherDirectoryService()
        with patch.object(service, "_fetch", AsyncMock(return_value=PAGE)):
            first = await service.get()
        service._expires = 0
        with patch.object(service, "_fetch", AsyncMock(side_effect=httpx.ConnectError("offline"))) as fetch:
            saved = await service.get()
            again = await service.get()
            self.assertEqual(fetch.await_count, 1)
        self.assertTrue(saved["stale"])
        self.assertEqual(saved["teachers"], first["teachers"])
        self.assertEqual(saved["updated_at"], first["updated_at"])
        self.assertEqual(saved, again)

    async def test_first_failure_is_not_replaced_by_empty_success(self):
        service = TeacherDirectoryService()
        with patch.object(service, "_fetch", AsyncMock(return_value="not a directory")):
            with self.assertRaises(ValueError):
                await service.get()


class DirectoryApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from .router import router

        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def test_public_directory_endpoint_needs_no_credentials(self):
        from .router import directory_service

        payload = {"success": True, "teachers": parse_teacher_directory(PAGE),
                   "stale": False, "updated_at": "2026-10-09T09:00:00+00:00"}
        with patch.object(directory_service, "get", AsyncMock(return_value=payload)):
            response = self.client.get("/api/v1/teachers/directory")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)

    def test_directory_error_is_sanitized_and_returns_502(self):
        from .router import directory_service

        with patch.object(directory_service, "get", AsyncMock(side_effect=ValueError("private upstream response"))):
            response = self.client.get("/api/v1/teachers/directory")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.text)

    def test_existing_schedule_endpoint_keeps_its_contract(self):
        from datetime import date
        from .parser import TeacherLesson

        lesson = TeacherLesson(date=date(2026, 10, 9), start="09:00", end="10:30",
                               subject="История", lesson_type="lecture", group_code="TEST", room="314")
        self.app.state.get_lessons = AsyncMock(return_value=([lesson], "memory", None))
        response = self.client.get("/api/v1/teachers/schedule", params={"fio": FIRST, "date": "2026-10-09", "week": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["lessons"][0]["room"], "314")
        self.assertEqual(response.json()["groups"]["preview"], ["TEST"])


if __name__ == "__main__":
    unittest.main()
