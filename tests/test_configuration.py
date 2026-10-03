import pytest

from iris_code_fix_agent.configuration import load_environment, openai_api_key
from iris_code_fix_agent.paths import matches_path


def test_dotenv_alias_without_interpolation_or_overwriting(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API", raising=False)
    path = tmp_path / "runtime.env"
    path.write_text('OPENAI_API="test-${NOT_EXPANDED}"\nAPI_KEY=file-key\n')
    monkeypatch.setenv("API_KEY", "explicit-key")
    load_environment(path)
    assert openai_api_key() == "test-${NOT_EXPANDED}"
    monkeypatch.setenv("OPENAI_API_KEY", "preferred")
    assert openai_api_key() == "preferred"


def test_root_and_nested_glob_consistency():
    assert matches_path("app.py", "**/*.py")
    assert matches_path("src/app.py", "**/*.py")
    assert not matches_path("app.js", "**/*.py")


@pytest.mark.parametrize("name", ["live_evaluate", "was_evaluate"])
def test_evaluator_cli_uses_dotenv_alias_without_paid_calls(
    name, tmp_path, monkeypatch
):
    import importlib.util
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(name, scripts / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key in (
        "OPENAI_API_KEY",
        "OPENAI_API",
        "API_KEY",
        "FIX_INPUT_PRICE_PER_MILLION",
        "FIX_OUTPUT_PRICE_PER_MILLION",
    ):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "OPENAI_API=offline-alias\nAPI_KEY="
        + "x" * 32
        + "\nFIX_INPUT_PRICE_PER_MILLION=1\nFIX_OUTPUT_PRICE_PER_MILLION=2\n"
    )
    monkeypatch.setenv("FIX_ENV_FILE", str(env_file))
    captured = []

    async def offline_evaluation(*arguments, **kwargs):
        runner = arguments[1] if name == "live_evaluate" else arguments[2]
        captured.append(runner.config.api_key)
        return {"allPassed": True, "modelCalls": 0}

    monkeypatch.setattr(module, "evaluate", offline_evaluation)
    arguments = [name, "--output", str(tmp_path / "report")]
    if name == "was_evaluate":
        arguments += ["--was-root", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 0
    assert captured == ["offline-alias"]
