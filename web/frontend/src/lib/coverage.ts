import type { Beat, Edition, Paper } from "../types";

/**
 * Does this newspaper, or this edition, actually cover the chosen force?
 *
 * The question the interface used to leave unanswered. Nothing stopped
 * "Jharkhand Police" being paired with the Chandigarh Tribune, and the pairing
 * looks perfectly reasonable on screen — two valid selections, no complaint.
 * It just costs five minutes of extraction to discover that a Chandigarh paper
 * has no Jharkhand news in it.
 *
 * Editions are matched against the force's own place names rather than a
 * lookup table of edition IDs. "Jalandhar Tribune" covers Punjab because
 * Jalandhar is one of Punjab's places, which is a fact the beat already
 * carries. A second table would be one more thing to keep in step with the
 * first, and it would go stale the moment a publisher renamed an edition.
 */

/** Word-boundary match, so "Delhi Edition" hits but "New Delhika" would not. */
function mentions(haystack: string, needle: string): boolean {
  if (needle.length < 3) return false;
  const i = haystack.toLowerCase().indexOf(needle.toLowerCase());
  if (i < 0) return false;
  const before = haystack[i - 1];
  const after = haystack[i + needle.length];
  const isWord = (ch: string | undefined) => !!ch && /[\p{L}\p{N}]/u.test(ch);
  return !isWord(before) && !isWord(after);
}

export function paperCovers(paper: Paper, beat: Beat | null): boolean {
  if (!beat) return true;
  if (paper.national) return true;
  return (paper.states ?? []).includes(beat.state_code);
}

/** The place that made this edition a match, or "" if none did. */
export function editionMatch(edition: Edition, beat: Beat | null): string {
  if (!beat) return "";
  const name = String(edition.name ?? "");
  // Longest first, so "New Delhi" is reported rather than "Delhi".
  const places = [...(beat.places ?? [])].sort((a, b) => b.length - a.length);
  return places.find((p) => mentions(name, p)) ?? "";
}

export function splitPapers(papers: Paper[], beat: Beat | null) {
  if (!beat) return { covering: papers, other: [] as Paper[] };
  const covering = papers.filter((p) => paperCovers(p, beat));
  return { covering, other: papers.filter((p) => !covering.includes(p)) };
}

/**
 * How good a match this edition is: 0 is best.
 *
 * A state-level edition beats a district one. "Bathinda Edition" and "Haryana
 * Edition" are both correct matches, but for a state-wide beat the state
 * edition is the one to open — the first version took whichever happened to
 * come first in the publisher's list and picked Bathinda for Punjab.
 */
function editionRank(edition: Edition, beat: Beat): number {
  const hit = editionMatch(edition, beat);
  if (!hit) return 99;
  const state = beat.state.toLowerCase();
  const h = hit.toLowerCase();
  if (h === state) return 0; // "Haryana Edition"
  if (state.startsWith(h) || h.startsWith(state.split(" ")[0])) return 1; // "Himachal" for Himachal Pradesh
  if (hit === beat.state_hi) return 2;
  return 3; // a district: correct, but narrower than the beat
}

export function splitEditions(editions: Edition[], beat: Beat | null) {
  if (!beat) return { covering: editions, other: [] as Edition[] };
  const covering = editions
    .filter((e) => editionMatch(e, beat))
    .sort((a, b) => editionRank(a, beat) - editionRank(b, beat));
  return { covering, other: editions.filter((e) => !editionMatch(e, beat)) };
}

/** The edition to select when a force is chosen, or undefined if none fits. */
export function bestEdition(editions: Edition[], beat: Beat | null) {
  if (!beat) return undefined;
  return splitEditions(editions, beat).covering[0];
}

/**
 * Is there any local paper for this force at all?
 *
 * Worth its own answer rather than being folded into the warning below. The
 * beat vocabulary covers all 36 states; the newspaper catalogue covers about a
 * third of them. Picking Jharkhand Police is a perfectly sensible thing to do
 * and there is simply no Jharkhand title configured — that is a gap in the
 * catalogue, not a mistake by whoever chose it, and it should read that way.
 */
export function catalogueGap(papers: Paper[], beat: Beat | null): string {
  if (!beat) return "";
  const local = papers.filter((p) => !p.national && paperCovers(p, beat));
  if (local.length) return "";
  const national = papers.filter((p) => p.national);
  return national.length
    ? `No ${beat.state} newspaper is configured yet — only ${national
        .map((p) => p.name)
        .join(", ")} (national). Add one to the catalogue in web/app.py for proper coverage.`
    : `No newspaper covering ${beat.state} is configured yet. Add one to the catalogue in web/app.py.`;
}

export type CoverageWarning = {
  level: "none" | "edition" | "no-edition" | "paper";
  message: string;
  suggestion: string;
  /** A paper that does cover this force, offered as a one-click switch. */
  switchTo?: Paper;
};

/**
 * What to tell the user about the current combination.
 *
 * Deliberately a warning and never a block. Coverage data is a good guess, not
 * ground truth — a Chandigarh paper genuinely can carry a Jharkhand story, and
 * a publisher can add an edition we know nothing about. Refusing the run would
 * make the guess authoritative, which it has not earned.
 *
 * The `no-edition` case fires before anything is picked, and it exists because
 * the first version only spoke up once an edition was CHOSEN. Selecting
 * Himachal Pradesh Police and Dainik Tribune showed a silent, ungrouped list
 * of five Haryana and Chandigarh editions with a header promising that
 * Himachal ones were listed first. Nothing was wrong with the matching — Dainik
 * Tribune simply has no Himachal edition — but the interface only admitted it
 * after a selection it had given no reason to distrust.
 */
export function coverageWarning(
  beat: Beat | null,
  paper: Paper | undefined,
  edition: Edition | undefined,
  allEditions: Edition[],
  otherPapers: Paper[] = [],
): CoverageWarning {
  if (!beat || !paper) return { level: "none", message: "", suggestion: "" };

  const elsewhere = otherPapers.filter(
    (p) => p.host !== paper.host && !p.national && paperCovers(p, beat),
  );
  const tryInstead = elsewhere.length
    ? ` ${elsewhere.map((p) => p.name).join(" and ")} also cover${elsewhere.length === 1 ? "s" : ""} ${beat.state}.`
    : "";
  const switchTo = elsewhere[0];

  if (!paperCovers(paper, beat)) {
    return {
      level: "paper",
      message: `${paper.name} does not circulate in ${beat.state}.`,
      suggestion: `Expect few or no ${beat.label} stories.${tryInstead}`,
      switchTo,
    };
  }

  const covering = allEditions.filter((e) => editionMatch(e, beat));

  if (allEditions.length > 0 && covering.length === 0) {
    return {
      level: "no-edition",
      message: `No ${paper.name} edition is named for a ${beat.state} district.`,
      suggestion: `Its ${allEditions.length} edition${allEditions.length === 1 ? "" : "s"} may still carry ${beat.state} news — the name is all we can go on.${tryInstead}`,
      switchTo,
    };
  }

  if (edition && !editionMatch(edition, beat)) {
    return {
      level: "edition",
      message: `“${edition.name}” is not a ${beat.state} edition.`,
      suggestion: `${covering.map((e) => `“${e.name}”`).join(" or ")} would match ${beat.state}.`,
    };
  }

  return { level: "none", message: "", suggestion: "" };
}
