import { useEffect, useState } from "react";
import { api } from "../api";
import type { DateOption, Edition, Paper } from "../types";

/**
 * Cascading paper -> edition -> date selection, shared by the New Extraction
 * and Topic Search views. Each level resets and reloads whenever the level
 * above it changes.
 */
export function useCatalogue() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [papersLoading, setPapersLoading] = useState(true);
  const [papersError, setPapersError] = useState("");

  const [host, setHost] = useState("");
  const [editions, setEditions] = useState<Edition[]>([]);
  const [editionsLoading, setEditionsLoading] = useState(false);
  const [editionsError, setEditionsError] = useState("");

  const [titleId, setTitleId] = useState("");
  const [dates, setDates] = useState<DateOption[]>([]);
  const [datesLoading, setDatesLoading] = useState(false);
  const [datesError, setDatesError] = useState("");

  const [date, setDate] = useState("");

  useEffect(() => {
    let alive = true;
    api
      .papers()
      .then((ps) => alive && setPapers(ps))
      .catch((e) => alive && setPapersError(e.message))
      .finally(() => alive && setPapersLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    setEditions([]);
    setTitleId("");
    setEditionsError("");
    if (!host) return;
    let alive = true;
    setEditionsLoading(true);
    api
      .editions(host)
      .then((es) => alive && setEditions(es))
      .catch((e) => alive && setEditionsError(e.message))
      .finally(() => alive && setEditionsLoading(false));
    return () => {
      alive = false;
    };
  }, [host]);

  useEffect(() => {
    setDates([]);
    setDate("");
    setDatesError("");
    if (!host || !titleId) return;
    let alive = true;
    setDatesLoading(true);
    api
      .dates(host, titleId)
      .then((ds) => alive && setDates(ds))
      .catch((e) => alive && setDatesError(e.message))
      .finally(() => alive && setDatesLoading(false));
    return () => {
      alive = false;
    };
  }, [host, titleId]);

  const paper = papers.find((p) => p.host === host) ?? null;
  const edition = editions.find((e) => String(e.id) === titleId) ?? null;

  return {
    papers,
    papersLoading,
    papersError,
    host,
    setHost,
    paper,
    editions,
    editionsLoading,
    editionsError,
    titleId,
    setTitleId,
    edition,
    dates,
    datesLoading,
    datesError,
    date,
    setDate,
  };
}

export type Catalogue = ReturnType<typeof useCatalogue>;
