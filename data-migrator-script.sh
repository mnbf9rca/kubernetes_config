#!/bin/bash
# check if DONT_RUN is set
if [ -z "$DONT_RUN" ]; then
    echo "DONT_RUN is not set - running the script"
    rsync -vcrog --timeout=20 /source/* /target/
else
    echo "DONT_RUN is set"
fi
