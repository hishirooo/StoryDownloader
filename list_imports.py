#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import ast, os, sys
from pathlib import Path

# --- Fallback stdlib set (dùng khi sys.stdlib_module_names không có) ---
FALLBACK_STDLIB = {
    # builtins thường gặp
    *sys.builtin_module_names,
    # các package stdlib phổ biến
    "argparse","asyncio","base64","bz2","collections","concurrent","contextlib","csv",
    "dataclasses","datetime","decimal","fractions","functools","glob","gzip","hashlib",
    "heapq","html","http","importlib","io","itertools","json","logging","lzma","math",
    "multiprocessing","numbers","operator","os","pathlib","pickle","platform","plistlib",
    "queue","random","re","sched","secrets","select","selectors","shutil","signal","socket",
    "sqlite3","ssl","statistics","string","struct","subprocess","sys","sysconfig","tempfile",
    "textwrap","threading","time","typing","types","unittest","urllib","uuid","venv",
    "warnings","weakref","xml","xmlrpc","zipfile","zoneinfo","email","traceback"
}

# Map module -> distribution tên chuẩn trên PyPI
ALIAS_DIST = {
    "bs4": "beautifulsoup4",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
}

def stdlib_names() -> set[str]:
    # Python 3.10+ có danh sách stdlib chính xác
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names) | set(sys.builtin_module_names)
    return set(FALLBACK_STDLIB)

def extract_imports_from_code(code: str) -> set[str]:
    out = set()
    try:
        tree = ast.parse(code)
    except Exception:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                top = (n.name or "").split(".")[0]
                if top: out.add(top)
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0):  # bỏ import tương đối: from .x import y
                continue
            mod = (node.module or "").split(".")[0] if node.module else ""
            if mod: out.add(mod)
    return out

def extract_project_imports(root: Path) -> set[str]:
    mods = set()
    for cur, _, files in os.walk(root):
        for fn in files:
            if fn.endswith(".py"):
                p = Path(cur) / fn
                try:
                    code = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                mods |= extract_imports_from_code(code)
    return mods

def is_local_module(root: Path, name: str) -> bool:
    # module nội bộ project? (./name.py hoặc ./name/__init__.py)
    return (root / f"{name}.py").is_file() or (root / name / "__init__.py").is_file()

def main():
    root = Path(".").resolve()  # thư mục hiện tại
    mods = extract_project_imports(root)

    std = stdlib_names()
    externals = set()

    for m in mods:
        # bỏ stdlib
        if m in std or m == "":
            continue
        # bỏ module nội bộ project
        if is_local_module(root, m):
            continue
        # coi những module không rõ là "ngoài" (để không bỏ sót)
        externals.add(m)

    # chuyển alias -> distribution đẹp
    reqs = []
    for m in sorted(externals):
        reqs.append(ALIAS_DIST.get(m, m))

    print("📦 Thư viện cài thêm (theo module top-level):")
    for m in sorted(externals):
        dist = ALIAS_DIST.get(m, "")
        print(f"- {m}" + (f"  (dist: {dist})" if dist else ""))

    # ghi requirements.txt
    out = root / "requirements.txt"
    out.write_text("\n".join(sorted(set(reqs))) + ("\n" if reqs else ""), encoding="utf-8")
    print(f"\n✅ Đã ghi {out}")

if __name__ == "__main__":
    main()
