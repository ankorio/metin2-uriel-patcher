"""Entry point. Drop the exe in the game folder and run it."""
import os
import sys

USAGE = """usage: TriarchPatcher.exe [folder] [--console] [--live]
       python -m triarch_patcher <folder> [--console] [--live]

  folder      the game folder (the one holding triarch.exe). The frozen exe
              defaults to the folder it was dropped into; from source it is
              required.
  --console   run the pipeline in this terminal instead of the Tk window
  --live      original method: launch the game and harvest from memory
  -h, --help  show this text and exit"""

FLAGS = ("--console", "--live")


def _folder():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        return os.path.abspath(args[0])
    # frozen exe: the folder it was dropped into, not a temp extraction dir
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # From source there is no sensible default: falling back to cwd once ran
    # the whole pipeline against a repo checkout because of a mistyped flag.
    sys.stderr.write(USAGE + "\nerror: a game folder is required when run from source\n")
    sys.exit(2)


def main():
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(USAGE)
        sys.exit(0)
    unknown = [a for a in sys.argv[1:] if a.startswith("-") and a not in FLAGS]
    if unknown:
        # an unrecognised flag must not silently run the pipeline
        sys.stderr.write(USAGE + "\nerror: unknown option(s): %s\n" % " ".join(unknown))
        sys.exit(2)
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
