"""抽取 index.html 的内联 <script> 体，写到临时 .js 供 node --check 做语法检查。"""
import re
import sys
from pathlib import Path

html = Path(sys.argv[1]).read_text(encoding="utf-8")
# 只取 <script> ... </script>（无 src 属性的那段内联脚本）
m = re.search(r"<script>\s*(.*?)\s*</script>", html, re.S)
if not m:
    print("NO_INLINE_SCRIPT")
    sys.exit(2)
out = Path(sys.argv[2])
out.write_text(m.group(1), encoding="utf-8")
print(f"EXTRACTED {len(m.group(1))} chars")
