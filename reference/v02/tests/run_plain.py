import importlib.util, sys, traceback, pathlib
failed = 0
for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    for name in dir(mod):
        if name.startswith("test_"):
            try:
                getattr(mod, name)(); print("PASS", path.name, name)
            except Exception:
                failed += 1; print("FAIL", path.name, name); traceback.print_exc()
print("failed:", failed); sys.exit(1 if failed else 0)
