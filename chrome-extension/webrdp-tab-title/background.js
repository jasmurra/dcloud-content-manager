const PENDING_MS = 20000;

function cleanTitle(value) {
  return String(value || "").replace(/[\r\n\t]+/g, " ").trim().slice(0, 180);
}

function bareUrl(value) {
  return String(value || "").split("#")[0].split("?")[0].replace(/\/$/, "");
}

function fromLocalApp(sender) {
  try {
    const url = new URL(sender?.url || sender?.tab?.url || "");
    return url.protocol === "http:" && (url.hostname === "127.0.0.1" || url.hostname === "localhost");
  } catch {
    return false;
  }
}

function holdTabTitle(title) {
  const desired = String(title || "");
  if (!desired) return;
  const root = window;
  root.__dcmWantedTabTitle = desired;
  const apply = () => {
    if (document.title !== root.__dcmWantedTabTitle) document.title = root.__dcmWantedTabTitle;
  };
  apply();
  if (root.__dcmTabTitleHeld) return;
  root.__dcmTabTitleHeld = true;
  const arm = () => {
    const el = document.querySelector("title");
    if (!el || el.dataset.dcmWatching === "1") return;
    el.dataset.dcmWatching = "1";
    new MutationObserver(apply).observe(el, { childList: true, subtree: true, characterData: true });
  };
  arm();
  if (document.documentElement) {
    new MutationObserver(arm).observe(document.documentElement, { childList: true, subtree: true });
  }
  setInterval(apply, 400);
}

async function titles() {
  const data = await chrome.storage.session.get("tabTitles");
  return data.tabTitles || {};
}

async function rememberTitle(tabId, title) {
  const map = await titles();
  map[String(tabId)] = title;
  await chrome.storage.session.set({ tabTitles: map });
  chrome.scripting.executeScript({
    target: { tabId },
    func: holdTabTitle,
    args: [title],
  }).catch(() => {});
}

async function pendingOpens() {
  const data = await chrome.storage.session.get("pendingOpens");
  const now = Date.now();
  return (data.pendingOpens || []).filter((row) => now - row.at < PENDING_MS);
}

async function noteOpen(title, url) {
  const rows = await pendingOpens();
  rows.push({ title, url: bareUrl(url), at: Date.now() });
  await chrome.storage.session.set({ pendingOpens: rows.slice(-20) });
  const recent = await chrome.storage.session.get("recentNav");
  for (const nav of recent.recentNav || []) {
    if (Date.now() - nav.at < PENDING_MS && bareUrl(nav.url) === bareUrl(url)) {
      await rememberTitle(nav.tabId, title);
    }
  }
}

async function noteNavigation(tabId, url) {
  const data = await chrome.storage.session.get("recentNav");
  const now = Date.now();
  const rows = (data.recentNav || []).filter((row) => now - row.at < PENDING_MS);
  rows.push({ tabId, url, at: now });
  await chrome.storage.session.set({ recentNav: rows.slice(-40) });
  const known = (await titles())[String(tabId)];
  if (known) {
    await rememberTitle(tabId, known);
    return;
  }
  const hit = (await pendingOpens()).find((row) => row.url && row.url === bareUrl(url));
  if (hit) await rememberTitle(tabId, hit.title);
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type !== "webrdp-tab" || !fromLocalApp(sender)) return;
  const title = cleanTitle(message.title);
  const url = String(message.url || "");
  if (!title || !/^https:\/\//i.test(url)) return;
  noteOpen(title, url);
});

chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId !== 0) return;
  noteNavigation(details.tabId, details.url);
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const map = await titles();
  if (!map[String(tabId)]) return;
  delete map[String(tabId)];
  await chrome.storage.session.set({ tabTitles: map });
});
