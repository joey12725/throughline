import { describe, it, expect } from "vitest";
import { curlSnippet, pythonSnippet, nodeSnippet, snippetFor, type ChannelDraft } from "./snippets";

const draft: ChannelDraft = {
  name: "import.mydata",
  plugin: "webimport",
  description: "A test channel",
  fields: [
    { name: "rating", type: "integer", description: "1-5" },
    { name: "tag", type: "text" },
  ],
};

describe("snippets", () => {
  it("curl hits the real endpoints and carries the token placeholder", () => {
    const s = curlSnippet(draft, { baseUrl: "https://host" });
    expect(s).toContain("https://host/v1/channels");
    expect(s).toContain("https://host/v1/ingest/messages");
    expect(s).toContain("Authorization: Bearer $THROUGHLINE_TOKEN");
    expect(s).toContain("Idempotency-Key");
    expect(s).toContain('"dry_run": true');
  });

  it("curl embeds a valid channel body with the declared fields", () => {
    const s = curlSnippet(draft);
    const m = s.match(/-d '(\{[\s\S]*?\})'/);
    expect(m).toBeTruthy();
    const body = JSON.parse(m![1]!);
    expect(body.name).toBe("import.mydata");
    expect(body.plugin).toBe("webimport");
    expect(body.fields.map((f: { name: string }) => f.name)).toEqual(["rating", "tag"]);
  });

  it("python references the endpoints and env token", () => {
    const s = pythonSnippet(draft);
    expect(s).toContain("/v1/channels");
    expect(s).toContain("/v1/ingest/messages");
    expect(s).toContain('os.environ["THROUGHLINE_TOKEN"]');
  });

  it("node references the endpoints and env token", () => {
    const s = nodeSnippet(draft);
    expect(s).toContain("/v1/channels");
    expect(s).toContain("/v1/ingest/messages");
    expect(s).toContain("process.env.THROUGHLINE_TOKEN");
  });

  it("snippetFor dispatches by language", () => {
    expect(snippetFor("curl", draft)).toContain("curl");
    expect(snippetFor("python", draft)).toContain("httpx");
    expect(snippetFor("node", draft)).toContain("fetch");
  });
});
