#!/bin/sh
# recut.sh <upstream checkout> <candidate dir>
# Cut with vignette.py, finish with upstream's finish.py, hold colour with tone.py, then
# run qa.py's pre-flight checks (exit 1 on a fault; see lost.jpg and qa.json).
# Runs in upstream's locked environment (.fugart/upstream-env), which has numpy.
# Leaves the scripts in the candidate so `fugart replace` files the recipe with the cut.
set -e
here=$(cd "$(dirname "$0")" && pwd)
py="$here/../.fugart/upstream-env/bin/python"
upstream=$(cd "$1" && pwd)
d=$(cd "$2" && pwd)
cp "$here/vignette.py" "$here/tone.py" "$here/qa.py" "$d/"
"$py" -B "$d/vignette.py" "$d"
"$py" -B "$upstream/tools/platereview/finish.py" "$d/cut.png" "$d/final-finish.png"
"$py" -B "$d/tone.py" "$d"
"$py" -B "$d/qa.py" "$d"
