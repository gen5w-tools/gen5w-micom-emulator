#!/usr/bin/env python3
"""Disassemble selected VLE windows without executing firmware."""
import argparse
from pathlib import Path
import pypcode
from micom import vle_context
p=argparse.ArgumentParser()
p.add_argument('image',type=Path)
p.add_argument('start',type=lambda s:int(s,0))
p.add_argument('size',type=lambda s:int(s,0))
p.add_argument('--pcode',action='store_true')
a=p.parse_args()
c=vle_context()
b=a.image.read_bytes()[a.start:a.start+a.size]
print(c.translate(b,a.start) if a.pcode else c.disassemble(b,a.start))
