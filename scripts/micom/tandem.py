#!/usr/bin/env python3
"""Boot MICOM before its private Android HU, preserving the initial handshake."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('flash',type=Path);p.add_argument('out',type=Path)
    p.add_argument('--steps',type=int,default=0,help='live MICOM instruction budget; 0 until interrupted')
    a=p.parse_args()
    if a.steps<0:p.error('--steps must be nonnegative')
    if not shutil.which('emuctl'):p.error('install emuctl from the gen5w-emulator project first')
    root=Path(__file__).resolve().parents[2];a.out=a.out.resolve()
    a.out.mkdir(parents=True,exist_ok=False)
    child=None;hu_id=None;stopped=False
    def stop(*_):
        nonlocal stopped
        stopped=True
        if child is not None and child.poll() is None:child.send_signal(signal.SIGTERM)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    def micom(name,options):
        nonlocal child
        with (a.out/f'{name}.log').open('x') as log:
            child=subprocess.Popen([str(root/'scripts/micom/run.sh'),str(a.flash.resolve()),str(a.out/name),*options],stdout=log,stderr=subprocess.STDOUT)
            code=child.wait();child=None
            if code:raise subprocess.CalledProcessError(code,'MICOM runner')
    try:
        # The car's MCU is already running before the application processor.
        # Establish that ordering explicitly; no virtual/wall-clock speed race.
        print(f'Preparing MICOM; traces: {a.out}',flush=True)
        micom('prepared',['--board','--events',str(root/'profiles/acc-on.jsonl'),'--steps','6000000'])
        if stopped:return 130
        result=json.loads((a.out/'prepared/result.json').read_text())
        if result['error'] is not None or not result['uart1_open']:
            raise RuntimeError('MICOM did not initialize its UART')
        launched=subprocess.run(['emuctl','start','V015','--micom','--name',f'micom-tandem-{os.getpid()}','--json'],check=True,capture_output=True,text=True)
        config=json.loads(launched.stdout);hu_id=config['id']
        (a.out/'hu.json').write_text(json.dumps(config,indent=2)+'\n')
        inspected=subprocess.run(['emuctl','inspect',hu_id],check=True,capture_output=True,text=True)
        (a.out/'hu-inspect.json').write_text(inspected.stdout)
        socket=Path(json.loads(inspected.stdout)['directory'])/'micom.sock'
        if stopped:return 130
        print(f'Android {hu_id}; cable {socket}; interrupt to checkpoint and stop both.',flush=True)
        micom('live',['--resume',str(a.out/'prepared'),'--uart-socket',str(socket),'--steps',str(a.steps)])
        return 130 if stopped else 0
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM);child.wait()
        if hu_id:
            subprocess.run(['emuctl','stop',hu_id],check=True)
            print(f'Stopped private Android {hu_id}; retained state and logs.',flush=True)

if __name__=='__main__':raise SystemExit(main())
