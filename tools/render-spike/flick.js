// Evaluate in YOUR private GNOME Shell through org.gnome.Shell.Eval.
// Input is synthesized on compositor paint events; no sleeps or pass budgets.
(()=>{
 const C=imports.gi.Clutter, G=imports.gi.GLib;
 global._renderSpikePointer=C.get_default_backend().get_default_seat().create_virtual_device(C.InputDeviceType.POINTER_DEVICE);
 global._renderSpikePointer.notify_absolute_motion(G.get_monotonic_time(),900,450);
 global._renderSpikeInput=[];
 let frame=0,stroke=0;
 const deltas=[30,55,90,85,65,0];
 const id=global.stage.connect('after-paint',()=>{
   const n=frame%80;
   if(n<6){let t=G.get_monotonic_time(),dy=deltas[n]*(stroke<4?1:-1);
     global._renderSpikeInput.push([t,stroke,n,dy]);
     global._renderSpikePointer.notify_scroll_continuous(t,0,dy,C.ScrollSource.FINGER,n===5?C.ScrollFinishFlags.VERTICAL:C.ScrollFinishFlags.NONE);
   }
   frame++;if(frame%80===0)stroke++;
   if(stroke===6){global.stage.disconnect(id);global._renderSpikeDone=true;}
   else global.stage.queue_redraw();
 });
 global._renderSpikeDone=false;global.stage.queue_redraw();return 'six native flicks scheduled';
})()
