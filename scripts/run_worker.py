"""Entry point compatible with isolated Windows Python distributions."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.queue.worker import main

if __name__=='__main__':main()
