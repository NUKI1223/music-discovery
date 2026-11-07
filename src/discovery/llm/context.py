"""Shared input sizing. Estimate tokens independently of the transport byte ceiling."""
import json
import math


def input_size(payload: dict) -> tuple[int, int]:
    text = json.dumps(payload, ensure_ascii=False)
    # Deliberately padded heuristic, NOT a provider tokenizer. Unicode prose gets
    # a larger allowance than ASCII. Actual usage/cost remains checked by client.
    ascii_count = sum(ord(c) < 128 for c in text)
    nonascii_bytes = sum(len(c.encode('utf-8')) for c in text if ord(c) >= 128)
    estimated_tokens = math.ceil(ascii_count / 3 + nonascii_bytes / 1.5) + 512
    return len(text.encode('utf-8')), estimated_tokens


def fits_input(payload: dict, settings, *, headroom: int = 0) -> bool:
    size, tokens = input_size(payload)
    return size + headroom <= settings.max_input_bytes and tokens + headroom <= settings.max_input_tokens
