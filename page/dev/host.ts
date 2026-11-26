import { createFakeHost } from "../src/fakeHost";

// Shows the page as Prompting Time will: in an iframe sandboxed to scripts only, so the page has an
// opaque origin and talks to this window through postMessage alone, here with the fake host.
// ?review=<run id> opens that review at once, as when the user picks it in Prompting Time.
const frame = document.createElement("iframe");
frame.title = "Whooper";
frame.sandbox.add("allow-scripts");
frame.src = "/index.html";
document.body.append(frame);

const host = createFakeHost((message) => frame.contentWindow?.postMessage(message, "*"), {
  confirmSubmit: (text) => window.confirm(text),
  openOnReady: new URLSearchParams(location.search).get("review") ?? undefined,
});
window.addEventListener("message", (event) => {
  if (event.source === frame.contentWindow) host.receive(event.data);
});
