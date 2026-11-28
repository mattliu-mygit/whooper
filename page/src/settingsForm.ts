import { changedSettings, fieldKey, leafFields, settingFields, settingValue, valueAt, type FormValue, type LeafField } from "@prompting-time/ui";
import schema from "../../schemas/settings.json";

// The Settings tab's fields, drawn from schemas/settings.json, the schema Prompting Time's settings
// form and whooper-settings read too, by the same settings-form logic Prompting Time's form uses, so
// the tab, that form, and the prerequisites Prompting Time checks against the settings' [jobs] all
// read and write the same values. Like Prompting Time's form, the tab sends only the settings it
// changed, nested as the settings nest, and whooper-settings set checks them again and rewrites only
// those lines of config.toml. What stays here is what Whooper checks beyond the schema.

/** The settings as whooper-settings get prints them. */
export type Settings = Record<string, unknown>;

/** The tab's top-level fields, one group per table of config.toml. */
export const FIELDS = settingFields(JSON.stringify(schema));
const LEAVES = leafFields(FIELDS);

/** An emptied input: a number, text, or choice holding only spaces, which saves nothing. */
const isEmpty = (field: LeafField, value: FormValue) => field.kind !== "list" && typeof value === "string" && value.trim() === "";

/** The bounds JSON Schema gives a number, which the schema may set on any number setting. */
export interface Bounds {
  minimum?: number;
  exclusiveMinimum?: number;
  maximum?: number;
  exclusiveMaximum?: number;
}
type SchemaNode = Bounds & { properties?: Record<string, SchemaNode> };

/** The schema's bounds for the setting at path. settingFields keeps only minimum, so they come from the schema itself. */
const boundsAt = (path: readonly string[]): Bounds => path.reduce<SchemaNode>((node, key) => node.properties?.[key] ?? {}, schema as SchemaNode);

/**
 * Why number breaks bounds, or null, in the words of the refusal whooper-settings set prints
 * (check_schema in lib/whooper.py): where it says "is below 1", this asks for 1 or more, and where it
 * says "is not above 0", for a number above 0. check_schema has no maximum or exclusiveMaximum yet;
 * for those of 60, this asks for 60 or less, or for a number below 60.
 */
export function boundsProblem(number: number, bounds: Bounds): string | null {
  if (bounds.minimum !== undefined && number < bounds.minimum) return `Enter ${bounds.minimum} or more.`;
  if (bounds.exclusiveMinimum !== undefined && number <= bounds.exclusiveMinimum) return `Enter a number above ${bounds.exclusiveMinimum}.`;
  if (bounds.maximum !== undefined && number > bounds.maximum) return `Enter ${bounds.maximum} or less.`;
  if (bounds.exclusiveMaximum !== undefined && number >= bounds.exclusiveMaximum) return `Enter a number below ${bounds.exclusiveMaximum}.`;
  return null;
}

/** A 24-hour time as whooper-run reads [schedule] start and end (strptime's %H:%M), in minutes. */
function minutesOf(text: string): number | null {
  const match = /^(\d{1,2}):(\d{1,2})$/.exec(text.trim());
  if (!match || Number(match[1]) > 23 || Number(match[2]) > 59) return null;
  return Number(match[1]) * 60 + Number(match[2]);
}

/**
 * Why each field's value would be refused, by field key, before anything is sent: what the schema
 * says, a number's bounds included, and what whooper-settings set checks beyond it before it saves
 * (read_schedule in lib/whooper.py): start and end as 24-hour times with the end after the start,
 * and at least one day. whooper-settings set can't take a setting out of config.toml, so an emptied
 * field that config.toml gives a value is refused too.
 */
export function problems(saved: Settings, value: (field: LeafField) => FormValue): Record<string, string> {
  const found: Record<string, string> = {};
  for (const field of LEAVES) {
    const text = value(field);
    if (isEmpty(field, text)) {
      if (valueAt(saved, field.path) !== undefined) found[fieldKey(field)] = "Enter a value. To leave it out, delete its line in config.toml.";
      continue;
    }
    if (field.kind === "integer" || field.kind === "number") {
      const number = settingValue(field, text) as number;
      const outside = boundsProblem(number, boundsAt(field.path));
      if (!Number.isFinite(number)) found[fieldKey(field)] = "Enter a number.";
      else if (field.kind === "integer" && !Number.isInteger(number)) found[fieldKey(field)] = "Enter a whole number.";
      else if (outside) found[fieldKey(field)] = outside;
    }
  }
  const schedule = (key: string) => LEAVES.find((field) => field.path.join(".") === `schedule.${key}`)!;
  const [start, end, days] = ["start", "end", "days"].map(schedule);
  const [startMinutes, endMinutes] = [start, end].map((field) => minutesOf(String(value(field))));
  if (startMinutes === null) found[fieldKey(start)] ??= "Enter a 24-hour time such as 18:00.";
  if (endMinutes === null) found[fieldKey(end)] ??= "Enter a 24-hour time such as 18:00.";
  if (startMinutes !== null && endMinutes !== null && endMinutes <= startMinutes) found[fieldKey(end)] = "Enter a time after the start.";
  if ((value(days) as string[]).length === 0) found[fieldKey(days)] = "Choose at least one day.";
  return found;
}

/**
 * Only the settings the form changed, as Prompting Time's form sends them, with the spaces around
 * each value dropped first, since whooper-settings set saves text as it comes and read_schedule
 * would refuse " 18:00".
 */
export function toSave(saved: Settings, draft: Readonly<Record<string, FormValue>>): Settings {
  const trimmed = Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, typeof value === "string" ? value.trim() : value]));
  return changedSettings(FIELDS, saved, trimmed);
}
