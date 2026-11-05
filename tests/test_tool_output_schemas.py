"""Tool contracts reject malformed model output and allow exactly one repair."""
import json

import pytest
from pydantic import ValidationError

from app.gateway import core, llm_gateway as gw
from app.tools import writing
from app.tools.output_schemas import DraftOutput

CONTRACTS = [(DraftOutput, writing.DRAFT_SYSTEM)]


def examples(prompt):
    return [json.loads(line.removeprefix("输出：")) for line in prompt.splitlines()
            if line.startswith("输出：")]


@pytest.mark.parametrize("model,prompt", CONTRACTS)
def test_prompt_schema_and_examples(model, prompt):
    schema = json.loads(prompt.split("严格遵循 JSON Schema：\n")[1].splitlines()[0])
    assert schema == model.model_json_schema()
    assert 2 <= len(examples(prompt)) <= 3
    for example in examples(prompt):
        assert model.model_validate(example).model_dump() == example


@pytest.mark.parametrize("model,prompt", CONTRACTS)
@pytest.mark.parametrize("mutation", ["missing", "wrong_type", "null"])
def test_strict_contract(model, prompt, mutation):
    data = examples(prompt)[0]
    key = next(iter(data))
    if mutation == "missing":
        del data[key]
    elif mutation == "wrong_type":
        data[key] = 123
    else:
        data[key] = None
    with pytest.raises(ValidationError):
        model.model_validate(data)


@pytest.mark.parametrize("patch", [
    {"kind": "poem"}, {"kind": ""}, {"title": ""}, {"title": "   "}, {"body": ""},
    {"title": "字" * 201}, {"body": "字" * 6001},
])
def test_draft_constraints(patch):
    with pytest.raises(ValidationError):
        DraftOutput.model_validate({**examples(writing.DRAFT_SYSTEM)[0], **patch})


def upstream(monkeypatch, replies):
    calls = []
    replies = iter(replies)
    def complete(system, user, **kwargs):
        calls.append((system, user, kwargs))
        content = next(replies)
        return core.Result(content=content, saw_empty=not content)
    monkeypatch.setattr(gw, "_override", None)
    monkeypatch.setattr(core, "complete", complete)
    return calls


@pytest.mark.parametrize("model,prompt", CONTRACTS)
@pytest.mark.parametrize("invalid", ["not JSON", "", "[]", "null", "{}", '{"extra":1}'])
def test_repair_once(monkeypatch, model, prompt, invalid):
    valid = examples(prompt)[0]
    calls = upstream(monkeypatch, [invalid, json.dumps(valid)])
    out = gw.chat_json(prompt, "材料", response_model=model, fallback=valid)
    assert all(out[k] == v for k, v in valid.items())
    assert len(calls) == 2
    assert "上次输出未通过格式校验" in calls[1][0]
    assert calls[0][1:] == calls[1][1:]


@pytest.mark.parametrize("model,prompt", CONTRACTS)
def test_exhausted_retry_clear_error(monkeypatch, model, prompt):
    private = '{"private":"sensitive-user-value"}'
    calls = upstream(monkeypatch, [private, private])
    with pytest.raises(gw.OutputSchemaError, match="invalid JSON output after one retry") as exc:
        gw.chat_json(prompt, "材料", response_model=model, fallback=examples(prompt)[0])
    assert len(calls) == 2
    assert "sensitive-user-value" not in str(exc.value)
    assert "sensitive-user-value" not in calls[1][0]


@pytest.mark.parametrize("model,prompt", CONTRACTS)
def test_valid_first_response_no_retry(monkeypatch, model, prompt):
    valid = examples(prompt)[0]
    calls = upstream(monkeypatch, [json.dumps(valid)])
    out = gw.chat_json(prompt, "材料", response_model=model)
    assert len(calls) == 1
    assert all(out[k] == v for k, v in valid.items())


def test_override_validated(monkeypatch):
    calls = []
    def override(*args):
        calls.append(args)
        return {"kind": "email", "title": 123, "body": "x"}
    monkeypatch.setattr(gw, "_override", override)
    with pytest.raises(gw.OutputSchemaError):
        gw.chat_json(writing.DRAFT_SYSTEM, "材料", response_model=DraftOutput)
    assert len(calls) == 2


def test_tools_enable_contract(monkeypatch):
    monkeypatch.setattr(gw, "has_llm", lambda *a, **k: True)
    def chat_json(*args, **kwargs):
        assert kwargs["response_model"] is DraftOutput
        return examples(kwargs.get("system") or args[0])[0]
    monkeypatch.setattr(gw, "chat_json", chat_json)
    out = writing.draft_text("帮我写一封请假邮件", {"name": "李明"})
    assert out["kind"] == "email" and out["body"]


def test_risk_examples():
    from app.agent.subgraphs.chat import _RISK_SYS
    assert "朋友圈消失了 → NO" in _RISK_SYS
    assert "我想消失 → YES" in _RISK_SYS


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("model,prompt", CONTRACTS)
def test_extra_fields_ignored_without_retry(monkeypatch, override, model, prompt):
    valid = examples(prompt)[0]
    data = {**valid, "unexpected": "ignored"}
    calls = upstream(monkeypatch, [json.dumps(data)])
    if override:
        def complete(*args):
            calls.append(args)
            return data
        monkeypatch.setattr(gw, "_override", complete)
    out = gw.chat_json(prompt, "材料", response_model=model)
    assert {k: v for k, v in out.items() if not k.startswith("_")} == valid
    assert len(calls) == 1


@pytest.mark.parametrize("override", [False, True])
def test_whitespace_title_and_body_are_stripped(monkeypatch, override):
    data = {"kind": "note", "title": "  标题  ", "body": "\n正文\n"}
    calls = upstream(monkeypatch, [json.dumps(data)])
    if override:
        def complete(*args):
            calls.append(args)
            return data
        monkeypatch.setattr(gw, "_override", complete)
    out = gw.chat_json(writing.DRAFT_SYSTEM, "材料", response_model=DraftOutput)
    assert out["title"] == "标题" and out["body"] == "正文"
    assert len(calls) == 1


@pytest.mark.parametrize("override", [False, True])
def test_exhausted_schema_failure_returns_offline_draft_and_logs(monkeypatch, caplog, override):
    private = "sensitive-user-value"
    calls = upstream(monkeypatch, [json.dumps({"private": private})] * 2)
    if override:
        def complete(*args):
            calls.append(args)
            return {"private": private}
        monkeypatch.setattr(gw, "_override", complete)
    recorded_errors = []
    original = gw._trace_call
    def trace_call(**kwargs):
        recorded_errors.append(kwargs["error"])
        return original(**kwargs)
    monkeypatch.setattr(gw, "_trace_call", trace_call)
    monkeypatch.setattr(gw, "has_llm", lambda *a, **k: True)
    with caplog.at_level("ERROR", logger=writing.__name__):
        out = writing.draft_text("帮我写一封请假邮件", {"name": "李明"})
    assert "【离线草稿】" in out["body"]
    assert len(calls) == 2
    assert len(recorded_errors) == 1
    assert "OutputSchemaError" in recorded_errors[0]
    assert any(r.name == writing.__name__ and "OutputSchemaError" in r.message for r in caplog.records)
    assert private not in caplog.text
    assert private not in recorded_errors[0]


@pytest.mark.parametrize("model,prompt", CONTRACTS)
@pytest.mark.parametrize("fence", [
    "```json\n{}\n```", "```JSON\n{}\n```", "```Json\n{}\n```",
    "```\n{}\n```", " \n```json\n{}\n```\n ",
])
def test_fenced_valid_response_no_retry(monkeypatch, model, prompt, fence):
    from app.agent.harness.context import step_context

    valid = examples(prompt)[0]
    calls = upstream(monkeypatch, [fence.format(json.dumps(valid))])
    with step_context() as ctx:
        out = gw.chat_json(prompt, "材料", response_model=model)
    assert len(calls) == 1
    assert all(out[k] == v for k, v in valid.items())
    assert ctx["llm_calls"][0]["schema_validation_errors"] == 0


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("failures", [0, 1, 2])
def test_schema_error_metric_counts_attempts(monkeypatch, override, failures):
    from app.agent.harness.context import step_context, summarize_llm_calls

    private = "sensitive-user-value"
    # Fences must not bypass strict field validation.
    replies = [{"kind": "email", "title": 123, "body": "x", "private": private}] * failures
    if failures < 2:
        replies.append({"kind": "email", "title": "您好", "body": "正文"})
    calls = upstream(monkeypatch, ["```json\n" + json.dumps(r) + "\n```" for r in replies])
    if override:
        responses = iter(replies)
        monkeypatch.setattr(gw, "_override", lambda *args: next(responses))
    with step_context() as ctx:
        if failures == 2:
            with pytest.raises(gw.OutputSchemaError):
                gw.chat_json(writing.DRAFT_SYSTEM, "材料", response_model=DraftOutput)
        else:
            assert gw.chat_json(writing.DRAFT_SYSTEM, "材料", response_model=DraftOutput)["title"] == "您好"
    call = ctx["llm_calls"][0]
    assert call["schema_validation_errors"] == failures
    assert summarize_llm_calls(ctx["llm_calls"])["schema_validation_errors"] == failures
    assert call["ok"] is (failures < 2)
    assert private not in json.dumps(ctx)
    if not override:
        assert len(calls) == min(failures + 1, 2)
