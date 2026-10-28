// Clicking the toolbar icon opens the downloader in Chrome's side panel, next to
// the suno.com tab whose signed-in session it uses.
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error(error));
