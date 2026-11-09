import DOMPurify from "dompurify";
import MarkdownIt from "markdown-it";
import { katex } from "@mdit/plugin-katex";
import "katex/dist/katex.min.css";

interface RenderEnvironment {
  highlightTerm?: string;
}

export type MarkdownHighlighter = (source: string, language: string) => string;
export type MarkdownRenderer = (source: string, highlightTerm?: string) => string;
type MarkdownItInstance = InstanceType<typeof MarkdownIt>;
const SAFE_BREAK_PATTERN = /^<br(?:\s*\/)?>/i;

function renderHighlightedText(markdown: MarkdownItInstance, value: string, term: string): string {
  const needle = term.trim();
  if (!needle) return markdown.utils.escapeHtml(value);

  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  let output = "";
  let cursor = 0;
  let match = pattern.exec(value);

  while (match) {
    output += markdown.utils.escapeHtml(value.slice(cursor, match.index));
    output += `<mark>${markdown.utils.escapeHtml(match[0])}</mark>`;
    cursor = match.index + match[0].length;
    if (match[0].length === 0) pattern.lastIndex += 1;
    match = pattern.exec(value);
  }

  return output + markdown.utils.escapeHtml(value.slice(cursor));
}

export function createMarkdownRenderer(highlight?: MarkdownHighlighter): MarkdownRenderer {
  const markdown = new MarkdownIt({
    breaks: false,
    html: false,
    linkify: true,
    typographer: false,
    highlight: highlight ? (source, language) => highlight(source, language) : undefined,
  });

  markdown.inline.ruler.before("html_inline", "safe_br", (state, silent) => {
    if (state.src.charCodeAt(state.pos) !== 0x3c) return false;

    const match = SAFE_BREAK_PATTERN.exec(state.src.slice(state.pos, state.posMax));
    if (!match) return false;

    if (!silent) state.push("hardbreak", "br", 0);
    state.pos += match[0].length;
    return true;
  });

  markdown.use(katex, {
    delimiters: "all",
    mathFence: true,
    output: "htmlAndMathml",
    strict: "ignore",
    throwOnError: false,
    trust: false,
  });

  markdown.renderer.rules.text = (tokens, index, _options, env) =>
    renderHighlightedText(
      markdown,
      tokens[index].content,
      (env as RenderEnvironment | undefined)?.highlightTerm ?? ""
    );

  return (source, highlightTerm = "") => {
    const rendered = markdown.render(source, { highlightTerm });
    return DOMPurify.sanitize(rendered);
  };
}

const defaultRenderer = createMarkdownRenderer();

export function renderMarkdown(source: string, highlightTerm = ""): string {
  return defaultRenderer(source, highlightTerm);
}
