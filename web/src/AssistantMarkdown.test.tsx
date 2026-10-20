import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AssistantMarkdown from "./AssistantMarkdown";

const render = (markdown: string) => renderToStaticMarkup(<AssistantMarkdown>{markdown}</AssistantMarkdown>);
const withoutTokenSpans = (html: string) => html.replace(/<\/?span\b[^>]*>/g, "");

describe("assistant Markdown", () => {
  it("renders SQL explanations with CommonMark and GFM structure", () => {
    const html = render([
      "## Query details",
      "",
      "**Latest** record with *one* `session_id` and ~~old~~ rows.",
      "",
      "- Filter by date",
      "- Group by session",
      "",
      "1. Read the table",
      "2. Keep the latest row",
      "",
      "> The limit is applied last.",
      "",
      "| Column | Meaning |",
      "| --- | --- |",
      "| session_id | Session identifier |",
      "",
      "```sql",
      "SELECT session_id FROM events;",
      "```",
    ].join("\n"));
    expect(html).toContain("<h2>Query details</h2>");
    expect(html).toContain("<strong>Latest</strong>");
    expect(html).toContain("<em>one</em>");
    expect(html).toContain("<code>session_id</code>");
    expect(html).toContain("<del>old</del>");
    expect(html).toContain("<ul>");
    expect(html).toContain("<ol>");
    expect(html).toContain("<blockquote>");
    expect(html).toContain('class="assistant-table-scroll"><table>');
    expect(html).toContain("<th>Column</th>");
    expect(withoutTokenSpans(html)).toContain('<pre><code class="language-sql">SELECT session_id FROM events;\n</code></pre>');
    expect(html).toContain('<span class="hljs-keyword">SELECT</span>');
  });

  it("keeps an unfinished streaming code fence as code until completion", () => {
    const start = "Here is the query:\n\n```sql\nSELECT id";
    expect(withoutTokenSpans(render(start))).toContain('<pre><code class="language-sql">SELECT id\n</code></pre>');
    const extended = `${start},\n  created_at\nFROM events;`;
    expect(withoutTokenSpans(render(extended))).toContain("SELECT id,\n  created_at\nFROM events;\n</code></pre>");
    expect(render(extended)).toBe(render(`${extended}\n\`\`\``));
  });

  it("uses SQL grammar tokens without changing SQL source text", () => {
    const source = "SELECT count(*), 3 FROM entries WHERE note = 'sample' AND ready = true; -- comment\n";
    const html = render(`\`\`\`sql\n${source}\`\`\``);
    expect(html).toContain('<span class="hljs-keyword">SELECT</span>');
    expect(html).toContain('<span class="hljs-built_in">count</span>');
    expect(html).toContain('<span class="hljs-number">3</span>');
    expect(html).toContain('<span class="hljs-string">&#x27;sample&#x27;</span>');
    expect(html).toContain('<span class="hljs-literal">true</span>');
    expect(html).toContain('<span class="hljs-comment">-- comment</span>');
    expect(withoutTokenSpans(html)).toContain(renderToStaticMarkup(<code className="language-sql">{source}</code>));
  });

  it.each(["postgresql", "mysql", "sqlite"])("highlights SQL fence alias %s", (language) => {
    expect(render(`\`\`\`${language}\nSELECT 1;\n\`\`\``)).toContain('<span class="hljs-keyword">SELECT</span>');
  });

  it("leaves inline code and other code languages untouched", () => {
    const html = render("`SELECT 3`\n\n```javascript\nconst answer = 'SELECT 3';\n```");
    expect(html).toContain("<code>SELECT 3</code>");
    expect(html).not.toContain("hljs-");
    expect(html).toContain('class="language-javascript"');
  });

  it("preserves incomplete strings and comments while streaming SQL", () => {
    const source = "SELECT 'incomplete string'; -- partial comment";
    for (let length = 1; length <= source.length; length++) {
      const partial = source.slice(0, length);
      const html = withoutTokenSpans(render(`\`\`\`sql\n${partial}`));
      expect(html).toContain(renderToStaticMarkup(<code className="language-sql">{`${partial}\n`}</code>));
    }
  });

  it("does not create HTML elements or request images from untrusted output", () => {
    const html = render([
      '<script>alert("unsafe")</script>',
      "",
      '<iframe src="https://untrusted.example/embed"></iframe>',
      "",
      '<img src="https://untrusted.example/html.png" onerror="alert(1)">',
      "",
      '![Diagram](https://untrusted.example/markdown.png "remote image")',
      "",
      "![Embedded](data:image/svg+xml,unsafe)",
    ].join("\n"));
    expect(html).not.toMatch(/<(?:script|iframe|img|link)\b/);
    expect(html).not.toContain("untrusted.example");
    expect(html).not.toContain("onerror");
    expect(html).toContain('<span class="assistant-image-alt">Diagram</span>');
    expect(html).toContain('<span class="assistant-image-alt">Embedded</span>');
  });

  it.each([
    "javascript:alert%281%29", "data:text/html,unsafe", "file:///etc/passwd",
    "vbscript:unsafe", "/api/queries", "//untrusted.example", "#fragment",
    "javascript&#58;alert%281%29",
  ])("leaves %s as text instead of a navigable link", (url) => {
    const html = render(`[Read more](${url})`);
    expect(html).not.toContain("<a ");
    expect(html).toContain("Read more");
  });

  it.each(["https://example.com/sql", "http://example.com/sql", "mailto:hello@example.com"])("opens %s safely in a new window", (url) => {
    const html = render(`[Read more](${url})`);
    expect(html).toContain(`href="${url}"`);
    expect(html).toContain('target="_blank" rel="noopener noreferrer"');
  });

  it("keeps HTML-looking SQL literals escaped inside code", () => {
    const html = render("```sql\nSELECT '<script>alert(1)</script>';\n```");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
  });
});
