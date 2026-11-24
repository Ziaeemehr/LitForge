"""Every menu-bar command of the desktop window is one the page knows (window.LFMenu in app.js)."""
import re
from importlib import resources

from litforge.desktop import host


def test_menu_commands_are_handled_by_the_page():
    js = (resources.files("litforge.ui") / "static" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("var GO = ["):]
    body = body[:body.index("\n    };")]
    known = set(re.findall(r"'([a-z]+)'", body))
    commands = {item[1] for _, items in host.MENUS for item in items if item}
    assert commands <= known, commands - known
