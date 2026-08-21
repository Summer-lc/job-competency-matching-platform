import json

import pytest


def test_incremental_array_decoder_rejects_trailing_comma():
    from src.bounded_json import decode_json_array_incrementally

    with pytest.raises(json.JSONDecodeError):
        decode_json_array_incrementally('[{"id": 1},]', max_records=10)
