#!/usr/bin/env python3
"""Stream logcat into validated, direction-preserving UART fixtures."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


def valid_frame(data):
    if len(data)<9 or data[0]!=0xff: return False
    if int.from_bytes(data[6:8],'big')+8 != len(data): return False
    x=0
    for b in data: x^=b
    return x==0


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('logcat',type=Path);p.add_argument('out',type=Path)
    p.add_argument('--limit',type=int,default=32)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    counts=Counter();records=[];sha=hashlib.sha256()
    for line_number,raw in enumerate(a.logcat.open('rb'),1):
        sha.update(raw)
        text=raw.decode('utf8','replace')
        match=re.search(r'(c => m|m => c)\s*:\s*:?\s*((?:[0-9a-fA-F]{2}\s*)+)',text)
        if not match: continue
        direction=match[1];data=bytes.fromhex(match[2])
        if not valid_frame(data):
            counts['invalid_'+direction]+=1;continue
        counts[direction]+=1
        if direction=='c => m' and len(records)<a.limit:
            records.append(dict(line=line_number,direction='hu-to-micom',data=data.hex()))
    (a.out/'hu-to-micom.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    manifest=dict(source=str(a.logcat),source_sha256=sha.hexdigest(),counts=dict(counts),
                  selected=len(records),timing='No timestamps in capture; virtual pacing, no original timing inferred',
                  validation='magic FF, BE length field includes checksum, XOR(all bytes) == 0')
    (a.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,indent=2))

if __name__=='__main__':main()
