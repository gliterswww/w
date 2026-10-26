import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nqtrader.data import split_sessions  # noqa: E402
from nqtrader.research.synthetic import generate_bars  # noqa: E402


@pytest.fixture(scope="session")
def random_walk_bars():
    return generate_bars(120, seed=5)


@pytest.fixture(scope="session")
def random_walk_sessions(random_walk_bars):
    return split_sessions(random_walk_bars)
