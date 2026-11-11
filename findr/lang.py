"""The language of what findr prints on the command line: Korean or English.
    lang()     -> 'ko' or 'en'
    L(ko, en)  -> the sentence in that language
Order: FINDR_LANG (ko / en), then the system's language (LC_ALL, LC_MESSAGES, LANG; on Windows the display language),
and English for everyone else. Asked on every call (cheap), so a change of FINDR_LANG is seen at once.
If the language cannot be read at all, Korean: what findr printed before it spoke two languages."""
import os

_NT = os.name == "nt"


def _windows_korean():
    """Windows without LANG and the like: the display language (primary language 0x12 is Korean), else the locale's name"""
    try:
        import ctypes
        return (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x12
    except Exception:   # noqa: BLE001
        import locale
        name = (locale.getlocale()[0] or "").lower()   # 'ko_KR' or 'Korean_Korea'
        return name.startswith("ko")


def _system():
    for k in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = (os.environ.get(k) or "").strip().lower()
        if v and v.split(".")[0] not in ("c", "posix"):   # 'C' says nothing about the person's language
            return "ko" if v.startswith("ko") else "en"
    if _NT:
        return "ko" if _windows_korean() else "en"
    return "en"


def lang():
    try:
        v = (os.environ.get("FINDR_LANG") or "").strip().lower()[:2]
        return v if v in ("ko", "en") else _system()
    except Exception:   # noqa: BLE001
        return "ko"


def L(ko, en):
    return en if lang() == "en" else ko
