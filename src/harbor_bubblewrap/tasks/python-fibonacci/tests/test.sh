#!/bin/bash
# Reward 1 only if fib.py prints the first ten Fibonacci numbers, one on each line.

mapfile -t lines < <(python3 /app/fib.py 2>/dev/null)
if [ "${lines[*]}" = "0 1 1 2 3 5 8 13 21 34" ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
