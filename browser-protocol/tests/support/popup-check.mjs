// Popup test helpers: bind the popup's active-tab lookup before popup.js runs its open check,
// and wait for explicit status-request completion (aria-busy="false") instead of fixed sleeps.
export async function openPopupForTab(context, extensionId, target, timeout = 30000) {
  const popup = await context.newPage();
  await popup.addInitScript(selector => {
    if (!globalThis.chrome?.tabs?.query) return;
    const query = chrome.tabs.query.bind(chrome.tabs);
    chrome.tabs.query = async options => {
      if (!options?.active) return query(options);
      if (typeof selector === 'number') return [await chrome.tabs.get(selector)];
      return (await query({})).filter(tab => tab.url === selector).slice(0, 1);
    };
  }, target);
  await popup.goto(`chrome-extension://${extensionId}/popup.html`);
  await popup.locator('#tab-status[aria-busy="false"]').waitFor({ timeout });
  return popup;
}

// Run one explicit "Check again" and wait for that status request (and any queued refresh) to finish.
export async function recheckPopup(popup, timeout = 30000) {
  await popup.locator('#tab-status[aria-busy="false"]').waitFor({ timeout });
  await popup.locator('#recheck').click();
  await popup.locator('#tab-status[aria-busy="false"]').waitFor({ timeout });
}
