// Deliberately small subset: no raw HTML, links, images, or attributes.
export function renderMarkdown(text) {
  const escaped = String(text ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  // Underscore emphasis only at word boundaries: LLM_FAST_API_KEY / user_id stay literal.
  const inline = (value) => value.replace(
    /`([^`\n]+)`|\*\*(.+?)\*\*|(?<![\p{L}\p{N}_])__(.+?)__(?![\p{L}\p{N}_])|\*([^*\n]+)\*|(?<![\p{L}\p{N}_])_([^_\n]+)_(?![\p{L}\p{N}_])/gu,
    (match, code, bold, boldUnder, italic, italicUnder) => {
      if (code !== undefined) return `<code>${code}</code>`;
      if (bold !== undefined || boldUnder !== undefined) return `<strong>${inline(bold ?? boldUnder)}</strong>`;
      return `<em>${inline(italic ?? italicUnder)}</em>`;
    }
  );
  const tableCells = (line) => {
    const cells = [""];
    const value = line.trim();
    for (let i = 0; i < value.length; i++) {
      if (value[i] === "\\" && i + 1 < value.length) {
        const next = value[++i];
        cells[cells.length - 1] += next === "|" ? next : "\\" + next;
      } else if (value[i] === "|") {
        cells.push("");
      } else {
        cells[cells.length - 1] += value[i];
      }
    }
    if (cells.length === 1) return null;
    if (cells[0] === "") cells.shift();
    if (cells[cells.length - 1] === "") cells.pop();
    return cells.map((cell) => cell.trim());
  };
  const blocks = [];
  let list = null;
  let quote = null;
  const lines = escaped.replace(/\r\n?/g, "\n").split("\n");
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    const header = tableCells(line);
    const delimiter = tableCells(lines[index + 1] ?? "");
    if (header?.length && delimiter?.length === header.length
        && delimiter.every((cell) => /^:?-+:?$/.test(cell))) {
      list = null;
      quote = null;
      const row = (cells, tag) => `<tr>${header.map((_, column) =>
        `<${tag}>${inline(cells[column] ?? "")}</${tag}>`).join("")}</tr>`;
      const rows = [];
      index++;
      while (index + 1 < lines.length) {
        const next = lines[index + 1];
        if (/^\s*(?:&gt; |[-+*]\s|\d+\.\s)/.test(next)) break;
        const cells = tableCells(next);
        if (!cells) break;
        rows.push(row(cells, "td"));
        index++;
      }
      blocks.push(`<table><thead>${row(header, "th")}</thead><tbody>${rows.join("")}</tbody></table>`);
      continue;
    }
    const quoted = line.match(/^&gt; (.*)$/);
    if (quoted) {
      list = null;
      if (!quote) {
        quote = { tag: "blockquote", items: [] };
        blocks.push(quote);
      }
      quote.items.push(inline(quoted[1]));
      continue;
    }
    quote = null;
    const item = line.match(/^\s*(?:([-+*])|\d+\.)\s+(.+)$/);
    if (item) {
      const tag = item[1] ? "ul" : "ol";
      if (!list || list.tag !== tag) {
        list = { tag, items: [] };
        blocks.push(list);
      }
      list.items.push(`<li>${inline(item[2])}</li>`);
    } else {
      list = null;
      blocks.push(inline(line));
    }
  }
  return blocks.map((block) => typeof block === "string"
    ? block : `<${block.tag}>${block.items.join(block.tag === "blockquote" ? "<br>" : "")}</${block.tag}>`).join("<br>");
}
