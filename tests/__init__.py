import os
import tempfile

# The tests never write to C:\ProgramData\RsHub: they use their own data folder.
os.environ.setdefault("RSHUB_DATA", os.path.join(tempfile.gettempdir(), "rshub_testdata"))

# Engine messages are compared in English: pin the language whatever Windows uses.
os.environ.setdefault("RSHUB_LANG", "en")
