#!/usr/bin/env python3
"""Stage the merged checkpoint for conversion, without touching the original.

The teacher is mounted read-only and must stay exactly as trained, but its
``tokenizer_config.json`` carries ``extra_special_tokens`` as a **list**.
transformers expects a dict keyed by attribute name and dies with::

    AttributeError: 'list' object has no attribute 'keys'

from deep inside tokenizer construction, naming neither the file nor the field
that caused it. An hour can go into that traceback before the checkpoint is
suspected at all.

The staging directory symlinks every file — no multi-gigabyte copy — and
rewrites only that one JSON.

Dropping the field rather than converting it is safe *and checked*: every token
it lists already appears in ``tokenizer.json``'s ``added_tokens``, so the
tokenizer is byte-identical without it. If a future checkpoint lists a token
that is not already added, this exits rather than quietly building a model with
a different vocabulary — a difference that would show up as subtly wrong
generation, not as an error.
"""

from __future__ import annotations

import json
import os
import sys


def main(src: str, stage: str) -> int:
    if os.path.exists(stage):
        for name in os.listdir(stage):
            os.remove(os.path.join(stage, name))
    else:
        os.makedirs(stage)

    for name in os.listdir(src):
        os.symlink(os.path.join(src, name), os.path.join(stage, name))

    config_path = os.path.join(src, "tokenizer_config.json")
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)

    extra = config.get("extra_special_tokens")
    if isinstance(extra, list):
        with open(os.path.join(src, "tokenizer.json"), encoding="utf-8") as handle:
            added = {
                token["content"]
                for token in json.load(handle).get("added_tokens", [])
            }
        missing = [token for token in extra if token not in added]
        if missing:
            print(
                "[stage] FATAL: extra_special_tokens lists tokens that are not "
                f"in tokenizer.json added_tokens: {missing}. Dropping the field "
                "would change the vocabulary.",
                file=sys.stderr,
            )
            return 1
        config.pop("extra_special_tokens")
        print(
            f"[stage] normalised tokenizer_config.json: dropped a list-valued "
            f"extra_special_tokens ({len(extra)} tokens, all already added)",
            file=sys.stderr,
        )

    staged = os.path.join(stage, "tokenizer_config.json")
    # Replace the symlink with a real file; writing through it would modify the
    # read-only source, which is the whole thing this avoids.
    os.remove(staged)
    with open(staged, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: stage_source.py <src-dir> <stage-dir>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
