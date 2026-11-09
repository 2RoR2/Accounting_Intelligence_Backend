"""API entry point for standard and isolated Windows Python installations."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn

if __name__=='__main__':
    uvicorn.run('main:app',host='127.0.0.1',port=8000)
