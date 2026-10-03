import pytest

from iris_code_fix_agent.redaction import secret_ranges


@pytest.mark.parametrize(
    "code",
    [
        'api_key = os.environ["API_KEY"]',
        "password = get_password_from_env()",
        "secret = settings.jwt_secret_key",
        'DATABASE_URL = os.getenv("DATABASE_URL")',
        "authorization: str = Header(None)",
        "redis://localhost:6379",
        "postgresql://db.internal:5432/app",
    ],
)
def test_reading_a_value_is_not_a_secret(code):
    assert secret_ranges(code) == []


@pytest.mark.parametrize(
    "text",
    [
        'password = "hunter2hunter2"',
        "api_key='abcdefgh12345'",
        "API_KEY=abcd1234efgh5678",
        "password: hunter2hunter2",
        "postgres://user:pw12345678@db/app",
        "redis://:secretpw@host:6379",
        "Authorization: Bearer abcdefghijkl",
        '{"password":"abcdefghijk123456"}',
        "{'api_key': 'abcdefghijk123456'}",
        '{"access_token": "abcdefghijk123456"}',
    ],
)
def test_literal_credentials_are_still_detected(text):
    assert secret_ranges(text)
