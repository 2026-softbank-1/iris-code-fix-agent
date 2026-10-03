"""Shared source exposure and candidate write protections."""

import fnmatch


def matches_path(path: str, pattern: str) -> bool:
    return (
        fnmatch.fnmatchcase(path, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
        or (pattern.endswith("/**") and path == pattern[:-3])
    )


def is_protected_path(path: str) -> bool:
    return any(
        part
        in {
            ".git",
            ".github",
            ".ssh",
            ".aws",
            ".npmrc",
            ".pypirc",
            "credentials",
            "credentials.json",
            "id_rsa",
            "id_ed25519",
        }
        or part.startswith(".env")
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        for part in path.casefold().split("/")
    )


def validate_file_tree(paths) -> None:
    files = set(paths)
    for path in files:
        parts = path.split("/")
        if any("/".join(parts[:index]) in files for index in range(1, len(parts))):
            raise ValueError("File and descendant path collision")
