"""Freeze target.

PyInstaller runs its entry script as __main__ with no package context, so the
package's own __main__.py cannot be used directly - its relative imports have no
parent. Importing through the package name keeps them valid.
"""
from triarch_patcher.__main__ import main

if __name__ == "__main__":
    main()
