"""JSON records spanning the bounded values of conversation labels."""

import json
from collections.abc import Mapping
from typing import Any

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN


def encode_json_labels(
    key: str, payload: Any, *, metadata: Mapping[str, Any] | None = None
) -> dict[str, str]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= LABEL_VALUE_MAX_LEN:
        return {key: text}
    chunks = [
        text[index : index + LABEL_VALUE_MAX_LEN]
        for index in range(0, len(text), LABEL_VALUE_MAX_LEN)
    ]
    return {
        key: json.dumps({**(metadata or {}), "chunks": len(chunks)}),
        **{f"{key}.{index}": chunk for index, chunk in enumerate(chunks)},
    }


def decode_json_labels(labels: Mapping[str, str], key: str) -> Any:
    try:
        record = json.loads(labels.get(key, "null"))
        if isinstance(record, dict) and "chunks" in record:
            count = record["chunks"]
            if type(count) is not int or count < 1 or count > len(labels):
                return None
            return json.loads("".join(labels[f"{key}.{index}"] for index in range(count)))
        return record
    except (ValueError, KeyError, TypeError):
        return None
