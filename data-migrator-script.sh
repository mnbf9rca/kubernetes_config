#!/bin/bash
# check if DONT_RUN is set
if [ -z "$DONT_RUN" ]; then
    echo "DONT_RUN is not set - running the script"
    rsync -va --no-o --no-g --timeout=20 /source/* /target/
else
    echo "DONT_RUN is set - skipping script"
fi
