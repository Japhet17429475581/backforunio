import unittest
from unittest.mock import AsyncMock, patch
from datetime import date
import httpx
from fastapi import HTTPException
from main import StudentScheduleParser, schedule
from teachers.parser import TeacherScheduleParser


class ScheduleStatesTests(unittest.IsolatedAsyncioTestCase):
    def test_empty_recognized_table_is_valid_but_unknown_html_is_not(self):
        for parser, owner in ((StudentScheduleParser(), 'Преподаватель'),
                              (TeacherScheduleParser(), 'Группа')):
            header = f'<tr><th>Дата Время Предмет {owner} Аудитория</th></tr>'
            self.assertEqual(parser.parse(f'<table>{header}</table>'), [])
            with self.assertRaises(ValueError):
                parser.parse('<html>Maintenance</html>')
            with self.assertRaises(ValueError):
                parser.parse(f'<table>{header}<tr><td>9:40–11:10</td><td>Test</td></tr></table>')

    async def test_upstream_error_is_not_a_successful_empty_week(self):
        for error in (httpx.ReadTimeout('test'), ValueError('invalid HTML')):
            with patch('main.get_lessons', AsyncMock(side_effect=error)):
                with self.assertRaises(HTTPException) as caught:
                    await schedule(group='TEST', teacher=None,
                                   target_date=date(2026, 10, 19), week=True,
                                   force_refresh=False)
                self.assertEqual(caught.exception.status_code, 502)

    async def test_valid_empty_response_retains_backwards_compatible_contract(self):
        with patch('main.get_lessons', AsyncMock(return_value=([], 'portal', None))):
            result = await schedule(group='TEST', teacher=None,
                                    target_date=date(2026, 10, 19), week=True,
                                    force_refresh=False)
            self.assertTrue(result['success'])
            self.assertEqual(result['lessons'], [])


if __name__ == '__main__':
    unittest.main()
