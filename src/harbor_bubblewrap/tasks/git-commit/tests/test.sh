#!/bin/bash
# Reward 1 only if /app/repo has one commit, "Initial commit", that adds only
# README.md with the line "hello".

repo=/app/repo
count="$(git -C "$repo" rev-list --count HEAD 2>/dev/null)"
message="$(git -C "$repo" log -1 --format=%s 2>/dev/null)"
files="$(git -C "$repo" ls-tree --name-only HEAD 2>/dev/null)"
readme="$(git -C "$repo" show HEAD:README.md 2>/dev/null)"
if [ "$count" = 1 ] && [ "$message" = "Initial commit" ] && [ "$files" = README.md ] && [ "$readme" = hello ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
