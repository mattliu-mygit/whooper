import { createMemo, createSignal, createUniqueId, For, onSettled, Show } from "solid-js";
import { Button, Checkbox, fieldKey, formValue, NumberField, Select, Textarea, TextField, valueAt, type FormValue, type LeafField, type SettingField } from "@prompting-time/ui";
import { FIELDS, problems, toSave, type Settings } from "./settingsForm";
import type { PageStore } from "./store";
import { TabHeader } from "./TabHeader";

/** The next round's time, "3:45 PM", with its day, "Mon 10:00 AM", when it isn't today. */
function roundTime(iso: string, now = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const today = date.toDateString() === now.toDateString();
  return date.toLocaleString([], { ...(today ? {} : { weekday: "short" }), hour: "numeric", minute: "2-digit" });
}
const optionLabel = (option: string) => option.charAt(0).toUpperCase() + option.slice(1);

/**
 * When saved settings take effect, from what changed. Nothing restarts: whooper-run reads [schedule]
 * each time it sets the next round, and whooper-poll, whooper-review, and whooper-ask read the config
 * as each starts, so a run that already started keeps the old settings.
 */
export function appliesWhen(changes: Settings, nextRound: string | null): string {
  const parts: string[] = [];
  if (changes.schedule)
    parts.push(nextRound ? `The round already set for ${roundTime(nextRound)} still starts then; the new schedule times the rounds after it.` : "The new schedule times the next round whooper-run sets.");
  if (changes.poll || changes.jobs || changes.agents || changes.review)
    parts.push("Each poll and review that starts from now on uses the new settings; one already running keeps the old ones.");
  if (changes.ask) parts.push("The next question you ask uses the new time limit.");
  return ["Saved.", ...parts].join(" ");
}

/**
 * The Settings tab at /settings: the settings schemas/settings.json describes, read with the manifest's
 * get-settings action and saved with save-settings, the two Prompting Time's settings form calls, so
 * both edit the same config.toml lines and Prompting Time's check for each agent's CLI follows [jobs].
 * Each field shows why its value would be refused as it changes, and Save sends only the changed
 * settings once none is refused. whooper-settings set checks them again and refuses with its reason.
 * After a save, the tab says when the change applies.
 */
export function SettingsTab(props: { store: PageStore }) {
  const store = props.store;
  const [saved, setSaved] = createSignal<Settings | null>(null);
  const [loadError, setLoadError] = createSignal<string | null>(null);
  const [draft, setDraft] = createSignal<Readonly<Record<string, FormValue>>>({});
  const [saving, setSaving] = createSignal(false);
  const [refusal, setRefusal] = createSignal<string | null>(null);
  const [note, setNote] = createSignal<string | null>(null);
  let disposed = false;

  const value = (field: LeafField) => draft()[fieldKey(field)] ?? formValue(field, valueAt(saved(), field.path));
  const errors = createMemo(() => (saved() ? problems(saved()!, value) : {}));
  const changes = createMemo(() => (saved() ? toSave(saved()!, draft()) : {}));
  const dirty = () => Object.keys(changes()).length > 0;
  const refused = () => Object.keys(errors()).length > 0;

  async function load() {
    setLoadError(null);
    try {
      const settings = await store.readSettings();
      if (!disposed) setSaved(settings);
    } catch (e) {
      if (!disposed) setLoadError((e as Error).message);
    }
  }

  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!dirty() || refused() || saving()) return;
    const sent = changes();
    setSaving(true);
    setRefusal(null);
    setNote(null);
    try {
      await store.saveSettings(sent);
      // Show what whooper-settings saved, which may read differently than what the form sent.
      const settings = await store.readSettings();
      if (disposed) return;
      setSaved(settings);
      setDraft({});
      setNote(appliesWhen(sent, store.state.status?.next_round ?? null));
    } catch (e) {
      if (!disposed) setRefusal((e as Error).message);
    } finally {
      if (!disposed) setSaving(false);
    }
  }

  function change(field: LeafField, next: FormValue) {
    setDraft({ ...draft(), [fieldKey(field)]: next });
    setNote(null);
  }

  onSettled(() => {
    void load();
    return () => {
      disposed = true;
    };
  });

  return (
    <>
      <TabHeader store={store} title="Settings" words="Whooper's config.toml" />
      <form class="w-tab-body w-settings" aria-label="Whooper settings" aria-busy={saving() ? "true" : "false"} novalidate onSubmit={(event) => void save(event)}>
        <Show
          when={saved()}
          fallback={
            <Show when={loadError()} fallback={<p class="w-empty" role="status">Reading the settings…</p>}>
              {(error) => (
                <div class="w-error" role="alert">
                  <p>Whooper could not print its settings: {error()}</p>
                  <Button onClick={() => void load()}>Read settings again</Button>
                </div>
              )}
            </Show>
          }
        >
          <For each={FIELDS}>{(field) => <FieldView field={field} value={value} errors={errors()} onChange={change} disabled={saving()} />}</For>
          <div class="w-save-bar">
            <div class="w-save-text">
              <Show
                when={refusal()}
                fallback={
                  <p class="w-hint" role="status">
                    {note() ?? "Save checks the settings and writes only the ones you changed. Nothing restarts: each round, poll, review, and question that starts after the save uses them."}
                  </p>
                }
              >
                {(text) => (
                  <p class="w-error w-refusal" role="alert">
                    {text()}
                  </p>
                )}
              </Show>
            </div>
            <Button
              disabled={!dirty() || saving()}
              onClick={() => {
                setDraft({});
                setRefusal(null);
              }}
            >
              Revert
            </Button>
            <Button type="submit" variant="primary" disabled={!dirty() || refused() || saving()}>
              {saving() ? "Saving…" : "Save"}
            </Button>
          </div>
        </Show>
      </form>
    </>
  );
}

/** One field with why its value would be refused, or a group of fields as a fieldset with its title as the legend. */
function FieldView(props: { field: SettingField; value: (field: LeafField) => FormValue; errors: Record<string, string>; onChange(field: LeafField, value: FormValue): void; disabled: boolean }) {
  const field = props.field;
  const hint = field.description ?? undefined;
  const error = () => (field.kind === "group" ? undefined : props.errors[fieldKey(field)]);
  switch (field.kind) {
    case "group":
      return (
        <fieldset class="w-settings-group">
          <legend>{field.title}</legend>
          <Show when={hint}>
            <p class="w-hint">{hint}</p>
          </Show>
          <For each={field.fields}>{(child) => <FieldView field={child} value={props.value} errors={props.errors} onChange={props.onChange} disabled={props.disabled} />}</For>
        </fieldset>
      );
    case "integer":
    case "number":
      return (
        <NumberField
          label={field.title}
          unit={field.unit ?? undefined}
          hint={hint}
          error={error()}
          min={field.minimum ?? undefined}
          step={field.kind === "integer" ? 1 : "any"}
          value={props.value(field) as string}
          disabled={props.disabled}
          onInput={(event) => props.onChange(field, event.currentTarget.value)}
        />
      );
    case "text":
      return (
        <TextField label={field.title} hint={hint} error={error()} value={props.value(field) as string} disabled={props.disabled} onInput={(event) => props.onChange(field, event.currentTarget.value)} />
      );
    case "choice": {
      // An unset choice shows as unset rather than as the first option, which would read as chosen.
      const options = () => [
        ...(props.value(field) === "" ? [{ value: "", label: "Not set" }] : []),
        ...field.options.map((option) => ({ value: option, label: optionLabel(option) })),
      ];
      return (
        <Select label={field.title} hint={hint} error={error()} options={options()} value={props.value(field) as string} disabled={props.disabled} onChange={(event) => props.onChange(field, event.currentTarget.value)} />
      );
    }
    case "choices":
      return (
        <fieldset class="w-settings-choices" aria-invalid={error() ? "true" : undefined}>
          <legend>{field.title}</legend>
          <div class="w-settings-choice-row">
            <For each={field.options}>
              {(option) => (
                <Checkbox
                  label={optionLabel(option)}
                  checked={(props.value(field) as string[]).includes(option)}
                  disabled={props.disabled}
                  onChange={(event) => {
                    const chosen = props.value(field) as string[];
                    props.onChange(field, event.currentTarget.checked ? [...chosen, option] : chosen.filter((item) => item !== option));
                  }}
                />
              )}
            </For>
          </div>
          <Show when={hint}>
            <p class="w-hint">{hint}</p>
          </Show>
          <Show when={error()}>{(text) => <p class="w-error">{text()}</p>}</Show>
        </fieldset>
      );
    case "list": {
      const id = createUniqueId();
      return (
        <div class="w-settings-list">
          <label class="w-settings-label" for={id}>
            {field.title}
          </label>
          <Textarea
            id={id}
            label={field.title}
            rows={3}
            grow
            value={props.value(field) as string}
            disabled={props.disabled}
            aria-describedby={`${id}-hint`}
            onInput={(event) => props.onChange(field, event.currentTarget.value)}
          />
          <p id={`${id}-hint`} class="w-hint">
            One per line.{hint ? ` ${hint}` : ""}
          </p>
        </div>
      );
    }
  }
}
