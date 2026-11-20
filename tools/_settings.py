"""Minimal .env reader for local developer tooling only (no external deps)."""
import os
from pathlib import Path

def get(name, default=''):
 value=os.getenv(name)
 if value:return value
 p=Path('.env')
 if p.exists():
  for line in p.read_text(encoding='utf8').splitlines():
   key,sep,val=line.partition('=')
   if sep and key.strip()==name:return val.strip().strip('"').strip("'")
 return default
