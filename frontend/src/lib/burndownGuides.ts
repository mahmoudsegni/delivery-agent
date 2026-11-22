/**
 * Reference geometry for the burndown chart: the dashed p50 forecast segment.
 *
 * The ideal line already arrives from the server — one `ideal_points` value
 * per sprint day, unit-aware — so the only client-side math is the forecast
 * projection. LineChart plots every series by *index* against the longest
 * one, so the projection has to be aligned onto the burndown's daily grid
 * (extended past the sprint end when the crossing lands beyond it), not
 * shipped as a bare two-point segment.
 */

export interface BurndownDay {
  date: string;
  ideal: number | null;
  remaining: number | null;
}

export function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function withProjection(
  days: BurndownDay[],
  today: string,
  p50Days: number | null,
) {
  const dates = days.map((d) => d.date);
  const last = days.reduce((acc, d, i) => (d.remaining != null ? i : acc), -1);
  // Math.round is half-up for positives: 2.5 days crosses on day 3.
  const crossing = p50Days != null && last >= 0 ? addDays(today, Math.round(p50Days)) : null;

  // The spine must reach the crossing or the segment is clipped off the chart.
  if (crossing != null && crossing > dates[dates.length - 1]) {
    let d = dates[dates.length - 1];
    while (d < crossing) {
      d = addDays(d, 1);
      dates.push(d);
    }
  }

  const projection: (number | null)[] = dates.map(() => null);
  const crossIdx = crossing != null ? dates.indexOf(crossing) : -1;
  if (crossIdx > last && last >= 0) {
    const start = days[last].remaining as number;
    for (let i = last; i <= crossIdx; i++) {
      projection[i] = start * (1 - (i - last) / (crossIdx - last));
    }
  }

  const pad = (values: (number | null)[]) => [
    ...values,
    ...dates.slice(values.length).map(() => null),
  ];
  return {
    dates,
    ideal: pad(days.map((d) => d.ideal)),
    actual: pad(days.map((d) => d.remaining)),
    projection,
  };
}
