#!/usr/bin/env bash
set -e
cd /root/scal
if [ -d "venv" ]; then
  . venv/bin/activate
fi
exec /root/scal/venv/bin/python -u /root/scal/scal_main.py
