import gc
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _collect_garbage_after_each_test():
    """Audit N13: collect after every test so a leaked socket / event loop raises its
    ResourceWarning (an error, see pytest.ini) in the test that leaked it, not in a random later one."""
    yield
    gc.collect()
