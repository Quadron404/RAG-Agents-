"""The map's *discovery* of a composer's publishing button, under `node`.

The other UI-map tests build raw entries with `in_editor_region` and
`with_editor` already set, so they never touch the DOM walk that decides those
flags -- and that walk is exactly what the live failure lived in.  A composer's
toolbar sits in the editor's own immediate wrapper; the walk stopped at the
first ancestor holding *any* actionable control, so it stopped at the toolbar,
and the publish button one level up (beside the field, outside that wrapper)
was never marked `with_editor`.  It then lost the map to the background while a
same-named sidebar link survived, and the model pressed the link.

This test runs the real map script -- `vm_agent.daemon._UI_MAP_JS`, the exact
string the daemon evaluates in the page -- against a generic nested fixture
built with a small DOM shim: a sidebar of named links, an editor whose own
wrapper holds a toolbar of secondary controls, a publish button outside that
wrapper but inside the composer, and a page full of background links.  It reads
back what discovery decided, then feeds the raw map through the real
`build_ui_map`/`format_ui_map` to prove the button also survives ranking.

`node` is required to execute the script; when it is absent the test skips
rather than guessing, the same way `test_computer_hit_resolve` does.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.computer.ui_map import build_ui_map, format_ui_map  # noqa: E402
from vm_agent import daemon  # noqa: E402

DISPLAY_W = 1280
DISPLAY_H = 800

#: The fixture, a generic nested page, and a DOM good enough for the map script
#: to read it: tags, attributes, text, boxes, and the four element queries the
#: script makes.  It evaluates whatever map source arrives on stdin and writes
#: the map's own JSON back, so the production discovery code runs unchanged.
_HARNESS_JS = r"""
const fs = require('fs');
const mapSrc = fs.readFileSync(0, 'utf8');

function matchesSel(el, sel) {
  var parts = sel.split(',');
  for (var pi = 0; pi < parts.length; pi++) {
    var p = parts[pi].trim();
    var m = p.match(/^([a-zA-Z]*)(\[([^\]=]+)(?:="([^"]*)")?\])?$/);
    if (!m) continue;
    var tag = m[1], attr = m[3], val = m[4];
    if (tag && (el.tagName || '').toLowerCase() !== tag.toLowerCase()) continue;
    if (attr) {
      var a = el.getAttribute(attr);
      if (val !== undefined) { if ((a || '') !== val) continue; }
      else { if (a === null || a === '') continue; }
    }
    if (!tag && !attr) continue;
    return true;
  }
  return false;
}

function El(tag, attrs, text) {
  this.tagName = tag.toUpperCase();
  this.attrs = attrs || {};
  this.childNodes = [];
  this.parentElement = null;
  this._ownText = text || '';
  this._rect = [0, 0, 0, 0];
  this.scrollLeft = 0; this.scrollTop = 0;
  this.scrollWidth = 0; this.scrollHeight = 0;
  this.clientWidth = 0; this.clientHeight = 0;
  var ce = this.attrs.contenteditable;
  this.isContentEditable = (ce === 'true' || ce === '');
  this.tabIndex = this.attrs.tabindex !== undefined ? parseInt(this.attrs.tabindex, 10) : -1;
  this.disabled = (this.attrs.disabled === 'true' || this.attrs.disabled === '');
}
El.prototype.getAttribute = function (n) {
  return Object.prototype.hasOwnProperty.call(this.attrs, n) ? String(this.attrs[n]) : null;
};
El.prototype.appendChild = function (c) { c.parentElement = this; this.childNodes.push(c); return c; };
El.prototype.getRootNode = function () { return DOC; };
El.prototype.contains = function (n) { for (var x = n; x; x = x.parentElement) { if (x === this) return true; } return false; };
El.prototype.querySelectorAll = function (sel) {
  var out = [];
  (function walk(node) {
    for (var i = 0; i < node.childNodes.length; i++) {
      var c = node.childNodes[i];
      if (matchesSel(c, sel)) out.push(c);
      walk(c);
    }
  })(this);
  return out;
};
El.prototype.querySelector = function (sel) { var r = this.querySelectorAll(sel); return r.length ? r[0] : null; };
El.prototype.getBoundingClientRect = function () {
  var r = this._rect;
  return { left: r[0], top: r[1], right: r[2], bottom: r[3],
           width: r[2] - r[0], height: r[3] - r[1], x: r[0], y: r[1] };
};
Object.defineProperty(El.prototype, 'textContent', {
  get: function () {
    var s = this._ownText;
    for (var i = 0; i < this.childNodes.length; i++) s += ' ' + this.childNodes[i].textContent;
    return s;
  }
});

var DOC = {};
var html = new El('html');
var body = new El('body');
html.appendChild(body);
DOC.documentElement = html;
DOC.body = body;
DOC.querySelectorAll = function (sel) { return html.querySelectorAll(sel); };
DOC.querySelector = function (sel) { return html.querySelector(sel); };
DOC.getElementById = function () { return null; };
DOC.elementFromPoint = function () { return null; };

var win = {
  innerWidth: 1280, innerHeight: 800, outerWidth: 1280, outerHeight: 800,
  screenX: 0, screenY: 0, devicePixelRatio: 1,
  getComputedStyle: function () {
    return { display: 'block', visibility: 'visible', pointerEvents: 'auto',
             overflowX: 'visible', overflowY: 'visible', zIndex: '0' };
  }
};
var screenObj = { width: 1280, height: 800 };

function mk(parent, tag, attrs, text, rect) {
  var e = new El(tag, attrs, text);
  if (rect) e._rect = rect;
  parent.appendChild(e);
  return e;
}

// Sidebar: two named links, one of them sharing the publish button's name.
var nav = mk(body, 'div', { role: 'navigation' }, '');
mk(nav, 'a', { href: '/home' }, 'Home', [20, 300, 320, 340]);
mk(nav, 'a', { href: '/post' }, 'Post', [20, 356, 320, 396]);

// Composer: editor + toolbar inside its immediate wrapper, and the publish
// button outside that wrapper but still inside the composer.
var main = mk(body, 'main', {}, '');
var composer = mk(main, 'div', {}, '');
var editorWrap = mk(composer, 'div', {}, '');
mk(editorWrap, 'div', { contenteditable: 'true' }, 'Post text', [420, 120, 740, 220]);
var toolbar = mk(editorWrap, 'div', { role: 'toolbar' }, '');
mk(toolbar, 'button', {}, 'Add image', [420, 240, 448, 268]);
mk(toolbar, 'button', {}, 'Emoji', [460, 240, 488, 268]);
var footer = mk(composer, 'div', {}, '');
mk(footer, 'button', {}, 'Post', [760, 360, 840, 396]);

// Background furniture, enough to crowd a 20-entry cap.
for (var i = 0; i < 25; i++) {
  mk(main, 'a', { href: '/b/' + i }, 'Background ' + i, [600, 20 + i * 12, 1200, 32 + i * 12]);
}

var evaluate = new Function('window', 'document', 'screen', 'location', 'return ' + mapSrc);
var out = evaluate(win, DOC, screenObj, { href: 'https://fixture.test/' });
process.stdout.write(out);
"""


def _node() -> str | None:
    return shutil.which("node")


@unittest.skipUnless(_node(), "node is required to run the UI-map discovery script")
class TheScannerDiscoversTheComposerAndItsPublishButton(unittest.TestCase):
    """The whole discovery path: real map script over a generic nested page."""

    def discovery(self) -> dict:
        source = (
            daemon._UI_MAP_JS
            .replace("__DISPLAY_W__", str(DISPLAY_W))
            .replace("__DISPLAY_H__", str(DISPLAY_H))
            .replace("__WIN_X__", "0")
            .replace("__WIN_Y__", "0")
            .replace("__WIN_W__", str(DISPLAY_W))
            .replace("__WIN_H__", str(DISPLAY_H))
        )
        result = subprocess.run(
            [_node(), "-e", _HARNESS_JS],
            input=source,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = json.loads(result.stdout)
        raw["display_width"] = DISPLAY_W
        raw["display_height"] = DISPLAY_H
        return raw

    def _by_name(self, raw: dict, name: str) -> list:
        return [e for e in raw["visible"] if e.get("name") == name]

    def test_the_publish_button_shares_the_editors_region(self):
        # The root cause: the editor's wrapper holds the toolbar, and the walk
        # must not stop there.  The button beside the field is discovered as the
        # composer's submit; the toolbar controls are not.
        raw = self.discovery()
        self.assertGreaterEqual(raw["diagnostics"]["candidates"], 30)
        self.assertFalse(raw["diagnostics"]["max_scan_reached"])
        self.assertGreaterEqual(raw["diagnostics"]["editors"], 1)

        publish = [e for e in self._by_name(raw, "Post") if e.get("role") == "button"]
        self.assertEqual(len(publish), 1, "the publish button should be discovered")
        self.assertTrue(publish[0]["in_editor_region"])
        self.assertTrue(publish[0]["with_editor"])
        self.assertEqual(publish[0]["context"], "active composer")

        toolbar = [e for e in self._by_name(raw, "Add image") if e.get("role") == "button"]
        self.assertEqual(len(toolbar), 1)
        self.assertTrue(toolbar[0]["in_editor_region"])
        self.assertFalse(
            toolbar[0].get("with_editor"),
            "a secondary toolbar control is not the composer's submit",
        )

        editor = [e for e in self._by_name(raw, "Post text") if e.get("editable")]
        self.assertEqual(len(editor), 1)
        self.assertTrue(editor[0]["in_editor_region"])

    def test_the_same_named_sidebar_link_is_discovered_and_placed(self):
        raw = self.discovery()

        links = [e for e in self._by_name(raw, "Post") if e.get("role") == "link"]
        self.assertEqual(len(links), 1)
        self.assertFalse(links[0]["in_editor_region"])
        self.assertFalse(links[0].get("with_editor"))
        self.assertEqual(links[0]["context"], "navigation")

    def test_both_controls_survive_ranking_with_distinguishable_context(self):
        # Feed the discovered map through the real ranking/formatting and prove
        # the button is not crowded out and that the two "Post" controls read
        # apart: the link as navigation, the button as the composer's submit.
        mapped = build_ui_map(self.discovery())
        self.assertGreater(mapped["diagnostics"]["raw_visible"], 20)
        self.assertGreater(mapped["diagnostics"]["visible_omitted_by_cap"], 0)
        posts = [e for e in mapped["visible"] if e["name"] == "Post"]
        self.assertEqual({e["role"] for e in posts}, {"link", "button"})
        self.assertEqual(len({e["id"] for e in posts}), 2)

        button = next(e for e in posts if e["role"] == "button")
        link = next(e for e in posts if e["role"] == "link")
        self.assertTrue(button["with_editor"])
        self.assertFalse(link.get("with_editor"))

        text = format_ui_map(mapped)
        self.assertIn(f'[{link["id"]}] link "Post"', text)
        self.assertIn(f'[{button["id"]}] button "Post"', text)
        self.assertIn("Context: navigation", text)
        self.assertIn("Context: active composer", text)
        self.assertIn("Relationship: Associated with the editor", text)
        self.assertNotIn("Action: Submit", text)


if __name__ == "__main__":
    unittest.main()
