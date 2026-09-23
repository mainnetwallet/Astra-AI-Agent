/* Minimal DOM + EventSource shim shared by the Assistant-chat UI tests.

Nothing here is application code: astra.js only touches the DOM through
getElementById/createElement/appendChild/classList/addEventListener, so this
lets tests drive the REAL static/js/astra.js with no browser at all.
*/
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..");
const HTML = fs.readFileSync(path.join(ROOT, "static/index.html"), "utf8");
const ID_RE = /id="([^"]+)"/g;
const REAL_SET_TIMEOUT = globalThis.setTimeout.bind(globalThis);
const REAL_SET_INTERVAL = globalThis.setInterval.bind(globalThis);

/* ------------------------------------------------------------- DOM shim -- */
class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  toggle(c, on) {
    const want = on === undefined ? !this.set.has(c) : !!on;
    if (want) this.set.add(c); else this.set.delete(c);
    return want;
  }
  contains(c) { return this.set.has(c); }
}

class El {
  constructor(doc, id) {
    this.ownerDocument = doc;
    this.id = id || "";
    this.tagName = "DIV";
    this.type = "text";
    this.dataset = {};
    this.style = {};
    this.children = [];
    this._listeners = {};
    this._html = "";
    this._attrs = {};
    this.classList = new ClassList();
    this.value = "";
    this.textContent = "";
    this.disabled = false;
    this.hidden = false;
    this._className = "";
    this.checked = false;
    this.parentElement = null;
    this.scrollTop = 0;
    this.scrollHeight = 200;
    this.clientHeight = 100;
  }
  get innerHTML() { return this._html; }
  // className and classList are two views of one thing, exactly like the real
  // DOM: assigning className re-sets the class set (astra.js paints card
  // state through className, the existing status through classList).
  get className() { return this._className; }
  set className(v) {
    this._className = String(v);
    this.classList = new ClassList();
    this._className.split(/\s+/).filter(Boolean)
      .forEach((c) => this.classList.add(c));
  }
  set innerHTML(v) {
    this.children.forEach((c) => { c.parentElement = null; });
    this.children = [];
    this._html = String(v);
    this.ownerDocument._scanIds(this._html);
  }
  get firstChild() { return this.children[0] || null; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  get previousElementSibling() {
    const p = this.parentElement;
    if (!p) return null;
    const i = p.children.indexOf(this);
    return i > 0 ? p.children[i - 1] : null;
  }
  get offsetHeight() { return 20; }
  get isConnected() {
    let n = this;
    while (n.parentElement) n = n.parentElement;
    return n === this.ownerDocument.root;
  }
  appendChild(child) {
    if (child.parentElement) {
      const i = child.parentElement.children.indexOf(child);
      if (i >= 0) child.parentElement.children.splice(i, 1);
    }
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) { this.children.splice(i, 1); child.parentElement = null; }
    return child;
  }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener() {}
  fire(type, ev) {
    const self = this;
    const event = ev || {};
    if (!event.preventDefault) event.preventDefault = () => {};
    return Promise.resolve().then(() => {
      let out;
      (self._listeners[type] || []).forEach((fn) => { out = fn(event); });
      return out;
    });
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  setAttribute(k, v) { this._attrs[k] = String(v); this[k] = String(v); }
  getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; }
  removeAttribute(k) { delete this._attrs[k]; }
  contains(node) {
    if (this === node) return true;
    return this.children.some((c) => c.contains(node));
  }
  focus() {}
  select() {}
  blur() {}
  click() {}
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
}

class Doc {
  constructor(html) {
    this._els = new Map();
    this.root = new El(this, "__root");
    this.head = new El(this, "");
    this.body = new El(this, "");
    this.title = "";
    this._scanIds(html);
  }
  _scanIds(html) {
    let m;
    ID_RE.lastIndex = 0;
    while ((m = ID_RE.exec(html))) this._ensure(m[1]);
  }
  _ensure(id) {
    if (!this._els.has(id)) this._els.set(id, new El(this, id));
    return this._els.get(id);
  }
  has(id) { return this._els.has(id); }
  getElementById(id) { return this._els.get(id) || null; }
  querySelector(sel) {
    // astra.js's $() helper takes a CSS selector; the subset it uses is a
    // plain "#id" (everything else goes through querySelectorAll).
    const s = String(sel);
    if (s.charAt(0) === "#" && s.indexOf(" ") < 0 && s.indexOf(".") < 0) {
      return this.getElementById(s.slice(1));
    }
    return null;
  }
  querySelectorAll() { return []; }
  createElement() { return new El(this, ""); }
  createElementNS() { return new El(this, ""); }
  createDocumentFragment() { return new El(this, ""); }
  createTextNode(text) {
    const el = new El(this, "");
    el.nodeType = 3;
    el.textContent = String(text);
    return el;
  }
  addEventListener() {}
}

/* ------------------------------------------------------------- helpers --- */
function findEl(root, cls) {
  if (!root) return null;
  const names = String(root.className || "").split(/\s+/);
  if (names.indexOf(cls) >= 0) return root;
  for (const c of root.children || []) {
    const hit = findEl(c, cls);
    if (hit) return hit;
  }
  return null;
}
function findAll(root, cls, out) {
  const acc = out || [];
  if (!root) return acc;
  if (String(root.className || "").split(/\s+/).indexOf(cls) >= 0) acc.push(root);
  for (const c of root.children || []) findAll(c, cls, acc);
  return acc;
}
function textOf(el) {
  if (!el) return "";
  let s = String(el.textContent || "");
  if (!el.children || el.children.length === 0) {
    // chatBubble() sets message text through innerHTML; the shim stores it as
    // a plain string, so include it (tags stripped) for the assertions.
    s += " " + String(el._html || "");
  } else {
    for (const c of el.children) s += " " + textOf(c);
  }
  return s.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
}

const flush = () => new Promise((r) => REAL_SET_TIMEOUT(r, 0));

module.exports = { ROOT, HTML, ID_RE, REAL_SET_TIMEOUT, REAL_SET_INTERVAL,
                   ClassList, El, Doc, findEl, findAll, textOf, flush };
