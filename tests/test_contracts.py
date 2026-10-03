import pytest
from pydantic import ValidationError

from iris_code_fix_agent.contracts import ModelProposal, TextEdit, safe_path


@pytest.mark.parametrize("path", ["../x", "/x", "a/../x", "a//x", "C:/x", "a\\x"])
def test_paths(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_model_strict_and_required_nullable_fields():
    with pytest.raises(ValidationError):
        TextEdit(path="a", operation="create", newText="a", evidenceIds=[], planIds=[])
    schema = ModelProposal.model_json_schema(by_alias=True)
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
