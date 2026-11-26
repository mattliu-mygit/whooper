import { createFakeHost } from "../src/fakeHost";
import { reviewRecords } from "../src/fixtures";

// Shows the page as Prompting Time will: in an iframe sandboxed to scripts only, so the page has an
// opaque origin and talks to this window through postMessage alone, here with the fake host. As in
// Prompting Time, a route opens at once: ?route=/reviews/<run id> or ?route=/causes/<cause id> names
// it, ?review=<run id> is short for the first, and without either the first of the fake host's
// reviews opens.
const frame = document.createElement("iframe");
frame.title = "Whooper";
frame.sandbox.add("allow-scripts");
frame.src = "/index.html";
document.body.append(frame);

const query = new URLSearchParams(location.search);
const host = createFakeHost((message) => frame.contentWindow?.postMessage(message, "*"), {
  confirmSubmit: (text) => window.confirm(text),
  openOnReady: query.get("route") ?? `/reviews/${query.get("review") ?? reviewRecords()[0].run_id}`,
});
window.addEventListener("message", (event) => {
  if (event.source === frame.contentWindow) host.receive(event.data);
});
