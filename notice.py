"""Small always-on-top countdown. Timeout starts Granola. Cancel or Esc does not."""

from __future__ import annotations

import argparse
import ctypes
import tkinter as tk

BG = "#1f2933"
FG = "#f5f7fa"
MUTED = "#d6deeb"
ACCENT = "#7d9a6a"
HINT = "#f0c674"


def show_notice(title: str, seconds: int, *, kind: str = "calendar", auto: str | None = None) -> bool:
    """Return True when the countdown finishes, False when the user cancels."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=BG)
    accepted = {"value": False, "done": False}

    outer = tk.Frame(root, bg="#4b5563")
    outer.pack(fill="both", expand=True)
    inner = tk.Frame(outer, bg=BG)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    tk.Frame(inner, bg=ACCENT, width=6).pack(side="left", fill="y")
    body = tk.Frame(inner, bg=BG)
    body.pack(side="left", fill="both", expand=True, padx=14, pady=12)

    count = tk.StringVar()
    heading = tk.Label(body, textvariable=count, bg=BG, fg=FG, font=("Segoe UI", 14, "bold"), anchor="w")
    heading.pack(fill="x")
    tk.Label(
        body,
        text=f"for {title}",
        bg=BG,
        fg=MUTED,
        font=("Segoe UI", 12),
        anchor="w",
        wraplength=320,
        justify="left",
    ).pack(fill="x", pady=(2, 0))
    if kind == "crash":
        tk.Label(
            body,
            text="Granola closed. Starting again unless you cancel.",
            bg=BG,
            fg=HINT,
            font=("Segoe UI", 9),
            anchor="w",
            wraplength=320,
            justify="left",
        ).pack(fill="x", pady=(6, 0))

    def finish(start: bool) -> None:
        if accepted["done"]:
            return
        accepted["done"] = True
        accepted["value"] = start
        root.destroy()

    tk.Button(
        body,
        text="Cancel",
        command=lambda: finish(False),
        bg="#f5f7fa",
        fg="#1f2933",
        activebackground="#e5e7eb",
        relief="flat",
        padx=14,
        pady=4,
        font=("Segoe UI", 10),
        cursor="hand2",
    ).pack(anchor="e", pady=(10, 0))

    remaining = {"n": max(1, seconds)}

    def tick() -> None:
        if accepted["done"]:
            return
        if remaining["n"] <= 0:
            finish(True)
            return
        count.set(f"Granola starting in {remaining['n']}s")
        remaining["n"] -= 1
        root.after(1000, tick)

    root.bind("<Escape>", lambda _event: finish(False))
    root.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    width, height = 390, 148 if kind == "crash" else 124
    root.update_idletasks()
    x = root.winfo_screenwidth() - width - 24
    root.geometry(f"{width}x{height}+{x}+24")
    root.deiconify()
    root.lift()
    root.after(50, root.focus_force)
    tick()
    if auto == "cancel":
        root.after(200, lambda: finish(False))
    elif auto == "start":
        root.after(200, lambda: finish(True))
    root.mainloop()
    return bool(accepted["value"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Show the Granola start countdown")
    parser.add_argument("--title", required=True)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--kind", default="calendar")
    parser.add_argument("--auto", choices=("cancel", "start"))
    args = parser.parse_args()
    try:
        started = show_notice(args.title, args.seconds, kind=args.kind, auto=args.auto)
    except Exception:
        return 2
    return 0 if started else 1


if __name__ == "__main__":
    raise SystemExit(main())
