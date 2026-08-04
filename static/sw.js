// KK Trading System -- push notification service worker.
// Only handles 'push' (show the notification) and 'notificationclick'
// (focus the dashboard tab if one's open, else open it) -- no fetch
// interception, no offline caching, so registering this never changes
// how the dashboard itself loads or behaves.

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: "KK Trading System", message: event.data ? event.data.text() : "" };
  }
  const title = data.title || "KK Trading System";
  const options = {
    body: data.message || "",
    icon: "/app/static/icon_192.png",
    badge: "/app/static/icon_192.png",
    data: { url: data.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url.indexOf(url) !== -1 && "focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
