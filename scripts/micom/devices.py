"""Offline peripheral contracts, independent of CPU and application state."""
import socket


class SPIMemory:
    """Port-4 SPI memory: command 2 write, 3 read, 16-bit byte address.

    Command 6 is the firmware's write-enable transaction. Chip selection stays
    asserted across separate driver calls until the caller requests release.
    """
    def __init__(self):
        self.memory = bytearray(65536)
        self.header = bytearray()
        self.address = 0
        self.command = None
        self.status = 0
        self.sleeping = False

    def exchange(self, data, release):
        reply = bytearray()
        for byte in data:
            result = 0
            if self.sleeping and self.command != 0xab and byte != 0xab:
                self.command = 0xb9
                reply.append(0xff)
                continue
            if self.command is None:
                self.command = byte
            elif self.command in (2, 3):
                if len(self.header) < 2:
                    self.header.append(byte)
                    self.address = int.from_bytes(self.header, 'big')
                else:
                    if self.command == 2:
                        if self.status & 2: self.memory[self.address] = byte
                    else:
                        result = self.memory[self.address]
                    self.address = (self.address + 1) & 0xffff
            elif self.command == 1:
                if self.status & 2: self.status = (byte & 0xfc) | 2
            elif self.command == 5:
                result = self.status
            elif self.command not in (4, 6, 0xab, 0xb9):
                raise RuntimeError(f'unsupported SPI memory command {self.command:#x}')
            reply.append(result)
        if release:
            if self.command == 6: self.status |= 2
            elif self.command == 4: self.status &= ~2
            elif self.command == 0xab: self.sleeping = False
            elif self.command == 0xb9: self.sleeping = True
            elif self.command in (1, 2): self.status &= ~2
            self.header.clear()
            self.command = None
        return bytes(reply)

    def snapshot(self):
        return dict(memory=self.memory.hex(), header=self.header.hex(), address=self.address, command=self.command, status=self.status, sleeping=self.sleeping)

    def restore(self, state):
        memory = bytes.fromhex(state['memory'])
        if len(memory) != 65536:
            raise ValueError('invalid SPI memory snapshot')
        self.memory[:] = memory
        self.header[:] = bytes.fromhex(state['header'])
        self.address, self.command = state['address'], state['command']
        self.status, self.sleeping = state.get('status',0), state.get('sleeping',False)


class UARTLink:
    """A bounded, nonblocking raw UART cable to one QEMU UNIX chardev."""
    LIMIT = 65536

    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.connect(str(path))
        self.socket.setblocking(False)
        self.pending = bytearray()
        self.received = self.sent = 0

    def transmit(self, data):
        if len(self.pending) + len(data) > self.LIMIT:
            raise RuntimeError('UART cable output queue full')
        self.pending.extend(data)

    def poll(self, capacity):
        if self.pending:
            try:
                count = self.socket.send(self.pending)
                del self.pending[:count]
                self.sent += count
            except BlockingIOError:
                pass
        if capacity <= 0:
            return b''
        try:
            data = self.socket.recv(min(capacity, 4096))
        except BlockingIOError:
            return b''
        if not data:
            raise RuntimeError('UART cable disconnected')
        self.received += len(data)
        return data

    def close(self):
        self.socket.close()


class FlexCAN:
    """Classic CAN mailbox model; byte-order follows the MCU's big-endian bus.

    Arbitration is deterministic. A pending RX waits until an enabled matching
    mailbox is empty. Frames never write application RAM or invoke handlers.
    """
    BASES = tuple(0xfffc0000 + i*0x4000 for i in range(6))

    def __init__(self, event):
        self.regs = [bytearray(0x1000) for _ in self.BASES]
        self.locked = [set() for _ in self.BASES]
        self.pending = []
        self.tx = []
        self.event = event
        self.received = 0
        self._irqs = None

    def locate(self, address, size):
        for unit, base in enumerate(self.BASES):
            if base <= address and address+size <= base+0x1000:
                return unit, address-base
        return None

    def get(self, unit, offset, size=4):
        return int.from_bytes(self.regs[unit][offset:offset+size], 'big')

    def put(self, unit, offset, value, size=4):
        self.regs[unit][offset:offset+size] = value.to_bytes(size, 'big')
        self._irqs = None

    def read(self, unit, offset, size):
        if offset == 8:
            for mb in self.locked[unit]:
                cs=self.get(unit,0x80+16*mb)
                if (cs>>24)&15 in (2,6):
                    self.put(unit,0x80+16*mb,(cs&0xf0ffffff)|0x04000000)
            self.locked[unit].clear()
        if 0x80 <= offset < 0x480 and (offset-0x80)%16 == 0:
            self.locked[unit].add((offset-0x80)//16)
        return self.get(unit,offset,size)

    def write(self, unit, offset, size, value):
        # IFLAG and ESR interrupt bits acknowledge with ones, including halfword accesses.
        for reg, mask in ((0x2c,0xffffffff),(0x30,0xffffffff),(0x20,0x6)):
            if reg <= offset and offset+size <= reg+4:
                shift=8*(reg+4-offset-size)
                old=self.get(unit,reg)
                bits=value<<shift
                self.put(unit,reg,old & ~(bits & mask))
                return
        self.put(unit,offset,value,size)
        if offset < 4:
            mcr=self.get(unit,0)
            if mcr & 0x02000000:
                self.regs[unit][:]=bytes(0x1000)
                mcr &= ~0x02000000
            mcr &= ~0x09100000
            if mcr & 0x80000000: mcr |= 0x08100000
            elif mcr & 0x50000000 == 0x50000000: mcr |= 0x09000000
            self.put(unit,0,mcr)
        if 0x80 <= offset < 0x480 and (offset-0x80)%16 == 0:
            mb=(offset-0x80)//16
            cs=self.get(unit,offset)
            if cs>>24 & 15 == 12:
                ide=bool(cs & 0x200000)
                ident=self.get(unit,offset+4)
                ident=ident & 0x1fffffff if ide else (ident>>18)&0x7ff
                length=min((cs>>16)&15,8)
                frame=dict(unit=unit,id=ident,extended=ide,data=self.regs[unit][offset+8:offset+8+length].hex())
                self.tx.append(frame)
                if len(self.tx)>4096:del self.tx[0]
                self.event('can-tx',**frame)
                self.put(unit,offset,(cs&0xf0ffffff)|0x08000000)
                flag=0x30 if mb<32 else 0x2c
                self.put(unit,flag,self.get(unit,flag)|(1<<(mb%32)))

    def enqueue(self, unit, ident, data, extended=False):
        if type(unit) is not int or type(ident) is not int or type(extended) is not bool:
            raise ValueError('CAN unit/ID must be integers and extended must be boolean')
        if not 0 <= unit < 6 or not 0 <= ident < (1<<29 if extended else 1<<11) or len(data)>8:
            raise ValueError('invalid classic CAN frame')
        if len(self.pending)>=4096: raise RuntimeError('CAN input queue full')
        self.pending.append(dict(unit=unit,id=ident,data=bytes(data).hex(),extended=bool(extended)))

    def poll(self):
        waiting=[]
        for frame in self.pending:
            unit=frame['unit']
            if self.get(unit,0)&0x9b000000:
                waiting.append(frame);continue
            ident=frame['id'] if frame['extended'] else frame['id']<<18
            delivered=False
            for mb in range(min((self.get(unit,0)&127)+1,64)):
                off=0x80+16*mb
                cs=self.get(unit,off)
                if cs>>24&15 != 4 or mb in self.locked[unit]:continue
                if bool(cs&0x200000)!=frame['extended']:continue
                mask=self.get(unit,0x880+mb*4) if self.get(unit,0)&0x10000 else self.get(unit,0x10)
                if (ident^self.get(unit,off+4)) & mask:continue
                data=bytes.fromhex(frame['data'])
                self.put(unit,off+4,ident)
                self.regs[unit][off+8:off+16]=data.ljust(8,b'\0')
                self.put(unit,off,(cs&0xf00000)|0x02000000|(len(data)<<16))
                flag=0x30 if mb<32 else 0x2c
                self.put(unit,flag,self.get(unit,flag)|(1<<(mb%32)))
                self.received+=1
                self.event('can-rx',mailbox=mb,**frame)
                delivered=True;break
            if not delivered:waiting.append(frame)
        self.pending=waiting

    def interrupts(self, flash):
        if self._irqs is not None:return self._irqs
        result=[]
        for unit in range(6):
            flags=(self.get(unit,0x2c)&self.get(unit,0x24))<<32 | (self.get(unit,0x30)&self.get(unit,0x28))
            if not flags:continue
            for mb in range(64):
                if flags & (1<<mb):
                    group=mb//4 if mb<16 else 4 if mb<32 else 5
                    result.append(flash[0x1144b8+group*8+unit])
        self._irqs=sorted(set(result))
        return self._irqs

    def snapshot(self):
        return dict(regs=[r.hex() for r in self.regs],locked=[sorted(s) for s in self.locked],pending=self.pending,tx=self.tx,received=self.received)

    def restore(self,state):
        self.regs=[bytearray.fromhex(r) for r in state['regs']]
        if len(self.regs)!=6 or any(len(r)!=0x1000 for r in self.regs):raise ValueError('invalid CAN snapshot')
        self.locked=[set(s) for s in state['locked']]
        self.pending=state['pending'];self.tx=state['tx'];self.received=state['received']


class SwitchMonitor:
    """SPI-0/CS-0 board input monitor, a 32-bit full-duplex switch scan.

    Input positions/polarities come from firmware b2c00, not inferred CAN IDs.
    The physical part number is unresolved. Timing and electrical diagnostics
    are abstracted at the SPI driver boundary, like the virtual UART DMA.
    """
    IDLE = (0x7f<<18) | (1<<11) | (1<<12)

    def __init__(self, inputs=IDLE):
        self.inputs=inputs
        self.commands=[]

    def exchange(self, data):
        if len(data)!=4:raise RuntimeError('switch monitor requires a 32-bit SPI transfer')
        command=int.from_bytes(data,'big')
        self.commands.append(command)
        if len(self.commands)>32:del self.commands[0]
        # Bit zero carries odd parity on the serial word. Reserved bits stay low.
        reply=self.inputs & 0x01fffffe
        reply |= 1 ^ (reply.bit_count() & 1)
        return reply.to_bytes(4,'big')

    def snapshot(self):
        return dict(inputs=self.inputs,commands=self.commands)

    def restore(self,state):
        self.inputs=state['inputs'];self.commands=state['commands']


class PeriodicTimers:
    """Eight PIT channels on an instruction-derived clock, with level IRQs."""
    BASE=0xc3ff0100

    def __init__(self, pace):
        self.pace=pace
        self.channels=[dict(load=0,control=0,flag=0,deadline=None) for _ in range(8)]

    def period(self,c):
        return max(1,((c['load']+1)*self.pace()+64000)//64001)

    def advance(self,step):
        for c in self.channels:
            if c['control']&1 and c['deadline'] is not None and step>=c['deadline']:
                c['flag']=1
                period=self.period(c)
                c['deadline']+=((step-c['deadline'])//period+1)*period

    def read(self,offset,step):
        self.advance(step);c=self.channels[offset//16];reg=offset%16
        # Preserve the runner's synchronous polled-delay contract. Periodic
        # interrupt channels keep independent deadlines and W1C flags.
        if c['control']&3==1 and reg in (4,12):
            c['flag']=1
            return 0 if reg==4 else 1
        if reg==0:return c['load']
        if reg==4:return max(0,((c['deadline']-step)*64001)//self.pace()) if c['deadline'] is not None else c['load']
        if reg==8:return c['control']
        if reg==12:return c['flag']
        raise RuntimeError('unsupported PIT register')

    def write(self,offset,value,step):
        self.advance(step);c=self.channels[offset//16];reg=offset%16
        if reg==0:c['load']=value
        elif reg==8:
            if value&4:raise RuntimeError('chained PIT clocks are not implemented')
            if value&1 and not c['control']&1:c['deadline']=step+self.period(c)
            if not value&1:c['deadline']=None
            c['control']=value
        elif reg==12:c['flag'] &= ~(value&1)
        else:raise RuntimeError('write to read-only PIT register')

    def interrupts(self,step,flash):
        self.advance(step)
        return [flash[0x114390+i] for i,c in enumerate(self.channels) if c['control']&3==3 and c['flag']]


class I2C:
    """Byte-register I2C controller with an offline register slave at 0x33.

    The board driver uses IRQ 125, repeated START and a dummy read to launch
    reception. Unknown addresses NACK. No guest semaphore or RAM is patched.
    """
    BASE=0xffe30000

    def __init__(self, event):
        self.regs=bytearray(6)
        self.memory=bytearray(256)
        self.pointer=0
        self.address=None
        self.start=False
        self.index=False
        self.reading=False
        self.event=event

    def complete(self):
        self.regs[3] |= 0x82

    def read(self,offset):
        value=self.regs[offset]
        if offset==4 and self.regs[2]&0x20 and not self.regs[2]&0x10:
            self.regs[4]=self.memory[self.pointer] if self.address==0x33 else 0xff
            self.pointer=(self.pointer+1)&255
            self.complete()
        return value

    def write(self,offset,value):
        if offset==3:
            self.regs[3] &= ~(value&0x12)
            return
        old=self.regs[offset];self.regs[offset]=value
        if offset==2:
            if value&0x80:
                self.regs[3]=0;self.start=False;self.address=None
                self.regs[2] &= ~0x20  # Disabling the module releases master ownership.
            elif old&0x80:
                pass  # Re-enabling a reset controller does not assert START.
            elif value&0x20:
                if not old&0x20 or value&4:
                    self.start=True
                self.regs[3] |= 0x20
                self.regs[2] &= ~4  # RSTA is a command, self-cleared by hardware.
                if old&0x10 and not value&0x10:self.reading=True
            else:
                self.regs[3] &= ~0x20
                self.address=None;self.reading=False
        elif offset==4 and self.regs[2]&0x20 and self.regs[2]&0x10:
            if self.start:
                self.address=value>>1;self.start=False
                self.index=not bool(value&1)
                self.regs[3]=(self.regs[3]&~1)|(self.address!=0x33)
                self.event('i2c-address',address=hex(self.address),read=bool(value&1),ack=self.address==0x33)
            elif self.address==0x33:
                if self.index:self.pointer=value;self.index=False
                else:
                    self.memory[self.pointer]=value
                    self.pointer=(self.pointer+1)&255
            self.complete()

    def interrupts(self,flash):
        return [flash[0x11448c]] if self.regs[2]&0x40 and self.regs[3]&2 else []

    def snapshot(self):
        return dict(regs=self.regs.hex(),memory=self.memory.hex(),pointer=self.pointer,address=self.address,start=self.start,index=self.index,reading=self.reading)

    def restore(self,state):
        self.regs=bytearray.fromhex(state['regs']);self.memory=bytearray.fromhex(state['memory'])
        for key in ('pointer','address','start','index','reading'):setattr(self,key,state[key])


class SPIBoardPeer:
    """Explicit absent-board peer for port 1's length-prefixed request/reply.

    Firmware emits [length, selector, ...payload], then clocks a status byte
    and selector data. A zero-initialized register backend is a fake peer,
    not a claim about the unidentified physical component's diagnostics.
    """
    def __init__(self):
        self.memory=bytearray(256);self.selector=0;self.cursor=0

    def request(self,data):
        if len(data)<2 or data[0]!=len(data):
            raise RuntimeError('unsupported board SPI request framing')
        self.selector=data[1];self.cursor=0
        for i,value in enumerate(data[2:]):self.memory[(self.selector+i)&255]=value
        return bytes(len(data))

    def receive(self,count):
        reply=bytes(0 if self.cursor+i==0 else self.memory[(self.selector+self.cursor+i-1)&255] for i in range(count))
        self.cursor+=count
        return reply

    def snapshot(self):
        return dict(memory=self.memory.hex(),selector=self.selector,cursor=self.cursor)

    def restore(self,state):
        self.memory=bytearray.fromhex(state['memory']);self.selector=state['selector'];self.cursor=state['cursor']
