#!/usr/bin/env python3
"""Production cron entry; --trial sends a preview without production side effects."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'hal-daily-check'))
from hal_daily_check import main
if __name__=='__main__':
    raise SystemExit(main(sys.argv[1:] or ['--production']))
