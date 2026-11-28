import schema from "../../schemas/settings.json";

// The Settings tab's fields, drawn from schemas/settings.json, the schema Prompting Time's settings
// form and whooper-settings read too, so the tab, that form, and the prerequisites Prompting Time
// checks against the settings' [jobs] all read and write the same values. Like Prompting Time's form,
// the tab sends only the settings it changed, nested as the settings nest, and whooper-settings set
// checks them again and rewrites only those lines of config.toml.

/** One field, or a group of them. `path` is the setting's place in the settings object. */
export type SettingField =
  | { kind: "group"; path: string[]; title: string; description: string | null; fields: SettingField[] }
  | { kind: "integer" | "number"; path: string[]; title: string; description: string | null; unit: string | null; minimum: number | null }
  | { kind: "text" | "list"; path: string[]; title: string; description: string | null }
  | { kind: "choice" | "choices"; path: string[]; title: string; description: string | null; options: string[] };

export type LeafField = Exclude<SettingField, { kind: "group" }>;
/** What a field's control holds: text for inputs and lists, one line per item, or the chosen options. */
export type FormValue = string | string[];
/** The settings as whooper-settings get prints them. */
export type Settings = Record<string, unknown>;

interface Schema {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, Schema>;
  enum?: unknown[];
  items?: Schema;
  minimum?: number;
}

function fieldsOf(node: Schema, path: string[]): SettingField[] {
  return Object.entries(node.properties ?? {}).flatMap(([key, child]) => {
    const field = fieldFor(child, [...path, key], key);
    return field ? [field] : [];
  });
}

function fieldFor(node: Schema, path: string[], key: string): SettingField | null {
  const title = node.title ?? key;
  const description = node.description ?? null;
  if (node.properties) return { kind: "group", path, title, description, fields: fieldsOf(node, path) };
  if (isStrings(node.enum)) return { kind: "choice", path, title, description, options: node.enum };
  switch (node.type) {
    case "integer":
    case "number": {
      // "Poll every (minutes)" reads as the label "Poll every" with the unit after the input.
      const unit = /^(.*\S)\s*\(([^()]+)\)$/.exec(title);
      return { kind: node.type, path, title: unit?.[1] ?? title, unit: unit?.[2] ?? null, description, minimum: node.minimum ?? null };
    }
    case "string":
      return { kind: "text", path, title, description };
    case "array":
      if (isStrings(node.items?.enum)) return { kind: "choices", path, title, description, options: node.items.enum };
      return node.items?.type === "string" ? { kind: "list", path, title, description } : null;
    default:
      return null;
  }
}

function isStrings(values: unknown[] | undefined): values is string[] {
  return Array.isArray(values) && values.length > 0 && values.every((value) => typeof value === "string");
}

/** The tab's top-level fields, one group per table of config.toml. */
export const FIELDS: SettingField[] = fieldsOf(schema as Schema, []);

export function leafFields(fields: readonly SettingField[] = FIELDS): LeafField[] {
  return fields.flatMap((field) => (field.kind === "group" ? leafFields(field.fields) : [field]));
}

export const fieldKey = (field: { path: string[] }) => field.path.join(".");

/** The setting at `path`, or undefined when the settings have none. */
export function valueAt(values: unknown, path: readonly string[]): unknown {
  return path.reduce<unknown>((value, key) => (typeof value === "object" && value !== null ? (value as Record<string, unknown>)[key] : undefined), values);
}

/** The control's value for a setting's value. A list shows one item per line. */
export function formValue(field: LeafField, value: unknown): FormValue {
  switch (field.kind) {
    case "integer":
    case "number":
      return typeof value === "number" ? String(value) : "";
    case "text":
    case "choice":
      return typeof value === "string" ? value : "";
    case "choices":
      return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
    case "list":
      return Array.isArray(value) ? value.filter((item) => typeof item === "string").join("\n") : "";
  }
}

/** The setting's value for what the control holds, or undefined for an empty field, which saves nothing. */
export function settingValue(field: LeafField, value: FormValue): unknown {
  switch (field.kind) {
    case "integer":
    case "number":
      return String(value).trim() === "" ? undefined : Number(value);
    case "text":
    case "choice":
      return String(value).trim() === "" ? undefined : String(value).trim();
    case "choices":
      return field.options.filter((option) => (value as string[]).includes(option));
    case "list":
      return String(value).split("\n").map((line) => line.trim()).filter(Boolean);
  }
}

/** A 24-hour time as whooper-run reads [schedule] start and end (strptime's %H:%M), in minutes. */
function minutesOf(text: string): number | null {
  const match = /^(\d{1,2}):(\d{1,2})$/.exec(text.trim());
  if (!match || Number(match[1]) > 23 || Number(match[2]) > 59) return null;
  return Number(match[1]) * 60 + Number(match[2]);
}

/**
 * Why each field's value would be refused, by field key, before anything is sent: what the schema
 * says, and what whooper-settings set checks beyond it before it saves (read_schedule in
 * lib/whooper.py): start and end as 24-hour times with the end after the start, and at least one day.
 * whooper-settings set can't take a setting out of config.toml, so an emptied field that config.toml
 * gives a value is refused too.
 */
export function problems(saved: Settings, value: (field: LeafField) => FormValue): Record<string, string> {
  const found: Record<string, string> = {};
  for (const field of leafFields()) {
    const text = value(field);
    const next = settingValue(field, text);
    if (next === undefined) {
      if (valueAt(saved, field.path) !== undefined) found[fieldKey(field)] = "Enter a value. To leave it out, delete its line in config.toml.";
      continue;
    }
    if (field.kind === "integer" || field.kind === "number") {
      const number = next as number;
      if (!Number.isFinite(number)) found[fieldKey(field)] = "Enter a number.";
      else if (field.kind === "integer" && !Number.isInteger(number)) found[fieldKey(field)] = "Enter a whole number.";
      else if (field.minimum !== null && number < field.minimum) found[fieldKey(field)] = `Enter ${field.minimum} or more.`;
    }
  }
  const schedule = (key: string) => leafFields().find((field) => fieldKey(field) === `schedule.${key}`)!;
  const [start, end] = ["start", "end"].map((key) => minutesOf(String(value(schedule(key)))));
  for (const [key, minutes] of [["start", start], ["end", end]] as const)
    if (minutes === null) found[`schedule.${key}`] ??= "Enter a 24-hour time such as 18:00.";
  if (start !== null && end !== null && end <= start) found["schedule.end"] = "Enter a time after the start.";
  if ((value(schedule("days")) as string[]).length === 0) found["schedule.days"] = "Choose at least one day.";
  return found;
}

/**
 * Only the settings the form changed, nested as the settings nest, such as
 * `{"schedule": {"interval_minutes": 30}}`, so whooper-settings rewrites only those lines and a save
 * from Prompting Time's form at the same time keeps its own changes.
 */
export function changedSettings(saved: Settings, draft: Readonly<Record<string, FormValue>>): Settings {
  const changes: Settings = {};
  for (const field of leafFields()) {
    const value = draft[fieldKey(field)];
    if (value === undefined) continue;
    const next = settingValue(field, value);
    // Both sides go through the control's value, so "15.0" for 15, or a list's blank line, is no change.
    if (next === undefined || JSON.stringify(next) === JSON.stringify(settingValue(field, formValue(field, valueAt(saved, field.path))))) continue;
    let target = changes;
    for (const key of field.path.slice(0, -1)) target = (target[key] ??= {}) as Settings;
    target[field.path.at(-1)!] = next;
  }
  return changes;
}
