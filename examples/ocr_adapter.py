#!/usr/bin/env python3
from __future__ import annotations

import os
import sys


MIN_PYTHON = (3, 10)


def ensure_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        current = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            f"Python {required}+ is required by MarkItDown. Current Python version is {current}. "
            "Create a virtual environment with python3.10 or newer."
        )


ensure_python_version()

from markitdown import MarkItDown


def build_converter() -> MarkItDown:
    api_key = os.getenv("OCR_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OCR_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
    model = os.getenv("OCR_OPENAI_MODEL") or os.getenv("OPENAI_MODEL")

    if not api_key or not model:
        return MarkItDown(enable_plugins=True)

    from openai import OpenAI

    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    return MarkItDown(
        enable_plugins=True,
        llm_client=OpenAI(**client_kwargs),
        llm_model=model,
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("Usage: ocr_adapter.py /path/to/input-file", file=sys.stderr)
        return 2

    converter = build_converter()
    result = converter.convert(argv[0])
    print(result.text_content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
