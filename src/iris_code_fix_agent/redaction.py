"""Conservative secret detection for model exposure, never for writeback."""

import re

PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s]+"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16})\b"
    ),
    re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*[\"\']?[^\s\"\',;]{8,}"
    ),
    re.compile(r"https?://[^\s/]+:[^\s/@]+@"),
    re.compile(
        r"(?i)https?://[^\s]+[?&](?:token|key|signature|x-amz-signature)=[^\s&]+"
    ),
]


def secret_ranges(text: str) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(
        {match.span() for pattern in PATTERNS for match in pattern.finditer(text)}
    ):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def mask_text(text: str) -> str:
    for start, end in reversed(secret_ranges(text)):
        text = text[:start] + "[REDACTED]" + text[end:]
    return text


def mask_value(value):
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if re.search(
                    r"(?i)^(downloadUrl|authorization|password|apiKey|token|secret)$",
                    key,
                )
                else mask_value(item)
            )
            for key, item in value.items()
        }
    return value
