"""The api's test suite. pytest rewrites the asserts of test modules and conftest.py only; the
shared helper modules are registered here, before anything imports them, so a failing
assert in one of them shows its values too."""

import pytest

pytest.register_assert_rewrite(
    "tests.admin_helpers",
    "tests.billing_helpers",
    "tests.chat_helpers",
    "tests.check_helpers",
    "tests.connector_helpers",
    "tests.factories",
    "tests.helpers",
    "tests.library_helpers",
)
