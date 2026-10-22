#!/bin/bash
# Reward 1 only if the world recorded a correct answer.

if python3 /tests/check.py /evidence/answers.jsonl; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
