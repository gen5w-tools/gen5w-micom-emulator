#!/usr/bin/env python3
"""Package a decrypted MICOM update as a flat flash image with provenance.

No decryption or guessed relocation. Original offsets are runtime addresses.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

DEFAULT_SHA256 = 'f6dde81bacc21b619896b6df2fc3bd8f880c4084133963f14ee3b0b276aefbb8'

def package(source, destination, expected_sha256=DEFAULT_SHA256):
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) < 8 or data[:4] != bytes.fromhex('005a0000'):
        raise ValueError('Expected big-endian VLE boot header 005a0000')
    entry = struct.unpack_from('>I',data,4)[0]
    if entry % 2 or not 8 <= entry < len(data):
        raise ValueError('Boot entry outside image or unaligned')
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError('Unexpected firmware hash; pass --expected-sha256 to run a different image')
    # Preserve the entire decrypted layout. The absent tail is erased flash.
    flash = data + b'\xff' * (0x180000-len(data))
    destination.mkdir(parents=True,exist_ok=False)
    (destination/'flash.bin').write_bytes(flash)
    # ELF32, big-endian, EM_PPC. Useful for debugger/import tooling; VLE backend required.
    ident = b'\x7fELF'+bytes([1,2,1])+b'\0'*9
    elf = struct.pack('>16sHHIIIIIHHHHHH',ident,2,20,1,entry,52,0,0,52,32,1,0,0,0)
    elf += struct.pack('>IIIIIIII',1,0x1000,0,0,len(flash),len(flash),5,0x1000)
    elf += b'\0'*(0x1000-len(elf)) + flash
    (destination/'micom.elf').write_bytes(elf)
    manifest = dict(source=str(source),source_sha256=digest,source_size=len(data),
                    layout='flat, address equals decrypted file offset; no relocation',
                    isa='PowerPC big-endian VLE/e200',entry=hex(entry),flash_base=0,
                    flash_size=len(flash),padding=dict(offset=hex(len(data)),size=len(flash)-len(data),value='ff'),
                    artifacts={name:hashlib.sha256(content).hexdigest() for name,content in [('flash.bin',flash),('micom.elf',elf)]})
    (destination/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,indent=2))

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('decrypted_package',type=Path)
    p.add_argument('output_directory',type=Path)
    p.add_argument('--expected-sha256',default=DEFAULT_SHA256,
                   help='required plaintext SHA-256 (default: tested V015 image); use "any" to skip the check')
    a=p.parse_args()
    expected=None if a.expected_sha256=='any' else a.expected_sha256
    package(a.decrypted_package,a.output_directory,expected)
