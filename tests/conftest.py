"""Shared opt-in offline fixture helpers; test modules may supply their own fixtures."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def syntax_repair_fixture():
    path = Path(__file__).resolve().parents[1] / "examples" / "make_request.py"
    spec = importlib.util.spec_from_file_location("repair_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_fixture("https://s3.amazonaws.com/example-bucket/source.tar")
