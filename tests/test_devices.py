import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/micom'))
from devices import SPIMemory, FlexCAN, UARTLink
from inputs import load_events


class SPIMemoryTests(unittest.TestCase):
    def test_split_transactions_and_checkpoint_mid_command(self):
        ram=SPIMemory()
        ram.exchange(b'\x06',True)
        ram.exchange(b'\x02\x14',False)
        clone=SPIMemory();clone.restore(ram.snapshot())
        for device in (ram,clone):
            device.exchange(b'\x00hello',True)
            device.exchange(b'\x03\x14\x00',False)
            self.assertEqual(device.exchange(bytes(5),True),b'hello')
        self.assertEqual(ram.snapshot(),clone.snapshot())

    def test_status_and_address_wrap(self):
        r=SPIMemory();r.exchange(b'\x06',True)
        r.exchange(b'\x05',False);self.assertEqual(r.exchange(b'\0',True),b'\x02')
        r.exchange(b'\x02\xff\xffab',True)
        r.exchange(b'\x03\xff\xff',False)
        self.assertEqual(r.exchange(bytes(2),True),b'ab')

    def test_write_enable_status_and_sleep(self):
        r=SPIMemory();r.exchange(b'\x02\0\0x',True)
        self.assertEqual(r.memory[0],0)
        r.exchange(b'\x06',True);r.exchange(b'\x01\x04',True)
        self.assertEqual(r.status,4)
        r.exchange(b'\xb9',True)
        self.assertEqual(r.exchange(b'\x03\0\0\0',True),b'\xff'*4)
        r.exchange(b'\xab',True);self.assertFalse(r.sleeping)
        r.exchange(b'\x03\0\0',False);self.assertEqual(r.exchange(b'\0',True),b'\0')


class CANTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.c=FlexCAN(lambda kind,**kw:self.events.append((kind,kw)))
        self.c.write(0,0,4,0x0001003f)
        self.c.put(0,0x880,0x1ffc0000)
        self.c.put(0,0x84,0x123<<18)
        self.c.put(0,0x80,0x04000000)
        self.c.write(0,0x28,4,1)
        self.flash=bytearray(0x114500);self.flash[0x1144b8]=68

    def test_filter_irq_ack_lock_and_backpressure(self):
        self.c.enqueue(0,0x122,b'bad');self.c.enqueue(0,0x123,b'12345678')
        self.c.poll();self.assertEqual(len(self.c.pending),1)
        self.assertEqual(self.c.interrupts(self.flash),[68])
        self.assertEqual(self.c.read(0,0x80,4)>>16 & 15,8)
        self.assertEqual(self.c.regs[0][0x88:0x90],b'12345678')
        self.c.enqueue(0,0x123,b'next');self.c.poll()
        self.assertEqual(self.c.received,1)
        self.c.write(0,0x32,2,1)
        self.assertEqual(self.c.interrupts(self.flash),[])
        self.c.read(0,8,4);self.c.poll()
        self.assertEqual(self.c.received,2)
        self.assertEqual(self.c.regs[0][0x88:0x8c],b'next')

    def test_masked_flag_and_freeze(self):
        self.c.write(0,0x28,4,0)
        self.c.write(0,0,4,0x5001003f)
        self.c.enqueue(0,0x123,b'x');self.c.poll();self.assertEqual(self.c.received,0)
        self.c.write(0,0,4,0x0001003f);self.c.poll()
        self.assertEqual(self.c.received,1);self.assertEqual(self.c.interrupts(self.flash),[])
        self.c.write(0,0x28,4,1);self.assertEqual(self.c.interrupts(self.flash),[68])

    def test_transmit_completion_and_snapshot(self):
        self.c.put(0,0x94,0x321<<18);self.c.regs[0][0x98:0x9b]=b'abc'
        self.c.write(0,0x90,4,0x0c030000)
        self.assertEqual(self.c.tx[0]['data'],'616263')
        self.assertEqual(self.c.get(0,0x30),2)
        clone=FlexCAN(lambda *a,**kw:None);clone.restore(self.c.snapshot())
        self.assertEqual(clone.snapshot(),self.c.snapshot())

    def test_reject_non_can_payloads(self):
        for unit,ident,data in [(6,1,b''),(0,2048,b''),(0,1,bytes(9))]:
            with self.assertRaises(ValueError):self.c.enqueue(unit,ident,data)


class CableTests(unittest.TestCase):
    def test_bidirectional_stream_fragmentation_and_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket(socket.AF_UNIX) as server:
            path=Path(tmp)/'uart.sock';server.bind(str(path));server.listen(1)
            cable=UARTLink(path);self.addCleanup(cable.close)
            peer,_=server.accept()
            with peer:
                peer.sendall(b'\xffabc')
                self.assertEqual(cable.poll(2),b'\xffa')
                self.assertEqual(cable.poll(2),b'bc')
                cable.transmit(b'\0\xff');cable.poll(0)
                self.assertEqual(peer.recv(10),b'\0\xff')
                with self.assertRaises(RuntimeError):cable.transmit(bytes(65537))
            with self.assertRaisesRegex(RuntimeError,'disconnected'):cable.poll(1)


class InputTests(unittest.TestCase):
    def test_bounded_repetition_and_reject_ram_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'events.jsonl'
            p.write_text(json.dumps(dict(step=1,kind='gpio',pin=99,value=1,period_steps=2000,until_step=4001)))
            self.assertEqual(load_events(p)[0]['pin'],99)
            p.write_text(json.dumps(dict(step=1,kind='ram',address=0x40011820,value=0x40000000)))
            with self.assertRaises(ValueError):load_events(p)


class TimerTests(unittest.TestCase):
    def test_independent_periods_masked_flags_and_acknowledgement(self):
        from devices import PeriodicTimers
        p=PeriodicTimers(lambda:10000);flash=bytearray(0x114398)
        flash[0x114390:0x114398]=bytes([59,60,61,127,128,129,130,131])
        p.write(0,64000,0);p.write(8,3,0)
        p.write(32,128001,0);p.write(40,3,0)
        self.assertEqual(p.interrupts(9999,flash),[])
        self.assertEqual(p.interrupts(10000,flash),[59])
        p.write(12,1,10000)
        self.assertEqual(p.interrupts(20001,flash),[59,61])
        p.write(44,1,20001);p.write(12,1,20001)
        self.assertEqual(p.interrupts(20001,flash),[])
        p.write(40,0,20002)
        self.assertEqual(p.interrupts(40000,flash),[59])

    def test_polled_completion_is_explicit_and_no_irq_is_manufactured(self):
        from devices import PeriodicTimers
        p=PeriodicTimers(lambda:10000);f=bytes(0x114398)
        p.write(16,100000000,0);p.write(24,1,0)
        self.assertEqual(p.read(28,1),1)
        self.assertEqual(p.interrupts(1,f),[])


class SwitchTests(unittest.TestCase):
    def test_wire_inputs_parity_and_checkpoint(self):
        from devices import SwitchMonitor
        m=SwitchMonitor(SwitchMonitor.IDLE | (1<<10) | (1<<6))
        reply=int.from_bytes(m.exchange(bytes.fromhex('0a000001')),'big')
        self.assertEqual(reply.bit_count()&1,1)
        self.assertEqual((reply>>10)&1,1);self.assertEqual((reply>>6)&1,1)
        clone=SwitchMonitor();clone.restore(m.snapshot())
        self.assertEqual(clone.exchange(bytes(4)),m.exchange(bytes(4)))
        with self.assertRaises(RuntimeError):m.exchange(b'x')


class I2CTests(unittest.TestCase):
    def test_register_slave_repeated_start_irq_and_checkpoint(self):
        from devices import I2C
        bus=I2C(lambda *a,**kw:None);flash=bytearray(0x11448d);flash[0x11448c]=125
        bus.write(2,0x70);bus.write(4,0x66)
        self.assertEqual(bus.interrupts(flash),[125])
        bus.write(3,0x12);self.assertEqual(bus.interrupts(flash),[])
        bus.write(4,0x18);bus.write(3,0x12)
        bus.write(4,0xa5);bus.write(3,0x12);bus.write(2,0x50)
        bus.write(2,0x70);bus.write(4,0x66);bus.write(3,0x12)
        bus.write(4,0x18);bus.write(3,0x12)
        bus.write(2,0x74);bus.write(4,0x67);bus.write(3,0x12)
        clone=I2C(lambda *a,**kw:None);clone.restore(bus.snapshot())
        for b in (bus,clone):
            b.write(2,0x68);b.read(4)  # Dummy read launches receive.
            self.assertEqual(b.interrupts(flash),[125]);b.write(3,0x12)
            self.assertEqual(b.read(4),0xa5)
            b.write(2,0x50);b.write(3,0x12)
            self.assertEqual(b.interrupts(flash),[])
        self.assertEqual(bus.snapshot(),clone.snapshot())

    def test_unknown_address_nacks(self):
        from devices import I2C
        bus=I2C(lambda *a,**kw:None)
        bus.write(2,0x70);bus.write(4,0x64)
        self.assertEqual(bus.read(3)&1,1)

    def test_module_disable_releases_master_before_interrupt_reenable(self):
        from devices import I2C
        bus=I2C(lambda *a,**kw:None)
        bus.write(2,0x70);bus.write(4,0x66)
        self.assertTrue(bus.read(3)&0x20)
        bus.write(2,0x30);bus.write(3,0x12)
        bus.write(2,bus.read(2)|0x80)
        bus.write(2,bus.read(2)&~0x80)
        bus.write(2,bus.read(2)|0x40)
        self.assertFalse(bus.read(3)&0x20)
        bus.write(2,0x70);bus.write(4,0x66)
        self.assertTrue(bus.read(3)&0x20)
        self.assertEqual(bus.address,0x33)


class SPIBoardPeerTests(unittest.TestCase):
    def test_request_reply_and_mid_reply_checkpoint(self):
        from devices import SPIBoardPeer
        p=SPIBoardPeer();p.request(bytes([4,0x15,0xa5,0x5a]))
        p.request(bytes([2,0x15]));self.assertEqual(p.receive(2),b'\0\xa5')
        clone=SPIBoardPeer();clone.restore(p.snapshot())
        self.assertEqual(p.receive(1),b'\x5a');self.assertEqual(clone.receive(1),b'\x5a')
        with self.assertRaises(RuntimeError):p.request(b'\x05\0')
