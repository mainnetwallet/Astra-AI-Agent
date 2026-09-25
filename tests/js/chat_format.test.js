/* Unit tests for static/js/chat_format.js (safe chat message formatting). */
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const F = require("../../static/js/chat_format.js");

test("fenced code becomes a code block with language label and Copy button", () => {
  const html = F.toHtml("Ei nao:\n```python\ndef f():\n    return 1\n```\nDone");
  assert.match(html, /<div class="code-block">/);
  assert.match(html, /<span class="code-lang">python<\/span>/);
  assert.match(html, /<button type="button" class="code-copy">Copy<\/button>/);
  assert.match(html, /<pre class="code-pre"><code>def f\(\):\n    return 1<\/code><\/pre>/);
  // no stray fence characters leak into the output
  assert.ok(!html.includes("```"));
});

test("code indentation and newlines are preserved inside the block", () => {
  const html = F.toHtml("```\na\n  b\n\n    c\n```");
  assert.ok(html.includes("<code>a\n  b\n\n    c</code>"));
  assert.ok(!html.includes("<br>"));          // no <br> injected into code
});

test("HTML in the message is escaped, never injected (text and code)", () => {
  const evil = '<img src=x onerror=alert(1)> <script>x</script>';
  const t = F.toHtml(evil + "\n```html\n" + evil + "\n```");
  assert.ok(!/<img|<script/i.test(t));
  assert.ok(t.includes("&lt;img src=x onerror=alert(1)&gt;"));
});

test("bold, inline code and line breaks still work", () => {
  const html = F.toHtml("a **b** and `c`\nnext");
  assert.strictEqual(html, "a <b>b</b> and <code>c</code><br>next");
});

test("no extra blank line around a block", () => {
  const html = F.toHtml("before\n```js\nx\n```\nafter");
  assert.ok(!/<br><div class="code-block"/.test(html));
  assert.ok(!/<\/div><br>after/.test(html));
  assert.match(html, /^before<div class="code-block">/);
  assert.match(html, /<\/div>after$/);
});

test("an unclosed fence renders the rest as a code block", () => {
  const html = F.toHtml("```js\nlet a = 1;");
  assert.match(html, /<code>let a = 1;<\/code>/);
});

test("one-line fence and unknown language tag are handled", () => {
  assert.match(F.toHtml("x ```a=1``` y"), /<code>a=1<\/code>/);
  const html = F.toHtml("```not a lang!\nbody\n```");
  assert.match(html, /<span class="code-lang">code<\/span>/);
  assert.ok(html.includes("not a lang!"));      // kept as code content, not lost
});

test("multiple blocks in one message", () => {
  const html = F.toHtml("```py\na\n```\ntext\n```sh\nb\n```");
  assert.strictEqual((html.match(/class="code-block"/g) || []).length, 2);
});

test("empty / null input is safe", () => {
  assert.strictEqual(F.toHtml(""), "");
  assert.strictEqual(F.toHtml(null), "");
  assert.strictEqual(F.toHtml(undefined), "");
});
