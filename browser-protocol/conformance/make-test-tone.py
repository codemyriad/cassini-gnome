#!/usr/bin/env python3
"""Create fake Chrome microphone input for an authorized manual test call."""
import argparse, math, pathlib, struct, wave
p=argparse.ArgumentParser();p.add_argument('output',type=pathlib.Path);p.add_argument('--frequency',type=int,default=880);p.add_argument('--seconds',type=int,default=60);a=p.parse_args()
assert 20<=a.frequency<=10000 and 1<=a.seconds<=600
with wave.open(str(a.output),'wb') as out:
 out.setnchannels(1);out.setsampwidth(2);out.setframerate(48000)
 block=b''.join(struct.pack('<h',round(8192*math.sin(2*math.pi*a.frequency*i/48000))) for i in range(48000))
 for _ in range(a.seconds):out.writeframes(block)
print(a.output.resolve())
