import os


def bounded(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, str(default)))))
    except ValueError:
        return default


ENABLED = os.getenv('AI_ENABLED', 'false').lower() == 'true'
# This release is a closed test build. Set false before a public rollout.
TEST_MODE = os.getenv('AI_TEST_MODE', 'true').lower() == 'true'
MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
KEY = os.getenv('DEEPSEEK_API_KEY', '')
MAX_OUTPUT = bounded('AI_MAX_OUTPUT_TOKENS', 900, 200, 1600)
CONTEXT_CHARS = bounded('AI_CONTEXT_CHARS', 8000, 2000, 12000)
USER_DAILY = bounded('AI_USER_DAILY_REQUESTS', 20, 1, 100)
DAY_BUDGET = bounded('AI_DAILY_TOKEN_BUDGET', 200000, 1000, 10000000)
MONTH_BUDGET = bounded('AI_MONTHLY_TOKEN_BUDGET', 3000000, 1000, 100000000)
PARALLEL = bounded('AI_MAX_CONCURRENT', 2, 1, 4)
