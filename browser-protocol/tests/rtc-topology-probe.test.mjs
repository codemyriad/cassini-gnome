import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {installRtcTopologyProbe,summarizeRtcCohortComparison} from './rtc-topology-probe.mjs';

// Generated descriptors only. Raw values stay in these fixture realms; reports
// contain the same opaque integer references as the real two-room study.
function realm(room) {
  const context=vm.createContext({room});
  vm.runInContext(`
    window=globalThis;
    class Description { constructor(value){this.value=value} get sdp(){return this.value} }
    RTCSessionDescription=Description;
    class Peer {
      getSenders(){return []} getReceivers(){return []} async getStats(){return new Map()}
      get localDescription(){return this.local} get remoteDescription(){return this.remote}
    }
    RTCPeerConnection=Peer;
    MediaStreamTrack=class {clone(){return new MediaStreamTrack()}};
    navigator={mediaDevices:{async getUserMedia(){return {getAudioTracks(){return []}}}}};
    (${installRtcTopologyProbe.toString()})();
    const description=new Description('v=0\\r\\no=- '+room+' 1 IN IP4 127.0.0.1\\r\\ns=-\\r\\na=fingerprint:sha-256 generated-shared-server\\r\\na=ssrc:1 cname:'+room+'-cname\\r\\na=msid:'+room+'-stream track\\r\\n');
    for(let i=0;i<2;i++){const pc=new RTCPeerConnection();pc.local=description;pc.remote=description}
  `,context);
  return context;
}

test('one private table distinguishes same-backend room-specific fields across realms',()=>{
  const first=realm('generated-room-a'),second=realm('generated-room-b'),groups=new Map();
  const factsA=first.cassiniRtcCohortFacts(groups),factsB=second.cassiniRtcCohortFacts(groups);
  const report=summarizeRtcCohortComparison(factsA,factsB);
  assert.equal(report.notAdmissionRule,true);
  assert.deepEqual(report.sharedRemoteGroups.fingerprint,[0],'the backend fingerprint is deliberately shared');
  assert.deepEqual(report.sharedRemoteGroups.originSession,[]);
  assert.deepEqual(report.sharedRemoteGroups.rtcpCname,[]);
  assert.deepEqual(report.sharedRemoteGroups.streamId,[]);
  assert.deepEqual(report.sharedRemoteGroups.sessionName,[],'placeholder session names never identify a room');
  assert.equal(factsA.peers[0].remote.rtcpCname[0],factsA.peers[1].remote.rtcpCname[0],'within-room equality is preserved');
  assert.notEqual(factsA.peers[0].remote.rtcpCname[0],factsB.peers[0].remote.rtcpCname[0]);
  assert.doesNotMatch(JSON.stringify(report),/generated-|sha-256|127\.0\.0\.1|v=0/,'raw descriptors never enter the numeric report');
});

test('common backend placeholders are reported as cross-room ambiguity, never admission',()=>{
  const first=realm('generated-common-placeholder'),second=realm('generated-common-placeholder'),groups=new Map();
  const report=summarizeRtcCohortComparison(first.cassiniRtcCohortFacts(groups),second.cassiniRtcCohortFacts(groups));
  assert.equal(report.notAdmissionRule,true);
  for(const field of ['fingerprint','rtcpCname','streamId'])assert.deepEqual(report.sharedRemoteGroups[field],[0]);
  assert.equal('callEvidence' in report,false);
});
