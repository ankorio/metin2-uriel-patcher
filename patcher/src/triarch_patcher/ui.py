"""A log window. Deliberately plain: the point is to watch a step fail."""
import os
import queue
import threading
import tkinter as tk
from tkinter import scrolledtext

from . import pipeline

COLOURS = {"step": "#4ea1ff", "ok": "#3fbf5f", "fail": "#ff5c5c",
           "info": "#d0d0d0", "head": "#e8c04a", "nudge": "#ffd24a"}


class App(object):
    def __init__(self, folder, live=False):
        self.folder = folder
        self.live = live
        self.q = queue.Queue()
        self._belled = False
        self.root = tk.Tk()
        self.root.title("Triarch patcher")
        self.root.geometry("880x520")
        self.root.configure(bg="#1e1e1e")

        self.txt = scrolledtext.ScrolledText(
            self.root, bg="#141414", fg="#d0d0d0", insertbackground="#d0d0d0",
            font=("Consolas", 10), wrap="none", borderwidth=0)
        self.txt.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        for k, v in COLOURS.items():
            self.txt.tag_config(k, foreground=v)
        self.txt.configure(state="disabled")

        bar = tk.Frame(self.root, bg="#1e1e1e")
        bar.pack(fill="x", padx=8, pady=(0, 8))
        self.status = tk.Label(bar, text="working...", bg="#1e1e1e", fg="#d0d0d0",
                               font=("Consolas", 10))
        self.status.pack(side="left")
        self.btn = tk.Button(bar, text="Close", command=self.root.destroy,
                             state="disabled", width=12)
        self.btn.pack(side="right")

    def log(self, msg, level="info"):
        self.q.put((msg, level))

    def _drain(self):
        try:
            while True:
                msg, level = self.q.get_nowait()
                self.txt.configure(state="normal")
                self.txt.insert("end", msg + "\n", level)
                self.txt.see("end")
                self.txt.configure(state="disabled")
                # The log scrolls; a step that needs the operator would scroll
                # away unnoticed, so mirror it in the status bar.
                if level == "nudge":
                    self._attention()
                elif level == "step":
                    self._belled = False
                    self.status.config(text="working...", fg=COLOURS["info"])
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _attention(self):
        self.status.config(text="ACTION NEEDED: click the game window and move "
                                "the mouse", fg=COLOURS["nudge"])
        # One banner is ~15 nudge lines; ring once per step, not once per line.
        if not self._belled:
            self._belled = True
            try:
                self.root.bell()
            except tk.TclError:
                pass

    def _worker(self):
        ok = pipeline.run(self.folder, self.log, live=self.live)
        self.q.put(("", "info"))
        self.root.after(150, lambda: self._finish(ok))

    def _finish(self, ok):
        self.status.config(text="SUCCESS - patch complete" if ok else "FAILED - see log above",
                           fg=COLOURS["ok"] if ok else COLOURS["fail"])
        self.btn.config(state="normal")

    def run(self):
        self.log("Triarch patcher - folder: %s" % self.folder, "head")
        self.log("", "info")
        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(80, self._drain)
        self.root.mainloop()


def main(folder=None, live=False):
    folder = folder or os.getcwd()
    try:
        App(folder, live=live).run()
    except Exception as e:                     # no window station / no display
        from . import pipeline
        os.makedirs(os.path.join(folder, "_patcher"), exist_ok=True)
        path = os.path.join(folder, "_patcher", "patcher_run.log")
        with open(path, "w") as fh:
            fh.write("GUI unavailable (%s: %s) - running headless\n\n" % (type(e).__name__, e))
            def log(m, lvl="info"):
                fh.write(m + "\n")
                fh.flush()
            pipeline.run(folder, log, live=live)
        print("GUI unavailable; wrote %s" % path)
