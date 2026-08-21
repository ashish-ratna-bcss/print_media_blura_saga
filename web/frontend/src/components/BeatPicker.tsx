import { Check, ChevronDown, MapPin, Search, ShieldCheck } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { Beat } from "../types";
import styles from "./BeatPicker.module.css";

/**
 * Choose one of the 36 state and union-territory forces, by typing.
 *
 * This was a native select, chosen so it would get type-ahead and keyboard
 * handling for free. At 36 entries that stopped being enough: a native select
 * only type-aheads on the option's leading characters, so reaching Himachal
 * Pradesh Police means typing "himachal" exactly, and there is no way at all
 * to find Punjab by remembering that Ludhiana is in it.
 *
 * So: a real combobox. What it buys over the select —
 *
 *   - matches anywhere in the name, not just the start
 *   - matches the Hindi name and the two-letter code
 *   - MATCHES PLACE NAMES. Typing "Ludhiana" finds Punjab Police and says
 *     why. The beat already ships its districts for the coverage check, so
 *     this is free, and it is how people actually think about a story.
 *   - two lines per row, so long names stop truncating mid-word
 *
 * The keyboard contract a select gave us is reimplemented rather than lost:
 * arrows move, Enter commits, Escape reverts, Tab closes, and the listbox is
 * wired up with aria-activedescendant so a screen reader follows along.
 */

type Scored = { beat: Beat; why: string };

function norm(s: string) {
  return (s || "").toLowerCase().trim();
}

/** Rank matches so the most likely one is first and pre-highlighted. */
function search(beats: Beat[], raw: string): Scored[] {
  const q = norm(raw);
  if (!q) return beats.map((beat) => ({ beat, why: "" }));

  const out: { beat: Beat; why: string; rank: number }[] = [];
  for (const beat of beats) {
    const label = norm(beat.label);
    const state = norm(beat.state);
    const code = norm(beat.state_code);

    if (code === q) out.push({ beat, why: "", rank: 0 });
    else if (state.startsWith(q) || label.startsWith(q)) out.push({ beat, why: "", rank: 1 });
    else if (state.includes(q) || label.includes(q)) out.push({ beat, why: "", rank: 2 });
    else if ((beat.state_hi || "").includes(raw.trim())) out.push({ beat, why: "", rank: 3 });
    else {
      // Place-name fallback, and it reports which place matched — otherwise a
      // row appearing for "Ludhiana" reads as a bug rather than a feature.
      const place = (beat.places || []).find((p) => norm(p).startsWith(q));
      const loose = place || (beat.places || []).find((p) => norm(p).includes(q));
      if (loose) out.push({ beat, why: loose, rank: place ? 4 : 5 });
    }
  }
  out.sort((a, b) => a.rank - b.rank || a.beat.label.localeCompare(b.beat.label));
  return out.map(({ beat, why }) => ({ beat, why }));
}

export function BeatPicker({
  beats,
  value,
  onChange,
  id,
  label = "Beat",
  anyLabel,
  hint,
  showHint = true,
}: {
  beats: Beat[];
  value: string;
  onChange: (id: string) => void;
  id: string;
  label?: string;
  /** When set, a "clear the choice" row is offered with this wording. */
  anyLabel?: string;
  hint?: string;
  /**
   * The counts line under the control. Off inside a row of filters that
   * bottom-aligns its fields, where an extra line would lift this control
   * out of line with its neighbours for information nobody is reading while
   * filtering.
   */
  showHint?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();

  const chosen = beats.find((b) => b.id === value) ?? null;
  const results = useMemo(() => search(beats, query), [beats, query]);

  // Grouped only when nothing is typed. Once filtering, relevance order is
  // more useful than "union territories first" and headers just add noise.
  const grouped = !query.trim();
  const rows: Scored[] = grouped
    ? [
        ...results.filter((r) => r.beat.region_kind === "ut"),
        ...results.filter((r) => r.beat.region_kind === "state"),
        ...results.filter(
          (r) => r.beat.region_kind !== "ut" && r.beat.region_kind !== "state",
        ),
      ]
    : results;

  useEffect(() => setActive(0), [query, open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  // Keep the highlighted row visible when arrowing past the fold.
  useEffect(() => {
    if (!open) return;
    document
      .getElementById(`${listId}-${active}`)
      ?.scrollIntoView({ block: "nearest" });
  }, [active, open, listId]);

  function commit(beatId: string) {
    onChange(beatId);
    setQuery("");
    setOpen(false);
    inputRef.current?.blur();
  }

  const total = rows.length + (anyLabel ? 1 : 0);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => (i + step + total) % total);
    } else if (e.key === "Enter") {
      if (!open) return;
      e.preventDefault();
      if (anyLabel && active === 0) commit("");
      else {
        const row = rows[anyLabel ? active - 1 : active];
        if (row) commit(row.beat.id);
      }
    } else if (e.key === "Escape") {
      if (!open) return;
      e.preventDefault();
      setQuery("");
      setOpen(false);
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  const row = (content: React.ReactNode, index: number, onPick: () => void, key: string) => (
    <li
      key={key}
      id={`${listId}-${index}`}
      role="option"
      aria-selected={index === active}
      className={`${styles.opt} ${index === active ? styles.optActive : ""}`}
      onPointerEnter={() => setActive(index)}
      onClick={onPick}
    >
      {content}
    </li>
  );

  let i = -1;

  return (
    <div className={styles.wrap} ref={rootRef}>
      <label className="field-label" htmlFor={id}>
        {label}
      </label>

      <div className={`${styles.control} ${open ? styles.controlOpen : ""}`}>
        <Search size={13} className={styles.leadIcon} aria-hidden="true" />
        <input
          id={id}
          ref={inputRef}
          type="text"
          role="combobox"
          autoComplete="off"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={open ? `${listId}-${active}` : undefined}
          className={styles.input}
          value={open ? query : (chosen?.label ?? "")}
          placeholder={
            open
              ? (chosen?.label ?? `Search ${beats.length} forces or a district…`)
              : (anyLabel ?? "Select…")
          }
          onChange={(e) => {
            setQuery(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
        />
        <ChevronDown
          size={14}
          className={styles.caret}
          aria-hidden="true"
          onPointerDown={(e) => {
            e.preventDefault();
            setOpen((v) => !v);
            inputRef.current?.focus();
          }}
        />
      </div>

      {open && (
        <ul className={styles.list} role="listbox" id={listId}>
          {anyLabel &&
            row(<span className={styles.anyRow}>{anyLabel}</span>, ++i, () => commit(""), "any")}

          {rows.length === 0 && (
            <li className={styles.empty}>
              No force matches “{query}”. Try a state, a district, or a code like PB.
            </li>
          )}

          {rows.map((r, n) => {
            const idx = ++i;
            const first = grouped && n === 0;
            const firstState =
              grouped &&
              r.beat.region_kind === "state" &&
              rows[n - 1]?.beat.region_kind === "ut";
            return (
              <div key={r.beat.id}>
                {first && <div className={styles.group}>Union territories</div>}
                {firstState && <div className={styles.group}>States</div>}
                {row(
                  <>
                    <span className={styles.optMain}>
                      <span className={styles.optName}>{r.beat.label}</span>
                      {r.beat.id === value && (
                        <Check size={13} className={styles.tick} aria-hidden="true" />
                      )}
                    </span>
                    <span className={styles.optMeta}>
                      {r.beat.state_hi && <span className={styles.hi}>{r.beat.state_hi}</span>}
                      <span>{r.beat.n_terms} terms</span>
                      <span>{r.beat.n_places} places</span>
                      {r.why && (
                        <span className={styles.why}>
                          <MapPin size={10} />
                          {r.why}
                        </span>
                      )}
                    </span>
                  </>,
                  idx,
                  () => commit(r.beat.id),
                  r.beat.id,
                )}
              </div>
            );
          })}
        </ul>
      )}

      {showHint && (
        <p className={styles.hint}>
          {chosen ? (
            <>
              <ShieldCheck size={12} />
              {chosen.n_terms} terms · {chosen.n_places} places
              {chosen.builtin ? "" : " · custom"}
            </>
          ) : (
            hint || `${beats.length} forces available`
          )}
        </p>
      )}
    </div>
  );
}
