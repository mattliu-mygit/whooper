#!/usr/bin/env bash
# Removes what setup.example.sh started. bin/whooper-review runs it when the review ends, finished
# or not, with the same WHOOPER_* variables, so it must succeed when setup did nothing.
docker rm --force "whooper-$WHOOPER_RUN_ID-postgres" >/dev/null 2>&1 || true
