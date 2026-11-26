/**
 * `token` typed at a textarea's caret: it replaces the selection between `start` and `end`
 * (equal when nothing is selected), and the caret lands right after it. The caller writes
 * `text` back and puts the caret at `caret` once the textarea has re-rendered.
 */
export function insertAtCaret(
  value: string,
  start: number,
  end: number,
  token: string,
): { text: string; caret: number } {
  return { text: value.slice(0, start) + token + value.slice(end), caret: start + token.length }
}
