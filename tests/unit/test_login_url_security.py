import pytest

from src.scraper import _is_login_url


@pytest.mark.parametrize("url,expected", [
    ("https://passport.goofish.com/login", True),
    ("https://www.goofish.com/mini_login", True),
    ("https://PASSPORT.GOOFISH.COM/login", True),
    ("https://evil.example/?redirect=passport.goofish.com", False),
    ("https://passport.goofish.com.evil.example/login", False),
    ("https://passport.goofish.com@evil.example/mini_login", False),
    ("https://evil.example/mini_login", False),
    ("javascript:passport.goofish.com", False),
    ("http://[invalid", False),
    ("", False),
])
def test_login_url_requires_real_goofish_hostname(url, expected):
    assert _is_login_url(url) is expected
