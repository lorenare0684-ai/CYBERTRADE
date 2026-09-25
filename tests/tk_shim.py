"""A permissive fake ``tkinter`` so the GUI panels can be tested headless.

The desktop terminal needs tkinter, which CI and headless sandboxes do not
have. Rather than skip the panels entirely, this shim installs just enough of
the toolkit for a panel to be constructed and driven: widgets are inert
objects that record their calls, and ``StringVar`` really holds its value.

Usage::

    with fake_tk():
        from cybertrade.gui.panels import ConnectionPanel
        ...

Import ``cybertrade.gui.*`` only *inside* the context manager — the modules
bind ``tkinter`` at import time.
"""

from __future__ import annotations

import contextlib
import sys
import types
from typing import Any, Dict, List, Optional


class _Widget:
    """Inert stand-in for any Tk widget: absorbs every call, records state."""

    def __init__(self, master=None, *args: Any, **kwargs: Any) -> None:
        self.master = master
        self.kwargs = kwargs
        self.children: List["_Widget"] = []
        self.calls: List[tuple] = []
        self._label = kwargs.get("text", "")
        self._color = kwargs.get("color", "")
        if master is not None and hasattr(master, "children"):
            master.children.append(self)

    # -- option / menu-style access ----------------------------------------
    def __getitem__(self, key: str) -> Any:
        if key not in self.kwargs:
            self.kwargs[key] = _Widget(self)
        return self.kwargs[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.kwargs[key] = value

    def add_command(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("add_command", kwargs.get("label", "")))

    def index(self, *args: Any) -> int:
        return len([c for c in self.calls if c[0] == "add_command"])

    # -- geometry / lifecycle ---------------------------------------------
    def grid(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("grid", args, kwargs))

    def pack(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("pack", args, kwargs))

    def pack_forget(self) -> None:
        self.calls.append(("pack_forget",))

    def grid_forget(self) -> None:
        self.calls.append(("grid_forget",))

    def place(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("place", args, kwargs))

    def destroy(self) -> None:
        self.calls.append(("destroy",))

    def bind(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("bind", args))

    def config(self, **kwargs: Any) -> None:
        self.calls.append(("config", kwargs))
        if "text" in kwargs:
            self._label = kwargs["text"]
        if "fg" in kwargs:
            self.kwargs["fg"] = kwargs["fg"]

    configure = config

    def after(self, ms: int, fn=None, *args: Any) -> str:
        self.calls.append(("after", ms))
        if fn is not None:
            fn(*args)
        return "after#0"

    def after_cancel(self, _id: str) -> None:
        self.calls.append(("after_cancel",))

    def winfo_width(self) -> int:
        return 1280

    def winfo_height(self) -> int:
        return 800

    def winfo_screenwidth(self) -> int:
        return 1920

    def winfo_screenheight(self) -> int:
        return 1080

    # -- canvas drawing ----------------------------------------------------
    def create_rectangle(self, *args: Any, **kwargs: Any) -> int:
        self.calls.append(("create_rectangle",))
        return 1

    def create_text(self, *args: Any, **kwargs: Any) -> int:
        return 2

    def create_line(self, *args: Any, **kwargs: Any) -> int:
        return 3

    def create_oval(self, *args: Any, **kwargs: Any) -> int:
        return 4

    def create_polygon(self, *args: Any, **kwargs: Any) -> int:
        return 5

    def create_arc(self, *args: Any, **kwargs: Any) -> int:
        return 6

    def delete(self, *args: Any) -> None:
        self.calls = [c for c in self.calls if c[0] != "add_command"]

    def itemconfigure(self, *args: Any, **kwargs: Any) -> None:
        pass

    def bbox(self, *args: Any) -> Optional[tuple]:
        return (0, 0, 10, 10)

    # -- toplevel behaviour ------------------------------------------------
    def title(self, text: str = "") -> None:
        self.kwargs["title"] = text

    def geometry(self, spec: str = "") -> None:
        self.kwargs["geometry"] = spec

    def minsize(self, w: int = 0, h: int = 0) -> None:
        self.kwargs["minsize"] = (w, h)

    def maxsize(self, w: int = 0, h: int = 0) -> None:
        self.kwargs["maxsize"] = (w, h)

    def resizable(self, w: bool = True, h: bool = True) -> None:
        self.kwargs["resizable"] = (w, h)

    def protocol(self, name: str, fn) -> None:
        self.kwargs.setdefault("protocols", {})[name] = fn

    def mainloop(self, n: int = 0) -> None:
        """Never block: a headless test has no event loop to run."""
        self.calls.append(("mainloop",))

    def quit(self) -> None:
        self.calls.append(("quit",))

    def attributes(self, *args: Any, **kwargs: Any) -> None:
        pass

    def focus_force(self) -> None:
        pass

    def lift(self) -> None:
        pass

    # -- introspection the panels use --------------------------------------
    def winfo_toplevel(self) -> "_Widget":
        return self

    def nametowidget(self, _name: str) -> "_Widget":
        return self

    def tk(self) -> "_Call":
        return _Call()


class _Call:
    def call(self, *args: Any) -> Any:
        return 1.0


class _Var:
    """A StringVar that actually stores its value."""

    def __init__(self, master: Any = None, value: str = "", **kwargs: Any) -> None:
        self._value = value
        self.trace_calls: List[tuple] = []

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value

    def trace_add(self, mode: str, fn) -> None:
        self.trace_calls.append((mode, fn))


class _MessageBox:
    """Records dialog invocations so tests can assert on them."""

    def __init__(self) -> None:
        self.shown: List[Dict[str, Any]] = []

    def askyesno(self, title: str = "", message: str = "", **kw: Any) -> bool:
        self.shown.append({"kind": "askyesno", "title": title, "message": message})
        return True

    def askokcancel(self, title: str = "", message: str = "", **kw: Any) -> bool:
        self.shown.append({"kind": "askokcancel", "title": title,
                           "message": message})
        return True

    def askquestion(self, title: str = "", message: str = "", **kw: Any) -> str:
        self.shown.append({"kind": "askquestion", "title": title,
                           "message": message})
        return "yes"

    def askretrycancel(self, title: str = "", message: str = "", **kw: Any) -> bool:
        self.shown.append({"kind": "askretrycancel", "title": title,
                           "message": message})
        return True

    def showinfo(self, title: str = "", message: str = "", **kw: Any) -> None:
        self.shown.append({"kind": "showinfo", "title": title, "message": message})

    def showerror(self, title: str = "", message: str = "", **kw: Any) -> None:
        self.shown.append({"kind": "showerror", "title": title, "message": message})

    def showwarning(self, title: str = "", message: str = "", **kw: Any) -> None:
        self.shown.append({"kind": "showwarning", "title": title,
                           "message": message})


MESSAGEBOX = _MessageBox()


def _build_module() -> types.ModuleType:
    tk = types.ModuleType("tkinter")
    for name in ("Frame", "Tk", "Toplevel", "Canvas", "Label", "Entry",
                 "Radiobutton", "Button", "Checkbutton", "Text", "Listbox",
                 "Scrollbar", "Menu", "PanedWindow", "Scale", "Spinbox",
                 "Message", "OptionMenu"):
        setattr(tk, name, type(name, (_Widget,), {}))
    tk.Misc = _Widget
    tk.Widget = _Widget
    tk.StringVar = _Var
    tk.IntVar = _Var
    tk.DoubleVar = _Var
    tk.BooleanVar = _Var
    tk.END = "end"
    tk.INSERT = "insert"
    tk.LEFT = "left"
    tk.RIGHT = "right"
    tk.TOP = "top"
    tk.BOTTOM = "bottom"
    tk.BOTH = "both"
    tk.X = "x"
    tk.Y = "y"
    tk.NONE = "none"
    tk.N = "n"
    tk.S = "s"
    tk.E = "e"
    tk.W = "w"
    tk.NSEW = "nsew"
    tk.HORIZONTAL = "horizontal"
    tk.VERTICAL = "vertical"
    tk.DISABLED = "disabled"
    tk.NORMAL = "normal"
    tk.SUNKEN = "sunken"
    tk.FLAT = "flat"
    tk.RAISED = "raised"
    tk.GROOVE = "groove"
    tk.CENTER = "center"
    tk.font = types.SimpleNamespace(nametofont=lambda *a, **kw: _Widget())
    tk.TclError = type("TclError", (Exception,), {})
    tk._default_root = None

    mbox = types.ModuleType("tkinter.messagebox")
    for name in ("askyesno", "askokcancel", "askquestion", "askretrycancel",
                 "showinfo", "showerror", "showwarning"):
        setattr(mbox, name, getattr(MESSAGEBOX, name))
    tk.messagebox = mbox

    ttk = types.ModuleType("tkinter.ttk")
    ttk.Frame = type("Frame", (_Widget,), {})
    ttk.Style = type("Style", (_Widget,), {})
    tk.ttk = ttk
    return tk


@contextlib.contextmanager
def fake_tk():
    """Install the shim as ``tkinter`` for the duration of the block."""
    module = _build_module()
    saved = {name: sys.modules.get(name) for name in
             ("tkinter", "tkinter.messagebox", "tkinter.ttk")}
    sys.modules["tkinter"] = module
    sys.modules["tkinter.messagebox"] = module.messagebox
    sys.modules["tkinter.ttk"] = module.ttk
    # drop any cybertrade.gui module that bound the real (missing) tkinter
    dropped = [name for name in list(sys.modules)
               if name.startswith("cybertrade.gui")]
    for name in dropped:
        del sys.modules[name]
    try:
        yield module
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        for name in dropped:
            sys.modules.pop(name, None)
