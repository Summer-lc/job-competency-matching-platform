from __future__ import annotations

import json
from dataclasses import dataclass


class JSONResourceLimitError(ValueError):
    """JSON decoding exceeded a configured resource bound."""


@dataclass(frozen=True)
class DecodedJSONArrayItem:
    raw_text: str
    value: object


def decode_json_array_incrementally(
    text: str, *, max_records: int
) -> list[DecodedJSONArrayItem]:
    decoder = json.JSONDecoder()
    length = len(text)

    def skip_whitespace(index: int) -> int:
        while index < length and text[index].isspace():
            index += 1
        return index

    position = skip_whitespace(0)
    if position >= length or text[position] != "[":
        raise json.JSONDecodeError("expected JSON array", text, position)
    position += 1
    items: list[DecodedJSONArrayItem] = []
    expecting_item = False
    while True:
        position = skip_whitespace(position)
        if position < length and text[position] == "]":
            if expecting_item:
                raise json.JSONDecodeError(
                    "trailing comma in JSON array", text, position
                )
            position = skip_whitespace(position + 1)
            if position != length:
                raise json.JSONDecodeError("extra data", text, position)
            return items
        if len(items) >= max_records:
            raise JSONResourceLimitError(
                f"JSON array record count exceeds limit {max_records}: {max_records + 1}"
            )
        start = position
        try:
            value, position = decoder.raw_decode(text, position)
        except RecursionError as exc:
            raise JSONResourceLimitError("JSON nesting exceeds decoder resources") from exc
        expecting_item = False
        items.append(DecodedJSONArrayItem(text[start:position], value))
        position = skip_whitespace(position)
        if position >= length:
            raise json.JSONDecodeError("unterminated JSON array", text, position)
        if text[position] == ",":
            position += 1
            expecting_item = True
            continue
        if text[position] != "]":
            raise json.JSONDecodeError("expected ',' or ']'", text, position)


__all__ = [
    "DecodedJSONArrayItem",
    "JSONResourceLimitError",
    "decode_json_array_incrementally",
]
