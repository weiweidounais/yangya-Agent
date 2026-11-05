"""Single source of truth for tool prompt JSON contracts."""

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")


NonEmpty = Annotated[str, Field(min_length=1, pattern=r"\S")]


class DraftOutput(ToolOutput):
    kind: Literal["email", "message", "article", "note", "other"]
    title: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    body: Annotated[str, Field(min_length=1, max_length=6000, pattern=r"\S")]

    @field_validator("title", "body", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


def output_contract(model: type[ToolOutput]) -> str:
    return (
        "\n输出格式：只输出一个 JSON 对象，不要 Markdown 或解释。所有字段必填，"
        "不得增加字段，不得用 null，不得把数字或布尔值当字符串。严格遵循 JSON Schema：\n"
        + json.dumps(model.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    )
