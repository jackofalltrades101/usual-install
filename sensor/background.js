// Usual Install -- browser-side sensor.
//
// Deliberately thin. It holds no catalogue, makes no install decision, and
// never names a package or a compatibility layer. It reports where the user is
// and what they clicked; the session agent decides everything else.
//
// There are no host_permissions and no content scripts: download events carry
// the URL and referrer already, and the page title comes from the tabs API, so
// the sensor never reads page content on any site.

const HOST = "com.usualinstall.host";

// Downloads paused while awaiting a verdict, so we can resume on failure.
const pending = new Map();
// Windows downloads we were told to let finish, so the agent can set up a
// compatibility layer around the completed file. id -> page context.
const awaitingFile = new Map();

async function pageContext(item) {
  // The initiating page is the authority, not the download URL -- vendors
  // routinely serve the file itself from a CDN on an unrelated domain.
  let url = item.url;
  let title = "";
  if (item.referrer && item.referrer.startsWith("http")) url = item.referrer;
  try {
    const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (tab) {
      if (tab.url && tab.url.startsWith("http") && !item.referrer) url = tab.url;
      // The title is the strongest signal for identifying a Windows product,
      // which has no domain-keyed catalogue entry to fall back on.
      if (tab.title) title = tab.title;
    }
  } catch (e) { /* tabs may be unavailable; the agent copes without a title */ }
  return { url, title };
}

function send(message) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    // If the agent is missing or wedged, fail open rather than hanging a download.
    const timer = setTimeout(() => done({ action: "ignore", reason: "agent timeout" }), 4000);
    try {
      chrome.runtime.sendNativeMessage(HOST, message, (reply) => {
        clearTimeout(timer);
        if (chrome.runtime.lastError) {
          done({ action: "ignore", reason: chrome.runtime.lastError.message });
        } else {
          done(reply || { action: "ignore", reason: "empty reply" });
        }
      });
    } catch (e) {
      clearTimeout(timer);
      done({ action: "ignore", reason: String(e) });
    }
  });
}

chrome.downloads.onCreated.addListener(async (item) => {
  // Pause first, ask second. Resolution is a round trip to the agent, and a
  // download that has already written bytes cannot be un-started.
  try {
    await chrome.downloads.pause(item.id);
    pending.set(item.id, true);
  } catch (e) {
    return; // too late or not pausable -- leave it alone
  }

  const ctx = await pageContext(item);
  const reply = await send({
    type: "download",
    pageUrl: ctx.url,
    pageTitle: ctx.title,
    downloadUrl: item.url,
    filename: item.filename || "",
    mime: item.mime || "",
    bytes: item.totalBytes || 0,
  });

  pending.delete(item.id);

  if (reply.action === "handled") {
    // A native package is being installed instead; the download is redundant.
    try { await chrome.downloads.cancel(item.id); } catch (e) {}
    try { await chrome.downloads.erase({ id: item.id }); } catch (e) {}
    return;
  }

  if (reply.action === "compat") {
    // A Windows installer we can run through a compatibility layer -- but the
    // file itself is what gets run, so it has to finish downloading.
    awaitingFile.set(item.id, { pageUrl: ctx.url, pageTitle: ctx.title });
  }

  try { await chrome.downloads.resume(item.id); } catch (e) {}
});

chrome.downloads.onChanged.addListener(async (delta) => {
  if (!awaitingFile.has(delta.id)) return;
  if (!delta.state || delta.state.current !== "complete") {
    if (delta.error) awaitingFile.delete(delta.id);
    return;
  }
  const ctx = awaitingFile.get(delta.id);
  awaitingFile.delete(delta.id);

  // onChanged carries no path; ask for the finished item.
  let path = "";
  try {
    const [found] = await chrome.downloads.search({ id: delta.id });
    if (found) path = found.filename || "";
  } catch (e) {}
  if (!path) return;

  await send({ type: "downloaded", path, pageUrl: ctx.pageUrl, pageTitle: ctx.pageTitle });
});

// If the sensor is torn down mid-flight, don't leave downloads paused forever.
chrome.runtime.onSuspend.addListener(() => {
  for (const id of pending.keys()) {
    chrome.downloads.resume(id).catch(() => {});
  }
});
