import re
# minimal test
for s in ["#define X", "//#define X", "// #define X"]:
    for p in [r"^//?\s*#define\s+X", r"^(?://)?\s*#define\s+X", r"^(?:/{0,2})\s*#define\s+X"]:
        m = re.search(p, s)
        print(f"{s!r:25s} {p!r:40s} -> {bool(m)}")
