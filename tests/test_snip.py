"""search.snip: where the snippet starts in the source, and never a cut inside a number"""
from findr.core.search import snip, snip_at

NUMS = ("1,234,567", "010-1234-5678", "2026.11.14")


def test_start_offset_points_into_the_source():
    t = "alpha beta " * 20 + "needle here " + "tail " * 60
    s, a = snip_at(t, ["needle"])
    assert a > 0 and s.startswith("…")
    assert t[a:a + 5] == s[1:6]   # the first source character after the ellipsis
    assert snip(t, ["needle"]) == s


def test_start_zero_for_short_text():
    assert snip_at("short text", ["text"]) == ("short text", 0)
    assert snip_at("", ["x"]) == ("", 0)


def test_start_never_inside_a_number():
    for num in NUMS:
        for cut in range(1, len(num)):   # the plain cut would start `cut` characters into the number
            pre = "w " * 10 + num[:0]
            start = len(pre)
            t = pre + num + " " + "z" * 80
            pos = start + cut + 40
            t = t[:pos] + "needle" + t[pos:] if pos < len(t) else t + "needle"
            s, a = snip_at(t, ["needle"])
            assert a == start, (num, cut, a)
            assert s[1:1 + len(num)] == num


def test_end_never_inside_a_number():
    for num in NUMS:
        for cut in range(1, len(num)):
            t = "needle " + "d" * (160 - 7 - cut) + num + " tail " * 5
            s, a = snip_at(t, ["needle"])
            assert a == 0 and num in s, (num, cut, s[-20:])
            assert s.rstrip("…").endswith(num)


def test_offset_survives_folded_whitespace():
    t = "a \n\n\t b " * 30 + "needle 2026.11.14 end"
    s, a = snip_at(t, ["needle"])
    assert t[a:].startswith(s.strip("…").split(" ")[0])
