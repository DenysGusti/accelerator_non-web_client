from pathlib import Path

wdrv_path = Path("/mnt/wdrv")
print(f"=== Starting scan on {wdrv_path} ===")

found_any = False
if wdrv_path.exists():
    for fpath in wdrv_path.rglob("*"):
        if fpath.is_file():
            found_any = True
            print(f"\n--- Reading first 5 rows of: {fpath} ---")
            try:
                with fpath.open("r", encoding="utf-8", errors="replace") as f:
                    for i in range(5):
                        line = f.readline()
                        if not line:
                            break
                        print(f"[{i+1}] {line.rstrip()}")
            except Exception as e:
                print(f"Error reading {fpath}: {e}")
    if not found_any:
        print(f"No files found inside {wdrv_path}.")
else:
    print(f"Directory {wdrv_path} not found.")

print("\n=== End program ===")
