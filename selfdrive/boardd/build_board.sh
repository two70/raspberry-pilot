#!/bin/bash
# This must be run from inside the pipenv!
cd ~/raspilot/selfdrive/boardd
make clean
PYTHONPATH=~/raspilot make
