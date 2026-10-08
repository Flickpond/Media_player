import pytest
from pydantic import ValidationError

from app.schemas.upload import PART_SIZE, CompleteUpload, StartUpload


@pytest.mark.parametrize("size", [0, -1, True, "100", 1.5, PART_SIZE * 10000 + 1])
def test_declared_size_must_be_a_bounded_positive_integer(size):
    with pytest.raises(ValidationError):
        StartUpload(filename="a.mp4", content_type="video/mp4", size=size)


@pytest.mark.parametrize(
    "parts", [[], [{"n": 2, "etag": "a"}], [{"n": 1, "etag": "a"}, {"n": 1, "etag": "b"}]]
)
def test_completion_rejects_missing_or_duplicate_part_numbers(parts):
    with pytest.raises(ValidationError):
        CompleteUpload(parts=parts)


def test_completion_canonicalizes_order_and_preserves_etag_quotes():
    parts = [{"n": 2, "etag": '"two"'}, {"n": 1, "etag": '"one"'}]
    assert CompleteUpload(parts=parts).manifest() == list(reversed(parts))
