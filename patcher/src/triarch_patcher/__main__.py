"""Entry point. Drop the exe in the game folder and run it."""
import os
import sys


def _folder():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return os.path.abspath(args[0])
    # frozen exe: the folder it was dropped into, not a temp extraction dir
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.getcwd()


def main():
    folder = _folder()
    live = "--live" in sys.argv          # original method: launch + harvest from memory
    if "--console" in sys.argv:
        from . import pipeline
        ok = pipeline.run(folder, lambda m, l="info": print(m), live=live)
        sys.exit(0 if ok else 1)
    from . import ui
    ui.main(folder, live=live)


if __name__ == "__main__":
    main()
