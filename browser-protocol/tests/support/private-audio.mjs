// Tests that un-mute Chromium (ignoreDefaultArgs: ['--mute-audio']) render real tones. They may only run
// against a private Pulse sink, never the desktop sound server. CI starts one; locally use the private harness.
export function requirePrivateAudio(env = process.env) {
  if (env.CASSINI_ISOLATED_AUDIO !== '1') throw Error('Use the private audio harness, never the desktop sound server.');
  const pulse = env.PULSE_SERVER || '';
  if (!/^unix:\/tmp\/cassini-audio-[^/]+\/pulse\.sock$/.test(pulse) && !(env.CI === 'true' && /^unix:\/run\/user\/\d+\/pulse\/native$/.test(pulse)))
    throw Error('A private Pulse socket is required.');
  return pulse;
}
