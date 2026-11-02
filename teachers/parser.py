from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime as dt

from bs4 import BeautifulSoup

DAY_RE = re.compile(r"^(ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s+(\d{1,2})\.(\d{1,2})$", re.I)
TIME_RE = re.compile(r"(\d{1,2}:\d{2})\s*[–-]\s*(\d{1,2}:\d{2})")
TYPE_RE = re.compile(r"^(лек|пр|лпз|лаб|сем|конс|к|экз|зач|диф\.?\s*зач)\s+", re.I)

TYPE_MAP = {
    "лек": "lecture",
    "пр": "practice",
    "лпз": "practice",
    "лаб": "lab",
    "сем": "seminar",
    "конс": "consultation",
    "к": "consultation",
    "экз": "exam",
    "зач": "credit",
    "диф.зач": "credit",
}


@dataclass(slots=True, frozen=True)
class TeacherLesson:
    date: date
    start: str
    end: str
    subject: str
    lesson_type: str | None
    group_code: str
    room: str


class TeacherScheduleParser:
    def parse(self, content: str) -> list[TeacherLesson]:
        soup = BeautifulSoup(content, "html.parser")
        table = self._find_schedule_table(soup)
        if table is None:
            raise ValueError("Таблица расписания преподавателя не найдена")

        lessons: list[TeacherLesson] = []
        current_date: date | None = None

        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            values = [self.clean_text(c.get_text(" ", strip=True)) for c in cells]
            values = [v for v in values if v]
            if not values:
                continue

            detected = self._parse_day(values)
            if detected:
                current_date = detected
                continue

            if self._looks_like_header(values):
                continue
            if current_date is None:
                if TIME_RE.search(" ".join(values)):
                    raise ValueError("Schedule row has no recognized date")
                continue

            lesson = self._parse_lesson(values, current_date)
            if lesson:
                lessons.append(lesson)
            elif TIME_RE.search(" ".join(values)):
                raise ValueError("Schedule row could not be parsed")

        return self._deduplicate(lessons)

    def _find_schedule_table(self, soup: BeautifulSoup):
        for table in soup.find_all("table"):
            text = self.clean_text(table.get_text(" ", strip=True)).lower()
            if all(x in text for x in ("дата", "время", "предмет", "группа", "аудитория")):
                return table

        for table in soup.find_all("table"):
            text = self.clean_text(table.get_text(" ", strip=True))
            if re.search(r"(?:ПН|ВТ|СР|ЧТ|ПТ|СБ|ВС)\s+\d{1,2}\.\d{1,2}", text, re.I) and TIME_RE.search(text):
                return table
        return None

    @staticmethod
    def _parse_day(values: list[str]) -> date | None:
        for value in values:
            match = DAY_RE.match(value)
            if match:
                return TeacherScheduleParser._resolve_year(int(match.group(2)), int(match.group(3)))
        return None

    @staticmethod
    def _resolve_year(day: int, month: int) -> date:
        today = date.today()
        candidates = [date(y, month, day) for y in (today.year - 1, today.year, today.year + 1)]
        return min(candidates, key=lambda item: abs((item - today).days))

    def _parse_lesson(self, values: list[str], lesson_date: date) -> TeacherLesson | None:
        time_index = None
        match = None
        for index, value in enumerate(values):
            match = TIME_RE.search(value)
            if match:
                time_index = index
                break

        if match is None or time_index is None:
            return None

        after = values[time_index + 1:]
        if len(after) < 3:
            return None

        subject, lesson_type = self._split_type(after[0])
        if not subject:
            return None

        return TeacherLesson(
            date=lesson_date,
            start=match.group(1),
            end=match.group(2),
            subject=subject,
            lesson_type=lesson_type,
            group_code=after[1],
            room=after[2],
        )

    @staticmethod
    def _split_type(value: str) -> tuple[str, str | None]:
        match = TYPE_RE.match(value)
        if not match:
            return value.strip(), None
        raw = re.sub(r"\s+", "", match.group(1).lower()).replace("ё", "е")
        return value[match.end():].strip(), TYPE_MAP.get(raw)

    @staticmethod
    def _looks_like_header(values: list[str]) -> bool:
        joined = " ".join(values).lower()
        return "дата" in joined and "время" in joined and "предмет" in joined

    @staticmethod
    def clean_text(value: str) -> str:
        for char in ("\u200b", "\u200c", "\u200d", "\ufeff", "\xa0"):
            value = value.replace(char, " ")
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _deduplicate(lessons: list[TeacherLesson]) -> list[TeacherLesson]:
        unique = {
            (x.date, x.start, x.end, x.subject, x.lesson_type, x.group_code, x.room): x
            for x in lessons
        }
        return sorted(
            unique.values(),
            key=lambda x: (x.date, dt.strptime(x.start, "%H:%M").time(), x.subject),
        )
