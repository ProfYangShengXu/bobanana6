import os
import subprocess

snap_dir = os.path.join(os.getcwd(), "_run", "snapshots")
print(f"Looking for snapshots in: {snap_dir}")

try:
    result = subprocess.run(["dir", snap_dir], capture_output=True, text=True, timeout=10)
    print(result.stdout)
except Exception as e:
    print(f"Error: {e}")

# 或者直接用 ls
try:
    result = subprocess.run(["ls", "-la", snap_dir], capture_output=True, text=True, timeout=10)
    print(result.stdout)
except Exception as e:
    print(f"Error: {e}")