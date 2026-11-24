"""RsHub launcher.  Usage: pythonw run.py   (--debug opens the developer tools)"""
import multiprocessing
import os
import sys

if __name__ == "__main__":
    # The storage scan reads the NTFS table with worker processes. In the installed exe a worker is started as
    # "RsHub.exe --multiprocessing-fork ...": freeze_support() turns it into a worker and exits, instead of
    # letting it boot a whole second RsHub (which made copies pile up and the window reopen by itself).
    multiprocessing.freeze_support()

if sys.platform != "win32":
    sys.exit("RsHub only runs on Windows.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rshub.app import main  # noqa: E402

if __name__ == "__main__":
    main()
