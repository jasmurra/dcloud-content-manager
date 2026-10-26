document.addEventListener("dcm-webrdp-tab", (event) => {
  const detail = event.detail || {};
  const title = String(detail.title || "").trim();
  const url = String(detail.url || "").trim();
  if (!title || !url) return;
  chrome.runtime.sendMessage({ type: "webrdp-tab", title, url }).catch(() => {});
});
