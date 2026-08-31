import pytest

from careeros.materials.models import ValidationError
from careeros.materials.storage import prepare_root, stage_bytes


def test_stream_stage_bound(tmp_path):
    root = tmp_path / "root"
    prepare_root(root)
    with pytest.raises(ValidationError):
        stage_bytes(root, [b"x" * (25 * 1024 * 1024 + 1)])
    assert not list((root / ".staging").iterdir())
