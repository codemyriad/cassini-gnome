import subprocess, wave, sys
turns = [
 ("me","Daniel","Good morning everyone. Let's start the weekly planning meeting for the Cassini launch."),
 ("them","Samantha","Thanks Daniel. The beta build went out yesterday and we have about forty new testers."),
 ("me","Daniel","Great. What is the biggest problem they reported so far?"),
 ("them","Karen","Mostly the export dialog. People could not find where the transcript was saved."),
 ("me","Daniel","Okay, let's fix that first. Karen, can you write the design for the export flow by Friday?"),
 ("them","Karen","Yes, I can have a draft by Thursday afternoon."),
 ("them","Samantha","I will collect the crash reports and send a summary on Monday."),
 ("me","Daniel","Perfect. One more thing: the budget review moved to November twelfth. Thanks all, talk next week."),
]
R=16000
def tts(i,voice,text):
    f=f"_tts{i}.wav"; subprocess.run(["say","-v",voice,"--data-format=LEI16@16000","-o",f,text],check=True)
    w=wave.open(f); d=w.readframes(w.getnframes()); w.close(); return d
gap=b"\0\0"*int(R*0.7)
tracks={"me":b"","them":b""}
for i,(who,v,t) in enumerate(turns):
    d=tts(i,v,t)
    for k in tracks: tracks[k]+= (d if k==who else b"\0"*len(d)) + gap
for k,d in tracks.items():
    w=wave.open(f"{k}.wav","wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(R); w.writeframes(d); w.close()
print(len(tracks["me"])/2/R,"seconds")
