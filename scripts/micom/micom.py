#!/usr/bin/env python3
"""Offline VLE firmware runner; no host device access. See README.md."""
import argparse
from collections import Counter, deque
import hashlib
import json
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET
import pypcode
from pcode_cpu import compile_instruction
from devices import SPIMemory, UARTLink, FlexCAN, SwitchMonitor, PeriodicTimers, I2C, SPIBoardPeer

LANGUAGE = 'MICOM:BE:32:VLE'
DEFAULT_FLASH_SHA256 = '113b94238cceeb3134e85bc6045fbf892c2711de3d413c33939690116d4bebf4'

def vle_context():
    definition = ET.fromstring('<language id="MICOM:BE:32:VLE" processor="PowerPC" endian="big" size="32" variant="VLE" version="1.0" slafile="micom_vle.sla" processorspec="ppc_32.pspec"><description>32-bit Power ISA VLE</description></language>')
    ctx = pypcode.Context(pypcode.ArchLanguage('/opt/micom-vle', definition))
    ctx.setVariableDefault('vle',1)
    return ctx

def signed(x, size):
    bit = 1 << (size * 8 - 1)
    return (x ^ bit) - bit

class Machine:
    def __init__(self, image, out, expected_flash_sha256=DEFAULT_FLASH_SHA256):
        self.flash = bytes(image)
        if expected_flash_sha256 is not None and hashlib.sha256(self.flash).hexdigest()!=expected_flash_sha256:
            raise ValueError('unexpected flash image; pass --expected-flash-sha256 to run a different image')
        self.ram = bytearray(0x18000)
        self.regs = bytearray(0x20000)
        self.unique = bytearray(0x100000)
        self.mmio = {}
        self.out = Path(out)
        self.out.mkdir(parents=True,exist_ok=True)
        if any(self.out.iterdir()):raise ValueError('output directory must be empty')
        self.event_file = (self.out/'live.jsonl').open('x')
        self.ctx = vle_context()
        self.cache = {}
        self.use_compiler = True
        self.stop_requested=False
        self.uart_pending=b''
        self.pc = int.from_bytes(image[4:8], 'big')
        self.steps = 0
        self.visits = Counter()
        self.history = deque(maxlen=40)
        self.events = deque(maxlen=4096)
        self.io_counts = Counter()
        self.uart_tx = {}
        self.hooks = Counter()
        self.swi_pending = False
        self.active_irq = None
        self.next_tick = 0
        self.tick_steps = 10000
        self.pit=PeriodicTimers(lambda:self.tick_steps)
        self.pit_pending = False
        self.gpio_default = 0
        self.gpio = {2: 1}
        self.adc_sample = 512
        self._gpio_irqs=None
        self.input_events = []
        self.can = None
        self.i2c = None
        self.spi_peer = None
        self.switch_monitor = None
        self.spi_memory = None
        self.uart_link = None
        self.trace_functions = set()
        self.watch_addresses = set()
        self.rx_frames = deque()
        self.rx_next = 0
        self.rx_loaded = 0
        self.rx_consumed = 0

    def restore(self, directory):
        directory=Path(directory)
        state=json.loads((directory/'state.json').read_text())
        if state['flash_sha256'] != hashlib.sha256(self.flash).hexdigest():
            raise ValueError('snapshot firmware mismatch')
        if state.get('schema',1) not in (1,2,3):raise ValueError('unsupported snapshot schema')
        if state.get('spec_sha256',self.spec_hash())!=self.spec_hash():raise ValueError('snapshot decoder mismatch')
        registers=(directory/'registers.bin').read_bytes()
        if len(registers)!=len(self.regs):raise ValueError('invalid snapshot register size')
        ram=(directory/'ram.bin').read_bytes()
        if len(ram)<len(self.ram):raise ValueError('truncated snapshot SRAM')
        if any(ram[len(self.ram):]): raise ValueError('snapshot exceeds firmware SRAM window')
        self.ram[:]=ram[:len(self.ram)]
        self.regs[:]=registers
        self.mmio={int(k):v for k,v in json.loads((directory/'mmio.json').read_text()).items()}
        for name in ('pc','steps','swi_pending','active_irq','next_tick','tick_steps','pit_pending','gpio_default','adc_sample'):
            setattr(self,name,state[name])
        if state.get('pit_channels') is not None:self.pit.channels=state['pit_channels']
        else:
            for i in range(8):
                for off in (0,8):
                    addr=self.pit.BASE+16*i+off
                    value=int.from_bytes(bytes(self.mmio.get(addr+j,0) for j in range(4)),'big')
                    self.pit.write(16*i+off,value,self.steps)
        self.gpio={int(k):v for k,v in state['gpio'].items()}
        self.input_events=state.get('input_events',[])
        self.uart_pending=bytes.fromhex(state.get('uart_pending',''))
        self.rx_frames=deque(bytes.fromhex(s) for s in state.get('rx_frames',[]))
        for name in ('rx_next','rx_loaded','rx_consumed'):
            setattr(self,name,state.get(name,0))
        if state.get('switch_monitor') is not None:
            self.switch_monitor=SwitchMonitor()
            self.switch_monitor.restore(state['switch_monitor'])
        if state.get('spi_peer') is not None:
            self.spi_peer=SPIBoardPeer()
            self.spi_peer.restore(state['spi_peer'])
        if state.get('i2c') is not None:
            self.i2c=I2C(self.event)
            self.i2c.restore(state['i2c'])
        if state.get('can') is not None:
            self.can=FlexCAN(self.event)
            self.can.restore(state['can'])
        if state.get('spi_memory',state.get('shared_ram')) is not None:
            self.spi_memory = SPIMemory()
            self.spi_memory.restore(state.get('spi_memory',state.get('shared_ram')))
        self.event('restore',source=str(directory))

    @staticmethod
    def spec_hash():
        return hashlib.sha256(Path('/opt/micom-vle/micom_vle.sla').read_bytes()).hexdigest()

    def event(self, kind, **kw):
        item = dict(step=self.steps, pc=hex(self.pc), kind=kind, **kw)
        self.events.append(item)
        self.event_file.write(json.dumps(item)+'\n')
        self.event_file.flush()
        if kind != 'mmio':
            print(json.dumps(item), flush=True)

    def reg(self, name, value=None):
        v = self.ctx.registers[name]
        if value is None:
            return self.get(v)
        self.put(v, value)

    def read(self, addr, size):
        addr &= 0xffffffff
        if addr + size <= len(self.flash):
            return int.from_bytes(self.flash[addr:addr+size], 'big')
        if 0x40000000 <= addr and addr+size <= 0x40000000+len(self.ram):
            a = addr-0x40000000
            return int.from_bytes(self.ram[a:a+size], 'big')
        if addr >= 0xc0000000:
            self.io_counts[('read',addr,size)] += 1
            if self.i2c and self.i2c.BASE<=addr<self.i2c.BASE+6 and size==1:return self.i2c.read(addr-self.i2c.BASE)
            if self.pit.BASE<=addr<self.pit.BASE+128 and size==4:return self.pit.read(addr-self.pit.BASE,self.steps)
            if self.can:
                location=self.can.locate(addr,size)
                if location is not None:return self.can.read(*location,size)
            if 0xfff48020 <= addr and addr+size <= 0xfff48028: return 0
            if addr == 0xfff48010 and size == 4 and self.active_irq is not None:
                return 0x40000000 + self.active_irq*4
            # MC_ME global status: requested RUN0 mode, oscillator stable.
            if addr == 0xc3fdc000 and size == 4: return 0x00000044
            # PLL lock bit (PowerPC bit numbering: bit 21 -> 0x400).
            if addr == 0xc3f88000 and size == 4: return 0x400
            # SIUL GPDI inputs: pin 2 ready; other pins configurable, default low.
            if 0xc3f90800 <= addr < 0xc3f90900 and size == 1: return self.gpio.get(addr-0xc3f90800,self.gpio_default)
            # ADC data registers: valid conversion with a stable 10-bit sample.
            if 0xffe00100 <= addr < 0xffe00160 and size == 4: return 0x80000 | self.adc_sample
            return int.from_bytes(bytes(self.mmio.get(addr+i,0) for i in range(size)), 'big')
        raise RuntimeError(f'unmapped read {addr:#x}/{size}')

    def write(self, addr, size, value):
        addr &= 0xffffffff
        b = (value & ((1 << (8*size))-1)).to_bytes(size, 'big')
        if 0x40000000 <= addr and addr+size <= 0x40000000+len(self.ram):
            a = addr-0x40000000
            if a <= 0x942 < a+size and self.ram[0x942] != b[0x942-a]:
                self.event('board-log-enable', value=b[0x942-a])
            for watched in self.watch_addresses:
                if addr <= watched < addr+size:
                    self.event('memory-write',address=hex(addr),size=size,value=hex(value),caller=hex(self.reg('LR')))
            self.ram[a:a+size] = b
        elif addr >= 0xc0000000:
            self.io_counts[('write',addr,size)] += 1
            if self.i2c and self.i2c.BASE<=addr<self.i2c.BASE+6 and size==1:
                self.i2c.write(addr-self.i2c.BASE,value)
                return
            if self.pit.BASE<=addr<self.pit.BASE+128 and size==4:
                self.pit.write(addr-self.pit.BASE,int.from_bytes(b,'big'),self.steps)
                return
            if self.can:
                location=self.can.locate(addr,size)
                if location is not None:
                    self.can.write(location[0],location[1],size,int.from_bytes(b,'big'))
                    return
            if 0xc3f90000 <= addr <= 0xc3f94030:self._gpio_irqs=None
            if addr in (0xc3f90014,0xc3f94014) and size == 4:
                old=int.from_bytes(bytes(self.mmio.get(addr+i,0) for i in range(4)),'big')
                b=(old & ~value).to_bytes(4,'big')
            if addr <= 0xfff48021 < addr+size:
                command = b[0xfff48021-addr]
                if command & 2: self.swi_pending = True
                if command & 1: self.swi_pending = False
            if addr == 0xfff48018: self.active_irq = None
            if self.io_counts[('write',addr,size)] <= 4:
                self.event('mmio', address=hex(addr), size=size, value=hex(value))
            for i,x in enumerate(b):
                self.mmio[addr+i] = x
        else:
            raise RuntimeError(f'unmapped/flash write {addr:#x}/{size}={value:#x}')

    def get(self, v):
        space = v.space.name
        if space == 'const': return v.offset
        if space == 'ram': return self.read(v.offset,v.size)
        mem = self.regs if space == 'register' else self.unique if space == 'unique' else None
        if mem is None: raise RuntimeError(f'unknown space {space}')
        return int.from_bytes(mem[v.offset:v.offset+v.size], 'big')

    def put(self, v, value):
        value &= (1 << (8*v.size))-1
        if v.space.name == 'ram': return self.write(v.offset,v.size,value)
        mem = self.regs if v.space.name == 'register' else self.unique
        mem[v.offset:v.offset+v.size] = value.to_bytes(v.size,'big')

    def decode(self, pc):
        if pc not in self.cache:
            if not 0 <= pc < len(self.flash): raise RuntimeError(f'execute outside flash {pc:#x}')
            b = self.flash[pc:pc+32]
            dis = self.ctx.disassemble(b,pc,max_instructions=1).instructions[0]
            ops = self.ctx.translate(b,pc,max_instructions=1).ops
            self.cache[pc] = (dis.length, str(dis), ops, compile_instruction(pc, dis.length, ops, signed) if self.use_compiler else None)
        return self.cache[pc]

    def userop(self, op):
        name = op.inputs[0].getUserDefinedOpName()
        if name == 'returnFromInterrupt':
            self.put(op.output, self.get(op.inputs[-1]))
            return
        if name in ('memoryBarrier', 'instructionSynchronize', 'enforceInOrderExecutionIO', 'sync', 'isync'):
            # Single CPU, synchronous memory/MMIO; all accesses already ordered.
            return
        if name == 'WriteExternalEnableImmediate':
            enabled = self.get(op.inputs[1])
            self.reg('MSR', (self.reg('MSR') & ~0x8000) | (0x8000 if enabled else 0))
            return
        raise RuntimeError(f'unimplemented CPU userop {name}: {op}')

    def feed_uart(self):
        if not self.rx_frames or self.steps < self.rx_next: return
        cfg=self.read(0x4000f108,4)
        desc=self.read(0x4000f148,4)
        if not cfg or not desc: return
        capacity=self.read(cfg+0x1e,2)
        base=self.read(desc,4)
        available=self.read(desc+12,2)
        frame=self.rx_frames[0]
        if not capacity or available >= capacity: return
        count=min(len(frame),capacity-available)
        portion=frame[:count]
        cursor=self.read(desc+10,2)
        for byte in portion:
            self.write(base+cursor,1,byte)
            cursor=(cursor+1)%capacity
        self.write(desc+10,2,cursor)
        self.write(desc+12,2,available+len(portion))
        self.write(desc+4,4,1)
        # Mode 1 uses the UART ISR ring; mode 2 uses the fixed RX DMA
        # channel 2*UART+1. Config +0x14 is interrupt priority, not DMA ID.
        if self.read(cfg+12,4)==2:
            self.write(0xfff45010+0x20*3,4,base+cursor)
        if count == len(frame):
            self.rx_frames.popleft()
            self.rx_consumed += 1
        else: self.rx_frames[0]=frame[count:]
        self.rx_next=self.steps+(0 if self.uart_link else self.tick_steps*20)
        self.event('uart-rx',channel=1,data=portion.hex(),remaining=len(self.rx_frames))

    def set_gpio(self,pin,value):
        self._gpio_irqs=None
        old=self.gpio.get(pin,self.gpio_default)
        self.gpio[pin]=value
        if old == value:return
        banks=[(0xc3f90000,{p:i for i,p in enumerate(self.flash[0x114508:0x114520])}),
               (0xc3f94000,{p:i+2 for i,p in enumerate(self.flash[0x1145c2:0x1145dd])})]
        for base,pins in banks:
            if pin not in pins:continue
            bit=1<<pins[pin]
            edge=base+(0x28 if value else 0x2c)
            if self.read(edge,4)&bit:
                addr=base+0x14
                flags=self.read(addr,4)|bit
                for i,byte in enumerate(flags.to_bytes(4,'big')):self.mmio[addr+i]=byte
        self.event('gpio-edge',pin=pin,value=value)

    def feed_events(self):
        while self.input_events and self.input_events[0]['step'] <= self.steps:
            event=self.input_events.pop(0)
            if event['kind']=='gpio':self.set_gpio(event['pin'],event['value'])
            elif event['kind']=='uart':
                self.rx_frames.append(bytes.fromhex(event['data']))
                self.rx_loaded+=1
                self.event('uart-input',data=event['data'])
            elif event['kind']=='switches':
                if self.switch_monitor is None:raise RuntimeError('switch event requires --switch-monitor')
                self.switch_monitor.inputs=event['value']
                self.event('switch-inputs',value=hex(event['value']))
            elif event['kind']=='can':
                if self.can is None:raise RuntimeError('CAN event requires --can')
                self.can.enqueue(event['unit'],event['id'],bytes.fromhex(event['data']),event.get('extended',False))
            period=event.get('period_steps',0)
            if period:
                event=dict(event,step=event['step']+period)
                if event['step'] <= event['until_step']:
                    self.input_events.append(event)
                    self.input_events.sort(key=lambda item:item['step'])

    def switch_transfer(self):
        if self.reg('r3')!=0 or self.reg('r4')!=0:return False
        tx8,tx16,rx8,rx16=(self.reg(f'r{i}') for i in (7,8,9,10))
        count=self.read(self.reg('r1')+8,4)
        if tx16 or rx16 or count!=4:raise RuntimeError('unsupported switch monitor SPI format')
        data=bytes(self.read(tx8+i,1) for i in range(count))
        reply=self.switch_monitor.exchange(data)
        for i,byte in enumerate(reply):self.write(rx8+i,1,byte)
        self.hooks['switch_scan']+=1
        if self.hooks['switch_scan']<=32:self.event('switch-scan',data=data.hex(),reply=reply.hex())
        self.reg('r3',0);self.pc=self.reg('LR') & 0xfffffffe;self.steps+=1
        return True

    def spi_transfer(self):
        unit, channel, select, mode, tx8, tx16, count = (self.reg(f'r{i}') for i in range(3,10))
        device=self.spi_memory if unit==4 else self.spi_peer if unit==1 else None
        if device is None or channel != 0:return False
        if tx16 or count > 65536:
            raise RuntimeError('unsupported SPI transfer width/length')
        receive = self.pc == 0xf239a
        data = bytes(count) if receive else bytes(self.read(tx8+i,1) for i in range(count))
        reply = device.exchange(data, select in (0,2)) if unit==4 else device.receive(count) if receive else device.request(data)
        if receive:
            for i, byte in enumerate(reply): self.write(tx8+i,1,byte)
        self.hooks['spi_read' if receive else 'spi_write'] += 1
        if self.hooks['spi_read' if receive else 'spi_write'] <= 100:
            self.event('spi-transfer',unit=unit,receive=receive,select=select,mode=mode,data=data.hex(),reply=reply.hex(),caller=hex(self.reg('LR')))
        self.reg('r3',0)
        self.pc=self.reg('LR') & 0xfffffffe
        self.steps += 1
        return True

    def step(self):
        if self.input_events and self.input_events[0]["step"] <= self.steps:self.feed_events()
        if self.can and self.steps % 1024 == 0: self.can.poll()
        if self.pc in self.trace_functions:
            self.hooks[hex(self.pc)] += 1
            if self.hooks[hex(self.pc)] <= 32:self.event('observe',address=hex(self.pc),r3=hex(self.reg('r3')),caller=hex(self.reg('LR')),history=list(self.history)[-12:])
        if self.uart_link and self.steps % 1024 == 0:
            capacity=65536-sum(map(len,self.rx_frames))
            data=self.uart_link.poll(capacity)
            if data:
                self.rx_frames.append(data)
                self.rx_loaded += 1
                self.event('uart-cable-rx',data=data.hex())
        if self.switch_monitor and self.pc==0xf1f70 and self.switch_transfer():return
        if (self.spi_memory or self.spi_peer) and self.pc in (0xf14fe,0xf239a) and self.spi_transfer(): return
        if self.rx_frames and self.steps % 1024 == 0: self.feed_uart()
        if self.active_irq is None and self.reg('MSR') & 0x8000 and self.mmio.get(0xfff4800b,0) == 0:
            extra=list(self.can.interrupts(self.flash)) if self.can else []
            if self._gpio_irqs is None:
                enabled=self.read(0xc3f90014,4)&self.read(0xc3f90018,4)
                wake=self.read(0xc3f94014,4)&self.read(0xc3f94018,4)
                self._gpio_irqs=sorted(set([41+i//8 for i in range(24) if enabled&(1<<i)]+[46+i//8 for i in range(32) if wake&(1<<i)]))
            extra += self._gpio_irqs
            if self.i2c:extra += self.i2c.interrupts(self.flash)
            timer_irqs=self.pit.interrupts(self.steps,self.flash)
            irq=1 if self.swi_pending else min(timer_irqs+extra) if timer_irqs or extra else None
            if irq is not None:
                self.reg('SRR0',self.pc)
                self.reg('SRR1',self.reg('MSR'))
                self.reg('MSR',self.reg('MSR') & ~0x8000)
                self.active_irq = irq
                self.hooks[f'irq{irq}'] += 1
                if self.hooks[f'irq{irq}'] < 30: self.event('interrupt', vector=irq)
                # e200 fixed IVOR4 offset, established by firmware's vector table.
                self.pc = (self.reg('spr03f') & 0xffff0000) | 0x40
        if self.pc in (0xec75a,0xecf74) and self.reg('r3')==1:
            self.event('uart-lifecycle',operation='open' if self.pc==0xec75a else 'close',config=hex(self.reg('r4')),caller=hex(self.reg('LR')))
        if self.pc == 0xe7588:
            self.hooks['timer_insert'] += 1
            if self.hooks['timer_insert'] < 30:
                self.event('timer-insert', head=hex(self.reg('r3')), node=hex(self.reg('r4')), delay=self.reg('r5'), caller=hex(self.reg('LR')), old_head=hex(self.read(self.reg('r3'),4)))
        if self.pc == 0xe70d4:
            self.hooks['context_restore'] += 1
            if self.hooks['context_restore'] < 50:
                self.event('context-restore', task=hex(self.read(0x40012410,4)))
        # UART driver boundary: complete a synchronous virtual transmission.
        # Firmware builds and formats every byte; no application handlers are replaced.
        if self.pc == 0xe9568:
            ch, ptr, count = (self.reg(n) for n in ('r3','r4','r5'))
            if ch > 7 or count > 65536: raise RuntimeError('invalid UART write')
            data = bytes(self.read(ptr+i,1) for i in range(count))
            with (self.out/f'uart{ch}-tx.bin').open('ab') as capture: capture.write(data)
            self.uart_tx[ch]=self.uart_tx.get(ch,0)+len(data)
            if ch == 1 and self.uart_link: self.uart_link.transmit(data)
            self.event('uart-tx',channel=ch,data=data.hex(),text=data.decode('ascii','backslashreplace'))
            self.hooks['serial_write'] += 1
            self.reg('r3',0)
            self.pc = self.reg('LR') & 0xfffffffe
            self.steps += 1
            return
        length, dis, ops, execute = self.decode(self.pc)
        self.history.append(dis)
        self.visits[self.pc] += 1
        if self.visits[self.pc] == 1 and dis.split(': ',1)[-1].startswith(('e_bl ', 'se_bl ', 'se_bctrl')):
            self.event('call', instruction=dis)
        if execute is not None:
            self.pc = execute(self)
            self.steps += 1
            return
        nextpc = (self.pc+length)&0xffffffff
        ip = 0
        while ip < len(ops):
            op = ops[ip]
            code = op.opcode.name
            vs = op.inputs
            if code == 'IMARK': ip+=1; continue
            if code == 'CALLOTHER': self.userop(op); ip+=1; continue
            if code in ('BRANCH','CBRANCH','CALL','BRANCHIND','CALLIND','RETURN'):
                take = code != 'CBRANCH' or self.get(vs[1]) != 0
                if take:
                    if vs[0].space.name == 'const' and code in ('BRANCH','CBRANCH'):
                        ip += signed(vs[0].offset,vs[0].size)
                        continue
                    nextpc = (vs[0].offset if vs[0].space.name == 'ram' else self.get(vs[0]))&0xffffffff
                    break
                ip+=1; continue
            if code == 'LOAD': value=self.read(self.get(vs[1]),op.output.size)
            elif code == 'STORE': self.write(self.get(vs[1]),vs[2].size,self.get(vs[2])); ip+=1; continue
            else:
                a = self.get(vs[0])
                b = self.get(vs[1]) if len(vs)>1 else 0
                sa = signed(a,vs[0].size)
                sb = signed(b,vs[1].size) if len(vs)>1 else 0
                if code in ('COPY','INT_ZEXT','CAST'): value=a
                elif code == 'INT_SEXT': value=sa
                elif code == 'INT_ADD': value=a+b
                elif code == 'INT_SUB': value=a-b
                elif code == 'INT_MULT': value=a*b
                elif code == 'INT_DIV': value=a//b
                elif code == 'INT_REM': value=a%b
                elif code in ('INT_SDIV','INT_SREM'):
                    q = (abs(sa)//abs(sb)) * (-1 if (sa<0)!=(sb<0) else 1)
                    value = q if code=='INT_SDIV' else sa-q*sb
                elif code in ('INT_AND','BOOL_AND'): value=a&b
                elif code in ('INT_OR','BOOL_OR'): value=a|b
                elif code in ('INT_XOR','BOOL_XOR'): value=a^b
                elif code == 'INT_NEGATE': value=~a
                elif code == 'INT_2COMP': value=-a
                elif code == 'BOOL_NEGATE': value=int(not a)
                elif code == 'INT_LEFT': value=a<<b if b < op.output.size*8 else 0
                elif code == 'INT_RIGHT': value=a>>b
                elif code == 'INT_SRIGHT': value=sa>>b
                elif code == 'INT_EQUAL': value=int(a==b)
                elif code == 'INT_NOTEQUAL': value=int(a!=b)
                elif code == 'INT_LESS': value=int(a<b)
                elif code == 'INT_LESSEQUAL': value=int(a<=b)
                elif code == 'INT_SLESS': value=int(sa<sb)
                elif code == 'INT_SLESSEQUAL': value=int(sa<=sb)
                elif code == 'INT_CARRY': value=int(a+b >= 1<<(vs[0].size*8))
                elif code in ('INT_SCARRY','INT_SBORROW'):
                    x = sa+sb if code=='INT_SCARRY' else sa-sb
                    value=int(not -(1<<(vs[0].size*8-1)) <= x < 1<<(vs[0].size*8-1))
                elif code == 'SUBPIECE': value=a>>(8*b)
                elif code == 'PIECE': value=(a<<(8*vs[1].size))|b
                elif code == 'POPCOUNT': value=a.bit_count()
                elif code == 'LZCOUNT': value=vs[0].size*8-a.bit_length()
                else: raise RuntimeError(f'unimplemented pcode {op}')
            self.put(op.output,value)
            ip+=1
        self.pc=nextpc
        self.steps+=1

    def run(self, budget):
        start=time.monotonic()
        error=None
        try:
            end=self.steps+budget if budget else None
            while not self.stop_requested and (end is None or self.steps<end):
                self.step()
                if self.steps % 1000000 == 0:
                    print(json.dumps(dict(progress_steps=self.steps,pc=hex(self.pc),timer_irqs=self.hooks['irq59'])),flush=True)
        except Exception as e:
            error=f'{type(e).__name__}: {e}'
        state={name:getattr(self,name) for name in ('pc','steps','swi_pending','active_irq','next_tick','tick_steps','pit_pending','gpio_default','gpio','adc_sample')}
        state['schema']=3
        state['spec_sha256']=self.spec_hash()
        state['pit_channels']=self.pit.channels
        state['uart_pending']=bytes(self.uart_link.pending).hex() if self.uart_link else self.uart_pending.hex()
        state['input_events']=self.input_events
        state['switch_monitor']=self.switch_monitor.snapshot() if self.switch_monitor else None
        state['spi_peer']=self.spi_peer.snapshot() if self.spi_peer else None
        state['i2c']=self.i2c.snapshot() if self.i2c else None
        state['can']=self.can.snapshot() if self.can else None
        state['spi_memory']=self.spi_memory.snapshot() if self.spi_memory else None
        state['flash_sha256']=hashlib.sha256(self.flash).hexdigest()
        state['rx_frames']=[frame.hex() for frame in self.rx_frames]
        for name in ('rx_next','rx_loaded','rx_consumed'):state[name]=getattr(self,name)
        (self.out/'state.json').write_text(json.dumps(state,indent=2)+'\n')
        (self.out/'ram.bin').write_bytes(self.ram)
        (self.out/'registers.bin').write_bytes(self.regs)
        (self.out/'mmio.json').write_text(json.dumps(self.mmio))
        result=dict(vehicle_status=hex(self.read(0x40011820,4)),acc_mode=self.read(0x40000674,4),uart1_open=bool(self.read(0x4000f108,4)),can_received=self.can.received if self.can else 0,can_pending=len(self.can.pending) if self.can else 0,cable_rx_bytes=self.uart_link.received if self.uart_link else 0,cable_tx_bytes=self.uart_link.sent if self.uart_link else 0,cable_tx_pending=len(self.uart_link.pending) if self.uart_link else len(self.uart_pending),rx_loaded=self.rx_loaded,rx_injected=self.rx_consumed,rx_pending=len(self.rx_frames),language=LANGUAGE,flash_sha256=hashlib.sha256(self.flash).hexdigest(),pypcode_version=pypcode.__version__,spec_sha256=self.spec_hash(),hooks=dict(self.hooks),ram_sha256=hashlib.sha256(self.ram).hexdigest(),steps=self.steps,pc=hex(self.pc),error=error,seconds=time.monotonic()-start,
                    hot=[(hex(k),v) for k,v in self.visits.most_common(20)],
                    registers={n:hex(self.reg(n)) for n in [f'r{i}' for i in range(32)]+['LR','CTR','MSR','SRR0','SRR1','spr03f','spr194'] if n in self.ctx.registers},
                    history=list(self.history),io=[dict(op=k[0],address=hex(k[1]),size=k[2],count=v) for k,v in self.io_counts.items()])
        self.out.mkdir(parents=True,exist_ok=True)
        (self.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        # live.jsonl is the complete streamed trace; the in-memory tail is bounded.
        (self.out/'events.jsonl').symlink_to('live.jsonl')
        print(json.dumps({k:v for k,v in result.items() if k not in ('io','history')},indent=2))
        return error is None

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('image',type=Path)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--steps',type=int,default=6000000)
    p.add_argument('--resume',type=Path)
    p.add_argument('--rx-frames',type=Path)
    p.add_argument('--switch-monitor',action='store_true',help='attach the SPI board switch input monitor')
    p.add_argument('--switch-inputs',type=lambda s:int(s,0),help='raw switch input bits; defaults to inactive polarity profile')
    p.add_argument('--board',action='store_true',help='attach CAN, I2C, SPI memory, switch monitor and board peer')
    p.add_argument('--events',type=Path,help='instruction-timed GPIO, switch and CAN JSONL input')
    p.add_argument('--spi-peer',action='store_true',help='attach the absent port-1 board peer')
    p.add_argument('--i2c',action='store_true',help='attach board I2C controller and register slave')
    p.add_argument('--can',action='store_true',help='model FlexCAN mailboxes')
    p.add_argument('--spi-memory',action='store_true',help='attach the port-4 SPI memory device')
    p.add_argument('--uart-socket',type=Path,help='raw bidirectional QEMU UART UNIX socket')
    p.add_argument('--gpio',action='append',default=[],metavar='PIN=0|1')
    p.add_argument('--tick-steps',type=int)
    p.add_argument('--adc-sample',type=int)
    p.add_argument('--trace-function',action='append',type=lambda s:int(s,0),default=[])
    p.add_argument('--watch-address',action='append',type=lambda s:int(s,0),default=[],help='trace writes covering this address')
    p.add_argument('--interpret',action='store_true')
    p.add_argument('--expected-flash-sha256',default=DEFAULT_FLASH_SHA256,
                   help='required flash SHA-256 (default: tested V015 image); use "any" to skip the check')
    args=p.parse_args()
    expected=None if args.expected_flash_sha256=='any' else args.expected_flash_sha256
    m=Machine(args.image.read_bytes(),args.out,expected)
    m.trace_functions=set(args.trace_function)
    m.watch_addresses=set(args.watch_address)
    if args.resume: m.restore(args.resume)
    if args.switch_inputs is not None and (not 0<=args.switch_inputs<=0x1fffffe or args.switch_inputs&1):p.error('switch inputs must fit bits 1..24')
    if (args.switch_monitor or args.board or args.switch_inputs is not None) and m.switch_monitor is None:m.switch_monitor=SwitchMonitor()
    if args.switch_inputs is not None:m.switch_monitor.inputs=args.switch_inputs
    if args.spi_peer or args.board:m.spi_peer=m.spi_peer or SPIBoardPeer()
    if args.i2c or args.board: m.i2c=m.i2c or I2C(m.event)
    if (args.can or args.board) and m.can is None: m.can=FlexCAN(m.event)
    if (args.spi_memory or args.board) and m.spi_memory is None: m.spi_memory=SPIMemory()
    if args.uart_socket:
        m.uart_link=UARTLink(args.uart_socket)
        m.uart_link.transmit(m.uart_pending)
        m.uart_pending=b''
    if args.steps<0:p.error('--steps must be nonnegative (0 runs until interrupted)')
    import signal
    def stop(*_):m.stop_requested=True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    if args.events:
        from inputs import load_events
        m.input_events += load_events(args.events)
        m.input_events.sort(key=lambda item:item['step'])
    for item in args.gpio:
        pin,value=(int(s,0) for s in item.split('='))
        if not 0 <= pin < 256 or value not in (0,1): p.error('GPIO requires PIN 0..255 and value 0 or 1')
        m.set_gpio(pin,value)
    if args.tick_steps is not None:
        if args.tick_steps < 100: p.error('--tick-steps must be at least 100')
        m.tick_steps=args.tick_steps
    if args.adc_sample is not None:
        if not 0 <= args.adc_sample <= 1023: p.error('--adc-sample must be 0..1023')
        m.adc_sample=args.adc_sample
    if args.rx_frames:
        from extract_frames import valid_frame
        for line in args.rx_frames.read_text().splitlines():
            record=json.loads(line)
            if record.get('direction') != 'hu-to-micom': p.error('RX fixture must have direction hu-to-micom')
            frame=bytes.fromhex(record['data'])
            if not valid_frame(frame): p.error('Invalid RX fixture frame')
            if 'step' in record:
                if type(record['step']) is not int or record['step']<0:p.error('RX fixture step must be nonnegative')
                m.input_events.append(dict(step=record['step'],kind='uart',data=frame.hex()))
            else:m.rx_frames.append(frame)
        m.input_events.sort(key=lambda item:item['step'])
        m.rx_loaded=m.rx_consumed+len(m.rx_frames)
    m.use_compiler=not args.interpret
    try:return 0 if m.run(args.steps) else 1
    finally:
        if m.uart_link:m.uart_link.close()
        m.event_file.close()

if __name__=='__main__': sys.exit(main())
