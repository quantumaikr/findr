"""Text that findr writes to a pipe or a file on Windows, in the encoding the reader expects (#66-10).
A console shows Unicode whatever the code page, but piped output is bytes, and readers disagree on how to decode them:
  - PowerShell and cmd read a program's piped output in the console's output code page (949 on Korean Windows, 65001
    after 'chcp 65001'). UTF-8 there showed as mojibake.
  - Programs with no console window read UTF-8: the findr app (CREATE_NO_WINDOW), AI assistants and editors running
    findr, Git Bash. Python would otherwise write the ANSI code page there, and fail on characters outside it.
So: a console window -> its output code page; none -> UTF-8; characters the code page lacks become '?', never an error.
An explicit PYTHONIOENCODING is kept, MCP's channel is always UTF-8 (its library writes bytes), and the choice is passed to
the indexer and other helpers started from here through PYTHONIOENCODING. Not Windows, or a console: nothing changes."""
import os
import sys


def _console_cp():
    """the console's output code page when this process has a console window, else 0"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        if not k.GetConsoleWindow():
            return 0
        return int(k.GetConsoleOutputCP())
    except Exception:
        return 0


def pipe_encoding(console_cp=None):
    """the encoding for piped output: 'utf-8', or a code page name such as 'cp949'"""
    cp = _console_cp() if console_cp is None else console_cp
    if not cp or cp == 65001:
        return "utf-8"
    name = f"cp{cp}"
    try:
        "".encode(name)
    except LookupError:   # a code page Python does not know: UTF-8 is the better guess
        return "utf-8"
    return name


def fit(streams=None, console_cp=None, environ=None, windows=None):
    """set the encoding of piped standard output and error; returns the encoding chosen, or None when nothing changed"""
    env = os.environ if environ is None else environ
    if not (os.name == "nt" if windows is None else windows) or env.get("PYTHONIOENCODING"):
        return None
    streams = (sys.stdout, sys.stderr) if streams is None else streams
    piped = [s for s in streams if s is not None and hasattr(s, "reconfigure") and not _tty(s)]
    if not piped:
        return None
    enc = pipe_encoding(console_cp)
    for s in piped:
        try:
            s.reconfigure(encoding=enc, errors="replace")
        except Exception:   # a stream that cannot change: left as it is
            pass
    env["PYTHONIOENCODING"] = f"{enc}:replace"   # the indexer and helpers started from here write the same
    return enc


def _tty(s):
    try:
        return s.isatty()
    except Exception:
        return False
