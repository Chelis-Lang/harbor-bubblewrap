#!/bin/bash
set -euo pipefail

mkdir -p /app/repo
cd /app/repo
git init -q
printf 'hello\n' > README.md
git add README.md
git -c user.name=Oracle -c user.email=oracle@example.invalid commit -q -m 'Initial commit'
