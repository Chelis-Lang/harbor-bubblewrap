#!/bin/bash
set -euo pipefail

cat > /app/fib.py <<'EOF'
first, second = 0, 1
for _ in range(10):
    print(first)
    first, second = second, first + second
EOF
