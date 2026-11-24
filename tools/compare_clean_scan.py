"""Runs the cleanup scan twice, the way it was before the speed-up (git ref, default HEAD~0 = the committed
cleaner before this change; pass --ref) and now, and checks that both report the same categories, sizes and file
counts (tiny drift is allowed: files change between runs). Read-only. Usage: python tools/compare_clean_scan.py [--ref REF]"""
import importlib
import pathlib
import subprocess
import sys
import time

def main():
    global ROOT
    ROOT = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))
    ref = sys.argv[sys.argv.index("--ref") + 1] if "--ref" in sys.argv else "HEAD"
    old_file = ROOT / "rshub" / "engine" / "removal" / "_cleaner_old.py"
    src = subprocess.check_output(["git", "show", f"{ref}:rshub/engine/removal/cleaner.py"], cwd=ROOT)
    old_file.write_bytes(src)
    try:
        from rshub.engine.removal import cleaner as new
        old = importlib.import_module("rshub.engine.removal._cleaner_old")

        def run(mod):
            t = time.perf_counter()
            r = mod.scan(days=7, exclude=())
            return time.perf_counter() - t, {i["id"]: i for i in r}

        run(new)  # warm the file-system cache so neither side is favoured
        ta, a = run(old)
        tb, b = run(new)
        tc, c = run(old)
        td, d = run(new)
        bad = 0
        assert a.keys() == b.keys(), "different categories"
        for k in a:
            for x, y in ((a[k], b[k]), (c[k], d[k])):
                ds, dc = abs(x["size"] - y["size"]), abs(x["count"] - y["count"])
                tol = max(2_000_000, x["size"] * 0.002)
                ok = ds <= tol and dc <= max(20, x["count"] * 0.002) and x.get("keptCount") == y.get("keptCount") or \
                    (ds <= tol and dc <= max(20, x["count"] * 0.002) and abs(x.get("keptCount", 0) - y.get("keptCount", 0)) <= 3)
                st = lambda i: sorted((r["path"], r["status"]) for r in i.get("rows", []))
                if st(x) != st(y):
                    same = {p: s for p, s in st(x)}
                    diff = [(p, same.get(p), s) for p, s in st(y) if same.get(p) != s]
                    ok = ok and len(diff) <= 3
                if not ok:
                    bad += 1
                    print("DIFF", k, x["size"], y["size"], x["count"], y["count"])
            print(f"{k:14s} size {a[k]['size']:>14,} / {b[k]['size']:>14,}  files {a[k]['count']:>7} / {b[k]['count']:>7}")
        print(f"old {ta:.2f}s {tc:.2f}s   new {tb:.2f}s {td:.2f}s")
        sys.exit(1 if bad else 0)
    finally:
        old_file.unlink(missing_ok=True)


if __name__ == "__main__":   # the helper processes of the scan re-import this file
    main()
