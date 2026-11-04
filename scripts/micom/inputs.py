"""Strict, bounded offline hardware stimuli; no guest memory-write interface."""
import json


def load_events(path):
    events=[]
    for line in path.read_text().splitlines():
        e=json.loads(line)
        if len(events)>=4096:raise ValueError('too many input events')
        if type(e.get('step')) is not int or e['step']<0:raise ValueError('event step must be nonnegative')
        if e.get('kind')=='gpio':
            if type(e.get('pin')) is not int or not 0<=e['pin']<256 or type(e.get('value')) is not int or e['value'] not in (0,1):raise ValueError('invalid GPIO event')
        elif e.get('kind')=='switches':
            if type(e.get('value')) is not int or (not 0<=e['value']<=0x1fffffe or e['value']&1):raise ValueError('invalid switch input word')
        elif e.get('kind')=='uart':
            if not isinstance(e.get('data'),str) or not 1<=len(bytes.fromhex(e['data']))<=4096:raise ValueError('invalid UART input bytes')
        elif e.get('kind')=='can':
            from devices import FlexCAN
            FlexCAN(lambda *a,**k:None).enqueue(e['unit'],e['id'],bytes.fromhex(e['data']),e.get('extended',False))
        else:raise ValueError('unknown input event kind')
        if 'period_steps' in e:
            if type(e['period_steps']) is not int or e['period_steps']<1024 or type(e.get('until_step')) is not int or e['until_step']<e['step']:raise ValueError('invalid bounded event period')
        events.append(e)
    return sorted(events,key=lambda item:item['step'])
