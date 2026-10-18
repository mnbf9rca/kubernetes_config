#!/usr/bin/env python3
"""Production cron entry; installation does not schedule or run it."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'hal-daily-check'))
from hal_daily_check import main
if __name__=='__main__':
    raise SystemExit(main(['--production']))
