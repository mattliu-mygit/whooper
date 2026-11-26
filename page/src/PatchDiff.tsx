import { FileDiff, type DiffLineAnnotation, type FileDiffMetadata, type FileDiffOptions } from "@pierre/diffs";
import { createEffect, createSignal, Errored, For, onSettled, untrack } from "solid-js";
import { Portal, type JSX } from "@solidjs/web";
import { DIFF_THEME, registerDiffTheme } from "./diffTheme";

// @prompting-time/ui's Diff, which takes a file's old and new contents, with one change: it draws a
// file parsed from a patch. A review keeps only diff.patch, so the page has no whole files. Delete
// this file and diffTheme.ts once the package's Diff takes a parsed patch; the props and the
// .pt-diff styles are the same. Comments on the package's Diff explain the rest.

export interface PatchDiffProps<T> {
  file: FileDiffMetadata;
  layout?: "split" | "unified";
  annotations?: readonly DiffLineAnnotation<T>[];
  annotationKey?: (annotation: DiffLineAnnotation<T>) => string;
  renderAnnotation?: (annotation: () => DiffLineAnnotation<T>) => JSX.Element;
}

interface Placed<T> {
  annotation: DiffLineAnnotation<T>;
  element: HTMLElement;
}

export function PatchDiff<T>(props: PatchDiffProps<T>) {
  registerDiffTheme();
  let wrapper!: HTMLDivElement;
  let instance: FileDiff<T> | undefined;
  const [placed, setPlaced] = createSignal<ReadonlyMap<string, Placed<T>>>(new Map());

  const options = (layout: "split" | "unified"): FileDiffOptions<T, undefined> => ({
    theme: DIFF_THEME,
    themeType: "dark",
    diffStyle: layout,
    renderAnnotation: (annotation) => {
      const element = document.createElement("div");
      const key = props.annotationKey?.(annotation) ?? `${annotation.side}:${annotation.lineNumber}`;
      setPlaced((map) => new Map(map).set(key, { annotation, element }));
      return element;
    },
    onPostRender: () => setPlaced((map) => new Map([...map].filter(([, entry]) => entry.element.isConnected))),
  });

  createEffect(
    () => ({ file: props.file, layout: props.layout ?? "split", annotations: props.annotations ?? [] }),
    ({ file, layout, annotations }) => {
      if (instance) instance.setOptions(options(layout));
      else instance = new FileDiff<T>(options(layout));
      instance.render({ fileDiff: file, lineAnnotations: [...annotations], containerWrapper: wrapper, forceRender: true });
    },
  );
  onSettled(() => () => instance?.cleanUp());

  return (
    <div class="pt-diff">
      <div ref={wrapper} />
      <For each={[...placed()]} keyed={([key]) => key}>
        {(entry) => (
          <Portal mount={entry()[1].element}>
            <Errored fallback={(error) => <p class="pt-diff-thread-error">This comment could not be shown: {String(error())}</p>}>
              {untrack(() => props.renderAnnotation?.(() => entry()[1].annotation))}
            </Errored>
          </Portal>
        )}
      </For>
    </div>
  );
}
