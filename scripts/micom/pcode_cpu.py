"""Compile integer SLEIGH p-code to Python for the offline MICOM runner.

Unsupported operations fall back to the checked interpreter, never to a NOP.
Unique temporaries stay local only when their def/use and aliasing are safe.
"""

def compile_instruction(pc, length, ops, signed):
    lines=['def execute(m):', ' r=m.regs', ' read=m.read', ' write=m.write']
    defined={}
    def get(v):
        sp=v.space.name
        if sp=='const': return str(v.offset)
        if sp=='ram': return f'read({v.offset},{v.size})'
        if sp=='register': return f"int.from_bytes(r[{v.offset}:{v.offset+v.size}], 'big')"
        if sp=='unique':
            key=(v.offset,v.size)
            if key not in defined: raise ValueError('unique alias/uninitialized')
            return defined[key]
        raise ValueError(sp)
    def put(v,expr):
        mask=(1<<(v.size*8))-1
        expr=f'(({expr}) & {mask})'
        sp=v.space.name
        if sp=='unique':
            key=(v.offset,v.size)
            for other in list(defined):
                if max(other[0],v.offset)<min(other[0]+other[1],v.offset+v.size): del defined[other]
            name=f't{v.offset:x}_{v.size}'
            defined[key]=name
            lines.append(f' {name}={expr}')
        elif sp=='register': lines.append(f" r[{v.offset}:{v.offset+v.size}]={expr}.to_bytes({v.size},'big')")
        elif sp=='ram': lines.append(f' write({v.offset},{v.size},{expr})')
        else: raise ValueError(sp)
    try:
        for op in ops:
            code=op.opcode.name
            vs=op.inputs
            if code=='IMARK': continue
            if code=='CALLOTHER': return None
            if code in ('BRANCH','CBRANCH','CALL','BRANCHIND','CALLIND','RETURN'):
                if vs[0].space.name=='const': return None
                target=str(vs[0].offset) if vs[0].space.name=='ram' else get(vs[0])
                if code=='CBRANCH': lines.append(f' if {get(vs[1])}: return ({target}) & 0xffffffff')
                else:
                    lines.append(f' return ({target}) & 0xffffffff')
                    break
                continue
            if code=='LOAD': put(op.output,f'read({get(vs[1])},{op.output.size})'); continue
            if code=='STORE': lines.append(f' write({get(vs[1])},{vs[2].size},{get(vs[2])})'); continue
            a=get(vs[0]); b=get(vs[1]) if len(vs)>1 else '0'
            sa=f'signed({a},{vs[0].size})'
            sb=f'signed({b},{vs[1].size})' if len(vs)>1 else '0'
            binary={'INT_ADD':'+','INT_SUB':'-','INT_MULT':'*','INT_DIV':'//','INT_REM':'%',
                    'INT_AND':'&','INT_OR':'|','INT_XOR':'^','BOOL_AND':'&','BOOL_OR':'|','BOOL_XOR':'^',
                    'INT_EQUAL':'==','INT_NOTEQUAL':'!=','INT_LESS':'<','INT_LESSEQUAL':'<='}
            if code in ('COPY','INT_ZEXT','CAST'): expr=a
            elif code=='INT_SEXT': expr=sa
            elif code in binary: expr=f'({a}) {binary[code]} ({b})'
            elif code=='INT_NEGATE': expr=f'~({a})'
            elif code=='INT_2COMP': expr=f'-({a})'
            elif code=='BOOL_NEGATE': expr=f'not ({a})'
            elif code=='INT_LEFT': expr=f'({a}) << ({b}) if ({b}) < {op.output.size*8} else 0'
            elif code=='INT_RIGHT': expr=f'({a}) >> ({b})'
            elif code=='INT_SRIGHT': expr=f'({sa}) >> ({b})'
            elif code=='INT_SLESS': expr=f'({sa}) < ({sb})'
            elif code=='INT_SLESSEQUAL': expr=f'({sa}) <= ({sb})'
            elif code=='INT_CARRY': expr=f'({a}) + ({b}) >= {1<<(vs[0].size*8)}'
            elif code in ('INT_SCARRY','INT_SBORROW'):
                sign='+' if code=='INT_SCARRY' else '-'
                expr=f'not {-(1<<(vs[0].size*8-1))} <= (({sa}) {sign} ({sb})) < {1<<(vs[0].size*8-1)}'
            elif code=='SUBPIECE': expr=f'({a}) >> (8*({b}))'
            elif code=='PIECE': expr=f'(({a}) << {8*vs[1].size}) | ({b})'
            elif code=='POPCOUNT': expr=f'({a}).bit_count()'
            elif code=='LZCOUNT': expr=f'{vs[0].size*8} - ({a}).bit_length()'
            else: return None
            put(op.output,expr)
        lines.append(f' return {(pc+length)&0xffffffff}')
        namespace={'signed':signed}
        exec(compile('\n'.join(lines),f'<VLE {pc:#x}>','exec'),namespace)
        return namespace['execute']
    except ValueError:
        return None
