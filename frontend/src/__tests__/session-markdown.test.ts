import { describe, expect, it, vi } from "vitest";
import {
  createSessionMarkdownRenderer,
  type SessionCodeHighlighter,
} from "../markdown/session";

function fakeHighlighter(): SessionCodeHighlighter & {
  codeToHtml: ReturnType<typeof vi.fn>;
} {
  return {
    codeToHtml: vi.fn((source: string, options: { lang: string; theme: string }) => {
      return `<pre class="fake-shiki" data-lang="${options.lang}" data-theme="${options.theme}">${source}</pre>`;
    }),
  };
}

describe("session Markdown renderer", () => {
  it("highlights supported code after sanitization and keeps formulas and prose search working", async () => {
    const renderer = await createSessionMarkdownRenderer();
    const html = renderer.render(
      String.raw`Result: $x_k + 1$.

~~~javascript
const answer = "playwright";
~~~

$$
E = mc^2
$$`,
      "result"
    );

    expect(html).toContain("<mark>Result</mark>");
    expect(html).toContain('class="katex"');
    expect(html).toContain('class="katex-display"');
    expect(html).toContain("shiki github-dark");
    expect(html).toMatch(/style="color:#[0-9A-F]{6}"/i);
    expect(html).toContain("const");
    expect(html).toContain("playwright");
  });

  it("keeps raw HTML and executable code inert", async () => {
    const renderer = await createSessionMarkdownRenderer();
    const html = renderer.render(
      '<img src=x onerror="alert(1)">\n\n```html\n<script>alert(2)</script>\n```\n\n[bad](javascript:alert(3))'
    );

    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
    expect(html).not.toContain('href="javascript:alert');
    expect(html).toContain("&lt;img");
  });

  it("falls back to complete escaped text for unknown, oversized, and overlong blocks", async () => {
    const renderer = await createSessionMarkdownRenderer();
    const unknown = renderer.render("```not-a-language\n<script>alert(1)</script>\n```");
    const oversized = renderer.render(`\`\`\`python\n${"x".repeat(64 * 1024 + 1)}\n\`\`\``);
    const manyLines = renderer.render(`\`\`\`js\n${"const x = 1;\n".repeat(1001)}\`\`\``);

    expect(unknown).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(unknown).not.toContain('class="shiki');
    expect(oversized).not.toContain('class="shiki');
    expect(oversized).toContain("x".repeat(64 * 1024 + 1));
    expect(manyLines).not.toContain('class="shiki');
    expect(manyLines).toContain("const x = 1;");
  });

  it("normalizes aliases and caches by language and source until cleared", async () => {
    const highlighter = fakeHighlighter();
    const renderer = await createSessionMarkdownRenderer(async () => highlighter);
    const source = '```py\nprint("cached")\n```';

    renderer.render(source);
    renderer.render(source);
    renderer.render('```js\nconsole.log("different language")\n```');

    expect(highlighter.codeToHtml).toHaveBeenCalledTimes(2);
    expect(highlighter.codeToHtml.mock.calls[0][1]).toEqual({
      lang: "python",
      theme: "github-dark",
    });

    renderer.clearCache();
    renderer.render(source);
    expect(highlighter.codeToHtml).toHaveBeenCalledTimes(3);
  });

  it("evicts the least recently used entry after 128 cached blocks", async () => {
    const highlighter = fakeHighlighter();
    const renderer = await createSessionMarkdownRenderer(async () => highlighter);
    const blocks = Array.from(
      { length: 129 },
      (_, index) => `\`\`\`js\nconst value${index} = ${index};\n\`\`\``
    ).join("\n\n");

    renderer.render(blocks);
    expect(highlighter.codeToHtml).toHaveBeenCalledTimes(129);

    renderer.render("```js\nconst value1 = 1;\n```");
    expect(highlighter.codeToHtml).toHaveBeenCalledTimes(129);

    renderer.render("```js\nconst value0 = 0;\n```");
    expect(highlighter.codeToHtml).toHaveBeenCalledTimes(130);
  });

  it("evicts cached blocks to respect the 4 MiB byte budget", async () => {
    const highlighter = fakeHighlighter();
    const renderer = await createSessionMarkdownRenderer(async () => highlighter);
    const payload = "x".repeat(60 * 1024);
    const block = (index: number) => `\`\`\`js\nconst block${index} = "${payload}";\n\`\`\``;
    const blocks = Array.from({ length: 72 }, (_, index) => block(index)).join("\n\n");

    renderer.render(blocks);
    const callsAfterFirstPass = highlighter.codeToHtml.mock.calls.length;
    expect(callsAfterFirstPass).toBe(72);

    renderer.render(block(0));
    expect(highlighter.codeToHtml.mock.calls.length).toBe(callsAfterFirstPass + 1);
  });

  it("falls back when highlighter initialization fails", async () => {
    const renderer = await createSessionMarkdownRenderer(async () => {
      throw new Error("highlighter unavailable");
    });
    const html = renderer.render("```python\nprint('still visible')\n```");

    expect(html).not.toContain('class="shiki');
    expect(html).toContain("print('still visible')");
  });

  it("falls back for one block without breaking other blocks", async () => {
    const highlighter = fakeHighlighter();
    highlighter.codeToHtml.mockImplementation((source: string, options: { lang: string }) => {
      if (options.lang === "python") throw new Error("grammar failed");
      return `<pre class="fake-shiki">${source}</pre>`;
    });
    const renderer = await createSessionMarkdownRenderer(async () => highlighter);
    const html = renderer.render("```python\nbroken()\n```\n\n```js\nconst ok = true;\n```");

    expect(html).toContain("broken()");
    expect(html).toContain('class="fake-shiki"');
    expect(highlighter.codeToHtml).toHaveBeenCalledTimes(2);
  });
});
