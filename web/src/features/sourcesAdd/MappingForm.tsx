/*
 * Map the file's columns onto the envelope, name the channel, and describe the
 * declared fields. The required slots (time, id, text) are called out; the rest
 * become typed, documented fields that the lens engine reads. Every control is
 * labelled and keyboard-reachable.
 */
import { type ReactNode } from "react";
import { Field, Input, Select } from "@/ui";
import type { FieldType } from "@/lens";
import type { ParseResult } from "./parse";
import {
  ENV_SLOTS,
  withSlot,
  type EnvSlot,
  type Mapping,
  type MappingProblem,
} from "./envelope";
import styles from "./Add.module.css";

const FIELD_TYPES: FieldType[] = ["text", "integer", "number", "boolean", "timestamp", "json"];

export interface MappingFormProps {
  parsed: ParseResult;
  mapping: Mapping;
  onChange: (m: Mapping) => void;
  problems: MappingProblem[];
}

export function MappingForm({ parsed, mapping, onChange, problems }: MappingFormProps): ReactNode {
  const cols = parsed.columns;
  const colType = new Map(cols.map((c) => [c.name, c.type]));

  const setSlot = (slot: EnvSlot, value: string): void => {
    onChange(withSlot(mapping, slot, value || null, cols));
  };

  const setField = (column: string, patch: Partial<{ name: string; type: FieldType; description: string }>): void => {
    onChange({
      ...mapping,
      fields: mapping.fields.map((f) => (f.column === column ? { ...f, ...patch } : f)),
    });
  };

  return (
    <div className={styles.mapping}>
      <section className={styles.mapSection}>
        <h3 className={styles.mapHeading}>Channel</h3>
        <Field label="Name" hint="Dot.separated lower_snake_case, e.g. import.my_source.">
          <Input
            value={mapping.channelName}
            spellCheck={false}
            onChange={(e) => onChange({ ...mapping, channelName: e.target.value })}
            aria-label="Channel name"
          />
        </Field>
        <Field label="Description" hint="What this channel holds. Shown on its page.">
          <Input
            value={mapping.channelDescription}
            onChange={(e) => onChange({ ...mapping, channelDescription: e.target.value })}
            aria-label="Channel description"
          />
        </Field>
      </section>

      <section className={styles.mapSection}>
        <h3 className={styles.mapHeading}>The envelope</h3>
        <p className={styles.mapNote}>
          Every record needs a time, an id and text. The rest is optional — a thread groups records into conversations.
        </p>
        <div className={styles.slotGrid}>
          {ENV_SLOTS.map((slot) => {
            const current = mapping.slots[slot.slot] ?? "";
            const missing = slot.required && !current;
            return (
              <div key={slot.slot} className={styles.slotRow} data-missing={missing}>
                <label className={styles.slotLabel} htmlFor={`slot-${slot.slot}`}>
                  {slot.label}
                  {slot.required ? <span className={styles.req} aria-hidden>*</span> : null}
                </label>
                <Select
                  id={`slot-${slot.slot}`}
                  value={current}
                  onChange={(e) => setSlot(slot.slot, e.target.value)}
                  aria-label={`Column for ${slot.label}`}
                >
                  <option value="">{slot.required ? "— choose a column —" : "— none —"}</option>
                  {cols.map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.name} ({colType.get(c.name)})
                    </option>
                  ))}
                </Select>
                <span className={styles.slotHint}>{slot.description}</span>
              </div>
            );
          })}
        </div>

        {!mapping.slots.thread ? (
          <Field label="Threads" hint="With no thread column, how should records be grouped?">
            <Select
              value={mapping.threadMode}
              onChange={(e) => onChange({ ...mapping, threadMode: e.target.value as Mapping["threadMode"] })}
              aria-label="Thread mode"
            >
              <option value="per_row">One thread per record</option>
              <option value="single">All records in one thread</option>
            </Select>
          </Field>
        ) : null}
      </section>

      <section className={styles.mapSection}>
        <h3 className={styles.mapHeading}>
          Declared fields <span className={styles.count}>{mapping.fields.length}</span>
        </h3>
        <p className={styles.mapNote}>
          Columns kept as typed fields. Give each a short description — the engine and the helper use it.
        </p>
        {mapping.fields.length === 0 ? (
          <p className={styles.mapNote}>No extra columns — every column is mapped to the envelope.</p>
        ) : (
          <div className={styles.fieldTable}>
            {mapping.fields.map((f) => (
              <div key={f.column} className={styles.fieldRow}>
                <span className={styles.fieldCol} title={`from column "${f.column}"`}>
                  {f.column}
                </span>
                <Input
                  className={styles.fieldNameInput}
                  value={f.name}
                  spellCheck={false}
                  onChange={(e) => setField(f.column, { name: e.target.value })}
                  aria-label={`Field name for ${f.column}`}
                />
                <Select
                  value={f.type}
                  onChange={(e) => setField(f.column, { type: e.target.value as FieldType })}
                  aria-label={`Type for ${f.column}`}
                >
                  {FIELD_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </Select>
                <Input
                  className={styles.fieldDescInput}
                  value={f.description}
                  placeholder="description"
                  onChange={(e) => setField(f.column, { description: e.target.value })}
                  aria-label={`Description for ${f.column}`}
                />
              </div>
            ))}
          </div>
        )}
      </section>

      {problems.length > 0 ? (
        <ul className={styles.problems}>
          {problems.map((p, i) => (
            <li key={i} className={styles.problem} data-level={p.level}>
              {p.message}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
