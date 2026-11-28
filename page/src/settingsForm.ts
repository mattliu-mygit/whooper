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
  for (const field of LEAVES) {
    const text = value(field);
    if (isEmpty(field, text)) {
      if (valueAt(saved, field.path) !== undefined) found[fieldKey(field)] = "Enter a value. To leave it out, delete its line in config.toml.";
      continue;
    }
    if (field.kind === "integer" || field.kind === "number") {
      const number = settingValue(field, text) as number;
      if (!Number.isFinite(number)) found[fieldKey(field)] = "Enter a number.";
      else if (field.kind === "integer" && !Number.isInteger(number)) found[fieldKey(field)] = "Enter a whole number.";
      else if (field.minimum !== null && number < field.minimum) found[fieldKey(field)] = `Enter ${field.minimum} or more.`;
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
