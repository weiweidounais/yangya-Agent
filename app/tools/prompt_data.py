"""Boundaries for untrusted source material in tool prompts."""

from html import escape
from typing import Literal

from app.prompts import load_prompt

UNTRUSTED_DATA_RULE = load_prompt("shared.untrusted_data", "v1")


def data_block(tag: Literal["request", "profile", "memory", "draft", "feedback", "source"], material: object) -> str:
    """Escape markup before wrapping so source text cannot close a boundary."""
    return f"<{tag}>\n{escape(str(material), quote=False)}\n</{tag}>"
