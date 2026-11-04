#!/usr/bin/env python3
"""Execute real firmware helpers with independent oracles and both CPU engines."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import random
from micom import Machine, DEFAULT_FLASH_SHA256
from extract_frames import valid_frame


def call(m,pc,args,budget=10000):
    m.pc=pc
    m.reg('MSR',0)
    m.reg('r1',0x40017f00)
    for i,value in enumerate(args,3):m.reg(f'r{i}',value)
    m.reg('LR',0x180000)
    for _ in range(budget):
        if m.pc==0x180000:return m.reg('r3')
        m.step()
    raise AssertionError(f'firmware call {pc:#x} did not return, PC={m.pc:#x}')


def validate(image,fixtures,out,expected_flash_sha256=None):
    if expected_flash_sha256 is None:
        expected_flash_sha256 = DEFAULT_FLASH_SHA256
    results=[]
    seeds=[0,1,0xffffffff,0x12345678,0x80000000,0xaabb0000]+[random.Random(42+i).getrandbits(32) for i in range(20)]
    cases=[(x,1,2) for x in seeds]+[(0xaabb0000,0,2),(0x12340000,0,2),(0x12345678,1,8),(0x12345678,1,3)]
    for compiled in (True,False):
        m=Machine(image,out/('compiled' if compiled else 'interpreted'),expected_flash_sha256)
        m.use_compiler=compiled
        for seed,valid,level in cases:
            m.write(0x400119ee,4,seed)
            actual=call(m,0x5b518,[level,valid])
            # Swap adjacent bytes in each halfword, then the firmware's
            # asymmetric left/right shifts; this is not a rotate-by-11.
            swapped=((seed&0x00ff00ff)<<8)|((seed&0xff00ff00)>>8)
            shifted=((swapped<<11)|(swapped>>22))&0xffffffff
            expected=(shifted^0x12345678^(seed&0x87654321)) if valid else (0xb18aa157 if seed>>16==0xaabb else 0xffffffff)
            if level not in (2,8):expected=0
            assert actual==expected, f'{seed:08x}: {actual:08x} != {expected:08x}'
            results.append(dict(test='seed-helper',compiled=compiled,seed=hex(seed),valid=valid,level=level,result=hex(actual)))
        for record in fixtures:
            frame=bytes.fromhex(record['data'])
            assert record['direction']=='hu-to-micom' and valid_frame(frame)
            # Test the fake DMA completion and actual serial read function.
            # Cursor starts near the end to require wraparound for each frame.
            cfg,desc,base,dest=0x40010000,0x40010100,0x40010200,0x40010400
            m.write(0x4000f108,4,cfg);m.write(cfg+12,4,1);m.write(cfg+30,2,128)
            m.write(0x4000f148,4,desc);m.write(desc,4,base)
            m.write(desc+4,4,2);m.write(desc+8,2,120);m.write(desc+10,2,120);m.write(desc+12,2,0)
            m.rx_frames.append(frame);m.rx_next=0;m.feed_uart()
            assert not m.rx_frames
            status=call(m,0xed6ba,[1,dest,len(frame)])
            received=bytes(m.read(dest+i,1) for i in range(len(frame)))
            assert status==0 and received==frame
            assert m.read(desc+12,2)==0 and m.read(desc+8,2)==(120+len(frame))%128
            checksum=call(m,0xa11d4,[dest,len(frame)])
            assert checksum==0
            m.write(dest+len(frame)-1,1,frame[-1]^1)
            assert call(m,0xa11d4,[dest,len(frame)])==1
            results.append(dict(test='uart-rx-ring-and-checksum',compiled=compiled,line=record['line'],length=len(frame)))
        # isel is the Book E form omitted from stock e200's SLEIGH composition.
        for cr,expected in [(0,0x1234),(2,0x9876)]:
            m.pc=0xedff0;m.reg('MSR',0);m.reg('cr0',cr);m.reg('r0',0x1234);m.reg('r11',0x9876)
            m.step();assert m.reg('r0')==expected and m.pc==0xedff4
            results.append(dict(test='isel',compiled=compiled,cr0=cr,result=hex(expected)))
        # Pin 99 is WKPU input 17 in this firmware's published pin table.
        # An edge latches while masked; unmasking delivers IRQ 48 through IVOR4.
        mask=1<<17
        m.write(0xc3f94028,4,mask);m.write(0xc3f94018,4,0)
        m.set_gpio(99,1)
        assert m.read(0xc3f94014,4)==mask
        m.pc=0x5b66;m.reg('MSR',0x8000);m.reg('spr03f',0x40000)
        m.step();assert m.active_irq is None
        m.write(0xc3f94018,4,mask);before=m.pc
        m.step()
        assert m.active_irq==48 and m.reg('SRR0')==before and m.reg('SRR1')&0x8000
        results.append(dict(test='masked-wake-edge-and-real-vector-entry',compiled=compiled,irq=48))
        m.write(0xc3f94014,4,mask);assert m.read(0xc3f94014,4)==0
        m.set_gpio(99,0);assert m.read(0xc3f94014,4)==0
        results.append(dict(test='wake-w1c-and-disabled-falling-edge',compiled=compiled))
        # Writes to flash must fail explicitly.
        try:m.write(0,4,0)
        except RuntimeError:pass
        else:raise AssertionError('flash write accepted')
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('image',type=Path);p.add_argument('fixtures',type=Path);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--expected-flash-sha256',default=None,
                   help='required flash SHA-256 (default: tested V015 image); use "any" to skip the check')
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=False)
    fixtures=[json.loads(s) for s in a.fixtures.read_text().splitlines()]
    expected=None if a.expected_flash_sha256 in (None,'any') else a.expected_flash_sha256
    with contextlib.redirect_stdout(io.StringIO()):
        results=validate(a.image.read_bytes(),fixtures,a.out,expected)
    (a.out/'validation.json').write_text(json.dumps(dict(language='MICOM:BE:32:VLE',checks=results),indent=2)+'\n')
    print(f'PASS: {len(results)} checks across compiled and interpreted execution of real seed, UART read, checksum, isel code and GPIO interrupt delivery')

if __name__=='__main__':main()
