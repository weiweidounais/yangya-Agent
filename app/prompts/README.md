# Versioned runtime prompts

Call sites pin `load_prompt("chat.persona", "v1")` to
`app/prompts/chat.persona/v1.md`. Add `v2.md` and update the explicit pin for a
new revision; keep old versions for review and reproduction. There is no remote
fetch, environment override, or automatic latest-version selection.

Files are UTF-8 and their whitespace is significant, including the absence of a
final newline. The v1 files preserve the previous Python strings byte for byte.
`{{untrusted_data_rule}}` and `{{output_contract}}` are explicit substitution
slots. Schemas still come from the existing Pydantic models. JSON braces and
inserted values are not interpreted recursively. Missing files, invalid names or
versions, and missing/extra substitutions fail immediately.

Existing prompt registration and leak detection are unchanged. Generation traces
keep `prompt_name` and the content hash in `prompt_version`; they additionally
record `prompt_file_version` (for example `v1`). Langfuse's own numeric prompt
versions and hash-based links retain their existing meaning. The rendered hash
also captures changes to schemas or shared rules.

The migration covers the eight registered runtime prompts plus the shared data
rule and question-card guide. Evaluation rubrics remain outside this migration.

Validation:

```sh
python -m pytest tests/unit tests/test_prompt_injection.py -q
AUTH_SECRET=golden-mock-test-only-not-a-real-secret python tests/golden/run_golden.py
```

`tests/unit/test_prompt_files.py` pins pre-migration rendered hashes and verifies
loader failures, whitespace preservation, and substitution boundaries.
