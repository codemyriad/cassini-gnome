// The popup has no timer: it reflects what the worker pushes and re-inspects the meeting page only on
// open or when the person clicks "Check again". Tests act as that person until the state they expect shows.
export async function waitForHeading(popup, name, timeout = 30000) {
  const heading = popup.getByRole('heading', {name, exact: true});
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await heading.isVisible().catch(() => false)) return;
    await popup.locator('#recheck').click().catch(() => {});
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  await heading.waitFor({timeout: 1000});
}
