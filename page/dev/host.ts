import { createFakeHost } from "../src/fakeHost";

// Shows the page as Prompting Time will: in an iframe sandboxed to scripts only, so the page has an
// opaque origin and talks to this window through postMessage alone, here with the fake host. As in
// Prompting Time, a route opens at once: ?route=/past, ?route=/reviews/<run id>, or ?route=/causes/<cause id>
// names it, ?review=<run id> is short for the second, and without either the Reviews tab at / opens.
const frame = document.createElement("iframe");
frame.title = "Whooper";
frame.sandbox.add("allow-scripts");
frame.src = "/index.html";
document.body.append(frame);

const query = new URLSearchParams(location.search);
const host = createFakeHost((message) => frame.contentWindow?.postMessage(message, "*"), {
  confirmSubmit: (text) => window.confirm(text),
  openOnReady: query.get("route") ?? (query.has("review") ? `/reviews/${query.get("review")}` : "/"),
});
window.addEventListener("message", (event) => {
  if (event.source === frame.contentWindow) host.receive(event.data);
});
