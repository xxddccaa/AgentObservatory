import { describe, expect, it } from "vitest";
import { createMarkdownRenderer, renderMarkdown } from "../markdown/render";

describe("renderMarkdown", () => {
  it("renders common Markdown while keeping raw HTML inert", () => {
    const html = renderMarkdown("## Result\n\n- **one**\n- `two`\n\n<script>alert(1)</script>");

    expect(html).toContain("<h2>Result</h2>");
    expect(html).toContain("<strong>one</strong>");
    expect(html).toContain("<code>two</code>");
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });

  it("renders both bracket and dollar display math", () => {
    const html = renderMarkdown(String.raw`
\[
\mathbb E[h(x_k)h(x_k)^\top]
\]

$$
\boxed{\text{EKF update}}
$$
`);

    expect(html.match(/class="katex-display"/g)).toHaveLength(2);
    expect(html).toContain("mathbb");
    expect(html).toContain("boxed");
  });

  it("highlights prose without corrupting formula output", () => {
    const html = renderMarkdown("Result: $x_k + 1$.", "result");

    expect(html).toContain("<mark>Result</mark>");
    expect(html).toContain('class="katex"');
  });

  it("creates an isolated renderer with the same safety and search behavior", () => {
    const renderer = createMarkdownRenderer();
    const html = renderer("## Result\n\nResult: $x_k + 1$.\n\n<script>alert(1)</script>", "result");

    expect(html).toContain("<h2><mark>Result</mark></h2>");
    expect(html).toContain("<mark>Result</mark>");
    expect(html).toContain('class="katex"');
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });

  it("passes fenced code to a custom highlighter while keeping math fences intact", () => {
    const renderer = createMarkdownRenderer((source, language) => {
      return `<pre data-language="${language}">${source}</pre>`;
    });
    const html = renderer('```python\nprint("hello")\n```\n\n```math\nx^2\n```');

    expect(html).toContain('<pre data-language="python">');
    expect(html).toContain('class="katex-display"');
  });
});
