"""Make the repo root importable so `pytest` works as well as `python -m pytest`."""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def tiny_artifacts(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A small model trained on synthetic data — fast enough for every test.

    The committed/CI model comes from `python -m pricing.training.train`;
    tests train their own so they never depend on artifact freshness.
    """
    from pricing.training.train import train

    out = tmp_path_factory.mktemp("artifacts")
    train(n=4000, seed=3, out_dir=out, control=False)
    return pathlib.Path(out)
