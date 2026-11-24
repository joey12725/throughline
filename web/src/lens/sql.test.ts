import { describe, expect, test } from "vitest";
import {
  EXTENDED,
  LensSqlError,
  NARROW,
  builtinProbes,
  channelView,
  channelViewName,
  chooseGrain,
  compileView,
  freeAlias,
  grainSeconds,
  lit,
  quoteIdent,
  renderParam,
  sampleModulo,
  scanParams,
  sqlFns,
  substituteParams,
  wrapStatement,
} from "./sql";
import type { ParamValue, ViewSpec } from "./types";

const text = (value: unknown): ParamValue => ({ type: "text", value });

describe("identifiers", () => {
  test("plain lower-case names stay bare, everything else is quoted", () => {
    expect(quoteIdent("weight_kg")).toBe("weight_kg");
    expect(quoteIdent("Mixed Case")).toBe('"Mixed Case"');
    expect(quoteIdent("to")).toBe('"to"');
    expect(quoteIdent("references")).toBe('"references"');
    expect(quoteIdent("position")).toBe('"position"');
    expect(quoteIdent("end")).toBe('"end"');
    expect(quoteIdent('a"b')).toBe('"a""b"');
    expect(quoteIdent("1st")).toBe('"1st"');
    expect(quoteIdent("x; DROP TABLE y")).toBe('"x; DROP TABLE y"');
    expect(() => quoteIdent("")).toThrow(LensSqlError);
  });

  test("channel view naming follows tl.refresh_channel and prefers bootstrap", () => {
    expect(channelViewName("health.oura.daily")).toBe("api.ch_health_oura_daily");
    expect(channelViewName("visits")).toBe("api.ch_visits");
    expect(channelViewName("Odd-Name.x")).toBe('api."ch_Odd-Name_x"');
    expect(channelView({ name: "a.b", view: "api.ch_a_b" })).toBe("api.ch_a_b");
    expect(channelView({ name: "a.b", view: "ch_a_b" })).toBe("api.ch_a_b");
    expect(channelView({ name: "a.b", view: 'api."ch_Weird one"' })).toBe('api."ch_Weird one"');
    // A view outside schema api is never trusted: fall back to the convention.
    expect(channelView({ name: "a.b", view: "tl.messages" })).toBe("api.ch_a_b");
    expect(channelView({ name: "a.b", view: "" })).toBe("api.ch_a_b");
  });

  test("freeAlias avoids collisions", () => {
    expect(freeAlias("n", ["bucket"])).toBe("n");
    expect(freeAlias("n", ["bucket", "n"])).toBe("n_2");
  });
});

describe("literals", () => {
  test("text doubles quotes and drops NUL", () => {
    expect(lit.text("it's")).toBe("'it''s'");
    expect(lit.text("a\u0000b")).toBe("'ab'");
    expect(lit.text("back\\slash")).toBe("'back\\slash'");
    expect(lit.text(null)).toBe("NULL");
    expect(lit.text("'; DROP TABLE tl.messages; --")).toBe("'''; DROP TABLE tl.messages; --'");
  });

  test("numbers", () => {
    expect(lit.int(42)).toBe("42");
    expect(lit.int(-3)).toBe("(-3)");
    expect(lit.int("17")).toBe("17");
    expect(() => lit.int(1.5)).toThrow(LensSqlError);
    expect(() => lit.int("1; SELECT 1")).toThrow(LensSqlError);
    expect(lit.number(2.5)).toBe("2.5");
    expect(lit.number(100)).toBe("100.0");
    expect(lit.number(-0.25)).toBe("(-0.25)");
    expect(lit.number(Number.NaN)).toBe("NULL");
    expect(lit.number(Number.POSITIVE_INFINITY)).toBe("NULL");
    expect(() => lit.number("1 OR 1=1")).toThrow(LensSqlError);
    expect(lit.bool(true)).toBe("true");
    expect(() => lit.bool("yes")).toThrow(LensSqlError);
  });

  test("timestamps keep microseconds and reject anything that is not a time", () => {
    expect(lit.timestamp("2026-11-20T22:22:21.300001Z")).toBe("timestamptz '2026-11-20T22:22:21.300001Z'");
    expect(lit.timestamp("2026-11-20")).toBe("timestamptz '2026-11-20T00:00:00Z'");
    expect(lit.timestamp("2026-11-20 08:30")).toBe("timestamptz '2026-11-20T08:30Z'");
    expect(lit.timestamp("2026-11-20T08:30:00+02:00")).toBe("timestamptz '2026-11-20T08:30:00+02:00'");
    expect(lit.timestamp(new Date("2026-01-02T03:04:05Z"))).toBe("timestamptz '2026-01-02T03:04:05.000Z'");
    expect(lit.timestamp("infinity")).toBe("timestamptz 'infinity'");
    expect(() => lit.timestamp("2026-01-01'; DROP")).toThrow(LensSqlError);
    expect(() => lit.timestamp("yesterday")).toThrow(LensSqlError);
    expect(() => lit.timestamp("2026-13-45")).toThrow(LensSqlError);
    expect(lit.date("2026-02-28")).toBe("date '2026-02-28'");
    expect(() => lit.date("2026-02-28 x")).toThrow(LensSqlError);
  });

  test("intervals, zones, grains, arrays", () => {
    expect(lit.interval(90)).toBe("interval '90 seconds'");
    expect(lit.interval("15 minutes")).toBe("interval '15 minutes'");
    expect(lit.interval("P1DT2H")).toBe("interval 'P1DT2H'");
    expect(() => lit.interval("1 day'; --")).toThrow(LensSqlError);
    expect(lit.tz("America/New_York")).toBe("'America/New_York'");
    expect(lit.tz(null)).toBe("'UTC'");
    expect(() => lit.tz("UTC'; SELECT")).toThrow(LensSqlError);
    expect(lit.grain("week")).toBe("'week'");
    expect(() => lit.grain("fortnight")).toThrow(LensSqlError);
    expect(lit.textArray(["a", "b'c"])).toBe("ARRAY['a', 'b''c']::text[]");
    expect(lit.textArray([])).toBe("ARRAY[]::text[]");
    expect(lit.intArray([1, -2])).toBe("ARRAY[1, (-2)]::bigint[]");
    expect(lit.numberArray([0.5])).toBe("ARRAY[0.5]::float8[]");
    expect(renderParam({ type: "text[]", value: ["x"] })).toBe("ARRAY['x']::text[]");
    expect(() => renderParam({ type: "text[]", value: "x" })).toThrow(LensSqlError);
  });
});

describe("parameter scanning", () => {
  const names = (sql: string) => scanParams(sql).map((r) => r.name);

  test("finds parameters and skips casts", () => {
    expect(names("SELECT x::int FROM t WHERE a >= :from AND b < :to")).toEqual(["from", "to"]);
    expect(names("SELECT (:lo)::float8, :hi::numeric, x::text")).toEqual(["lo", "hi"]);
    expect(names("SELECT a:=1")).toEqual([]);
  });

  test("skips string literals, including doubled quotes and E-strings", () => {
    expect(names("SELECT ':from', 'it''s :to', :tz")).toEqual(["tz"]);
    expect(names("SELECT E'a\\':from', :x")).toEqual(["x"]);
    // Without E a backslash is an ordinary character and the quote ends the string.
    expect(names("SELECT 'a\\', :y")).toEqual(["y"]);
    expect(names("SELECT to_char(t, 'HH24:MI:SS') FROM x WHERE t > :from")).toEqual(["from"]);
  });

  test("skips quoted identifiers, comments and dollar quotes", () => {
    expect(names('SELECT ":from" FROM t WHERE x = :a')).toEqual(["a"]);
    expect(names("SELECT 1 -- :from here\n, :b")).toEqual(["b"]);
    expect(names("SELECT /* :x /* nested :y */ still :z */ :c")).toEqual(["c"]);
    expect(names("SELECT $$ :from $$, $tag$ :to $tag$, :d")).toEqual(["d"]);
    expect(names("SELECT $q$ it's :p $q$ || :e")).toEqual(["e"]);
  });

  test("array slices are not parameters", () => {
    expect(names("SELECT arr[1:n], (arr)[2:to] FROM t WHERE a = :to")).toEqual(["to"]);
  });

  test("an unterminated string swallows the rest rather than inventing parameters", () => {
    expect(names("SELECT 'oops :from")).toEqual([]);
  });
});

describe("substitution", () => {
  test("single pass: a value containing a parameter name or a quote is never re-read", () => {
    const out = substituteParams("SELECT :a, :b", { a: text(":b ' -- "), b: text("x") });
    expect(out.sql).toBe("SELECT ':b '' -- ', 'x'");
    expect(out.used.sort()).toEqual(["a", "b"]);
  });

  test("hostile strings stay inside their literal", () => {
    const hostile = ["'; DROP TABLE api.messages; --", "\\'; SELECT 1; --", "$$; SELECT pg_sleep(10); $$", "a'/*", "*/ OR 1=1 --", "\u0000'", "x\n-- y", "':from"];
    for (const h of hostile) {
      const out = substituteParams("SELECT * FROM api.messages WHERE content = :q AND happened_at >= :from", {
        q: text(h),
        from: { type: "timestamp", value: "2026-01-01" },
      });
      // After substitution nothing is left to substitute, and the value is one literal:
      // scanning the result finds no parameters even though the text may contain ":from".
      expect(scanParams(out.sql)).toEqual([]);
      const literal = lit.text(h);
      expect(out.sql).toBe(`SELECT * FROM api.messages WHERE content = ${literal} AND happened_at >= timestamptz '2026-01-01T00:00:00Z'`);
      // Every quote inside the literal is doubled.
      expect(literal.slice(1, -1).replace(/''/g, "")).not.toContain("'");
    }
  });

  test("unknown names are reported and left alone", () => {
    const out = substituteParams("SELECT :known, :unknown", { known: text("k") });
    expect(out.sql).toBe("SELECT 'k', :unknown");
    expect(out.unresolved).toEqual(["unknown"]);
  });

  test("prototype names are not parameters", () => {
    const out = substituteParams("SELECT :constructor, :toString", {});
    expect(out.unresolved.sort()).toEqual(["constructor", "toString"]);
  });
});

describe("grain", () => {
  test("stays under the bucket budget", () => {
    const cases: [string, string][] = [
      ["2026-11-20T00:00:00Z", "2026-11-20T06:00:00Z"],
      ["2026-11-01", "2026-11-20"],
      ["2026-01-01", "2026-11-20"],
      ["2017-09-01", "2026-11-20"],
      ["1990-01-01", "2026-11-20"],
      ["0500-01-01", "2026-11-20"],
    ];
    for (const [from, to] of cases) {
      for (const budget of [1500, 200, 40]) {
        const g = chooseGrain(from, to, { budget });
        const buckets = (Date.parse(to) - Date.parse(from)) / 1000 / grainSeconds(g);
        if (g !== "year") expect(buckets).toBeLessThanOrEqual(budget);
      }
    }
    expect(chooseGrain("2026-11-20T00:00:00Z", "2026-11-20T06:00:00Z")).toBe("minute");
    expect(chooseGrain("2026-01-01", "2026-11-20")).toBe("day");
    expect(chooseGrain("2026-10-01", "2026-11-20")).toBe("hour");
    expect(chooseGrain("2017-09-01", "2026-11-20")).toBe("week");
    expect(chooseGrain("2017-09-01", "2026-11-20", { budget: 150 })).toBe("month");
    expect(chooseGrain(null, null)).toBe("month");
    expect(chooseGrain("2026-11-01", "2026-11-02", { min: "day" })).toBe("day");
  });
});

describe("compileView", () => {
  const spec: ViewSpec = {
    v: 1,
    title: "t",
    why: "w",
    lens: "timeseries",
    source: {
      sql: "SELECT date_trunc(:grain, happened_at, :tz) AS bucket, count(*) AS n FROM api.ch_x WHERE happened_at >= :from AND happened_at < :to AND id % :k = 0 GROUP BY 1",
      row_cap: 2000,
      params: { k: { type: "int", value: 3 } },
    },
    encode: { x: "bucket", y: "n" },
    options: { extent: ["2020-01-01T00:00:00Z", "2026-01-01T00:00:00Z"] },
    provenance: { by: "engine", channel: "x", sampled: false },
  };

  test("unbounded scope uses the data's extent for the grain", () => {
    const c = compileView(spec);
    expect(c.grain).toBe("week");
    expect(c.unresolved).toEqual([]);
    expect(c.sql).toContain("date_trunc('week', happened_at, 'UTC')");
    expect(c.sql).toContain("happened_at >= timestamptz '-infinity' AND happened_at < timestamptz 'infinity'");
    expect(c.sql).toContain("id % 3 = 0");
  });

  test("a narrow scope refines the grain; locals can be overridden", () => {
    const c = compileView(spec, { from: "2025-06-01", to: "2025-06-02", tz: "Europe/Berlin", params: { k: 1 } });
    expect(c.grain).toBe("minute");
    expect(c.sql).toContain("'Europe/Berlin'");
    expect(c.sql).toContain("id % 1 = 0");
  });

  test("a hostile zone or local value is refused, not passed through", () => {
    expect(() => compileView(spec, { tz: "UTC'); DROP" })).toThrow(LensSqlError);
    expect(() => compileView(spec, { params: { k: "1; DROP" } })).toThrow(LensSqlError);
  });

  test("bucket budget and minimum grain from the spec are honoured", () => {
    const c = compileView({ ...spec, options: { ...spec.options, bucket_budget: 100, min_grain: "day" } });
    expect(c.grain).toBe("month");
  });
});

describe("wrapStatement", () => {
  test("wraps one statement, tolerating a trailing semicolon and a line comment", () => {
    expect(wrapStatement("SELECT 1 AS a;")).toBe("(\nSELECT 1 AS a\n) AS q");
    expect(wrapStatement("SELECT 1 AS a -- note")).toBe("(\nSELECT 1 AS a -- note\n) AS q");
    expect(wrapStatement("SELECT ';' AS a")).toBe("(\nSELECT ';' AS a\n) AS q");
  });
  test("refuses empty text and more than one statement", () => {
    expect(wrapStatement("")).toBeNull();
    expect(wrapStatement("-- nothing")).toBeNull();
    expect(wrapStatement("SELECT 1; SELECT 2")).toBeNull();
  });
});

describe("capability table", () => {
  test("extended formulations call the builtin, narrow ones avoid it", () => {
    const ext = sqlFns(EXTENDED);
    const nar = sqlFns(NARROW);
    expect(ext.floor("x")).toBe("floor(x)");
    expect(nar.floor("x")).not.toMatch(/floor\(/);
    expect(ext.widthBucket("x", "0", "10", "5")).toMatch(/width_bucket\(/);
    expect(nar.widthBucket("x", "0", "10", "5")).not.toMatch(/width_bucket|floor\(/);
    expect(ext.lag("x", { orderBy: "id" })).toBe("lag(x) OVER (ORDER BY id)");
    expect(nar.lag("x", { partitionBy: "g", orderBy: "id" })).toBe("max(x) OVER (PARTITION BY g ORDER BY id ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING)");
    expect(ext.percentile(0.5, "x")).toMatch(/percentile_cont\(0\.5\) WITHIN GROUP/);
    expect(nar.percentile(0.5, "x")).toBeNull();
    expect(nar.ln("x")).toBeNull();
    expect(nar.objectKeys("j", "k")).toBeNull();
    expect(ext.arrayElementsText("m.tags", "e").join).toMatch(/jsonb_array_elements_text/);
    expect(nar.arrayElementsText("m.tags", "e").join).toBe("generate_series(0, 49) AS e(i)");
    expect(nar.dateBin(900, "t")).not.toMatch(/date_bin/);
    expect(ext.dateBin(900, "t")).toMatch(/^date_bin\(interval '900 seconds'/);
  });

  test("no narrow formulation uses a function outside the narrow list", () => {
    const allowed = new Set(NARROW.names);
    for (const probe of builtinProbes(NARROW)) {
      const calls = [...probe.sql.matchAll(/\b([a-z_][a-z0-9_]*)\s*\(/g)].map((m) => m[1] as string);
      const keywords = new Set(["over", "filter", "in", "from", "as", "by", "when", "and", "then", "else", "select", "where", "on", "between", "numeric", "lateral", "case", "e", "ix"]);
      for (const c of calls) {
        if (keywords.has(c)) continue;
        expect(allowed.has(c), `${probe.name} calls ${c}: ${probe.sql}`).toBe(true);
      }
    }
  });

  test("sampleModulo", () => {
    expect(sampleModulo(1000, 20000)).toBe(1);
    expect(sampleModulo(300000, 20000)).toBe(15);
  });
});
