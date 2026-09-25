"""loads_lenient (astra/ai/json_extract.py).

Regression: a model's `{"action": "final", "answer": "...multi-line code..."}`
reply routinely contains a REAL line break inside the "answer" string value
instead of the JSON-required `\\n` escape. That is invalid per the JSON
grammar, so `json.loads` (and every existing fallback in `loads_lenient`,
which all called `json.loads` on some substring) raised, `_parse_action`
returned None, and the whole raw JSON wrapper was treated as the model's
plain-text final answer and shown to the user verbatim — or, for a tool-call
wrapper with several protocol keys, stripped down to nothing by
`response_boundary`'s cleanup, producing the "internal formatting issue"
fallback. Reproduced end-to-end against both symptoms below.
"""
import json
import unittest

from astra.ai.json_extract import loads_lenient
from astra.ai.agent_tool_loop import _parse_action
from astra.ai.response_boundary import sanitize_final_response


CODE_ANSWER = ('{"action": "final", "answer": "```python\n'
               'class Calculator:\n'
               '    def add(self, a, b):\n'
               '        return a + b\n'
               '```"}')

TRIPLE_QUOTED_ANSWER = '''{
  "action": "final",
  "answer": """
Here is the Python calculator class with the requested methods:

```python
class Calculator:
    def add(self, a, b):
        """Add two numbers."""
        return a + b

    def divide(self, a, b):
        """Divide two numbers."""
        if b == 0:
            return 0.0
        return a / b
```

Save this as `calculator.py`.
"""
}'''

TOOL_CALL_WITH_RAW_NEWLINE = (
    '{"action": "tool", "tool": "runtime_command", '
    '"args": {"command": "pwd"}, "session_id": "s1", '
    '"thought": "First I will\ncheck the directory"}')


class TestLoadsLenientRawControlChars(unittest.TestCase):
    def test_bare_json_with_raw_newline_in_a_string_value(self):
        data = loads_lenient(CODE_ANSWER)
        self.assertEqual(data["action"], "final")
        self.assertIn("class Calculator", data["answer"])
        self.assertIn("\n", data["answer"])   # the newline survives, just escaped

    def test_raw_tab_and_carriage_return_are_tolerated_too(self):
        raw = '{"a": "col1\tcol2\r\nrow"}'
        self.assertEqual(loads_lenient(raw), {"a": "col1\tcol2\r\nrow"})

    def test_fenced_json_with_raw_newline_inside_a_string(self):
        raw = '```json\n{"a": "x\ny"}\n```'
        self.assertEqual(loads_lenient(raw), {"a": "x\ny"})

    def test_preamble_and_trailing_prose_around_broken_json_still_works(self):
        raw = 'Sure, here you go:\n{"action": "final", "answer": "l1\nl2"}\nHope that helps!'
        self.assertEqual(loads_lenient(raw),
                         {"action": "final", "answer": "l1\nl2"})

    def test_a_quoted_literal_backslash_n_is_never_double_escaped(self):
        # already-valid JSON containing an escaped newline must round-trip
        # unchanged — the sanitizer must not touch text outside a raw
        # control character (i.e. must not mangle an existing `\n`).
        raw = json.dumps({"a": "line1\nline2", "b": 'has "quotes" too'})
        self.assertEqual(loads_lenient(raw),
                         {"a": "line1\nline2", "b": 'has "quotes" too'})

    def test_ordinary_well_formed_json_is_unaffected_fast_path(self):
        self.assertEqual(loads_lenient('{"x": 1}'), {"x": 1})

    def test_genuinely_non_json_text_still_raises(self):
        with self.assertRaises(ValueError):
            loads_lenient("this is not json at all")

    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            loads_lenient("")


class TestLoadsLenientPythonTripleQuotedValue(unittest.TestCase):
    """Regression: a model sometimes wraps the "answer" value in Python-style
    triple quotes (\"\"\"...\"\"\") instead of a properly escaped JSON string,
    and that value's own fenced code routinely contains ITS OWN triple-quoted
    docstrings. The fix must pair the outer opening \"\"\" with the LAST \"\"\"
    in the text (not the next one, which would just be the first docstring's
    opening marker and truncate the answer after a few words)."""

    def test_triple_quoted_answer_with_nested_docstrings_is_unwrapped(self):
        data = loads_lenient(TRIPLE_QUOTED_ANSWER)
        self.assertEqual(data["action"], "final")
        answer = data["answer"]
        self.assertNotIn('"action"', answer)          # no wrapper text leaks
        self.assertIn("class Calculator", answer)
        self.assertIn('"""Add two numbers."""', answer)  # inner docstring intact
        self.assertIn("Save this as `calculator.py`.", answer)

    def test_end_to_end_via_parse_action_no_wrapper_leak(self):
        from astra.ai.agent_tool_loop import _parse_action
        action = _parse_action(TRIPLE_QUOTED_ANSWER)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "final")
        self.assertNotIn('"action"', action["answer"])
        self.assertIn("class Calculator", action["answer"])

    def test_triple_quoted_value_inside_a_json_fence_also_works(self):
        raw = '```json\n' + TRIPLE_QUOTED_ANSWER + '\n```'
        data = loads_lenient(raw)
        self.assertEqual(data["action"], "final")
        self.assertIn("class Calculator", data["answer"])

    def test_triple_quoted_value_with_surrounding_prose_also_works(self):
        raw = 'Sure, here you go:\n' + TRIPLE_QUOTED_ANSWER + '\nHope that helps!'
        data = loads_lenient(raw)
        self.assertEqual(data["action"], "final")
        self.assertIn("class Calculator", data["answer"])

    def test_ordinary_well_formed_json_with_a_stray_triple_quote_substring_unaffected(self):
        # """ appearing legitimately inside an already-valid JSON string
        # (properly escaped) must not be mistaken for the broken pattern.
        raw = json.dumps({"a": 'contains \\"\\"\\" three quotes, still fine'})
        self.assertEqual(loads_lenient(raw), json.loads(raw))


class TestEndToEndUnwrap(unittest.TestCase):
    """The two visible symptoms, fixed at their actual source."""

    def test_final_answer_with_multiline_code_is_unwrapped_not_leaked(self):
        action = _parse_action(CODE_ANSWER)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "final")
        answer = action["answer"]
        self.assertNotIn('"action"', answer)     # no wrapper text leaks
        self.assertIn("class Calculator", answer)

    def test_tool_call_with_raw_newline_in_thought_is_recognized_as_a_tool_call(self):
        action = _parse_action(TOOL_CALL_WITH_RAW_NEWLINE)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "tool")
        self.assertEqual(action["tool"], "runtime_command")
        self.assertEqual(action["args"], {"command": "pwd"})

    def test_boundary_guard_never_needs_to_fall_back_for_this_case(self):
        # Once _parse_action correctly extracts just the "answer" text,
        # that clean text (not the JSON wrapper) is what ever reaches
        # sanitize_final_response — so it passes through unchanged.
        action = _parse_action(CODE_ANSWER)
        cleaned = sanitize_final_response(action["answer"])
        self.assertEqual(cleaned, action["answer"])
        self.assertNotIn("internal formatting issue", cleaned)


if __name__ == "__main__":
    unittest.main()
