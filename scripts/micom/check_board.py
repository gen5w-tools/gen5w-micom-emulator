#!/usr/bin/env python3
"""Cold board acceptance plus exact checkpoint replay; firmware stays in Docker."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from extract_frames import valid_frame


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('flash',type=Path);p.add_argument('out',type=Path)
    a=p.parse_args();root=Path(__file__).resolve().parents[2]
    a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    def run(name,steps,resume=None):
        cmd=[str(root/'scripts/micom/run.sh'),str(a.flash.resolve()),str(a.out/name),'--steps',str(steps)]
        if resume:cmd+=['--resume',str(a.out/resume)]
        else:cmd+=['--board','--events',str(root/'profiles/acc-on.jsonl'),'--rx-frames',str(root/'tests/fixtures/hu-startup.jsonl')]
        with (a.out/f'{name}.log').open('x') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True)
    run('full',9000000);run('split',6000000);run('resumed',3000000,'split')
    for name in ('ram.bin','registers.bin','mmio.json','state.json'):
        assert (a.out/'full'/name).read_bytes()==(a.out/'resumed'/name).read_bytes(),f'checkpoint diverged: {name}'
    full=(a.out/'full/uart1-tx.bin').read_bytes()
    combined=b''.join((a.out/n/'uart1-tx.bin').read_bytes() if (a.out/n/'uart1-tx.bin').exists() else b'' for n in ('split','resumed'))
    assert full==combined,'UART replay diverged'
    data=full;ids=[]
    while data:
        assert len(data)>=8
        count=int.from_bytes(data[6:8],'big')+8;frame=data[:count]
        assert valid_frame(frame),'invalid firmware TX frame'
        ids.append(int.from_bytes(frame[4:6],'big'));data=data[count:]
    assert {0x8101,0x8c5f,0x8c01}<=set(ids),f'missing firmware replies: {ids}'
    result=json.loads((a.out/'full/result.json').read_text())
    assert result['error'] is None and result['uart1_open'] and int(result['vehicle_status'],16)&0x8000
    assert result['rx_injected']==3
    report=dict(checkpoint='exact CPU, RAM, MMIO, devices, queues and UART replay',replies=[hex(i) for i in ids],tx_sha256=hashlib.sha256(full).hexdigest(),flash_sha256=result['flash_sha256'])
    (a.out/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    print('PASS: cold firmware handshake, settings replies, checksums and exact checkpoint replay')

if __name__=='__main__':main()
