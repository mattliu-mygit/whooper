import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "./render";

afterEach(cleanup);

// jsdom has no ResizeObserver, which Pierre and Zag.js observe sizes with.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
