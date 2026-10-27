#!/usr/bin/env python3
"""Deliberately corruptible host fixture. Implements only the happy-path test conversation."""
import base64, datetime, json, os, pathlib, struct, sys, uuid, wave
mode=os.environ.get('CASSINI_BAD_HOST','')
root=pathlib.Path(os.environ['CASSINI_FAKE_ROOT']);directory=None;manifest=None;audio={}
def reply(value):
 data=json.dumps(value).encode();sys.stdout.buffer.write(struct.pack('=I',len(data))+data);sys.stdout.buffer.flush()
def checkpoint():
 for track in manifest['tracks']:
  data=audio[track['id']]
  track['frames']=len(data)//(track['channels']*2);track['ended']=True
  if track.get('startOffsetMs') is not None:track['endOffsetMs']=track['startOffsetMs']+track['frames']/track['sampleRate']*1000
  with wave.open(str(directory/track['file']),'wb') as out:
   out.setnchannels(track['channels']);out.setsampwidth(2);out.setframerate(1 if mode=='wav-format' else track['sampleRate']);out.writeframes(data)
 (directory/'session.json').write_text(json.dumps(manifest))
while True:
 prefix=sys.stdin.buffer.read(4)
 if not prefix:break
 size,=struct.unpack('=I',prefix);m=json.loads(sys.stdin.buffer.read(size));kind=m['type']
 if manifest and manifest['state']=='completed':
  reply(dict(type='error',code='state',message='Session already stopped'))
  if mode=='keep-alive-after-stop':continue
  sys.exit(0 if mode=='zero-after-stop' else 1)
 if kind=='start':
  id=str(uuid.uuid4());directory=root/('Recording-'+id);directory.mkdir()
  manifest=dict(version=1,id=id,createdAt=datetime.datetime.now(datetime.timezone.utc).isoformat(),title=42 if mode=='title' else ('Wrong title' if mode=='semantic-title' else m.get('title','Browser recording')),source='macos' if mode=='source' else 'chromium',state='recording',tracks=[])
  reply(dict(type='started',path=str(directory)))
 elif kind=='track':
  manifest['tracks'].append(dict(id=m['id'],label=m.get('label',m['kind']),kind='tab' if mode=='kind' else m['kind'],sampleRate=44100 if mode=='rate' else m['sampleRate'],channels=m['channels'],file='track-x.wav',frames=0,labelSource='browser'))
  audio[m['id']]=b'';reply(dict(type='ack'))
 elif kind=='audio':
  track=next(t for t in manifest['tracks'] if t['id']==m['id'])
  track.setdefault('startOffsetMs',m['offsetMs']+10 if mode=='offset' else m['offsetMs'])
  data=base64.b64decode(m['pcm']);audio[m['id']]+=b'\0'*len(data) if mode=='pcm' else data;reply(dict(type='ack'))
 elif kind=='stop':
  manifest['state']='completed';checkpoint();reply(dict(type='stopped',path='/nowhere' if mode=='path' else str(directory)))
 else:
  reply(dict(type='error',code='version',message='Unknown message'))
  if mode!='keep-alive':sys.exit(1)
if manifest and manifest['state']=='recording':manifest['state']='interrupted';manifest['problem']='Disconnected';checkpoint()
