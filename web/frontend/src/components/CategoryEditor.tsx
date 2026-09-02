import { Check, Pencil, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { CategoryRow } from "../types";
import styles from "./CategoryEditor.module.css";

/**
 * Correct one article's category and tags, in place.
 *
 * The counterpart to the review queue, and both are needed: that screen fixes
 * the category LIST, this fixes one ARTICLE. Merging two sloppy entries does
 * nothing for a story the classifier filed under the wrong one.
 *
 * The category field is a free-text input backed by a datalist rather than a
 * select, because the whole design is that categories are not a fixed list. You
 * pick an existing one when it fits and type a new one when it does not, and
 * the backend normalises either the same way.
 *
 * A correction here is marked as made by a person and survives every later
 * bulk relabel — without that, the next overnight run would quietly undo it.
 */
export function CategoryEditor({
  uid,
  category,
  tags,
  source,
  options,
  onSaved,
}: {
  uid: string;
  category: string;
  tags: string[];
  source?: string;
  options: CategoryRow[];
  onSaved: (category: string, tags: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [cat, setCat] = useState(category);
  const [tagText, setTagText] = useState(tags.join(", "));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    setBusy(true);
    setErr("");
    try {
      const nextTags = tagText
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      const r = await api.setArticleCategory(uid, {
        category: cat.trim(),
        tags: nextTags,
      });
      onSaved(r.article.category, r.article.tags);
      setOpen(false);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        className={styles.trigger}
        onClick={() => {
          setCat(category);
          setTagText(tags.join(", "));
          setErr("");
          setOpen(true);
        }}
        title={
          source === "human"
            ? "Category set by hand — bulk relabelling leaves it alone"
            : "Correct the category or tags for this article"
        }
      >
        <Pencil size={11} />
        {source === "human" ? "edited" : "edit category"}
      </button>
    );
  }

  return (
    <div className={styles.editor}>
      <div className={styles.field}>
        <label htmlFor={`cat-${uid}`}>Category</label>
        <input
          id={`cat-${uid}`}
          list="known-categories"
          value={cat}
          placeholder="pick one, or type a new one"
          onChange={(e) => setCat(e.target.value)}
          autoFocus
        />
      </div>
      <div className={styles.field}>
        <label htmlFor={`tags-${uid}`}>Keywords</label>
        <input
          id={`tags-${uid}`}
          value={tagText}
          placeholder="comma separated"
          onChange={(e) => setTagText(e.target.value)}
        />
      </div>

      {/* Shared by every editor on the page; the browser dedupes by id. */}
      <datalist id="known-categories">
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label} ({o.n})
          </option>
        ))}
      </datalist>

      <div className={styles.actions}>
        <button type="button" className={styles.save} onClick={save} disabled={busy}>
          <Check size={12} /> Save
        </button>
        <button
          type="button"
          className={styles.cancel}
          onClick={() => setOpen(false)}
          disabled={busy}
        >
          <X size={12} /> Cancel
        </button>
        {/* Clearing is a decision a person is allowed to make, so it is a
            visible action rather than something you achieve by emptying a
            field and hoping. */}
        <button
          type="button"
          className={styles.cancel}
          onClick={() => setCat("")}
          disabled={busy}
          title="Clear the category, leaving the article unfiled"
        >
          Clear
        </button>
      </div>
      {err && <p className={styles.err}>{err}</p>}
    </div>
  );
}
