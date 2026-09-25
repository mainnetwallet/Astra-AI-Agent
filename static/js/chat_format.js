/* Astra chat message formatting — DOM-free so it can be unit-tested in node.
 *
 * Turns an assistant/user message into safe HTML:
 *   - every piece of message text is HTML-escaped FIRST, so a message can
 *     never inject markup (model output and user text are untrusted);
 *   - ```lang ... ``` fences become a code block with a language label and a
 *     Copy button (wired by astra.js through event delegation);
 *   - **bold**, `inline code` and line breaks behave as they always did.
 * An unclosed fence (a cut-off reply) renders the rest as a code block.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.AstraChatFormat = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }

  // Inline formatting on ALREADY-ESCAPED text.
  function inline(escaped) {
    return escaped
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
  }

  var LANG_RE = /^[A-Za-z0-9_+#.\-]{0,30}$/;

  // Split a message into [{t:"text", v} | {t:"code", lang, v}] in order.
  function split(text) {
    var s = String(text == null ? "" : text);
    var parts = [];
    var i = 0;
    while (i < s.length) {
      var open = s.indexOf("```", i);
      if (open < 0) { parts.push({ t: "text", v: s.slice(i) }); break; }
      if (open > i) parts.push({ t: "text", v: s.slice(i, open) });
      var afterOpen = open + 3;
      var lang = "";
      var bodyStart = afterOpen;
      var nl = s.indexOf("\n", afterOpen);
      if (nl >= 0) {
        var head = s.slice(afterOpen, nl).trim();
        if (LANG_RE.test(head)) { lang = head; bodyStart = nl + 1; }
      }
      var close = s.indexOf("```", bodyStart);
      var body, next;
      if (close < 0) { body = s.slice(bodyStart); next = s.length; }
      else { body = s.slice(bodyStart, close); next = close + 3; }
      parts.push({ t: "code", lang: lang, v: body.replace(/\n$/, "") });
      i = next;
    }
    return parts;
  }

  function codeBlockHtml(lang, body) {
    return '<div class="code-block">' +
      '<div class="code-head"><span class="code-lang">' +
      escapeHtml(lang || "code") + '</span>' +
      '<button type="button" class="code-copy">Copy</button></div>' +
      '<pre class="code-pre"><code>' + escapeHtml(body) + '</code></pre></div>';
  }

  function toHtml(text) {
    var parts = split(text);
    var out = [];
    for (var k = 0; k < parts.length; k++) {
      var p = parts[k];
      if (p.t === "code") { out.push(codeBlockHtml(p.lang, p.v)); continue; }
      var v = p.v;
      // a code block is a block element: the newline right before/after it
      // is layout, not an extra blank line
      if (k > 0 && parts[k - 1].t === "code") v = v.replace(/^\n/, "");
      if (k + 1 < parts.length && parts[k + 1].t === "code") v = v.replace(/\n$/, "");
      if (!v) continue;
      out.push(inline(escapeHtml(v)).replace(/\n/g, "<br>"));
    }
    return out.join("");
  }

  return { escapeHtml: escapeHtml, split: split, toHtml: toHtml };
});
