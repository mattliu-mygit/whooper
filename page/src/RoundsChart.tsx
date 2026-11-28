import { createSignal, For, Show } from "solid-js";
import type { RoundsOfDay } from "./protocol";
import { plural } from "./text";

/** How a round ended, in the order the bars stack them from the baseline up and the legend lists them, each
 * with its word: red for a poll that failed, orange for a round stopped partway, gray for a round that
 * finished, so that a normal strip stays calm, and an outline for one whooper-run skipped while Whooper was
 * paused. app.css gives each its color. */
const RESULTS = [
  { id: "poll_failed", word: "Poll failed" },
  { id: "interrupted", word: "Interrupted" },
  { id: "done", word: "Done" },
  { id: "skipped", word: "Skipped while paused" },
] as const;

const roundsOf = (day: RoundsOfDay) => RESULTS.reduce((sum, result) => sum + day[result.id], 0);

/** The day's local date, such as 2031-07-17, at its local midnight; new Date("2031-07-17") would read it as
 * UTC and show the day before west of Greenwich. */
function dateOf(day: RoundsOfDay): Date {
  const [year, month, date] = day.date.split("-").map(Number);
  return new Date(year, month - 1, date);
}

/** A day's date and counts: as its bar's label, "Thu, Jul 17: 29 rounds, 29 poll failed", which a screen
 * reader says, and as the line below the strip once the day is chosen, "Thu, Jul 17 · 29 rounds · 29 poll
 * failed"; a day without rounds has "no rounds". */
function dayText(day: RoundsOfDay): { label: string; line: string } {
  const date = dateOf(day).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const rounds = roundsOf(day);
  const counts =
    rounds === 0
      ? ["no rounds"]
      : [plural(rounds, "round"), ...RESULTS.filter((result) => day[result.id] > 0).map((result) => `${day[result.id]} ${result.word.toLowerCase()}`)];
  return { label: `${date}: ${counts.join(", ")}`, line: [date, ...counts].join(" · ") };
}

/** "Jul 13", for the dates at the ends of the strip. */
const shortDay = (day: RoundsOfDay) => dateOf(day).toLocaleDateString([], { month: "short", day: "numeric" });

/**
 * Whooper's rounds per day, from whooper-status's rounds_by_day, below the causes on the Causes tab: one bar
 * per day, oldest first, stacked by how the day's rounds ended, under a legend that names each color with
 * its total over the days. A click on a bar shows that day's counts in the line below the strip, and so does
 * Enter on the bar j and k move to once the strip has the focus; the same bar again, or Escape, hides them.
 * Each bar's label says its date and counts, also for a day without rounds, whose slot stays empty.
 */
export function RoundsChart(props: { days: readonly RoundsOfDay[] }) {
  const [chosen, setChosen] = createSignal<string | null>(null);
  // The bar that holds the strip's one stop for Tab: the one last focused while it is listed, else today's.
  const [focused, setFocused] = createSignal<string | null>(null);
  const tabStop = () => (props.days.some((day) => day.date === focused()) ? focused() : props.days.at(-1)?.date);
  const most = () => Math.max(1, ...props.days.map(roundsOf));
  const total = (result: (typeof RESULTS)[number]["id"]) => props.days.reduce((sum, day) => sum + day[result], 0);
  const chosenDay = () => props.days.find((day) => day.date === chosen());

  return (
    <section class="w-rounds" aria-labelledby="rounds-heading">
      <h2 id="rounds-heading">Rounds per day, last {props.days.length} days</h2>
      <ul class="w-rounds-legend" aria-label="What each color means, with its total">
        <For each={RESULTS}>
          {(result) => (
            <li>
              <span class="w-rounds-key" data-result={result.id} />
              {result.word} · {total(result.id)}
            </li>
          )}
        </For>
      </ul>
      <div class="w-rounds-chart">
        <div
          class="w-rounds-strip"
          role="group"
          aria-label="Days, oldest first"
          onKeyDown={(event) => {
            if (event.metaKey || event.ctrlKey || event.altKey) return;
            if (event.key === "Escape" && chosen() !== null) {
              event.preventDefault();
              setChosen(null);
              return;
            }
            if (event.key !== "j" && event.key !== "k") return;
            event.preventDefault();
            // Around the ends, as j and k move through the lists' rows.
            const days = props.days;
            const at = days.findIndex((day) => day.date === event.target.getAttribute("data-date"));
            const next = days[(at + (event.key === "j" ? 1 : -1) + days.length) % days.length];
            event.currentTarget.querySelector<HTMLButtonElement>(`[data-date="${next.date}"]`)?.focus();
          }}
        >
          {/* Keyed by date, so a new read of the status keeps the focused bar. */}
          <For each={props.days} keyed={(day) => day.date}>
            {(day) => (
              <button
                type="button"
                class="w-rounds-bar"
                data-date={day().date}
                tabindex={day().date === tabStop() ? 0 : -1}
                aria-pressed={chosen() === day().date ? "true" : "false"}
                aria-label={dayText(day()).label}
                onFocus={() => setFocused(day().date)}
                onClick={(event) => {
                  // WebKit leaves a clicked button unfocused, and j and k move on from the focused bar.
                  event.currentTarget.focus();
                  setChosen(chosen() === day().date ? null : day().date);
                }}
              >
                <For each={RESULTS.filter((result) => day()[result.id] > 0)}>
                  {(result) => (
                    <span class="w-rounds-part" data-result={result.id} style={{ "flex-basis": `${(day()[result.id] / most()) * 100}%` }} />
                  )}
                </For>
              </button>
            )}
          </For>
        </div>
        <p class="w-rounds-axis" aria-hidden="true">
          <span>{shortDay(props.days[0])}</span>
          <span>{shortDay(props.days[props.days.length - 1])}</span>
        </p>
      </div>
      <Show when={chosenDay()} fallback={<p class="w-rounds-day w-rounds-hint">Click a day for its counts, or move between days with j and k and press Enter.</p>}>
        {(day) => <p class="w-rounds-day">{dayText(day()).line}</p>}
      </Show>
    </section>
  );
}
