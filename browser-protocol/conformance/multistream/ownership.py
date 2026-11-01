"""Independent D52 fixture oracle; apps must exercise their own placement path."""

def windows(events, track):
    left, right = track['startMs'], track['endMs']
    boundaries = sorted({left, right} | {e['atMs'] for e in events if left < e['atMs'] < right})
    selected, source = None, None
    result = []
    for begin, end in zip(boundaries, boundaries[1:]):
        for event in events:
            if event['atMs'] > begin:
                break
            if event['type'] == 'selection': selected = event
            if event['type'] == 'othersSource': source = event['source']
        me = track['kind'] in ('microphone', 'local')
        allowed = (selected is None or selected['me' if me else 'others']) and (
            me or source is None or source == ('system' if track['kind'] == 'system' else 'browser'))
        if allowed:
            if result and result[-1][1] == begin: result[-1][1] = end
            else: result.append([begin, end])
    return result
